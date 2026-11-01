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
import time
from dataclasses import dataclass, field

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bigfish.replier import ModelConfig, ModelError, call_model  # noqa: E402
from bigfish.soul import Soul, load_soul                        # noqa: E402
from bigfish.watcher import Incoming, MessageWatcher            # noqa: E402
from bigfish.wechat import WeChatReader                         # noqa: E402
from bigfish.wechat_sender import (SendError, UiaSender,        # noqa: E402
                                   desktop_available, is_admin)

HR = "=" * 70
CONFIG_PATH = os.path.join(_ROOT, "config.json")
HISTORY_LIMIT = 16


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------
@dataclass
class AppConfig:
    """运行时配置（config.json 的 auto_reply 段）。"""

    chats: list[str] = field(default_factory=list)
    allow_self: bool = False
    interval: float = 1.2
    max_reply_chars: int = 300
    quiet_hours: list[int] = field(default_factory=list)  # 如 [0,1,2,3,4,5,6]
    group_reply: bool = True       # 群聊是否响应
    group_allow_all: list[str] = field(default_factory=list)  # 无条件回的群

    @classmethod
    def load(cls, path: str = CONFIG_PATH) -> "AppConfig":
        cfg = cls()
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    d = json.load(f).get("auto_reply") or {}
                cfg.chats = list(d.get("chats") or [])
                cfg.allow_self = bool(d.get("allow_self", False))
                cfg.interval = float(d.get("interval", cfg.interval))
                cfg.max_reply_chars = int(
                    d.get("max_reply_chars", cfg.max_reply_chars))
                cfg.quiet_hours = [int(h) for h in
                                   (d.get("quiet_hours") or [])]
                cfg.group_reply = bool(d.get("group_reply", True))
                cfg.group_allow_all = [str(x) for x in
                                       (d.get("group_allow_all") or [])]
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
        self.convs: dict[str, Conversation] = {}
        self.stats = {"recv": 0, "reply": 0, "fail": 0, "skip": 0}
        self._gate = None       # 群聊触发器，首次用到时构造

    # ------------------------------------------------------------------
    def log(self, s: str) -> None:
        print(s, flush=True)

    def conv(self, key: str) -> Conversation:
        if key not in self.convs:
            self.convs[key] = Conversation()
        return self.convs[key]

    # ------------------------------------------------------------------
    def in_quiet_hours(self) -> bool:
        return time.localtime().tm_hour in self.cfg.quiet_hours

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

        user_parts = []
        mem = ctx.render_memory()
        if mem:
            user_parts.append("## 你记得的事\n\n" + mem)

        is_group = msg.chat_id.endswith("@chatroom")
        scene = (f"你在微信群「{msg.chat_name}」里，"
                 f"{msg.sender_name or '某人'} 刚说了话。" if is_group
                 else f"你在微信上和「{msg.chat_name}」聊天。")
        user_parts.append(
            f"## 这一轮\n\n{scene}\n"
            f"以{self.soul.name}的身份回一条消息。\n\n"
            "要求：\n"
            "- 像真人发微信，短，口语，不用 Markdown，不列清单\n"
            "- 不要复述对方说过的话，直接回应\n"
            "- 需要更多信息就一句话追问"
        )
        user_parts.append("## 最近的对话\n\n"
                          + conv.transcript(self.soul.address or "我"))
        return ("\n".join(sys_parts).strip(), "\n\n".join(user_parts))

    # ------------------------------------------------------------------
    def handle(self, msg: Incoming) -> None:
        self.stats["recv"] += 1

        if msg.is_self and not self.cfg.allow_self:
            self.stats["skip"] += 1
            return
        if not (msg.text or "").strip():
            self.stats["skip"] += 1
            self.log(f"[跳过] {msg.chat_name}: 空内容({msg.msg_type})")
            return
        if self.in_quiet_hours():
            self.stats["skip"] += 1
            self.log(f"[安静时段] 跳过 {msg.chat_name}: {msg.preview(30)}")
            return

        # ── 群聊触发判定 ──
        is_group = msg.chat_id.endswith("@chatroom")
        body = msg.text
        if is_group:
            from bigfish.group_gate import GroupGate

            if self._gate is None:
                self._gate = GroupGate(
                    self.reader._db, self.reader._db.wxid,
                    enable=self.cfg.group_reply,
                    allow_all=msg.chat_name in self.cfg.group_allow_all
                    or msg.chat_id in self.cfg.group_allow_all)
            else:
                self._gate.allow_all = (
                    msg.chat_name in self.cfg.group_allow_all
                    or msg.chat_id in self.cfg.group_allow_all)

            t = self._gate.check(msg.chat_id, msg.text, True)
            if not t.respond:
                self.stats["skip"] += 1
                self.log(f"[不接] {msg.chat_name} / {msg.sender_name}: "
                         f"{msg.preview(40)}（{t.reason}）")
                return
            body = t.clean_text
            self.log(f"[触发] {msg.chat_name} / {msg.sender_name}: "
                     f"{t.reason}")

        key = msg.chat_id
        conv = self.conv(key)
        who = (msg.sender_name if is_group else msg.chat_name) or "对方"
        conv.add(who, body)
        self.log(f"\n[收] {msg.chat_name}"
                 + (f" / {msg.sender_name}" if is_group and msg.sender_name else "")
                 + f"：{msg.preview(60)}")

        system, user = self.build_prompt(conv, msg, body)
        t0 = time.time()
        try:
            reply = call_model(self.model_cfg, system, user)
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
        if len(reply) > self.cfg.max_reply_chars:
            reply = reply[: self.cfg.max_reply_chars] + "…"

        self.log(f"[想] {dt:.1f}s  in={len(system)+len(user)}字 "
                 f"out={len(reply)}字")
        conv.add(self.soul.address or "我", reply)

        if self.dry:
            self.log(f"[预演] {reply!r}")
            return

        send_text = reply
        if is_group and msg.sender_name:
            # 群里回复要带 @，否则看不出在跟谁说话
            send_text = f"@{msg.sender_name} {reply}"

        try:
            r = self.sender.send(msg.chat_name, send_text)
            if r.ok:
                self.stats["reply"] += 1
                self.log(f"[发] ✓ → {msg.chat_name}：{reply[:70]}")
            else:
                self.stats["fail"] += 1
                self.log(f"[发] ✗ {msg.chat_name}：{r.detail}")
        except SendError as exc:
            self.stats["fail"] += 1
            self.log(f"[发] ✗ {exc.kind}：{exc}")


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
        cfg.allow_self = True
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

    # ---- 灵魂 ----
    try:
        soul = load_soul()
    except FileNotFoundError as exc:
        print(f"  [!] {exc}")
        return 1
    print(f"  灵魂       : {soul.name}（rev {soul.revision}），称呼「{soul.address}」")

    # ---- 模型 ----
    mcfg = ModelConfig.load()
    print(f"  模型       : {mcfg.model}")
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
    watcher.on_message = app.handle

    print()
    print(HR)
    print("  开始监听。" + ("（预演，不会发送）" if args.dry else "会自动回复。"))
    print("  Ctrl+C 退出。")
    print(HR)

    watcher.start()
    t_start = time.time()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[bigfish] 停止中 ...")
        watcher.stop()

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
