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
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path


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

# ⚠ 配置和 data/ 是同一层的东西，都放**exe 旁边**（_APP_DIR），不放 _internal。
#
#   <安装目录>/BigFish.exe
#   <安装目录>/config.json     ← 用户可见、可备份、重装不丢
#   <安装目录>/data/           ← 记忆 / 情绪 / 插件
#   <安装目录>/_internal/...   ← 程序自己的东西，升级时整个覆盖
#
#   这里曾经写成 _ROOT（= _internal），于是出现了一个很难看的现象：
#   设置页保存成功、配置文件里明明是新值，但界面刷新后又变回旧的 ——
#   因为写的是 _internal，读的是…… 其实是同一份，但 packaging 时的
#   deploy 会把它连同整个 _internal 一起换掉。更早还有一份外层的老配置
#   在 `paths.app_root()` 那套里被读到，两边打架。
#
#   现在只有一个来源：paths.app_root()/config.json。
CONFIG_PATH = os.path.join(_APP_DIR, "config.json")

#: 设置页那个「处理自己发的消息」开关，代表的就是这一个会话。
#:
#: ⚠ 读和写必须都带上它。曾经写会话层、读全局层，结果是
#:   「保存成功、界面还是关的」——钱花了没生效，最容易让人怀疑人生。
#:   这个开关的语义就是「只对文件传输助手生效的自测通道」，
#:   写成全局等于"所有会话都能回应自己"，正是 11-04 事故的形态。
SELF_TEST_CHAT = "filehelper"

#: 微信把表情读回来时渲染成的样子（render.py 里 type 47 / 11000）
STICKER_MARKS = ("[表情]", "[动画表情]")

#: 句子里出现这些词，就是"明确要一张图"。
#: 命中后**不掷概率、不受冷却限制** —— 用户开口要了，还让他等 180 秒说不过去。
STICKER_WANT_WORDS = ("表情包", "表情", "斗图", "发个图", "发张图", "来张图", "发图")


def wants_sticker(text: str) -> bool:
    """这句是不是在要表情包。

    只做包含判断，不做语义分析：误判的代价是多发一张（她本来也会发），
    漏判的代价是用户明确要求了却没反应 —— 后者难受得多。
    """
    value = str(text or "")
    return any(w in value for w in STICKER_WANT_WORDS)

#: 睡醒之后多久开始回"睡着期间收到的消息"（秒）。
#:
#: 为什么不是立刻：刚睁眼就秒回不像人；也给她留一点"刚醒"的状态进提示词
#: （`life.briefing` 会写「刚看到」那一段）。
CATCHUP_DELAY = 20.0

#: 发出表情后，多久之内读回来的同一条算回环。
#: 实测数据库落库要 10~12 秒（比文字慢得多），10 秒窗口会把"已发出"
#: 误判成"没发出去"。这里给到 45 秒，留足余量。
STICKER_ECHO_WINDOW = 45.0


def restore_watching_for(c: dict, acc: str) -> list[str]:
    """把某个号上次的监听列表恢复到 `auto_reply.chats`，并返回它。

    三件事必须一起做，少一件就会出"界面勾着、启动说没选"，
    或者"换了号还把上一个号的会话带过来"：

      1. 这个号专属的那份（per_account）优先；
      2. 从没存过就用全局那份当种子 —— 老版本只有这一份，
         直接清空等于把老用户的选择丢掉；
      3. **无论结果空不空，都写回 auto_reply.chats** ——
         留着上一个号的值，启动时的兜底会把别人的会话捡起来。
    """
    ar = c.setdefault("auto_reply", {})
    per = c.setdefault("per_account", {})
    saved = per.get(acc)
    if saved is None:
        restored = [str(x) for x in (ar.get("chats") or [])]
        if restored:
            per[acc] = {"chats": list(restored)}
    else:
        restored = [str(x) for x in ((saved or {}).get("chats") or [])]
    ar["chats"] = list(restored)
    return restored


def take_sticker_echo(sent: dict, chat: str, now: float,
                      window: float = STICKER_ECHO_WINDOW) -> bool:
    """这次读回来的表情，是不是我们刚发出去的那个？

    表情在库里没有文本（content 是加密串），没法像文字那样比对内容，
    只能靠**时间**：发出去的时候记一个时间戳，读回来的时候取走一个。

    一对一取走（而不是"有记录就算"），是为了不误伤：她连发两张，
    对方真的回一张时，取第二张的戳就不该再拦。
    """
    stamps = sent.get(chat)
    if not stamps:
        return False
    while stamps and (now - stamps[0]) > window:
        stamps.pop(0)
    if not stamps:
        return False
    stamps.pop(0)
    return True


def is_admin() -> bool:
    """当前进程是否有管理员权限（UIA 热激活需要）。"""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


_ADMIN_CACHE: list = []


def _is_admin_cached(bridge=None) -> bool:
    """is_admin 的缓存版。

    `IsUserAnAdmin` 本身不贵，但它要进一次 shell32 ——
    get_status 每 2 秒调一趟，加上旁边的微信进程探测，
    这些"每轮都算"的小东西叠起来就是"点按钮慢半拍"。
    结果缓存在 bridge 实例上，进程级兜底。
    """
    if bridge is not None and getattr(bridge, "_admin_flag", None) is not None:
        return bridge._admin_flag
    if not _ADMIN_CACHE:
        _ADMIN_CACHE.append(is_admin())
    if bridge is not None:
        bridge._admin_flag = _ADMIN_CACHE[0]
    return _ADMIN_CACHE[0]


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
# ⚠ 别在这里再写一次 CONFIG_PATH。
#   这里曾经有一行 `CONFIG_PATH = os.path.join(_ROOT, "config.json")`，
#   静默覆盖了上面那个指向 exe 目录的定义 —— 于是「保存成功但界面刷新
#   又变回去」。UI_DIR 用 _ROOT 是对的（ui/ 确实在 _internal 里），
#   但配置不在那里，它是用户的文件。


# ══════════════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════════════
def _migrate_config_path(old: str, new: str) -> None:
    """把早先误写在 `_internal` 里的配置搬到 exe 旁边。

    2026-11-04 之前的版本把 `CONFIG_PATH` 指向了 `_ROOT`（=`_internal`），
    于是用户在设置页保存的模型配置全写在那儿。路径改回来之后要搬一次 ——
    不搬的话用户看到的是「昨晚配好的 agnes 又变回 magpie 了」。

    只在两份都有明确内容时才挑：外层已经有 `llm` 段就以它为准，
    旧的那份改名 `.bak` 留个证据，不直接删。
    """
    if not os.path.isfile(old) or os.path.abspath(old) == os.path.abspath(new):
        return
    try:
        with open(old, "r", encoding="utf-8") as f:
            od = json.load(f)
    except Exception:
        return
    if not isinstance(od, dict):
        return

    cur: dict = {}
    if os.path.isfile(new):
        try:
            with open(new, "r", encoding="utf-8") as f:
                cur = json.load(f)
        except Exception:
            cur = {}
    if not isinstance(cur, dict):
        cur = {}

    # 外层已经是新结构 → 以它为准，旧份收起来
    if isinstance(cur.get("llm"), dict) or not isinstance(od.get("llm"), dict):
        try:
            os.replace(old, old + ".bak")
        except Exception:
            pass
        return

    merged = dict(cur)
    merged.update(od)          # _internal 那份更新，覆盖外层
    try:
        tmp = new + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        os.replace(tmp, new)
        os.replace(old, old + ".bak")
    except Exception:
        pass


_config_migrated = False


def _ensure_config_home() -> None:
    """跑一次：把配置固定在 exe 旁边。

    frozen 时才有"两份"的问题；开发态 _APP_DIR == _ROOT，不用管。
    """
    global _config_migrated
    if _config_migrated:
        return
    _config_migrated = True
    if not FROZEN:
        return
    lo = os.path.join(_RES_DIR, "config.json")
    if os.path.isfile(lo):
        _migrate_config_path(lo, CONFIG_PATH)


def _cut_at_sentence(text: str, limit: int) -> str:
    """超长回复按**句子边界**截断，宁可少说一句，也不吐半截字。

    以前是 `reply[:limit] + "…"`：31 字的回复被砍成「…你要不说我…」，
    用户看到的就是「日志里内容显示不全」—— 其实是她的话被打断了。

    `{{split}}` 不算字数：它不是她说的字，却占 11 个字符，
    出现一次就让 30 字预算缩水三分之一。标记本身要留着，
    后面 `say.split_reply` 还要靠它分段。
    """
    marker = "{{split}}"
    plain = text.replace(marker, "")
    if len(plain) <= limit:
        return text

    # 按句末标点 / 分条标记切段（标记单独成段，不占预算）
    segs: list = []
    buf = ""
    i = 0
    while i < len(text):
        if text.startswith(marker, i):
            if buf:
                segs.append(buf)
                buf = ""
            segs.append(marker)
            i += len(marker)
            continue
        buf += text[i]
        if text[i] in "。！？!?～~\n":
            segs.append(buf)
            buf = ""
        i += 1
    if buf:
        segs.append(buf)

    out: list = []
    used = 0
    for seg in segs:
        if seg == marker:
            out.append(seg)
            continue
        if used + len(seg) > limit:
            break
        out.append(seg)
        used += len(seg)

    joined = "".join(out).strip()
    # 截在标记后面会多出一段空话（split_reply 会把它当一条空消息）
    while joined.endswith(marker):
        joined = joined[: -len(marker)].rstrip()
    if joined.replace(marker, "").strip():
        # 截掉的部分不补省略号：完整的句子比"…"更像人说话
        return joined
    # 第一句就超预算：**宁可超一点，也不要半句**。
    #
    # 2026-11-09 实测：跨轮配额把预算压到 18 字，她那句
    # 「交接这段最难熬，还得对着那领导笑」被硬切成
    # 「交接这段最难熬，还得对着那领导」—— 缺一个谓语，直接成了病句，
    # 用户以为是语音被砍了一半。
    # 一句完整的话比"少数几个字"重要得多，所以只有离谱超长（2 倍以上）
    # 才硬切 —— 那种情况一定是模型写嗨了。
    if len(plain) <= limit * 2:
        return text.strip()
    return plain[:limit].rstrip("，,、 ") + "…"


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


def _bonds_of(db: str, owner: str, limit: int = 20) -> list:
    """某人的关系卡，给印象页用。出错返回空列表 —— 一栏读不出来不该让整页白屏。"""
    try:
        from bigfish import bond as B

        if not (db and owner):
            return []
        B.ensure_schema(db)
        return [{
            "id": it["id"],
            "kind": it["kind"],
            "kindLabel": B.KIND_LABELS.get(it["kind"], it["kind"]),
            "text": it.get("text") or "",
            "evidence": it.get("evidence") or "",
        } for it in B.list_bonds(db, "dafeiyu", owner, limit=limit)]
    except Exception:
        return []


def _bond_events_of(db: str, owner: str, limit: int = 8) -> list:
    """关系事件时间线，给印象页用。没有事件就返回空列表。"""
    try:
        from bigfish import bond as B

        if not (db and owner):
            return []
        B.ensure_schema(db)
        return [{
            "id": it.get("id"),
            "kind": it.get("kind") or "",
            "kindLabel": B.EVENT_LABELS.get(it.get("kind") or "", "事件"),
            "what": it.get("what") or "",
            "evidence": it.get("evidence") or "",
            "at": it.get("at") or 0,
            "atText": ts_to_local(it.get("at") or 0),
        } for it in B.list_events(db, "dafeiyu", owner, limit=limit)]
    except Exception:
        return []


def _bond_block(owner: str) -> str:
    """关系卡：她怎么称呼这个人、有什么一直没说出口。

    每条都带**原话证据**，写不进来的就写不进来 —— 那是它和
    「画像」的区别：画像是她归纳的，关系卡是从某句话里长出来的。
    """
    try:
        from bigfish import bond as B, vault as V

        db = V.memory_db_path()
        if not (db and os.path.isfile(db) and owner):
            return ""
        return B.render_for_prompt(db, "dafeiyu", owner)
    except Exception:
        return ""


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


