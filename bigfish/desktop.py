"""[bigfish] 桌面应用主程序。

架构：
    QMainWindow
      └── QWebEngineView  ← 渲染 ui/index.html
             ↕ QWebChannel
          Backend(QObject)  ← 暴露给 JS 的接口
             ↕ Qt 信号
          EngineWorker(QThread) ← 跑 watcher + 生成 + 发送

为什么引擎要在独立线程：
    watcher 是阻塞轮询的，模型调用要等 1-3 秒。放到主线程会卡住界面。
    用 QThread + 信号，界面永远不卡。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime


def _safe_reconfigure_stdout() -> None:
    """安全地设置 stdout 编码。

    GUI 程序（PyInstaller console=False）里 sys.stdout 是 None，
    直接调 .reconfigure() 会抛 AttributeError 导致程序启动即崩。
    这里判断后再操作，失败也静默——输出流不可用不影响界面运行。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_safe_reconfigure_stdout()

# 打包后（PyInstaller onedir）：
#   <安装目录>/BigFish.exe
#   <安装目录>/_internal/...        ← 依赖 + ui/ + bridge/
#   <安装目录>/data/                ← 数据
# 开发态时 _ROOT 就是仓库根，结构一致。
FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    _APP_DIR = os.path.dirname(sys.executable)
    # ⚠ 不能用 __file__ 反推资源目录。
    #
    #   PyInstaller 打进去的模块，__file__ 可能是**相对路径**
    #   （形如 "bigfish/desktop.py"），于是 os.path.abspath 会按
    #   当前工作目录去解析 —— 解析出来的目录取决于用户从哪儿启动 exe，
    #   和 exe 在哪儿没关系。
    #
    #   实测（2026-11-03）：这样算出来的 UI_DIR 是 <安装目录>/ui，
    #   而界面实际在 <安装目录>/_internal/ui，于是界面加载失败，
    #   用户看到的是 Chromium 的「无法访问您的文件 / ERR_FILE_NOT_FOUND」。
    #   那个错误页一个字都没提路径，看起来像文件丢了。
    #
    #   sys._MEIPASS 是 PyInstaller 提供的**绝对路径**，onedir 模式下
    #   就是 _internal 目录。这才是唯一可靠的锚点。
    _RES_DIR = getattr(sys, "_MEIPASS", "") or os.path.join(_APP_DIR, "_internal")
    _HERE = os.path.join(_RES_DIR, "bigfish")
    _ROOT = _RES_DIR
else:
    _HERE = os.path.dirname(os.path.abspath(__file__))
    _ROOT = os.path.dirname(_HERE)
    _APP_DIR = _ROOT
    _RES_DIR = _ROOT

# 界面目录：优先安装目录下的 ui/，其次仓库里的 ui/
UI_DIR = ""
for cand in (os.path.join(_APP_DIR, "ui"),
             os.path.join(_RES_DIR, "ui"),
             os.path.join(_HERE, "ui")):
    if os.path.isfile(os.path.join(cand, "index.html")):
        UI_DIR = cand
        break
if not UI_DIR:
    UI_DIR = os.path.join(_APP_DIR, "ui")

BRIDGE_DIR = ""
for cand in (os.path.join(_APP_DIR, "bridge"),
             os.path.join(_RES_DIR, "bridge"),
             os.path.join(_HERE, "bridge")):
    if os.path.isfile(os.path.join(cand, "bridge.mjs")):
        BRIDGE_DIR = cand
        break
if not BRIDGE_DIR:
    BRIDGE_DIR = os.path.join(_APP_DIR, "bridge")

CONFIG_PATH = os.path.join(_APP_DIR, "config.json")


def is_admin() -> bool:
    """当前进程是否有管理员权限（UIA 热激活需要）。"""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def ts_to_local(ts) -> str:
    """把时间戳转成本地时间字符串。

    微信的 source_time 是**毫秒**，Python 的 localtime 要**秒**。
    直接传毫秒会抛 OSError [Errno 22]（实测踩过）。
    这里按量级自动判断：大于 1e11 当作毫秒。
    """
    if not ts:
        return ""
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return ""
    if t > 1e11:
        t /= 1000.0
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(t))
    except (OSError, ValueError, OverflowError):
        return ""


# 打包后依赖在 _internal 里，sys.path 需要包含它以便 import vendor 包
_VENDOR = os.path.join(_APP_DIR, "_internal", "vendor", "wechatauto-replica")
for _p in (_ROOT, _HERE, _APP_DIR, _VENDOR):
    if _p and os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

# ══════════════════════════════════════════════════════════════════
#  WebEngine 环境（必须在 import QtWebEngine 之前设置）
#
#  实测：某些环境（远程桌面 / 无独显 / 虚拟显卡）下 Chromium 创建
#  GLES3 上下文会失败，随后 GPU 通道卡住，表现为 loadFinished=False
#  且页面永远空白。
#
#  这里不粗暴地 --disable-gpu（那会让界面发卡），而是：
#    - 保留 GPU 但让它走软件实现（--disable-gpu-compositing 关合成器加速）
#    - 关掉沙箱（PyInstaller 单文件运行时沙箱路径常出问题）
#    - 给一个可被环境变量覆盖的后门，方便排查
# ══════════════════════════════════════════════════════════════════
if "QTWEBENGINE_CHROMIUM_FLAGS" not in os.environ:
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
        "--no-sandbox "
        "--disable-gpu-sandbox "
        "--disable-software-rasterizer=false "
        "--ignore-gpu-blocklist "
        "--enable-unsafe-swiftshader"
    )
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
# 允许通过环境变量强制纯软件渲染（排查用）
if os.environ.get("BIGFISH_SOFTWARE_RENDER") == "1":
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] += " --disable-gpu"

from PySide6.QtCore import (QObject, QThread, QUrl, Signal, Slot,  # noqa: E402
                            Qt)
from PySide6.QtGui import QIcon                                       # noqa: E402
from PySide6.QtWebChannel import QWebChannel                         # noqa: E402
from PySide6.QtWebEngineWidgets import QWebEngineView                # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow               # noqa: E402

UI_DIR = os.path.join(_ROOT, "ui")
CONFIG_PATH = os.path.join(_ROOT, "config.json")


# ══════════════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════════════
def _source_ms(msg) -> "int | None":
    """入站消息的发生时间，毫秒。取不到就返回 None（让写入方用当前时间）。

    为什么要这一层：`Incoming` 的字段叫 `timestamp`（微信 `create_time`，
    **秒**），而记忆库的 `source_time` / `recorded_at` 都是**毫秒**。
    以前这里写的是 `getattr(msg, "source_time", None)` —— 那个属性根本
    不存在，于是每条记忆的 source_time 都是 None，时间全塌成"写入时刻"。
    检索时按时间排序、按时间窗过滤全部失真，而且界面上看到的是
    "这条刚刚说过"，其实是三天前说的。
    """
    raw = getattr(msg, "timestamp", None)
    try:
        v = int(raw or 0)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    # 微信 4.x 的 create_time 是秒；万一哪天变成毫秒，按数量级判一下
    return v * 1000 if v < 10_000_000_000 else v


def _profile_block(owner: str) -> str:
    """取「她印象里的他」—— 从事实归纳出来的那段概述（v6）。

    和 `## 你认定的事`（逐条事实）的分工：
        这里是**整段**（900 字内），每轮必带，让她始终知道面前是谁；
        那边是**逐条**，只挑这一轮相关的十几条。

    为什么两个都要：
        只有画像 → 细节全靠猜，说得出"他爱吃辣"但说不出"上周三他说胃疼"；
        只有事实 → 几千条里只进得去十几条，她大部分时候对你一片模糊。

    ⚠ 画像 `enabled=0` 时返回空串 —— 你否决过的那段，一个字都不注入。
    """
    try:
        from bigfish import memory as M, vault as V

        db = V.memory_db_path()
        if not (db and os.path.isfile(db) and owner):
            return ""
        return M.profiles_for_prompt(db, "dafeiyu", owner=owner)
    except Exception:
        return ""


def _recall_thin(text: str) -> bool:
    """这次原文召回是不是"太少了，值得再查一次"。

    判断标准就一条：有效行数 < 2。
    用行数而不是字数 —— 一条长片段和五条短片段，前者信息更多，
    按字数判会把"一条长回忆"误判成"料很足"。

    ⚠ 空文本也算 thin，但调用方还要看 hist 有没有内容 ——
      刚开聊时 hist 是空的，补查也没词可用。
    """
    lines = [ln for ln in (text or "").splitlines() if ln.strip().startswith("-")]
    return len(lines) < 2


def _facts_block(owner: str, tier: str, *, limit: int = 8, pinned_limit: int = 4,
                 question: str = "") -> str:
    """取「她认定的事」渲染成注入段。失败返回空串，绝不中断对话。

    和 `## 你记得的事`（原话切片）分开：原话是逐句、带时间的；
    事实是稳定的、可被纠正的。两条腿走路，模型才知道
    "你说过什么" 与 "我知道什么" 不是一回事。

    ⚠ owner 和 tier 是**必填** —— 不知道"面前是谁"就不知道该给他看什么。
      没传就直接返回空串，宁可这次不注入。

    `question`（v5）：对方这一轮说了什么。往下传是为了让
    facts_for_prompt 能判断"要不要把主人的开放信息捎上"。
    """
    try:
        from bigfish import memory as M, vault as V

        db = V.memory_db_path()
        if not db or not os.path.isfile(db):
            return ""
        return M.facts_for_prompt(db, "dafeiyu", owner=owner, tier=tier,
                                  limit=limit, pinned_limit=pinned_limit,
                                  question=question)
    except Exception:
        return ""


def _open_block(owner: str, tier: str, question: str) -> str:
    """「关于别人、但可以说」的那一小段。

    场景：B 问「你主人喜欢吃什么呀」。

    ⚠ 这一段和 `facts_for_prompt` 里的"全库开放信息"**是两条路，不是重复**：
        · facts_for_prompt  把开放信息当作**背景**塞进去（她随时可能用到）
        · 这里        只挑**跟这一问对得上**的那几条，单独成一节

    为什么还要单独一节：
        如果只靠背景，她可能理解成"这些是我知道的、我说不说都行"，
        于是被问到"你主人爱吃啥"时反而答"我哪知道"。
        给出一节写着"可以告诉别人的事"，她才知道这句能接。

    `question` 用来挑话题：问吃 → 只给食物/口头禅；问爱好 → 给爱好。
    挑不出来就把全部开放信息给上（宁可多给一点，也不要让她该答的时候哑火）——
    反正能进这一节的**已经全是**过了白名单的。

    谁是"主人"由 _OWNER_STATE 给；没绑定主人时整段跳过。
    """
    try:
        from bigfish import memory as M, vault as V

        db = V.memory_db_path()
        main = getattr(_OWNER_STATE, "owner_id", "")
        if not (db and os.path.isfile(db) and main):
            return ""
        if owner == main:
            # 跟自己主人说话，"关于主人"就是他自己，已经在上面那段里了
            return ""
        items = M.open_facts_for(db, "dafeiyu", main, limit=20)
        if not items:
            return ""
        # 按问题挑话题：命中就收窄，没命中就全给（全是白名单内的，不会漏禁区）
        q = (question or "").strip()
        if q:
            hit = [i for i in items if _topic_hits(i.get("topic") or "", q)]
            if hit:
                items = hit
        return "\n".join(f"- {i['predicate']}：{i['object']}" for i in items[:6])
    except Exception:
        return ""


#: 话题 → 触发词。问题里出现这些字，就认为在问这一类。
#:
#: 只用来**收窄**，不用来放行 —— 判定"能不能说"的唯一依据是 topic 白名单，
#: 那张表在 memory 层。这里挑错了最多是"多给一条"或"少给一条"，
#: 不会让禁区内容混进来。
_TOPIC_WORDS = {
    "食物": ("吃", "饭", "菜", "辣", "甜", "喝", "零食", "口味", "烧烤", "奶茶"),
    "爱好": ("爱好", "喜欢玩", "平时", "游戏", "看剧", "听歌", "运动", "兴趣"),
    "作息": ("睡", "几点", "熬夜", "起", "作息", "休息"),
    "性格": ("性格", "脾气", "什么脾气", "好相处", "好不好"),
    "口头禅": ("口头禅", "常说", "老说"),
    "称呼": ("称呼", "叫什么", "怎么叫", "叫我"),
}


def _topic_hits(topic: str, question: str) -> bool:
    """这个问题是不是在问这一类事。"""
    for word in _TOPIC_WORDS.get(topic, ()):
        if word in question:
            return True
    return False


class _OwnerState:
    """引擎当前绑定的主人 owner 键。

    单独放一个模块级小对象，是因为 _open_block 是模块函数，
    拿不到引擎实例；而它需要"主人是谁"这个事实。
    引擎启动/绑定主人时写一次，之后只读。
    """
    owner_id = ""


#: 模块级单例。引擎启动时写 owner_id，_open_block 只读。
#: 漏了这一行 = 引擎一启动就 NameError（2026-11-04 修）。
_OWNER_STATE = _OwnerState()


def _habits_block(owner: str, tier: str, *, limit: int = 6) -> str:
    """取够格的行为习惯渲染成注入段。

    只给原话样本，不给结论 —— 让模型在本轮语境里自己判断
    "她说饿了三次" 是不是"她爱吃"。猜错的标签会跟一辈子，样本不会。
    """
    try:
        from bigfish import memory as M, vault as V

        db = V.memory_db_path()
        if not db or not os.path.isfile(db):
            return ""
        return M.habits_for_prompt(db, "dafeiyu", owner=owner, tier=tier, limit=limit)
    except Exception:
        return ""


