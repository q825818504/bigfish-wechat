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

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

# 打包后（PyInstaller onedir）：
#   <安装目录>/大肥鱼.exe
#   <安装目录>/_internal/...        ← 依赖
#   <安装目录>/ui/                  ← 界面（外部，可改）
#   <安装目录>/bridge/              ← Node 桥接（外部）
# 开发态时 _ROOT 就是仓库根，结构一致。
FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    _APP_DIR = os.path.dirname(sys.executable)
    _RES_DIR = os.path.dirname(_HERE)   # _internal 的上一级也可能有 ui/
else:
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

    def __init__(self, watching: list[str], dry: bool = False):
        super().__init__()
        self.watching = list(watching)
        self.dry = dry
        self._stop = False
        self.recv_count = 0
        self.reply_count = 0

        self.reader = None
        self.sender = None
        self.watcher = None
        self.soul = None
        self.mcfg = None

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
        self.reader = WeChatReader().open()
        info = self.reader.self_info()
        self._log(f"账号：{info.get('nick_name')}", "in")

        cfg = load_json_config().get("auto_reply") or {}
        interval = float(cfg.get("interval", 1.2))
        allow_self = bool(cfg.get("allow_self", False))
        max_chars = int(cfg.get("max_reply_chars", 300))
        quiet = [int(h) for h in (cfg.get("quiet_hours") or [])]
        allow_all = [str(x) for x in (cfg.get("group_allow_all") or [])]
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
        # 记录「本引擎刚刚发出去的文本」，以及发送时间。
        # 文件传输助手这类「自己对自己」的会话里，发出去的回复
        # 会被监听器当成新消息再读回来，进而触发下一次回复，
        # 形成无限对话（实测会一直自问自答停不下来）。
        recently_sent: list = []        # [(文本, 时间戳)]
        ECHO_WINDOW = 20.0              # 秒
        ECHO_MAX = 20

        # 第二层：单会话连发上限。
        # 内容比对有可能漏（模型改写、微信截断），这里再加一道硬闸：
        # 同一会话在 BURST_WINDOW 秒内回复超过 BURST_MAX 条就熔断。
        last_reply_at: dict = {}        # chat_id -> [时间戳...]
        BURST_WINDOW = 90.0
        BURST_MAX = 4

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
            """这段文字是不是我们自己刚发出去的。"""
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
                k = min(20, len(n), len(t))
                if k >= 6 and (t.startswith(n[:k]) or n.startswith(t[:k])):
                    return True
            return False

        def remember_sent(text: str) -> None:
            recently_sent.append((text.strip(), time.time()))
            if len(recently_sent) > ECHO_MAX:
                del recently_sent[:-ECHO_MAX]

        def handle(msg) -> None:
            if self._stop:
                return
            if msg.is_self and not allow_self:
                return
            if not (msg.text or "").strip():
                return

            # 回环防护第一层：内容比对
            if text_is_echo(msg.text):
                self._log(f"[回环拦截] 这是我们刚发的，不再回："
                          f"{msg.preview(40)}", "")
                return

            # 回环防护第二层：单会话频率熔断
            now_ts = time.time()
            span = last_reply_at.setdefault(msg.chat_id, [])
            span[:] = [t for t in span if now_ts - t < BURST_WINDOW]
            if len(span) >= BURST_MAX:
                self._log(f"[熔断] {msg.chat_name} 在 "
                          f"{int(BURST_WINDOW)}s 内已回 {len(span)} 条，"
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

            self._log(f"[收] {msg.chat_name}：{msg.preview(50)}", "in")
            self.chat_event.emit(msg.chat_name, msg.preview(60), "in")

            # ---- 组装上下文 ----
            ctx = load_context(query=body or "")
            sys_parts = [ctx.persona or self.soul.render_persona()]
            if ctx.mood and ctx.mood.injected_text.strip():
                sys_parts.append("\n## 这会儿的状态\n\n"
                                 + ctx.mood.injected_text.strip())

            user_parts = []
            mem = ctx.render_memory()
            if mem:
                user_parts.append("## 你记得的事\n\n" + mem)
            scene = (f"你在微信群「{msg.chat_name}」里，"
                     f"{msg.sender_name or '某人'} @了你。" if is_group
                     else f"你在微信上和「{msg.chat_name}」聊天。")
            user_parts.append(
                f"## 这一轮\n\n{scene}\n"
                f"以{self.soul.name}的身份回一条消息。\n\n"
                "要求：\n"
                "- 像真人发微信，短，口语，不用 Markdown，不列清单\n"
                "- 不要复述对方说过的话，直接回应\n"
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
            hist.append((self.soul.address or "我", reply))

            if self.dry:
                self._log(f"[预演] {reply}", "think")
                return

            send_text = reply
            if is_group and msg.sender_name:
                # 群里回复时带上 @，不然没人知道在跟谁说话
                send_text = f"@{msg.sender_name} {reply}"

            if self.sender is None:
                self._log(f"[未发送] UIA 未就绪，生成内容：{reply[:50]}",
                          "think")
                return

            try:
                r = self.sender.send(msg.chat_name, send_text)
                if r.ok:
                    self.reply_count += 1
                    # 记下来，等这条消息被监听器读回来时能认出来
                    remember_sent(send_text)
                    remember_sent(reply)
                    last_reply_at.setdefault(msg.chat_id, []).append(time.time())
                    self.stats.emit(self.recv_count, self.reply_count)
                    self._log(f"[发] {reply[:60]}", "out")
                    self.chat_event.emit(msg.chat_name, reply[:60], "out")
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
    def _reader_or_open(self):
        if self._reader is None:
            from bigfish.wechat import WeChatReader
            self._reader = WeChatReader().open()
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
                    "unread": s["unread"],
                    "watching": s["username"] in self.watching,
                })
            return json.dumps({"chats": out}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def set_watching(self, usernames_json: str) -> str:
        try:
            lst = json.loads(usernames_json)
            self.watching = [str(x) for x in lst]
            return json.dumps({"ok": True, "count": len(self.watching)})
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

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
                return json.dumps({"error": "范围未确定。请先到「数据目录」页选好数据根与范围。",
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
                "scope": vault.current_scope_key(root),
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
                fresh["scope"] = vault.current_scope_key(root)
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
            merged["scope"] = vault.current_scope_key(root)
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
                "scopes": vault.list_scopes(),
                "currentScope": vault.current_scope_key(),
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

    # ── 记忆（可读可删可导出） ──
    @Slot(result=str)
    def memory_stats(self) -> str:
        try:
            from bigfish import vault

            root = vault.resolve_data_root()
            key = vault.current_scope_key(root)
            db = vault.memory_db_path(root, key)
            if not db:
                return json.dumps({"error": "范围未确定"}, ensure_ascii=False)
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

    # ── 数据目录（根 / 范围 / 搬家） ──
    @Slot(result=str)
    def get_data_root(self) -> str:
        try:
            from bigfish import vault

            root = vault.resolve_data_root()
            sc = vault.read_scope(root)
            key = vault.current_scope_key(root)
            # 可写性：灵魂文件所在目录能不能真写进去。
            # 界面要能提前告诉用户"这里改不了"，而不是等他点了保存才失败。
            from bigfish.soul import soul_file_path
            probe_target = soul_file_path(root, key) or os.path.join(root, ".probe")
            writable, w_err = vault.probe_writable(probe_target)
            return json.dumps({
                "ok": True,
                "root": root,
                "custom": vault.read_data_root(),
                "defaultRoot": vault.default_data_root(),
                "rootFile": vault.root_store_path(),
                "scopeFile": vault.scope_store_path(root),
                "scope": sc,
                "currentKey": key,
                "dshHome": vault.dsh_home(),
                "scopes": vault.list_scopes(root),
                "writable": writable,
                "writeError": w_err,
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    @Slot(str, result=str)
    def set_data_root(self, path: str) -> str:
        """直接改数据根（不搬数据）。想搬请走 inspect_move/apply_move。"""
        try:
            from bigfish import vault

            p = (path or "").strip()
            if p and not os.path.isdir(p):
                return json.dumps({"ok": False,
                                   "error": f"目录不存在：{p}"}, ensure_ascii=False)
            ok, err = vault.write_data_root(p)
            if not ok:
                return json.dumps({"ok": False, "error": err}, ensure_ascii=False)
            self.log_message.emit(f"[数据] 数据根 → {p or '(默认)'}", "")
            return json.dumps({"ok": True, "root": vault.resolve_data_root()},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(str, str, str, result=str)
    def set_scope(self, mode: str, workspace_id: str, session_ids_json: str) -> str:
        """写 scope.json。mode = workspace | session。"""
        try:
            from bigfish import vault

            ids = []
            if session_ids_json:
                try:
                    parsed = json.loads(session_ids_json)
                    if isinstance(parsed, list):
                        ids = [str(x) for x in parsed]
                except Exception:
                    ids = [s.strip() for s in session_ids_json.split(",") if s.strip()]

            root = vault.resolve_data_root()
            ok, err = vault.write_scope(root, mode, workspace_id, ids)
            if not ok:
                return json.dumps({"ok": False, "error": err}, ensure_ascii=False)
            self.log_message.emit(
                f"[数据] 范围 → {vault.current_scope_key(root)}", "")
            return json.dumps({"ok": True,
                               "scope": vault.read_scope(root),
                               "currentKey": vault.current_scope_key(root)},
                              ensure_ascii=False)
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

    @Slot(result=str)
    def get_scope_candidates(self) -> str:
        """扫 DSH 里现有的工作区与会话，给「范围」页做下拉。

        只读 DSH 的 storages/workspace.json 和 sessions/ 目录 —— 不改任何东西。
        """
        try:
            from bigfish import vault

            home = vault.dsh_home()
            workspaces = []
            sessions = []

            wpath = os.path.join(home, "storages", "workspace.json")
            try:
                with open(wpath, "r", encoding="utf-8") as f:
                    w = json.load(f)
                table = ((w or {}).get("tables") or {}).get("workspaces") or {}
                for uuid, v in table.items():
                    workspaces.append({
                        "id": str(uuid),
                        "title": str((v or {}).get("title") or ""),
                        "path": str((v or {}).get("path") or ""),
                    })
            except Exception:
                pass

            sess_root = os.path.join(home, "sessions")
            try:
                for ws in sorted(os.listdir(sess_root)):
                    wsdir = os.path.join(sess_root, ws)
                    if not os.path.isdir(wsdir):
                        continue
                    for sid in sorted(os.listdir(wsdir)):
                        # 裸 UUID 形状的是中间层目录，不是会话
                        if len(sid) >= 8 and sid[8] == "-":
                            continue
                        if not os.path.isfile(os.path.join(
                                wsdir, sid, "session.v4.jsonl.zstd")):
                            continue
                        sessions.append({"id": sid, "workspace": ws})
            except Exception:
                pass

            # 给工作区补上「有灵魂/记忆」的标记，用户能直接看出哪个是活的
            scopes = vault.list_scopes()
            live_dirs = {}
            for s in scopes:
                live_dirs[s["key"]] = s

            return json.dumps({
                "ok": True,
                "workspaces": workspaces,
                "sessions": sessions[:500],
                "liveScopes": {k: {
                    "hasSoul": v["has_soul"],
                    "hasMemory": v["has_memory"],
                    "name": v["name"],
                } for k, v in live_dirs.items()},
            }, ensure_ascii=False)
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
            "max_tokens": c.get("max_tokens", 1600),
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
            c["max_tokens"] = int(d.get("max_tokens", 1600))

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

        w = EngineWorker(self.watching, dry=dry)
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
    r = WeChatReader().open()
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
            r2 = WeChatReader().open()
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
    app = QApplication(sys.argv)
    app.setApplicationName("大肥鱼")
    app.setOrganizationName("bigfish")

    win = MainWindow()
    win.show()
    return app.exec()


def run_selftest() -> int:
    """在 exe 内部跑自检，输出到控制台。

    检查项和 tools/selftest.py 一致，但跑的是**打包后的代码**——
    这才是用户实际会遇到的那份。

    exe 是 console=False 的 GUI 程序，默认没有控制台。
    从命令行带 --selftest 启动时，主动 AttachConsole 到父进程
    （cmd/PowerShell），这样输出才能被看到。
    """
    import traceback

    # GUI 程序附加到父控制台，否则 print 全部丢弃
    if getattr(sys, "frozen", False):
        try:
            import ctypes
            ATTACH_PARENT_PROCESS = -1
            if ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
                sys.stdout = open("CONOUT$", "w", encoding="utf-8",
                                  errors="replace", buffering=1)
                sys.stderr = open("CONOUT$", "w", encoding="utf-8",
                                  errors="replace", buffering=1)
        except Exception:
            pass

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
        from bigfish.soul import load_soul
        s = load_soul()
        p = s.render_persona()
        assert s.name and p
        return f"{s.name} rev{s.revision}，人格 {len(p)} 字符"
    print()
    print("工厂自检")
    print("-" * 66)
    item("灵魂", t_soul)

    # 2. 心情
    def t_mood():
        from bigfish.bridge_client import load_mood_only
        m = load_mood_only()
        assert m and m.value is not None
        return f"{m.label} {m.value:.1f}"
    item("心情", t_mood)

    # 3. 记忆
    def t_mem():
        import glob
        import sqlite3
        import shutil

        from bigfish.bridge_client import _default_data_root, _pick_scope_dir
        root = _default_data_root()
        assert root, "找不到 dataRoot"
        scope = _pick_scope_dir(root)
        assert scope, "找不到 scope"
        dbs = glob.glob(os.path.join(scope, "memory-*.sqlite3"))
        assert dbs, "找不到记忆库"
        tmp = os.path.abspath(os.path.join(_APP_DIR, ".tmp",
                                           "exe_selftest.sqlite3"))
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        shutil.copyfile(dbs[0], tmp)
        conn = sqlite3.connect(tmp)
        try:
            n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            conn.close()
        assert n > 0, "记忆库为空"
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
            assert V.scope_key("workspace", "abc") == "ws-abc"
            assert V.short_key("ws-83ba9d80-ec53-4a24") == "ws-83ba9d80-ec53"

            # 心情配置：未知字段必须保留
            assert V.INJECT_MODES == ["off", "label", "event"]
            return f"读写/渲染/分档 全部正常（rev {back['revision']}）"
        finally:
            _sh.rmtree(tmpd, ignore_errors=True)
    item("可写层", t_vault)

    # 9. 数据根
    def t_root():
        from bigfish import vault as V
        root = V.resolve_data_root()
        assert root, "数据根为空"
        key = V.current_scope_key(root)
        writable, werr = V.probe_writable(
            V.scope_file_path(root, key, "soul") if key
            else os.path.join(root, ".probe"))
        return (f"{root}"
                + (f"｜范围 {key}" if key else "｜范围未定")
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