def _owner_echo_block(owner: str, tier: str, chat_id: str,
                      hours: float = 24.0, limit: int = 5) -> str:
    """主人专用：「最近别处提到你的事」。

    为什么需要这一段：
      A2 把原文检索按 session 收窄了（防止翻到别人的私聊），副作用是
      **主人自己也看不到别人在别的会话里提到他**。四条丶说「其实我知道
      你主人是谁」「你主人是老爸！」，主人回头问「听说有人在打听我」——
      她手里一条依据都没有，只能答「我怎么不知道」（2026-11-08 现场）。

    边界：
      · 只对 tier=owner 生效，只读 messages 表，不做关键词放行判断
      · 不碰 `_open_block` 的白名单 —— 那段管"能不能告诉别人"，
        这段管"主人自己该知道什么"
      · 只挑提到「主人」或主人昵称的原文，避免把别人的闲话整段搬过来
    """
    try:
        from bigfish import memory as M, vault as V

        if tier != M.TIER_OWNER or not owner:
            return ""
        db = V.memory_db_path()
        if not (db and os.path.isfile(db)):
            return ""
        cutoff = int((time.time() - hours * 3600) * 1000)
        conn = M.connect(db, read_only=True)
        try:
            names = {}
            try:
                for row in conn.execute("SELECT owner, display_name FROM owners"):
                    names[str(row[0])] = str(row[1] or "")
            except Exception:
                names = {}
            rows = conn.execute(
                "SELECT session, text, COALESCE(source_time, recorded_at) AS t "
                "FROM messages WHERE bank=? AND role='user' AND session<>? "
                "AND COALESCE(source_time, recorded_at)>=? "
                "ORDER BY t DESC LIMIT 200",
                ("dafeiyu", f"wechat:{chat_id}", cutoff)).fetchall()
        finally:
            conn.close()

        me_name = names.get(owner, "") or ""
        words = [w for w in ("主人", me_name) if w]
        if not words:
            return ""
        out = []
        for r in rows:
            text = str(r["text"] or "").strip()
            if not text or not any(w in text for w in words):
                continue
            sess = str(r["session"] or "")
            who = names.get("private:" + sess) or sess.replace("wechat:", "")
            out.append(f"- {who}：{text[:60]}")
            if len(out) >= limit:
                break
        return "\n".join(out)
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
    _ensure_config_home()
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
    _ensure_config_home()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def _voice_devices() -> list:
    """播放设备列表；只读，不打开设备。气泡路线选 CABLE Input。"""
    try:
        from bigfish.voice_audio_out import list_output_devices

        return list_output_devices()
    except Exception:
        return []


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
        #: chat_id -> [(时刻, 这一轮发了几条)]，**跨轮条数配额**用。
        #: 只放内存：进程重启清零（和账本 D-9 同一个理由 ——
        #: 残留的旧计数会让重启后第一句话莫名变短）。
        self._sent_parts: dict = {}
        #: 睡醒补回：[(到点时刻, chat_id, 会话名, 对方最后一句)]。
        #: 她睡着时别人发的消息，醒来要**主动回一条**，
        #: 而不是"等对方下次说话才带出来"（2026-11-09 用户要求）。
        self._catchup: list = []
        #: 她的生活状态（bigfish/life.py）。None = 还没加载。
        #: 只在引擎线程里读写，不跨线程共享。
        self._life = None
        #: 上一批 tick 之后她又回了几轮（聊天也累，计入精力消耗）
        self._talk_turns = 0
        #: 她自己会做的事那一半（bigfish/world.py）：情绪平复、叙事、
        #: 主动开口、追问。全部在独立模块里，不再往 _run 的闭包里塞。
        self._world = None
        #: 最近一次"对方说话"的会话。主动消息的待追问状态按它清理。
        self._last_peer_chat = ""
        # ── 接话：这一句接不接 / 要不要补一句（0.9.4）──
        #: 连续几轮"不值得接"。连着 2 次之后第三次强制回 ——
        #: 不然你连着说三句她一声不吭，和程序坏了没有区别。
        self._silent_streak = 0
        #: 上次她说话的时刻（补一句前要判断"这期间对方说过话没有"）
        self._last_reply_at = 0.0
        #: 待发的"补一句"
        self._after: dict = {}
        #: 上次补一句的时刻 / 之后过了几条回复
        self._last_after_at = 0.0
        self._replies_since_after = 99
        #: 每个会话各自收到过几条消息。用来判断"她补那句话之后，
        #: **这个会话**里对方有没有再说话" —— 全局计数会把群聊里的
        #: 无关消息也算进来，让她的半句话莫名其妙消失。
        self._peer_seq: dict = {}
        #: 记账器与账本（_run 里创建后挂上来，主动发送要用同一条账）
        self._guard = None
        self._ledger = None
        #: 声音：每个会话的冷却/每日/连续计数
        self._voice_stats: dict = {}
        self._voice_lock = threading.Lock()

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

    # ── 跨轮条数配额（0.11.1）──
    def _note_sent_parts(self, chat_id: str, n: int) -> None:
        """记下「这个会话刚刚发出去几条」。给下一轮的表达预算用。

        为什么需要：单轮看每轮都合理，连起来就是刷屏 ——
        用户实测「我连发两条，她第一轮 3 条、第二轮又 3 条 = 一次 6 条」。
        """
        try:
            now = time.time()
            span = self._sent_parts.setdefault(str(chat_id), [])
            span.append((now, max(1, int(n or 1))))
            span[:] = [t for t in span if now - t[0] < 300.0]
        except Exception:
            pass

    def _recent_sent_parts(self, chat_id: str, window: float = 90.0):
        """最近 window 秒内：这个会话发了几条、回了几轮。"""
        try:
            now = time.time()
            span = [t for t in self._sent_parts.get(str(chat_id), [])
                    if now - t[0] < window]
            return sum(n for _t, n in span), len(span)
        except Exception:
            return 0, 0

    def _listen_policy(self):
        """把「说法」面板上的攒批参数翻成 ListenPolicy。

        为什么住在说法插件里：这几项决定「她什么时候开始想」，
        属于「怎么说话」的一部分，和分条、节奏是同一类东西。

        读不到就整份用默认 —— 攒批不该因为配置坏了而停摆。
        """
        from bigfish import listen as _L

        cfg = {}
        try:
            from bigfish import plugins as _P

            _P.ensure_loaded()
            cfg = _P.config_of("say") if _P.is_enabled("say") else {}
        except Exception:
            cfg = {}

        def _num(key, default, lo, hi):
            try:
                v = float(cfg.get(key, default))
            except (TypeError, ValueError):
                v = float(default)
            return max(lo, min(hi, v))

        min_wait = _num("batch_min_wait", 6.0, 0.5, 20.0)
        cap = _num("batch_max_wait", 30.0, 6.0, 120.0)
        # 硬上限必须比最短等待长，否则「至少等 6 秒」和「最多等 5 秒」自相矛盾
        cap = max(cap, min_wait + 1.0)
        return _L.ListenPolicy(
            batch_min_wait=min_wait,
            judge_enabled=bool(cfg.get("batch_judge", True)),
            judge_extra=_num("batch_judge_extra", 8.0, 1.0, 20.0),
            judge_max_rounds=int(_num("batch_judge_rounds", 3, 0, 10)),
            judge_timeout=_num("batch_judge_timeout", 6.0, 1.0, 30.0),
            max_wait=cap,
        )

    def _pick_sticker(self, msg, body: str, reply: str,
                      mood=None, mark: str = "") -> str:
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

        # ---- 模型自己表态 ----
        #
        # 以前「要不要发」只有掷骰子：模型想配图也表达不了（D-78 的尾巴）。
        # 现在它可以在正文里写标记，系统照它的意思办：
        #   [[text]]    → 这一轮明确不配图（比冷却、概率都优先）
        #   [[sticker]] → 跳过概率，但**仍受冷却** —— 每句都配图就是刷屏
        if mark == "text":
            return "", "after"
        forced = mark == "sticker"

        # ---- 被明确要求时，跳过冷却和概率 ----
        #
        # 以前这里只有「冷却 + 15% 概率」，**完全不看用户说了什么**。
        # 于是用户说"发个表情给爷看看"，掷骰子没中就什么都不发，
        # 她只能拿文字糊弄一句「鼓腮算表情不」。
        wanted = wants_sticker(body)

        if not wanted:
            # ---- 冷却 ----
            now = time.time()
            span = self._sticker_sent.setdefault(msg.chat_id, [])
            span[:] = [t for t in span if now - t < float(cfg.get("cooldown", 180))]
            if span:
                return "", "after"

            # ---- 概率 ----
            # 模型自己点了 [[sticker]] 就不掷骰子：它看过这句话，
            # 比一个固定概率更知道该不该配图。冷却上面已经拦过了。
            if not forced:
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

    def _compose(self, *, chat_id: str, chat_name: str, sender_name: str,
                 incoming: str, is_group: bool, hist: list,
                 owner_raw: str = "", is_owner_chat: bool = False,
                 owner_id: str = "", proactive_reason: str = "",
                 must_reply: bool = False):
        """组装这一轮要喂给模型的两段（system / user）。

        正常回复和主动开口**共用这一份**，只在最后一段指令上分叉：

            正常  以她的身份回一条消息
            主动  以她的身份先说一句，不回应谁

        为什么必须共用：主动消息以前只带人格和 8 条历史，没有记忆、
        没有心情、没有生活状态 —— 于是"她主动说的那句"和"她回你的那句"
        人格密度完全不一样，用户能感觉到不像同一个人。

        返回的 owner / tier / origin / group_owner 还要给调用方记账用。
        """
        # 局部 import：桥接起不来不该拖垮整个程序启动（和 memory 同一个理由）
        from bigfish.bridge_client import load_context

        ctx = load_context(query=incoming or "", chat=chat_id)
        # 桥接挂了不能等于她失忆：降级状态要让用户看见，否则
        # "她怎么不记得了"会被当成记忆坏了，实际是 Node 没起来。
        if getattr(ctx, "degraded", ""):
            self._log(
                "[记] 记忆降级："
                + ("用本地最近原文兜底" if ctx.degraded == "recent"
                   else "记忆不可用（这一轮只有人格与对话）"),
                "" if ctx.degraded == "recent" else "err")

        # system 段按**身份序**排：内建段（灵魂 860 / 心情 900）与
        # 插件段（生活 870 / 目标 880 / 关系 920 / 守卫 9990）一起排序。
        #
        # 为什么不直接 append：插件有自己的座位，顺序决定语气。
        # 守卫必须最后出现（越靠后越有分量），时间感知要在心情之前
        # （"现在几点"是处境，"此刻怎样"是气氛，处境先立住）。
        # 详见 bigfish/plugins/__init__.py 的 BUILTIN_SEATS / PLUGIN_SEATS。
        _persona_text = ctx.persona or self.soul.render_persona()
        # 人格段里写着「称呼对方「爸」」——那是她对**主人**的叫法。
        # 照搬到四条丶的会话里，她就会把对方叫成"爸"（2026-11-08 实锤：
        # 心情日志把四条丶写成了"爸突然问她主人是谁"）。
        # 这里只替换这一处措辞，人格的其余部分一个字不动。
        _addr = str(getattr(self.soul, "address", "") or "").strip()
        _peer = self._peer_name_parts(chat_id, chat_name, sender_name)
        if _addr and _peer and _peer != _addr:
            _persona_text = _persona_text.replace(
                f"称呼对方「{_addr}」", f"称呼对方「{_peer}」")
        # ⚠ 三段都带**段名**。快照面板要靠它回答「哪一段吃掉了预算」——
        #   以前这里只存 (座位, 正文)，写快照时标题被填成空串，
        #   于是「她这轮看到了什么」只能看到一个总字数。
        _sections = [(860, "人格", _persona_text)]
        if ctx.mood and ctx.mood.injected_text.strip():
            _sections.append((900, "这会儿的状态",
                              "\n## 这会儿的状态\n\n"
                              + ctx.mood.injected_text.strip()))

        # ── 此刻的表达预算（p0-2）──
        #
        # 心情和精力以前只进提示词当"背景"。于是她说"心情 38"，
        # 回你的话还是四条、每段 40 字 —— 状态被描述了，没被使用。
        #
        # 这里把它翻成四个数（条数 / 字数 / 不接许可 / 强制回阈值），
        # 再叠上余韵的修正：还堵着的时候，话更短、更容易不接。
        #
        # 只改预算，不改内容 —— 说什么仍然由她自己决定。
        # 代码永远不替她掷"不回"的骰子：不回必须是她输出 `-` 的选择，
        # 否则用户看到的是"她明明想了却没发"，那是 bug 的味道。
        _turn_policy = None
        try:
            from bigfish import policy as _policy

            _mood_v = getattr(ctx.mood, "value", None) if ctx.mood else None
            _mood_dims = getattr(ctx.mood, "dims", None) if ctx.mood else None
            _energy = (getattr(self._life, "energy", None)
                       if self._life is not None else None)
            # 跨轮配额：最近 90 秒她已经说了几条、回了几轮。
            # 用户实测过「连发两条 → 6 条」的场景，单轮预算看不出来，
            # 必须把"刚说过话"喂进预算里。
            # 用 getattr：老 entry / 测试替身没有这个方法时按 0 算，
            # 不要让一条配额把整段预算打进 except。
            _rp_fn = getattr(self, "_recent_sent_parts", None)
            _recent_parts, _recent_replies = (
                _rp_fn(chat_id) if callable(_rp_fn) else (0, 0))
            _turn_policy = _policy.response_policy(
                mood=_mood_v, dims=_mood_dims, energy=_energy,
                now=time.time(),
                recent_parts=_recent_parts,
                recent_replies=_recent_replies)

            from bigfish import afterglow as _afterglow

            _turn_policy = _policy.apply_afterglow(
                _turn_policy, _afterglow.current_effect(now=time.time()))
        except Exception as _exc:
            _turn_policy = None
            self._log(f"[预算] 计算失败（按默认走）：{_exc}", "")
        self._policy = _turn_policy
        if _turn_policy is not None:
            try:
                from bigfish import policy as _policy2

                _bits = [_turn_policy.tone_line,
                         _policy2.silence_hint(_turn_policy.silence_bias)]
                _bits = [b for b in _bits if b]
                if _bits:
                    _sections.append(
                        (905, "这一轮的说话方式",
                         "\n## 这一轮的说话方式\n\n" + "\n".join(_bits)))
            except Exception:
                pass

        try:
            from bigfish import plugins as _P

            for _order, _title, _text in _P.prompt_sections({
                "now": datetime.now(),
                "channel": "group" if is_group else "private",
                "chat_name": chat_name,
                "sender_name": sender_name,
                "user_text": incoming or "",
                "soul": self.soul,
                # v0.8：身体那一段（871）要拿她自己此刻的状态。
                # 传内存里那份而不是让插件读盘 —— 引擎刚 tick 过，
                # 磁盘上那份最多晚 30 秒，会出现"说自己醒着但其实刚睡"。
                "life": self._life,
                "tier": "owner" if (owner_raw and is_owner_chat) else "known",
            }):
                _sections.append((_order, _title or "", _text))
        except Exception as _exc:
            # 插件系统坏了不该让她失语
            self._log(f"[插件] 注入失败：{_exc}", "err")

        _sections.sort(key=lambda item: item[0])
        sys_parts = [text for _o, _t, text in _sections]

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
            #   sender_name 装的其实是**wxid**（watcher 用
            #   sender_username() 从分片库的 Name2Id 查出来的），
            #   字段名起错了，但值是对的。拿不到时（shard 为空）
            #   退回群池 + origin 标"未知发言人"，绝不猜成某个人。
            _speaker = (sender_name or "").strip()
            _group_owner = _M.owner_of_group(chat_id)
            _origin = f"group:{chat_id}"
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
            _owner = _M.owner_of_private(chat_id)
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
        # 关系卡（称呼 / 没说出口的话）并进同一段。
        #
        # 它原来是独立的一段（座位 920「关系」）。并过来的理由：
        # 「我叫他老爸」和「他是个嘴硬心软的人」是同一件事的两个侧面 ——
        # 都是"我对这个人的认识"。分成两段注入，模型会把它们当成
        # 两个不同来源的信息，甚至各自成立地互相打架。
        _impression = "\n\n".join(
            x for x in (_profile_block(_owner), _bond_block(_owner))
            if x and x.strip())
        if _impression:
            user_parts.append("## 你印象里的他\n\n" + _impression)

        # ── 主人专用：最近别处提到你的事 ──
        #
        # 原文检索按会话隔离（A2），代价是主人看不到别人在别处怎么提他。
        # 这一节只给 tier=owner，且只挑提到「主人」/他昵称的原文。
        _echo = _owner_echo_block(_owner, _tier, chat_id)
        if _echo:
            user_parts.append("## 最近别处提到你的事（只有你能看到）\n\n" + _echo)

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
                if alt.strip() and alt.strip() != (incoming or "").strip():
                    extra_ctx = load_context(query=alt.strip()[:200], chat=chat_id)
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
        facts = _facts_block(_owner, _tier, question=incoming)
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
        open_said = _open_block(_owner, _tier, incoming)
        if open_said:
            user_parts.append(
                "## 可以告诉别人的事\n\n" + open_said
                + "\n\n（这些是主人同意对外说的。只有对方问起的相关话题才提，"
                  "不要主动一股脑报出来。）")

        # ── 她注意到的（p1-7）──
        #
        # 天气 / 日子 / 你提过但还没到的事。**只读缓存，不联网** ——
        # 这里跑在引擎线程上，一次 4 秒超时就是"她突然不说话了"。
        # 联网在 world 的后台线程里做（每 30 分钟一次）。
        #
        # 放在"你认定的事"之后、"我可以说的"之前：
        # 它和事实同级，都是"背景"，不是"这一轮的任务"。
        try:
            from bigfish import senses as _senses

            _notice = _senses.render(
                now=time.time(),
                facts=(facts or "").split("\n") if facts else None)
            if _notice:
                user_parts.append(_notice)
        except Exception:
            pass

        # 习惯层：说够三次的原话样本（只给样本，不给结论）
        habit_lines = _habits_block(_owner, _tier)
        if habit_lines:
            user_parts.append(
                "## 对方反复说过的事\n\n" + habit_lines
                + "\n\n（这些是原话样本，不是结论。是否成立、属于哪类，"
                  "你按本轮语境自己判断。）")

        try:
            from bigfish import plugins as _pl

            _pl.ensure_loaded()
            _sticker_on = _pl.is_enabled("sticker")
        except Exception:
            _sticker_on = False

        # ── 她睡着期间这个会话有人找过 → 醒来第一句带出来 ──
        #
        # 这是 P0 里唯一一处「她主动开口」的例外，而且**不是主动开口**：
        # 它只在对方已经发过消息、她又醒了的情况下生效，走的完全是
        # 原来那条回复链（同一轮模型调用、同一套发送三态与账本）。
        # 真正的主动消息（没人说话她也发）留给 P1，那条要单独验自环。
        _wake_brief = ""
        try:
            from bigfish import life as _life_mod

            if self._life is not None:
                _wake_brief = _life_mod.briefing(self._life, chat_id,
                                                 time.time())
        except Exception:
            _wake_brief = ""
        if _wake_brief:
            user_parts.append(
                "## 刚看到\n\n" + _wake_brief
                + "\n\n（一句话带过就行，比如「刚看到」「昨晚睡了」。"
                  "别解释为什么没回，别道歉一大段，也别提「睡觉」以外的理由。）")

        scene = (f"你在微信群「{chat_name}」里，"
                 f"{sender_name or '某人'} @了你。" if is_group
                 else f"你在微信上和「{chat_name}」聊天。")
        if proactive_reason:
            # 主动开口：不回应谁，是她自己想说话。
            #
            # 和"回消息"最大的区别是**没有对方那句话当锚点**。所以这里
            # 不提"对方说了什么"，只给一句起因，剩下全靠上面那几段
            # （人格 / 记忆 / 心情 / 生活 / 印象）自己长出话来。
            user_parts.append(
                f"## 这一轮\n\n{scene}\n"
                "没有人在跟你说话。是你自己想先开口。\n"
                f"起因：{proactive_reason}\n\n"
                "要求：\n"
                "- 像真人发微信：短，口语，不用 Markdown，不列清单\n"
                "- **一句话就够**，最多两句。别铺垫，别解释自己为什么突然说话\n"
                "- 不要问「在吗」「忙吗」这种空转的问句 —— 直接说事或者直接说感受\n"
                "- 不要说「想你了」这种没有由头的话，除非起因里真的有料\n"
                "- 不提「系统」「提示词」「记忆」「状态」这些词，也不报自己的数值\n"
                "- 不要复述最近的对话，那是给你当背景的，不是给你念的")
        else:
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
                "- 需要更多信息就一句话追问"
                "\n- **大多数时候正常回**。只有当对方这句实在没什么可接的时候"
                "（比如只回了个「嗯」「哦」），才只输出一个 `-`，表示这一轮不接"
                "\n- 如果说完还觉得有半句没说出口，另起一行写「补：」再接一句"
                "（最多一句、15 字以内；必须是同一件事的延续，"
                "**不能换话题，不能催对方回**）")
            if must_reply:
                # 连着不接太多次的兜底：把 `-` 这条路临时关掉。
                # 不跟她说"你刚才一直没理人"，只说这一次必须回 ——
                # 说多了她会把"必须回"理解成任务。
                user_parts.append(
                    "（这一次**一定要回一句**，不要用 `-`。"
                    "实在没什么可说的就回一个「嗯」或者「怎么了」。）")
        # 表情包：她**确实有**这个能力，但发不发由系统掷骰子决定
        # （见 _pick_sticker）。不告诉她这件事，她就会用「（探头）」
        # 这种括号文字假装发了张图 —— 用户看到的是一个气泡加两个括号。
        if _sticker_on:
            user_parts.append(
                "- 你有发表情包的能力，但**发不发由系统决定**，你控制不了，"
                "也不要在文字里表示自己发了。绝对不要用「（探头）」「（鼓腮）」"
                "这类括号文字冒充表情或动作")
        user_parts.append("## 最近的对话\n\n" +
                          "\n".join(f"{w}: {t}" for w, t in hist))

        t0 = time.time()
        # 快照只记「哪一段占多少字」，不重算内容；插件段有就带上

        return {
            "sys_parts": sys_parts,
            "user_parts": user_parts,
            # 这三个是调用方还要用的：trace 分段、醒来补话、这一轮耗时
            "sections": _sections,
            "wake_brief": _wake_brief,
            "t0": t0,
            "owner": _owner,
            "tier": _tier,
            "origin": _origin,
            "group_owner": _group_owner,
        }

    def _maybe_queue_after(self, *, text: str, main: str, parts: list,
                           chat_id: str, chat_name: str) -> None:
        """把「补的那句」排进延迟队列 —— 但先过六道筛。

        为什么要在本地筛
        ────────────────
        让模型写「补：」本来就会滥用：它不知道什么时候该补，只知道格式上
        可以补。只靠 prompt 约束的结果是"每条回复后面都跟一句"，
        那比不补还假。

        规则是**生成宽松、发送严格**：模型想补就让它写，但只有
        "主回复本身不完整"（短、单条、不是问句）的时候才真的发。
        """
        import random as _rnd

        from bigfish import say as _S

        # 长度兜底：prompt 说 15 字以内，模型可能写 80 字。
        # 补的话就该比主回复更短，超过 40 字那已经不是"补一句"了。
        if len(str(text or "")) > 40:
            self._log(f"[接话] 半句话太长（{len(str(text))} 字），不补", "think")
            return

        ok, why = _S.allow_afterthought(
            main, parts, now=time.time(),
            last_after_at=self._last_after_at,
            recent_count=self._replies_since_after,
            rng=_rnd.Random())
        if not ok:
            self._log(f"[接话] 有半句没说出口，但不补（{why}）", "think")
            return

        now = time.time()
        delay = _rnd.uniform(8.0, 25.0)
        self._after = {
            "text": text,
            "at": now + delay,
            "chat_id": chat_id,
            "chat_name": chat_name,
            "sent_at": now,
            # 用**这个会话**收到消息的序号判断"这期间对方说话没有" ——
            # 比时间戳准，也不受别的会话干扰。
            "peer_seq": int(self._peer_seq.get(chat_id, 0)),
        }
        self._log(f"[接话] 待补一句：{text}（{delay:.0f} 秒后）", "think")

    def _flush_after(self, now: float) -> None:
        """到点了就把「补的那句」发出去 —— 也可能不发。

        四条取消条件，缺一条就会出乱子：

          1. 这期间对方说话了  → 她补的话会和新消息撞车
          2. 她睡着了          → 睡着的消息不该发（和 sleep-gate 一致）
          3. 超过 90 秒        → 语境已经过去，这时候补更像自言自语
          4. UIA 没就绪        → 发不出去，直接丢（不重试，避免延迟错位）
        """
        a = self._after
        if not a:
            return
        if now < float(a.get("at") or 0):
            return

        self._after = {}
        text = str(a.get("text") or "").strip()
        if not text:
            return

        _chat = str(a.get("chat_id") or "")
        if int(a.get("peer_seq", -1)) != int(self._peer_seq.get(_chat, 0)):
            self._log("[接话] 补的那句取消：对方已经说话了", "think")
            return
        if now - float(a.get("sent_at") or 0) > 90.0:
            self._log("[接话] 补的那句取消：拖太久了", "think")
            return
        if self._life is not None:
            try:
                from bigfish import life as _L

                if _L.is_asleep(self._life, now):
                    self._log("[接话] 补的那句取消：她睡了", "think")
                    return
            except Exception:
                pass
        if getattr(self, "dry", False):
            self._log(f"[接话·预演] 本来会补一句：{text}", "think")
            return
        if self.sender is None and not self._ensure_sender():
            self._log("[接话] 补的那句取消：UIA 未就绪", "err")
            return

        from bigfish import sending as _sending

        try:
            verdict = _sending.classify(
                result=self.sender.send(str(a.get("chat_name") or ""), text))
        except Exception as exc:            # noqa: BLE001
            verdict = _sending.classify(error=exc)

        if not verdict.sent:
            self._log(f"[接话] 补的那句没发出去（{'✗' if verdict.failed else '?'}）"
                      f"{verdict.detail}", "err")
            return

        self._last_after_at = now
        self._replies_since_after = 0
        self.reply_count += 1
        # 她多说了话，精力要跟着掉 —— 和正常回复同一个口径，
        # 不然"补一句"是免费的，她会越补越多
        self._talk_turns += 1
        try:
            from bigfish import history as _H4

            # 署名「我」而不是 soul.address —— 见 handle() 里同一处注释
            _H4.append(str(a.get("chat_id") or ""), "我", text)
        except Exception:
            pass
        self._log(f"[接话] 补一句：{text}", "out")

    def _ensure_sender(self, quiet: bool = False) -> bool:
        """确保 UIA 发送通道可用；不可用就当场再试一次。

        ⚠ 启动时失败 ≠ 整晚都失败。

        以前只在 `_run()` 开头试一次，失败就把 `self.sender` 钉死成 None：
        之后每一轮都生成回复、每一轮都「[未发送] UIA 未就绪」——
        用户看到的是"启动成功"，实际一条都没发出去（2026-11-08 现场）。

        而第一次失败的原因往往过几秒就没了：微信还收在托盘、
        微信刚重启、桌面刚解锁、窗口还在重排。所以每轮都给它一次机会。

        `quiet=True` 只给启动那一刻用：外层会自己打一条更完整的说明，
        这里就不要再报一遍原因了。
        """
        if self.sender is not None:
            return True
        try:
            from bigfish.wechat_sender import UiaSender as _UiaSender

            sender = _UiaSender()
            sender.wake()
        except Exception as exc:
            # 限频：60 秒最多说一次，否则每轮回复都刷一行同样的红字
            now = time.time()
            if not quiet and now - float(getattr(self, "_sender_warn_at", 0.0)) >= 60:
                self._sender_warn_at = now
                self._log(f"[警告] UIA 仍未就绪，这一轮发不出去：{exc}", "err")
                try:
                    self.failed.emit(str(exc))
                except Exception:
                    pass
            return False

        self.sender = sender
        self._log("UIA 已就绪（自动恢复，不用重启程序）", "in")
        try:
            self.failed.emit("")      # 清掉界面上的红字
        except Exception:
            pass
        return True

    # ==================================================================
    # 声音：什么时候出声 + 生成音频
    # ==================================================================

    def _voice_settings(self):
        from bigfish.voice import VoiceSettings

        return VoiceSettings.from_config(path=CONFIG_PATH)

    def _voice_state(self, chat_id: str) -> dict:
        today = time.strftime("%Y-%m-%d")
        with self._voice_lock:
            st = self._voice_stats.setdefault(chat_id, {
                "day": today, "count": 0, "last": 0.0, "consecutive": 0})
            if st.get("day") != today:
                st.update(day=today, count=0, consecutive=0)
            return dict(st)

    def _note_voice_sent(self, chat_id: str) -> None:
        today = time.strftime("%Y-%m-%d")
        with self._voice_lock:
            st = self._voice_stats.setdefault(chat_id, {
                "day": today, "count": 0, "last": 0.0, "consecutive": 0})
            if st.get("day") != today:
                st.update(day=today, count=0, consecutive=0)
            st["count"] = int(st.get("count") or 0) + 1
            st["consecutive"] = int(st.get("consecutive") or 0) + 1
            st["last"] = time.time()

    def _note_text_sent(self, chat_id: str) -> None:
        with self._voice_lock:
            st = self._voice_stats.setdefault(chat_id, {
                "day": time.strftime("%Y-%m-%d"), "count": 0,
                "last": 0.0, "consecutive": 0})
            st["consecutive"] = 0

    def _plan_voice(self, msg, body: str, reply: str, parts: list,
                    *, is_group: bool, is_owner: bool) -> dict:
        """判定这一轮要不要出声；要就当场合成（生成阶段，不堵投递线程）。

        返回 {} 表示只发文字。合成失败也返回 {}，文字照发。
        """
        try:
            from bigfish import afterglow as _AG
            from bigfish import plugins as _P
            from bigfish import voice_director as _D
            from bigfish import voice_policy as _VP

            _P.ensure_loaded()
            if not _P.is_enabled("voice"):
                return {}
            settings = self._voice_settings()
            if not settings.api_key or not settings.voice:
                return {}
            policy = _VP.VoicePolicy.from_config(settings.policy)
            if policy.mode == "off":
                return {}

            mood = self._current_mood() or {}
            dims = mood.get("dims") if isinstance(mood.get("dims"), dict) else {}
            life = self._life
            energy = float(getattr(life, "energy", 70) or 70)
            asleep = bool(getattr(life, "asleep", False))
            doing = str(getattr(life, "doing", "") or "")
            try:
                ag_items = _AG.active(_AG.load(), now=time.time())
                ag_kinds = tuple(str(x.get("kind")) for x in ag_items
                                 if x.get("kind"))
            except Exception:
                ag_kinds = ()
            hour = time.localtime().tm_hour
            req = _VP.is_requested(body)
            plan = _D.from_state(
                text=reply, user_text=body,
                mood=float(mood.get("value") or 65), dims=dims,
                life={"energy": energy, "asleep": asleep, "doing": doing},
                hour=hour, is_group=is_group, is_owner=is_owner,
                afterglow=ag_kinds, requested=req,
                profile=settings.profile)
            st = self._voice_state(msg.chat_id)
            decision = _VP.decide(
                text=reply, chat_id=msg.chat_id, parts=parts, policy=policy,
                requested=req, is_group=is_group, is_owner=is_owner,
                scene=plan.scene, last_voice_at=float(st.get("last") or 0),
                today_count=int(st.get("count") or 0),
                consecutive=int(st.get("consecutive") or 0))

            where = f"{msg.chat_name}｜场景 {plan.scene}"
            if decision.preview:
                self._log(f"[声·预演] {'会发语音' if decision.want else '发文字'}"
                          f"：{decision.reason}｜{where}", "think")
                return {}
            if not decision.want:
                # 只记一句，不刷屏；概率未中这种最常见的降噪
                if "未中" not in decision.reason:
                    self._log(f"[声] 文字：{decision.reason}｜{where}", "think")
                return {}

            self._log(f"[声] 准备出声：{decision.reason}｜{where}"
                      f"｜{len(decision.text)}字", "think")
            if decision.send_as == "bubble" and not settings.device:
                self._log("[声] 气泡模式还没选虚拟声卡播放端；这一轮改发文字",
                          "err")
                return {}
            from bigfish.voice import VoiceEngine

            # 每轮按当前 config 新建：用户在声音页改了 Key/模型，
            # 下一条就生效，不用重启。构造只是读路径，不发请求。
            engine = VoiceEngine(settings)
            t0 = time.time()
            res = engine.synthesize_turn(
                [decision.text], mood=float(mood.get("value") or 65),
                dims=dims, energy=energy, asleep=asleep, doing=doing,
                hour=hour, is_group=is_group, is_owner=is_owner,
                afterglow=ag_kinds, requested=req,
                profile=settings.profile)
            if not res.ok:
                self._log(f"[声] 合成失败，改发文字：{res.error}", "err")
                return {}
            # 语音上限 60 秒，留 10 秒容错：超过 50 秒一律改文字。
            if res.duration > 50.0:
                self._log(f"[声] 太长（{res.duration:.1f}s），超过 50 秒；"
                          "这一轮改发文字", "err")
                return {}
            if decision.send_as == "bubble" and res.duration < 1.0:
                self._log(f"[声] 气泡太短（{res.duration:.2f}s），"
                          "微信可能不认；这一轮改发文字", "err")
                return {}
            self._log(f"[声] 好了：{res.summary}｜{os.path.basename(res.path)}",
                      "think")
            return {
                "path": res.path,
                "delivery": "voice_only",
                "send_as": decision.send_as,
                "device": settings.device,
                "hotkey": settings.hotkey,
                "reason": decision.reason,
                "duration": res.duration,
                "ms": int((time.time() - t0) * 1000),
                "fallback": bool(policy.fallback_text),
            }
        except Exception as exc:  # noqa: BLE001 —— 声音坏了不能影响文字
            self._log(f"[声] 判定/合成异常，改发文字：{exc}", "err")
            return {}

    def _send_voice_file(self, msg, path: str) -> bool:
        """音频文件发送。气泡失败时也走这里，避免同一份音频白合成。"""
        from bigfish import sending as _sending
        from bigfish.wechat_sender import classify_media

        try:
            # 文件发送也先把微信顶到前台：语音气泡和文字都不能赌用户
            # 刚好停在微信窗口上。
            from bigfish.wechat_sender import force_show_wechat

            force_show_wechat(wait=0.4)
            kind = classify_media(path)
            r = self.sender.send_media(msg.chat_name, path)
            verdict = _sending.classify(result=r)
        except Exception as exc:  # noqa: BLE001
            kind, verdict = "音频", _sending.classify(error=exc)
        if verdict.sent:
            self._log(f"[声] 已发 {os.path.basename(path)}（{kind}）", "out")
            self._note_voice_sent(msg.chat_id)
            return True
        self._log(f"[声] 文件发送失败：{verdict.detail}", "err")
        return False

    def _send_voice(self, msg, path: str, *, send_as: str = "file",
                    device: str = "", hotkey: str = "Ctrl+Alt"):
        """发音频。返回 (确认/可能发出, 是否已经按过录音键)。

        文件：`send_media` 发 .wav，失败可安全补文字。
        气泡：按住微信录音键 + 播到 CABLE Input；一旦按过键，
        失败就是"可能已经发了一半"，绝不能补文字（whale_girl 的教训）。

        气泡在**按键前**失败（前台、按钮、声卡、快捷键）时，直接把同一份
        WAV 当音频文件发出去 —— 合成已经花过积分，不能再让用户白花一次。
        """
        if self.sender is None and not self._ensure_sender(quiet=True):
            return False, False

        if send_as == "bubble":
            from bigfish.voice_bubble import BubbleError, send_bubble

            try:
                seconds = send_bubble(
                    sender=self.sender, who=msg.chat_name, wav=path,
                    device=device, hotkey=hotkey, log=self._log)
                self._log(f"[声] 语音气泡已录完 {seconds:.2f}s（未确认微信送达）",
                          "out")
                self._note_voice_sent(msg.chat_id)
                return True, True
            except BubbleError as exc:
                self._log(f"[声] 气泡失败：{exc}", "err")
                if exc.started:
                    self._log("[声] 已经按过录音键，结果不确定 —— "
                              "不补发文字，避免重复打扰", "err")
                    return False, True
                # 还没碰录音键：音频是好的，改走文件，别浪费这份合成。
                self._log("[声] 气泡没开始录音，改发同一份音频文件", "")
                return self._send_voice_file(msg, path), False
            except Exception as exc:  # noqa: BLE001
                self._log(f"[声] 气泡异常：{exc}", "err")
                self._log("[声] 改发同一份音频文件", "")
                return self._send_voice_file(msg, path), False

        return self._send_voice_file(msg, path), False


    def _say_plan(self, reply: str, policy=None):
        """按「说法」插件的参数切条 + 算节奏。插件坏了就整段发。

        每轮现读插件配置：面板里改完，下一条就生效，不用重启。

        `policy` 显式传入时用它：主动消息是在后台线程里 `_compose` 的，
        而 `self._policy` 是引擎线程最后一次设置的那份 —— 两个会话
        同时进行时会串味（私聊的预算套到主动消息上）。
        """
        try:
            from bigfish import plugins as _P
            from bigfish.say import PacePolicy, SplitPolicy, plan_pacing, split_reply

            _P.ensure_loaded()
            on = _P.is_enabled("say")
            cfg = _P.config_of("say") if on else {}
            _max_parts = int(cfg.get("max_parts", 4))
            # 状态预算在这里生效：心情/精力/余韵算出来的条数上限，
            # 比面板配得更严时以状态为准 —— 面板配的是"上限的上限"。
            # 关掉说法插件也照样生效：她心情差时话少，
            # 不该取决于用户有没有装一个"分条"插件。
            _pol = policy if policy is not None else getattr(self, "_policy", None)
            if _pol is not None:
                _max_parts = _pol.clamp_parts(_max_parts)
            policy = SplitPolicy(
                max_parts=_max_parts,
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

        if self.sender is None and not self._ensure_sender(quiet=True):
            return
        import os as _os

        name = _os.path.basename(path)
        kind = classify_media(path)
        label = "表情包" if kind == "sticker" else "图片"
        from bigfish import sending as _sending

        try:
            r = self.sender.send_media(msg.chat_name, path)
            verdict = _sending.classify(result=r)
        except Exception as exc:            # noqa: BLE001
            r = None
            verdict = _sending.classify(error=exc)

        if verdict.failed:
            self._log(f"[表情] 未成功：{verdict.detail}", "err")
            return

        if verdict.unknown:
            # 表情的落库比文字慢得多（实测 10~12 秒）。超时**不等于**没发出去 ——
            # 实测就出现过"日志说失败、对方却收到了"。按可能已发出处理：
            # 不重发，并且照常记时间戳，好让读回来的那条被认成回环。
            self._log(f"[表情] 结果未知（{verdict.detail}）—— 可能已经发出去了，"
                      f"按已发处理", "err")

        # 到这里就是"确定发了"或"很可能发了"。两种都要记时间戳，
        # 因为**读回来的那条不会告诉我们当时到底成没成**。
        self._sticker_sent.setdefault(msg.chat_id, []).append(time.time())
        if verdict.sent:
            self._log(f"[表情] 已发 {label}：{name}", "out")
        else:
            self._log(f"[表情] 记为已发（结果未知）：{label} {name}", "out")
        self.chat_event.emit(msg.chat_name, f"[{label}]", "out")
        # 表情**不往文本账本里记**：占位符只有"[表情]"三个字，任何一张
        # 表情都对得上，写进去会把对方发的表情也误杀。
        # 回环改用上面那个时间戳（`_sticker_sent` + `take_sticker_echo`）。
        # 顺手记一笔：她发过表情。以后能拿来做"表达习惯"。
        try:
            from bigfish import vault as _v

            _v.record_sticker_sent(msg.chat_id, name)
        except Exception:
            pass

    def _peer_name(self, msg) -> str:
        """这一轮的「对方」该怎么称呼。

        ⚠ `soul.address` 是**她对主人的称呼**（这里配的是「爸」），
          不是"所有人"的称呼。以前心情判定无条件拿它当 owner_name，
          于是四条丶问一句「你主人是谁」，日志就写成
          「爸突然问她主人是谁」—— 把第三者写成了主人（2026-11-08 用户发现）。

        规则：
          · 群聊 → 说话人的名字（群成员不能共用一个称呼）
          · 主人那条私聊 → 用她平时叫他的那个称呼（「爸」）
          · 其他私聊 → 会话名（「四条丶」）
        """
        return self._peer_name_parts(
            str(getattr(msg, "chat_id", "") or ""),
            str(getattr(msg, "chat_name", "") or ""),
            str(getattr(msg, "sender_name", "") or ""))

    def _peer_name_parts(self, chat_id: str, chat_name: str,
                         sender_name: str = "") -> str:
        """`_peer_name` 的拆开版：`_compose` 手里只有字段，没有 msg 对象。"""
        chat_id = str(chat_id or "")
        chat_name = str(chat_name or "").strip()
        sender = str(sender_name or "").strip()
        addr = str(getattr(self.soul, "address", "") or "").strip()
        if chat_id.endswith("@chatroom"):
            return sender or chat_name or "对方"
        if addr:
            try:
                from bigfish import memory as _M

                if self.owner_id and _M.owner_of_private(chat_id) == self.owner_id:
                    return addr
            except Exception:
                pass
        return chat_name or "对方"

    def _judge_mood_async(self, user_text: str, reply: str, msg) -> None:
        """后台判断心情，不阻塞下一轮对话。

        判断要调一次模型（1~2 秒）。如果同步做，两条消息紧接着来时
        会明显卡顿；而心情是"事后记录"，晚两秒完全没关系。

        ⚠ 用最简单的 threading.Thread 而不是 QThread：
          它不碰 Qt 对象，只做 HTTP + 写文件，跑完就退。
          需要往界面吐日志时通过 _log（signal，跨线程安全）。
        """
        # 心情页的「自动判断」关掉后，这里就不再跟着对话动 ——
        # 心情只会按半衰期回落。以前这个开关没人读，关了照样每轮烧一次模型。
        try:
            from bigfish import vault as _vault_mj

            if _vault_mj.read_mood_config().get("autoJudge") is False:
                return
        except Exception:
            pass

        import threading

        self_name = getattr(self.soul, "name", "") or ""
        owner = self._peer_name(msg)
        chat_id = str(getattr(msg, "chat_id", "") or "")

        def work() -> None:
            try:
                from bigfish import afterglow, mood_judge

                # 判定前先记住此刻。余韵的判据是**真实差值**，
                # 不是模型自报的 moodDelta（那个已经除过 3、还夹过界）。
                before: dict = {}
                try:
                    before = mood_judge.read_mood_state_now() or {}
                except Exception:
                    before = {}

                r = mood_judge.judge_and_apply(
                    user_text=user_text, reply=reply,
                    self_name=self_name, owner_name=owner)
                if not r.get("ok"):
                    self._log(f"[心情] 没判出来：{r.get('error') or '未知'}", "")
                    return
                v = r.get("verdict") or {}
                cur = r.get("mood")
                delta = int(v.get("moodDelta") or 0)
                reason = str(v.get("reason") or "")[:60]
                self._log(
                    f"[心情] {cur if cur is not None else '?'}"
                    f"（判定 {delta:+d}，可信 {v.get('confidence')}）"
                    f" {reason}", "")

                # ── 余韵（p0-3）──
                #
                # 放在这里而不是发送成功时：只有判完心情才有 before/after，
                # 而"她带着这个情绪回你"的时机就是下一轮。
                try:
                    after = mood_judge.read_mood_state_now() or {}
                    b_dims = (before.get("dims")
                              if isinstance(before.get("dims"), dict) else {})
                    a_dims = (after.get("dims")
                              if isinstance(after.get("dims"), dict) else {})
                    dims_delta = {}
                    for k in ("happy", "calm", "sweet", "heartache",
                              "worry", "sad", "jealous"):
                        try:
                            dims_delta[k] = (float(a_dims.get(k) or 0)
                                             - float(b_dims.get(k) or 0))
                        except (TypeError, ValueError):
                            dims_delta[k] = 0.0
                    item = afterglow.from_turn(
                        chat_id=chat_id, user_text=user_text, reply=reply,
                        mood_before=before.get("mood"),
                        mood_after=after.get("mood"),
                        dims_delta=dims_delta,
                        judged_delta=delta / 3.0)
                    if item:
                        self._log(
                            f"[余韵] {item.get('kind')}：{item.get('residue')}",
                            "think")
                except Exception as exc:
                    self._log(f"[余韵] 写入失败：{exc}", "")
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
        # ⚠ 限频要**按会话**，不能一个全局变量。
        #
        #   以前这里是 `self._last_reflect` 一个数：群里聊十句，私聊这
        #   十分钟里的消息就一条都不反思了。而水位是按会话推进的，那些
        #   被跳过的消息随后会被水位跨过去 —— 于是**永久**不会被提炼。
        #
        #   症状：只有最近说话的那个会话有事实，其他会话永远是陌生人。
        #   闸门只是省调用的，它不该改变"哪些消息会被记住"。
        if not isinstance(getattr(self, "_last_reflect_by_chat", None), dict):
            self._last_reflect_by_chat = {}
        _last_map = self._last_reflect_by_chat
        if now - float(_last_map.get(chat_id, 0.0)) < 600:
            return
        _last_map[chat_id] = now
        # 顺手收敛：只留最近 50 个会话，否则这是个只增不减的字典。
        if len(_last_map) > 50:
            for _k in sorted(_last_map, key=lambda k: _last_map[k])[:-50]:
                _last_map.pop(_k, None)

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

                # ── 顺带提炼关系卡（称呼 / 没说出口的话）──
                #
                # 跟着反思一起跑，不单独开线程、不单独限频：
                # 两件事读的是同一批原话，分开跑等于把同一段文本读两遍。
                # 但**只在距上次 6 小时以上**才做 —— 关系不是每轮都变的。
                try:
                    now_b = _t.time()
                    if now_b - float(getattr(self, "_last_bond", 0.0)) >= 6 * 3600:
                        from bigfish import bond as _B
                        from bigfish import history as _H

                        lines = _H.load(chat_id, limit=60)
                        transcript = chr(10).join(f"{w}: {txt}" for w, txt in lines)
                        if transcript.strip():
                            sys_b, user_b = _B.build_request(transcript)
                            # 关系提炼给一倍半预算：推理模型经常把 3000 全花在
                            # reasoning 上、正文为空（2026-11-09 每个会话都撞上）。
                            # `call_model` 遇到 empty_content 还会再扩，这里是起点。
                            _mcfg_b = self.mcfg
                            try:
                                from dataclasses import replace as _replace

                                _mcfg_b = _replace(
                                    self.mcfg,
                                    max_tokens=max(1200, int(self.mcfg.max_tokens * 1.5)))
                            except Exception:
                                pass
                            raw_b = call_model(_mcfg_b, sys_b, user_b)
                            passed, rejected = _B.parse_bonds(raw_b, transcript)
                            added = 0
                            for item in passed:
                                ok, why = _B.upsert_bond(
                                    db, "dafeiyu",
                                    owner or getattr(self, "owner_id", ""),
                                    kind=item["kind"], text=item["text"],
                                    evidence=item["evidence"],
                                    confidence=item["confidence"])
                                if ok and why == "added":
                                    added += 1
                            # ⚠ 计时器必须放在**跑完之后**。
                            #   以前它在 call_model 之前 —— 一次 502 就把
                            #   关系提炼关掉 6 小时，下一次真成功也要等到
                            #   下一个 6 小时窗口，日志还只说"提炼失败"。
                            self._last_bond = now_b
                            if added:
                                self._log(f"[关系] +{added} 条", "")
                            if rejected:
                                self._log(f"[关系] {len(rejected)} 条没有原话依据，已丢弃"
                                          f"（{rejected[0][:40]}）", "")
                except Exception as exc:
                    # 30 分钟后再试。这类失败基本都是上游的（502 / 推理把
                    # max_tokens 吃光导致空正文），不是数据坏了 —— 把基准
                    # 往前挪 6 小时再减 30 分钟，下一次判断自然落在 30 分钟后。
                    self._last_bond = _t.time() - 6 * 3600 + 1800
                    self._log(f"[关系] 提炼失败（30 分钟后再试）：{exc}", "")
            except Exception as exc:
                self._log(f"[记] 反思异常：{exc}", "err")

        threading.Thread(target=work, daemon=True,
                         name="reflect").start()

    # ------------------------------------------------------------------
    def _drain_catchup(self, now: float, enqueue) -> None:
        """到点的补回消息，一条一条交给正常回复链。

        `enqueue` 由调用方传进来 —— 正常路径上传的是"丢进生成队列"，
        **不是直接生成**。这一点很要命：主循环每 300ms 一轮，还管着主动
        消息的发送和生活 tick；在这里同步调模型会把它按住 1~16 秒，
        别的会话、主动开口全跟着停。

        为什么走同一条生成链而不是自己拼 prompt：记忆、心情、刚醒的状态、
        发送三态与账本全都复用，不会长出第二套。
        """
        if not self._catchup:
            return
        from bigfish.watcher import Incoming

        left = []
        for due_at, chat_id, chat_name, text in self._catchup:
            if now < due_at:
                left.append((due_at, chat_id, chat_name, text))
                continue
            if self._stop:
                return
            # 没正文就不补：handle 里 `if not text: return`，但那样队列会被
            # 静默吃掉一条，日志也看不出来。这里直接跳过，留个记录。
            if not str(text or "").strip():
                continue
            try:
                msg = Incoming(
                    chat_id=chat_id,
                    chat_name=chat_name or chat_id,
                    text=text,
                    msg_type="文本",
                    sender_name="",
                    # timestamp 是**秒**（微信 create_time），`_source_ms` 会转
                    timestamp=int(time.time()),
                    identity="peer",
                    identity_rule="wake_catchup",
                    raw_content=text,
                    replayed=True,
                )
                self._log(f"[生活] 醒来补回：{chat_name or chat_id}"
                          f"（睡着时发的）", "in")
                enqueue(msg)
            except Exception as exc:
                self._log(f"[生活] 补回失败：{exc}", "err")
        self._catchup = left

    # ------------------------------------------------------------------
    def _advance_life(self, now: float, *, talk_turns: int = 0) -> list:
        """推进她的生活状态。**不调模型、不读微信库、不发消息。**

        这是「她会在没有消息的时候继续过日子」的唯一入口。
        没有它，程序里除了收消息就没有别的动作 —— 她的全部时间都发生在
        「对方说完话之后」，那不是一个人，是一个回声。

        失败一律吞掉并记一行：生活推进坏了不该让她失语。
        """
        try:
            from bigfish import life as _L

            if self._life is None:
                self._life = _L.load()
            events = _L.tick(self._life, now, talk_turns=talk_turns)
            if events:
                _L.save(self._life)
            for ev in events:
                # 换手头的事不值得刷屏；其余（睡/醒/困）都留一行
                self._log(f"[生活] {ev.text}",
                          "" if ev.kind == "doing" else "think")
            if any(e.kind == "woke" for e in events):
                # ── 睡醒补回 ──
                #
                # 以前这里只打一行日志「下次说话时会带出来」—— 也就是说
                # 对方不发新消息，她永远不会回那句。用户 2026-11-09 明确要求：
                # 「我不希望是这种，而是睡醒了看到没回的消息主动回复」。
                #
                # 做法：把 pending 会话排进 self._catchup，主循环到点后
                # 重新走一遍正常回复链（同一轮模型调用、同一套发送三态与账本），
                # 提示词里的「## 刚看到」段会告诉她"睡着期间他找过你"。
                # 延迟 20 秒：刚睁眼就秒回不像人，也免得跟"刚醒"那段抢话。
                pending = self._life.pending or {}
                if pending:
                    queued = []
                    for _cid, _it in list(pending.items()):
                        _it = _it if isinstance(_it, dict) else {}
                        _txt = str(_it.get("text") or _it.get("preview") or "").strip()
                        if not _cid or not _txt:
                            continue
                        queued.append((now + CATCHUP_DELAY, _cid,
                                       str(_it.get("chat_name") or ""), _txt))
                    if queued:
                        self._catchup = queued
                        self._log(
                            f"[生活] 睡着期间有 {len(queued)} 个会话找过她，"
                            f"醒来会主动回一条（{CATCHUP_DELAY:.0f} 秒后）", "")
            _L.expire_pending(self._life, now)
            return events
        except Exception as exc:
            self._log(f"[生活] 推进失败：{exc}", "err")
            return []

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
        from bigfish.loopguard import LoopGuard
        from bigfish.watcher import MessageWatcher
        from bigfish.wechat import WeChatReader
        from bigfish.wechat_sender import SendError, UiaSender

        self._log("正在加载灵魂 ...")
        self.soul = load_soul()
        self._log(f"灵魂：{self.soul.name}（rev {self.soul.revision}）", "in")

        self.mcfg = ModelConfig.load()
        self._log(f"模型：{self.mcfg.preset.name} / {self.mcfg.eff_model}", "in")

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
        # 身份闸（A-1）：拿不到自己的 wxid 就不启动。
        # 那种状态下所有归属判断都失效，任何自环防护都从源头失效。
        from bigfish import identity as _id_gate
        _id_gate.require_me(_id_gate.self_id_of(self.reader), where="desktop")
        info = self.reader.self_info()
        self._log(f"账号：{info.get('nick_name')}", "in")

        _root_cfg = load_json_config()
        cfg = _root_cfg.get("auto_reply") or {}
        interval = float(cfg.get("interval", 1.2))

        # ── 作用域（C-1）──
        #
        # 五个行为字段按 会话 → 账号 → 全局 回退，旧配置照旧能读
        # （scopes 的 legacy 映射）。线上那份 `allow_self: true` 不会再
        # 泄漏到私聊：它只落到 filehelper 一个会话。
        #
        # 每轮重读：这里只是把"怎么算"定下来，真正的值在每条消息到达时算。
        from bigfish import scopes as _scopes
        _bot_id = str(getattr(self.reader, "wxid", "") or "") or str(
            cfg.get("account") or "")
        try:
            # 昵称 → id；套一层缓存，避免每条消息都查几遍微信库
            _resolver = _scopes.memo_resolver(self.reader.resolve)
        except Exception:
            _resolver = None

        def scope_of(key, chat_id=""):
            return _scopes.get(_root_cfg, key, bot_id=_bot_id or None,
                               chat_id=str(chat_id or "") or None,
                               resolver=_resolver)

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
            # 监听和生成仍然有价值，只是暂时发不出去。
            # ⚠ 但也**不能一次失败定终身** —— 后面每一轮都会再试
            #   （见 _ensure_sender），微信窗口打开后自己就恢复了。
            if not self._ensure_sender(quiet=True):
                self._log(
                    "[警告] 发送通道不可用：现在只监听、不发送。"
                    "把微信窗口打开（或从托盘点出来）后会自动恢复，"
                    "不需要重启程序。", "err")
                try:
                    self.failed.emit(
                        "UIA 未就绪：现在只监听、不发送。"
                        "打开微信窗口后会自动恢复。")
                except Exception:
                    pass

        from bigfish.bridge_client import load_context

        convs: dict[str, list] = {}
        # ── 自环防护 ──
        #
        # 2026-11-04 线上事故后抽成独立模块（bigfish/loopguard.py）。
        #
        # 当时她跟「老爸」私聊里连续两分钟自问自答十几条，只能杀进程。
        # 四个原因叠在一起：
        #   ① allow_self=True 留在生产配置里 —— 那是文件传输助手自测开关，
        #      但它是全局的，于是老爸的会话也变成"我自己发的也算数"。
        #   ② 记账晚于写库：UIA 回车 → 微信异步写库 → watcher 1.2s 轮询，
        #      记账和读回谁先谁后没有保证。漏一次，这条就被当成对方说的。
        #   ③ 没有轮级熔断，漏一次就进入正反馈，雪崩。
        #   ④ 防护全埋在 _run() 的闭包里，4500 行的函数，写不了测试 ——
        #      所以这次事故没有任何用例能提前发现。
        #
        # 现在四层闸门，任何一层单独失手都不成环：
        #   L0 静音     熔断后该会话完全不处理
        #   L1 白名单   allow_self 只对 filehelper 生效
        #   L2 记账     发送前先记、发失败回滚（消灭竞态窗口）
        #   L3 熔断     连续 4 轮自动回复且中间无对方消息 → 静音 120s
        #
        # 对方发来的消息 is_self 恒为 False，永远不会被这四层误伤。
        # 共同账本（D-1）：拦截、发送确认、否决判据都看这一份
        from bigfish import sending as _sending
        from bigfish.ledger import Ledger
        _ledger = Ledger()
        guard = LoopGuard(allow_self_for=lambda chat: bool(
            scope_of("allow_self", chat)), ledger=_ledger)
        remember_sent = guard.note_sent        # 旧名字，表情包路径还在用
        forget_sent = guard.rollback_sent
        # 主动开口也走这一条账：它发出去的消息会被自己读回来，
        # 没有记账就等于给自环开了一个新口子。
        self._guard = guard
        self._ledger = _ledger

        # 第二层：单会话连发上限。
        #
        # ⚠ 这一层也踩过：早期是 90 秒内 4 条就熔断，
        #   而正常聊天里连着说四五句太平常了（问一句、答一句、再追问）。
        #   实测正常对话被硬生生截断。
        #
        #   现在的判据是"**我们自己主动连发**"：只统计引擎自动发的，
        #   而且要密集到不正常（30 秒 6 条）。人聊天的节奏不会这样。
        last_reply_at: dict = {}        # chat_id -> [时间戳...]
        #: 她刚说完话的时刻（monotonic），给倾听层做「回复后冷却」用。
        #   ⚠ 不能用 last_reply_at：那是 time.time()，而 batcher 全程用
        #   time.monotonic()，两种时钟混着比会得出负数。
        _reply_mono: dict = {}
        # 昵称缓存：chat_id -> 上次记下的名字。
        # 微信里改备注/昵称之后，wxid 不变但名字变了 —— 每次来消息顺手
        # 比对一次，变了才写库（不然每条消息都要开一次 sqlite，聊天密了会拖）。
        _name_seen: dict = {}
        BURST_WINDOW = 30.0
        BURST_MAX = 6

        def _trace_identity(msg, who, is_group, me, decision=None, *,
                            force=False) -> None:
            """把"拿不准"的判定写进证据链（旁路，失败静默）。"""
            try:
                from bigfish import identity as _ident, identity_trace

                identity_trace.record(
                    who,
                    # 同上：Decision 的真值是 proceed，proceed=False 不等于没给
                    decision if decision is not None
                    else _ident.admit(who, is_group=is_group),
                    chat_id=msg.chat_id, is_group=is_group,
                    sender=msg.sender_name, me=me,
                    text=getattr(msg, "raw_content", "") or msg.text,
                    force=force)
            except Exception:
                pass

        def handle(msg) -> "dict | None":
            nonlocal gate          # 群聊触发器在 run 里建，这里只改引用
            if self._stop:
                return None

            # 这一轮的计时起点。生成有硬预算：模型卡住 / 挑图太慢时，
            # 宁可丢掉这一轮，也不要十分钟后突然冒出一句三天前的话。
            _t_turn0 = time.time()
            _t_recv = float(getattr(msg, "timestamp", 0) or 0) or _t_turn0
            _turn_timeout = float(scope_of("turn_timeout", msg.chat_id) or 90.0)

            text = (msg.text or "").strip()
            if not text:
                return

            now_ts = time.time()

            # ⚠ 收到就记，**在这里**，不是等到拼 prompt 的时候。
            #
            # 「补一句」要判断"这期间这个会话有没有新消息"。记晚了的话，
            # 被 gate 拦住的那条（她睡着、守卫拦下）不算数 —— 于是她醒来
            # 补的那句没有被你早上的消息取消。
            #
            # 按会话计数，不用全局 recv_count：群里的消息不该影响私聊里的话。
            self._peer_seq[msg.chat_id] = int(self._peer_seq.get(msg.chat_id, 0)) + 1

            # ── 归属判定（A-1）：这条到底是谁说的 ──
            #
            # 11-04 之前这里只有 LoopGuard 的 L1，而 L1 读的是 `msg.is_self`，
            # 那个 bool 表达不了"我其实不知道"。sender 解析不出时它是 False，
            # 于是"拿不准"被当成"对方说的"，直接进入回复。
            #
            # 现在三态：私聊拿不准就放弃（保守）；群里拿不准要等闸门判定
            # "叫没叫她"（宽松但有门槛，在下面 group 分支里处理）。
            from bigfish import identity as _identity

            _me = _identity.self_id_of(self.reader)
            _who = _identity.decide(
                msg.sender_name, _me,
                text=getattr(msg, "raw_content", "") or msg.text,
                chat_id=msg.chat_id,
                is_group=str(msg.chat_id).endswith("@chatroom"))
            _is_group_msg = str(msg.chat_id).endswith("@chatroom")

            # 表情的回环（D-1 补口）：表情在库里没有文本可比 —— 读回来只会
            # 渲染成 "[表情]"，判据说"这是自己发的"，L2 的文本比对认不出来。
            # 于是她发一张表情、读回来、当成对方发来的、再回一句（线上发生过）。
            # 这里改用时间戳：刚发过一张，就吃掉一个戳。
            if (_who.who == _identity.WHO_SELF
                    and text in STICKER_MARKS
                    and take_sticker_echo(self._sticker_sent, msg.chat_id,
                                          time.time())):
                self._log(f"[回环] {msg.chat_name}：读到自己刚发的表情，跳过",
                          "")
                return

            # 账本能否决判据（D-1）：判据说"对方"，但我一秒前正好发过这句。
            # 这是 11-04 那晚唯一缺的一环。
            if _who.who == _identity.WHO_PEER:
                _hit = _ledger.lookup(msg.chat_id,
                                      getattr(msg, "raw_content", "") or msg.text)
                if _hit is not None:
                    _ago = max(0.0, time.time() - _hit.at)
                    self._log(f"[否决] {msg.chat_name}：判据说对方说的，"
                              f"但账本里 {_ago:.0f} 秒前发过同一句，按自己处理", "")
                    _vetoed = _identity.Verdict(
                        _identity.WHO_SELF, "ledger_veto",
                        _who.evidence + (("ledger", _hit.text[:24]),),
                        f"账本里 {_ago:.0f} 秒前发过这句")
                    _trace_identity(
                        msg, _vetoed, _is_group_msg, _me, force=True,
                        decision=_identity.Decision(
                            False, "ledger_veto",
                            f"账本里 {_ago:.0f} 秒前发过同一句"))
                    return

            if _who.who == _identity.WHO_UNKNOWN and not _is_group_msg:
                self._log(f"[跳过] {msg.chat_name}：拿不准是谁发的"
                          f"（{_who.rule}），不回", "")
                _trace_identity(msg, _who, False, _me)
                return

            # ── 闸门 L0~L2：静音 / 白名单 / 记账比对 ──
            # 全在 LoopGuard 里，一行调用。返回值带原因码，直接进日志。
            verdict = guard.admit(msg)
            if not verdict.allow:
                self._log(f"[回环] {msg.chat_name}：{verdict.detail}"
                          f"（{msg.preview(30)}）", "")
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

            from bigfish.app import _quiet_now

            if _quiet_now(scope_of("quiet_hours", msg.chat_id),
                          time.localtime()):
                self._log(f"[安静时段] 跳过 {msg.chat_name}", "err")
                return

            self.recv_count += 1
            self.stats.emit(self.recv_count, self.reply_count)

            hist = convs.get(msg.chat_id)
            if hist is None:
                # 重启之后她还记得刚才聊到哪 —— 靠这份 jsonl，
                # 不是靠记忆库检索（那是"记得哪些事"，不是"上一句说了什么"）。
                try:
                    from bigfish import history as _H1

                    hist = list(_H1.load(msg.chat_id))
                except Exception:
                    hist = []
                convs[msg.chat_id] = hist
            is_group = msg.chat_id.endswith("@chatroom")
            who = (msg.sender_name if is_group else msg.chat_name) or "对方"

            # ---- 群聊触发判定 ----
            body = msg.text
            if is_group:
                from bigfish.group_gate import GroupGate

                # 群聊两个字段也按会话取；enable 以前只在构造时设置一次，
                # 改了配置不重开程序不生效 —— 顺手修掉。
                _allow_all = tuple(scope_of("group_allow_all", msg.chat_id) or ())
                _enable = bool(scope_of("group_reply", msg.chat_id))
                if gate is None:
                    gate = GroupGate(self.reader._db,
                                     self.reader._db.wxid,
                                     enable=_enable,
                                     allow_all=msg.chat_name in _allow_all
                                     or msg.chat_id in _allow_all)
                else:
                    gate.enable = _enable
                    gate.allow_all = (msg.chat_name in _allow_all
                                      or msg.chat_id in _allow_all)
                t = gate.check(msg.chat_id, msg.text, True)
                if not t.respond:
                    self._log(f"[不接] {msg.chat_name} / "
                              f"{msg.sender_name}: {msg.preview(36)}"
                              f"（{t.reason}）", "")
                    return
                # 群里拿不准时，只有真的叫她（@ / 叫名字）才当对方处理。
                # 免检群（group-allow-all）不算 —— 那不是"有人叫她"。
                if (_who.who == _identity.WHO_UNKNOWN
                        and not _identity.woken_from_reason(t.reason)):
                    self._log(f"[跳过] {msg.chat_name}：拿不准是谁发的，"
                              f"也没叫她（{t.reason}）", "")
                    _trace_identity(msg, _who, True, _me)
                    return
                if _who.who == _identity.WHO_UNKNOWN:
                    self._log(f"[拿不准] {msg.chat_name}：但被叫到了，"
                              f"按对方处理（{_who.rule}）", "")
                    _trace_identity(msg, _who, True, _me)
                body = t.clean_text
                self._log(f"[触发] {msg.chat_name} / "
                          f"{msg.sender_name}: {t.reason}", "in")

            # 对方道歉 / 示好 → 余韵软消（p0-3）。
            # 放在入历史之前：这一轮她自己就该感觉到"他哄了一句"，
            # 否则要等下一轮才软下来，节奏会慢半拍。
            try:
                from bigfish import afterglow as _ag_repair

                if _ag_repair.note_repair(body or text, now=time.time()):
                    self._log("[余韵] 他哄了一句，气消了一半", "think")
            except Exception:
                pass

            hist.append((who, body))
            if len(hist) > 16:
                del hist[:-16]
            # 对方说话了 → 主动消息的"待追问"可以清掉了。
            # 不清的话，她会在对方已经回来之后又追问一次"在忙？"。
            self._last_peer_chat = msg.chat_id
            if self._world is not None:
                try:
                    self._world.note_peer(time.time())
                except Exception:
                    pass
            # 落盘一份：进程重启/崩溃之后，"最近聊了什么"不会归零。
            # 失败只影响这句记不住，不影响这一轮。
            try:
                from bigfish import history as _H2

                _H2.append(msg.chat_id, who, body)
            except Exception:
                pass

            # 用户说过的话**不依赖这一轮回没回成功**。
            # 以前只在发送成功后调 record_exchange，于是「她没接上」的那句
            # 在记忆页里根本不存在 —— 用户质问「你怎么没回我」时，
            # 她查不到任何依据，只能编。
            #
            # ⚠ 补回的消息（replayed）不要重复写：她睡着时那条已经进过库，
            #   再写一次会让记忆页里同一句话出现两遍，而且 local_id 不同，
            #   UNIQUE 挡不住。
            _replayed = bool(getattr(msg, "replayed", False))
            if not _replayed:
                try:
                    from bigfish import vault as _v_user

                    _lid = getattr(msg, "local_id", None)
                    _v_user.capture_message(
                        f"wechat:{msg.chat_id}",
                        f"wx-u-{_lid}" if _lid else f"wx-u-{int(time.time() * 1000)}",
                        "user", body, _source_ms(msg))
                except Exception:
                    pass

            # 日志不在这里打 —— 即时那行已经由 enqueue() 在消息到手的瞬间
            # 发过了。这里再打一次等于同一句话在日志里出现两遍，
            # 而中间那 2 秒的空窗正是用户抱怨「卡一会」的来源。
            # 补回的消息也不上屏（它当时已经出现过一次）。
            if not _replayed:
                self.chat_event.emit(msg.chat_name, msg.preview(60), "in")

            # 昵称跟着微信走：wxid 是稳定的，昵称/备注会改。
            # 记下当前名字，印象页和记忆页显示的就是最新的那个。
            # 只在变化时写库（见 _name_seen 的注释）。
            try:
                _nick = (msg.chat_name or "").strip()
                if (_nick and _nick != msg.chat_id
                        and _name_seen.get(msg.chat_id) != _nick):
                    from bigfish import memory as _Mn, vault as _Vn

                    _dbn = _Vn.memory_db_path()
                    if _dbn:
                        _is_grp = msg.chat_id.endswith("@chatroom")
                        _ownn = (_Mn.owner_of_group(msg.chat_id) if _is_grp
                                 else _Mn.owner_of_private(msg.chat_id))
                        _Mn.ensure_owner(_dbn, "dafeiyu", _ownn,
                                         display_name=_nick)
                        _name_seen[msg.chat_id] = _nick
            except Exception:
                pass

            # ── 她还醒着的话，这一条让她多撑一会儿 ──
            #
            # 只在"接近或已过入睡点"才生效（白天聊多久都不算熬夜），
            # 一晚总延长有硬上限 —— 她不是不会累的服务。
            try:
                from bigfish import life as _life_mod

                if self._life is not None and _life_mod.try_extend(self._life,
                                                                   time.time()):
                    self._log(f"[生活] 她本来该睡了，还撑着"
                              f"（今晚已延 {self._life.extended_minutes:.0f} 分钟）",
                              "think")
                    _life_mod.save(self._life)
            except Exception:
                pass

            # ── 插件判定（gate 类）：该不该回 ──
            #
            # 放在这里而不是更早：此时 body 已确定（群里已剥掉 @）、hist 已入列，
            # 判定插件能拿到完整上下文；而"收"已经记过账 —— 她确实收到了，
            # 只是选择不说。这两种事在日志里必须分得开。
            #
            # v0.8：判定现在能拿到 `life`（她自己的生活状态）与 `tier`。
            # `tier` 在这里提前算，是因为「睡觉不回」默认只对主人生效 ——
            # 睡着时被陌生人叫醒，比不理人更不像人。
            is_owner_chat = (
                (msg.sender_name == owner_raw) if is_group
                else (msg.chat_id == owner_raw))
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
                    "life": self._life,
                    "tier": "owner" if (owner_raw and is_owner_chat) else "known",
                })
            except Exception as _exc:
                _silent, _why = False, ""
                self._log(f"[插件] 判定失败：{_exc}", "err")
            if _silent:
                # 她睡着的沉默要记账，否则醒来就说不出「我睡着的时候你找过我」。
                # 别的沉默原因（守卫、人设不接话）不记 —— 那不是"没看到"。
                try:
                    from bigfish import life as _life_mod

                    if (self._life is not None
                            and _life_mod.is_asleep(self._life, time.time())):
                        _life_mod.note_pending(self._life, msg.chat_id,
                                               body or "", time.time(),
                                               chat_name=msg.chat_name)
                        _life_mod.save(self._life)
                        self._log(
                            f"[生活] {msg.chat_name} 在她睡着时发了"
                            f" {(_life_mod.peek_pending(self._life, msg.chat_id) or {}).get('count', 1)} 条，"
                            f"记着，醒来补", "")
                except Exception:
                    pass
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
            # 连着两轮"不值得接"之后，第三次强制回 ——
            # 不然你连着说三句她一声不吭，和程序坏了没区别。
            _policy_now = getattr(self, "_policy", None)
            _force_after = (_policy_now.must_reply_after
                            if _policy_now is not None else 2)
            _must_reply = self._silent_streak >= _force_after
            _turn = self._compose(
                chat_id=msg.chat_id, chat_name=msg.chat_name,
                sender_name=msg.sender_name or "", incoming=body or "",
                is_group=is_group, hist=hist,
                owner_raw=owner_raw, is_owner_chat=is_owner_chat,
                owner_id=owner_id, must_reply=_must_reply)
            sys_parts = _turn["sys_parts"]
            user_parts = _turn["user_parts"]
            _sections = _turn["sections"]
            _wake_brief = _turn["wake_brief"]
            t0 = _turn["t0"]
            _owner = _turn["owner"]
            _tier = _turn["tier"]
            _origin = _turn["origin"]
            _group_owner = _turn["group_owner"]
            try:
                # (座位, 段名, 字数)。旧格式是二元组，两种都认。
                _trace_sections = []
                for _it in _sections:
                    if len(_it) >= 3:
                        _o, _n, _t = _it[0], _it[1], _it[2]
                    else:
                        _o, _n, _t = _it[0], "", _it[1]
                    _trace_sections.append((int(_o), str(_n or ""), len(_t)))
            except Exception:
                _trace_sections = []
            try:
                reply = call_model(
                    self.mcfg,
                    "\n".join(sys_parts).strip(),
                    "\n\n".join(user_parts),
                    trace_meta={"chat": msg.chat_id, "owner": _owner,
                                "sections": _trace_sections},
                )
            except ModelError as exc:
                self._log(f"[生成失败] {exc}", "err")
                self.failed.emit(str(exc))
                return
            dt = time.time() - t0
            # ── 生成预算 ──
            # call_model 自己会超时重试，但"重试三次 + 挑图"叠起来可能几分钟。
            # 超过预算的这一轮直接丢掉：十分钟后冒出一句旧话，比不回更糟。
            _spent = time.time() - _t_turn0
            if _spent > _turn_timeout:
                self._log(
                    f"[超时] 这一轮生成用了 {_spent:.0f}s（上限 {_turn_timeout:.0f}s），"
                    f"丢弃 —— 迟到的回复比不回复更奇怪", "err")
                try:
                    from bigfish import turns as _turns_t

                    _turns_t.record(chat=msg.chat_id, chat_name=msg.chat_name,
                                    outcome="timeout",
                                    wait_ms=int((_t_turn0 - _t_recv) * 1000),
                                    gen_ms=int(_spent * 1000))
                except Exception:
                    pass
                return None

            reply = (reply or "").strip()
            if not reply:
                self._log("[生成失败] 空回复", "err")
                return

            # ── 这一句接不接 ──
            #
            # 真人看到「嗯」不一定接。硬接的结果是"嗯嗯你也是" ——
            # 那是整个系统最像机器人的地方之一。
            #
            # 群聊不参与：@ 你的话不接等于没听见，看起来像挂了。
            from bigfish import say as _saymod

            _after_text = ""
            if is_group:
                _main = reply
            else:
                _main, _after_text = _saymod.split_afterthought(reply)

            # ⚠ 群聊不参与：@ 你的话不接等于没听见，看起来像挂了。
            #   注释以前就写着这句，但代码没排 —— 群里模型输出 `-` 一样沉默。
            if (not is_group) and _saymod.is_silent(_main):
                if not _must_reply:
                    self._silent_streak += 1
                    self._log(
                        f"[接话] 不接（连着第 {self._silent_streak} 次）："
                        f"对方这句没内容可接", "think")
                    try:
                        from bigfish import turns as _turns_s

                        _turns_s.record(chat=msg.chat_id, chat_name=msg.chat_name,
                                        outcome="silent",
                                        wait_ms=int((_t_turn0 - _t_recv) * 1000),
                                        gen_ms=int((time.time() - _t_turn0) * 1000))
                    except Exception:
                        pass
                    return None
                # 说了"必须回"还是给了 - ：给一个最简反应，别让这一轮空着
                _main = "嗯"
            if not is_group:
                # 群聊不该重置私聊里的"连续没接"计数 —— 两件事无关。
                self._silent_streak = 0
            reply = _main

            # 硬上限兜底。policy 的字数是"提示词里的目标"，模型不配合时
            # 这里再兜一层；但下限给 18 字 —— 14 字的目标配硬切会把一句话
            # 切成半句，那不像人，像被截断的日志。
            _max_chars = int(scope_of("max_reply_chars", msg.chat_id) or 0)
            _policy_now = getattr(self, "_policy", None)
            if _policy_now is not None:
                _max_chars = min(_max_chars or 10 ** 6,
                                 max(18, _policy_now.max_chars))
            if _max_chars > 0 and len(reply.replace("{{split}}", "")) > _max_chars:
                reply = _cut_at_sentence(reply, _max_chars)

            self._log(f"[想] {dt:.1f}s  {len(reply)}字", "think")

            if self.dry:
                # 预演没有发送环节，直接进历史
                if _after_text:
                    self._log(f"[接话·预演] 本来会补一句：{_after_text}", "think")
                hist.append(("我", reply))
                try:
                    from bigfish import history as _H3

                    _H3.append(msg.chat_id, "我", reply)
                except Exception:
                    pass
                self._log(f"[预演] {reply}", "think")
                return

            _t_plan0 = time.time()
            # 模型可能写 [[voice]] / [[text]] 来表态；它们是给系统的标记，
            # 绝不能出现在对方收到的文字里。但判定层要看到原始标记，
            # 所以先留一份 raw，再清正文。
            _voice_reply = reply
            # 表情标记也在**原始** reply 上读（和语音标记同一处）：
            #   [[sticker]] 要配图 / [[text]] 不配图。
            # 读完立刻被 strip_tags 清掉，绝不发给对方。
            _sticker_mark = ""
            try:
                _low = (reply or "").lower()
                if "[[sticker]]" in _low:
                    _sticker_mark = "sticker"
                elif "[[text]]" in _low:
                    _sticker_mark = "text"
            except Exception:
                pass
            try:
                from bigfish.voice_policy import strip_tags as _strip_voice_tags

                reply = _strip_voice_tags(reply)
            except Exception:
                pass
            parts, gaps = self._say_plan(reply)
            if is_group and msg.sender_name and parts:
                # 群里回复时带上 @，只加第一条 —— 每条都 @ 很吵
                parts[0] = f"@{msg.sender_name} {parts[0]}"

            _t_pick0 = time.time()
            # 声音放在生成阶段：合成 2~4 秒，放在投递线程会堵住整台机器。
            # 判定/合成失败都只回 {}，文字照发。
            _voice = self._plan_voice(
                msg, body or "", _voice_reply, parts,
                is_group=is_group, is_owner=bool(is_owner_chat))
            _sticker_mood = self._current_mood()
            sticker_pos = self._sticker_pos_hint(reply, _sticker_mood)
            sticker_path = ""
            # ⭐ 表情在**生成阶段**挑好，投递阶段只负责发。
            #
            # 以前后置挑图放在"发完文字之后"，而且是串行的：调一次模型
            # 0~40 秒，整条引擎就停在那儿 —— 别的会话全被堵住。
            # 归到生成阶段以后，挑图只耽误这一条进 outbox 的时间。
            if sticker_pos in ("before", "after"):
                # 挑图也要调模型。这一轮已经用掉大半预算时直接跳过 ——
                # 表情是附加品，文字先出去更重要。
                if time.time() - _t_turn0 > _turn_timeout * 0.6:
                    self._log("[表情] 这一轮已经用掉大半预算，跳过挑图", "think")
                    sticker_pos = "none"
                    sticker_path = ""
                else:
                    _t_pick = time.time()
                    try:
                        with _sticker_lock:
                            sticker_path, _pos = self._pick_sticker(
                                msg, body, reply, mood=_sticker_mood,
                                mark=_sticker_mark)
                    except Exception as exc:
                        self._log(f"[表情] 挑选失败：{exc}", "err")
                    _cost = time.time() - _t_pick
                    if _cost >= 3.0:
                        self._log(
                            f"[耗时] {'前置' if sticker_pos == 'before' else '后置'}"
                            f"挑图 {_cost:.1f}s（只耽误这一条，不堵别的会话）",
                            "think")

            # ── 生成完了，交给投递线程 ──
            #
            # 这里不再自己发：生成与投递分开之后，两个会话可以同时"想"，
            # 但终究只有一个窗口能"说"。同一会话的下一批会被 per-chat 锁
            # 挡在这条投递完成之前，保证上下文不打架。
            return {
                "msg": msg, "body": body, "reply": reply,
                "hist": hist,
                "parts": parts, "gaps": gaps,
                "voice": _voice,
                "sticker_pos": sticker_pos, "sticker_path": sticker_path,
                "after_text": _after_text, "wake_brief": _wake_brief,
                "owner": _owner, "tier": _tier,
                "origin": _origin, "group_owner": _group_owner,
                "t_plan0": _t_plan0, "t_pick0": _t_pick0,
                "t_gen0": _t_turn0, "t_gen1": time.time(), "t_recv": _t_recv,
            }

        def _deliver(entry: dict) -> None:
            """把一轮生成结果发出去。**整台机器上唯一碰 UIA 的线程**在跑它。

            ⚠ 发送必须串行：一个微信窗口、一个输入框、一个剪贴板。
              生成可以在两个 worker 里并行，投递不行 —— 两个线程同时写
              会互相踩，发错人。
            """
            msg = entry["msg"]
            body = entry["body"]
            reply = entry["reply"]
            # hist 是 handle 的局部变量，_deliver 是 handle 的**兄弟**闭包，
            # 不能直接引用。显式随 entry 传过来；老 entry 没有就现读。
            hist = entry.get("hist")
            if hist is None:
                try:
                    from bigfish import history as _H4

                    hist = list(_H4.load(msg.chat_id))
                except Exception:
                    hist = []
            parts = entry["parts"]
            gaps = entry["gaps"]
            _after_text = entry.get("after_text") or ""
            _wake_brief = bool(entry.get("wake_brief"))
            _owner = entry.get("owner") or ""
            _tier = entry.get("tier") or ""
            _origin = entry.get("origin") or ""
            _group_owner = entry.get("group_owner") or ""
            _t_plan0 = float(entry.get("t_plan0") or 0.0)
            _t_pick0 = float(entry.get("t_pick0") or 0.0)
            _lock = entry.get("lock")
            # 这一轮的观测字段（写完进 data/turns.jsonl）
            _t_send_start = time.time()
            _gen0 = float(entry.get("t_gen0") or 0.0)
            _gen1 = float(entry.get("t_gen1") or 0.0)
            _recv = float(entry.get("t_recv") or 0.0)
            _fin = {"outcome": "not_sent"}
            _voice = entry.get("voice") or {}
            # 只要这一轮命中语音，就只发语音；旧配置的 text_then_voice
            # 也在这里被强制成 voice_only —— 同一内容不允许发两遍。
            _voice_only = bool(_voice)
            _voice_sent = False
            _voice_started = False
            try:
                if self.sender is None:
                    self._ensure_sender()
                if self.sender is None:
                    # 内容不丢：写进日志，用户把窗口弄好之后能看见她本来想说什么
                    self._log(
                        f"[未发送] UIA 仍未就绪，这条没能发出去：{reply[:60]}",
                        "err")
                    return

                _t_send0 = time.time()
                sent_any = False
                # 这一轮有没有"结果未知"的条。未知**不等于**没发出去 ——
                # 说错一句话的代价，是用户拿着手机里收到的消息来对账，
                # 然后这一整份日志都不再可信。
                _unknown_any = False
                # 只发语音：先把音频发出去。成功就不再发文字；
                # 失败且允许补文字，则 parts 保持原样、走下面的文字循环。
                if _voice_only and _voice.get("path"):
                    _voice_sent, _voice_started = self._send_voice(
                        msg, _voice["path"],
                        send_as=_voice.get("send_as", "file"),
                        device=_voice.get("device", ""),
                        hotkey=_voice.get("hotkey", "Ctrl+Alt"))
                    if _voice_sent:
                        sent_any = True
                        parts = []
                    elif _voice_started:
                        # 已经按过录音键：可能已经收到半截语音，绝不补文字。
                        _unknown_any = True
                        parts = []
                    elif not _voice.get("fallback", True):
                        self._log("[声] 语音没发出，且关了补文字 —— 这一轮只有日志",
                                  "err")
                        parts = []
                try:
                    for _i, _part in enumerate(parts):
                        if _i > 0:
                            # 条间停顿：像在打字，不是一口气刷屏
                            time.sleep(gaps[_i] if _i < len(gaps) else 0.8)
                        # ⭐ 先记账，再发送。
                        #
                        #   以前是发完才 remember_sent —— 而「UIA 按回车」到
                        #   「微信写库」是异步的：行可能先落库，账还没记上，
                        #   watcher 恰好在那一刻轮询，这条就被当成对方说的。
                        #   1.2 秒一轮，单个窗口很窄，但聊一晚上必然撞上；
                        #   撞上一次就进入自问自答（线上真发生了）。
                        #   顺序反过来就没有窗口：库里出现的那一刻，账早就记好了。
                        remember_sent(_part, msg.chat_id)
                        try:
                            _r = self.sender.send(msg.chat_name, _part)
                            _verdict = _sending.classify(result=_r)
                        except Exception as _exc:          # noqa: BLE001
                            _verdict = _sending.classify(error=_exc)

                        if not _verdict.sent:
                            if _verdict.failed:
                                # 确定没发出去，这笔账必须撤 —— 留着会让对方
                                # 原样说一句同样的话时被误判成回环。
                                forget_sent(_part, msg.chat_id)
                            # 未知：账本**保留**（它可能已经发出去了，读回来要认得出），
                            # 而且绝不重发 —— 那会让对方收到两遍。
                            self._log(
                                f"[发] {'✗' if _verdict.failed else '?'} "
                                f"{_verdict.detail}", "err")
                            if _verdict.unknown:
                                _unknown_any = True
                                self._log(
                                    "消息结果未知 —— 可能已经发出去了，所以不会重发。"
                                    "如果对方说没收到，是这一条。", "err")
                            break
                        sent_any = True
                    # 只要这一轮发了文字，就把"连续语音"计数清零；
                    # 只发语音时不 reset，否则连续上限永远不生效。
                    if sent_any and not (_voice_only and _voice_sent):
                        self._note_text_sent(msg.chat_id)
                    if sent_any:
                        _fin["outcome"] = "sent"
                        self._last_reply_at = time.time()
                        _reply_mono[msg.chat_id] = time.monotonic()
                        self._replies_since_after += 1
                        if _after_text:
                            self._maybe_queue_after(
                                text=_after_text, main=reply, parts=parts,
                                chat_id=msg.chat_id, chat_name=msg.chat_name)
                        # ⚠ 只有真的发出去了才写进对话历史。
                        #   以前这行在发送之前，于是「打开会话失败」那轮也被记成
                        #   「我说过」，下一轮 prompt 里带着一句她从没发出去的话 ——
                        #   用户看到的"幻觉"就是这么来的。
                        # 记忆里存的是「她真正说出口的话」：分条标记和换行
                        # 不该留在正文里 —— 下一轮 prompt 会把它们原样带回去。
                        import re as _re
                        _clean_reply = _re.sub(r"\s+", " ",
                                               reply.replace("{{split}}", " ")).strip()
                        hist.append(("我", reply))
                        # 短期历史落盘（重启后"接着刚才说"靠它）。
                        # ⚠ 必须留在 if sent_any 里面：
                        #   放在外面时，发送失败那轮也会被记成"她说过"，
                        #   下一轮 prompt 里带着一句对方从没收到的话。
                        #
                        # ⚠ 署名必须是「我」，不是 soul.address。
                        #   address 是**她对主人的称呼**（配的是「爸」），
                        #   用它署名时 history 里她的每句话都变成「爸: 矮宝？…」，
                        #   下一轮 prompt 把这句话当成**对方说过的**。
                        try:
                            from bigfish import history as _H3

                            _H3.append(msg.chat_id, "我", _clean_reply)
                        except Exception:
                            pass
                        self.reply_count += 1
                        # 聊天也累：累计到下一次 tick，作为精力消耗的一部分
                        self._talk_turns += 1
                        # 「刚看到」那句已经带出去了，pending 用掉 ——
                        # 否则下次说话她会再说一遍"昨晚你发过消息"
                        if _wake_brief:
                            try:
                                from bigfish import life as _life_mod

                                _life_mod.take_pending(self._life, msg.chat_id)
                                _life_mod.save(self._life)
                            except Exception:
                                pass
                        # 整段回复也记一笔（分条时每条已经记过；这里记的是拼起来的样子，
                        # 因为读回来可能被合并成一条）。必须带会话 ——
                        # 不带就会变成"全局账"，别的会话里出现同样文字会被误否决。
                        # 只发语音时没有文字到达对方，不能把这句话记成"发过"，
                        # 否则对方之后原样说一句会被当成回环。
                        if not (_voice_only and _voice_sent):
                            remember_sent(reply, msg.chat_id)
                        last_reply_at.setdefault(msg.chat_id, []).append(time.time())

                        # 闸门 L3：轮级熔断。一轮自动回复 = 一次。
                        # 中间只要夹了一条对方的消息，guard.admit 已经清零了。
                        if guard.note_reply(msg.chat_id):
                            self._log(
                                f"[自环熔断] {msg.chat_name} 连续 {guard.trip} 轮"
                                f"自动回复、中间一条对方消息都没有 —— "
                                f"判定她在自问自答，该会话静音 {int(guard.mute)}s。"
                                f"检查 allow_self 与回环识别。", "err")

                        # 记下这一轮发了几条 —— 下一轮的跨轮配额要用。
                        # 语音只发一条，不能按文字分段数算。
                        _n_sent = (1 if (_voice_only and _voice_sent)
                                   else len([p for p in (parts or [reply]) if p]))
                        self._note_sent_parts(msg.chat_id, _n_sent)
                        self.stats.emit(self.recv_count, self.reply_count)
                        # 用分段后的样子记：{{split}} 留在正文里看不懂，
                        # 而且用户想核对"她到底发了几条、每条什么"。
                        self._log(
                            (f"[发] [语音] {reply[:70]}" if _voice_only and _voice_sent
                             else f"[发] {' / '.join(p for p in (parts or [reply]) if p)[:80]}"),
                            "out")
                        # 耗时拆开看一眼。「聊几句之后突然安静几十秒」这种问题，
                        # 光看 [想]/[发] 之间的空白是查不出来的 —— 必须知道
                        # 是挑图、还是 UIA 发送吃的。超过 3 秒才打，免得刷屏。
                        _seg = (("切条", _t_pick0 - _t_plan0),
                                ("挑图", _t_send0 - _t_pick0),
                                ("发送", time.time() - _t_send0))
                        if max(v for _n, v in _seg) >= 3.0:
                            self._log("[耗时] " + " / ".join(
                                f"{n} {v:.1f}s" for n, v in _seg), "think")
                        self.chat_event.emit(msg.chat_name, reply[:60], "out")

                        # ⭐ 发完文字，再挑表情包。
                        #   失败只记日志，不影响这一轮 —— 对方已经从文字里
                        #   读到内容了，表情是附加品，丢了不算聊崩。
                        # 表情是生成阶段挑好的，这里只发 —— 投递线程不做模型调用
                        if (not (_voice_only and _voice_sent)
                                and entry.get("sticker_pos") == "after"
                                and entry.get("sticker_path")):
                            self._send_sticker(msg, entry["sticker_path"], remember_sent)

                        # ⭐ 把这一问一答写进记忆库。
                        #   以前完全没有这一步 —— 聊一晚上记忆里一条都没有，
                        #   她永远想不起微信里说过什么。这是设计缺口，不是 bug。
                        #   放在"发送成功之后"：没发出去的话不算对话发生过。
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
                                # ⚠ 必须在这里 import `memory`。
                                #   以前写的是 `_M.observe_habit(...)`，而 `_M`
                                #   在这个作用域**根本没有定义** → NameError →
                                #   被下面的 `except Exception: pass` 吞掉。
                                #   于是习惯层从来没有过任何一条观察：
                                #   `habits` 表恒空、score 永不衰减、
                                #   「对方反复说过的事」那一段永远不出现。
                                #   这类 bug 不会报错、不会进日志，只会让一个功能
                                #   安静地不存在 —— 比崩溃难查十倍。
                                from bigfish import memory as _M
                                from bigfish import vault as _v2

                                _M.observe_habit(_v2.memory_db_path(), "dafeiyu",
                                                 _owner, body,
                                                 at=_source_ms(msg))
                        except Exception as exc:
                            self._log(f"[习] 观察失败：{exc}", "")
                    else:
                        # 失败原因在循环里已经逐条记过（`[发] ✗/?` + 记账回滚）。
                        # 这里以前是 `f"[发送失败] {r.detail}"`，而 `r` 在
                        # 这个作用域不存在 —— 第一条就发失败时它会抛 NameError，
                        # 而外层只 `except SendError`，捕获不到，
                        # 会一直冒到 watcher 的线程里。现在只留一句结论。
                        if _unknown_any:
                            # 有"结果未知"的条：它可能已经到了对方手机上。
                            # 这种时候说"一条都没发出去"，就是上一版被用户
                            # 拿截图打脸的那句 —— 换句话说，结论必须比事实弱。
                            self._log("[发] 这一轮没有确认成功的条"
                                      "（有结果未知的，可能已到达，见上）", "err")
                        else:
                            self._log("[发] 这一轮一条都没发出去", "err")
                        _fin["outcome"] = "unknown" if _unknown_any else "failed"
                except SendError as exc:
                    self._log(f"[发送失败] {exc}", "err")
                    self.failed.emit(str(exc))
            finally:
                # 每轮一条结构化记录：她到底等了多久、想了多久、排了多久、
                # 发了多久。排查"她为什么慢"不用再从中文日志里数时间。
                try:
                    from bigfish import turns as _turns

                    _turns.record(
                        chat=msg.chat_id, chat_name=msg.chat_name,
                        outcome=_fin["outcome"],
                        wait_ms=(int(max(0.0, _gen0 - _recv) * 1000)
                                 if _gen0 and _recv else None),
                        gen_ms=(int(max(0.0, _gen1 - _gen0) * 1000)
                                if _gen0 and _gen1 else None),
                        queue_ms=(int(max(0.0, _t_send_start - _gen1) * 1000)
                                  if _gen1 else None),
                        send_ms=int((time.time() - _t_send_start) * 1000),
                        parts=len(parts),
                        sticker=entry.get("sticker_pos") or "none",
                    )
                except Exception:
                    pass
                # 同会话的下一批在等这把锁 —— 投递失败也必须放行
                if _lock is not None:
                    try:
                        _lock.release()
                    except Exception:
                        pass

        # ── 倾听层：等对方说完，再回 ──
        #
        # 以前是 `watcher.on_message = handle`：watcher 线程直接调 handle，
        # 而 handle 里要调模型（2~16 秒）。于是：
        #
        #   t=0.0  对方「大肥鱼」
        #   t=1.2  watcher 读到 → handle → 调模型（线程被占住）
        #   t=1.5  对方「你在不在」    ← 已写库，但这一秒读不到
        #   t=1.9  对方「吃饭了没」
        #   t=3.2  模型返回、发送      ← 这时才看到后面两句
        #   t=3.4  watcher 恢复 → 逐条再调两次模型 → 又回两次
        #
        # 两件事一起坏：**监听线程被模型挡住**，**每条消息各自成一轮**。
        # 表现就是「别人刚说一句她就开始想，然后插话」。
        #
        # 现在：watcher 只把消息丢进 batcher（立即返回），另起一个线程
        # 按自适应窗口封批，一批只调一次模型。窗口长短看字数与句尾标点
        # （见 listen.adaptive_quiet —— 短消息等 4 秒，长消息带句号只等 1.5）。
        import threading

        from bigfish import listen as L
        from bigfish.watcher import Incoming

        batcher = L.MessageBatcher(
            self._listen_policy(),
            allow_self_for=lambda chat: bool(scope_of("allow_self", chat)),
            on_log=self._log,
            # 启用「到点问一次模型」：判定由 _judge_one 在后台线程跑，
            # 攒批器本身不开 HTTP。见 docs/接话与补一句.md 的「攒批判定」。
            judge=True,
        )

        # ── 并发：生成并行，投递串行 ──
        #
        # 以前是一条 bigfish-batch 线程从防抖一直干到发送：两个会话同时说话，
        # 第二个要等第一个"想完 + 说完 + 挑完图"，最坏 30~50 秒。
        #
        # 现在三段：
        #   防抖（batch 线程）→ 生成（2 个 gen worker）→ 投递（1 个 send worker）
        #
        # 为什么生成要 2 个：模型调用是同步 HTTP，一个会话在想的时候，
        # 另一个会话可以同时在另一线程里想。为什么投递只有 1 个：见 _deliver。
        import queue as _queue

        _gen_queue: "_queue.Queue" = _queue.Queue(maxsize=64)

        def _enqueue_gen(msg) -> None:
            """把一条消息丢给生成 worker。**不阻塞** —— 队列满就丢并记一笔。

            睡醒补回也走这里：那条消息同样要过模型、同样会占一个生成位，
            但不该把调用方（主循环）按住。
            """
            try:
                _gen_queue.put_nowait(msg)
            except _queue.Full:
                self._log("[并发] 生成队列已满，这条补回的消息丢了", "err")
        _outbox: "_queue.Queue" = _queue.Queue()
        _chat_locks: dict = {}
        _chat_locks_guard = threading.Lock()
        _sticker_lock = threading.Lock()

        def _lock_for(chat_id: str):
            with _chat_locks_guard:
                lk = _chat_locks.get(chat_id)
                if lk is None:
                    lk = threading.Lock()
                    _chat_locks[chat_id] = lk
                return lk

        def _gen_worker() -> None:
            """生成阶段：调模型、切条、挑图。两个实例并行跑。"""
            while True:
                incoming = _gen_queue.get()
                if incoming is None:
                    break
                lock = _lock_for(incoming.chat_id)
                lock.acquire()
                entry = None
                try:
                    entry = handle(incoming)
                except Exception as exc:          # noqa: BLE001
                    self._log(f"[生成] 异常：{exc}", "err")
                if entry is None:
                    # 静默 / 生成失败 / 预演 / 不回：这一轮没有要发的东西
                    lock.release()
                    continue
                entry["lock"] = lock
                _outbox.put(entry)

        def _send_worker() -> None:
            """投递阶段：唯一碰 UIA 的线程。"""
            while True:
                entry = _outbox.get()
                if entry is None:
                    break
                try:
                    _deliver(entry)
                except Exception as exc:          # noqa: BLE001
                    self._log(f"[投递] 异常：{exc}", "err")

        def enqueue(msg) -> None:
            """watcher 回调：只入批，立刻返回。

            ⚠ 这里绝对不许调模型。一旦阻塞，watcher 的轮询就被按住，
              对方接着发的话读不到 —— 那正是这次要修的东西。

            ⚠ 「收到了」必须**在这里**打，不能留到 handle()。
              以前那行 `[收]` 在 handle 里，也就是要等轮询(≤1.2s) + 攒批(2s)
              之后才出现；用户看到的是「有人发消息，程序卡一会才收到」。
              消息其实早就在手里了，只是没告诉他。现在到手机就报。
            """
            try:
                # 只报**对方**的消息。她自己发的也会被 watcher 读回来
                # （发送确认 + 防自环要读），但那不是"收到" ——
                # 以前这里不看 is_self，于是日志里会出现
                # 「[收] 老爸：那你今天干点啥了」，其实那是她刚说的话。
                # 自己的消息由 [发] 覆盖，读回由倾听层的 [跳过] 兜底说明。
                if not getattr(msg, "is_self", False):
                    self._log(
                        f"[收] {msg.chat_name or ''}：{msg.preview(50)}", "in")
            except Exception:
                pass
            try:
                batcher.feed(L.BatchMessage(
                    chat_id=msg.chat_id,
                    author=(msg.sender_name or msg.chat_name),
                    text=(msg.text or ""),
                    chat_name=(msg.chat_name or ""),
                    local_id=int(getattr(msg, "local_id", 0) or 0),
                    msg_type=(msg.msg_type or "文本"),
                    # 群聊 @ 的判定在 handle 里做，这里拿不到触发结论。
                    # 代价是群里被 @ 也要等满自适应窗口（最多 4 秒）——
                    # 换来的是一条都不漏，先接受。
                    at_bot=False,
                    is_self=bool(getattr(msg, "is_self", False)),
                    received_at=time.monotonic(),
                ), replied_at=_reply_mono.get(msg.chat_id))
            except Exception as exc:
                self._log(f"[倾听] 入批失败：{exc}", "err")

        def handle_batch(batch) -> None:
            """一批消息 → 合成一条 → 交给原来的 handle。

            私聊里对方连发三句，模型看到的是三行带说话人的文本，
            而不是三次独立的思考。
            """
            msgs = batch.messages
            if not msgs:
                return
            last = msgs[-1]

            if len(msgs) == 1:
                body = last.text
            else:
                body = L.batch_transcript(batch, with_time=False)
                self._log(f"[倾听] {last.chat_name} 攒了 {len(msgs)} 条"
                          f"（{batch.reason}，等了 {batch.waited:.1f}s）", "in")
                # 合并后只用最后一条的 local_id 去记记忆，前面几条要补记，
                # 否则「他说的那句话」在记忆里根本不存在。
                # capture_message 按 event_key 幂等，和 handle 里那次重复
                # 调用不会写成两条。
                for m in msgs[:-1]:
                    try:
                        from bigfish import vault as _v3

                        _v3.capture_message(
                            f"wechat:{m.chat_id}",
                            f"wx-u-{m.local_id}" if m.local_id
                            else f"wx-u-{int(time.time() * 1000)}",
                            "user", m.text, None)
                    except Exception:
                        pass

            # 不在这里直接生成 —— 丢给 gen worker。
            # batch 线程只做防抖，一秒都不能被模型占住。
            #
            # ⚠ 用 put_nowait：队列满时**不能**阻塞 batch 线程，
            #   否则防抖停摆、watcher 也跟着堵。宁可丢这一批并记一笔。
            try:
                _gen_queue.put_nowait(Incoming(
                    chat_id=last.chat_id,
                    chat_name=last.chat_name,
                    local_id=int(last.local_id or 0),
                    sort_seq=0,
                    timestamp=int(time.time()),
                    msg_type=last.msg_type,
                    sender_id=None,
                    sender_name=last.author,
                    text=body,
                    is_self=bool(last.is_self),
                    # 进批的只有对方说的话（feed 会丢掉 is_self）
                    identity="peer",
                    identity_rule="batch",
                    raw_content=body,
                ))
            except _queue.Full:
                self._log("[并发] 生成队列已满（64），丢弃这一批 —— "
                          "她在前面的会话里还没想完", "err")

        # ── 攒批判定（0.11.1）──
        #
        # 安静窗口到点后先问一次模型「他说完了吗」：
        #   more=true  → 再等 judge_extra 秒（最多 judge_max_rounds 次）
        #   more=false → 立刻封批
        # 一次请求 0.5~2 秒，必须离开 tick 线程，否则别的会话会被按住。
        # 并发限 2：同一会话不会重复判（batcher 的 awaiting 挡着），
        # 限并发是为了别为几个会话同时打爆模型额度。
        from concurrent.futures import ThreadPoolExecutor

        _judge_pool = ThreadPoolExecutor(max_workers=2,
                                         thread_name_prefix="bigfish-judge")

        def _judge_one(chat_id: str, msgs: list) -> None:
            """问模型「他还有下一句吗」，然后把结论交回攒批器。"""
            more, why = False, ""
            try:
                from bigfish import listen_judge as _LJ

                last = msgs[-1] if msgs else None
                more, why = _LJ.judge_more(
                    [{"text": m.text} for m in msgs],
                    peer=((getattr(last, "author", "") or
                           getattr(last, "chat_name", "") or "对方")
                          if last else "对方"),
                    self_name=getattr(self.soul, "name", "") or "",
                    timeout=float(batcher.policy.judge_timeout),
                )
                if why:
                    self._log("[倾听] 判定："
                              + ("他还有下一句" if more else "说完了")
                              + f"（{why}）", "think")
            except Exception as exc:          # noqa: BLE001
                # 判定失败一律按"说完了"走：宁可早回一句，也不让人干等
                self._log(f"[倾听] 判定失败（按说完了处理）：{exc}", "err")
                more = False
            try:
                _b = batcher.verdict(chat_id, more)
                if _b is not None:
                    handle_batch(_b)
            except Exception as exc:          # noqa: BLE001
                self._log(f"[倾听] 判定收尾异常：{exc}", "err")

        _pol_at = 0.0

        def _batch_loop() -> None:
            """每 200ms 看一眼有没有该封的批、该判定的批。

            为什么不复用主循环那个 `msleep(300)`：主循环管的是主动开口和
            生活状态（5 秒一次就够），而封批精度直接决定「她等多久」。
            200ms 的误差对 6 秒的窗口来说可以忽略。
            """
            nonlocal _pol_at
            while not self._stop:
                time.sleep(0.2)
                try:
                    # 面板改完 5 秒内生效。读 plugins.json 有磁盘 IO，
                    # 不该每 200ms 来一次。
                    if time.time() - _pol_at >= 5.0:
                        _pol_at = time.time()
                        batcher.set_policy(self._listen_policy())
                    for _b in batcher.tick():
                        handle_batch(_b)
                    for _cid, _msgs in batcher.due():
                        _judge_pool.submit(_judge_one, _cid, _msgs)
                except Exception as exc:
                    self._log(f"[倾听] 封批异常：{exc}", "err")
            # 退出前把没封的批次冲掉 —— 不然对方最后那几句永远等不到回
            try:
                _judge_pool.shutdown(wait=False)
            except Exception:
                pass
            try:
                for _b in batcher.flush_all():
                    handle_batch(_b)
            except Exception:
                pass

        threading.Thread(target=_batch_loop, name="bigfish-batch",
                         daemon=True).start()
        # 生成 2 条、投递 1 条。数字不放大是刻意的：单个微信窗口只能一个
        # 一个发，生成再多也只是把 outbox 堆长。
        for _gi in range(2):
            threading.Thread(target=_gen_worker, name=f"bigfish-gen{_gi + 1}",
                             daemon=True).start()
        threading.Thread(target=_send_worker, name="bigfish-send",
                         daemon=True).start()

        self.watcher.on_message = enqueue
        self.watcher.start()
        # 拟人节流的档位。**必须在真正发送之前设好** ——
        # 默认的 natural 是「120 秒内 6 次」，而一轮就发 2~4 条，
        # 两轮就撞上，然后被按住 30~75 秒（线上真的这样）。
        try:
            from bigfish.wechat_sender import RHYTHM_NOTES, apply_rhythm

            _cfg = load_json_config().get("auto_reply") or {}
            _want = str(_cfg.get("rhythm") or "fast")
            _got = apply_rhythm(_want)
            if _got:
                self._log(f"[节流] 节奏档 {_got} —— {RHYTHM_NOTES.get(_got, '')}",
                          "")
            else:
                self._log(f"[节流] 设不了节奏档 {_want!r}，按底层默认走", "err")
        except Exception as exc:
            self._log(f"[节流] 设置失败：{exc}", "err")

        self._log(f"开始监听 {len(self.watching)} 个会话", "in")

        # ── 她的生活从这里开始走 ──
        #
        # 这个 300ms 的空转循环以前真的只是空转。现在每 5 秒推一次状态：
        # 到点睡、到点醒、精力往下掉、手头的事换一样。
        #
        # 5 秒是拍的量级：状态里最小可见的变化是「困 / 不困」，
        # 那种事不需要更细的粒度；而每 5 秒写一次盘会先把 SSD 写坏，
        # 所以 save() 内部还有"变了才写 + 30 秒最短间隔"两道闸。
        try:
            from bigfish import life as _life

            self._advance_life(time.time())
            if self._life is not None:
                self._log(f"[生活] {_life.condition(self._life)}"
                          f"（手头：{self._life.doing or '—'}）", "in")
        except Exception as exc:
            self._log(f"[生活] 初始化失败：{exc}", "err")

        # ── 她自己会做的那一半 ──
        #
        # 情绪平复、睡前写三行、主动开口、追问。
        # 放在独立模块（bigfish/world.py）里，因为 0.4.1 那次事故的复盘
        # 已经写明：逻辑埋进 _run 的闭包就等于没有测试。
        try:
            from bigfish.world import World

            self._world = World(self)
        except Exception as exc:
            self._world = None
            self._log(f"[主动] 初始化失败，本次只回消息不主动开口：{exc}", "err")

        tick_every = 5.0
        next_tick = time.time() + tick_every
        while not self._stop:
            self.msleep(300)
            now = time.time()
            # 待发的主动消息每 300ms 看一眼 —— 生成在后台，发送必须在这条线程
            if self._world is not None:
                self._world.flush(now)
            # 补一句：到点了才发，也可能被取消（对方说话了 / 她睡了 / 拖太久）
            try:
                self._flush_after(now)
            except Exception as exc:
                self._log(f"[接话] 补一句异常：{exc}", "err")
            # ── 睡醒补回 ──
            #
            # 她睡着时对方发的消息，醒来 20 秒后重新走一遍正常回复链。
            # 走 handle 而不是自己拼 prompt：记忆、心情、刚醒的状态、
            # 发送三态与账本全都复用同一条路，不会长出第二套。
            try:
                self._drain_catchup(now, _enqueue_gen)
            except Exception as exc:
                self._log(f"[生活] 补回异常：{exc}", "err")
            if now >= next_tick:
                next_tick = now + tick_every
                turns = self._talk_turns
                self._talk_turns = 0
                self._advance_life(now, talk_turns=turns)
                if self._world is not None:
                    # 心情平复：身体功能，不受插件开关影响
                    self._world.tick(now)
                    # 主动开口：只有「主动」插件开着才跑。
                    # 关掉就是整块不执行 —— 不是算完再丢掉，
                    # 那样日志里照样会冒出「她本来想说什么」。
                    try:
                        from bigfish import plugins as _P2

                        _P2.run_hooks({"now": now, "world": self._world,
                                       "engine": self})
                    except Exception as exc:
                        self._log(f"[插件] 动作失败：{exc}", "err")