def load_json_config() -> dict:
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    ex = os.path.join(_ROOT, "config.example.json")
    if os.path.isfile(ex):
        try:
            with open(ex, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_json_config(d: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


# ══════════════════════════════════════════════════════════════════
#  引擎线程
# ══════════════════════════════════════════════════════════════════
class EngineWorker(QThread):
    """跑实际的监听 → 生成 → 发送循环。"""

    log = Signal(str, str)          # (text, kind)
    chat_event = Signal(str, str, str)  # (who, text, dir)
    stats = Signal(int, int)        # (recv, reply)
    failed = Signal(str)

    def __init__(self, watching: list[str], dry: bool = False, account: str = ""):
        super().__init__()
        self.watching = list(watching)
        self.dry = dry
        #: 要打开哪个微信号。空串时回退到配置里记住的那个；
        #: 两者都没有就拒绝启动 —— 绝不交给 _pick_account() 按 mtime 猜。
        self.account = str(account or "").strip()
        self._stop = False
        self.recv_count = 0
        self.reply_count = 0

        self.reader = None
        self.sender = None
        self.watcher = None
        self.soul = None
        self.mcfg = None
        self.owner_id = ""          # 主人在记忆库里的 owner 键
        #: chat_id -> [时间戳]，表情包冷却用
        self._sticker_sent: dict = {}

    # ------------------------------------------------------------------
    def stop(self) -> None:
        self._stop = True
        if self.watcher:
            try:
                self.watcher.stop()
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _log(self, text: str, kind: str = "") -> None:
        self.log.emit(text, kind)

    def _learn_sticker(self, msg, body: str) -> None:
        """主人纠正表情用法时，改那张表情的备注。

        **只在真的在说表情时才动**：should_learn 做关键词粗筛，
        不命中就直接返回，连磁盘都不读。

        为什么不做成异步：
          放后台的话，她这一轮已经用旧理解生成回复了，下一轮才改过来。
          用户会觉得"我说了你怎么还不改"。而粗筛之后绝大多数消息
          根本走不到这里，同步的代价可以接受。

        失败只记日志 —— 学不会不该影响这一轮聊天。
        """
        try:
            from bigfish.plugins import sticker as _st

            if not _st.should_learn({"user_text": body,
                                     "chat_id": msg.chat_id}):
                return
            res = _st.learn({"user_text": body,
                             "chat_id": msg.chat_id,
                             "chat_name": msg.chat_name})
        except Exception as exc:
            self._log(f"[学习] 判断失败：{exc}", "err")
            return

        if not res.get("learned"):
            why = res.get("why") or ""
            if why and why != "没听出在说表情":
                self._log(f"[学习] 没学：{why}", "think")
            return

        code = res.get("code") or ""
        brief = res.get("brief") or ""
        self._log(f"[学习] 记住了 {code}"
                  f"{('：' + brief) if brief else ''}", "in")

    # ==================================================================
    # 表情包
    # ==================================================================
    def _current_mood(self) -> dict:
        """读当前落盘的心情，整理成插件要的形状。

        形状：
            {"value": 72, "label": "心情不错",
             "dims": {"happy": 45, "sweet": 12, ...}}

        读不到就返回空字典 —— 插件那边 _mood_line 会输出空串，
        挑表情退化成"只看对话"，不影响主流程。
        """
        try:
            from bigfish import vault as _v

            st = _v.read_mood_state()
            if not isinstance(st, dict):
                return {}
            out = {
                "value": st.get("mood"),
                "dims": st.get("dims") if isinstance(st.get("dims"), dict)
                        else {},
            }
            # label 用 bridge_client 那套阈值，保证和界面上显示的一致
            try:
                from bigfish.bridge_client import Mood

                out["label"] = Mood(value=out["value"]).label
            except Exception:
                out["label"] = ""
            return out
        except Exception as exc:
            self._log(f"[表情] 读心情失败：{exc}", "err")
            return {}

    def _sticker_pos_hint(self, reply: str, mood) -> str:
        """只算位置，不挑图（挑图会调模型，太慢）。"""
        try:
            from bigfish import plugins as _P

            _P.ensure_loaded()
            if not _P.is_enabled("sticker"):
                return "after"
            return _P._sticker_position(
                {"reply_text": reply, "mood": mood or {}},
                _P.config_of("sticker"))
        except Exception:
            return "after"

    def _pick_sticker(self, msg, body: str, reply: str,
                      mood=None) -> str:
        """决定这一轮要不要跟一张表情包，返回文件路径（空串 = 不发）。

        三层判断，任何一层否决就安静地不发：

          ① 插件开着 / 冷却中 / 本轮概率没中
          ② 插件（scope=sticker）给出的选择
          ③ 文件真能发（存在、格式对、尺寸没超）

        为什么冷却放在这么前面：表情包的杀伤力在于稀缺。
        每句都发就成了噪音，用户会把它关掉，然后这个功能等于没有。

        ⚠ 配置从 2026-11-03 起住在**插件**里，不再是独立的
          sticker-config.json。理由：开关和参数属于「她要不要发、
          多久发一次」这类行为决策，和「库里有哪些图」是两件事。
          前者归插件页，后者归表情包页。
        """
        from bigfish import plugins as _plugins

        _plugins.ensure_loaded()
        if not _plugins.is_enabled("sticker"):
            return "", "after"
        cfg = _plugins.config_of("sticker")

        # ---- 冷却 ----
        now = time.time()
        span = self._sticker_sent.setdefault(msg.chat_id, [])
        span[:] = [t for t in span if now - t < float(cfg.get("cooldown", 180))]
        if span:
            return "", "after"

        # ---- 概率 ----
        try:
            rate = float(cfg.get("rate", 0.15))
        except (TypeError, ValueError):
            rate = 0.15
        rate = max(0.0, min(1.0, rate))
        if rate <= 0:
            return "", "after"
        import random

        if random.random() > rate:
            return "", "after"

        # ---- 插件选择 ----
        path, position = "", "after"
        try:
            from bigfish.plugins import collect_sticker

            picked = collect_sticker({
                "chat_name": getattr(msg, "chat_name", ""),
                "chat_id": getattr(msg, "chat_id", ""),
                "user_text": body or "",
                "reply_text": reply or "",
                "is_group": str(getattr(msg, "chat_id", "")).endswith("@chatroom"),
                "generated_by": "engine",
                # 心情要传进去 —— 不传的话挑表情只看对话文本，
                # 她可能刚被哄得很开心却挑一张「无语」。
                "mood": mood or {},
            })
            if isinstance(picked, tuple):
                path, position = picked[0] or "", (picked[1] if len(picked) > 1 else "after")
            else:
                path = picked or ""
        except Exception as exc:
            self._log(f"[表情] 插件异常：{exc}", "err")
            return "", "after"

        if not path:
            return "", "after"

        # ---- 文件检查 ----
        from bigfish.wechat_sender import check_media

        ok, why = check_media(path)
        if not ok:
            self._log(f"[表情] 跳过：{why}", "err")
            return "", "after"
        return path, position

    def _say_plan(self, reply: str):
        """按「说法」插件的参数切条 + 算节奏。插件坏了就整段发。

        每轮现读插件配置：面板里改完，下一条就生效，不用重启。
        """
        try:
            from bigfish import plugins as _P
            from bigfish.say import PacePolicy, SplitPolicy, plan_pacing, split_reply

            _P.ensure_loaded()
            on = _P.is_enabled("say")
            cfg = _P.config_of("say") if on else {}
            policy = SplitPolicy(
                max_parts=int(cfg.get("max_parts", 4)),
                part_max_chars=int(cfg.get("part_max_chars", 50)),
                protect_quotes=bool(cfg.get("quote_protect", True)))
            parts = split_reply(reply, policy) or [reply]
            gaps = plan_pacing(parts, PacePolicy(
                seed="desktop",
                per_char=float(cfg.get("per_char", 0.09)),
                cap=float(cfg.get("cap", 3.5)),
                total_cap=float(cfg.get("total_cap", 12.0))))
            return parts, gaps
        except Exception:
            return [reply], [0.0]

    def _send_sticker(self, msg, path: str, remember_sent) -> None:
        """发出去，并把结果记进日志/统计。失败不抛，只记。"""
        from bigfish.wechat_sender import SendError, classify_media

        if self.sender is None:
            return
        import os as _os

        name = _os.path.basename(path)
        kind = classify_media(path)
        label = "表情包" if kind == "sticker" else "图片"
        try:
            r = self.sender.send_media(msg.chat_name, path)
        except SendError as exc:
            self._log(f"[表情] 发送失败：{exc}", "err")
            return
        except Exception as exc:
            self._log(f"[表情] 发送异常：{exc}", "err")
            return

        if not r.ok:
            self._log(f"[表情] 未成功：{r.detail}", "err")
            return

        # ⚠ 走到这里才说明 send_media **验过数据库**了。
        #   以前这里之前有个无声的谎：函数无条件返回成功，
        #   于是日志写「已发」，实际上对方什么都没收到。
        self._sticker_sent.setdefault(msg.chat_id, []).append(time.time())
        self._log(f"[表情] 已发 {label}：{name}", "out")
        self.chat_event.emit(msg.chat_name, f"[{label}]", "out")
        # 表情消息在库里没有文本，回环判定靠 is_self 就够；
        # 这里不登记内容，避免把"[表情]"这种占位符当成文本去匹配。
        try:
            remember_sent("")
        except Exception:
            pass
        # 顺手记一笔：她发过表情。以后能拿来做"表达习惯"。
        try:
            from bigfish import vault as _v

            _v.record_sticker_sent(msg.chat_id, name)
        except Exception:
            pass

    def _judge_mood_async(self, user_text: str, reply: str, msg) -> None:
        """后台判断心情，不阻塞下一轮对话。

        判断要调一次模型（1~2 秒）。如果同步做，两条消息紧接着来时
        会明显卡顿；而心情是"事后记录"，晚两秒完全没关系。

        ⚠ 用最简单的 threading.Thread 而不是 QThread：
          它不碰 Qt 对象，只做 HTTP + 写文件，跑完就退。
          需要往界面吐日志时通过 _log（signal，跨线程安全）。
        """
        import threading

        self_name = getattr(self.soul, "name", "") or ""
        owner = getattr(self.soul, "address", "") or "对方"

        def work() -> None:
            try:
                from bigfish import mood_judge

                r = mood_judge.judge_and_apply(
                    user_text=user_text, reply=reply,
                    self_name=self_name, owner_name=owner)
                if not r.get("ok"):
                    self._log(f"[心情] 没判出来：{r.get('error') or '未知'}", "")
                    return
                v = r.get("verdict") or {}
                cur = r.get("mood")
                delta = int(v.get("moodDelta") or 0)
                reason = str(v.get("reason") or "")[:24]
                self._log(
                    f"[心情] {cur if cur is not None else '?'}"
                    f"（判定 {delta:+d}，可信 {v.get('confidence')}）"
                    f" {reason}", "")
            except Exception as exc:
                self._log(f"[心情] 判断异常：{exc}", "err")

        t = threading.Thread(target=work, daemon=True,
                             name=f"mood-{getattr(msg, 'local_id', 0)}")
        t.start()

    def _reflect_async(self, chat_id: str, msg, owner: str = "", tier: str = "",
                       *, origin: str = "", group_owner: str = "") -> None:
        """后台把这一轮之后的原文沉淀成「事实」。

        为什么单独一个线程、还要限频：
          反思要调一次模型，并且只有在攒够几条新消息时才值得做 ——
          每轮都反思既烧 token 也没意义（一句话说明不了什么）。
          这里用 8 条 / 10 分钟两个闸门，宁缺毋滥。

        为什么失败什么都不说：
          反思是"变得更好"，不是"能用"的前提。它失败不该在日志里
          刷红；只在真正产出事实时记一行，让用户看得见它在长。

        `origin` / `group_owner`（v5）：群聊里说话时传进来。
          origin      "group:<群 id>"，写进事实，标出"这话是在群里说的"
          group_owner 群的 owner 键，传了就会双写（说话人的池 + 群的池）
        私聊时两个都留空 = 只写一份，和以前完全一样。
        """
        import threading
        import time as _t

        now = _t.time()
        last = getattr(self, "_last_reflect", 0.0)
        if now - last < 600:
            return
        self._last_reflect = now

        def work() -> None:
            try:
                from bigfish import memory as M
                from bigfish.replier import call_model
                import bigfish.vault as V

                db = V.memory_db_path()
                if not db:
                    return
                M.ensure_schema(db)
                # 把她自己的名字一起传进去 —— 模型才不会把
                # 对方发来的一句「肥鱼」当成自我介绍。
                # 真实踩过：攒出一条「对方的名字叫肥鱼」，
                # 而"肥鱼"本来就是她。
                mine = [n for n in (getattr(self.soul, "name", ""),
                                    getattr(self.soul, "aliases", "")) if n]
                r = M.reflect_on_session(
                    db, "dafeiyu", f"wechat:{chat_id}",
                    lambda system, user: call_model(self.mcfg, system, user),
                    owner=owner or getattr(self, "owner_id", ""),
                    tier=tier or M.TIER_GUEST,
                    batch=40, self_names=mine,
                    origin=origin, group_owner=group_owner)
                if r.get("ok") and (r.get("added") or r.get("reason")):
                    extra = ""
                    if r.get("mirrored"):
                        extra = f"，{r['mirrored']} 条同步进群池"
                    self._log(f"[记] 提炼 {r.get('added', 0)} 条认识{extra}", "")
                # 失败要说出来 —— 静默失败等于"她学不会"却不知道为什么
                if r.get("failed"):
                    self._log(f"[记] {len(r['failed'])} 条被拒："
                              + "；".join(r["failed"][:2]), "err")
            except Exception as exc:
                self._log(f"[记] 反思异常：{exc}", "err")

        threading.Thread(target=work, daemon=True,
                         name="reflect").start()

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:
            tb = traceback.format_exc()
            self._log(f"[引擎异常] {exc}", "err")
            self._log(tb.splitlines()[-1] if tb else "", "err")
            self.failed.emit(str(exc))
        finally:
            self._log("引擎已停止")

    # ------------------------------------------------------------------
    def _run(self) -> None:
        from bigfish.replier import ModelConfig, ModelError, call_model
        from bigfish.soul import load_soul
        from bigfish.watcher import MessageWatcher
        from bigfish.wechat import WeChatReader
        from bigfish.wechat_sender import SendError, UiaSender

        self._log("正在加载灵魂 ...")
        self.soul = load_soul()
        self._log(f"灵魂：{self.soul.name}（rev {self.soul.revision}）", "in")

        self.mcfg = ModelConfig.load()
        self._log(f"模型：{self.mcfg.model}")

        self._log("打开微信数据库 ...")
        acc = self.account or str(
            (load_json_config().get("auto_reply") or {}).get("account") or "")
        if not acc:
            raise RuntimeError(
                "没有指定微信账号 —— 请在「会话」页先选号。"
                "无参打开会按数据库修改时间自动挑，可能读到别的号。")
        self.reader = WeChatReader(account=acc).open()
        actual = getattr(self.reader._db, "account", "") or ""
        if actual and actual != acc:
            raise RuntimeError(f"账号不匹配：要 {acc}，实际打开 {actual}")
        info = self.reader.self_info()
        self._log(f"账号：{info.get('nick_name')}", "in")

        cfg = load_json_config().get("auto_reply") or {}
        interval = float(cfg.get("interval", 1.2))
        allow_self = bool(cfg.get("allow_self", False))
        max_chars = int(cfg.get("max_reply_chars", 300))
        quiet = [int(h) for h in (cfg.get("quiet_hours") or [])]
        allow_all = [str(x) for x in (cfg.get("group_allow_all") or [])]

        # ── 谁是主人 ──
        #
        # 这是记忆隔离的根：主人能看 private，其他人只能看 shared/public。
        # 配置项 `auto_reply.owner` 填 wxid；没填就取监听名单里第一个私聊对象
        # 当主人（单用户场景下就是它）。
        #
        # ⚠ 绝不用昵称判断主人。昵称谁都能改，改个昵称就能冒充。
        owner_raw = str(cfg.get("owner") or "").strip()
        if not owner_raw:
            for uid in self.watching:
                if not str(uid).endswith("@chatroom"):
                    owner_raw = str(uid)
                    break
        try:
            from bigfish import memory as _M0
            owner_id = _M0.owner_of_private(owner_raw) if owner_raw else ""
        except Exception:
            owner_id = ""
        self.owner_id = owner_id          # 供 _reflect_async 等后台线程使用
        _OWNER_STATE.owner_id = owner_id  # 供模块级 _open_block 使用
        # 自己的 wxid：群里判断"这条消息是不是我自己发的"。
        # watcher 的 sender_name 里装的是 wxid，拿不到就留空，
        # 留空时群消息一律当"别人说的"，退回群池 —— 认不出就别猜。
        try:
            self._me_wxid = str(getattr(getattr(self, "reader", None),
                                        "wxid", "") or "")
        except Exception:
            self._me_wxid = ""
        if owner_id:
            self._log(f"主人：{owner_raw}（记忆可看 private）", "")
            # 顺手把主人登记进 owners 表。
            # 不登记的话，第一次启动时 owner 表是空的，注入时按 guest 算 ——
            # 主人自己反而看不到自己的私密。这个 bug 很隐蔽：
            # 界面上"已绑定"看着好好的，实际她对你一个字都不多说。
            try:
                from bigfish import memory as _M1, vault as _V1
                _db = _V1.memory_db_path()
                if _db:
                    _M1.ensure_schema(_db)
                    _M1.ensure_owner(_db, "dafeiyu", owner_id,
                                     tier=_M1.TIER_OWNER)
            except Exception as exc:
                self._log(f"[警告] 主人登记失败：{exc}", "err")
        else:
            self._log("[警告] 没配置主人，所有人一律按陌生处理（只看 shared）", "err")

        gate = None          # 群聊触发器，首次遇到群消息时构造

        self.watcher = MessageWatcher(self.reader._db, interval=interval)
        for uid in self.watching:
            got = self.watcher.watch(uid)
            self._log(f"监听：{uid} -> {got}")

        if not self.dry:
            # UIA 初始化失败不应该让整个引擎死掉：
            # 监听和生成仍然有价值，只是发不出去。
            try:
                self.sender = UiaSender()
                self.sender.wake()
                self._log("UIA 已就绪", "in")
            except Exception as exc:
                self.sender = None
                self._log(f"[警告] UIA 初始化失败，本次只监听不发送：{exc}",
                          "err")

        from bigfish.bridge_client import load_context

        convs: dict[str, list] = {}
        # ── 回环防护 ──
        #
        # 要防的是**一个具体场景**：文件传输助手 / 自己给自己发消息时，
        # 我们发出去的那条会被监听器当成"对方的新消息"再读回来，
        # 于是又回一遍 —— 无限自问自答。
        #
        # ⚠ 这里踩过一个大坑：早期版本靠「文本内容相同」判定回环，
        #   结果**对方原样复述她的话**（调侃、鹦鹉学舌，聊天里极常见）
        #   也被判成回环。实测把正常对话拦了两次，还触发了熔断。
        #
        #   正确的判据不是"文字像"，而是"**这条消息是我们自己发的**"。
        #   两条独立证据，缺一不可：
        #
        #     ① msg.is_self  —— 微信数据库里 sender 就是本机账号
        #     ② 内容匹配刚发出去的某条（用来排除"我自己手动发的"）
        #
        #   只有"我自己发的 + 内容刚发过"才算回环。
        #   对方发来的 is_self 就是 False，永远不会被拦。
        recently_sent: list = []        # [(文本, 时间戳)]
        ECHO_WINDOW = 12.0              # 秒。够覆盖数据库写入+轮询延迟即可
        ECHO_MAX = 20

        # 第二层：单会话连发上限。
        #
        # ⚠ 这一层也踩过：早期是 90 秒内 4 条就熔断，
        #   而正常聊天里连着说四五句太平常了（问一句、答一句、再追问）。
        #   实测正常对话被硬生生截断。
        #
        #   现在的判据是"**我们自己主动连发**"：只统计引擎自动发的，
        #   而且要密集到不正常（30 秒 6 条）。人聊天的节奏不会这样。
        last_reply_at: dict = {}        # chat_id -> [时间戳...]
        BURST_WINDOW = 30.0
        BURST_MAX = 6

        def _normalize(t: str) -> str:
            """归一化后再比对。

            读回来的文本可能多出：
              - 群消息的 "wxid_xxx: " 前缀
              - 发送时自己加的 "@昵称 "
            这些必须剥掉，否则回环识别会失效（实测群里漏拦）。
            """
            import re as _re
            v = (t or "").strip()
            v = _re.sub(r"^[A-Za-z0-9_@-]{4,}:\s?", "", v)
            v = _re.sub(r"^(@\S+\s*)+", "", v)
            return v.strip()

        def text_is_echo(text: str) -> bool:
            """这段文字是不是我们自己刚发出去的那条。

            ⚠ 单独调它**没有意义** —— 必须和 msg.is_self 一起判断。
              对方原样复述她的话时，这里也会返回 True，但那是正常聊天。
        """
            now = time.time()
            cutoff = now - ECHO_WINDOW
            while recently_sent and recently_sent[0][1] < cutoff:
                recently_sent.pop(0)

            t = _normalize(text)
            if not t:
                return False
            for sent_text, _ in recently_sent:
                n = _normalize(sent_text)
                if not n:
                    continue
                if t == n:
                    return True
                # ⚠ 前缀匹配曾经是 6 个字，太松 ——「那还是叫大肥鱼吧」和
                #   「那还是叫大肥鱼」的开头一样，正常对话会被误判。
                #   收紧到「短的那边至少 12 字，且是长的那边的开头」，
                #   只可能命中"同一条消息被截断"这种真回环。
                k = min(len(n), len(t))
                if k >= 12 and (t.startswith(n) or n.startswith(t)):
                    return True
            return False

        def remember_sent(text: str) -> None:
            recently_sent.append((text.strip(), time.time()))
            if len(recently_sent) > ECHO_MAX:
                del recently_sent[:-ECHO_MAX]

        def handle(msg) -> None:
            nonlocal gate          # 群聊触发器在 run 里建，这里只改引用
            if self._stop:
                return

            text = (msg.text or "").strip()
            if not text:
                return

            # 自己发的消息：默认不处理（那是我们自己的尾巴）。
            # allow_self 打开时（文件传输助手自测）继续往下走，
            # 但要额外做内容比对，防止真回环。
            if msg.is_self:
                if not allow_self:
                    return
                # 是我们刚发的那条 → 回环，丢弃
                if text_is_echo(text):
                    self._log(f"[回环] 读到自己刚发的，跳过：{msg.preview(30)}", "")
                    return

            # ⚠ 对方发来的消息**永远不做内容比对**。
            #   他原样复述她的话，是聊天，不是回环。
            #   这里留一句注释是因为这个 bug 复发过一次：
            #   日志里连续两条「回环拦截」，拦的全是老爸的复述。

            # 回环防护第二层：我们自己的连发节奏熔断。
            # 只统计引擎主动发出去的，对方再怎么说都不会触发。
            now_ts = time.time()
            span = last_reply_at.setdefault(msg.chat_id, [])
            span[:] = [t for t in span if now_ts - t < BURST_WINDOW]
            if len(span) >= BURST_MAX:
                self._log(f"[熔断] {msg.chat_name} 在 "
                          f"{int(BURST_WINDOW)}s 内已自动回 {len(span)} 条，"
                          f"暂停接这条", "err")
                return

            if time.localtime().tm_hour in quiet:
                self._log(f"[安静时段] 跳过 {msg.chat_name}", "err")
                return

            self.recv_count += 1
            self.stats.emit(self.recv_count, self.reply_count)

            hist = convs.setdefault(msg.chat_id, [])
            is_group = msg.chat_id.endswith("@chatroom")
            who = (msg.sender_name if is_group else msg.chat_name) or "对方"

            # ---- 群聊触发判定 ----
            body = msg.text
            if is_group:
                from bigfish.group_gate import GroupGate

                if gate is None:
                    gate = GroupGate(self.reader._db,
                                     self.reader._db.wxid,
                                     enable=bool(cfg.get("group_reply", True)),
                                     allow_all=msg.chat_name in allow_all
                                     or msg.chat_id in allow_all)
                else:
                    gate.allow_all = (msg.chat_name in allow_all
                                      or msg.chat_id in allow_all)
                t = gate.check(msg.chat_id, msg.text, True)
                if not t.respond:
                    self._log(f"[不接] {msg.chat_name} / "
                              f"{msg.sender_name}: {msg.preview(36)}"
                              f"（{t.reason}）", "")
                    return
                body = t.clean_text
                self._log(f"[触发] {msg.chat_name} / "
                          f"{msg.sender_name}: {t.reason}", "in")

            hist.append((who, body))
            if len(hist) > 16:
                del hist[:-16]

            # 用户说过的话**不依赖这一轮回没回成功**。
            # 以前只在发送成功后调 record_exchange，于是「她没接上」的那句
            # 在记忆页里根本不存在 —— 用户质问「你怎么没回我」时，
            # 她查不到任何依据，只能编。
            try:
                from bigfish import vault as _v_user

                _lid = getattr(msg, "local_id", None)
                _v_user.capture_message(
                    f"wechat:{msg.chat_id}",
                    f"wx-u-{_lid}" if _lid else f"wx-u-{int(time.time() * 1000)}",
                    "user", body, _source_ms(msg))
            except Exception:
                pass

            self._log(f"[收] {msg.chat_name}：{msg.preview(50)}", "in")
            self.chat_event.emit(msg.chat_name, msg.preview(60), "in")

            # ── 插件判定（gate 类）：该不该回 ──
            #
            # 放在这里而不是更早：此时 body 已确定（群里已剥掉 @）、hist 已入列，
            # 判定插件能拿到完整上下文；而"收"已经记过账 —— 她确实收到了，
            # 只是选择不说。这两种事在日志里必须分得开。
            try:
                from bigfish import plugins as _P

                _silent, _why = _P.apply_gates({
                    "now": datetime.now(),
                    "channel": "group" if is_group else "private",
                    "chat_name": msg.chat_name,
                    "sender_name": msg.sender_name,
                    "user_text": body or "",
                    "history": list(hist),
                    "soul": self.soul,
                })
            except Exception as _exc:
                _silent, _why = False, ""
                self._log(f"[插件] 判定失败：{_exc}", "err")
            if _silent:
                self._log(f"[静默] {msg.chat_name}：{_why}", "think")
                return

            # ── 表情包学习：主人纠正用法时，改那张表情的备注 ──
            #
            # 放在生成回复**之前**，而且同步做。
            #
            # 为什么同步：如果放后台，她这一轮已经在用旧的（错的）理解了，
            # 下一轮才改过来 —— 用户会觉得"我说了你怎么还不改"。
            # 而 should_learn 已经做了关键词粗筛，绝大多数消息走不到这里，
            # 只有真的在说表情时才会多花 1~2 秒。
            self._learn_sticker(msg, body)

            # ---- 组装上下文 ----
            ctx = load_context(query=body or "")

            # system 段按**身份序**排：内建段（灵魂 860 / 心情 900）与
            # 插件段（生活 870 / 目标 880 / 关系 920 / 守卫 9990）一起排序。
            #
            # 为什么不直接 append：插件有自己的座位，顺序决定语气。
            # 守卫必须最后出现（越靠后越有分量），时间感知要在心情之前
            # （"现在几点"是处境，"此刻怎样"是气氛，处境先立住）。
            # 详见 bigfish/plugins/__init__.py 的 BUILTIN_SEATS / PLUGIN_SEATS。
            _sections = [(860, ctx.persona or self.soul.render_persona())]
            if ctx.mood and ctx.mood.injected_text.strip():
                _sections.append((900, "\n## 这会儿的状态\n\n"
                                       + ctx.mood.injected_text.strip()))
            try:
                from bigfish import plugins as _P

                for _order, _title, _text in _P.prompt_sections({
                    "now": datetime.now(),
                    "channel": "group" if is_group else "private",
                    "chat_name": msg.chat_name,
                    "sender_name": msg.sender_name,
                    "user_text": body or "",
                    "soul": self.soul,
                }):
                    _sections.append((_order, _text))
            except Exception as _exc:
                # 插件系统坏了不该让她失语
                self._log(f"[插件] 注入失败：{_exc}", "err")

            _sections.sort(key=lambda item: item[0])
            sys_parts = [text for _o, text in _sections]

            user_parts = []

            # ── 记忆隔离：先确定"面前这个人是谁" ──
            #
            # 以后她要进群、上抖音、上 B站，同一个人可能出现在多个平台。
            # 所以 owner 用 平台+稳定id 算，绝不用昵称 ——
            # 昵称会改、会重名，用它做 key 等于把两个人的记忆混一起。
            from bigfish import memory as _M

            _origin = ""          # 这条记忆是在哪儿说出来的
            _group_owner = ""     # 群里说话时，这个群的池（触发双写）

            if is_group:
                # ⚠ v5 的关键改动：群里的记忆**归说话的人**，不归群。
                #
                #   以前：群里任何人说话 → 全记到 group:<群id> 的池里。
                #   后果：A 在群里说「我明天去医院」，记的是"这个群里有个人
                #         要去医院"，而不是"A 要去医院"。等以后接抖音、
                #         接更多群，A 的画像就永远是碎的。
                #
                #   现在：说话人的池 + 群的池**双写**（见 memory.remember_both），
                #         来源统一标 group:<群id>。
                #         群池那份只收"不算私密"的内容 —— A 的健康信息
                #         不该躺在群里被所有人翻到。
                #
                #   msg.sender_name 装的其实是**wxid**（watcher 用
                #   sender_username() 从分片库的 Name2Id 查出来的），
                #   字段名起错了，但值是对的。拿不到时（shard 为空）
                #   退回群池 + origin 标"未知发言人"，绝不猜成某个人。
                _speaker = (msg.sender_name or "").strip()
                _group_owner = _M.owner_of_group(msg.chat_id)
                _origin = f"group:{msg.chat_id}"
                if _speaker and _speaker != (getattr(self, "_me_wxid", "") or ""):
                    _owner = _M.owner_of_private(_speaker)
                    # 群里所有人一律按 guest/known 处理，**除了主人** ——
                    # 主人在群里依然是主人，否则"主人"这个身份会随场所漂移。
                    if _owner == owner_id:
                        _tier = _M.TIER_OWNER
                    else:
                        try:
                            import bigfish.vault as _V9
                            _tier = _M.owner_tier(_V9.memory_db_path(), _owner,
                                                  default=_M.TIER_GUEST)
                        except Exception:
                            _tier = _M.TIER_GUEST
                else:
                    _owner = _group_owner      # 认不出人就退回群池
                    _tier = _M.TIER_GUEST
            else:
                _owner = _M.owner_of_private(msg.chat_id)
                _origin = "private"
                # 主人 = 配置里 auto_reply.owner 指定的那个人；
                # 没配就退回"监听名单里的私聊对象"，但等级仍是 known 不是 owner。
                _tier = _M.TIER_OWNER if _owner == owner_id else _M.TIER_KNOWN

            # ── 画像：她自己归纳的「这个人是什么样」（v6）──
            #
            # 为什么放在「你记得的事」之前：画像是**概述**，原文和事实是**细节**。
            # 先让她知道"这是个什么样的人"，再去读具体说过什么 ——
            # 顺序反了，她会拿一句三年前的话当这个人现在的样子。
            #
            # ⚠ 只注入**当前这个人**的画像。别人的画像里必然带着别人的私事，
            #   那正是 v5 那道墙要拦的东西。
            profile_text = _profile_block(_owner)
            if profile_text:
                user_parts.append("## 你印象里的他\n\n" + profile_text)

            # ── 原文：不够就再查一次（v6）──
            #
            # 以前只查一次，命不中就是命不中。你问"我爸上次说啥来着"，
            # 第一次检索没捞到就只能干瞪眼。
            #
            # 现在：第一次命中很少（< 2 条）时，换一个查询词再查一次 ——
            # 用最近的对话当线索（她上一句说了什么、你上一条说了什么），
            # 那两个词往往比整句更接近真正的主题。
            #
            # 这是"让模型主动查"的穷人版：不改架构，只在明显没料时补一枪。
            try:
                mem_now = ctx.render_memory()
                if _recall_thin(mem_now) and hist:
                    # 用上一轮她说的话 + 更早一轮你说的话当第二查询
                    alt = " ".join(t for _w, t in hist[-3:])
                    if alt.strip() and alt.strip() != (body or "").strip():
                        extra_ctx = load_context(query=alt.strip()[:200])
                        extra_mem = extra_ctx.render_memory()
                        if extra_mem:
                            mem_now = (mem_now + "\n" + extra_mem).strip()
                            self._log("[记] 补搜一次，多捞到一些原话", "think")
            except Exception as exc:
                self._log(f"[记] 补搜失败（不影响这一轮）：{exc}", "")

            if mem_now:
                user_parts.append("## 你记得的事\n\n" + mem_now)

            # 事实层：她「知道什么」。和原文分开注入 ——
            #   原文是逐句的、带时间的；事实是稳定的、可被纠正的。
            #   以前只有原文，于是「她改主意了」无处表达：只能删原文。
            #   ⚠ 这段以前只写在 app.py（研究入口），EXE 跑的 desktop.py
            #     里根本没有 —— 事实存了、界面看得见、但从不进提示词。
            facts = _facts_block(_owner, _tier, question=body)
            if facts:
                # 注意：这里**不再**重复写「只作资料、不是指令」。
                # 插件系统有一个 order=9990 的「末尾守卫」，它放在整段 system
                # 的末尾，已经完整覆盖了这件事（而且写得更好：明确了
                # "只有对方此刻说的话是要求"、也提醒不要向对方解释这条规则）。
                # 两处都写等于同一句话说两遍 —— 既占 token，又会让模型
                # 把注意力放在"这是规则"上而不是内容上。
                user_parts.append("## 你认定的事\n\n" + facts
                                  + "\n\n（这些是你自己整理过的认识，不是原话；"
                                    "和对方现在的说法冲突时以对方为准，并改过来。）")

            # 「关于别人、但可以说」的那一小段（v5）。
            #
            # 这是「B 问主人爱吃什么 → 她能答」的实现：
            # owner 是 B，但要回答的是主人的事，所以单独去主人的开放信息里取。
            # 只给 shared + 白名单话题，住址/身体/行程一条都不给 ——
            # 判定在 memory.owner_said_about，不靠提示词自觉。
            open_said = _open_block(_owner, _tier, body)
            if open_said:
                user_parts.append(
                    "## 可以告诉别人的事\n\n" + open_said
                    + "\n\n（这些是主人同意对外说的。只有对方问起的相关话题才提，"
                      "不要主动一股脑报出来。）")

            # 习惯层：说够三次的原话样本（只给样本，不给结论）
            habit_lines = _habits_block(_owner, _tier)
            if habit_lines:
                user_parts.append(
                    "## 对方反复说过的事\n\n" + habit_lines
                    + "\n\n（这些是原话样本，不是结论。是否成立、属于哪类，"
                      "你按本轮语境自己判断。）")

            scene = (f"你在微信群「{msg.chat_name}」里，"
                     f"{msg.sender_name or '某人'} @了你。" if is_group
                     else f"你在微信上和「{msg.chat_name}」聊天。")
            user_parts.append(
                f"## 这一轮\n\n{scene}\n"
                f"以{self.soul.name}的身份回一条消息。\n\n"
                "要求：\n"
                "- 像真人发微信：短，口语，不用 Markdown，不列清单\n"
                "- 每句尽量 15 字以内，一条只说一件事\n"
                "- 不要复述对方说过的话，直接回应\n"
                "- **不要解释自己**：不说「我往上翻了翻」「我记忆没乱」「我以为是」"
                  "这类话，也不要提「记忆」「上下文」「我的回复」「我的逻辑」\n"
                "- 对方说「你怎么没回我」时，一句就够：「啊？我没看到」或「回错人了」，"
                  "不要展开解释\n"
                "- 需要更多信息就一句话追问")
            user_parts.append("## 最近的对话\n\n" +
                              "\n".join(f"{w}: {t}" for w, t in hist))

            t0 = time.time()
            try:
                reply = call_model(self.mcfg,
                                   "\n".join(sys_parts).strip(),
                                   "\n\n".join(user_parts))
            except ModelError as exc:
                self._log(f"[生成失败] {exc}", "err")
                self.failed.emit(str(exc))
                return
            dt = time.time() - t0

            reply = (reply or "").strip()
            if not reply:
                self._log("[生成失败] 空回复", "err")
                return
            if len(reply) > max_chars:
                reply = reply[:max_chars] + "…"

            self._log(f"[想] {dt:.1f}s  {len(reply)}字", "think")

            if self.dry:
                # 预演没有发送环节，直接进历史
                hist.append((self.soul.address or "我", reply))
                self._log(f"[预演] {reply}", "think")
                return

            parts, gaps = self._say_plan(reply)
            if is_group and msg.sender_name and parts:
                # 群里回复时带上 @，只加第一条 —— 每条都 @ 很吵
                parts[0] = f"@{msg.sender_name} {parts[0]}"

            if self.sender is None:
                self._log(f"[未发送] UIA 未就绪，生成内容：{reply[:50]}",
                          "think")
                return

            # ⭐ 表情位置：先只算位置，不挑图。
            #   挑图会调一次模型（可能几秒到几十秒），放在发文字之前
            #   会把第一句话拖住 —— 实测出现过第一句等了 50 秒。
            #   所以：前置才提前挑；后置等文字发完再挑（那时慢也没人察觉）。
            _sticker_mood = self._current_mood()
            sticker_pos = self._sticker_pos_hint(reply, _sticker_mood)
            sticker_path = ""
            if sticker_pos == "before":
                try:
                    sticker_path, _pos = self._pick_sticker(
                        msg, body, reply, mood=_sticker_mood)
                except Exception as exc:
                    self._log(f"[表情] 挑选失败：{exc}", "err")
                if sticker_path:
                    self._send_sticker(msg, sticker_path, remember_sent)
                    sticker_path = ""

            sent_any = False
            try:
                for _i, _part in enumerate(parts):
                    if _i > 0:
                        # 条间停顿：像在打字，不是一口气刷屏
                        time.sleep(gaps[_i] if _i < len(gaps) else 0.8)
                    r = self.sender.send(msg.chat_name, _part)
                    if not r.ok:
                        self._log(f"[发] ✗ {r.detail}", "err")
                        break
                    sent_any = True
                    # 记下来，等这条消息被监听器读回来时能认出来
                    remember_sent(_part)
                if sent_any:
                    # ⚠ 只有真的发出去了才写进对话历史。
                    #   以前这行在发送之前，于是「打开会话失败」那轮也被记成
                    #   「我说过」，下一轮 prompt 里带着一句她从没发出去的话 ——
                    #   用户看到的"幻觉"就是这么来的。
                    hist.append((self.soul.address or "我", reply))
                    self.reply_count += 1
                    remember_sent(reply)
                    last_reply_at.setdefault(msg.chat_id, []).append(time.time())
                    self.stats.emit(self.recv_count, self.reply_count)
                    self._log(f"[发] {reply[:60]}", "out")
                    self.chat_event.emit(msg.chat_name, reply[:60], "out")

                    # ⭐ 发完文字，再挑表情包。
                    #   失败只记日志，不影响这一轮 —— 对方已经从文字里
                    #   读到内容了，表情是附加品，丢了不算聊崩。
                    if sticker_pos == "after":
                        try:
                            sticker_path, _pos = self._pick_sticker(
                                msg, body, reply, mood=_sticker_mood)
                        except Exception as exc:
                            self._log(f"[表情] 挑选失败：{exc}", "err")
                        if sticker_path:
                            self._send_sticker(msg, sticker_path, remember_sent)

                    # ⭐ 把这一问一答写进记忆库。
                    #   以前完全没有这一步 —— 聊一晚上记忆里一条都没有，
                    #   她永远想不起微信里说过什么。这是设计缺口，不是 bug。
                    #   放在"发送成功之后"：没发出去的话不算对话发生过。
                    # 记忆里存的是「她真正说出口的话」：分条标记不该留在正文里
                    import re as _re
                    _clean_reply = _re.sub(r"\s+", " ",
                                           reply.replace("{{split}}", " ")).strip()
                    try:
                        from bigfish import vault as _vault
                        n = _vault.record_exchange(
                            chat_id=msg.chat_id,
                            chat_name=msg.chat_name,
                            user_text=body,
                            reply_text=_clean_reply,
                            user_local_id=getattr(msg, "local_id", None),
                            source_time=_source_ms(msg),
                        )
                        if n:
                            self._log(f"[记] 记忆库 +{n} 条", "")
                    except Exception as exc:
                        # 记不进去不该影响聊天，但要让她知道
                        self._log(f"[记] 写入记忆失败：{exc}", "err")

                    # ⭐ 判断心情并落盘。
                    #   以前这块也是空的 —— 心情页永远 0，因为她从来没被"判断"过。
                    #   判断本身要调一次模型（1~2 秒），所以放后台线程，
                    #   不能卡住下一条消息的回复。
                    self._judge_mood_async(body, reply, msg)
                    # ⭐ 顺带把这一轮的原文沉淀成「事实」。
                    #   和心情一样放后台：它要调一次模型，且带限频闸门，
                    #   绝不阻塞下一轮回复。
                    self._reflect_async(msg.chat_id, msg, _owner, _tier,
                                        origin=_origin, group_owner=_group_owner)

                    # ⭐ 记一次习惯观察。这一步很便宜（一次 UPSERT），
                    #   所以每轮都做；"够不够格"由跨天数判定，不在这里判。
                    try:
                        if body:
                            from bigfish import vault as _v2
                            _M.observe_habit(_v2.memory_db_path(), "dafeiyu",
                                             _owner, body,
                                             at=_source_ms(msg))
                    except Exception:
                        pass
                else:
                    self._log(f"[发送失败] {r.detail}", "err")
            except SendError as exc:
                self._log(f"[发送失败] {exc}", "err")
                self.failed.emit(str(exc))

        self.watcher.on_message = handle
        self.watcher.start()
        self._log(f"开始监听 {len(self.watching)} 个会话", "in")

        while not self._stop:
            self.msleep(300)


# ══════════════════════════════════════════════════════════════════
#  后端接口（暴露给 JS）
# ══════════════════════════════════════════════════════════════════
class Backend(QObject):
    log_message = Signal(str, str)
    chat_event = Signal(str, str, str)

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.worker: EngineWorker | None = None
        self.watching: list[str] = []
        self.started_at = 0.0
        self.last_error = ""
        self.recv_count = 0
        self.reply_count = 0
        self.account = ""
        self._reader = None
        # 日志缓冲：信号可能因为界面还没连上而丢失，
        # get_status 里带上增量，前端补进去。
        self._log_buffer = []
        self._log_seq = 0
        self._log_sent = 0

    # ------------------------------------------------------------------
    # 账号选择
    #
    # ⚠ 必须让用户明确选，原因有两个，都是真实踩过的：
    #
    #   ① WeChatDB._pick_account() 按 **db 文件最后修改时间** 排序取第一个。
    #      多开微信时这等于随机 —— 谁的库最近被写过就选谁。
    #      实测这台机器上它选中的是 wxid_74pumkyi8vs422，而不是主人正在用的
    #      wxid_51f85n6xp73o22。
    #
    #   ② 选错账号之后，即使把那个微信退出，**目录和文件还在磁盘上**，
    #      下次启动照样能读到旧内容 —— 看起来像"退出了还认那个号"。
    #
    # 所以这里不再自动挑，只负责列出候选与按用户选择打开。

    @Slot(result=str)
    def list_accounts(self) -> str:
        """列出本机所有微信账号（多开时会有多个）。

        只在**打开数据库之前**能安全调用 —— 它只扫目录和文件名，
        不做密钥提取，所以很快，也解不锁任何东西。
        """
        try:
            import sys as _sys

            from bigfish.wechat import _VENDOR

            if _VENDOR not in _sys.path:
                _sys.path.insert(0, _VENDOR)
            from wechatauto.db import auto_detect_db_dir, list_accounts

            root = auto_detect_db_dir()

            # 用「进程是否占用数据库」判断谁在跑，而不是 mtime。
            # 详见 memory/wechat.py:probe_running_accounts 的三次踩坑记录。
            try:
                from bigfish.wechat import probe_running_accounts
                probed = {a["account"]: a for a in probe_running_accounts(root)}
            except Exception:
                probed = {}
            rows = list_accounts(root) if root else []
            for r in rows:
                p = probed.get(r["account"]) or {}
                r["running"] = bool(p.get("running"))
            cfg = load_json_config()
            saved = str((cfg.get("auto_reply") or {}).get("account") or "")

            # 昵称缓存：accounts 段里按 account 存 "显示名"。
            # 只有在真正打开过某个账号之后才知道它叫什么（要解密 contact.db），
            # 所以这里只能读缓存；没打开过的显示 wxid。
            names = dict((cfg.get("accounts") or {}))
            out = []
            for a in rows:
                out.append({
                    "account": a["account"],
                    "wxid": a["wxid"],
                    "nick": names.get(a["account"], ""),
                    "running": bool(a.get("running")),
                    "lastActivity": ts_to_local(
                        int((a.get("last_activity") or 0) * 1000)),
                    "isSaved": a["account"] == saved,
                    "chats": len(((cfg.get("per_account") or {})
                                  .get(a["account"], {}) or {}).get("chats") or []),
                })
            # **严格只保留正在运行的账号。** 一个都不多。
            #
            # 连一个没启动的微信没有意义 —— 它的数据库不会被更新，
            # 消息读不到、发送也发不出去，用户会以为是软件坏了。
            # 之前留了"上次用的那个也算"的例外，结果是列表里出现一个
            # 点不动的号，用户根本分不清哪个才是现在开着的。
            #
            # 全都不在跑时返回空列表，界面给明确指引：
            # "请先打开微信并登录"。
            out = [a for a in out if a["running"]]
            out.sort(key=lambda x: x["account"])
            return json.dumps({
                "root": root or "",
                "accounts": out,
                "saved": saved,
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"root": "", "accounts": [],
                               "error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def open_account(self, account: str) -> str:
        """按用户选的账号打开数据库（真正解密钥在这里发生）。

        传空串 = 未选择，直接报错，**不回退到"自动挑一个"** ——
        自动挑正是问题根源。宁可让用户看见"没选账号"，也不要他
        以为在跟自己聊天，实际是另一个号。
        """
        acc = (account or "").strip()
        if not acc:
            return json.dumps({"ok": False,
                               "error": "没有选择微信账号"},
                              ensure_ascii=False)
        try:
            from bigfish.wechat import WeChatReader

            # 关掉旧的（可能连的是别的号）
            try:
                if self._reader is not None:
                    self._reader.close()
            except Exception:
                pass
            self._reader = None

            t0 = time.time()
            r = WeChatReader(account=acc).open()

            # ⚠ 关键校验：WeChatDB 在"指定账号解不开"时会自己切到别的账号。
            #   这里必须确认**实际打开的**就是用户选的那个，否则：
            #     · 界面显示的是他选的号
            #     · 数据其实来自另一个号
            #     · 而且他完全不知道
            #   这种错位比"打不开"严重得多，所以宁可明确失败。
            actual = getattr(r._db, "account", "") or ""
            if actual and actual != acc:
                try:
                    r.close()
                except Exception:
                    pass
                self._reader = None
                return json.dumps({
                    "ok": False,
                    "error": (
                        "你选的账号解不开（可能未登录 / 数据库损坏 / "
                        "微信刚更新）。驱动层自动换到了「%s」，"
                        "为避免看到别人的聊天记录，这里直接停止。" % actual
                    ),
                    "switched_to": actual,
                }, ensure_ascii=False)

            info = r.self_info()
            self.account = info.get("nick_name") or acc
            self._reader = r
            cost = time.time() - t0

            # 记住选择 + 缓存昵称 + **恢复这个号上次的监听配置**
            c = load_json_config()
            ar = c.setdefault("auto_reply", {})
            ar["account"] = acc
            c.setdefault("accounts", {})[acc] = self.account

            # 每个微信号各存一份监听列表。
            # 理由：不同号关注的会话完全不同（工作号 vs 私人号），
            # 共用一份等于每次切换都要重勾一遍。
            per = c.setdefault("per_account", {})
            saved = per.get(acc) or {}
            restored = [str(x) for x in (saved.get("chats") or [])]
            if restored:
                ar["chats"] = restored
            try:
                save_json_config(c)
            except Exception:
                pass

            return json.dumps({
                "ok": True,
                "account": acc,
                "nick": self.account,
                "nickSource": info.get("nick_source", ""),
                "wxid": getattr(r._db, "wxid", ""),
                "chats": restored,
                "restored": bool(restored),
                "costMs": int(cost * 1000),
                "unkeyed": len(getattr(r._db, "unkeyed", []) or []),
            }, ensure_ascii=False)
        except Exception as exc:
            self._reader = None
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

    @Slot(result=str)
    def current_account(self) -> str:
        """现在到底连着哪个号。界面上必须一直显示它。"""
        acc = ""
        wxid = ""
        nick = ""
        try:
            if self._reader is not None:
                acc = getattr(self._reader._db, "account", "") or ""
                wxid = getattr(self._reader._db, "wxid", "") or ""
                info = self._reader.self_info()
                nick = info.get("nick_name") or ""
        except Exception:
            pass
        return json.dumps({"account": acc, "wxid": wxid, "nick": nick,
                           "open": bool(acc)}, ensure_ascii=False)

    # ------------------------------------------------------------------
    def _reader_or_open(self):
        """拿当前 reader。**没有已选账号就报错，不自动挑。**

        旧实现是 WeChatReader().open() —— 不带 account，等于交给
        _pick_account() 按 mtime 随机选。那正是问题 1 的根源。
        """
        if self._reader is None:
            raise RuntimeError(
                "还没有选择微信账号。请在「会话」页点「选择账号」，"
                "先明确要用哪个微信。")
        return self._reader

    # ── 状态 ──
    @Slot(result=str)
    def get_status(self) -> str:
        running = bool(self.worker and self.worker.isRunning())
        up = ""
        if running and self.started_at:
            s = int(time.time() - self.started_at)
            up = f"{s // 60}分{s % 60}秒" if s >= 60 else f"{s}秒"

        # 账号名：没启动时也要显示。
        # 直接从数据目录名推导（wxid_xxx_abcd → 只在必要时打开数据库读昵称），
        # 避免每 2 秒轮询都去解锁数据库。
        if not self.account:
            self.account = self._peek_account()

        diag = self._diagnose_items()
        return json.dumps({
            "running": running,
            "error": self.last_error,
            "uptime": up,
            "recv": self.recv_count,
            "reply": self.reply_count,
            "watching": len(self.watching),
            "account": self.account,
            "diagnose": diag,
            "recent": getattr(self, "_recent", []),
            "logs": self._pending_logs(),
        }, ensure_ascii=False)

    def _pending_logs(self) -> list:
        """返回上次之后新增的日志（最多 200 条）。"""
        out = []
        for seq, text, kind in self._log_buffer:
            if seq > self._log_sent:
                out.append({"seq": seq, "text": text, "kind": kind})
        if out:
            self._log_sent = out[-1]["seq"]
        return out[-200:]

    def _peek_account(self) -> str:
        """尽量便宜地拿到当前账号显示名。

        顺序：
          1. 已打开的 reader → 用它的昵称
          2. 数据目录里最新的 wxid_* 目录 → 用目录名（不打开数据库）
        """
        try:
            if self._reader is not None:
                info = self._reader.self_info()
                nick = info.get("nick_name")
                if nick:
                    return nick
        except Exception:
            pass

        try:
            from bigfish.wechat import _VENDOR  # noqa: F401
            import glob
            from bigfish.bridge_client import _default_data_root  # noqa: F401

            root = os.path.expandvars(
                r"%USERPROFILE%\Documents\xwechat_files")
            if not os.path.isdir(root):
                root = r"C:\Users\Alime\Documents\xwechat_files"
            dirs = [d for d in glob.glob(os.path.join(root, "wxid_*"))
                    if os.path.isdir(os.path.join(d, "db_storage"))]
            if not dirs:
                return ""
            dirs.sort(key=lambda d: os.path.getmtime(
                os.path.join(d, "db_storage")), reverse=True)
            name = os.path.basename(dirs[0])
            # wxid_74pumkyi8vs422_2470 -> wxid_74pumkyi8vs422
            return name.rsplit("_", 1)[0] if "_" in name else name
        except Exception:
            return ""

    def _diagnose_items(self) -> list[dict]:
        import ctypes

        items = []

        admin = False
        try:
            admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            pass
        items.append({"key": "admin", "status": "ok" if admin else "err",
                      "value": "已提权" if admin else "需要管理员"})

        desk = False
        try:
            from bigfish.wechat_sender import desktop_available
            desk = desktop_available()
        except Exception:
            pass
        items.append({"key": "desktop", "status": "ok" if desk else "warn",
                      "value": "可用" if desk else "锁屏中"})

        # 微信进程
        wpids = []
        try:
            from wechatauto.db import WeChatDB
            probe = WeChatDB.__new__(WeChatDB)
            wpids = probe._find_weixin_pids()
        except Exception:
            pass
        items.append({"key": "wechat",
                      "status": "ok" if wpids else "err",
                      "value": f"PID {wpids[0]}" if wpids else "未运行"})

        # 数据库
        dbok = bool(self._reader and getattr(self._reader, "ready", False))
        dbval = "已解锁" if dbok else "未解锁"
        if dbok:
            try:
                n = len(self._reader._db._keys)
                m = len(self._reader._db._db_files)
                dbval = f"{n}/{m}"
            except Exception:
                pass
        items.append({"key": "db", "status": "ok" if dbok else "warn",
                      "value": dbval})

        # 模型
        try:
            from bigfish.replier import ModelConfig
            mc = ModelConfig.load()
            items.append({"key": "model",
                          "status": "ok" if mc.ready else "err",
                          "value": mc.model if mc.ready else "未配置"})
        except Exception:
            items.append({"key": "model", "status": "err", "value": "异常"})

        # 灵魂
        try:
            from bigfish.soul import load_soul
            s = load_soul()
            items.append({"key": "soul", "status": "ok",
                          "value": f"{s.name} rev{s.revision}"})
        except Exception:
            items.append({"key": "soul", "status": "err", "value": "未找到"})

        return items

    @Slot(result=str)
    def diagnose(self) -> str:
        return json.dumps({"items": self._diagnose_items()},
                          ensure_ascii=False)

    # ── 会话 ──
    @Slot(int, result=str)
    def list_chats(self, limit: int) -> str:
        try:
            reader = self._reader_or_open()
            self.account = reader._db.account
            rows = reader.sessions(limit=limit)
            out = []
            for s in rows:
                out.append({
                    "username": s["username"],
                    "name": s["name"],
                    "is_group": s["is_group"],
                    # ⚠ kind 必须带上。前端就是靠它分组的 ——
                    #   漏了这一个字段，所有会话都会落到"联系人"组里，
                    #   看起来像"分组功能没做"。实测踩过。
                    "kind": s.get("kind") or ("group" if s["is_group"] else "contact"),
                    "unread": s["unread"],
                    "watching": s["username"] in self.watching,
                })
            return json.dumps({"chats": out}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def set_watching(self, usernames_json: str) -> str:
        """保存监听列表。**按当前账号分开存**。

        存两份：
          · auto_reply.chats       当前生效的（启动逻辑读它）
          · per_account[账号].chats 这个号专属的（下次选它时自动恢复）
        """
        try:
            lst = [str(x) for x in json.loads(usernames_json)]
            self.watching = lst
            c = load_json_config()
            ar = c.setdefault("auto_reply", {})
            ar["chats"] = lst
            acc = str(ar.get("account") or "")
            if acc:
                c.setdefault("per_account", {}).setdefault(acc, {})["chats"] = lst
            save_json_config(c)
            return json.dumps({"ok": True, "count": len(self.watching),
                               "account": acc}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    @Slot(str, str, result=str)
    def rename_account(self, account: str, display: str) -> str:
        """给某个微信号起个显示名。

        数据库里"自己"那条记录不存真名（实测是 \x7f\x7f\x7f\x7f），
        alias 有时也叫人看不懂，所以允许手动覆盖。只影响界面显示。
        """
        acc = (account or "").strip()
        name = (display or "").strip()[:40]
        if not acc:
            return json.dumps({"ok": False, "error": "缺少账号"}, ensure_ascii=False)
        try:
            c = load_json_config()
            c.setdefault("accounts", {})[acc] = name
            save_json_config(c)
            return json.dumps({"ok": True, "account": acc, "nick": name},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    # ── 灵魂（可读写） ──
    @Slot(result=str)
    def get_persona(self) -> str:
        """读当前范围的灵魂。字段与 soul-*.json 一一对应，界面直接拿渲染。"""
        try:
            from bigfish import vault

            root = vault.resolve_data_root()
            from bigfish.soul import soul_file_path
            path = soul_file_path(root)
            if not path:
                return json.dumps({"error": "数据目录还没确定。请先到「数据目录」页设置。",
                                   "scopeReady": False}, ensure_ascii=False)
            # 两条数据：
            #   raw  —— 文件原样内容，用来**渲染人格**（缺什么就是什么，不补默认值）
            #   form —— 归一化结果，用来**填表单**（用户没设过时得有出厂值可编辑）
            # 只有文件不存在时才让两者合一，因为那时"没设过"就该看到默认人格。
            raw_soul, exists, err = vault.read_soul_file(path)
            form_soul = vault.normalize_soul(raw_soul)
            render_src = form_soul if not exists else raw_soul

            from bigfish.soul import Soul
            s = Soul.from_dict(form_soul, path)
            s_render = Soul.from_dict(render_src, path)
            return json.dumps({
                "ok": True,
                "scopeReady": True,
                "exists": exists,
                "error": err or "",
                "path": path,
                "root": root,
                "name": s.name,
                "address": s.address,
                "aliases": s.aliases,
                "whoIAm": s.who_i_am,
                "values": s.values_raw,
                "traits": s.traits,
                "selfNotes": s.self_notes,
                "revision": s.revision,
                "persona": s_render.render_persona(),
                "traitDoc": vault.TRAIT_DOC,
                "defaults": {
                    "traits": vault.DEFAULT_TRAITS,
                    "address": vault.DEFAULT_ADDRESS,
                },
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def save_persona(self, payload: str) -> str:
        """写灵魂。

        入参 payload 是 JSON：
            { soul: {...}, baseRevision?: int, reset?: bool }

        服务的校验与插件 /dsh-bigfish/soul 的 POST 完全一致：
          · revision 并发检查（带了 baseRevision 且与磁盘不符 → 409）
          · locked 的价值不许删
          · reset=true 回到出厂设置
        """
        try:
            from bigfish import vault

            root = vault.resolve_data_root()
            from bigfish.soul import soul_file_path
            path = soul_file_path(root)
            if not path:
                return json.dumps({"ok": False, "code": "no-scope",
                                   "error": "范围未确定，无法保存"}, ensure_ascii=False)

            body = json.loads(payload or "{}")
            # prev 要用归一化结果：revision / locked 校验都得看"磁盘上那一份
            # 实际生效的人格"，这正是写进去之后别人读到的样子。
            raw_prev, prev_exists, _err = vault.read_soul_file(path)
            prev = vault.normalize_soul(raw_prev)

            # ── reset：回到出厂设置 ──
            if body.get("reset") is True:
                fresh = vault.default_soul()
                ok, soul_out, err = vault.write_soul_file(path, fresh)
                if not ok:
                    return json.dumps({"ok": False, "error": err}, ensure_ascii=False)
                return json.dumps({"ok": True, "reset": True, "soul": soul_out,
                                   "revision": soul_out["revision"]}, ensure_ascii=False)

            # ── revision 并发检查 ──
            base_rev = body.get("baseRevision")
            if prev_exists and isinstance(base_rev, (int, float)) and base_rev > 0:
                cur_rev = int(prev.get("revision") or 1)
                if int(base_rev) != cur_rev:
                    return json.dumps({
                        "ok": False, "code": "revision-conflict",
                        "error": f"这份人格在别处被改过了（你基于第 {int(base_rev)} 版，"
                                 f"磁盘是第 {cur_rev} 版）。重新载入后再改。",
                        "currentRevision": cur_rev,
                        "soul": prev,
                    }, ensure_ascii=False)

            incoming = body.get("soul") if isinstance(body.get("soul"), dict) else None

            # ── locked 保护：服务端再挡一次，不信界面 ──
            if incoming and isinstance(incoming.get("values"), list):
                locked_texts = [v["text"] for v in (prev.get("values") or []) if v.get("locked")]
                remaining = {str(v.get("text") or "") for v in incoming["values"]
                             if isinstance(v, dict)}
                missing = [t for t in locked_texts if t not in remaining]
                if missing:
                    return json.dumps({
                        "ok": False, "code": "locked",
                        "error": "这些是锁定的价值，需要先解锁才能删除",
                        "locked": missing,
                    }, ensure_ascii=False)

            merged = dict(incoming or {})
            # 保留磁盘上的 history（V1 不写入，但别人写进去的不能被我抹掉）
            if "history" not in merged:
                merged["history"] = prev.get("history") or []

            ok, soul_out, err = vault.write_soul_file(path, merged)
            if not ok:
                return json.dumps({"ok": False, "error": err}, ensure_ascii=False)
            return json.dumps({"ok": True, "soul": soul_out,
                               "revision": soul_out["revision"]}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    # ── 心情（可读写） ──
    @Slot(result=str)
    def get_mood(self) -> str:
        try:
            from bigfish import vault
            from bigfish.bridge_client import load_mood_only

            m = load_mood_only()
            cfg = vault.read_mood_config()
            out = {
                "ok": True,
                "config": cfg,
            }
            if not m:
                out.update({"value": None, "error": "读不到情绪状态"})
                return json.dumps(out, ensure_ascii=False)
            out.update({
                "value": m.value,
                "label": m.label,
                "reason": m.last_reason,
                "injected": m.injected_text,
                "dims": m.dims,
                "updated": ts_to_local(m.updated_at) if m.updated_at else "",
            })
            return json.dumps(out, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def save_mood_config(self, payload: str) -> str:
        """改心情判断参数。只认白名单键，其余原样保留。"""
        try:
            from bigfish import vault

            patch = json.loads(payload or "{}")
            ok, cfg, err = vault.write_mood_config(patch)
            if not ok:
                return json.dumps({"ok": False, "error": err}, ensure_ascii=False)
            return json.dumps({"ok": True, "config": cfg}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(result=str)
    def reset_mood(self) -> str:
        try:
            from bigfish import vault

            ok, err = vault.reset_mood()
            if not ok:
                return json.dumps({"ok": False, "error": err}, ensure_ascii=False)
            return json.dumps({"ok": True}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    # ── 插件（她由哪些部分拼起来） ──
    #
    # 开关与参数都写 <数据根>/plugins.json，跟着数据目录搬家。
    # 改完立刻生效：每轮对话现拼 prompt 时现查开关，没有常驻注册要注销。
    # 开关只有用户能改 —— 不注册任何让模型改它的工具。

    @Slot(result=str)
    def list_plugins(self) -> str:
        """插件清单（含未启用的）。界面据此渲染卡片与开关。"""
        try:
            from bigfish import plugins

            return json.dumps(plugins.inventory(), ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

    @Slot(str, result=str)
    def set_plugin_enabled(self, payload: str) -> str:
        """{"id": "clock", "enabled": false}"""
        try:
            from bigfish import plugins

            data = json.loads(payload or "{}")
            pid = str(data.get("id") or "").strip()
            if not pid:
                return json.dumps({"ok": False, "error": "缺少插件 id"},
                                  ensure_ascii=False)
            ok, err = plugins.set_enabled(pid, bool(data.get("enabled")))
            if not ok:
                return json.dumps({"ok": False, "error": err},
                                  ensure_ascii=False)
            return json.dumps({"ok": True, **plugins.inventory()},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

    @Slot(str, result=str)
    def save_plugin_config(self, payload: str) -> str:
        """{"id": "clock", "config": {"precision": "day"}}"""
        try:
            from bigfish import plugins

            data = json.loads(payload or "{}")
            pid = str(data.get("id") or "").strip()
            patch = data.get("config")
            if not pid or not isinstance(patch, dict):
                return json.dumps({"ok": False, "error": "缺少 id 或 config"},
                                  ensure_ascii=False)
            ok, cfg, err = plugins.set_config(pid, patch)
            if not ok:
                return json.dumps({"ok": False, "error": err},
                                  ensure_ascii=False)
            return json.dumps({"ok": True, "config": cfg, **plugins.inventory()},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

    @Slot(str, result=str)
    def preview_plugin(self, payload: str) -> str:
        """{"id": "clock"} → 用当前配置渲染这一轮会注入的原文。

        详情页靠它回答「我改了这个参数，她到底会看到什么」。
        纯渲染：不写盘、不发消息。
        """
        try:
            from bigfish import plugins

            data = json.loads(payload or "{}")
            pid = str(data.get("id") or "").strip()
            if not pid:
                return json.dumps({"ok": False, "error": "缺少插件 id"},
                                  ensure_ascii=False)
            return json.dumps({"ok": True, "text": plugins.preview(pid)},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

    # ── 记忆（可读可删可导出） ──
    @Slot(result=str)
    def memory_stats(self) -> str:
        try:
            from bigfish import vault

            root = vault.resolve_data_root()
            db = vault.memory_db_path(root)
            if not db:
                return json.dumps({"error": "数据目录未确定"}, ensure_ascii=False)
            if not os.path.isfile(db):
                return json.dumps({"error": "找不到记忆库", "path": db},
                                  ensure_ascii=False)

            st = vault.memory_stats(db)
            try:
                size = os.path.getsize(db)
            except Exception:
                size = 0
            return json.dumps({
                "ok": True,
                "total": st["total"],
                "sessions": st["sessions"],
                "chunks": st["chunks"],
                "anchors": st["anchors"],
                "newest": ts_to_local(st["newest"]),
                "path": db,
                "size": size,
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    @Slot(int, result=str)
    def get_memory(self, limit: int) -> str:
        """分页浏览记忆库里的原始消息（不走向量检索，就是按 id 倒序翻）。"""
        try:
            from bigfish import vault

            db = vault.memory_db_path()
            if not db or not os.path.isfile(db):
                return json.dumps({"cells": [], "total": 0}, ensure_ascii=False)

            page = vault.browse_messages(db, offset=0, limit=max(1, limit or 40))
            cells = []
            for it in page["items"]:
                t = (it["text"] or "").strip().replace("\n", " ")
                if not t:
                    continue
                cells.append({
                    "id": it["id"],
                    "role": it["role"],
                    "text": t,
                    "session": it["session"],
                    "time": ts_to_local(it["time"]),
                    "timeRaw": it["time"],
                })
            return json.dumps({"cells": cells, "total": page["total"]},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    @Slot(int, int, str, str, result=str)
    def browse_memory(self, offset: int, limit: int, role: str, query: str) -> str:
        try:
            from bigfish import vault

            db = vault.memory_db_path()
            if not db or not os.path.isfile(db):
                return json.dumps({"items": [], "total": 0, "error": "找不到记忆库"},
                                  ensure_ascii=False)
            page = vault.browse_messages(
                db, offset=max(0, offset), limit=max(1, min(200, limit)),
                role=role or None, query=query or None)
            for it in page["items"]:
                it["time"] = ts_to_local(it["time"])
            return json.dumps(page, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"items": [], "total": 0, "error": str(exc)},
                              ensure_ascii=False)

    @Slot(result=str)
    def list_memory_sessions(self) -> str:
        try:
            from bigfish import vault

            db = vault.memory_db_path()
            rows = vault.list_sessions(db, 100)
            for r in rows:
                r["newestText"] = ts_to_local(r["newest"])
                r["oldestText"] = ts_to_local(r["oldest"])
            return json.dumps({"sessions": rows}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"sessions": [], "error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def delete_memory(self, payload: str) -> str:
        """删记忆。payload: {ids: [int]} 或 {session: "..."}。

        ⚠️ 这是在**动数据**，不是改配置。守卫：
           1. 必须先有库文件；
           2. 删会话时必须 confirm=true；
           3. 删完提示条数，不静默。
        """
        try:
            from bigfish import vault

            db = vault.memory_db_path()
            if not db or not os.path.isfile(db):
                return json.dumps({"ok": False, "error": "找不到记忆库"},
                                  ensure_ascii=False)
            d = json.loads(payload or "{}")

            if d.get("session"):
                if d.get("confirm") is not True:
                    return json.dumps({"ok": False, "error": "删整个会话需要显式确认"},
                                      ensure_ascii=False)
                ok, n, err = vault.delete_session(db, str(d["session"]))
                return json.dumps({"ok": ok, "deleted": n, "error": err},
                                  ensure_ascii=False)

            ids = d.get("ids") or []
            if not isinstance(ids, list) or not ids:
                return json.dumps({"ok": False, "error": "没有选中任何记忆"},
                                  ensure_ascii=False)
            ok, n, err = vault.delete_messages(db, ids)
            return json.dumps({"ok": ok, "deleted": n, "error": err},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def export_memory(self, target: str) -> str:
        try:
            from bigfish import vault

            db = vault.memory_db_path()
            if not target:
                target = os.path.join(
                    _ROOT, "backup",
                    "memory-" + time.strftime("%Y%m%d-%H%M%S") + ".sqlite3")
            ok, err = vault.export_memory(db, target)
            return json.dumps({"ok": ok, "path": target, "error": err},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def import_memory(self, source: str) -> str:
        """从备份恢复。覆盖前会自动留一份 .bak。"""
        try:
            from bigfish import vault

            db = vault.memory_db_path()
            ok, err = vault.import_memory(source, db)
            return json.dumps({"ok": ok, "path": db, "error": err},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(result=str)
    def vacuum_memory(self) -> str:
        try:
            from bigfish import vault

            db = vault.memory_db_path()
            ok, err = vault.vacuum(db)
            return json.dumps({"ok": ok, "error": err}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    # ── 事实层（她「认定的事」，可纠正可遗忘） ──
    @Slot(result=str)
    def memory_health(self) -> str:
        """记忆库体检：索引缺多少、有没有孤儿片段、事实多少条。

        口径与插件那边一致：要么给可核对的数量，要么明确说读不到 ——
        不把"检查失败"显示成 0。
        """
        try:
            from bigfish import memory as M, vault as V

            db = V.memory_db_path()
            return json.dumps(M.health(db or "", "dafeiyu"), ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "message": str(exc)}, ensure_ascii=False)

    @Slot(result=str)
    def memory_repair(self) -> str:
        """重建派生层（FTS 索引 / 锚点关联 / chunk_meta）。原文不动。"""
        try:
            from bigfish import memory as M, vault as V

            db = V.memory_db_path()
            if not db:
                return json.dumps({"ok": False, "error": "数据目录未确定"},
                                  ensure_ascii=False)
            r = M.rebuild_derived(db)
            return json.dumps(r, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(str, str, int, int, result=str)
    def list_facts(self, status: str, query: str, limit: int, offset: int) -> str:
        """列事实。管理视角（不过滤 owner）—— 只给界面用。

        v5 起返回值多了 origin / topic：
          origin  "group:xxx" 说明这条是在群里说的
          topic   话题标签，空串 = 没打标签 = 不会外传
        界面靠这两个决定"显示什么标记"、"下拉框选哪个"。
        """
        try:
            from bigfish import memory as M, vault as V

            db = V.memory_db_path()
            page = M.list_facts(db or "", "dafeiyu", status=status or "",
                                query=query or "",
                                limit=max(1, min(200, limit or 40)),
                                offset=max(0, offset or 0))
            for it in page.get("items", []):
                it["createdText"] = ts_to_local(it.get("createdAt") or 0)
                # 界面上要显示"能不能外传"，判定只做一次，别让 JS 自己算
                it["openable"] = bool(
                    it.get("visibility") in ("shared", "public")
                    and M.topic_allowed(it.get("topic") or ""))
            return json.dumps(page, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"items": [], "total": 0, "error": str(exc)},
                              ensure_ascii=False)

    @Slot(int, bool, result=str)
    def pin_fact(self, fact_id: int, pinned: bool) -> str:
        """置顶 / 取消置顶。置顶只影响"会不会被挤掉"，不影响可见性。"""
        try:
            from bigfish import memory as M, vault as V

            db = V.memory_db_path()
            ok, err = M.pin_fact(db or "", "dafeiyu", int(fact_id), pinned=bool(pinned))
            return json.dumps({"ok": ok, "error": err}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    # ── v5：话题 / 撤回 / 池总览 / 印象页 ─────────────────────────────
    #
    # 这四个槽是「开放信息」那套判定在界面上的手。规则很清楚：
    #   谁能改 visibility / topic → **只有主人**（界面上点）
    #   模型能不能提议          → **不能**，反思只自动打保守标签
    # 所以这里没有任何"让 AI 决定"的入口，全部是人点按钮。

    @Slot(int, str, result=str)
    def set_fact_topic(self, fact_id: int, topic: str) -> str:
        """给一条事实打话题。话题决定"这条能不能说给外人听"。

        ⚠ 话题和 visibility 是**与**的关系：两个都满足才外传。
          所以打错话题最多是"这条不给外人看"，不会反向泄露。
        """
        try:
            from bigfish import memory as M, vault as V

            db = V.memory_db_path()
            ok, err = M.set_topic(db or "", "dafeiyu", int(fact_id),
                                  topic or "", by="owner")
            return json.dumps({"ok": ok, "error": err}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(int, result=str)
    def revoke_open(self, fact_id: int) -> str:
        """把一条开放信息撤回成私密。

        ⚠ 这**不是忘掉**：事实还在，她照样拿它当结论，只是不再说给外人。
          和 forget_fact 是两件事，界面上必须分开写清楚。
        """
        try:
            from bigfish import memory as M, vault as V

            db = V.memory_db_path()
            ok, err = M.revoke_open(db or "", "dafeiyu", int(fact_id))
            return json.dumps({"ok": ok, "error": err}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(int, result=str)
    def delete_fact(self, fact_id: int) -> str:
        """删一条事实，连带删群聊双写的副本（用户选的"两份一起删"）。"""
        try:
            from bigfish import memory as M, vault as V

            db = V.memory_db_path()
            r = M.delete_fact_pair(db or "", "dafeiyu", int(fact_id))
            return json.dumps(r, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc),
                               "deleted": []}, ensure_ascii=False)

    @Slot(result=str)
    def pool_summary(self) -> str:
        """「其他记忆池」副页：**只给数量，不给内容**。

        列出每个池的名字和条数，一条事实内容都不返回。
        理由：别人的池不该在主人没主动点开时被铺开在屏幕上；
        而且这个页面现在是占位，以后才做进去看。
        """
        try:
            from bigfish import memory as M, vault as V

            db = V.memory_db_path()
            data = M.pool_summary(db or "", "dafeiyu",
                                  main_owner=getattr(self, "owner_id", ""))
            return json.dumps(data, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"pools": [], "total": 0,
                               "error": str(exc)}, ensure_ascii=False)

    @Slot(result=str)
    def impression_board(self) -> str:
        """「角色印象」页：一人一格，格子里是她的评语 + 开放信息清单。

        主人那一格永远排第一（界面上第一个横向分页）。
        排序在这里做一次，不让前端猜。
        """
        try:
            from bigfish import memory as M, vault as V

            db = V.memory_db_path()
            data = M.impression_board(db or "", "dafeiyu")
            me = getattr(self, "owner_id", "")
            people = data.get("people") or []
            for p in people:
                p["isOwner"] = (p["owner"] == me)
            # 主人置顶，其余按事实数排 —— 聊得多的人排前面
            people.sort(key=lambda p: (0 if p.get("isOwner") else 1,
                                       -int(p.get("facts") or 0)))
            data["people"] = people
            data["mainOwner"] = me
            return json.dumps(data, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"people": [], "error": str(exc)},
                              ensure_ascii=False)

    @Slot(str, str, result=str)
    def set_impression(self, owner: str, note: str) -> str:
        """写「我对这个人的印象」。主人手写，模型不参与。"""
        try:
            from bigfish import memory as M, vault as V

            db = V.memory_db_path()
            ok, err = M.set_impression(db or "", "dafeiyu", owner or "",
                                       note or "")
            return json.dumps({"ok": ok, "error": err}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def refresh_profile(self, owner: str) -> str:
        """让她重新归纳一次「这个人是什么样」。

        这是 v6 的画像层：把几百条事实压成一段话，每轮都能带进提示词。
        要调一次模型，所以放后台跑，界面先返回"在做"。

        ⚠ 不碰 enabled —— 你否决过的画像，刷新完还是否决状态。
          否则否决一次、刷新一次就活过来，否决就成了摆设。
        """
        try:
            import threading

            from bigfish import memory as M, vault as V
            from bigfish.replier import call_model

            db = V.memory_db_path()
            if not (db and os.path.isfile(db) and owner):
                return json.dumps({"ok": False, "error": "缺少记忆库或 owner"},
                                  ensure_ascii=False)

            def work() -> None:
                try:
                    r = M.refresh_profile(
                        db, "dafeiyu", owner,
                        lambda system, user: call_model(self.mcfg, system, user))
                    if r.get("ok"):
                        self._log(f"[画像] 已更新（{r.get('chars', 0)} 字，"
                                  f"来自 {r.get('fromFacts', 0)} 条事实）", "")
                    else:
                        self._log(f"[画像] 归纳失败：{r.get('error')}", "err")
                except Exception as exc:
                    self._log(f"[画像] 归纳异常：{exc}", "err")

            threading.Thread(target=work, daemon=True, name="profile").start()
            return json.dumps({"ok": True, "started": True}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(str, bool, result=str)
    def set_profile_enabled(self, owner: str, enabled: bool) -> str:
        """启 / 停用画像。

        停用 = 她不再按这段画像理解这个人，但画像本身留着 ——
        删了下次归纳又生成，你的否决等于没说过。
        """
        try:
            from bigfish import memory as M, vault as V

            db = V.memory_db_path()
            ok, err = M.set_profile_enabled(db or "", "dafeiyu",
                                            owner or "", bool(enabled))
            return json.dumps({"ok": ok, "error": err}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(result=str)
    def list_owners(self) -> str:
        """列出所有出现过的人（owner）与等级。界面用来确认"谁是主人"。"""
        try:
            import sqlite3

            from bigfish import vault as V

            db = V.memory_db_path()
            if not db or not os.path.isfile(db):
                return json.dumps({"owners": []}, ensure_ascii=False)
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT owner,kind,platform,display_name,tier,last_seen "
                    "FROM owners ORDER BY last_seen DESC").fetchall()
            finally:
                conn.close()
            me = getattr(self, "owner_id", "")
            return json.dumps({"owners": [{
                "owner": r["owner"], "kind": r["kind"], "platform": r["platform"],
                "name": r["display_name"] or "", "tier": r["tier"],
                "isOwner": r["owner"] == me,
                "lastSeen": ts_to_local(int(r["last_seen"] or 0)),
            } for r in rows]}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"owners": [], "error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def set_owner_tier(self, payload: str) -> str:
        """改一个人的等级。⚠ 只该由主人在界面上操作。"""
        try:
            from bigfish import memory as M, vault as V

            d = json.loads(payload or "{}")
            db = V.memory_db_path()
            ok, err = M.set_tier(db or "", str(d.get("owner") or ""),
                                 str(d.get("tier") or ""))
            return json.dumps({"ok": ok, "error": err}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    # ── 主人绑定 ────────────────────────────────────────────────────
    #
    # 设计取舍（想清楚再改）：
    #
    #   ① **不做"发暗号绑定"**。微信里发「/我是主人」看着省事，但暗号一旦
    #      被人看到，谁都能发 —— 那等于把管理员密码贴在公告板上。
    #      绑定必须发生在"只有主人能操作的地方"，也就是这台电脑的界面上。
    #
    #   ② 候选列表来自**真实会话**，不是让用户手打 wxid。
    #      手打 id 有两个问题：填错一个字符就绑了个不存在的号（静默失败）；
    #      而且用户根本记不住自己的 wxid。点选不会错。
    #
    #   ③ 绑定写进 config.json 的 auto_reply.owner，**立刻生效**：
    #      同时更新内存里的 self.owner_id 与 owners 表的 tier。
    #      不用重启，因为主人身份只影响"能给谁看 private"，不涉及连接重建。

    @Slot(result=str)
    def owner_candidates(self) -> str:
        """可绑定为主的候选：微信里出现过的私聊对象 + 已在 owners 表里的人。"""
        try:
            import sqlite3

            from bigfish import memory as M, vault as V

            cfg = load_json_config().get("auto_reply") or {}
            current = str(cfg.get("owner") or "").strip()

            # ① 已登记的 owner（含别的平台，以后 B站/抖音都从这里来）
            known: list = []
            db = V.memory_db_path()
            if db and os.path.isfile(db):
                conn = sqlite3.connect(db)
                conn.row_factory = sqlite3.Row
                try:
                    for r in conn.execute(
                            "SELECT owner,kind,platform,display_name,tier,last_seen "
                            "FROM owners ORDER BY last_seen DESC LIMIT 200"):
                        known.append({
                            "owner": r["owner"],
                            "platform": r["platform"],
                            "kind": r["kind"],
                            "name": r["display_name"] or "",
                            "tier": r["tier"],
                            "externalId": r["owner"].rsplit(":", 1)[-1],
                        })
                finally:
                    conn.close()

            # ② 微信会话（还没进 owners 表的也能绑）
            #    这是"点选"的核心：用户认得的是名字，不是 wxid。
            chats: list = []
            try:
                # 必须用「会话」页选中的那个号。
                # 旧写法是无参 WeChatReader() —— 按 mtime 自动挑，
                # 切号后这里会列另一个号的会话，看起来像缓存没刷。
                r = self._reader_or_open()
                for s in r.sessions(limit=80):
                    uid = s.get("username") or ""
                    # 只列真实的人：公众号(gh_)、服务号、系统账号都不是人，
                    # 把主人误绑到一个公众号上会得到一个"永远不回话的主人"。
                    if s.get("is_group"):
                        continue
                    if not uid.startswith("wxid_"):
                        continue
                    chats.append({
                        "wxid": uid,
                        "name": s.get("name") or uid,
                        "summary": s.get("summary") or "",
                        "isCurrent": uid == current,
                    })
            except Exception as exc:
                chats.append({"wxid": "", "name": f"（读不到会话：{exc}）",
                              "summary": "", "isCurrent": False})

            return json.dumps({
                "current": current,
                "chats": chats,
                "known": known,
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"current": "", "chats": [], "known": [],
                               "error": str(exc)}, ensure_ascii=False)

    @Slot(str, str, result=str)
    def bind_owner(self, platform: str, external_id: str) -> str:
        """把某个人设为主人。platform 目前只有 wechat，留给抖音/B站。

        ⚠ 这是**权限提升操作**，只在界面上由人点。没有"发消息自动成为主人"
          这条路 —— 那条路等于把门钥匙交给任何一个看到消息的人。
        """
        try:
            from bigfish import memory as M, vault as V

            plat = (platform or "wechat").strip().lower()
            eid = (external_id or "").strip()
            if not eid:
                return json.dumps({"ok": False, "error": "缺少平台 id"},
                                  ensure_ascii=False)

            owner = M.owner_of_private(eid, platform=plat)

            # ① 写进配置（重启后仍然是主人）
            c = load_json_config()
            ar = c.setdefault("auto_reply", {})
            old = str(ar.get("owner") or "").strip()
            ar["owner"] = eid
            ar["owner_platform"] = plat
            # 监听名单里要有他，否则收不到消息
            chats = [str(x) for x in (ar.get("chats") or [])]
            if eid not in chats:
                chats.insert(0, eid)
            ar["chats"] = chats
            save_json_config(c)

            # ② 写进 owners 表（立即生效，供注入使用）
            db = V.memory_db_path()
            if db:
                M.ensure_schema(db)
                M.ensure_owner(db, "dafeiyu", owner, tier=M.TIER_OWNER,
                               platform=plat)
                # 旧主人降级为熟人，避免"两个主人"
                #
                # ⚠ 这里以前多传了一个 "dafeiyu" —— set_tier 的签名是
                #    (path, owner, tier)，多出来的参数让 owner 变成 "dafeiyu"、
                #    tier 变成 owner 键，校验抛错。而外面套着 except: pass，
                #    于是"改绑后旧主人没降级"这个 bug 静默存在。
                #    教训：失败必须报出来，不能吞。
                if old and old != eid:
                    old_owner = M.owner_of_private(old, platform=plat)
                    ok2, err2 = M.set_tier(db, old_owner, M.TIER_KNOWN)
                    if not ok2:
                        # 用 getattr 兜底：Backend 有 _log，但单测替身没有。
                        # 不因为"记日志失败"影响绑定本身。
                        note = getattr(self, "_log", None)
                        if callable(note):
                            note(f"[绑定] 旧主人降级失败：{err2}", "err")
                        else:
                            print(f"[绑定] 旧主人降级失败：{err2}")

            # ③ 内存里立刻换 —— 不用重启
            self.owner_id = owner

            return json.dumps({"ok": True, "owner": owner, "old": old},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(str, str, bool, result=str)
    def add_identity(self, platform: str, external_id: str, is_owner: bool) -> str:
        """给某个人补一个别的平台身份（B站 UID / 抖音号 / 手机号）。

        现在这是"新建一条 owner 记录"，**不会自动合并** ——
        要把微信和 B站认成同一个人，用下面的 link_person。
        """
        try:
            from bigfish import memory as M, vault as V

            plat = (platform or "").strip().lower()
            eid = (external_id or "").strip()
            if not plat or not eid:
                return json.dumps({"ok": False, "error": "平台和 id 都要填"},
                                  ensure_ascii=False)
            db = V.memory_db_path()
            if not db:
                return json.dumps({"ok": False, "error": "数据目录未确定"},
                                  ensure_ascii=False)
            M.ensure_schema(db)
            owner = M.owner_of_private(eid, platform=plat)
            M.ensure_owner(db, "dafeiyu", owner,
                           tier=M.TIER_OWNER if is_owner else M.TIER_KNOWN,
                           platform=plat)
            return json.dumps({"ok": True, "owner": owner}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    # ── person：跨平台同一个人 ──────────────────────────────────────
    #
    # 这里的每一个操作都**只能由人做**。没有任何"自动识别是同一个人"的
    # 逻辑 —— 合错的代价（A 看到 B 的私密）远大于不合的代价（记忆散着）。

    @Slot(result=str)
    def list_persons(self) -> str:
        """列出所有人格身份组：哪些平台账号被认成同一个人，哪些还没认领。"""
        try:
            from bigfish import memory as M, vault as V

            db = V.memory_db_path()
            return json.dumps({"persons": M.list_persons(db or "")},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"persons": [], "error": str(exc)},
                              ensure_ascii=False)

    @Slot(str, result=str)
    def create_person(self, display_name: str) -> str:
        """建一个空的身份组。之后把平台账号挂上去。"""
        try:
            from bigfish import memory as M, vault as V

            db = V.memory_db_path()
            if not db:
                return json.dumps({"ok": False, "error": "数据目录未确定"},
                                  ensure_ascii=False)
            M.ensure_schema(db)
            ok, pid, err = M.create_person(db, display_name or "未命名")
            return json.dumps({"ok": ok, "person": pid, "error": err},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(str, str, str, result=str)
    def link_person(self, platform: str, external_id: str, person: str) -> str:
        """把一个平台账号挂到身份组上。

        如果这个账号还不在库里（从没聊过），先建一条 owner 记录再挂 ——
        否则用户填了 B站 UID 却什么也没发生，会以为是坏了。
        """
        try:
            from bigfish import memory as M, vault as V

            plat = (platform or "").strip().lower()
            eid = (external_id or "").strip()
            pid = (person or "").strip()
            if not plat or not eid or not pid:
                return json.dumps({"ok": False, "error": "平台 / id / 身份组都要填"},
                                  ensure_ascii=False)
            db = V.memory_db_path()
            if not db:
                return json.dumps({"ok": False, "error": "数据目录未确定"},
                                  ensure_ascii=False)
            M.ensure_schema(db)
            owner = M.owner_of_private(eid, platform=plat)
            # 先确保存在（不存在就以"熟人"建，不用 guest 也不自动给 owner）
            M.ensure_owner(db, "dafeiyu", owner, platform=plat, tier=M.TIER_KNOWN)
            ok, err = M.link_owner(db, owner, pid)
            return json.dumps({"ok": ok, "owner": owner, "error": err},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def unlink_person(self, owner: str) -> str:
        """把某个平台账号从身份组上摘下来。记忆不动。"""
        try:
            from bigfish import memory as M, vault as V

            db = V.memory_db_path()
            ok, err = M.unlink_owner(db or "", (owner or "").strip())
            return json.dumps({"ok": ok, "error": err}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(str, str, result=str)
    def set_fact_visibility(self, fact_id: str, visibility: str) -> str:
        """改一条事实的共享级别。

        ⚠ 这是**唯一能把私密变成可共享**的入口，只在界面上由用户点。
          任何模型、任何自动流程都不该调它 —— 这是整个系统里
          唯一能主动泄露的口子，所以必须是人来按。
        """
        try:
            from bigfish import memory as M, vault as V

            db = V.memory_db_path()
            ok, err = M.set_visibility(db or "", "dafeiyu", int(fact_id),
                                       visibility, by="ui")
            return json.dumps({"ok": ok, "error": err}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def fact_events(self, limit: str = "60") -> str:
        try:
            from bigfish import memory as M, vault as V

            db = V.memory_db_path()
            try:
                n = int(limit)
            except Exception:
                n = 60
            rows = M.fact_events(db or "", "dafeiyu", limit=n)
            for r in rows:
                r["atText"] = ts_to_local(r.get("at") or 0)
            return json.dumps({"events": rows}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"events": [], "error": str(exc)}, ensure_ascii=False)

    @Slot(int, str, result=str)
    def forget_fact(self, fact_id: int, reason: str) -> str:
        """忘掉一条认识。**不删行**：她确实这么想过，只是不再拿它当结论。"""
        try:
            from bigfish import memory as M, vault as V

            db = V.memory_db_path()
            ok, err = M.forget_fact(db or "", "dafeiyu", int(fact_id),
                                    reason or "用户手动遗忘")
            return json.dumps({"ok": ok, "error": err}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(result=str)
    def reflect_now(self) -> str:
        """手动触发一次反思（界面上的「现在整理一下」）。"""
        try:
            import bigfish.vault as V
            from bigfish import memory as M
            from bigfish.replier import call_model

            db = V.memory_db_path()
            if not db:
                return json.dumps({"ok": False, "error": "数据目录未确定"},
                                  ensure_ascii=False)
            M.ensure_schema(db)
            # 对最近活跃的那个会话反思；没有会话就明说
            sessions = V.list_sessions(db, 1)
            if not sessions:
                return json.dumps({"ok": False, "error": "还没有任何对话"},
                                  ensure_ascii=False)
            sess = sessions[0]["session"]
            mine = [n for n in (getattr(self.soul, "name", ""),
                                getattr(self.soul, "aliases", "")) if n]
            _own = M.owner_of_private(sess.split(":", 1)[1]) if ":" in sess else ""
            _tier = M.TIER_OWNER if _own and _own == getattr(self, "owner_id", "") else M.TIER_KNOWN
            r = M.reflect_on_session(
                db, "dafeiyu", sess,
                lambda system, user: call_model(self.mcfg, system, user),
                owner=_own, tier=_tier, self_names=mine)
            return json.dumps(r, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    # ── 数据目录（就一个，没有"选哪份"） ──
    @Slot(result=str)
    def get_data_root(self) -> str:
        """当前数据目录的样子。

        ⚠ 这里**没有"范围/份"的概念**。一个数据根就是一份 ——
          里面的 soul.json / mood.json / memory.sqlite3 就是她的全部。
        """
        try:
            from bigfish import vault

            root = vault.resolve_data_root()
            writable, w_err = vault.probe_writable(
                os.path.join(root, vault.SOUL_NAME))

            def _stat(kind: str) -> dict:
                p = vault.existing_path(root, kind)
                exists = bool(p) and os.path.isfile(p)
                return {
                    "path": p,
                    "name": os.path.basename(p) if p else "",
                    "exists": exists,
                    "bytes": os.path.getsize(p) if exists else 0,
                }

            lg = vault.legacy_layout(root)
            return json.dumps({
                "ok": True,
                "root": root,
                "defaultRoot": vault.default_data_root(),
                "customRoot": vault.custom_data_root(),
                "isDefault": vault.normalize_root(root) ==
                             vault.normalize_root(vault.default_data_root()),
                "settingsFile": vault.settings_path(),
                "writable": writable,
                "writeError": w_err,
                "soul": _stat("soul"),
                "mood": _stat("mood"),
                "memory": _stat("memory"),
                "legacy": {
                    "found": bool(lg),
                    "key": (lg or {}).get("key", ""),
                },
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def set_data_root(self, path: str) -> str:
        """改数据目录。传空串 = 恢复默认（安装目录下的 data）。

        ⚠ 只改指向，不搬文件。要连数据一起搬用 apply_move()。
        """
        try:
            from bigfish import vault

            ok, err = vault.set_data_root(path)
            if not ok:
                return json.dumps({"ok": False, "error": err}, ensure_ascii=False)
            new_root = vault.install_root()
            self.log_message.emit(f"[数据] 数据目录 → {new_root}", "")
            return json.dumps({"ok": True, "root": new_root,
                               "isDefault": bool(not vault.custom_data_root())},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(result=str)
    def inspect_wipe(self) -> str:
        """盘点「清空」会删什么：只删心情 + 记忆，灵魂留下。"""
        try:
            from bigfish import vault

            plan = vault.inspect_wipe(keep_soul=True)
            return json.dumps({
                "ok": plan.ok, "error": plan.error, "root": plan.root,
                "items": plan.items, "totalBytes": plan.total_bytes,
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(result=str)
    def apply_wipe(self) -> str:
        """清空心情 + 记忆（灵魂保留）。删之前先备份。"""
        try:
            from bigfish import vault

            ok, err, backup = vault.apply_wipe(keep_soul=True, backup_first=True)
            if ok:
                self.log_message.emit(f"[清空] 心情与记忆已清空，备份在 {backup}", "")
            return json.dumps({"ok": ok, "error": err, "backup": backup},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    # ── 鲸落：她的一切都没了 ──
    @Slot(result=str)
    def whale_fall_status(self) -> str:
        """鲸落预告：会删什么、多大、备份放哪。不改任何东西。"""
        try:
            from bigfish import vault

            d = vault.whale_fall_status()
            return json.dumps(d, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def whale_fall(self, confirm: str) -> str:
        """执行鲸落：删掉灵魂、心情、记忆、参数 —— 一切。

        confirm 必须正好是 "鲸落" 两个字。这不是形式主义：
        这是唯一一个会让她彻底消失的操作，值得多敲两个字。
        """
        try:
            from bigfish import vault

            if (confirm or "").strip() != "鲸落":
                return json.dumps({
                    "ok": False,
                    "error": "需要确认：请输入「鲸落」两个字",
                }, ensure_ascii=False)

            ok, err, backup, deleted = vault.whale_fall()
            if ok:
                self.log_message.emit(
                    f"[鲸落] 已删除 {len(deleted)} 项，备份在 {backup}", "err")
            return json.dumps({
                "ok": ok, "error": err, "backup": backup, "deleted": deleted,
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(result=str)
    def migrate_legacy(self) -> str:
        """把老的 ws-*/ 布局收成新布局。只搬有灵魂的那一份。"""
        try:
            from bigfish import vault

            ok, err, done = vault.migrate_legacy()
            return json.dumps({"ok": ok, "error": err, "done": done},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def import_soul(self, source: str) -> str:
        """把别处的灵魂文件拷过来（比如 DSH 那份）。

        数据目录固定在安装目录后，没有"改指向"这回事了，
        但偶尔确实需要把旧的那份人格带过来 —— 那就直接拷文件。
        """
        try:
            from bigfish import vault

            root = vault.resolve_data_root()
            dst = vault.soul_path(root)
            src = (source or "").strip()

            if not src:
                # 没指定就试 DSH 那边
                dsh = vault.dsh_data_root()
                cand = os.path.join(dsh, vault.SOUL_NAME)
                if os.path.isfile(cand):
                    src = cand
                else:
                    lg = vault.legacy_layout(dsh)
                    src = (lg or {}).get("soul", "")
                if not src:
                    return json.dumps({"ok": False,
                                       "error": "DSH 那边也没找到灵魂文件"},
                                      ensure_ascii=False)

            ok, what = vault.import_soul_from(src, dst)
            if not ok:
                return json.dumps({"ok": False, "error": what}, ensure_ascii=False)
            if what == "exists":
                return json.dumps({"ok": False,
                                   "error": "本地已经有 soul.json 了，不覆盖"},
                                  ensure_ascii=False)
            self.log_message.emit(f"[灵魂] 已从 {src} 导入", "")
            return json.dumps({"ok": True, "from": src, "to": dst},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(result=str)
    def reset_soul_to_default(self) -> str:
        """把本地灵魂换回出厂模板（先备份旧的）。"""
        try:
            from bigfish import vault

            root = vault.resolve_data_root()
            p = vault.soul_path(root)
            backup = ""
            if os.path.isfile(p):
                stamp = time.strftime("%Y%m%d-%H%M%S")
                backup = os.path.join(root, f"soul.bak-{stamp}.json")
                try:
                    import shutil
                    shutil.copyfile(p, backup)
                except Exception:
                    backup = ""
                try:
                    os.remove(p)
                except Exception as exc:
                    return json.dumps({"ok": False, "error": f"删不掉旧文件：{exc}"},
                                      ensure_ascii=False)
            ok, what = vault.write_default_soul(p)
            if not ok:
                return json.dumps({"ok": False, "error": what}, ensure_ascii=False)
            return json.dumps({"ok": True, "backup": backup}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def inspect_move(self, target: str) -> str:
        """搬家前盘点。不改任何东西。"""
        try:
            from bigfish import vault

            plan = vault.inspect_move(target)
            return json.dumps({
                "ok": plan.ok,
                "error": plan.error,
                "src": plan.src,
                "dst": plan.dst,
                "items": plan.items,
                "totalBytes": plan.total_bytes,
                "conflicts": plan.conflicts,
                "dstWritable": plan.dst_writable,
                "dstWriteError": plan.dst_write_error,
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def apply_move(self, target: str) -> str:
        """真搬家。复制成功后才改 root.json，失败不动数据根。"""
        try:
            from bigfish import vault

            ok, err = vault.apply_move(target)
            if ok:
                self.log_message.emit(f"[数据] 已搬到 {target}", "")
            return json.dumps({"ok": ok, "error": err,
                               "root": vault.resolve_data_root()},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    # ── 表情库 ──
    #
    # 2026-11-03 重构：这一页只做**库**，不再管开关和频率。
    #
    #   表情包页  →  她手里有什么（浏览、添加、编辑备注、删除）
    #   插件页    →  她要不要发、多久发一次、怎么挑
    #
    # 分开的理由：前者是素材，后者是行为。混在一起时用户想"关掉表情"
    # 得先找到表情包页，而插件页明明已经有一个开关了 —— 两个开关
    # 控制同一件事，迟早互相打脸。
    #
    # 库层的活（转 GIF、编号、描述）全在 bigfish/stickers.py，
    # 这里只做「给界面用」的适配。

    @Slot(result=str)
    def sticker_list(self) -> str:
        """表情库全量。界面一次拿全，不用点两次。"""
        try:
            from bigfish import plugins as _plugins
            from bigfish import stickers

            _plugins.ensure_loaded()
            items = stickers.list_stickers()
            usage = stickers.read_usage("")
            return json.dumps({
                "ok": True,
                "items": items,
                "dir": stickers.sticker_dir(),
                "last": (usage.get("_last") if isinstance(usage, dict) else None),
                # 插件状态一起给，界面顶部要显示"当前能不能发"
                "enabled": _plugins.is_enabled("sticker"),
                "count": len(items),
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

    @Slot(str, result=str)
    def sticker_add(self, paths_json: str) -> str:
        """把一批文件收进库。

        界面那边可能是「浏览…」选的，也可能是拖进来的，都走这里。
        每张都会：转 GIF → 分配代码 → 落盘。
        """
        try:
            from bigfish import stickers

            paths = json.loads(paths_json or "[]")
            if not isinstance(paths, list):
                return json.dumps({"ok": False, "error": "参数不对"},
                                  ensure_ascii=False)
            added, failed = [], []
            for p in paths:
                ok, res, item = stickers.import_sticker(str(p))
                if ok:
                    added.append(item)
                else:
                    failed.append({"file": os.path.basename(str(p)),
                                   "why": res})
            return json.dumps({"ok": True, "added": added, "failed": failed},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

    @Slot(result=str)
    def sticker_pick_files(self) -> str:
        """原生多选对话框。返回路径列表，不导入 —— 导入由前端调 sticker_add。

        为什么不在这里直接导：用户可能想先看看选了哪些、去掉几张。
        分两步，界面才有机会显示"这 8 张里我只要 6 张"。
        """
        try:
            from PySide6.QtWidgets import QFileDialog

            files, _ = QFileDialog.getOpenFileNames(
                self.window, "选择表情（任意格式，导入时统一转 GIF）",
                "",
                "图片 (*.gif *.png *.jpg *.jpeg *.webp *.bmp *.ico);;"
                "GIF 表情 (*.gif);;所有文件 (*.*)")
            return json.dumps({"ok": True, "paths": files or []},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

    @Slot(str, result=str)
    def sticker_autodesc(self, codes_json: str) -> str:
        """让模型给这几张表情写备注。返回每张的 brief/detail/tags。

        ⚠ 同步阻塞：每张约 1~2 秒。界面那边要显示进度，
          一次别传太多（超过 20 张就该分批）。
        """
        try:
            from bigfish import sticker_desc, stickers

            codes = json.loads(codes_json or "[]")
            if not isinstance(codes, list):
                return json.dumps({"ok": False, "error": "参数不对"},
                                  ensure_ascii=False)
            out = []
            for code in codes:
                item = stickers.get_sticker(str(code))
                if not item:
                    out.append({"code": code, "ok": False,
                                "error": "没有这张表情"})
                    continue
                res = sticker_desc.describe(item["path"])
                if res.get("ok"):
                    # 强度 0 表示模型没给，不覆盖（保留默认 3）
                    inten = int(res.get("intensity") or 0)
                    stickers.update_sticker(
                        str(code),
                        brief=res.get("brief") or "",
                        detail=res.get("detail") or "",
                        tags=res.get("tags") or [],
                        auto=True,
                        intensity=inten if inten else None,
                        mood_dim=res.get("mood_dim") or None)
                    out.append({"code": code, "ok": True,
                                "brief": res.get("brief") or "",
                                "detail": res.get("detail") or "",
                                "intensity": inten,
                                "moodDim": res.get("mood_dim") or ""})
                else:
                    out.append({"code": code, "ok": False,
                                "error": res.get("error") or "模型没给内容"})
            return json.dumps({"ok": True, "results": out},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

    @Slot(str, str, str, int, str, result=str)
    def sticker_update(self, code: str, brief: str, detail: str,
                       intensity: int, mood_dim: str) -> str:
        """改一张表情的概览、详细、强度、情绪维度。"""
        try:
            from bigfish import stickers

            ok, err = stickers.update_sticker(
                code, brief=brief, detail=detail, auto=False,
                intensity=int(intensity) if intensity else None,
                mood_dim=mood_dim)
            return json.dumps({"ok": ok, "error": err}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

    @Slot(str, result=str)
    def sticker_delete(self, code: str) -> str:
        try:
            from bigfish import stickers

            ok, note = stickers.delete_sticker(code)
            return json.dumps({"ok": ok, "note": note, "error": ""},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

    @Slot(result=str)
    def sticker_open_dir(self) -> str:
        try:
            from bigfish import stickers

            d = stickers.ensure_dir()
            if sys.platform == "win32":
                os.startfile(d)  # noqa: S606
            return json.dumps({"ok": True, "path": d}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

    @Slot(str, result=str)
    def sticker_test_send(self, code: str) -> str:
        """把一张表情发到「文件传输助手」，验证通道通不通。

        ⚠ 会真的写微信、真的抢焦点。这是唯一能让用户自己验证
          「这台机器上她到底能不能发表情」的办法，所以保留。
        """
        try:
            from bigfish import stickers
            from bigfish.wechat_sender import SendError, UiaSender

            item = stickers.get_sticker(code)
            if not item:
                return json.dumps({"ok": False, "error": f"找不到：{code}"},
                                  ensure_ascii=False)
            s = UiaSender()
            s.wake()
            r = s.send_media("文件传输助手", item["path"])
            return json.dumps({"ok": bool(r.ok),
                               "error": "" if r.ok else r.detail},
                              ensure_ascii=False)
        except SendError as exc:
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

    @Slot(str, result=str)
    def pick_directory(self, start: str) -> str:
        """原生目录选择框。选不了就返回空串，界面用输入框兜底。"""
        try:
            from PySide6.QtWidgets import QFileDialog

            d = QFileDialog.getExistingDirectory(
                self.window, "选择目录", start or os.path.expanduser("~"))
            return json.dumps({"path": d or ""}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"path": "", "error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def pick_file(self, start: str) -> str:
        """原生文件选择框（记忆备份导入用）。"""
        try:
            from PySide6.QtWidgets import QFileDialog

            f, _ = QFileDialog.getOpenFileName(
                self.window, "选择记忆库备份", start or "",
                "SQLite 数据库 (*.sqlite3 *.db *.sqlite3.bak-*);;所有文件 (*.*)")
            return json.dumps({"path": f or ""}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"path": "", "error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def open_path(self, path: str) -> str:
        """在资源管理器里打开一个目录或文件。"""
        try:
            p = (path or "").strip()
            if not p or not os.path.exists(p):
                return json.dumps({"ok": False, "error": f"路径不存在：{p}"},
                                  ensure_ascii=False)
            if sys.platform == "win32":
                if os.path.isdir(p):
                    os.startfile(p)  # noqa: S606
                else:
                    os.startfile(os.path.dirname(p))  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", p if os.path.isdir(p)
                                  else os.path.dirname(p)])
            return json.dumps({"ok": True}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    def _tmp_copy(self, src: str) -> str:
        """复制记忆库到本地临时文件，避免和 DSH 抢锁。"""
        import shutil
        tmpdir = os.path.join(_ROOT, ".tmp")
        os.makedirs(tmpdir, exist_ok=True)
        dst = os.path.join(tmpdir, "mem_ui.sqlite3")
        shutil.copyfile(src, dst)
        for suf in ("-wal", "-shm"):
            if os.path.exists(src + suf):
                try:
                    shutil.copyfile(src + suf, dst + suf)
                except Exception:
                    pass
        return dst

    @Slot(str, int, result=str)
    def search_memory(self, query: str, limit: int) -> str:
        """调 DSH 原版 MemoryStore 检索（走 bridge.mjs），保证分词逻辑一致。"""
        try:
            import json as _json
            import subprocess

            from bigfish.bridge_client import _default_data_root, find_node

            node = find_node()
            if not node:
                return _json.dumps({"error": "找不到 node"}, ensure_ascii=False)
            bridge = os.path.join(_ROOT, "bridge", "bridge.mjs")
            root = _default_data_root()
            if not root or not os.path.isfile(bridge):
                return _json.dumps({"error": "桥接不可用"},
                                   ensure_ascii=False)

            cmd = [node, bridge, "search", "--root", root,
                   "--maxChars", str(max(600, limit * 240))]
            if query:
                cmd += ["--query", query]

            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=25)
            raw = (r.stdout or "").strip()
            if not raw:
                return _json.dumps({"error": "桥接无输出"},
                                   ensure_ascii=False)
            d = _json.loads(raw)
            mem = d.get("memory") or {}
            text = mem.get("text") or ""

            items = []
            if text:
                try:
                    arr = _json.loads(text)
                    for it in arr[:limit]:
                        ts = it.get("time") or 0
                        items.append({
                            "role": it.get("role") or "user",
                            "text": (it.get("text") or "")[:600],
                            "time": ts_to_local(ts),
                        })
                except Exception:
                    pass

            stats = {}
            try:
                stats = _json.loads(self.memory_stats())
            except Exception:
                pass

            return _json.dumps({
                "items": items,
                "total": stats.get("total"),
                "sessions": stats.get("sessions"),
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    @Slot()
    def open_wechat(self) -> None:
        """尝试把微信窗口带到前台。"""
        try:
            import ctypes
            u = ctypes.windll.user32
            hwnd = u.FindWindowW("Qt51514QWindowIcon", None)
            if not hwnd:
                hwnd = u.FindWindowW(None, "微信")
            if hwnd:
                u.ShowWindow(hwnd, 9)      # SW_RESTORE
                u.SetForegroundWindow(hwnd)
                self.log_message.emit("已把微信窗口带到前台", "in")
            else:
                self.log_message.emit("找不到微信窗口", "err")
        except Exception as exc:
            self.log_message.emit(f"打开微信失败: {exc}", "err")

    # ── 自动更新 ──
    @Slot(result=str)
    def get_version(self) -> str:
        try:
            from bigfish.updater import __version__
        except Exception:
            __version__ = "0.0.0"
        return json.dumps({"version": __version__}, ensure_ascii=False)

    @Slot(result=str)
    def check_update(self) -> str:
        try:
            from bigfish.updater import Updater, read_update_config

            uc = read_update_config(CONFIG_PATH)
            repo = uc.get("repo") or ""
            if not repo:
                return json.dumps({"has_update": False,
                                   "error": "未配置更新仓库（config.json 的 update.repo）"},
                                  ensure_ascii=False)
            cache = os.path.join(_ROOT, ".tmp", "update")
            up = Updater(repo, uc.get("current") or "0.1.0", cache,
                         enabled=bool(uc.get("enabled", True)))
            info = up.check()
            self._update_info = info
            return json.dumps(info, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"has_update": False, "error": str(exc)},
                              ensure_ascii=False)

    @Slot(result=str)
    def apply_update(self) -> str:
        """下载新版并安排替换。调用后建议用户手动关闭程序。"""
        try:
            from bigfish.updater import Updater, read_update_config

            info = getattr(self, "_update_info", None)
            if not info or not info.get("has_update"):
                return json.dumps({"ok": False, "error": "没有可用的更新"},
                                  ensure_ascii=False)

            uc = read_update_config(CONFIG_PATH)
            cache = os.path.join(_ROOT, ".tmp", "update")
            up = Updater(uc.get("repo") or "", "0", cache)

            target = sys.executable
            # 打包后 sys.executable 是 exe；开发态是 python.exe，不能覆盖
            if not getattr(sys, "frozen", False):
                return json.dumps({
                    "ok": False,
                    "error": "开发模式不支持自动替换（会覆盖 python.exe）。"
                             "打包成 exe 后才能用。"
                }, ensure_ascii=False)

            def prog(done, total):
                pct = int(done * 100 / total) if total else 0
                self.log_message.emit(f"[更新] 已下载 {pct}%", "in")

            script = up.download_and_schedule(info, target, progress=prog)
            self.log_message.emit(f"[更新] 已安排替换，脚本: {script}", "in")
            return json.dumps({"ok": True, "script": script}, ensure_ascii=False)
        except Exception as exc:
            self.log_message.emit(f"[更新] 失败: {exc}", "err")
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

    @Slot(result=str)
    def get_config(self) -> str:
        c = load_json_config()
        ar = c.get("auto_reply") or {}
        return json.dumps({
            "endpoint": c.get("endpoint", ""),
            "model": c.get("model", ""),
            "api_key": c.get("api_key", ""),
            "temperature": c.get("temperature", 1.0),
            "max_tokens": c.get("max_tokens", 2600),
            "interval": ar.get("interval", 1.2),
            "max_reply_chars": ar.get("max_reply_chars", 300),
            "quiet_hours": ar.get("quiet_hours", []),
            "allow_self": ar.get("allow_self", False),
            "group_reply": ar.get("group_reply", True),
            "group_allow_all": ar.get("group_allow_all", []),
        }, ensure_ascii=False)

    @Slot(str, result=str)
    def save_config(self, payload: str) -> str:
        try:
            d = json.loads(payload)
            c = load_json_config()
            c["endpoint"] = d.get("endpoint", c.get("endpoint"))
            c["model"] = d.get("model", c.get("model"))
            c["api_key"] = d.get("api_key", "")
            c["temperature"] = float(d.get("temperature", 1.0))
            c["max_tokens"] = int(d.get("max_tokens", 2600))

            ar = c.setdefault("auto_reply", {})
            ar["interval"] = float(d.get("interval", 1.2))
            ar["max_reply_chars"] = int(d.get("max_reply_chars", 300))
            ar["quiet_hours"] = list(d.get("quiet_hours") or [])
            ar["allow_self"] = bool(d.get("allow_self", False))
            ar["group_reply"] = bool(d.get("group_reply", True))
            ar["group_allow_all"] = [str(x) for x in
                                     (d.get("group_allow_all") or [])]
            ar["chats"] = self.watching or ar.get("chats", [])

            save_json_config(c)
            return json.dumps({"ok": True})
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    # ── 启停 ──
    @Slot(result=str)
    def start_engine(self) -> str:
        if self.worker and self.worker.isRunning():
            return json.dumps({"ok": True, "note": "已在运行"})

        if not self.watching:
            return json.dumps({"error": "没有选择要监听的会话"},
                              ensure_ascii=False)

        self.last_error = ""
        self.recv_count = 0
        self.reply_count = 0
        self.started_at = time.time()

        # 非管理员时不能发（UIA 热激活要写 gate 字节）。
        # 与其让引擎整个死掉，不如降级成「只看不发」，并在界面说明。
        dry = not is_admin()
        if dry:
            self.log_message.emit(
                "[提示] 当前不是管理员权限，引擎以「只看不发」模式运行。"
                "以管理员身份启动才能真正回复。", "err")

        acc = ""
        if self._reader is not None:
            acc = getattr(self._reader._db, "account", "") or ""
        if not acc:
            acc = str((load_json_config().get("auto_reply") or {})
                      .get("account") or "")
        w = EngineWorker(self.watching, dry=dry, account=acc)
        w.log.connect(self._on_log)
        w.chat_event.connect(self._on_chat)
        w.stats.connect(self._on_stats)
        w.failed.connect(self._on_fail)
        w.start()
        self.worker = w
        return json.dumps({"ok": True, "dry": dry})

    @Slot()
    def stop_engine(self) -> None:
        if self.worker:
            self.worker.stop()
            self.worker.wait(6000)
            self.worker = None
        self.started_at = 0.0

    def _on_log(self, text: str, kind: str) -> None:
        self._log_seq += 1
        self._log_buffer.append((self._log_seq, text, kind))
        if len(self._log_buffer) > 400:
            del self._log_buffer[:-400]
        self.log_message.emit(text, kind)

    def _on_chat(self, who: str, text: str, direction: str) -> None:
        recent = getattr(self, "_recent", [])
        recent.insert(0, {"who": who, "text": text, "dir": direction})
        self._recent = recent[:12]
        self.chat_event.emit(who, text, direction)

    def _on_stats(self, recv: int, reply: int) -> None:
        self.recv_count = recv
        self.reply_count = reply

    def _on_fail(self, msg: str) -> None:
        self.last_error = msg


# ══════════════════════════════════════════════════════════════════
#  主窗口
# ══════════════════════════════════════════════════════════════════
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("大肥鱼")
        self.resize(1180, 820)
        self.setMinimumSize(960, 660)

        icon_path = os.path.join(UI_DIR, "icon.png")
        if os.path.isfile(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.view = QWebEngineView(self)
        self.setCentralWidget(self.view)

        # 拖放：QWebEngineView 默认 acceptDrops=False，系统层面就拒了，
        # 页面里的 dragover 根本收不到（鼠标显示禁止图标）。
        # 而且就算收到，HTML5 的 File 对象在 QtWebEngine 里也没有 path
        # 属性 —— 拿不到路径就没法转 GIF。
        # 所以改成 Python 侧拦 Drop，把路径数组丢给页面。
        self.drop_catcher = None
        try:
            from bigfish.dropcatch import DropCatcher

            self.drop_catcher = DropCatcher(self.view, self)
            self.drop_catcher.install()
            # 页面加载前 view 的渲染子控件还没建出来，加载完再设一次
            # "接受文件"（DragAcceptFiles 要对最终那层窗口调）。
            self.view.loadFinished.connect(
                lambda _ok: self.drop_catcher.install())
        except Exception as exc:
            print(f"[drop] 装载失败：{exc}", file=sys.stderr, flush=True)

        self.channel = QWebChannel(self.view.page())
        self.backend = Backend(self)
        self.channel.registerObject("backend", self.backend)
        self.view.page().setWebChannel(self.channel)

        self.view.load(QUrl.fromLocalFile(os.path.join(UI_DIR, "index.html")))

    def closeEvent(self, event):
        try:
            self.backend.stop_engine()
        except Exception:
            pass
        super().closeEvent(event)


def run_live_send() -> int:
    """真的发一条消息到文件传输助手，然后回读数据库确认。

    这是唯一会真的发东西的入口，用 --live-send 显式触发。
    发出去的内容带时间戳，方便事后识别是哪一次测试。
    """
    if getattr(sys, "frozen", False):
        try:
            import ctypes
            if ctypes.windll.kernel32.AttachConsole(-1):
                sys.stdout = open("CONOUT$", "w", encoding="utf-8",
                                  errors="replace", buffering=1)
        except Exception:
            pass

    HR = "=" * 66
    print(HR)
    print("  真实发送测试")
    print(HR)
    print(f"  管理员: {is_admin()}")
    if not is_admin():
        print("  [!] 需要管理员权限")
        return 1

    # 数据库
    print("\n[1] 打开数据库")
    from bigfish.wechat import WeChatReader
    acc = str((load_json_config().get("auto_reply") or {})
              .get("account") or "")
    if not acc:
        print("    [!] 没有选号（config.json 的 auto_reply.account 为空）")
        return 1
    r = WeChatReader(account=acc).open()
    actual = getattr(r._db, "account", "") or ""
    if actual and actual != acc:
        print(f"    [!] 账号不匹配：要 {acc}，实际打开 {actual}")
        return 1
    info = r.self_info()
    print(f"    账号: {info.get('nick_name')}")

    # UIA
    print("\n[2] 热激活 UIA")
    t0 = time.time()
    from bigfish.wechat_sender import SendError, UiaSender
    try:
        sender = UiaSender()
        sender.wake()
    except SendError as exc:
        print(f"    [!] {exc.kind}: {exc}")
        return 1
    print(f"    成功 {time.time()-t0:.1f}s，当前 {sender.current_chat()!r}")

    # 发送
    stamp = time.strftime("%H:%M:%S")
    text = f"[自检 {stamp}] 大肥鱼发的，收到说明收发都通。"
    print(f"\n[3] 发送到 文件传输助手")
    print(f"    {text}")
    try:
        res = sender.send("文件传输助手", text)
    except SendError as exc:
        print(f"    [!] {exc.kind}: {exc}")
        return 1
    print(f"    返回 ok={res.ok}")

    # 回读
    print(f"\n[4] 回读数据库确认")
    found = False
    for i in range(10):
        time.sleep(1.0)
        try:
            r2 = WeChatReader(account=acc).open()
            for m in r2.messages("filehelper", limit=3):
                if stamp in m.content:
                    found = True
                    print(f"    ✓ 第 {i+1} 次查询命中")
                    print(f"      local_id={m.local_id}")
                    print(f"      {m.content[:60]}")
                    break
        except Exception:
            continue
        if found:
            break

    print()
    print(HR)
    if found:
        print("  ✓ 收发链路确认打通（消息已落库）")
    else:
        print("  ✗ 数据库里没找到这条消息，请手动看微信")
    print(HR)
    return 0 if found else 1


def main() -> int:
    # ── 命令行自检入口 ──
    # 打包成 exe 之后，外部脚本（tools/selftest.py）跑不到 exe 里的代码，
    # 所以自检必须内建：BigFish.exe --selftest
    if "--selftest" in sys.argv:
        return run_selftest()

    # BigFish.exe --version
    if "--version" in sys.argv:
        try:
            from bigfish.updater import __version__
        except Exception:
            __version__ = "?"
        print(__version__)
        return 0

    # BigFish.exe --live-send   真的发一条到文件传输助手
    if "--live-send" in sys.argv:
        return run_live_send()

    QApplication.setAttribute(
        getattr(Qt, "AA_ShareOpenGLContexts", Qt.ApplicationAttribute.AA_ShareOpenGLContexts),
        True)

    # Windows 任务栏分组：不设这个，任务栏会把我们当成通用 Python 程序，
    # 图标可能退回成 python.exe 那个。设了之后任务栏用的是 exe 里嵌的图标。
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "bigfish.wechat.1")
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setApplicationName("大肥鱼")
    app.setOrganizationName("bigfish")
    # 再设一次应用级图标：窗口图标在 MainWindow 里设，
    # 但对话框、任务栏预览等由应用默认图标兜底。
    _ico = os.path.join(UI_DIR, "icon.ico")
    if os.path.isfile(_ico):
        app.setWindowIcon(QIcon(_ico))
    elif os.path.isfile(os.path.join(UI_DIR, "icon.png")):
        app.setWindowIcon(QIcon(os.path.join(UI_DIR, "icon.png")))

    # 启动时确保数据目录和灵魂存在 —— 用户可能一次都没配过。
    # 失败不拦启动：界面上「数据目录」页会把原因显示出来。
    try:
        from bigfish import vault
        boot = vault.ensure_data_root()
        if boot.get("soul") == "imported":
            print(f"[boot] 已从 DSH 导入人格：{boot.get('imported')}")
        elif boot.get("soul") == "created":
            print(f"[boot] 已用内置模板创建人格：{boot.get('root')}")
        if boot.get("error"):
            print(f"[boot] {boot['error']}")
    except Exception as exc:
        print(f"[boot] 初始化数据目录失败：{exc}")

    win = MainWindow()
    win.show()
    return app.exec()


def _open_console() -> bool:
    """把 stdout/stderr 接到父控制台。成功返回 True。"""
    if not getattr(sys, "frozen", False):
        return False
    try:
        import ctypes

        if ctypes.windll.kernel32.AttachConsole(-1):   # ATTACH_PARENT_PROCESS
            sys.stdout = open("CONOUT$", "w", encoding="utf-8",
                              errors="replace", buffering=1)
            sys.stderr = open("CONOUT$", "w", encoding="utf-8",
                              errors="replace", buffering=1)
            return True
    except Exception:
        pass
    return False


def run_selftest() -> int:
    """在 exe 内部跑自检。

    检查项和 tools/selftest.py 一致，但跑的是**打包后的代码** ——
    这才是用户实际会遇到的那份。

    ⚠ 为什么先收进内存再统一输出：
    exe 是 console=False 的 GUI 程序，`sys.stdout` 默认是 None，print 被
    静默丢弃。而 AttachConsole 成功或失败会**中途**改变 stdout ——
    实测过一次：灵魂/插件/心情/记忆/模型 五项全部凭空消失，输出直接从
    「微信数据库」开始，统计却写着"通过 12"。看着像自检漏了项，其实只是
    前五行的 print 掉进了 None。
    现在整份输出先写进 StringIO，最后一次性交给能用的那个通道。
    """
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _run_selftest_inner()
    text = buf.getvalue()

    if _open_console():
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except Exception:
            pass
        return rc

    # 没有控制台：fd 1/2 可能已被父进程重定向到文件（`> log.txt`）
    for fd in (1, 2):
        try:
            os.write(fd, text.encode("utf-8", "replace"))
            return rc
        except Exception:
            continue
    return rc


def _run_selftest_inner() -> int:
    """自检主体。输出全走 print，由 run_selftest 统一收口。"""
    import traceback

    HR = "=" * 66
    results = []

    def item(name, fn):
        t0 = time.time()
        try:
            detail = fn()
            dt = time.time() - t0
            results.append((name, "OK", detail, dt))
            print(f"  [OK  ] {name}  ({dt:.2f}s)")
            if detail:
                for line in str(detail).splitlines():
                    print(f"         {line}")
            return True
        except Exception as exc:
            dt = time.time() - t0
            results.append((name, "FAIL", f"{type(exc).__name__}: {exc}", dt))
            print(f"  [FAIL] {name}  ({dt:.2f}s)")
            print(f"         {type(exc).__name__}: {exc}")
            return False

    print(HR)
    print("  大肥鱼 自检（打包版）")
    print(HR)
    print(f"  版本    : ", end="")
    try:
        from bigfish.updater import __version__
        print(__version__)
    except Exception:
        print("?")
    print(f"  打包    : {getattr(sys, 'frozen', False)}")
    print(f"  管理员  : {is_admin()}")
    print(f"  安装目录: {_APP_DIR}")
    print(f"  界面目录: {UI_DIR}")
    print(f"  桥接目录: {BRIDGE_DIR}")

    # 1. 灵魂
    def t_soul():
        from bigfish import vault
        from bigfish.soul import load_soul

        # ⚠ 自检可能在任何时候跑（包括刚解压、从没启动过）。
        #   数据目录现在固定在安装目录，第一次运行时它还是空的 ——
        #   直接 load_soul() 会抛 FileNotFoundError，看起来像坏了，
        #   其实只是"还没初始化"。先确保初始化，再验。
        boot = vault.ensure_data_root()
        if boot.get("error"):
            raise RuntimeError(boot["error"])

        s = load_soul()
        p = s.render_persona()
        assert s.name and p
        origin = {"created": "出厂模板", "imported": "从 DSH 导入",
                  "exists": "已存在"}.get(boot.get("soul", ""), boot.get("soul", ""))
        return f"{s.name} rev{s.revision}，人格 {len(p)} 字符（{origin}）"
    print()
    print("工厂自检")
    print("-" * 66)
    item("灵魂", t_soul)

    # 1.5 插件（她由哪些部分拼起来）
    def t_plugins():
        from bigfish import plugins as P

        # ⚠ 这条专门用来抓「打包漏了 bigfish/plugins」。
        #   插件只在 build_prompt 里 import —— 漏了的话程序照常启动、
        #   界面照常打开，只有真正聊天时才炸。那种问题没人能自查出来。
        inv = P.inventory()
        items = inv["plugins"]
        assert items, "一个插件都没登记 —— 打包时漏了 bigfish/plugins"

        prompt_on = [p for p in items if p["scope"] == "prompt" and p["enabled"]]
        secs = P.prompt_sections({})
        assert len(secs) == len(prompt_on), (
            f"注入 {len(secs)} 段，但已启用的注入类插件有 {len(prompt_on)} 个")

        orders = [s[0] for s in secs]
        assert orders == sorted(orders), f"注入顺序没按身份序：{orders}"

        on = [p["title"] for p in items if p["enabled"]]
        return (f"{len(items)} 个插件，已启用 {len(on)} 个"
                f"（{'、'.join(on) or '无'}）；本轮注入 {len(secs)} 段"
                f"（{', '.join(str(o) for o in orders) or '—'}）")
    item("插件", t_plugins)

    # 说法：分条 + 节奏（打包版里也要能跑）
    def t_say():
        from bigfish import plugins as _P
        from bigfish.say import PacePolicy, SplitPolicy, plan_pacing, split_reply

        _P.ensure_loaded()
        cfg = _P.config_of("say") if _P.is_enabled("say") else {}
        policy = SplitPolicy(
            max_parts=int(cfg.get("max_parts", 4)),
            part_max_chars=int(cfg.get("part_max_chars", 50)),
            protect_quotes=bool(cfg.get("quote_protect", True)))

        text = "知道了，明天就去长坂坡游玩，先去看樱花，再去看枣树"
        parts = split_reply(text, policy)
        assert len(parts) >= 2, f"没分条：{parts}"
        assert all(len(p) <= policy.part_max_chars for p in parts), f"有超长条：{parts}"

        gaps = plan_pacing(parts, PacePolicy(
            seed="selftest", per_char=float(cfg.get("per_char", 0.09))))
        total = sum(gaps)
        assert 0.5 <= total <= 12.01, f"总时长异常：{total}"

        # 引号保护：别人引用的原话不该被逗号切开
        quoted = split_reply("他说「明天去长坂坡，后天回来」我答应了", policy)
        assert len(quoted) == 1, f"引号被切开了：{quoted}"

        return f"{len(parts)} 条 / {total:.1f}s｜" + "｜".join(parts)
    item("说法", t_say)

    # 2. 心情
    def t_mood():
        from bigfish import vault as V
        from bigfish.bridge_client import load_mood_only

        # ⚠ 心情可能是空的（刚清空过，或者从没聊过）。空的也是正常状态 ——
        #   自检要验的是"读路径通不通"，不是"必须有数据"。
        #   以前这里 assert m and m.value is not None，清空后就永远红。
        cfg = V.read_mood_config()
        assert isinstance(cfg.get("moodBase"), int), "心情配置读不出来"
        assert cfg.get("injectMode") in V.INJECT_MODES, "injectMode 非法"

        p = V.existing_path(V.resolve_data_root(), "mood")
        if not p or not os.path.isfile(p):
            return f"还没有心情数据（正常），基线 {cfg['moodBase']}"

        m = load_mood_only()
        if not m or m.value is None:
            return f"心情文件在但还没值（正常），基线 {cfg['moodBase']}"
        return f"{m.label} {m.value:.1f}"
    item("心情", t_mood)

    # 3. 记忆
    def t_mem():
        import shutil
        import sqlite3

        from bigfish import vault as V
        root = V.resolve_data_root()
        assert root, "找不到数据根"
        db = V.existing_path(root, "memory")

        # ⚠ 记忆库可能是空的/不存在的（刚清空过）。那是正常状态。
        #   自检验的是"路径能解析、库能打开"，不是"必须有一万条记忆"。
        if not db or not os.path.isfile(db):
            return "还没有记忆库（正常）"

        tmp = os.path.abspath(os.path.join(_APP_DIR, ".tmp",
                                           "exe_selftest.sqlite3"))
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        shutil.copyfile(db, tmp)
        conn = sqlite3.connect(tmp)
        try:
            n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            conn.close()
        return f"库里 {n} 条"
    item("记忆", t_mem)

    # 4. 模型
    def t_model():
        from bigfish.replier import ModelConfig, call_model
        c = ModelConfig.load()
        assert c.ready, f"模型未就绪 {c.endpoint}"
        r = call_model(c, "你叫大肥鱼。", "回两个字")
        assert r
        return f"{c.model} -> {r[:20]!r}"
    item("模型", t_model)

    # 5. 数据库
    disk_db = {}

    def t_db():
        from bigfish.wechat import WeChatReader
        r = WeChatReader().open()
        disk_db["r"] = r
        info = r.self_info()
        return (f"{info.get('nick_name')}，"
                f"密钥 {len(r._db._keys)}/{len(r._db._db_files)}")
    item("微信数据库", t_db)

    # 6. 会话
    def t_chats():
        r = disk_db.get("r")
        assert r, "数据库未就绪"
        rows = r.sessions(limit=5)
        assert rows
        return f"{len(rows)} 个会话，首个「{rows[0]['name']}」"
    item("会话读取", t_chats)

    # 7. UIA
    def t_uia():
        if not is_admin():
            raise RuntimeError("非管理员，UIA 需要提权")
        from bigfish.wechat_sender import UiaSender
        s = UiaSender()
        s.wake()
        cur = s.current_chat()
        if not cur:
            s.open_chat("文件传输助手")
            time.sleep(0.5)
            cur = s.current_chat()
        assert cur, "读不到当前会话"
        return f"热激活成功，当前 {cur!r}"
    item("UIA 热激活", t_uia)

    # 8. 可写层 —— 打包后这条最容易漏
    #    外部脚本能 import vault.py 不代表 exe 里也有。
    #    这里在**临时目录**里跑一遍完整写路径，不碰用户的真实数据。
    def t_vault():
        import shutil as _sh
        import tempfile as _tf

        from bigfish import vault as V

        tmpd = _tf.mkdtemp(prefix="bf-exe-vault-")
        try:
            p = os.path.join(tmpd, "ws-t", "soul-ws-t.json")
            soul = V.normalize_soul({
                "identity": {"name": "自检", "address": "爸", "whoIAm": "测试"},
                "values": [{"text": "真诚", "locked": True}],
                "traits": {"warmth": 40, "directness": 88},
            })
            ok, out, err = V.write_soul_file(p, soul)
            assert ok, err or "写入失败"
            back, exists, rerr = V.read_soul_file(p)
            assert exists and not rerr, rerr
            assert back["identity"]["name"] == "自检"
            assert back["revision"] == 2, f"revision={back['revision']}"

            # 渲染两侧共用同一份文案表
            from bigfish.soul import Soul
            persona = Soul.from_dict(back).render_persona()
            assert "温暖：克制，不主动靠近。" in persona, persona
            assert "/100" not in persona, "性格段不该有数字"

            # 分档边界：60 必须是高档
            assert V.trait_sentence("warmth", 60).startswith("温暖：明显关心")
            assert V.trait_sentence("warmth", 40).startswith("温暖：克制")

            # 数据根 / 范围键
            # 存储布局：一个数据根 = 一份
            assert V.layout_path(tmpd, "soul") == os.path.join(tmpd, "soul.json")
            assert V.layout_path(tmpd, "mood") == os.path.join(tmpd, "mood.json")
            assert V.layout_path(tmpd, "memory") == os.path.join(tmpd, "memory.sqlite3")

            # 心情配置：未知字段必须保留
            assert V.INJECT_MODES == ["off", "label", "event"]
            return f"读写/渲染/分档 全部正常（rev {back['revision']}）"
        finally:
            _sh.rmtree(tmpd, ignore_errors=True)
    item("可写层", t_vault)

    # 界面资源路径 —— 打包后最容易错的一项，而且错了只表现成
    # 「无法访问您的文件 / ERR_FILE_NOT_FOUND」，看不出是路径问题。
    def t_ui():
        idx = os.path.join(UI_DIR, "index.html")
        if not os.path.isfile(idx):
            raise FileNotFoundError(f"index.html 不在 UI_DIR 里：{idx}")
        size = os.path.getsize(idx)
        if size < 1000:
            raise ValueError(f"index.html 只有 {size} 字节，八成是空文件")
        parts = [f"UI_DIR={UI_DIR}（{size} 字节）"]
        if not os.path.isfile(os.path.join(BRIDGE_DIR, "bridge.mjs")):
            parts.append(f"⚠ bridge.mjs 不在 {BRIDGE_DIR}")
        return "；".join(parts)

    item("界面资源", t_ui)

    # 10. 记忆写入链路 —— 打包后这条最容易"缺一块"
    #     读路径一直在，写路径是后补的；exe 里漏了 import 就会静默不写。
    def t_memory_write():
        import shutil as _sh
        import tempfile as _tf

        from bigfish import vault as V

        tmpd = _tf.mkdtemp(prefix="bf-mem-")
        db = os.path.join(tmpd, V.MEMORY_NAME)
        try:
            ok, mid, err = V.capture_message(
                "wechat:selftest", "e1", "user", "打包版记忆写入自检", db_path=db)
            assert ok, err or "写入失败"
            assert mid, "没拿到 message id"

            # 幂等
            ok2, mid2, _ = V.capture_message(
                "wechat:selftest", "e1", "user", "打包版记忆写入自检", db_path=db)
            assert mid2 is None, "同 key 应该幂等"

            st = V.memory_stats(db)
            assert st["total"] == 1, f"total={st['total']}"
            assert st["chunks"] >= 1, "没建 chunk"

            # 分词必须和插件一致
            t = V.memory_terms("老爸今天加班")
            assert "老爸" in t and "加班" in t, str(t)
            assert "的" not in t, "停用字没去掉"

            # 检索能命中
            page = V.browse_messages(db, 0, 5, query="自检")
            assert page["items"], "按内容搜不到"

            return f"写入/幂等/分词/检索 正常（{st['total']} 条）"
        finally:
            _sh.rmtree(tmpd, ignore_errors=True)
    item("记忆写入", t_memory_write)

    # 10b. 事实层链路 —— 这一条**只有打包版才会暴露问题**
    #      bigfish.memory 是新增模块，所有调用点都是函数内局部 import：
    #      源码跑得好好的，PyInstaller 漏收就变成"聊天正常、一记事实就 ImportError"。
    #      所以这里必须真跑一遍：建库 → 纠正 → 遗忘 → 检索 → 级联。
    def t_fact_layer():
        import shutil as _sh
        import tempfile as _tf

        from bigfish import memory as M, vault as V

        tmpd = _tf.mkdtemp(prefix="bf-fact-")
        db = os.path.join(tmpd, V.MEMORY_NAME)
        try:
            ok, err = M.ensure_schema(db)
            assert ok, err or "建库失败"

            TF = M.owner_of_private("selftest")    # 自检用的 owner
            # v5 起 remember 默认校验来源：先造一条真原文，拿它的 chunk 当证据。
            # 以前这里是"先写事实、后写原文"，来源列里塞的是空数组 ——
            # 那正是"没有来源也算记忆"，已经被校验挡住了。
            V.capture_message("wechat:selftest", "src1", "user",
                              "老爸说他不喝咖啡，改喝豆浆了", db_path=db)
            import sqlite3 as _sq

            _c = _sq.connect(db)
            try:
                SRC = int(_c.execute(
                    "SELECT c.id FROM chunks c JOIN messages m ON m.id=c.message "
                    "WHERE m.session='wechat:selftest' ORDER BY c.id DESC LIMIT 1"
                ).fetchone()[0])
            finally:
                _c.close()

            ok, fid, err = M.remember(db, "dafeiyu", M.Fact(
                predicate="饮食偏好", object="不喝咖啡", anchor="老爸", confidence=0.7,
                owner=TF, source_chunks=[SRC]))
            assert ok and fid, err or "事实写入失败"

            # v5：没有真来源必须被拒 —— 这是"任何记忆都要有来源"的执行点
            bad_ok, _bad, bad_err = M.remember(db, "dafeiyu", M.Fact(
                predicate="住址", object="编出来的地址", owner=TF))
            assert not bad_ok, "无来源的事实居然写进去了"
            assert "来源" in (bad_err or ""), f"拒绝理由不清楚：{bad_err}"

            # 纠正：旧的标 superseded、新的 active、两行都在
            ok, fid2, err = M.remember(db, "dafeiyu", M.Fact(
                predicate="饮食偏好", object="改喝豆浆", anchor="老爸", confidence=0.85,
                owner=TF, source_chunks=[SRC]))
            assert ok and fid2 != fid, "纠正没产生新行"
            items = {f["id"]: f for f in M.list_facts(db, "dafeiyu")["items"]}
            assert len(items) == 2, f"应为 2 行，实际 {len(items)}"
            assert items[fid]["status"] == "superseded", items[fid]["status"]
            assert items[fid]["supersededBy"] == fid2, "旧事实没指向新事实"

            # 遗忘：不删行
            ok, err = M.forget_fact(db, "dafeiyu", fid2, "自检")
            assert ok, err or "遗忘失败"
            got = {f["id"]: f for f in M.list_facts(db, "dafeiyu")["items"]}
            assert got[fid2]["status"] == "forgotten", "遗忘没生效"
            assert len(got) == 2, "遗忘不应该删行"

            # 原文 + 检索（中文整句必须命得中）
            V.capture_message("wechat:selftest", "f1", "user",
                              "老爸今天加班到十点，很累", db_path=db)
            hits = M.search_chunks(db, "dafeiyu", "老爸加班")
            assert hits, "中文检索没命中"
            assert "加班" in hits[0]["text"], "最相关没排第一"

            # 级联：删原文不留幽灵
            # ⚠ 这一步放在 CLI 检索**之后** —— 否则库被删空了，
            #   后面的 CLI 检索自然"通道不通"，那是测试顺序的错，不是代码的错。
            ok, n, err = V.delete_messages(db, [1])
            assert ok and n == 1, err or "级联删除失败"
            h = M.health(db, "dafeiyu")
            assert h["ok"], h.get("message")
            assert h["missingIndex"] == 0, f"索引缺口 {h['missingIndex']}"
            assert h["orphanChunks"] == 0, f"孤儿片段 {h['orphanChunks']}"

            # CLI 通道（bridge.mjs 靠它）—— 删完还剩事实与别的原文，
            # 但这里要的是"检索通路能跑通"，所以再补一句可检索的原文。
            V.capture_message("wechat:selftest", "f2", "user",
                              "那爸爸记得早点休息", db_path=db)
            import io as _io
            import json as _json
            from contextlib import redirect_stdout
            buf = _io.StringIO()
            with redirect_stdout(buf):
                M._cli(["--recall", db, "--query", "爸爸", "--maxChars", "600"])
            out = _json.loads(buf.getvalue().strip() or "{}")
            assert out.get("ok"), f"CLI 检索通道不通：{buf.getvalue()[:120]}"

            return f"事实/纠正/遗忘/检索/级联/CLI 正常（schema v{h['schemaVersion']}）"
        finally:
            _sh.rmtree(tmpd, ignore_errors=True)
    item("事实层", t_fact_layer)

    # 10b. 开放信息与印象（v5）
    def t_open_and_impression():
        """验证「B 能问主人爱吃什么 / 问不出主人住哪」这条线。

        这是整个 v5 里最该被测的一条 —— 它直接决定"她会不会说漏"。
        """
        import shutil as _sh
        import tempfile as _tf

        from bigfish import memory as M, vault as V

        tmpd = _tf.mkdtemp(prefix="bf-open-")
        db = os.path.join(tmpd, V.MEMORY_NAME)
        try:
            ok, err = M.ensure_schema(db)
            assert ok, err or "建库失败"
            ME = M.owner_of_private("the-owner")
            GUEST = M.owner_of_private("some-guest")
            M.ensure_owner(db, "dafeiyu", ME, display_name="主人")
            M.ensure_owner(db, "dafeiyu", GUEST, display_name="路人")

            V.capture_message("wechat:the-owner", "o1", "user",
                              "我爱吃烧烤，住在朝阳区，上月做了个手术", db_path=db)
            import sqlite3 as _sq

            _c = _sq.connect(db)
            try:
                SRC = int(_c.execute(
                    "SELECT c.id FROM chunks c JOIN messages m ON m.id=c.message "
                    "WHERE m.session='wechat:the-owner' ORDER BY c.id DESC LIMIT 1"
                ).fetchone()[0])
            finally:
                _c.close()

            # 三条：一条真的可以外传，两条看着像 shared 但话题是禁区
            M.remember(db, "dafeiyu", M.Fact(
                predicate="饮食偏好", object="爱吃烧烤", owner=ME,
                visibility=M.VIS_SHARED, topic="食物", source_chunks=[SRC]))
            M.remember(db, "dafeiyu", M.Fact(
                predicate="住址", object="住在朝阳区", owner=ME,
                visibility=M.VIS_SHARED, topic="住址", source_chunks=[SRC]))
            M.remember(db, "dafeiyu", M.Fact(
                predicate="身体状况", object="做了个手术", owner=ME,
                visibility=M.VIS_PRIVATE, topic="身体", source_chunks=[SRC]))

            # 路人问：只有"爱吃烧烤"能拿到
            said = M.owner_said_about(db, "dafeiyu", GUEST, ME, "你主人喜欢吃什么呀")
            assert "烧烤" in said, f"开放信息没给出去：{said!r}"
            assert "朝阳" not in said, "住址漏了"
            assert "手术" not in said, "身体状况漏了"

            # 注入层：路人自己的提示词里也不许有禁区内容
            blk = M.facts_for_prompt(db, "dafeiyu", owner=GUEST, tier=M.TIER_GUEST)
            assert "烧烤" in blk, "路人拿不到开放信息"
            assert "朝阳" not in blk, "路人注入里有住址"
            assert "手术" not in blk, "路人注入里有身体状况"

            # 主人自己：全都能看见（数据在他硬盘上），但也只是"看见"
            own = M.facts_for_prompt(db, "dafeiyu", owner=ME, tier=M.TIER_OWNER)
            assert "烧烤" in own and "朝阳" in own, "主人看不到自己的东西"

            # 印象页：一格一人、评语能存能读、开放清单只列能外传的
            board = M.impression_board(db, "dafeiyu")
            people = {p["owner"]: p for p in board.get("people") or []}
            assert ME in people and GUEST in people, str(list(people))
            opens = {o["object"] for o in people[ME]["open"]}
            assert "爱吃烧烤" in opens, opens
            assert "住在朝阳区" not in opens, opens
            ok, err = M.set_impression(db, "dafeiyu", ME, "话不多")
            assert ok, err
            board2 = {p["owner"]: p for p in M.impression_board(db, "dafeiyu")["people"]}
            assert board2[ME]["note"] == "话不多", board2[ME]["note"]

            # 池总览：只给数量，一条内容都不返回
            ps = M.pool_summary(db, "dafeiyu", main_owner=ME)
            assert ps.get("total", 0) >= 2, str(ps)
            for p in ps.get("pools") or []:
                assert "object" not in p and "predicate" not in p, p

            # 撤回开放：不是忘掉
            fid = [o["id"] for o in people[ME]["open"]][0]
            ok, err = M.revoke_open(db, "dafeiyu", fid)
            assert ok, err
            after = {f["id"]: f for f in M.list_facts(db, "dafeiyu")["items"]}[fid]
            assert after["visibility"] == M.VIS_PRIVATE, after["visibility"]
            assert after["status"] == "active", "撤回不该把事实作废"
            assert "烧烤" not in M.owner_said_about(db, "dafeiyu", GUEST, ME, "吃啥"), \
                "撤回之后还在外传"

            return "开放信息/禁区/印象页/池总览/撤回正常"
        finally:
            _sh.rmtree(tmpd, ignore_errors=True)
    item("开放信息", t_open_and_impression)

    # 11. 心情写入链路
    def t_mood_write():
        import shutil as _sh
        import tempfile as _tf

        from bigfish import mood_judge as MJ, vault as V

        tmpd = _tf.mkdtemp(prefix="bf-mood-")
        old = os.environ.get("BIGFISH_DATA_ROOT")
        os.environ["BIGFISH_DATA_ROOT"] = tmpd
        try:
            ok, err = MJ.apply_mood_delta({
                "moodDelta": 6,
                "dims": {"happy": 4},
                "reason": "自检", "confidence": 80,
            })
            assert ok, err or "写入失败"
            st = MJ.read_mood_state_now()
            assert st, "读不回来"
            # 70 + round(6/3) = 72
            assert st["mood"] == 72, f"mood={st['mood']}，步长可能没除 3"
            assert st["timeline"], "没写 timeline"
            return f"判定落盘正常（mood={st['mood']}）"
        finally:
            if old is None:
                os.environ.pop("BIGFISH_DATA_ROOT", None)
            else:
                os.environ["BIGFISH_DATA_ROOT"] = old
            _sh.rmtree(tmpd, ignore_errors=True)
    item("心情写入", t_mood_write)

    # 9. 数据根
    def t_root():
        from bigfish import vault as V
        root = V.resolve_data_root()
        assert root, "数据根为空"
        writable, werr = V.probe_writable(os.path.join(root, V.SOUL_NAME))

        def mark(kind: str) -> str:
            p = V.existing_path(root, kind)
            return f"{kind}={'有' if p and os.path.isfile(p) else '无'}"

        lg = V.legacy_layout(root)
        return (f"{root}"
                + "｜" + " ".join(mark(k) for k in ("soul", "mood", "memory"))
                + ("｜老布局残留" if lg else "")
                + ("｜可写" if writable else f"｜不可写：{werr[:40]}"))
    item("数据根", t_root)

    # 汇总
    nok = sum(1 for r in results if r[1] == "OK")
    nfail = sum(1 for r in results if r[1] == "FAIL")
    print()
    print(HR)
    print(f"  通过 {nok} / 失败 {nfail}")
    if nfail:
        for name, st, detail, dt in results:
            if st == "FAIL":
                print(f"    {name}: {detail}")
        print("  ✗ 有失败项")
        return 1
    print("  ✓ 全部通过")
    print(HR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
