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
import sys
import time
import traceback

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

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
            self.sender = UiaSender()
            self.sender.wake()
            self._log("UIA 已就绪", "in")

        from bigfish.bridge_client import load_context

        convs: dict[str, list] = {}

        def handle(msg) -> None:
            if self._stop:
                return
            if msg.is_self and not allow_self:
                return
            if not (msg.text or "").strip():
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

            try:
                r = self.sender.send(msg.chat_name, send_text)
                if r.ok:
                    self.reply_count += 1
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
        }, ensure_ascii=False)

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

    # ── 人设 ──
    @Slot(result=str)
    def get_persona(self) -> str:
        try:
            from bigfish.soul import load_soul

            soul = load_soul()
            return json.dumps({
                "name": soul.name,
                "address": soul.address,
                "whoIAm": soul.who_i_am,
                "values": soul.values,
                "traits": soul.traits,
                "selfNotes": soul.self_notes,
                "revision": soul.revision,
                "persona": soul.render_persona(),
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    # ── 心情 ──
    @Slot(result=str)
    def get_mood(self) -> str:
        try:
            from bigfish.bridge_client import load_mood_only

            m = load_mood_only()
            if not m:
                return json.dumps({"error": "读不到情绪状态"},
                                  ensure_ascii=False)
            return json.dumps({
                "value": m.value,
                "label": m.label,
                "reason": m.last_reason,
                "injected": m.injected_text,
                "dims": m.dims,
                "updated": time.strftime(
                    "%m-%d %H:%M",
                    time.localtime(m.updated_at / 1000)) if m.updated_at else "",
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    # ── 记忆 ──
    @Slot(result=str)
    def memory_stats(self) -> str:
        try:
            import glob
            import sqlite3

            from bigfish.bridge_client import _default_data_root
            root = _default_data_root()
            if not root:
                return json.dumps({"error": "找不到 dataRoot"},
                                  ensure_ascii=False)
            from bigfish.bridge_client import _pick_scope_dir
            scope_dir = _pick_scope_dir(root)
            if not scope_dir:
                return json.dumps({"error": "找不到 scope"},
                                  ensure_ascii=False)
            dbs = glob.glob(os.path.join(scope_dir, "memory-*.sqlite3"))
            if not dbs:
                return json.dumps({"error": "找不到记忆库"},
                                  ensure_ascii=False)

            tmp = self._tmp_copy(dbs[0])
            conn = sqlite3.connect(tmp)
            try:
                total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                sessions = conn.execute(
                    "SELECT COUNT(DISTINCT session) FROM messages").fetchone()[0]
                newest = conn.execute(
                    "SELECT MAX(source_time) FROM messages").fetchone()[0]
            finally:
                conn.close()
            return json.dumps({
                "total": total,
                "sessions": sessions,
                "newest": time.strftime("%m-%d %H:%M",
                                        time.localtime(newest))
                if newest else "",
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

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
                            "time": time.strftime(
                                "%m-%d %H:%M", time.localtime(ts / 1000)
                                if ts > 1e12 else time.localtime(ts))
                            if ts else "",
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

    # ── 记忆 ──
    @Slot(int, result=str)
    def get_memory(self, limit: int) -> str:
        try:
            import math
            import random
            from bigfish.bridge_client import _default_data_root

            root = _default_data_root()
            if not root:
                return json.dumps({"error": "找不到 dataRoot"},
                                  ensure_ascii=False)
            import glob
            from bigfish.bridge_client import _pick_scope_dir
            scope_dir = _pick_scope_dir(root)
            if not scope_dir:
                return json.dumps({"error": "找不到 scope 目录"},
                                  ensure_ascii=False)

            # 直接读 sqlite，拿最近的消息作为记忆点
            import sqlite3
            dbs = glob.glob(os.path.join(scope_dir, "memory-*.sqlite3"))
            if not dbs:
                return json.dumps({"cells": []}, ensure_ascii=False)

            # 复制一份再读，避免和 DSH 抢锁
            import shutil
            tmpdir = os.path.join(_ROOT, ".tmp")
            os.makedirs(tmpdir, exist_ok=True)
            tmp = os.path.join(tmpdir, "mem_ui.sqlite3")
            shutil.copyfile(dbs[0], tmp)
            for suf in ("-wal", "-shm"):
                if os.path.exists(dbs[0] + suf):
                    try:
                        shutil.copyfile(dbs[0] + suf, tmp + suf)
                    except Exception:
                        pass

            conn = sqlite3.connect(tmp)
            try:
                rows = conn.execute(
                    "SELECT id, role, text, session FROM messages "
                    "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            finally:
                conn.close()

            cells = []
            rnd = random.Random(42)
            for i, (mid, role, text, session) in enumerate(rows):
                t = (text or "").strip().replace("\n", " ")
                if not t:
                    continue
                # 粗略分级：长文本更可能是「长久」，短问答是「片段」
                cat = ("durable" if len(t) > 120
                       else "episodic" if len(t) > 40 else "fleeting")
                cells.append({
                    "id": mid,
                    "title": t[:90],
                    "category": cat,
                    "heat": min(8, 1 + len(t) / 40),
                    "size": min(30, 12 + len(t) / 18),
                    "ring": 30 + (i % 4) * 11,
                    "jitter": rnd.uniform(-0.35, 0.35),
                    "star": session or "",
                })
            return json.dumps({"cells": cells}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    # ── 配置 ──
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

        w = EngineWorker(self.watching, dry=False)
        w.log.connect(self._on_log)
        w.chat_event.connect(self._on_chat)
        w.stats.connect(self._on_stats)
        w.failed.connect(self._on_fail)
        w.start()
        self.worker = w
        return json.dumps({"ok": True})

    @Slot()
    def stop_engine(self) -> None:
        if self.worker:
            self.worker.stop()
            self.worker.wait(6000)
            self.worker = None
        self.started_at = 0.0

    def _on_log(self, text: str, kind: str) -> None:
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


def main() -> int:
    QApplication.setAttribute(
        getattr(Qt, "AA_ShareOpenGLContexts", Qt.ApplicationAttribute.AA_ShareOpenGLContexts),
        True)
    app = QApplication(sys.argv)
    app.setApplicationName("大肥鱼")
    app.setOrganizationName("bigfish")

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