# ══════════════════════════════════════════════════════════════════
#  后端接口（暴露给 JS）
# ══════════════════════════════════════════════════════════════════
def _parse_payload(raw: str) -> dict:
    """前端传过来的 JSON。坏了就当空，不让它把后台线程炸掉。"""
    try:
        d = json.loads(raw or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


class Backend(QObject):
    log_message = Signal(str, str)
    chat_event = Signal(str, str, str)
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.worker: EngineWorker | None = None
        # ⚠ 必须从这里读，不能留空。
        #
        #   真出过的 bug：界面上的勾选来自「选账号」那一处的返回值，
        #   而 start_engine 读的是 self.watching —— 后者原本只在**手动保存**
        #   时才被赋值。于是界面明明勾着 1 个，点启动却说"没有选择要监听的
        #   会话"，非要取消再勾一次才行。
        self.watching: list[str] = self._watching_from_config()
        self.started_at = 0.0
        self.last_error = ""
        self.recv_count = 0
        self.reply_count = 0
        #: 账号标识（`wxid_*` / 数据目录名）。**唯一键** ——
        #: 作用域解析、配置里的 accounts、启动引擎都用它。
        self.account_id = ""
        #: 展示用昵称。昵称会改、会重名，所以**绝不能拿它当键**。
        self.account_nick = ""
        #: 兼容旧调用方的展示名 = 昵称优先，没有昵称就用 id。
        self.account = ""
        self._reader = None
        # 日志缓冲：信号可能因为界面还没连上而丢失，
        # get_status 里带上增量，前端补进去。
        self._log_buffer = []
        self._log_seq = 0
        self._log_sent = 0
        # 「测试连接」的票据表。测试要真发一次请求（可能几秒到几十秒），
        # 同步做会把整个界面卡住 —— Qt 的 slot 在 GUI 线程里跑。
        # 所以开线程测，前端拿 ticket 轮询。
        self._tests: dict = {}
        # 「获取模型列表」的票据表。同一个理由，另一件事。
        self._fetches: dict = {}

    def _watching_from_config(self) -> list[str]:
        """配置里写着的监听列表。

        优先这个号专属的那份（per_account），没有再退回 auto_reply.chats。
        这两处必须和 `set_watching` 写的地方一一对应，否则又会出现
        "界面勾着、后端说没有"。
        """
        try:
            c = load_json_config()
            ar = c.get("auto_reply") or {}
            acc = str(ar.get("account") or "")
            if acc:
                per = ((c.get("per_account") or {}).get(acc) or {})
                saved = [str(x) for x in (per.get("chats") or [])]
                if saved:
                    return saved
            return [str(x) for x in (ar.get("chats") or [])]
        except Exception:
            return []

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
            # 标识与展示名分开存：acc 是目录名/wxid（唯一键），昵称只用来显示。
            # 以前一个字段轮流存两种东西，于是 `_scope_ctx` 拿昵称当 bot_id
            # 去查 config.accounts（键是目录名）—— 查不到就落回全局层。
            self.account_id = acc
            self.account_nick = str(info.get("nick_name") or "")
            self.account = self.account_nick or acc
            self._reader = r
            cost = time.time() - t0

            # 记住选择 + 缓存昵称 + **恢复这个号上次的监听配置**
            c = load_json_config()
            ar = c.setdefault("auto_reply", {})
            ar["account"] = acc
            c.setdefault("accounts", {})[acc] = self.account_nick or acc

            # 每个微信号各存一份监听列表。
            # 理由：不同号关注的会话完全不同（工作号 vs 私人号），
            # 共用一份等于每次切换都要重勾一遍。
            restored = restore_watching_for(c, acc)
            # 界面拿到的是 restored，后端也必须是同一份 —— 否则点启动会说没选。
            self.watching = list(restored)
            try:
                save_json_config(c)
            except Exception:
                pass

            return json.dumps({
                "ok": True,
                "account": acc,
                "nick": self.account_nick or acc,
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
            _peek = self._peek_account()
            self.account = _peek
            # _peek_account 优先给昵称，拿不到才给 wxid —— 两种要落到对应字段，
            # 否则 id 会一直是空的，作用域兜底又回到昵称当键的老问题。
            if _peek.startswith("wxid_"):
                self.account_id = self.account_id or _peek
            elif _peek:
                self.account_nick = self.account_nick or _peek

        diag = self._diagnose_items()
        # 发送通道没就绪时，每 20 秒在后台补试一次。
        #
        # 为什么放在 get_status 里：用户盯着「运行中 · 发不出去」的红字，
        # 点刷新只是重新读一遍状态，什么都解决不了。以前重试只发生在
        # 「下一条消息来了」的时候 —— 没消息就一直红着，看起来像坏了。
        if running and not getattr(self.worker, "dry", False) \
                and getattr(self.worker, "sender", None) is None:
            _now = time.time()
            if _now - float(getattr(self, "_uia_retry_at", 0.0)) >= 20:
                self._uia_retry_at = _now
                try:
                    import threading

                    threading.Thread(
                        target=self.worker._ensure_sender,
                        kwargs={"quiet": True},
                        daemon=True, name="bigfish-uia-retry").start()
                except Exception:
                    pass
        return json.dumps({
            "running": running,
            "error": self.last_error,
            # 发送通道是否可用。running 但 sender=False = 只监听不发送，
            # 界面要当场说清楚 —— 否则用户以为启动成功了，等她回话等到天亮。
            "sender": bool(getattr(self.worker, "sender", None)) if running else None,
            "dry": bool(getattr(self.worker, "dry", False)) if running else False,
            "uptime": up,
            "recv": self.recv_count,
            "reply": self.reply_count,
            "watching": len(self.watching),
            # account 是展示名（昵称优先）；accountId 才是唯一键。
            "account": self.account,
            "accountId": self.account_id,
            "diagnose": diag,
            # 提权与否启动时就定了，每 2 秒重算纯属浪费
            "admin": _is_admin_cached(self),
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
        items = []

        # 提权与否进程级常量，缓存住（见 _is_admin_cached 的说明）
        items.append({"key": "admin",
                      "status": "ok" if _is_admin_cached(self) else "err",
                      "value": "已提权" if _is_admin_cached(self) else "需要管理员"})

        desk = False
        try:
            from bigfish.wechat_sender import desktop_available
            desk = desktop_available()
        except Exception:
            pass
        items.append({"key": "desktop", "status": "ok" if desk else "warn",
                      "value": "可用" if desk else "锁屏中"})

        # 微信进程
        #
        # ⚠ 这一段以前**每 2 秒**跑一次。_find_weixin_pids 里要
        #   EnumWindows + NtQuerySystemInformation + tasklist 三路合一，
        #   实测 150~175 ms —— 全在 GUI 线程上。界面每隔两秒就卡一下
        #   （表现为点任何按钮的响应慢半拍），查了很久才发现是这里。
        #   现在缓存 30 秒：诊断信息本来就是给人看"是不是没连上"，
        #   晚 30 秒完全无所谓；代价是启动微信后最多等一下才显示。
        wpids = []
        try:
            now = time.time()
            cached = getattr(self, "_wechat_pids_cache", None)
            if cached and now - cached[0] < 30:
                wpids = cached[1]
            else:
                from wechatauto.db import WeChatDB
                probe = WeChatDB.__new__(WeChatDB)
                wpids = probe._find_weixin_pids()
                self._wechat_pids_cache = (now, wpids)
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
            # 这里拿到的是目录名/wxid —— 是**标识**，不是昵称。
            # 以前直接写进 self.account，于是同一个字段一会儿是昵称、
            # 一会儿是目录名，谁拿它当键谁踩坑。
            self.account_id = str(getattr(reader._db, "account", "") or "") \
                or self.account_id
            if not self.account:
                self.account = self.account_id
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
            prev = list(self.watching)
            self.watching = lst
            c = load_json_config()
            ar = c.setdefault("auto_reply", {})
            ar["chats"] = lst
            acc = str(ar.get("account") or "")
            if acc:
                c.setdefault("per_account", {}).setdefault(acc, {})["chats"] = lst
            save_json_config(c)
            # 运行中就把变化同步给 watcher，别让用户重启引擎。
            hot = self._sync_watching_hot(prev, lst)
            return json.dumps({"ok": True, "count": len(self.watching),
                               "account": acc, "hot": hot}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    def _sync_watching_hot(self, old: list, new: list) -> bool:
        """把监听列表的增删同步给正在跑的 watcher。

        ⚠ 以前 ``set_watching`` 只改内存 + 存盘：引擎已经在跑时，新加的会话
          永远收不到消息 —— ``watcher._chats`` 在启动那一刻就定死了，
          日志里连一行「监听：xxx」都没有。用户看到的是
          「加了监控、对方说话了、程序没反应、日志也没有」（2026-11-08 现场）。
        """
        worker = getattr(self, "worker", None)
        if worker is None or not worker.isRunning():
            return False
        watcher = getattr(worker, "watcher", None)
        if watcher is None:
            return False
        added = [x for x in new if x not in old]
        removed = [x for x in old if x not in new]
        if not added and not removed:
            return False
        try:
            for uid in added:
                got = watcher.watch(uid)
                worker._log(f"监听：{uid} -> {got}（运行中新增，从现在开始收）",
                            "in")
            for uid in removed:
                if watcher.unwatch(uid):
                    worker._log(f"取消监听：{uid}", "in")
            return True
        except Exception as exc:
            worker._log(f"[监听] 运行中更新失败（重启引擎才会生效）：{exc}",
                        "err")
            return False

    # ── 会话级配置（C-1 作用域） ──
    #
    # 为什么不复用 get_config / save_config：那一对读写的是**全局**设置
    # （scopes.global），这里读写的是**某一个会话**的覆盖层。
    # 混在一对方法里，早晚会出现"我以为改的是这个会话，其实改了全局"。

    def _scope_context(self):
        """(配置, bot_id, 解析器)。

        解析器只在有已选账号时可用 —— 微信没开时昵称键解析不出来，
        那是"未命中"，不是错误。
        """
        from bigfish import scopes as _scopes

        cfg = load_json_config()
        # bot_id 必须是**唯一键**（目录名/wxid）—— config.accounts 的键就是它。
        # 以前兜底用 self.account，而那个字段可能是昵称，查不到就落回全局层。
        bot = (str((cfg.get("auto_reply") or {}).get("account") or "")
               or self.account_id or self.account)
        resolver = None
        if self._reader is not None:
            try:
                resolver = _scopes.memo_resolver(self._reader.resolve)
            except Exception:
                resolver = None
        return cfg, bot, resolver

    def _scope_rows(self, cfg, bot, resolver, chat_id):
        from bigfish import scopes as _scopes

        row = _scopes.resolve(cfg, bot_id=bot or None, chat_id=chat_id,
                              resolver=resolver)
        # 「如果把这一项从会话层删掉，它会从哪来」——
        # 界面在用户点「继承」后要显示这个，而不是当前生效层。
        inherited = _scopes.resolve(cfg, bot_id=bot or None, chat_id=None,
                                    resolver=resolver)
        out = []
        for key, spec in _scopes.FIELDS.items():
            value, layer = row[key]
            if isinstance(value, tuple):
                value = list(value)
            inherit_value = inherited[key][0]
            if isinstance(inherit_value, tuple):
                inherit_value = list(inherit_value)
            out.append({
                "key": key,
                "label": spec.get("label", key),
                "kind": spec.get("kind", ""),
                "note": spec.get("note", ""),
                "placeholder": spec.get("placeholder", ""),
                "value": value,
                "layer": layer,
                "layer_label": _scopes.LAYER_LABELS.get(layer, layer),
                # 清掉会话覆盖之后的值与来源
                "inherit_value": inherit_value,
                "inherit_label": _scopes.LAYER_LABELS.get(
                    inherited[key][1], inherited[key][1]),
                # 只有落在会话层才算"我设过"；其余都是继承来的
                "overridden": layer == _scopes.LAYER_CHAT,
            })
        return out

    @Slot(str, result=str)
    def get_chat_scope(self, chat_id: str) -> str:
        """读某个会话的三层生效值 + 每一项的来源层。

        界面靠 `layer` / `overridden` 标「本会话」还是「继承自全局」。
        """
        cid = str(chat_id or "").strip()
        if not cid:
            return json.dumps({"ok": False, "error": "缺少会话 id"},
                              ensure_ascii=False)
        try:
            cfg, bot, resolver = self._scope_context()
            from bigfish import scopes as _scopes
            return json.dumps({
                "ok": True,
                "chat_id": cid,
                "fields": self._scope_rows(cfg, bot, resolver, cid),
                "problems": [list(p) for p in _scopes.diagnose(
                    cfg, bot_id=bot or None, chat_id=cid, resolver=resolver)],
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

    @Slot(str, str, result=str)
    def save_chat_scope(self, chat_id: str, payload: str) -> str:
        """写会话层覆盖。

        `payload` 形如 `{"allow_self": false, "quiet_hours": null}`：
        **值给 null 表示"恢复继承"**（把这一项从会话层删掉），
        删空了的会话块会一起清掉，不在配置里留空壳。
        """
        cid = str(chat_id or "").strip()
        if not cid:
            return json.dumps({"ok": False, "error": "缺少会话 id"},
                              ensure_ascii=False)
        try:
            changes = json.loads(payload or "{}")
            if not isinstance(changes, dict):
                return json.dumps({"ok": False, "error": "payload 必须是对象"},
                                  ensure_ascii=False)
            from bigfish import scopes as _scopes

            cfg = load_json_config()
            applied, cleared = [], []
            for key, value in changes.items():
                if key not in _scopes.FIELDS:
                    continue
                if value is None:
                    if _scopes.clear_chat_value(cfg, cid, key):
                        cleared.append(key)
                else:
                    _scopes.set_chat_value(cfg, cid, key, value)
                    applied.append(key)
            save_json_config(cfg)

            _cfg, bot, resolver = self._scope_context()
            return json.dumps({
                "ok": True,
                "chat_id": cid,
                "applied": applied,
                "cleared": cleared,
                "fields": self._scope_rows(_cfg, bot, resolver, cid),
            }, ensure_ascii=False)
        except ValueError as exc:
            # set_chat_value 对昵称键 / 未注册字段抛 ValueError，原样带出去
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

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
            # 这一轮的预算不是界面算的：mood / energy / 余韵在回复链路里
            # 真正怎么收紧，就在这里用同一套 policy 算给用户看。
            try:
                import time as _time
                from bigfish import policy as _policy
                from bigfish import life as _life
                from bigfish import afterglow as _ag

                st = _life.load()
                energy = st.energy if st.day else None
                pol = _policy.response_policy(
                    mood=m.value, dims=m.dims, energy=energy,
                    now=_time.time())
                pol = _policy.apply_afterglow(
                    pol, _ag.current_effect(now=_time.time()))
                out["budget"] = {
                    "parts": pol.max_parts,
                    "chars": pol.max_chars,
                    "silence": pol.silence_bias,
                    "must_after": pol.must_reply_after,
                    "tone": pol.tone_line,
                    "reason": pol.reason,
                    "energy": round(float(energy), 0) if energy is not None else None,
                }
                active = _ag.active(_ag.load(), now=_time.time())
                out["afterglow"] = {
                    "count": len(active),
                    "text": _ag.summary(now=_time.time()) if active else "",
                }
            except Exception as exc:
                out["budget"] = {"error": str(exc)}
            return json.dumps(out, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    @Slot(result=str)
    def get_today(self) -> str:
        """她今天的一片生活：生活状态、日程、心情、日记与感知。

        只读，不触发模型、不写盘。界面刷新不应该改变她的生活。
        """
        try:
            import datetime as _dt
            import time as _time
            from bigfish import life as _life
            from bigfish import senses as _senses
            from bigfish import journal as _journal
            from bigfish import afterglow as _ag
            from bigfish.bridge_client import load_mood_only

            now = _time.time()
            local = _dt.datetime.fromtimestamp(now)
            st = _life.load()
            has_life = bool(st.day)

            plan = []
            # 跨天没醒/引擎没跑时，life.day 会停在昨天，但 plan 仍然是她
            # 最后写的安排。这里不能按「plan_day == life.day」把它丢掉 ——
            # 那会让早上第一眼看到「今天还没有排日程」，而磁盘上明明有。
            # UI 用 plan_day 和真实今天比较，显示「安排来自前一天」。
            if st.plan:
                for item in st.plan:
                    if not isinstance(item, dict):
                        continue
                    plan.append({
                        "at": str(item.get("at") or ""),
                        "what": str(item.get("what") or ""),
                        "done": bool(item.get("done")),
                        "done_by": str(item.get("done_by") or ""),
                        "from": str(item.get("from") or ""),
                        "shareable": item.get("shareable") is not False,
                    })

            def hm(ts):
                try:
                    return _dt.datetime.fromtimestamp(float(ts)).strftime("%H:%M")
                except Exception:
                    return ""

            mood = load_mood_only()
            afterglow_items = []
            try:
                afterglow_items = _ag.active(_ag.load(), now=now)
            except Exception:
                pass

            weather = ""
            city = ""
            try:
                weather = _senses.cached_weather(now)
                city = str(((_senses.load() or {}).get("weather") or {}).get("city") or "")
            except Exception:
                pass

            days = []
            journal_text = ""
            journal_day = ""
            try:
                days = _journal.list_days(limit=7)
                journal_day = days[0] if days else ""
                if journal_day:
                    journal_text = _journal.read(journal_day)
            except Exception:
                pass

            return json.dumps({
                "ok": True,
                "now": local.isoformat(timespec="seconds"),
                # day/weekday 是**真实今天**；life_day 才是她生活状态那天。
                # 引擎整天没跑时二者不同，不能让顶栏和日程各说各的。
                "day": local.strftime("%Y-%m-%d"),
                "life_day": st.day or "",
                "weekday": "一二三四五六日"[local.weekday()],
                "greeting": ("早上好" if local.hour < 11 else
                             "下午好" if local.hour < 18 else "晚上好"),
                "life": {
                    "asleep": bool(st.asleep) if has_life else False,
                    "energy": round(float(st.energy), 0) if has_life else None,
                    "doing": str(st.doing or "") if has_life else "",
                    "condition": (_life.condition(st, now)
                                  if has_life else "还没有生活状态"),
                    "wake_at": hm(st.wake_at) if has_life else "",
                    "bedtime": hm(st.bedtime) if has_life else "",
                    "sleep_debt": round(float(st.sleep_debt or 0), 0),
                    "plan_day": str(st.plan_day or ""),
                    "trace": [
                        {"at": hm(x.get("at")), "doing": str(x.get("doing") or "")}
                        for x in (st.day_trace or [])[-4:]
                        if isinstance(x, dict)
                    ],
                },
                "plan": plan,
                "mood": {
                    "value": mood.value if mood else None,
                    "label": mood.label if mood else "",
                    "reason": mood.last_reason if mood else "",
                    "updated": (ts_to_local(mood.updated_at)
                                if mood and mood.updated_at else ""),
                },
                "afterglow": {
                    "count": len(afterglow_items),
                    "text": _ag.summary(now=now) if afterglow_items else "",
                },
                "weather": {"line": weather, "city": city},
                "journal": {
                    "day": journal_day,
                    "text": journal_text[:1200],
                    "days": days,
                    "path": _journal.path_for(journal_day) if journal_day else "",
                },
                "recent": list(getattr(self, "_recent", [])[:4]),
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

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

    # ── 关系卡（v0.9）──────────────────────────────────────────────
    @Slot(str, result=str)
    def bond_update(self, payload: str) -> str:
        """改一条关系卡。payload: {id, action, text?}

        action: reture(下线) / restore(恢复) / delete(删除)
        ⚠ 删除是真删。下线和删除分开，因为「她记错了」和
          「这件事不该存在」是两件事。
        """
        try:
            import bigfish.vault as V
            from bigfish import bond as B

            db = V.memory_db_path()
            if not db:
                return json.dumps({"error": "找不到记忆库"}, ensure_ascii=False)
            data = _parse_payload(payload)
            bid = int(data.get("id") or 0)
            action = str(data.get("action") or "")
            if not bid:
                return json.dumps({"error": "缺少 id"}, ensure_ascii=False)

            if action == "retire":
                ok = B.set_status(db, bid, B.STATUS_RETIRED)
            elif action == "restore":
                ok = B.set_status(db, bid, B.STATUS_ACTIVE)
            elif action == "delete":
                ok = B.delete_bond(db, bid)
            elif action == "edit":
                # 只改措辞，evidence（原话）不动
                ok, why = B.update_text(db, bid, str(data.get("text") or ""))
                if not ok:
                    return json.dumps({"error": why or "改不了"},
                                      ensure_ascii=False)
            else:
                return json.dumps({"error": f"不认识的动作：{action}"},
                                  ensure_ascii=False)
            return json.dumps({"ok": bool(ok)}, ensure_ascii=False)
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

    def _lazy_reader(self):
        """尽力拿一个 reader；拿不到就返回 None，不抛。

        印象页要显示昵称，而昵称只有微信的 contact 库知道。用户可能
        还没在「会话」页点过选号，但配置文件里有 account —— 那就按它开一个。

        为什么不复用 `_reader_or_open`：那个在没选号时会 raise，
        而「显示昵称」这种附加信息不该让整页报错。
        """
        if self._reader is not None:
            return self._reader
        try:
            acc = str((load_json_config().get("auto_reply") or {})
                      .get("account") or "")
            if not acc:
                return None
            from bigfish.wechat import WeChatReader

            self._reader = WeChatReader(account=acc).open()
            return self._reader
        except Exception:
            return None

    def _nick_of(self, uid: str) -> str:
        """wxid → 当前昵称。查不到返回空串。

        `_display_name` 已经有一整条解析链（备注 → 昵称 → 微信号 → 群名 →
        filehelper → 兜底），这里只是包一层异常。
        """
        if not uid:
            return ""
        r = self._lazy_reader()
        if r is None:
            return ""
        try:
            name = r._display_name(uid) or ""
        except Exception:
            return ""
        # 兜底占位（「未知联系人(abc123)」）别写进库里 ——
        # 那是"查不到"的显示形态，不是真昵称，写进去就永远修不掉了。
        if name.startswith("未知联系人("):
            return ""
        return name[:80]

    @Slot(result=str)
    def impression_board(self) -> str:
        """「角色印象」页：一人一格，格子里是她的评语 + 开放信息清单。

        主人那一格永远排第一（界面上第一个横向分页）。
        排序在这里做一次，不让前端猜。

        ⚠ 昵称在这里补一次。owners 表里的 display_name 以前从来没人写
          （四处 ensure_owner 都没传），所以印象页的标签是裸 wxid。
          每次打开这一页顺手把最新昵称刷进库：微信里改了备注或昵称，
          下次进页面就是新的 —— wxid 不变，只是名字跟着变。
        """
        try:
            from bigfish import memory as M, vault as V

            db = V.memory_db_path()
            data = M.impression_board(db or "", "dafeiyu")
            me = getattr(self, "owner_id", "")
            people = data.get("people") or []
            for p in people:
                p["isOwner"] = (p["owner"] == me)
                self._refresh_display_name(db, p)
                # 关系卡跟着人走：它本来就是「我对这个人的认识」的一部分，
                # 之前单独占一个侧栏板块，等于把同一件事说了两遍。
                p["bonds"] = _bonds_of(db, p["owner"])
                # 时间线只给真实事件（bond_events），没有就不返回；
                # 前端不会用 bonds 现编一条“事件”。
                p["events"] = _bond_events_of(db, p["owner"])
            # 主人置顶，其余按事实数排 —— 聊得多的人排前面
            people.sort(key=lambda p: (0 if p.get("isOwner") else 1,
                                       -int(p.get("facts") or 0)))
            data["people"] = people
            data["mainOwner"] = me
            return json.dumps(data, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"people": [], "error": str(exc)},
                              ensure_ascii=False)

    def _refresh_display_name(self, db: str, p: dict) -> None:
        """把一个人格子的 name 换成当前昵称（并写回 owners 表）。"""
        owner = str(p.get("owner") or "")
        if not owner or not db:
            return
        uid = owner.rsplit(":", 1)[-1]
        cur = str(p.get("name") or "")
        # 已经是昵称（不等于 wxid/external_id）就不折腾
        already = bool(cur) and cur != uid
        nick = self._nick_of(uid)
        if not nick:
            return
        if nick == cur:
            return
        p["name"] = nick
        if already and nick == cur:
            return
        try:
            from bigfish import memory as M

            M.ensure_owner(db, "dafeiyu", owner, display_name=nick)
        except Exception:
            pass

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

    @Slot(bool, result=str)
    def sticker_list(self, slim: bool = False) -> str:
        """表情库全量。界面一次拿全，翻页在前端做。

        `slim=True` 时省掉几个**只有详情弹窗才用**的重字段：
        `detail`（平均 220 KB）、`tags`、`path`（前端拼得出来）。
        翻页只显示缩略图 + 一句话，用不着这些；双击看详情时再走
        `sticker_get` 单独取那一条 —— 一页 60 张，一次 1 KB。
        """
        try:
            from bigfish import plugins as _plugins
            from bigfish import stickers

            _plugins.ensure_loaded()
            items = stickers.list_stickers(with_detail=not slim)
            if slim:
                items = [{
                    "code": it["code"],
                    "file": it["file"],
                    "thumb": it["thumb"],
                    "brief": it["brief"],
                    "code_": it["code"],
                    "size": it["size"],
                    "frames": it["frames"],
                    "intensity": it["intensity"],
                    "moodDim": it["moodDim"],
                    "moodDimLabel": it["moodDimLabel"],
                    "kind": it["kind"],
                } for it in items]
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
    def sticker_get(self, code: str) -> str:
        """取一张表情的完整信息（详情弹窗用）。

        为什么单独开一个：列表里不带 detail（220 KB），
        打开详情时只取这一条（约 300 字节）——
        比"为了弹窗把 2000 条 detail 全塞进列表"便宜三个数量级。
        """
        try:
            from bigfish import stickers

            it = stickers.get_sticker(str(code))
            if not it:
                return json.dumps({"ok": False, "error": "没有这张表情"},
                                  ensure_ascii=False)
            return json.dumps({"ok": True, "item": it}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

    @Slot(str, result=str)
    def sticker_add(self, paths_json: str) -> str:
        """把一批文件收进库（带去重）。

        界面那边可能是「浏览…」选的，也可能是拖进来的，都走这里。
        每张都会：**查重 → 转 GIF → 分配代码 → 落盘 → 做缩略图**。

        查重规则（见 bigfish/sticker_fp.py）：
          · 字节完全相同            → 跳过
          · 配色 + 构图几乎一致      → 跳过（同一张图的另一次导出）
          · 只是"很像"（同系列）     → 照收，但在报告里列出来

        ⚠ 分批：以前 2000 张一次性循环，这个 @Slot 跑在 GUI 线程上，
          整个窗口十来分钟不响应（连"取消"都点不了）。
          现在每 20 张就 processEvents + 回报一次进度，
          界面能刷新、能显示"导到第几张"。
          前端那边也应该分批调用，这里是第二道保险。
        """
        try:
            from bigfish import stickers

            paths = json.loads(paths_json or "[]")
            if not isinstance(paths, list):
                return json.dumps({"ok": False, "error": "参数不对"},
                                  ensure_ascii=False)

            # 刻意**不**在这里再切一次片：切片会让 index 每 20 张写一次
            # （1MB × N 次）。批次的粒度由前端控制，这里整批一次收。
            rep = stickers.import_batch([str(p) for p in paths])
            self._pump_events()
            return json.dumps({"ok": True,
                               "added": rep["added"],
                               "skipped": rep["skipped"],
                               "failed": rep["failed"],
                               "near": rep.get("near") or []},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

    #: 导入时每处理这么多张就让出一次事件循环。
    #: 太小则 processEvents 自己成了开销，太大则界面看起来像卡死。
    IMPORT_YIELD_EVERY = 20

    def _pump_events(self) -> None:
        """处理一轮挂起的界面事件，不阻塞、不递归进重入的弹窗。

        只在长循环的缝隙里调。用 ExcludeUserInputEvents 是为了
        不把"用户又点了什么"塞进正在跑的循环里 ——
        那会让导入逻辑和按钮回调交错执行。
        """
        try:
            from PySide6.QtCore import QCoreApplication, QEventLoop

            QCoreApplication.processEvents(
                QEventLoop.ExcludeUserInputEvents)
        except Exception:
            pass

    @Slot(int, result=str)
    def sticker_fill_thumbs(self, limit: int = 60) -> str:
        """给库里还缺缩略图的表情补一批，返回补了多少。

        ⚠ 有上限，而且是**每次调用补一批**：导入完成后 2000 张
          一次性补会卡 40 秒；分 34 次、每次 60 张（约 1.2s），
          界面在两次之间能正常响应。
          前端在 loadStickers 之后反复调它，直到返回 0。
        """
        try:
            from bigfish import stickers

            items = stickers.list_stickers(with_detail=False)
            missing = [it["code"] for it in items if not it.get("thumb")]
            # 顺带清掉早期版本的 PNG 缩略图。只动 _thumbs/，不碰素材。
            stickers.sweep_legacy_thumbs()
            if not missing:
                return json.dumps({"ok": True, "made": 0,
                                   "left": 0}, ensure_ascii=False)
            # 一次最多 60 张 ≈ 1.5s（实测 20ms/张）。
            # 界面每批之间让出事件循环，用户还能滚动。
            made = stickers.ensure_thumbs(missing, limit=int(limit or 60))
            self._pump_events()
            return json.dumps({"ok": True, "made": made,
                               "left": max(0, len(missing) - made)},
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

    @Slot(result=str)
    def prompt_trace_list(self) -> str:
        """最近几轮「她看到了什么」的摘要（给设置页用）。"""
        try:
            from bigfish import prompt_trace as T

            return json.dumps({"ok": True, "items": T.recent(20),
                               "stats": T.stats()}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

    @Slot(result=str)
    def model_health(self) -> str:
        """最近一次模型调用是否失败（给设置页显示"她为什么没回"）。"""
        try:
            from bigfish import replier as R

            return json.dumps({"ok": True, "last": R.last_error()},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

    @Slot(str, result=str)
    def prompt_trace_sections(self, path: str) -> str:
        """一份快照的段落表：每段座位 / 段名 / 字数。

        界面用它画「字数花在哪」—— 只有总字数的时候，
        看到 system 8000 字也不知道是哪一段涨的。
        """
        try:
            from bigfish import prompt_trace as T

            if not path or not os.path.isfile(path):
                return json.dumps({"ok": False, "error": "文件不在了",
                                   "sections": []}, ensure_ascii=False)
            return json.dumps({"ok": True, "sections": T.sections_of(path)},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc),
                               "sections": []}, ensure_ascii=False)

    @Slot(str, result=str)
    def prompt_trace_open(self, path: str) -> str:
        """打开一份快照；传空串打开目录。"""
        try:
            from bigfish import prompt_trace as T

            p = path or T.trace_dir()
            if not os.path.exists(p):
                return json.dumps({"ok": False, "error": "文件不在了"},
                                  ensure_ascii=False)
            if sys.platform == "win32":
                os.startfile(p)  # noqa: S606
            return json.dumps({"ok": True, "path": p}, ensure_ascii=False)
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
            # 文件名给了就打开这个文件，目录才打开目录。
            # 以前一律打开 dirname，点「打开 journal」只会停在文件夹，
            # 用户还得自己从一堆日期里找那一篇。
            if sys.platform == "win32":
                os.startfile(p)  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", p])
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

            # 同样要 CREATE_NO_WINDOW：GUI 进程没有控制台，不加就会闪黑框
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=25,
                               creationflags=getattr(subprocess,
                                                      "CREATE_NO_WINDOW", 0))
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

    @Slot(result=str)
    def open_wechat(self) -> str:
        """确保微信**进程**在跑（不负责把窗口拽到前台）。

        ⚠ 以前这里是 ``FindWindowW + ShowWindow`` —— 拽出来的是残留窗口：
          微信收在托盘后主窗的 HWND 还在，但 Qt 那头已经不处理输入，
          于是"窗口出现了，点它没反应、X 也关不掉"（2026-11-08 用户实测）。
          真要恢复只能靠微信自己的托盘图标；程序能做的是把没跑的进程拉起来。
        """
        try:
            from bigfish.wechat_sender import start_weixin_process

            ok, msg = start_weixin_process()
            self.log_message.emit(msg, "in" if ok else "err")
            return json.dumps({"ok": ok, "msg": msg}, ensure_ascii=False)
        except Exception as exc:
            msg = f"打开微信失败: {exc}"
            self.log_message.emit(msg, "err")
            return json.dumps({"ok": False, "msg": msg}, ensure_ascii=False)

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
        """查有没有新版。

        ⚠ 当前版本**只认 `bigfish.__version__`**。
          以前用的是 config.json 里的 `update.current`，而那份配置里写死了
          "0.1.0" —— 程序走到 0.4.1 了它还是 0.1.0，于是拿 0.1.0 去比
          Release 的 tag，0.4.0 就会显示成"有新版"。用户点了更新，
          装回来还是同一个版本。

        ⚠ 仓库也**不再要求用户自己配**。零配置能用是底线：
          `update.repo` 留空就用内置的 DEFAULT_REPO，fork 的人填自己的。
        """
        from bigfish import __version__ as _ver
        try:
            from bigfish.updater import (DEFAULT_REPO, Updater,
                                         normalize_repo, read_update_config)

            uc = read_update_config(CONFIG_PATH)
            repo = normalize_repo(uc.get("repo") or "") or DEFAULT_REPO
            current = _ver

            cache = os.path.join(_APP_DIR, ".tmp", "update")
            up = Updater(repo, current, cache,
                         enabled=bool(uc.get("enabled", True)))
            info = up.check()
            info["current"] = current
            info["repo"] = repo
            self._update_info = info
            return json.dumps(info, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"has_update": False, "error": str(exc),
                               "current": _ver},
                              ensure_ascii=False)

    @Slot(result=str)
    def apply_update(self) -> str:
        """下载新版并安排替换。调用后建议用户手动关闭程序。"""
        from bigfish import __version__ as _ver
        try:
            from bigfish.updater import (DEFAULT_REPO, Updater,
                                         normalize_repo, read_update_config)

            info = getattr(self, "_update_info", None)
            if not info or not info.get("has_update"):
                return json.dumps({"ok": False, "error": "没有可用的更新"},
                                  ensure_ascii=False)

            uc = read_update_config(CONFIG_PATH)
            repo = normalize_repo(uc.get("repo") or "") or DEFAULT_REPO
            cache = os.path.join(_APP_DIR, ".tmp", "update")
            up = Updater(repo, _ver, cache)

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
        from bigfish import providers as _P
        from bigfish import scopes as _scopes
        from bigfish.replier import ModelConfig

        c = load_json_config()
        ar = c.get("auto_reply") or {}
        mc = ModelConfig.load()
        ready, why = mc.status()
        return json.dumps({
            # 模型接入的完整状态。界面不再直接编辑 endpoint ——
            # 用户只需要「选一家 + 粘 Key」，地址和协议由预设给。
            "llm": {
                "channel": mc.channel,
                "base_url": mc.base_url or mc.preset.base_url,
                "model": mc.model or mc.eff_model,
                "api_key": mc.api_key,
                "protocol": mc.eff_protocol,
                "temperature": mc.temperature,
                "max_tokens": mc.max_tokens,
                "timeout": mc.timeout,
                "eff_base_url": mc.eff_base_url,
                "url": mc.url,
                "ready": ready,
                "status": why,
                "needs_key": mc.needs_key,
                "preset_name": mc.preset.name,
                "note": mc.preset.note,
                "key_hint": mc.preset.key_hint,
                "doc": mc.preset.doc,
                "suggest": list(mc.preset.models),
            },
            "channels": _P.channels(),
            "protocols": [{"id": p, "label": _P.PROTOCOL_LABEL[p]}
                          for p in _P.PROTOCOLS],
            "interval": ar.get("interval", 1.2),
            # 读**生效值**（scopes 三层，这里不带会话 → 全局视角）。
            # 不读原始 ar 字段：用户要知道"现在真正是什么"，
            # 而不是"文件里写着什么"。两者从 7.x 起就可能不是一回事。
            "max_reply_chars": _scopes.get(c, "max_reply_chars"),
            "quiet_hours": list(_scopes.get(c, "quiet_hours") or ()),
            "allow_self": bool(_scopes.get(c, "allow_self",
                                      chat_id=SELF_TEST_CHAT)),
            "group_reply": bool(_scopes.get(c, "group_reply")),
            "group_allow_all": list(_scopes.get(c, "group_allow_all") or ()),
        }, ensure_ascii=False)

    @Slot(str, result=str)
    def save_config(self, payload: str) -> str:
        try:
            from bigfish import providers as _P

            d = json.loads(payload)
            c = load_json_config()

            # 模型接入写进 llm 段，同时清掉老的顶层字段 ——
            # 留着会和 llm 打架（replier 优先读 llm，老字段变成没人看的
            # 僵尸配置，用户改它不生效，跟 CONFIG_PATH 那个坑同一个形状）。
            llm = d.get("llm")
            if isinstance(llm, dict):
                old = c.get("llm") if isinstance(c.get("llm"), dict) else {}
                merged = {
                    "channel": str(llm.get("channel")
                                   or old.get("channel")
                                   or _P.DEFAULT_CHANNEL),
                    "base_url": str(llm.get("base_url", old.get("base_url", ""))),
                    "model": str(llm.get("model", old.get("model", ""))),
                    "api_key": str(llm.get("api_key", old.get("api_key", ""))),
                    "protocol": str(llm.get("protocol", old.get("protocol", ""))),
                    "temperature": float(llm.get("temperature",
                                                 old.get("temperature", 1.0))),
                    "max_tokens": int(llm.get("max_tokens",
                                              old.get("max_tokens", 2600))),
                    "timeout": float(llm.get("timeout",
                                             old.get("timeout", 90.0))),
                }
                c["llm"] = merged
                for k in ("endpoint", "model", "api_key", "temperature",
                          "max_tokens", "timeout"):
                    c.pop(k, None)

            ar = c.setdefault("auto_reply", {})
            ar["interval"] = float(d.get("interval", 1.2))
            ar["chats"] = self.watching or ar.get("chats", [])

            # ── 五个行为字段写**新路径**（scopes）──
            #
            # 规格 C1 §4.1：读时兼容旧路径，写时只写新路径。
            # 旧的 auto_reply.* 保留在文件里不动，方便回滚到 0.5.0。
            #
            # ⚠ allow_self 是例外：它写**会话层**，不写 global。
            #   这个开关的语义是「只对文件传输助手生效的自测通道」，
            #   写成 global 就等于"所有会话都能回应自己" ——
            #   那正是 11-04 事故的形态。界面上这个勾选框代表的就是
            #   filehelper 那一个会话，所以写 scopes.chat["filehelper"]。
            from bigfish import scopes as _scopes

            _scopes.set_global_value(
                c, "max_reply_chars", int(d.get("max_reply_chars", 300)))
            _scopes.set_global_value(
                c, "quiet_hours",
                [str(x) for x in (d.get("quiet_hours") or [])])
            _scopes.set_global_value(
                c, "group_reply", bool(d.get("group_reply", True)))
            _scopes.set_global_value(
                c, "group_allow_all",
                [str(x) for x in (d.get("group_allow_all") or [])])
            # 显式写 true/false，不留 None：留 None 会被旧的全局 true 重新点亮。
            _scopes.set_chat_value(
                c, SELF_TEST_CHAT, "allow_self",
                bool(d.get("allow_self", False)))

            save_json_config(c)
            return json.dumps({"ok": True})
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    # ── 声音（插件：侧边栏「声音」页） ──
    #
    # 开关在 plugins.json（勾选「声音」插件），接入参数在 config.json 的
    # voice 段。两处分开的理由：开关属于「她要不要这个能力」，Key/地址属于
    # 「这台机器怎么连」，后者要跟配置一起备份，但不该跟着插件配置导出。

    @Slot(result=str)
    def get_voice_devices(self) -> str:
        try:
            from bigfish import voice_audio_out as _ao
            from bigfish import voice_driver as _drv

            pkg = _drv.package_status()
            return json.dumps({
                "ok": True,
                "outputs": _ao.list_output_devices(),
                "inputs": _ao.list_input_devices(),
                "cable": _ao.cable_installed(),
                "package": pkg,
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

    @Slot(result=str)
    def install_vb_cable(self) -> str:
        try:
            from bigfish import voice_driver as _drv

            return json.dumps(_drv.install(), ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

    @Slot(result=str)
    def open_voice_sound_settings(self) -> str:
        try:
            from bigfish import voice_driver as _drv

            return json.dumps(_drv.open_sound_settings(), ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

    @Slot(result=str)
    def get_voice_config(self) -> str:
        try:
            from bigfish import plugins
            from bigfish import vault
            from bigfish.voice import VoiceSettings

            plugins.ensure_loaded()
            s = VoiceSettings.from_config(path=CONFIG_PATH)
            cache = Path(vault.resolve_data_root()) / "voice" / "cache"
            files = list(cache.glob("*.wav")) if cache.exists() else []
            return json.dumps({
                "ok": True,
                "enabled": plugins.is_enabled("voice"),
                "settings": {
                    "engine": s.engine,
                    "base_url": s.base_url,
                    "path": s.path,
                    "api_key": s.api_key,
                    "model": s.model,
                    "voice": s.voice,
                    "language": s.language,
                    "speed": s.speed,
                    "guidance": s.guidance,
                    "volume": s.volume,
                    "output_format": s.output_format,
                    "profile": s.profile,
                    "device": s.device,
                    "hotkey": s.hotkey,
                    "timeout": s.timeout,
                    "cache_mb": s.cache_mb,
                    "max_turn_chars": s.max_turn_chars,
                    "policy": s.policy,
                },
                "ready": bool(s.api_key and s.voice),
                "cache": {
                    "files": len(files),
                    "mb": round(sum(f.stat().st_size for f in files)
                                / 1048576, 2),
                },
                "models": ["breeze-tts-2-multilingual", "breeze-tts-2"],
                "devices": _voice_devices(),
                "profiles": [
                    {"id": "bright_young", "name": "薄荷（明亮少女音）"},
                    {"id": "", "name": "通用（不按音色调整）"},
                ],
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

    @Slot(str, result=str)
    def save_voice_config(self, payload: str) -> str:
        try:
            from bigfish.voice import VoiceSettings
            from bigfish.voice_policy import VoicePolicy

            d = _parse_payload(payload)
            c = load_json_config()
            old = c.get("voice") if isinstance(c.get("voice"), dict) else {}
            s = VoiceSettings.from_config(old)
            # Key 留空 = 不改。避免用户只改语速时把 Key 清掉。
            for key in ("engine", "base_url", "path", "model", "voice",
                        "language", "output_format", "profile", "device",
                        "hotkey"):
                if key in d and str(d[key]).strip():
                    setattr(s, key, str(d[key]).strip())
            if str(d.get("api_key") or "").strip():
                s.api_key = str(d["api_key"]).strip()
            for key in ("speed", "guidance", "volume", "timeout"):
                if d.get(key) not in (None, ""):
                    setattr(s, key, float(d[key]))
            for key in ("cache_mb", "max_turn_chars"):
                if d.get(key) not in (None, ""):
                    setattr(s, key, int(d[key]))
            if not s.voice:
                return json.dumps({"ok": False, "error": "音色 ID 不能为空"},
                                  ensure_ascii=False)

            c["voice"] = {
                "engine": s.engine,
                "base_url": s.base_url,
                "path": s.path,
                "api_key": s.api_key,
                "model": s.model,
                "voice": s.voice,
                "language": s.language,
                "speed": max(0.5, min(2.0, s.speed)),
                "guidance": max(1.0, min(10.0, s.guidance)),
                "volume": max(0.01, min(2.0, s.volume)),
                "output_format": s.output_format,
                "profile": s.profile,
                "device": s.device,
                "hotkey": s.hotkey,
                "timeout": max(5.0, min(180.0, s.timeout)),
                "cache_mb": max(0, min(5000, s.cache_mb)),
                "max_turn_chars": max(20, min(1000, s.max_turn_chars)),
                "policy": VoicePolicy.from_config(
                    d.get("policy") if isinstance(d.get("policy"), dict)
                    else old.get("policy")).as_dict(),
            }
            save_json_config(c)
            return json.dumps({"ok": True, "settings": c["voice"]},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

    @Slot(str, result=str)
    def test_voice(self, payload: str) -> str:
        """测试 Key/地址/模型：只 GET /v1/models，不合成、不花积分。"""
        import threading
        import uuid

        try:
            from bigfish.voice import VoiceEngine, VoiceSettings

            d = _parse_payload(payload)
            s = VoiceSettings.from_config(path=CONFIG_PATH)
            for key in ("engine", "base_url", "path", "model", "voice",
                        "language", "profile"):
                if d.get(key) not in (None, ""):
                    setattr(s, key, str(d[key]))
            if str(d.get("api_key") or "").strip():
                s.api_key = str(d["api_key"]).strip()
            s.enabled = True
            tid = uuid.uuid4().hex[:10]
            self._tests[tid] = {"done": False, "ok": False,
                                "text": "正在连接声音模型…"}
            if len(self._tests) > 20:
                for k in list(self._tests)[:-20]:
                    self._tests.pop(k, None)

            def work() -> None:
                try:
                    h = VoiceEngine(s).health(timeout=20.0)
                    ok = bool(h.get("ok") and h.get("model_ok"))
                    text = ("连接正常" if ok
                            else str(h.get("reason") or "连接失败"))
                    if h.get("hint"):
                        text += "｜" + str(h["hint"])
                    self._tests[tid] = {"done": True, "ok": ok,
                                        "text": text,
                                        "models": h.get("models") or []}
                except Exception as exc:  # noqa: BLE001
                    self._tests[tid] = {
                        "done": True, "ok": False,
                        "text": f"{type(exc).__name__}: {exc}"}

            threading.Thread(target=work, name="bigfish-voice-test",
                             daemon=True).start()
            return json.dumps({"ticket": tid})
        except Exception as exc:
            return json.dumps({"done": True, "ok": False, "text": str(exc)},
                              ensure_ascii=False)

    @Slot(result=str)
    def clear_voice_cache(self) -> str:
        try:
            from bigfish import vault

            cache = Path(vault.resolve_data_root()) / "voice" / "cache"
            n = 0
            if cache.exists():
                for p in list(cache.glob("*.wav")) + list(cache.glob("*.json")):
                    try:
                        p.unlink()
                        n += 1
                    except OSError:
                        pass
            return json.dumps({"ok": True, "deleted": n},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)},
                              ensure_ascii=False)

    # ── 测试连接 / 获取模型列表 ──
    #
    # 两个都是「真发一次网络请求」，都要等 0.5~25 秒。Qt 的 slot 跑在 GUI
    # 线程，同步做会把窗口冻住。所以统一走「开线程 + 票据轮询」：
    #   xxx() 立刻返回 ticket → 前端每秒 poll_xxx(ticket) → done 了就显示。
    @Slot(str, result=str)
    def test_model(self, payload: str) -> str:
        import threading
        import uuid

        d = _parse_payload(payload)
        tid = uuid.uuid4().hex[:10]
        self._tests[tid] = {"done": False, "ok": False, "text": "正在连接…"}
        if len(self._tests) > 20:
            for k in list(self._tests)[:-20]:
                self._tests.pop(k, None)

        def work() -> None:
            try:
                from bigfish.replier import ModelConfig, test_connection

                cfg = ModelConfig(
                    channel=str(d.get("channel") or "custom"),
                    base_url=str(d.get("base_url") or ""),
                    model=str(d.get("model") or ""),
                    api_key=str(d.get("api_key") or ""),
                    protocol=str(d.get("protocol") or ""),
                    temperature=float(d.get("temperature") or 1.0),
                    max_tokens=int(d.get("max_tokens") or 2600),
                )
                ok, text = test_connection(cfg, timeout=25.0)
                self._tests[tid] = {"done": True, "ok": ok, "text": text}
            except Exception as exc:
                self._tests[tid] = {
                    "done": True, "ok": False,
                    "text": f"{type(exc).__name__}: {exc}"}

        threading.Thread(target=work, name="bigfish-test",
                         daemon=True).start()
        return json.dumps({"ticket": tid})

    @Slot(str, result=str)
    def fetch_models(self, payload: str) -> str:
        """拉这家「你这把 Key 能用哪些模型」。

        这一步是必须的：预设里写的模型 ID 只是样本，会过期；而
        「你这把 Key 能用哪些」只有对方知道（免费额度、区域、企业版都不一样）。
        """
        import threading
        import uuid

        d = _parse_payload(payload)
        tid = uuid.uuid4().hex[:10]
        self._fetches[tid] = {"done": False, "ok": False,
                              "text": "正在获取…", "models": []}
        if len(self._fetches) > 20:
            for k in list(self._fetches)[:-20]:
                self._fetches.pop(k, None)

        def work() -> None:
            try:
                from bigfish.replier import ModelConfig, list_models

                cfg = ModelConfig(
                    channel=str(d.get("channel") or "custom"),
                    base_url=str(d.get("base_url") or ""),
                    model=str(d.get("model") or ""),
                    api_key=str(d.get("api_key") or ""),
                    protocol=str(d.get("protocol") or ""),
                )
                ok, models, text = list_models(cfg, timeout=20.0)
                self._fetches[tid] = {"done": True, "ok": ok,
                                      "text": text, "models": models}
            except Exception as exc:
                self._fetches[tid] = {
                    "done": True, "ok": False, "models": [],
                    "text": f"{type(exc).__name__}: {exc}"}

        threading.Thread(target=work, name="bigfish-models",
                         daemon=True).start()
        return json.dumps({"ticket": tid})

    @Slot(str, result=str)
    def poll_test(self, ticket: str) -> str:
        r = self._tests.get(ticket or "")
        if not r:
            return json.dumps({"done": True, "ok": False, "text": "这次测试已失效"},
                              ensure_ascii=False)
        return json.dumps(r, ensure_ascii=False)

    @Slot(str, result=str)
    def poll_fetch(self, ticket: str) -> str:
        r = self._fetches.get(ticket or "")
        if not r:
            return json.dumps({"done": True, "ok": False, "models": [],
                               "text": "这次获取已失效"}, ensure_ascii=False)
        return json.dumps(r, ensure_ascii=False)

    # ── 启停 ──
    @Slot(result=str)
    def start_engine(self) -> str:
        if self.worker and self.worker.isRunning():
            return json.dumps({"ok": True, "note": "已在运行"})

        if not self.watching:
            # 兜底：配置里明明有，就别让用户去"取消再勾一次"。
            self.watching = self._watching_from_config()
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
        # 停止后 recv/reply 是上一轮的累计值，不该继续挂在「未启动」旁边
        # 像实时数字。下一轮 start_engine 会从 0 重新计。
        self.recv_count = 0
        self.reply_count = 0

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

    class SkipItem(Exception):
        """这一项在当前环境里测不了（例如没提权）。不算失败。

        源码版的 `tools/selftest.py` 早就有 SKIP，打包版一直没有 ——
        于是同一次自检，源码版 23 全绿、打包版「20 通过 / 2 失败」，
        用户看到的第一印象是「装完就是坏的」。
        测不了 ≠ 坏了，这个区分必须显示出来。
        """

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
        except SkipItem as exc:
            dt = time.time() - t0
            results.append((name, "SKIP", str(exc), dt))
            print(f"  [SKIP] {name}  ({dt:.2f}s)")
            if str(exc):
                print(f"         -> {exc}")
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
            msg = str(boot["error"])
            if "不可写" in msg:
                # 「不可写」最容易被误读成"程序坏了"，实际最常见的是
                # 安全软件拦了**刚编译出来的 exe** 写盘（同一个目录
                # 用 python 跑是能写的，我们实测过）。给一条能动手的建议，
                # 比只报 errno 有用。
                msg += ("｜若是刚更新过程序：安全软件可能拦了新 exe 写盘，"
                        "把 BigFish.exe 加进白名单，或右键以管理员身份运行再试一次")
            raise RuntimeError(msg)

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

        prompt_on = [p for p in items
                     if "prompt" in (p.get("scopes") or [p.get("scope")])
                     and p["enabled"]]
        secs = P.prompt_sections({})
        orders = [s[0] for s in secs]

        # ⚠ 以前这里断言「注入段数 == 已启用插件数」。加入
        #   **有条件注入**的插件之后这条就不成立了：
        #     life（生活）  还没有 life.json → 只说时间、不说状态，
        #                   极端情况下整段为空；此时不该算它没注入
        #   不说话是设计，不是坏了。所以改成「必注的必须在」+「不能凭空多出来」。
        must = ("life", "say", "guard")
        order_of = {p["id"]: p["order"] for p in prompt_on}
        missing = [pid for pid in must
                   if order_of.get(pid) not in set(orders)]
        assert not missing, (
            f"必须注入的段没出现：{missing}（本轮注入 {orders}）")
        assert len(secs) <= len(prompt_on), (
            f"注入 {len(secs)} 段，比已启用的 {len(prompt_on)} 个还多")
        assert orders == sorted(orders), f"注入顺序没按身份序：{orders}"

        on = [p["title"] for p in items if p["enabled"]]
        quiet = [p["title"] for p in prompt_on
                 if p["order"] not in set(orders)]
        return (f"{len(items)} 个插件，已启用 {len(on)} 个"
                f"（{'、'.join(on) or '无'}）；本轮注入 {len(secs)} 段"
                f"（{', '.join(str(o) for o in orders) or '—'}）"
                + (f"；这次没有内容可说的：{'、'.join(quiet)}" if quiet else ""))
    item("插件", t_plugins)

    # 资源两份一致：打包版把 ui/ 和 bridge/ 放进 _internal/，
    # 而程序找 bridge 时 **exe 同级优先**（bridge_client._find_bridge）。
    # 部署时只刷新了一份，就会变成"包内是新的、生效的是旧的" ——
    # 症状是记忆检索里汉字全变成 U+FFFD，而「记忆 库里 N 条」照样是绿的。
    def t_assets():
        import hashlib as _h
        import sys as _sys

        if not getattr(_sys, "frozen", False):
            return "源码态只有一份，跳过"

        from bigfish.paths import app_root, res_root

        def _digest(path):
            try:
                with open(path, "rb") as f:
                    return _h.sha1(f.read()).hexdigest()[:10]
            except Exception:
                return ""

        bad = []
        checked = 0
        for rel in ("ui/index.html", "ui/app.js", "bridge/bridge.mjs"):
            parts = rel.split("/")
            inner = os.path.join(res_root(), *parts)
            outer = os.path.join(app_root(), *parts)
            if not os.path.isfile(outer):
                continue          # 便携版允许只放 _internal 一份
            checked += 1
            if _digest(inner) != _digest(outer):
                bad.append(rel)
        assert not bad, (
            "这些资源「exe 同级」那份和包内那份不一致：" + "、".join(bad)
            + "。程序读的是 exe 同级那份，所以生效的是旧文件 —— "
              "重跑 tools/deploy.py，别手工替换。")
        return f"{checked} 个资源两份一致"
    item("资源", t_assets)

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

    # ── 0.6.0：重试 / 快照 / 降级（纯单元，不需要网络）──
    # 这三条都是"出问题时才起作用"的机制：平时不显眼，坏了就是
    # "她有时候不回""她突然失忆"。必须在打包版里验一次。
    def t_retry():
        from bigfish import replier as R

        orig_call, orig_backoff = R._call_once, R.RETRY_BACKOFF
        calls = {"n": 0}

        def fake(cfg, system, user):
            calls["n"] += 1
            if calls["n"] == 1:
                raise R.ModelError("boom", status_code=500,
                                   retryable=True, kind="http")
            return "在呢"

        R._call_once = fake              # type: ignore[assignment]
        R.RETRY_BACKOFF = (0.0, 0.0, 0.0)  # type: ignore[assignment]
        try:
            cfg = R.ModelConfig()
            cfg.channel = "selftest"
            cfg.base_url = "http://127.0.0.1:1/v1"
            cfg.status = lambda: (True, "")   # type: ignore[method-assign]
            out = R.call_model(cfg, "s", "u")
            assert out == "在呢" and calls["n"] == 2, \
                f"重试没生效：{out!r} / 调了 {calls['n']} 次"
        finally:
            R._call_once, R.RETRY_BACKOFF = orig_call, orig_backoff  # type: ignore
        return "500 重试一次后成功"
    item("模型重试", t_retry)

    def t_trace():
        import shutil
        import tempfile

        from bigfish import prompt_trace as T

        tmp = tempfile.mkdtemp(prefix="selftest-trace-")
        orig_root, orig_cfg = T._data_root, T._config
        T._data_root = lambda: tmp        # type: ignore[assignment]
        T._config = lambda: {"enabled": True, "keep": 10}  # type: ignore
        try:
            class _C:
                channel = "selftest"
                eff_protocol = "openai"
                eff_model = "m"
                eff_base_url = "http://127.0.0.1:1"
                temperature = 1.0
                max_tokens = 100
                api_key = "sk-SECRET"

            p = T.record(cfg=_C(), system="s", user="u", response="r",
                         elapsed_ms=1, meta={"chat": "selftest"})
            assert p, "没写快照"
            end = time.time() + 3
            while time.time() < end and not os.path.isfile(p):
                time.sleep(0.05)
            assert os.path.isfile(p), "快照没落盘"
            with open(p, "r", encoding="utf-8") as f:
                raw = f.read()
            assert "sk-SECRET" not in raw, "快照里出现了 api_key"
        finally:
            T._data_root, T._config = orig_root, orig_cfg  # type: ignore
            shutil.rmtree(tmp, ignore_errors=True)
        return "写入正常，未泄漏 api_key"
    item("请求快照", t_trace)

    def t_degrade():
        import shutil
        import tempfile

        from bigfish import memory as M

        tmp = tempfile.mkdtemp(prefix="selftest-degrade-")
        try:
            items, level = M.search_with_fallback(
                os.path.join(tmp, "none.sqlite3"), "b", "x")
            assert level == "empty" and items == [], f"缺库没降级：{level}"
            db = os.path.join(tmp, "m.sqlite3")
            ok, err = M.ensure_schema(db)
            assert ok, err
            _items, level2 = M.search_with_fallback(db, "b", "x")
            assert level2 == "ok", f"正常库被判降级：{level2}"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return "缺库→empty，正常库→ok"
    item("记忆降级", t_degrade)

    # 自环防护：这个模块坏了的表现是"她会自己跟自己聊"，启动不报错、
    # 界面正常、只有聊天时才出事 —— 必须在自检里验一次。
    def t_loop():
        from bigfish.loopguard import LoopGuard

        class _M:
            def __init__(self, chat, text, is_self):
                self.chat_id, self.text, self.is_self = chat, text, is_self

        # 生产默认：自己发的一律丢
        g = LoopGuard(allow_self=True)
        v = g.admit(_M("wxid_dad", "我自己发的", True))
        assert not v.allow and v.reason == "self", f"私聊没拦住：{v}"

        # 自测通道还能用
        g2 = LoopGuard(allow_self=True)
        assert g2.admit(_M("filehelper", "自测消息", True)).allow, "filehelper 被拦了"

        # 记账 + 熔断
        g2.note_sent("自测消息")
        assert not g2.admit(_M("filehelper", "自测消息", True)).allow, "echo 没认出"

        g3 = LoopGuard(trip=4)
        tripped = [g3.note_reply("c") for _ in range(4)]
        assert tripped == [False, False, False, True], f"熔断点不对：{tripped}"

        # 对方的消息永远放行
        g4 = LoopGuard(allow_self=True)
        g4.note_sent("一句她刚说的话")
        assert g4.admit(_M("wxid_dad", "一句她刚说的话", False)).allow, \
            "对方复述被误杀了"

        return "私聊拦住 / 自测放行 / 第4轮熔断 / 对方不误杀"
    item("自环", t_loop)

    # 更新：只验"配置读得通、仓库名收拾得对"，不联网。
    # 自检要能离线跑 —— 联网的那次留给用户点「检查」。
    def t_update():
        from bigfish import __version__ as _v
        from bigfish.updater import (DEFAULT_REPO, Updater, normalize_repo,
                                     read_update_config)

        uc = read_update_config(CONFIG_PATH)
        repo = normalize_repo(uc.get("repo") or "") or DEFAULT_REPO
        assert "/" in repo, f"仓库名不合法：{repo!r}"
        # 完整 URL 也要能收拾成 owner/repo
        assert normalize_repo("https://github.com/a/b.git") == "a/b"
        up = Updater(repo, _v, os.path.join(_APP_DIR, ".tmp", "update"))
        assert up.repo == repo
        src = "配置里填的" if (uc.get("repo") or "").strip() else "内置默认"
        return f"{repo}（{src}）｜当前 v{_v}"
    item("更新", t_update)

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
        ok, why = c.status()
        # c.endpoint 是老属性，已经没有了 —— 报错信息要用 status，
        # 它会说清缺的是 Key、地址还是模型 ID。
        assert ok, f"模型未就绪：{why}"
        r = call_model(c, "你叫大肥鱼。", "回两个字")
        assert r
        return f"{c.preset.name} · {c.eff_model} -> {r[:20]!r}"
    item("模型", t_model)

    # 5. 数据库
    disk_db = {}

    def t_db():
        from bigfish.wechat import WeChatReader

        # ⚠ 优先用配置里那个账号。
        #   无参 WeChatReader() 会按数据库 mtime 自动挑 —— 这台机器上同时
        #   登录着三个微信号，自检就挑到了没用过的那个，打出一行别人的昵称，
        #   看着像"接错号了"。自检要验的是配置的那条路，不是任意一条。
        acc = str((load_json_config().get("auto_reply") or {}).get("account") or "")
        try:
            r = WeChatReader(account=acc).open() if acc else WeChatReader().open()
        except Exception:
            r = WeChatReader().open()
        disk_db["r"] = r
        info = r.self_info()
        tag = "" if acc else "（未配置账号，按 mtime 自动挑）"
        return (f"{info.get('nick_name')}{tag}，"
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
            # 测不了，不是坏了 —— 记 SKIP，别让用户以为装失败了。
            raise SkipItem("非管理员，UIA 需要提权（右键 → 以管理员身份运行）")
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
    nskip = sum(1 for r in results if r[1] == "SKIP")
    print()
    print(HR)
    print(f"  通过 {nok} / 失败 {nfail}"
          + (f" / 跳过 {nskip}" if nskip else ""))
    if nfail:
        for name, st, detail, dt in results:
            if st == "FAIL":
                print(f"    {name}: {detail}")
        print("  ✗ 有失败项")
        return 1
    print("  ✓ 全部通过" + ("（有跳过项，见上文 ->）" if nskip else ""))
    print(HR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
