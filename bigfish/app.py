"""大肥鱼 × 微信 —— 正式入口。

    python bigfish/app.py                      # 用 config.json 里配的会话
    python bigfish/app.py --who 文件传输助手
    python bigfish/app.py --who 小麟 --who 老爸  # 多个会话
    python bigfish/app.py --dry                 # 只生成不发送

相比 run_uia.py（研究阶段的原型），这里：
  - 读 config.json，不用每次敲参数
  - 自己发出去的消息不会触发回复（除非 --allow-self）
  - 会话白名单，没登记的群不会被回复
  - 退出时打印本轮统计

前置条件：
  1. 管理员权限（UIA 热激活要写 gate 字节）
  2. 微信已登录，主窗口可见
  3. 桌面解锁
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from bigfish import scopes as SCOPES

for _s in (sys.stdout, sys.stderr):
    if _s is not None:
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bigfish.replier import ModelConfig, ModelError, call_model  # noqa: E402
from bigfish.say import (PacePolicy, SplitPolicy, plan_pacing,      # noqa: E402
                         send_parts, split_reply, split_reply_with_llm)
from bigfish.listen import (Batch, BatchMessage, ListenPolicy,     # noqa: E402
                            MessageBatcher, batch_transcript)
from bigfish.soul import Soul, load_soul                        # noqa: E402
from bigfish.watcher import Incoming, MessageWatcher            # noqa: E402
from bigfish.wechat import WeChatReader                         # noqa: E402
from bigfish.wechat_sender import (SendError, UiaSender,        # noqa: E402
                                   desktop_available, is_admin)
from bigfish import identity                                    # noqa: E402

HR = "=" * 70
#: 配置在「用户看得见」的那层，不在程序资源里（paths.py 的规范）。
from bigfish.paths import app_root as _app_root               # noqa: E402

CONFIG_PATH = os.path.join(_app_root(), "config.json")
HISTORY_LIMIT = 16

#: 分条标记。必须与 `bigfish.say.SplitPolicy.marker` 一致 ——
#: prompt 里教模型用它，分条器靠它切；两处对不上就等于没教。
SPLIT_MARK = "{{split}}"


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------
@dataclass
class AppConfig:
    """运行时配置（config.json 的 auto_reply 段）。

    ⚠ 行为类的五个字段（allow_self / group_reply / group_allow_all /
      max_reply_chars / quiet_hours）**不再是这个类上的属性**。
      它们住在 `scopes` 的三层继承里，只能用 `cfg.scope(名字, chat_id)` 取。

      为什么删掉而不是留一个"全局镜像"：镜像会漂移。
      谁哪天顺手读一次 `cfg.quiet_hours`，读到的是加载那一刻的值，
      和真正生效的值可能早就不是一回事 —— 这正是 D-61 的形状
      （两处都能跑通、都不报错，只有用户发现"我改的东西没生效"）。
      删掉之后，读错就是 AttributeError，当场看得见。
    """

    chats: list[str] = field(default_factory=list)
    interval: float = 1.2

    # ── 说法层（config.json 顶层的 say 段）──
    say_enabled: bool = True
    say_max_parts: int = 4
    say_part_chars: int = 50
    #: 长回复且没有 marker 时，让模型自己断一次（多一次模型调用，默认关）
    say_llm_split: bool = False
    #: 不同会话的并发批次数（同会话始终串行）
    say_workers: int = 2
    #: true = 库里没确认就停；false = L1 成功就继续（默认，避免话说一半）
    strict_confirm: bool = False

    # ── 倾听层（config.json 顶层的 listen 段）──
    listen_enabled: bool = True
    listen_quiet: float = 2.0
    listen_max_wait: float = 8.0
    listen_max_batch: int = 8

    # ── 作用域（C-1：全局 / 账号 / 会话 三层继承）──
    #
    # 上面那些扁平字段是**全局视角**，只能表达"所有会话一样"。
    # 11-04 自环事故就是它们撑不住：allow_self 只能全局开，
    # 于是任何一个人的私聊需要它，等于所有人的私聊都开着它。
    #
    # 有会话上下文的调用点请用 `cfg.scope("allow_self", chat_id)`，
    # 它按 chat → account → global → 默认 逐层回退。
    raw: dict = field(default_factory=dict, repr=False)
    bot_id: str = ""
    #: 昵称 → id 的解析器（wechat.WeChatReader.resolve）。微信没开时为 None
    resolver: Optional[object] = None
    #: 命令行 `--allow-self` 的强制开关：显式覆盖，只用于自测
    force_allow_self: bool = field(default=False, repr=False)

    def scope(self, key: str, chat_id: Optional[str] = None, default: object = None):
        """按三层作用域取一个字段的最终值。

        `chat_id` 为空时只看得到 global（含旧路径），拿不到会话覆盖 ——
        所以带会话语义的调用点必须把 chat_id 传进来。
        """
        if key == "allow_self" and self.force_allow_self:
            return True          # 命令行显式开，压过配置
        return SCOPES.get(self.raw, key, bot_id=self.bot_id or None,
                          chat_id=chat_id, resolver=self.resolver,
                          default=default)

    @classmethod
    def load(cls, path: str = CONFIG_PATH) -> "AppConfig":
        cfg = cls()
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                d = data.get("auto_reply") or {}
                cfg.raw = data
                cfg.bot_id = str(d.get("account") or "")
                cfg.chats = list(d.get("chats") or [])
                cfg.interval = float(d.get("interval", cfg.interval))

                say = data.get("say") or {}
                cfg.say_enabled = bool(say.get("enabled", True))
                cfg.say_max_parts = int(say.get("max_parts", cfg.say_max_parts))
                cfg.say_part_chars = int(say.get("part_max_chars", cfg.say_part_chars))
                cfg.say_llm_split = bool(say.get("llm_split", False))
                cfg.say_workers = int(say.get("workers", cfg.say_workers))
                cfg.strict_confirm = bool(say.get("strict_confirm", False))

                listen = data.get("listen") or {}
                cfg.listen_enabled = bool(listen.get("enabled", True))
                cfg.listen_quiet = float(listen.get("quiet_same_author", cfg.listen_quiet))
                cfg.listen_max_wait = float(listen.get("max_wait", cfg.listen_max_wait))
                cfg.listen_max_batch = int(listen.get("max_batch", cfg.listen_max_batch))
            except Exception:
                pass
        return cfg


# ----------------------------------------------------------------------
# 会话历史
# ----------------------------------------------------------------------
class Conversation:
    def __init__(self, limit: int = HISTORY_LIMIT):
        self.limit = limit
        self.turns: list[tuple[str, str]] = []

    def add(self, who: str, text: str) -> None:
        self.turns.append((who, text))
        if len(self.turns) > self.limit:
            self.turns = self.turns[-self.limit:]

    def transcript(self, me: str) -> str:
        if not self.turns:
            return "（暂无对话）"
        return "\n".join(f"{w}: {t}" for w, t in self.turns)


# ----------------------------------------------------------------------
# 免打扰时段
# ----------------------------------------------------------------------
def _hour_of(text: object) -> Optional[int]:
    """把 "23" / "23:00" 读成小时数。读不出返回 None。"""
    head = str(text or "").strip().split(":")[0].strip()
    if not head.isdigit():
        return None
    n = int(head)
    return n if 0 <= n <= 23 else None


def _quiet_now(values, tm) -> bool:
    """当前时刻是否落在这些时段里。

    两种写法都认（`scopes` 把值统一成字符串，因为 JSON 里可能混着数字）：

        "23"             某个整点
        "23:00-07:00"    区间，两端都含；start > end 表示跨夜
    """
    for raw in values or ():
        item = str(raw or "").strip()
        if not item:
            continue
        if "-" in item:
            left, _, right = item.partition("-")
            start, end = _hour_of(left), _hour_of(right)
            if start is None or end is None:
                continue
            if start <= end:
                if start <= tm.tm_hour < end:
                    return True
            elif tm.tm_hour >= start or tm.tm_hour < end:
                return True
        else:
            hour = _hour_of(item)
            if hour is not None and hour == tm.tm_hour:
                return True
    return False


# ----------------------------------------------------------------------
# 主逻辑
# ----------------------------------------------------------------------
class BigFishApp:
    def __init__(self, reader: WeChatReader, sender: UiaSender, soul: Soul,
                 model_cfg: ModelConfig, cfg: AppConfig, dry: bool = False):
        self.reader = reader
        self.sender = sender
        self.soul = soul
        self.model_cfg = model_cfg
        self.cfg = cfg
        self.dry = dry

        # 作用域解析器：配置里的昵称键要靠它换成 id 才能匹配上。
        # 微信没开时 resolve 返回 None，那一条配置就是"未命中"，
        # 不会抛异常、也不会误命中别的会话。
        try:
            # 套一层缓存：一条消息会问 4~5 个字段，不缓存就要查好几遍微信库
            cfg.resolver = SCOPES.memo_resolver(reader.resolve)
            cfg.bot_id = str(getattr(reader, "wxid", "") or "") or cfg.bot_id
        except Exception:
            cfg.resolver = None
        self.convs: dict[str, Conversation] = {}
        # 共同账本（D-1）：拦截与发送确认共用这一份
        from bigfish.ledger import Ledger
        self.ledger = Ledger()
        self.stats = {"recv": 0, "reply": 0, "fail": 0, "skip": 0}
        self._gate = None       # 群聊触发器，首次用到时构造

        # 倾听层：攒批。watcher 线程只 feed，flush 线程负责到期处理。
        self.batcher = MessageBatcher(ListenPolicy(
            quiet_same_author=cfg.listen_quiet,
            max_wait=cfg.listen_max_wait,
            max_batch=cfg.listen_max_batch,
        ))
        self._stop = threading.Event()
        self._flush_thread: Optional[threading.Thread] = None
        self._chat_locks: dict[str, threading.Lock] = {}   # 同一会话串行
        self._pool: Optional[ThreadPoolExecutor] = None    # 不同会话并发

        # 「说法」插件的参数缓存：每批处理前刷新一次，面板改完立刻生效。
        self._say_per_char = 0.09
        self._say_cap = 3.5
        self._say_total_cap = 12.0
        self._say_quote_protect = True
        self._say_async_confirm = True

    # ------------------------------------------------------------------
    def start(self) -> None:
        """起 flush 线程（listen_enabled=False 时不起，走单条直通）。"""
        if not self.cfg.listen_enabled:
            return
        if self._flush_thread and self._flush_thread.is_alive():
            return
        self._stop.clear()
        if self._pool is None:
            self._pool = ThreadPoolExecutor(
                max_workers=max(1, self.cfg.say_workers),
                thread_name_prefix="bigfish-say")
        self._flush_thread = threading.Thread(
            target=self._flush_loop, name="bigfish-flush", daemon=True)
        self._flush_thread.start()

    def stop(self) -> None:
        """停线程，并把没到期的批次处理掉（退出前不留半句）。"""
        self._stop.set()
        if self._flush_thread:
            self._flush_thread.join(timeout=5)
            self._flush_thread = None
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._pool = None
        for batch in self.batcher.flush_all():
            try:
                self.handle_batch(batch)
            except Exception as exc:
                self.log(f"[说法] 收尾批次异常: {exc}")

    def _flush_loop(self) -> None:
        while not self._stop.is_set():
            for batch in self.batcher.tick():
                if self._pool is not None:
                    self._pool.submit(self._run_batch, batch)
                else:
                    self._run_batch(batch)
            self._stop.wait(0.1)

    def _run_batch(self, batch: Batch) -> None:
        """同一会话串行，不同会话并发：一个群卡住不拖累另一个群。"""
        lock = self._chat_locks.get(batch.chat_id)
        if lock is None:
            lock = self._chat_locks.setdefault(batch.chat_id, threading.Lock())
        with lock:
            try:
                self.handle_batch(batch)
            except Exception as exc:
                self.log(f"[说法] 批次异常: {exc}")

    def _refresh_say_config(self) -> None:
        """从「说法」插件读最新参数。面板保存后，下一批就按新参数走。"""
        try:
            from bigfish import plugins as _P

            _P.ensure_loaded()
            if not _P.is_enabled("say"):
                return
            cfg = _P.config_of("say")
            self.cfg.say_max_parts = int(cfg.get("max_parts", self.cfg.say_max_parts))
            self.cfg.say_part_chars = int(cfg.get("part_max_chars", self.cfg.say_part_chars))
            self.cfg.strict_confirm = bool(cfg.get("strict_confirm", self.cfg.strict_confirm))
            self._say_per_char = float(cfg.get("per_char", self._say_per_char))
            self._say_cap = float(cfg.get("cap", self._say_cap))
            self._say_total_cap = float(cfg.get("total_cap", self._say_total_cap))
            self._say_quote_protect = bool(cfg.get("quote_protect", self._say_quote_protect))
            self._say_async_confirm = bool(cfg.get("async_confirm", self._say_async_confirm))
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _llm_split(self, text: str) -> str:
        """让模型把一段话断成 1~3 行。只断句，不改写。"""
        return call_model(
            self.model_cfg,
            "你是中文断句器。只输出断好的句子，每行一条，"
            "不要改写、不要解释、不要加任何标记。",
            "把下面这段话按语义断成 1~3 行，每行一条：\n\n" + text,
        )

    def _spawn_confirm(self, pending: tuple[str, int], chat: str,
                       conv: Conversation) -> None:
        """最后一条的异步确认：后台等落库，确认了才写历史。

        最后一条不该让整批陪它等 3 秒；确认不到也不影响已经发出去的前几条。
        """
        _text, base_id = pending
        me_id = identity.self_id_of(self.reader)

        def worker() -> None:
            for _ in range(12):          # 0.25s × 12 = 3s
                time.sleep(0.25)
                try:
                    for m in self.reader.messages(chat, 6):
                        # 这里问的是"我刚发的那句入库了没有"，所以要的是
                        # **确定是自己**，不是"is_self 为真"。拿不准的时候
                        # 宁可认为没入库（这一轮不进历史），也不能把别人的
                        # 一句话当成自己说的写进对话。
                        #
                        # identity 为空 = 这个对象没走过判定（旧对象、替身）。
                        # 那种情况按它自己的字段重算一次，而不是退回 is_self ——
                        # 退回旧字段就等于把这次改动白做了。
                        _who = getattr(m, "identity", "")
                        if not _who:
                            _who = identity.for_message(m, me_id).who
                        if (int(getattr(m, "local_id", 0)) > base_id
                                and _who == identity.WHO_SELF):
                            conv.add("我", m.content)
                            return
                except Exception:
                    return

        threading.Thread(target=worker, name="bigfish-confirm", daemon=True).start()

    # ------------------------------------------------------------------
    def log(self, s: str) -> None:
        print(s, flush=True)

    def conv(self, key: str) -> Conversation:
        if key not in self.convs:
            self.convs[key] = Conversation()
        return self.convs[key]

    # ------------------------------------------------------------------
    def in_quiet_hours(self, chat_id: str = "") -> bool:
        """现在是不是**这个会话**的免打扰时段。

        11-04 之前它读 `self.cfg.quiet_hours`（全局），于是给一个会话设的
        安静时段会静音所有会话。现在按 scopes 三层取，`chat_id` 传谁的
        就算谁的。
        """
        return _quiet_now(self.cfg.scope("quiet_hours", chat_id),
                          time.localtime())

    def build_prompt(self, conv: Conversation, msg: Incoming,
                     body: str = "") -> tuple[str, str]:
        """组装 system / user。

        body 是「真正要对的那句话」——群里被 @ 时已经去掉了 @昵称前缀，
        私聊时就是原文。
        """
        from bigfish.bridge_client import load_context

        text = body or msg.text or ""
        ctx = load_context(query=text)
        for w in ctx.warnings:
            self.log(f"[bridge] {w}")

        sys_parts = [ctx.persona or self.soul.render_persona()]
        if ctx.mood and ctx.mood.injected_text.strip():
            sys_parts.append("\n## 这会儿的状态\n\n"
                             + ctx.mood.injected_text.strip())
        elif ctx.mood and ctx.mood.value is not None:
            sys_parts.append("\n## 这会儿的状态\n\n"
                             f"- 心情：{ctx.mood.label}"
                             f"（{ctx.mood.value:.0f}/100）")

        # 插件段按身份序并入（与 desktop.py 同一套：灵魂 860 / 心情 900 /
        # 生活 870 / 守卫 9990）。两个入口必须一致 —— 否则同一句话在
        # 命令行跑和在 EXE 里跑，她会用不同的语气回。
        try:
            from bigfish import plugins as _P

            _sections = [(860, sys_parts[0])]
            _sections += [(900, t) for t in sys_parts[1:]]
            for _order, _title, _text in _P.prompt_sections({
                "now": datetime.now(),
                "channel": "group" if msg.chat_id.endswith("@chatroom") else "private",
                "chat_name": msg.chat_name,
                "sender_name": msg.sender_name,
                "user_text": text,
                "soul": self.soul,
            }):
                _sections.append((_order, _text))
            _sections.sort(key=lambda item: item[0])
            sys_parts = [t for _o, t in _sections]
        except Exception as exc:
            self.log(f"[插件] 注入失败：{exc}")

        user_parts = []
        # 原文检索：她「听过什么」。
        mem = ctx.render_memory()
        if mem:
            user_parts.append("## 你记得的事\n\n" + mem)

        # 事实层：她「知道什么」。与原文分开注入 ——
        #   原文是逐句的、带时间的；事实是稳定的、可被纠正的。
        #   以前只有原文，于是「她改主意了」这件事无处表达：只能删原文。
        facts = _facts_block()
        if facts:
            user_parts.append(
                "## 你认定的事\n\n" + facts
                + "\n\n（这些是你自己整理过的认识，不是说过的原话；"
                  "和对方现在的说法冲突时以对方为准，并改过来。）")

        is_group = msg.chat_id.endswith("@chatroom")
        scene = (f"你在微信群「{msg.chat_name}」里，"
                 f"{msg.sender_name or '某人'} 刚说了话。" if is_group
                 else f"你在微信上和「{msg.chat_name}」聊天。")
        user_parts.append(
            f"## 这一轮\n\n{scene}\n"
            f"以{self.soul.name}的身份回消息。\n\n"
            "要求：\n"
            "- 像真人发微信：短，口语，不用 Markdown，不列清单\n"
            "- 不要复述对方说过的话，直接回应\n"
            "- 需要更多信息就一句话追问\n"
            "- 一次只说一件事。想说的多就分几条发，"
            "每条之间用单独一行 " + SPLIT_MARK + " 隔开\n"
            "- 一般 1~3 条就够；不要为了凑条数把一句话拆碎，"
            "也不要把标记写进正文"
        )
        user_parts.append("## 最近的对话\n\n"
                          + conv.transcript("我"))
        return ("\n".join(sys_parts).strip(), "\n\n".join(user_parts))

    # ------------------------------------------------------------------
    def _trace_identity(self, msg, verdict, is_group, me, decision=None,
                        *, force=False):
        """把"拿不准"的判定写进证据链。

        旁路：写失败静默，绝不影响聊天。确定的判定不写（见 identity_trace）。
        """
        try:
            from bigfish import identity_trace

            identity_trace.record(
                verdict,
                # 不能用 `decision or ...`：Decision.__bool__ 返回 proceed，
                # 而否决的 decision 恰好是 proceed=False，会被当成「没给」，
                # 于是留痕里写成 self，看不出是谁按住的。
                decision if decision is not None
                else identity.admit(verdict, is_group=is_group),
                chat_id=msg.chat_id, is_group=is_group,
                sender=msg.sender_name, me=me,
                text=msg.raw_content or msg.text, force=force)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _accept(self, msg: Incoming) -> Optional[BatchMessage]:
        """过滤 + 群聊判定。通过则返回可入批的消息，否则 None。"""
        # ── 归属判定（A-1）──
        #
        # 11-04 之前这里是 `msg.is_self and not cfg.allow_self`：一个 bool，
        # 而 bool 表达不了"我其实不知道"。现在三态，私聊保守、群聊宽松：
        #   · 确定自己   → 只有自测通道才继续
        #   · 确定对方   → 继续
        #   · 拿不准     → 私聊直接放弃；群聊等到 gate 判定"叫没叫她"
        is_group = msg.chat_id.endswith("@chatroom")
        me = identity.self_id_of(self.reader)
        verdict = identity.decide(
            msg.sender_name, me,
            text=msg.raw_content or msg.text,
            chat_id=msg.chat_id, is_group=is_group)
        allow_self = bool(self.cfg.scope("allow_self", msg.chat_id))

        # 账本能否决判据（D-1）。只在判据说"对方"时用 ——
        # 这是 11-04 那晚唯一缺的一环：判据错了，还有东西能按住。
        if verdict.who == identity.WHO_PEER:
            hit = self.ledger.lookup(msg.chat_id, msg.raw_content or msg.text)
            if hit is not None:
                ago = max(0.0, time.time() - hit.at)
                self.log(f"[否决] {msg.chat_name}: 判据说对方说的，"
                         f"但账本里 {ago:.0f} 秒前发过同一句，按自己发的处理")
                vetoed = identity.Verdict(
                    identity.WHO_SELF, "ledger_veto",
                    verdict.evidence + (("ledger", hit.text[:24]),),
                    f"账本里 {ago:.0f} 秒前发过这句")
                # 留痕里的放行结论也要写清楚是"账本否决"，
                # 不然回看只听得到"self"，看不出是谁把它按住的。
                self._trace_identity(
                    msg, vetoed, is_group, me, force=True,
                    decision=identity.Decision(
                        False, "ledger_veto",
                        f"账本里 {ago:.0f} 秒前发过同一句"))
                self.stats["skip"] += 1
                return None

        if verdict.who != identity.WHO_UNKNOWN:
            decision = identity.admit(verdict, is_group=is_group,
                                      allow_self=allow_self)
            if not decision.proceed:
                self.stats["skip"] += 1
                self.log(f"[跳过] {msg.chat_name}: {decision.detail}"
                         f"（{decision.reason}）")
                return None
            if verdict.who == identity.WHO_SELF:
                # 自己发的也要防回音（L2）。
                #
                # 文件传输助手这个自测通道里，自己打的字和她回的话**是同一个
                # wxid 发的** —— 判据分不开，只有账本能分：她刚发过的是回音，
                # 我刚打进去的不是。
                #
                # 没有这一步，allow_self 打开就必然自答到天荒地老。
                hit = self.ledger.lookup(msg.chat_id, msg.raw_content or msg.text)
                if hit is not None:
                    self.stats["skip"] += 1
                    self.log(f"[回环] {msg.chat_name}: 读到自己刚发的，跳过"
                             f"（{msg.preview(24)}）")
                    return None
        elif not is_group:
            # 私聊拿不准 → 彻底静默（问过：不要提示、不要回一句）
            self.stats["skip"] += 1
            self.log(f"[跳过] {msg.chat_name}: 拿不准是谁发的"
                     f"（{verdict.rule}），不回")
            self._trace_identity(msg, verdict, is_group, me)
            return None

        if not (msg.text or "").strip():
            self.stats["skip"] += 1
            self.log(f"[跳过] {msg.chat_name}: 空内容({msg.msg_type})")
            return None
        if self.in_quiet_hours(msg.chat_id):
            self.stats["skip"] += 1
            self.log(f"[安静时段] 跳过 {msg.chat_name}: {msg.preview(30)}")
            return None

        body = msg.text
        at_bot = False
        if is_group:
            from bigfish.group_gate import GroupGate

            # 两个群聊字段也按会话取。以前 `enable` 只在构造时设置一次，
            # 改了配置不重开程序就不生效 —— 顺手修掉。
            gate_enable = bool(self.cfg.scope("group_reply", msg.chat_id))
            allow_list = self.cfg.scope("group_allow_all", msg.chat_id) or ()
            gate_allow_all = (msg.chat_name in allow_list
                              or msg.chat_id in allow_list)

            if self._gate is None:
                self._gate = GroupGate(
                    self.reader._db, self.reader._db.wxid,
                    enable=gate_enable, allow_all=gate_allow_all)
            else:
                self._gate.enable = gate_enable
                self._gate.allow_all = gate_allow_all

            t = self._gate.check(msg.chat_id, msg.text, True)
            if not t.respond:
                self.stats["skip"] += 1
                self.log(f"[不接] {msg.chat_name} / {msg.sender_name}: "
                         f"{msg.preview(40)}（{t.reason}）")
                return None

            # 群聊里"拿不准"要放行，但必须真的叫了她（@ 或叫名字）。
            # 免检群（group-allow-all）不算 —— 那是"这个群不用 @ 也回"，
            # 不是"有人冲她说话"。
            if verdict.who == identity.WHO_UNKNOWN:
                decision = identity.admit(verdict, is_group=True,
                                          woken=identity.woken_from_reason(t.reason))
                if not decision.proceed:
                    self.stats["skip"] += 1
                    self.log(f"[跳过] {msg.chat_name}: 拿不准是谁发的，"
                             f"也没叫她（{t.reason}）")
                    self._trace_identity(msg, verdict, is_group, me)
                    return None
                self.log(f"[拿不准] {msg.chat_name}: 但她被叫到了"
                         f"，按对方处理（{verdict.rule}）")
                self._trace_identity(msg, verdict, is_group, me)

            body = t.clean_text
            at_bot = t.reason.startswith(("at-marker", "at-name", "called:"))
            self.log(f"[触发] {msg.chat_name} / {msg.sender_name}: {t.reason}")

        self.stats["recv"] += 1
        return BatchMessage(
            chat_id=msg.chat_id,
            chat_name=msg.chat_name,
            author=(msg.sender_name if is_group else msg.chat_name) or "对方",
            text=body,
            local_id=msg.local_id,
            msg_type=msg.msg_type,
            at_bot=at_bot,
            is_self=msg.is_self,
            received_at=time.monotonic(),
        )

    # ------------------------------------------------------------------
    def enqueue(self, msg: Incoming) -> None:
        """watcher 回调：只入批，不调模型。等对方说完再交给 handle_batch。"""
        m = self._accept(msg)
        if m is None:
            return
        if not self.cfg.listen_enabled:
            self.handle_batch(Batch(m.chat_id, [m], "forced", 0.0, time.monotonic()))
            return
        immediate = self.batcher.feed(m)
        if immediate is not None:
            self.handle_batch(immediate)

    def handle(self, msg: Incoming) -> None:
        """兼容旧调用：单条立即处理，不攒批。"""
        m = self._accept(msg)
        if m is None:
            return
        self.handle_batch(Batch(m.chat_id, [m], "forced", 0.0, time.monotonic()))

    # ------------------------------------------------------------------
    def handle_batch(self, batch: Batch) -> None:
        """一批消息 → 一次模型 → 分条发送（每条确认后才发下一条）。"""
        self._refresh_say_config()
        conv = self.conv(batch.chat_id)
        for m in batch.messages:
            conv.add(m.author, m.text)

        last = batch.messages[-1]
        is_group = last.chat_id.endswith("@chatroom")
        self.log(f"\n[收] {last.chat_name}"
                 + (f" / {last.author}" if is_group and last.author else "")
                 + f"：{len(batch.messages)} 条 / 等 {batch.waited:.1f}s / {batch.reason}")

        body = batch_transcript(batch, with_time=False)
        system, user = self.build_prompt(conv, last, body)
        t0 = time.time()
        try:
            reply = call_model(self.model_cfg, system, user,
                               trace_meta={"chat": batch.chat_id})
        except ModelError as exc:
            self.stats["fail"] += 1
            self.log(f"[!] 生成失败: {exc}")
            return
        dt = time.time() - t0

        reply = (reply or "").strip()
        if not reply:
            self.stats["fail"] += 1
            self.log("[!] 模型返回空")
            return
        # 上限也按会话取：群聊和私聊的长度习惯本来就不一样
        max_chars = int(self.cfg.scope("max_reply_chars", batch.chat_id) or 0)
        if max_chars > 0 and len(reply) > max_chars:
            reply = reply[:max_chars] + "…"

        self.log(f"[想] {dt:.1f}s  in={len(system) + len(user)}字 out={len(reply)}字")

        if self.dry:
            self.log(f"[预演] {reply!r}")
            conv.add("我", reply)
            return

        # 分条 + 节奏 + 逐条确认
        split_policy = SplitPolicy(
            max_parts=self.cfg.say_max_parts,
            part_max_chars=self.cfg.say_part_chars,
            protect_quotes=self._say_quote_protect)
        if not self.cfg.say_enabled:
            parts = [reply]
        elif self.cfg.say_llm_split:
            parts = split_reply_with_llm(reply, self._llm_split, policy=split_policy)
        else:
            parts = split_reply(reply, split_policy)
        if is_group and last.author and parts:
            parts[0] = f"@{last.author} {parts[0]}"   # @ 只加第一条

        gaps = plan_pacing(parts, PacePolicy(
            seed=batch.chat_id,
            per_char=self._say_per_char,
            cap=self._say_cap,
            total_cap=self._say_total_cap))
        outcome = send_parts(
            self.sender.send,
            self.reader.messages,
            last.chat_name,
            parts,
            gaps,
            log=self.log,
            strict_confirm=self.cfg.strict_confirm,
            final_async_confirm=self._say_async_confirm,
            ledger=self.ledger,
            on_confirmed=lambda _i, m: conv.add("我", m.content),
        )
        if outcome.pending is not None:
            self._spawn_confirm(outcome.pending, last.chat_name, conv)
        if outcome.ok:
            self.stats["reply"] += 1
            extra = []
            if outcome.unconfirmed:
                extra.append(f"unconfirmed={outcome.unconfirmed}")
            if outcome.mismatches:
                extra.append(f"mismatch={outcome.mismatches}")
            self.log(f"[发] ✓ → {last.chat_name}：{len(parts)} 条 / {outcome.level}"
                     + (" / " + " ".join(extra) if extra else ""))
        elif outcome.unknown:
            # 未知不是失败：消息可能已经发出去了。
            # 所以既不重发，也不把账本里的那一笔撤掉（见 bigfish/sending.py）。
            self.stats["fail"] += 1
            self.log(f"[发] ? {last.chat_name}：{outcome.detail}")
            self.log("       消息结果未知 —— 可能已经发出去了，所以不会重发。"
                     "如果对方说没收到，是这一条。")
        else:
            self.stats["fail"] += 1
            self.log(f"[发] ✗ {last.chat_name}：{outcome.level} {outcome.detail}")


# ----------------------------------------------------------------------
def _facts_block(limit: int = 12) -> str:
    """从记忆库取「她认定的事」渲染成注入段。失败返回空串，绝不中断对话。"""
    try:
        from bigfish import memory as M, vault as V

        db = V.memory_db_path()
        if not db:
            return ""
        return M.facts_for_prompt(db, "dafeiyu", limit=limit)
    except Exception:
        return ""


# ----------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="大肥鱼 × 微信")
    ap.add_argument("--who", action="append", default=[],
                    help="要监听的会话（可多次，覆盖 config.json）")
    ap.add_argument("--dry", action="store_true", help="只生成不发送")
    ap.add_argument("--allow-self", action="store_true",
                    help="也处理自己发的消息")
    ap.add_argument("--interval", type=float, default=0,
                    help="轮询间隔秒（0=用配置）")
    args = ap.parse_args()

    print(HR)
    print("大肥鱼 × 微信")
    print(HR)

    cfg = AppConfig.load()
    if args.who:
        cfg.chats = args.who
    if args.allow_self:
        # 命令行显式开自测通道。走 force 而不是改配置值 ——
        # 不然它会和 scopes 的会话级判断打架，看不出到底谁在生效。
        cfg.force_allow_self = True
    if args.interval > 0:
        cfg.interval = args.interval

    admin = is_admin()
    desk = desktop_available()
    print(f"  管理员     : {admin}")
    print(f"  桌面可用   : {desk}")
    if not admin and not args.dry:
        print()
        print("  [!] 不是管理员。UIA 发送需要提权（热激活要写 gate 字节）。")
        print("      用 --dry 可以只测试生成，不发送。")
        return 1
    if not desk and not args.dry:
        print()
        print("  [!] 桌面锁着。UIA 需要可见的桌面，请解锁后重试。")
        return 1

    # ---- 拟人节流 ----
    # 和桌面版同一个理由：默认档 120 秒内只允许 6 次写动作，
    # 一轮发 2~4 条，两轮就撞上，然后被按住 30~75 秒。
    try:
        from bigfish.wechat_sender import RHYTHM_NOTES, apply_rhythm

        _rhythm_want = str(((cfg.raw or {}).get("auto_reply") or {})
                           .get("rhythm") or "fast")
        _rhythm_got = apply_rhythm(_rhythm_want)
        if _rhythm_got:
            print(f"  节流       : {_rhythm_got}（{RHYTHM_NOTES.get(_rhythm_got, '')}）")
    except Exception:
        pass

    # ---- 灵魂 ----
    try:
        soul = load_soul()
    except FileNotFoundError as exc:
        print(f"  [!] {exc}")
        return 1
    print(f"  灵魂       : {soul.name}（rev {soul.revision}），称呼「{soul.address}」")

    # ---- 模型 ----
    mcfg = ModelConfig.load()
    print(f"  模型       : {mcfg.preset.name} / {mcfg.eff_model}")
    if not mcfg.ready:
        print("  [!] 模型未就绪（检查 config.json 或 Magpie 网关）")
        return 1

    # ---- 数据库 ----
    print()
    print("  打开微信数据库 ...")
    try:
        reader = WeChatReader().open()
    except Exception as exc:
        print(f"  [!] 数据库打不开: {exc}")
        return 1
    me = reader.self_info()
    print(f"  账号       : {me.get('nick_name')}（{reader._db.account}）")

    # ---- 身份闸 ----
    #
    # 没有自己的 wxid 就没法判断哪条消息是自己发的 —— 所有自环防护
    # 都会从源头失效。这种状态不启动监听，而不是"先跑起来看看"。
    # 11-04 那晚的教训：不知道自己是谁的时候开口，就是在赌。
    try:
        identity.require_me(identity.self_id_of(reader), where="app.py")
    except identity.IdentityUnavailable as exc:
        print()
        print(f"  [!] {exc.reason}")
        return 1

    # ---- 监听会话 ----
    if not cfg.chats:
        print()
        print("  [!] 没有指定要监听的会话。")
        print("      用 --who <会话名>，或在 config.json 的")
        print("      auto_reply.chats 里配置。")
        return 1

    watcher = MessageWatcher(reader._db, interval=cfg.interval)
    print()
    for name in cfg.chats:
        uid = watcher.watch(name)
        print(f"  监听       : {name} -> {uid}")

    # ---- 发送器 ----
    sender = UiaSender()
    if not args.dry:
        try:
            sender.wake()
            print(f"  UIA        : 就绪（当前会话 {sender.current_chat()!r}）")
        except SendError as exc:
            print(f"  [!] UIA 初始化失败: {exc}")
            return 1
    else:
        print("  UIA        : 预演模式")

    app = BigFishApp(reader, sender, soul, mcfg, cfg, dry=args.dry)
    watcher.on_message = app.enqueue

    print()
    print(f"  说法       : {'分条 + 节奏' if cfg.say_enabled else '整段发送'}"
          f"（最多 {cfg.say_max_parts} 条）")
    print(f"  倾听       : {'等对方说完再回' if cfg.listen_enabled else '逐条直回'}"
          f"（安静 {cfg.listen_quiet:.1f}s / 上限 {cfg.listen_max_wait:.1f}s）")

    print()
    print(HR)
    print("  开始监听。" + ("（预演，不会发送）" if args.dry else "会自动回复。"))
    print("  Ctrl+C 退出。")
    print(HR)

    watcher.start()
    app.start()
    t_start = time.time()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[bigfish] 停止中 ...")
        watcher.stop()
        app.stop()

    dur = int(time.time() - t_start)
    print()
    print(HR)
    print(f"  运行 {dur//60}分{dur%60}秒")
    print(f"  收到 {app.stats['recv']} 条 / 回复 {app.stats['reply']} 条 "
          f"/ 跳过 {app.stats['skip']} / 失败 {app.stats['fail']}")
    print(HR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
