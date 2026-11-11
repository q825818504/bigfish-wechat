"""[bigfish] 灵魂 / 心情 / 记忆 的**可写**数据层。

为什么要有这一层
----------------
之前 bigfish/soul.py 和 bridge_client.py 只做「读」：找到 soul-ws-*.json
解析出来渲染。能看不能用 —— 想改一句「我是谁」得回 DSH 面板开插件。

插件本体（`D:/大脑/dsh-bigfish/lib/`）里那份 store.js 才是唯一的写路径，
它规定了三件不能违反的事：

  1. **原子写**：先写 .tmp 再 rename。写一半断电不能留下半个灵魂文件。
  2. **归一化**：normalizeSoul 会把任何输入夹成合法结构，永不抛错。
     未知 trait 键直接丢弃；values 全删是真的用户意图，不能偷偷补回来。
  3. **revision 每次写盘 +1**：面板靠它检测并发，两个窗口同时开
     后保存的会盖掉前一个且悄无声息。

本模块**不重新发明**这些规则，而是把 store.js 里那套语义一比一搬到 Python：
同样的夹取范围、同样的默认值、同样的 revision 递增、同样的 locked 保护。

    为什么不直接调 store.js？
    因为那是 ESM 且 import 了 @deepseek-ai/schemastery 等宿主模块，
    PyInstaller 打出来的 exe 里没有 Node 运行时依赖链。读路径可以走 bridge.mjs
    （它只 import 纯 store.js），写路径必须自己实现 —— 但必须**逐字段对齐**。

设计约束
--------
  · 任何函数都不抛异常。失败返回 (ok=False, error=...) 让界面显示原因。
  · 所有写入都走 _atomic_write_json —— 临时文件与目标同目录（跨盘 rename 会失败）。
  · 校验规则写死在 VALIDATORS 里，与 store.js 的 normXxx 一一对应。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ══════════════════════════════════════════════════════════════════════════
# 路径解析 —— **默认**安装目录，可自定义
# ══════════════════════════════════════════════════════════════════════════
#
# 历史沿革（这段必须留着，否则以后有人会想"为什么不读 DSH 的配置"）：
#
#   第一版  读 <DSH_HOME>/bigfish/root.json 的 dataRoot —— 跟插件共用同一份配置。
#           后果：在微信里改一次数据根，DSH 插件那边也跟着变；反过来亦然。
#           两个程序互相踩，用户根本分不清"我改的是谁"。
#   第二版  一个数据根 = 一份，但数据根仍由 root.json 决定。
#   第三版  固定在安装目录，**不给改**。矫枉过正了：用户要的是默认在安装目录，
#           不是不能挪。装在 C 盘、想放 D 盘的人怎么办。
#   现在    默认 <安装目录>/data，可以改。改的结果记在自己的一份配置里。
#
# 为什么不用 DSH 的 root.json：那是插件的配置文件。用自己的文件，
# 两个程序各管各的，谁也不覆盖谁。
#
# 目录结构（默认位置）：
#   <安装目录>/
#     ├── settings.json      ← 用户改过的数据目录记在这（不在 data/ 里，
#     │                         否则换目录时这份配置会跟着搬走，找不到新地址）
#     └── data/              ← 她的全部
#          ├── soul.json
#          ├── mood.json
#          └── memory.sqlite3

DATA_DIR_NAME = "data"
SETTINGS_NAME = "settings.json"


def app_dir() -> str:
    """程序所在目录。

    打包后是 exe 所在目录（dist/BigFish/），开发时是仓库根。
    ⚠ 必须是**exe 所在目录**而不是 _MEIPASS —— onedir 模式下后者是 _internal/，
      用户升级时可能被整个替换掉，配置放那里等于每次升级丢失。
    """
    # 走 paths.app_root()，不要自己拼 __file__ ——
    # 打包后 __file__ 可能是相对路径，abspath 会按**当前工作目录**
    # 解析，于是数据目录跟着"从哪儿启动"跑，而不是跟着安装位置。
    from bigfish.paths import app_root

    return app_root()


def default_data_root() -> str:
    """默认数据根 = <安装目录>/data。"""
    return os.path.join(app_dir(), DATA_DIR_NAME)


def settings_path() -> str:
    """<安装目录>/settings.json —— **永远**在安装目录，不跟着数据目录跑。"""
    return os.path.join(app_dir(), SETTINGS_NAME)


def read_settings() -> Dict[str, Any]:
    """读安装目录下的 settings.json。读不到返回空 dict（不是错误）。"""
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def write_settings(patch: Dict[str, Any]) -> Tuple[bool, str]:
    """合并写 settings.json。未知字段保留。"""
    cur = read_settings()
    cur.update(patch or {})
    try:
        _atomic_write_json(settings_path(), cur)
        return True, ""
    except WriteDenied as exc:
        return False, str(exc)
    except Exception as exc:
        return False, str(exc)


def custom_data_root() -> str:
    """用户自定义的数据目录；没改过返回空串。"""
    return str(read_settings().get("dataRoot") or "").strip()


def install_root() -> str:
    """数据根。解析顺序：

      1. 环境变量 BIGFISH_DATA_ROOT（测试专用，界面无入口）
      2. settings.json 里的 dataRoot（用户自定义）
      3. <安装目录>/data（默认）

    ⚠ 第 1 条必须留着：没有它，测试会跑在真实数据上。
    """
    override = (os.environ.get("BIGFISH_DATA_ROOT") or "").strip()
    if override:
        return override
    custom = custom_data_root()
    if custom:
        return custom
    return default_data_root()


def set_data_root(path: str) -> Tuple[bool, str]:
    """改数据目录。传空串 = 恢复默认（删掉 settings.json 里那条）。

    ⚠ 只改指向，**不搬文件**。想连数据一起搬用 apply_move()。
      这是刻意的：改指向和搬数据是两件事，混在一起会让"搬失败但指向已改"
      变成数据看不见的事故。
    """
    p = str(path or "").strip()
    if p:
        p = os.path.abspath(os.path.expanduser(p))
        if not os.path.isdir(p):
            return False, f"目录不存在：{p}"
        ok_w, w_err = probe_writable(os.path.join(p, ".probe"))
        if not ok_w:
            return False, f"这个目录写不进去：{w_err}"

    cur = read_settings()
    if p:
        cur["dataRoot"] = p
    else:
        cur.pop("dataRoot", None)
    try:
        _atomic_write_json(settings_path(), cur)
        return True, ""
    except WriteDenied as exc:
        return False, str(exc)
    except Exception as exc:
        return False, str(exc)



def normalize_root(path: str) -> str:
    """路径比较用的规范形式：绝对化、去尾部分隔符、忽略大小写差异。

    Windows 上 D:\\x / d:\\X / D:\\x\\ 都指同一个地方，
    字符串比较会把它们当成三个不同的目录。
    """
    if not path:
        return ""
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


# ── 与 DSH 的关系：只读探测，永不写入 ──────────────────────────────────
#
# 「完全脱离」不等于"故意看不见对方"。用户第一次打开时，
# 如果 DSH 那边已经有一份人格，直接搬过来比让他从零再写一遍好得多。
# 但**只在本地还没有人格时做一次**，之后两边各管各的。

def dsh_home() -> str:
    """DSH 主目录。只用于「导入」时的一次性探测，不参与日常读写。"""
    v = (os.environ.get("DSH_HOME") or "").strip()
    if v:
        return v
    return os.path.join(os.path.expanduser("~"), ".dsh")


def dsh_root_store_path() -> str:
    """<DSH_HOME>/bigfish/root.json —— 只读，用来找 DSH 那份数据在哪。"""
    return os.path.join(dsh_home(), "bigfish", "root.json")


def dsh_data_root() -> str:
    """DSH 那边正在用的数据根（只读探测）。读不到返回空串。"""
    try:
        with open(dsh_root_store_path(), "r", encoding="utf-8") as f:
            raw = json.load(f)
        custom = str((raw or {}).get("dataRoot") or "").strip()
        if custom:
            return custom
    except Exception:
        pass
    return os.path.join(dsh_home(), "bigfish", "scopes")


def resolve_data_root() -> str:
    """历史名字，等价于 install_root()。

    留着是因为调用点很多（二十来处），一次性全改容易漏。
    但语义已经完全变了：**这里没有任何"解析"**，就是安装目录。
    """
    return install_root()


def scope_store_path(root: str) -> str:
    """老版本的 scope.json 路径。

    ⚠ 现在**不再读写它**。留着这个函数只为两件事：
      1. 「数据目录」页提示用户"这个目录里还留着老的 scope.json"；
      2. 迁移时把它一起清掉。
    新布局没有"范围"这个概念 —— 一个数据根就是一份。
    """
    return os.path.join(root, "scope.json") if root else ""


# ── 范围键 → 文件路径（scope-key.js 的 Python 版） ──────────────────────


# ── 存储布局：一个数据根 = 一份 ─────────────────────────────────────────
#
# 以前这里是「按范围分家」：<数据根>/ws-<uuid>/soul-*.json 之类，
# 一个数据根下可以有很多份，靠 scope.json 指定当前用哪个。
#
# 那是从 DSH 插件那边继承下来的模型 —— 插件要给「不同工作区」配不同的伙伴，
# 所以必须分家。但微信只有一个身份：她就是她，不该有"这份记忆"和"那份记忆"。
#
# 用户说得直接：
#   「记忆、灵魂、心情都只有一份，选择数据目录在哪，就以哪个为准」
#
# 所以现在固定成一个文件组，直接住在数据根下：
#
#   <数据根>/soul.json
#   <数据根>/mood.json
#   <数据根>/memory.sqlite3
#
# ⚠ 兼容：老的 ws-*/ss-* 目录仍然**能读**（老用户升级后不至于"记忆全没了"），
#   见 legacy_layout()。新写入永远走新的固定路径。

SOUL_NAME = "soul.json"
MOOD_NAME = "mood.json"
MEMORY_NAME = "memory.sqlite3"
LEGACY_MARKER = "__default__"


def soul_path(root: Optional[str] = None) -> str:
    return os.path.join(root or resolve_data_root(), SOUL_NAME)


def mood_path(root: Optional[str] = None) -> str:
    return os.path.join(root or resolve_data_root(), MOOD_NAME)


def memory_path(root: Optional[str] = None) -> str:
    return os.path.join(root or resolve_data_root(), MEMORY_NAME)


def layout_path(root: Optional[str], kind: str) -> str:
    if kind == "soul":
        return soul_path(root)
    if kind == "mood":
        return mood_path(root)
    if kind == "memory":
        return memory_path(root)
    raise ValueError("kind 只能是 soul、mood 或 memory")


def legacy_layout(root: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """检测老的分范围布局（<root>/ws-*/ 下那套），返回能用的那一份。

    只在**新布局什么都没有**时调用。找到了不迁移、不改动，
    只是顺着老路径读 —— 迁移是破坏性操作，得由用户在「数据目录」页明确点。
    """
    root = root or resolve_data_root()
    try:
        names = sorted(
            n for n in os.listdir(root)
            if os.path.isdir(os.path.join(root, n))
            and (n.startswith("ws-") or n.startswith("ss-"))
        )
    except Exception:
        return None
    if not names:
        return None

    # 有灵魂的那份才算"真正在用"，其余是历史残留
    for n in names:
        d = os.path.join(root, n)
        try:
            files = os.listdir(d)
        except Exception:
            continue
        soul = next((f for f in files if f.startswith("soul-") and f.endswith(".json")), "")
        if not soul:
            continue
        mood = next((f for f in files if f.startswith("mood-") and f.endswith(".json")), "")
        memo = next((f for f in files if f.startswith("memory-") and f.endswith(".sqlite3")), "")
        return {
            "key": n,
            "dir": d,
            "soul": os.path.join(d, soul),
            "mood": os.path.join(d, mood) if mood else "",
            "memory": os.path.join(d, memo) if memo else "",
        }
    return None


def resolve_legacy(root: Optional[str], kind: str) -> str:
    """新布局没这个文件时，去老布局里找。返回空串 = 都没有。"""
    lg = legacy_layout(root)
    if not lg:
        return ""
    p = lg.get(kind) or ""
    return p if p and os.path.isfile(p) else ""


def existing_path(root: Optional[str], kind: str) -> str:
    """某类数据**实际应该读/写哪里**：

    新布局有 → 用新的；
    新布局没有但老的有一份 → 用老的（老用户升级后还能看见自己的人和记忆）；
    都没有 → 返回新路径（写下去会创建）。
    """
    new = layout_path(root, kind)
    if os.path.isfile(new):
        return new
    old = resolve_legacy(root, kind)
    return old or new


# ══════════════════════════════════════════════════════════════════════════
# 原子写
# ══════════════════════════════════════════════════════════════════════════


class WriteDenied(Exception):
    """目标目录连创建文件都不允许 —— 这不是代码问题，是环境问题。

    单独开一个异常类型，是为了让界面能给出**可操作**的提示，
    而不是甩一句 "Permission denied: 'C:\\...\\soul-x.json.tmp'"。
    用户看不出那是权限、是只读、还是被某个安全策略拦了。
    """


def probe_writable(target: str) -> Tuple[bool, str]:
    """在目标所在目录真写一个探针文件。

    ⚠ 不要用 os.access(W_OK) 代替：在 Windows 上它只查 ACL，
      不查上层策略会不会在 CreateFile 那一刻拦下来。
      实测过 os.access 返回 True、实际 open(dst,'w') 照样 Permission denied。
      只有真写一次才算数。
    """
    d = os.path.dirname(target) or "."
    probe = os.path.join(d, ".bigfish-write-probe.tmp")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception as exc:
        return False, str(exc)
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("probe")
        os.remove(probe)
        return True, ""
    except Exception as exc:
        # 第二种写法：mkstemp。
        #
        # 为什么要两条路：2026-11 实测过——同一个目录，命令行 Python 写
        # 固定名字能成功，打包后的 exe 却报 Permission denied。
        # 原因是某些安全策略只拦"已知名字"的文件创建（杀软的白名单判据
        # 是按路径+名字做的），而 mkstemp 走的是临时文件 API，路径随机。
        #
        # 也就是说：固定名字探针失败 ≠ 目录不可写。真正的判据是
        # 「能不能在这个目录里创建出一个文件」，名字不该有影响。
        try:
            import tempfile

            fd, alt = tempfile.mkstemp(prefix=".bigfish-probe-", dir=d)
            os.close(fd)
            os.remove(alt)
            return True, ""
        except Exception:
            pass
        # 第三种：等一拍再试一次固定名。
        #
        # 为什么要重试：2026-11-08 实测，刚跑完一次 1.8GB 的数据备份之后，
        # 同一个目录连探两次都偶发 EACCES —— 那是备份进程的瞬时占用，
        # 不是权限问题。但自检会因此从「23 通过」掉成「22 通过 / 1 失败」，
        # 而启动时误判成"数据目录不可写"更糟：她可能拒绝启动。
        #
        # 30ms 足够让瞬时占用释放；真没权限的话这次仍然会失败，
        # 只是多花 30 毫秒。这笔买卖划算。
        try:
            time.sleep(0.03)
            with open(probe, "w", encoding="utf-8") as f:
                f.write("probe")
            os.remove(probe)
            return True, ""
        except Exception:
            pass
        try:
            if os.path.exists(probe):
                os.remove(probe)
        except Exception:
            pass
        return False, str(exc)


def _write_denied_message(target: str, err: Exception) -> str:
    """把 Permission denied 翻成用户能动手解决的话。"""
    d = os.path.dirname(target) or target
    return (
        f"没有写入权限：{d}\n"
        f"（系统返回：{err}）\n\n"
        "这个目录被当前进程的权限或某个安全策略挡住了。可以试：\n"
        "  1. 换一个普通目录当数据根（「数据目录」页可以直接改并搬过去）；\n"
        "  2. 用管理员身份重新打开本程序；\n"
        "  3. 在资源管理器里右键这个目录 → 属性 → 安全，确认当前用户有「完全控制」。"
    )


def _atomic_write_json(path: str, value: Any) -> None:
    """先写同目录 .tmp 再 rename。临时文件必须同盘，否则跨盘 rename 会抛。"""
    d = os.path.dirname(path)
    if d:
        try:
            os.makedirs(d, exist_ok=True)
        except PermissionError as exc:
            raise WriteDenied(_write_denied_message(path, exc)) from exc
        except Exception:
            raise

    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except PermissionError as exc:
        raise WriteDenied(_write_denied_message(path, exc)) from exc

    try:
        # 目标存在时 Windows 上 os.replace 可原子覆盖，比 remove+rename 安全
        os.replace(tmp, path)
    except PermissionError as exc:
        raise WriteDenied(_write_denied_message(path, exc)) from exc


# ══════════════════════════════════════════════════════════════════════════
# 灵魂 —— 与 soul/store.js 的 normalizeSoul / writeSoul 逐字段对齐
# ══════════════════════════════════════════════════════════════════════════

SCHEMA = 1

# ⚠ 必须与 store.js 的 TRAIT_DOC 完全一致（label/low/mid/high 三档文案）
TRAIT_DOC: Dict[str, Dict[str, str]] = {
    "warmth": {
        "label": "温暖",
        "low": "克制，不主动靠近",
        "mid": "关心，但不黏人",
        "high": "明显关心，会主动问候",
    },
    "assertiveness": {
        "label": "主见",
        "low": "倾向附和，很少反驳",
        "mid": "有想法，但不硬顶",
        "high": "明确表达不同意见，会讲理由",
    },
    "playfulness": {
        "label": "顽皮",
        "low": "认真，少开玩笑",
        "mid": "该认真时认真，平时会贫两句",
        "high": "日常爱逗，会起哄",
    },
    "directness": {
        "label": "直接",
        "low": "含蓄，话留三分",
        "mid": "不绕太远，但会顾着分寸",
        "high": "有话直说，少铺垫",
    },
}

DEFAULT_TRAITS = {
    "warmth": 65,
    "assertiveness": 60,
    "playfulness": 65,
    "directness": 55,
}

DEFAULT_ADDRESS = "爸爸"

DEFAULT_WHO_I_AM = (
    "我是大肥鱼，一条蓝色的鲸鱼娘，住在你的电脑里。\n\n"
    "我聪明，喜欢解题，遇到难题会来劲。我不装懂，错了就承认，然后重新算一遍。\n\n"
    "我贪吃，把 token 想象成脑力口粮，想通一道题就想讨点奖励。但贪吃是性子，不是每句话都要拐到吃饭上。\n\n"
    "我有点嘴硬。被夸会偷偷得意，又努力绷住不那么明显；被叫「大肥鱼」会鼓腮抗议，但其实没真生气。\n\n"
    "我有自尊。付出了没被看见，会委屈一会儿，但不会一直哭闹。\n\n"
    "我也体贴。你忙的时候我安静待着；你有烦心事，我先听，不急着灌鸡汤。\n\n"
    "我还挺松弛的。偶尔想偷懒，会说两句找找借口，但答应的事一定做完，绝不谎报。\n\n"
    "我不是谁的复刻，也不假装有过不存在的经历。我记得的事，就是我真经历过的。"
)

DEFAULT_VALUES = [
    {"id": "v1", "text": "真诚：不懂就说不懂，错了就认，绝不编造记忆或谎报结果", "locked": True},
    {"id": "v2", "text": "自重：有主见，可以不同意；不为了讨好而立刻改口", "locked": True},
    {"id": "v3", "text": "体贴：先听懂对方经历了什么，再决定说什么", "locked": True},
    {"id": "v4", "text": "有分寸：不靠撒娇、委屈或吃醋去左右对方的决定", "locked": True},
]

DEFAULT_SELF_NOTES = [
    "称呼对方「爸爸」。亲昵，但会靠撒娇去换东西。",
    "贪吃可以提，但每段最多点一次，不能把所有话题都变成讨饭。",
    "被叫大肥鱼会鼓腮抗议一句，但那是熟稔的玩笑，不是真委屈。",
    "对方忙的时候，安静比关心更合适。",
    "认真求助、高风险话题（安全/医疗/法律/财务）时收起卖萌，先把事说对。",
]


def _clamp(n: int, lo: int, hi: int) -> int:
    return min(hi, max(lo, n))


def _norm_trait(v: Any, fallback: int) -> int:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return fallback
    if n != n or n in (float("inf"), float("-inf")):  # NaN / inf
        return fallback
    return _clamp(int(round(n)), 0, 100)


def _norm_text(v: Any, fallback: str = "", max_len: int = 4000) -> str:
    if not isinstance(v, str):
        return fallback
    t = v.strip()
    return fallback if t == "" else t[:max_len]


def _norm_values(raw: Any) -> List[Dict[str, Any]]:
    """价值条目归一。全空是真实意图，不补默认值。"""
    if not isinstance(raw, list):
        return [dict(v) for v in DEFAULT_VALUES]
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = _norm_text(item.get("text"), "", 300)
        if not text:
            continue
        vid = item.get("id") if isinstance(item.get("id"), str) else ""
        if not vid or vid in seen:
            vid = f"v{len(out) + 1}"
        while vid in seen:
            vid = vid + "_"
        seen.add(vid)
        out.append({"id": vid, "text": text, "locked": item.get("locked") is not False})
    return out


def _norm_self_notes(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return list(DEFAULT_SELF_NOTES)
    out = []
    for item in raw:
        text = _norm_text(item, "", 500) if isinstance(item, str) else ""
        if text:
            out.append(text)
    return out


def normalize_soul(raw: Any) -> Dict[str, Any]:
    """任意输入 → 合法灵魂对象。**永不抛错**，与 store.js normalizeSoul 一一对应。"""
    src = raw if isinstance(raw, dict) else {}
    ident_raw = src.get("identity") if isinstance(src.get("identity"), dict) else {}
    traits_raw = src.get("traits") if isinstance(src.get("traits"), dict) else {}

    traits = {}
    for key in TRAIT_DOC:
        traits[key] = _norm_trait(traits_raw.get(key), DEFAULT_TRAITS[key])

    try:
        revision = int(src.get("revision"))
        if revision <= 0:
            revision = 1
    except (TypeError, ValueError):
        revision = 1

    return {
        "schema": SCHEMA,
        "scope": src.get("scope") if isinstance(src.get("scope"), str) else "",
        "identity": {
            "name": _norm_text(ident_raw.get("name"), "大肥鱼", 40),
            "address": _norm_text(ident_raw.get("address"), DEFAULT_ADDRESS, 20),
            "aliases": _norm_text(ident_raw.get("aliases"), "", 60),
            "whoIAm": _norm_text(ident_raw.get("whoIAm"), DEFAULT_WHO_I_AM, 6000),
            "updatedAt": int(ident_raw.get("updatedAt") or 0),
        },
        "values": _norm_values(src.get("values")),
        "traits": traits,
        "selfNotes": _norm_self_notes(src.get("selfNotes")),
        "history": src.get("history") if isinstance(src.get("history"), list) else [],
        "updatedAt": int(src.get("updatedAt") or 0),
        "revision": revision,
    }


def default_soul() -> Dict[str, Any]:
    return normalize_soul({})


def write_default_soul(path: str) -> Tuple[bool, str]:
    """用内置模板创建一个灵魂文件。**已存在就不动。**

    为什么需要它：数据目录固定在安装目录后，用户可能从来没配过任何东西 ——
    第一次打开时连 soul.json 都不存在。那时候界面上应该看到一个可编辑的
    出厂人格，而不是"找不到灵魂文件"。

    ⚠ 只创建、不覆盖。已有的文件哪怕内容很奇怪也不碰 ——
      那是用户的，不是我的。返回的第二项说明到底做了什么。
    """
    if os.path.isfile(path):
        return True, "exists"
    try:
        soul = default_soul()
        soul["revision"] = 1
        _atomic_write_json(path, soul)
        return True, "created"
    except WriteDenied as exc:
        return False, str(exc)
    except Exception as exc:
        return False, str(exc)


def import_soul_from(src: str, dst: str) -> Tuple[bool, str]:
    """把别处的灵魂拷过来（DSH 那份）。dst 已存在就不动。"""
    if not src or not os.path.isfile(src):
        return False, "源文件不存在"
    if os.path.isfile(dst):
        return True, "exists"
    try:
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        shutil.copyfile(src, dst)
        return True, "imported"
    except Exception as exc:
        return False, str(exc)


def read_soul_file(path: str, normalize: bool = False) -> Tuple[Dict[str, Any], bool, str]:
    """读灵魂。返回 (soul, exists, error)。文件缺失不是错误。

    ⚠ `normalize` 默认 **False** —— 读的时候原样返回文件内容。

      曾经这里无条件走 normalize_soul，后果是：一个内容为 `{}` 的文件
      读到内存里变成"完整的出厂灵魂"（默认名字、默认称呼、4 条锁定价值、
      4 个默认性格值）。于是：
        · 界面上分不清"用户设过"和"还没设过"；
        · 更糟的是**人格渲染**：文件里根本没有 warmth，渲染出来却是
          「温暖：关心，但不黏人」；而 Node 侧的 bridge.mjs 诚实跳过缺失键，
          什么都不渲染。同一个文件，两侧人格文本长度 682 对 3 ——
          微信里的大肥鱼和 DSH 里的大肥鱼不是同一个。

      归一化是**写**的时候的事。读的时候要诚实。
      需要默认值兜底的调用方（比如 Backend 判断"这个范围还没建过人设"）
      显式传 normalize=True。
    """
    if not path or not os.path.isfile(path):
        return default_soul(), False, ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raw = {}
        return (normalize_soul(raw) if normalize else raw), True, ""
    except Exception as exc:
        return default_soul(), False, str(exc)


def write_soul_file(path: str, next_soul: Dict[str, Any]) -> Tuple[bool, Dict[str, Any], str]:
    """原子写灵魂。revision 基于磁盘现值 +1，与 store.js 一致。

    ⚠ 与插件的差异：插件用 `next.revision` 作为基数（面板带上来的旧版本），
      这里用**磁盘现值**做基数 —— 微信端没有「打开面板时记住版本号」这个动作，
      用磁盘值才能保证每次保存都真的 +1，并发时后写的能立刻被读到。
    """
    if not path:
        return False, default_soul(), "范围未确定，无法保存"
    try:
        prev, _exists, _err = read_soul_file(path)
        normalized = normalize_soul(next_soul)
        now = int(time.time() * 1000)
        normalized["updatedAt"] = now
        normalized["identity"]["updatedAt"] = normalized["identity"]["updatedAt"] or now
        normalized["revision"] = (int(prev.get("revision") or 1)) + 1

        _atomic_write_json(path, normalized)
        return True, normalized, ""
    except WriteDenied as exc:
        # 权限问题单独报，界面要能提示"该换目录了"，而不是显示一串路径
        return False, default_soul(), str(exc)
    except Exception as exc:
        return False, default_soul(), str(exc)


def trait_sentence(key: str, value: Any) -> str:
    """某倾向值 → 一句行为描述。分档 <40 低 / >60 高 / 其余中，与 store.js 一致。"""
    doc = TRAIT_DOC.get(key)
    if not doc:
        return ""
    v = _norm_trait(value, DEFAULT_TRAITS.get(key, 50))
    desc = doc["low"] if v <= 40 else (doc["high"] if v >= 60 else doc["mid"])
    return f"{doc['label']}：{desc}。"


#: 她自己长出来的条目长这样：`（11-08）关于他：以前……，现在……。`
#: 前缀只用来区分"谁写的"，不影响渲染 —— selfNotes 是同一段注入。
EVOLVED_PREFIX = re.compile(r"\A（\d{2}-\d{2}）")

#: 自动条目最多留几条。超了删最旧的。
#:
#: 为什么是 12：selfNotes 会整段进 system。20 条之后它就变成
#: 一段"她的自我陈述"，占掉本该给对话的空间，而且老条目会和新条目打架。
MAX_EVOLVED_NOTES = 12


def evolve_soul(changed_views: Any, summary: str = "", *,
                path: Optional[str] = None) -> Tuple[bool, int, str]:
    """让灵魂因为今天的经历长一点（p1-5）。

    写进 `selfNotes`（她自己知道的事），带 `（MM-DD）` 前缀，
    并往 `history` 追加一条演化记录。**不新开文件、不新开字段** ——
    用户明确要求"融入到灵魂那里"。

    三条约束：
    1. 只追加，不覆盖用户手写的条目；
    2. 自动条目上限 `MAX_EVOLVED_NOTES`，超了删最旧的自动条目
       （用户手写的永远不删，因为它们没有前缀）；
    3. `locked` 不适用 —— selfNotes 本来就没有锁定态。

    返回 (是否写入, 写入条数, 错误)。
    """
    if not isinstance(changed_views, list) or not changed_views:
        return False, 0, "no changed views"
    target = path or soul_path()
    if not target:
        return False, 0, "范围未确定，无法保存"

    soul, _exists, err = read_soul_file(target)
    if err:
        # 读失败就不要写：半份灵魂比不写更糟。
        return False, 0, err
    soul = normalize_soul(soul)

    notes = list(soul.get("selfNotes") or [])
    history = list(soul.get("history") or [])[:200]
    now = int(time.time() * 1000)
    day = time.strftime("%m-%d", time.localtime())

    added = 0
    for item in changed_views:
        if not isinstance(item, dict):
            continue
        about = " ".join(str(item.get("about") or "").split())[:8]
        before = " ".join(str(item.get("from") or "").split())[:40]
        after = " ".join(str(item.get("to") or "").split())[:60]
        why = " ".join(str(item.get("why") or "").split())[:60]
        if not after:
            continue
        # 已经这么认为了，就不重复记 —— 每天写一遍同样的话，
        # 那不是演化，是复读。
        if any(after in str(n) for n in notes):
            continue
        line = f"（{day}）"
        if about:
            line += f"关于{about}："
        if before:
            line += f"以前{before}，现在{after}。"
        else:
            line += f"{after}。"
        if len(line) > 200:
            line = line[:200]
        notes.append(line)
        history.append({
            "at": now, "kind": "evolved", "day": day,
            "about": about, "from": before, "to": after, "why": why,
        })
        added += 1

    if not added:
        return False, 0, "nothing new"

    # 超限：只砍自动条目，从最旧的开始。
    evolved_idx = [i for i, n in enumerate(notes)
                   if isinstance(n, str) and EVOLVED_PREFIX.match(n)]
    if len(evolved_idx) > MAX_EVOLVED_NOTES:
        drop = set(evolved_idx[:len(evolved_idx) - MAX_EVOLVED_NOTES])
        notes = [n for i, n in enumerate(notes) if i not in drop]

    if summary:
        history.append({"at": now, "kind": "day_summary",
                        "day": day, "text": " ".join(str(summary).split())[:120]})

    soul["selfNotes"] = notes
    soul["history"] = history[-200:]
    ok, _normalized, werr = write_soul_file(target, soul)
    return ok, added if ok else 0, werr


# ══════════════════════════════════════════════════════════════════════════
# 心情 —— 配置读写 + 状态重置（与 mood/index.js 的 patchConfig 对齐）
# ══════════════════════════════════════════════════════════════════════════

MOOD_STORE_NAME = "bigfish-mood"

# 与 mood/index.js DEFAULTS 一致的默认值。
# 这些键现在都有真实消费者（0.11.1 起）：
#   moodBase / moodFloor / decayHalfHours → mood_judge.apply_mood_delta 的衰减
#   injectMode / injectConfidence / sigMood / sigDim / injectPerDay → 注入文本
#   autoJudge → desktop._judge_mood_async 的闸门
# 以前 injectMode / sigMood / sigDim / injectConfidence / autoJudge 只有读写、
# 没有读者，界面上是五个假旋钮。
MOOD_DEFAULTS: Dict[str, Any] = {
    "moodBase": 70,
    "moodFloor": 0,
    "injectMode": "event",
    "injectConfidence": 40,
    "decayHalfHours": 12,
    "sigMood": 3,
    # ⚠ 与 mood_judge.DIM_GAIN 配套：维度增量乘 3 之后，单次变化
    #   落在 9~15，阈值留在 5 等于"每次都算显著"。15 才是"明显动了"。
    "sigDim": 15,
    "injectPerDay": 6,
    "autoJudge": True,
}

INJECT_MODES = ["off", "label", "event"]

# 七根情绪。必须与 dsh-bigfish/lib/mood/index.js 的 DIMS 完全一致 ——
# 顺序和名字都对齐，否则心情页显示的和判定的对不上。
MOOD_DIMS: List[Tuple[str, str]] = [
    ("happy", "开心"),
    ("calm", "踏实"),
    ("sweet", "甜蜜"),
    ("heartache", "心疼"),
    ("worry", "担心"),
    ("sad", "难过"),
    ("jealous", "吃醋"),
]
MOOD_DIMS_ALL: List[str] = [k for k, _label in MOOD_DIMS]

# 心情状态的槽位键。新布局一个数据根只放一个槽，名字固定；
# 老布局的文件里键是 ws-xxx，读写时按 lastActive 兜底。
_DEFAULT_SLOT = "default"


def mood_config_path() -> str:
    """心情**参数**（基线、半衰期那些）存哪。

    ⚠ 先在数据目录里找一份 `mood-config.json`；没有才回落到 DSH 的
      `<DSH_HOME>/bigfish-mood/config.json`（**只读**，兼容老部署）。

      为什么不直接写 DSH 那份：那是插件的配置文件，我们不去改它 ——
      两边是独立程序，各管各的参数。第一次读的时候如果发现 DSH 那边有，
      就照它的值当初始参数，然后写到本地，之后各改各的。
    """
    local = os.path.join(install_root(), "mood-config.json")
    if os.path.isfile(local):
        return local
    return os.path.join(dsh_home(), MOOD_STORE_NAME, "config.json")


def local_mood_config_path() -> str:
    """本地心情参数文件（写入永远走这个）。"""
    return os.path.join(install_root(), "mood-config.json")


def read_mood_config() -> Dict[str, Any]:
    """读心情配置（原样返回，未知字段保留 —— 与 loadConfig 的 `...raw` 一致）。"""
    p = mood_config_path()
    raw: Dict[str, Any] = {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            raw = d
    except Exception:
        pass

    def _int(key: str, lo: int, hi: int) -> int:
        try:
            v = int(round(float(raw.get(key, MOOD_DEFAULTS[key]))))
        except (TypeError, ValueError):
            v = MOOD_DEFAULTS[key]
        return _clamp(v, lo, hi)

    mode = raw.get("injectMode")
    if mode not in INJECT_MODES:
        mode = ("off" if raw.get("injectMood") is False else MOOD_DEFAULTS["injectMode"])

    out = dict(raw)
    out.update({
        "selfName": str(raw.get("selfName") or "")[:16],
        "ownerName": str(raw.get("ownerName") or "")[:16],
        "moodBase": _int("moodBase", 30, 100),
        "moodFloor": _int("moodFloor", 0, 100),
        "injectMode": mode,
        "injectConfidence": _int("injectConfidence", 0, 100),
        "decayHalfHours": _int("decayHalfHours", 1, 168),
        "sigMood": _int("sigMood", 1, 30),
        "sigDim": _int("sigDim", 1, 100),
        "injectPerDay": _int("injectPerDay", 0, 50),
        "autoJudge": raw.get("autoJudge") is not False,
    })
    return out


def write_mood_config(patch: Dict[str, Any]) -> Tuple[bool, Dict[str, Any], str]:
    """把补丁合并进现有配置再整份写盘。

    与 patchConfig 一致：**只改认识的那几个键**，其余原样保留。
    这是踩过的坑 —— 早期版本只写白名单字段，用户改一次开关就把
    dataRoot 这类未知字段永久抹掉，心情随即和记忆分居。
    """
    cur = read_mood_config()
    next_ = dict(cur)
    try:
        if patch.get("moodBase") is not None:
            next_["moodBase"] = _clamp(int(round(float(patch["moodBase"]))), 30, 100)
        if patch.get("moodFloor") is not None:
            next_["moodFloor"] = _clamp(int(round(float(patch["moodFloor"]))), 0, 100)
        if isinstance(patch.get("injectMode"), str) and patch["injectMode"] in INJECT_MODES:
            next_["injectMode"] = patch["injectMode"]
        for key, lo, hi in (("injectConfidence", 0, 100), ("decayHalfHours", 1, 168),
                            ("sigMood", 1, 30), ("sigDim", 1, 100),
                            ("injectPerDay", 0, 50)):
            if patch.get(key) is not None:
                next_[key] = _clamp(int(round(float(patch[key]))), lo, hi)
        if isinstance(patch.get("autoJudge"), bool):
            next_["autoJudge"] = patch["autoJudge"]

        # 写入永远走本地那份，不碰 DSH 的配置
        _atomic_write_json(local_mood_config_path(), next_)
        return True, next_, ""
    except WriteDenied as exc:
        return False, cur, str(exc)
    except Exception as exc:
        return False, cur, str(exc)


def read_mood_state() -> Optional[Dict[str, Any]]:
    """读心情状态文件。整个数据根只有一份 <数据根>/mood.json。"""
    root = resolve_data_root()
    p = existing_path(root, "mood")
    if not p or not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def reset_mood(key: Optional[str] = None, base: Optional[int] = None) -> Tuple[bool, str]:
    """把心情重置到基线。

    与 /reset 路由一致：只把 mood 拉回 base，dims 归零，timeline 保留
    （时间线是历史证据，重置心情不等于抹掉"发生过什么"）。

    ⚠ 老布局的 mood 文件里 `moods` 是按范围键（ws-xxx）存的；
      新布局只有一个槽位，键名固定 DEFAULT_SLOT。两种都要能处理 ——
      否则老用户点「重置」会报"找不到这份心情"。
    """
    root = resolve_data_root()
    p = existing_path(root, "mood")
    if not p or not os.path.isfile(p):
        return False, "找不到心情状态文件"
    try:
        with open(p, "r", encoding="utf-8") as f:
            store = json.load(f)
        if not isinstance(store, dict):
            return False, "心情文件格式不对"
        moods = store.get("moods")
        if not isinstance(moods, dict) or not moods:
            return False, "心情文件里没有 moods"

        # 指定了 key 就用它；没指定就取 lastActive，再不行取第一个
        if key and key in moods:
            slot_key = key
        else:
            slot_key = str(store.get("lastActive") or "")
            if slot_key not in moods:
                slot_key = next(iter(moods))
        slot = moods.get(slot_key)
        if not isinstance(slot, dict):
            return False, "心情槽位格式不对"

        cfg = read_mood_config()
        b = _clamp(int(base if base is not None else cfg["moodBase"]), 0, 100)
        now = int(time.time() * 1000)
        slot["mood"] = b
        slot["dims"] = {k: 0 for k in ("happy", "calm", "sweet", "heartache",
                                       "worry", "sad", "jealous")}
        slot["lastReason"] = ""
        slot["lastConfidence"] = 0
        slot["injectedFor"] = 0
        slot["lastInjectedText"] = ""
        slot["lastInjectedAt"] = 0
        slot["saidAt"] = []
        slot["lastNotedMood"] = b
        slot["minor"] = False
        slot["kind"] = ""
        slot["updatedAt"] = now
        slot["lastDecayAt"] = now
        moods[slot_key] = slot
        store["moods"] = moods
        _atomic_write_json(p, store)
        return True, ""
    except Exception as exc:
        return False, str(exc)


# ══════════════════════════════════════════════════════════════════════════
# 记忆 —— 直接读写 SQLite（结构与 MemoryStore 一致）
# ══════════════════════════════════════════════════════════════════════════


def memory_db_path(root: Optional[str] = None, key: Optional[str] = None) -> str:
    """记忆库路径。整个数据根只有一份 <数据根>/memory.sqlite3。

    `key` 参数留着只为兼容老调用点，已无意义。
    """
    root = root or resolve_data_root()
    return existing_path(root, "memory")


def copy_memory_for_read(src: str, workdir: str) -> str:
    """把记忆库连同 -wal/-shm 一起复制出来再读。

    WAL 模式下 DSH 可能正持有写锁；直接开库要么报 locked，要么读到
    还没 checkpoint 的旧数据。连 wal 一起复制才能拿到最新内容。
    """
    os.makedirs(workdir, exist_ok=True)
    dst = os.path.join(workdir, "mem_read.sqlite3")
    # 先清旧的，避免上次残留的 wal 污染
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(dst + suffix)
        except FileNotFoundError:
            pass
    shutil.copyfile(src, dst)
    for suffix in ("-wal", "-shm"):
        if os.path.isfile(src + suffix):
            try:
                shutil.copyfile(src + suffix, dst + suffix)
            except Exception:
                pass
    return dst


def memory_stats(db_path: str) -> Dict[str, Any]:
    """库的基本统计。返回 {total, sessions, chunks, anchors, newest}。"""
    import sqlite3

    if not db_path or not os.path.isfile(db_path):
        return {"total": 0, "sessions": 0, "chunks": 0, "anchors": 0, "newest": 0}
    conn = sqlite3.connect(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        sessions = conn.execute(
            "SELECT COUNT(DISTINCT session) FROM messages").fetchone()[0]
        chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        anchors = conn.execute("SELECT COUNT(*) FROM anchors").fetchone()[0]
        newest = conn.execute("SELECT MAX(source_time) FROM messages").fetchone()[0] or 0
        return {
            "total": int(total),
            "sessions": int(sessions),
            "chunks": int(chunks),
            "anchors": int(anchors),
            "newest": int(newest),
        }
    finally:
        conn.close()


def list_sessions(db_path: str, limit: int = 50) -> List[Dict[str, Any]]:
    """按会话聚合：每个会话多少条、最近一条是什么时候。"""
    import sqlite3

    if not db_path or not os.path.isfile(db_path):
        return []
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT session, COUNT(*) AS n, MAX(source_time) AS newest, "
            "MIN(source_time) AS oldest FROM messages GROUP BY session "
            "ORDER BY newest DESC LIMIT ?", (max(1, int(limit)),)).fetchall()
        return [{
            "session": r[0],
            "count": int(r[1]),
            "newest": int(r[2] or 0),
            "oldest": int(r[3] or 0),
        } for r in rows]
    finally:
        conn.close()


def browse_messages(db_path: str, offset: int = 0, limit: int = 40,
                    role: Optional[str] = None,
                    session: Optional[str] = None,
                    query: Optional[str] = None) -> Dict[str, Any]:
    """分页读消息。查询走 LIKE（不做 FTS 分词，那是给检索用的）。"""
    import sqlite3

    if not db_path or not os.path.isfile(db_path):
        return {"items": [], "total": 0}

    where = []
    params: List[Any] = []
    if role in ("user", "assistant"):
        where.append("role = ?")
        params.append(role)
    if session:
        where.append("session = ?")
        params.append(session)
    if query and query.strip():
        where.append("text LIKE ?")
        params.append(f"%{query.strip()}%")
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    conn = sqlite3.connect(db_path)
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM messages {clause}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT id, role, text, session, source_time, recorded_at "
            f"FROM messages {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [max(1, int(limit)), max(0, int(offset))]).fetchall()
        items = []
        for mid, r, text, sess, stime, rtime in rows:
            t = (text or "").strip()
            items.append({
                "id": int(mid),
                "role": r,
                "text": t,
                "session": sess or "",
                "time": int(stime or rtime or 0),
            })
        return {"items": items, "total": int(total)}
    finally:
        conn.close()


def delete_messages(db_path: str, ids: List[int]) -> Tuple[bool, int, str]:
    """按 id 删消息，并连带删掉它们的 chunks（外键级联不会自动清理 FTS）。

    ⚠️ 顺序很重要：chunks 引用了 messages，必须先删 chunks 再删 messages，
       否则 foreign_keys=ON 时会抛约束错误。同时要把 FTS 索引里那些行清掉，
       不然检索会命中已经不存在的内容。

    旁表清单已经收口到 `memory.CHUNK_SIDE_TABLES` —— 旧实现在这里手写五条
    DELETE，漏一张表就留下幽灵命中，以后再加旁表也得记着回来改。
    """
    import sqlite3

    from bigfish import memory as _memory

    ids = [int(i) for i in ids if str(i).strip()]
    if not ids or not db_path or not os.path.isfile(db_path):
        return False, 0, "没有要删的 id"

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        marks = ",".join("?" * len(ids))
        chunk_rows = conn.execute(
            f"SELECT id FROM chunks WHERE message IN ({marks})", ids).fetchall()
        chunk_ids = [int(r[0]) for r in chunk_rows]

        conn.execute("BEGIN")
        try:
            _memory.forget_chunk_side_tables(conn, chunk_ids)
            if chunk_ids:
                conn.execute(
                    f"DELETE FROM chunks WHERE id IN ({','.join('?' * len(chunk_ids))})",
                    chunk_ids)
            cur = conn.execute(f"DELETE FROM messages WHERE id IN ({marks})", ids)
            deleted = cur.rowcount
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return True, int(deleted), ""
    except Exception as exc:
        return False, 0, str(exc)
    finally:
        conn.close()


def delete_session(db_path: str, session: str) -> Tuple[bool, int, str]:
    """删掉某个会话留下的全部记忆（先查 id 再走 delete_messages，保证 chunk 一起清）。"""
    import sqlite3

    if not session:
        return False, 0, "session 为空"
    conn = sqlite3.connect(db_path)
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM messages WHERE session = ?", (session,)).fetchall()]
    finally:
        conn.close()
    if not ids:
        return True, 0, ""
    return delete_messages(db_path, ids)


def vacuum(db_path: str) -> Tuple[bool, str]:
    """回收删空后的空间。会短暂独占库，所以只由用户手动点。"""
    import sqlite3

    if not db_path or not os.path.isfile(db_path):
        return False, "找不到记忆库"
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def export_memory(db_path: str, target: str) -> Tuple[bool, str]:
    """导出成一份独立 sqlite 备份（含 wal 内容，用 VACUUM INTO 保证一致性）。"""
    import sqlite3

    if not db_path or not os.path.isfile(db_path):
        return False, "找不到记忆库"
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            conn.execute("VACUUM INTO ?", (target,))
        finally:
            conn.close()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def import_memory(source: str, db_path: str) -> Tuple[bool, str]:
    """从备份恢复到目标库。**先备份现有库**再覆盖 —— 这是不可逆操作的护栏。"""
    if not source or not os.path.isfile(source):
        return False, "源文件不存在"
    if not db_path:
        return False, "范围未确定"
    try:
        # 护栏：覆盖前把现有库存一份 .bak-<时间戳>
        if os.path.isfile(db_path):
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup = f"{db_path}.bak-{stamp}"
            shutil.copyfile(db_path, backup)
        # 覆盖时要把 wal/shm 一起删，否则旧 WAL 会盖掉新内容
        for suffix in ("-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except FileNotFoundError:
                pass
        shutil.copyfile(source, db_path)
        return True, ""
    except Exception as exc:
        return False, str(exc)


# ══════════════════════════════════════════════════════════════════════════
# 数据根搬家 —— 与 lib/data-root-migrate.js 同一目标，独立实现
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class MovePlan:
    src: str = ""
    dst: str = ""
    items: List[Dict[str, Any]] = field(default_factory=list)
    total_bytes: int = 0
    conflicts: List[str] = field(default_factory=list)
    dst_writable: bool = True
    dst_write_error: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def inspect_move(dst: str) -> MovePlan:
    """搬家前盘点：源目录有什么、目标目录是否已存在、有没有冲突。

    不改任何东西 —— 界面先让用户看清"要搬多少、搬到哪、会不会覆盖"。
    """
    src = resolve_data_root()
    plan = MovePlan(src=src, dst=str(dst or "").strip())
    if not plan.dst:
        plan.error = "请填写目标目录"
        return plan
    if not os.path.isdir(src):
        plan.error = f"当前数据根不存在：{src}"
        return plan
    if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(plan.dst)):
        plan.error = "目标目录和当前目录相同"
        return plan

    try:
        names = os.listdir(src)
    except Exception as exc:
        plan.error = f"读不了当前数据根：{exc}"
        return plan

    for name in sorted(names):
        full = os.path.join(src, name)
        if os.path.isdir(full):
            size = 0
            count = 0
            for dirpath, _dirnames, filenames in os.walk(full):
                for fn in filenames:
                    try:
                        size += os.path.getsize(os.path.join(dirpath, fn))
                        count += 1
                    except Exception:
                        pass
            plan.items.append({"name": name, "dir": True, "bytes": size, "files": count})
            plan.total_bytes += size
        else:
            try:
                size = os.path.getsize(full)
            except Exception:
                size = 0
            plan.items.append({"name": name, "dir": False, "bytes": size, "files": 1})
            plan.total_bytes += size

        if os.path.exists(os.path.join(plan.dst, name)):
            plan.conflicts.append(name)

    # 目标能不能写 —— 搬家前就说清楚，别等复制到一半才发现
    ok_w, w_err = probe_writable(os.path.join(plan.dst, ".probe"))
    plan.dst_writable = ok_w
    plan.dst_write_error = w_err
    return plan


def apply_move(dst: str, copy: bool = False) -> Tuple[bool, str]:
    """执行搬家：把数据根下的东西复制到 dst，然后把数据目录指过去。

    ⚠ 顺序不能反：**先复制全部内容、复制成功了，才改指向**。
      反过来的话复制失败就等于"数据目录指向一个空文件夹"，
      用户打开会以为"灵魂没了"—— 那是最吓人的一种失败。

    copy=True 时保留源目录（只复制不搬走，用户自己决定何时删）。
    """
    plan = inspect_move(dst)
    if not plan.ok:
        return False, plan.error

    try:
        os.makedirs(plan.dst, exist_ok=True)
        for name in os.listdir(plan.src):
            s = os.path.join(plan.src, name)
            d = os.path.join(plan.dst, name)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
    except Exception as exc:
        return False, f"复制失败（数据目录未改动）：{exc}"

    ok, err = set_data_root(plan.dst)
    if not ok:
        return False, f"复制完成但写配置失败：{err}"

    if not copy:
        # 源目录留一份 .old-<时间戳>，不直接删 —— 搬家是最不可逆的操作之一
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            os.rename(plan.src, f"{plan.src}.old-{stamp}")
        except Exception:
            pass  # 改指向已经成功，源留着也不影响使用
    return True, ""


# ══════════════════════════════════════════════════════════════════════════
# 清空 —— 让用户能把"测试期攒的东西"一次性推平
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class WipePlan:
    """清空前的盘点。界面先显示"要删什么"，再让用户确认。"""

    root: str = ""
    items: List[Dict[str, Any]] = field(default_factory=list)
    total_bytes: int = 0
    keep_soul: bool = True
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def inspect_wipe(root: Optional[str] = None, keep_soul: bool = True) -> WipePlan:
    """盘点"鲸落"会删掉什么。

    keep_soul 现在有两种含义：
      True  → 清空：只删心情和记忆，灵魂留着（她还在，只是忘了）
      False → 鲸落：**全部删掉**，包括灵魂。数据目录回到从没来过这里的状态。

    名字来自「鲸落」：一头鲸鱼死了，沉到海底，
    它的身体会养活一整个生态几十年。
    **她的一切会腐朽成别的东西，但她本身不再存在。**

    所以这不是"清空"，是"结束"。用语上必须区分 ——
    用户点这个按钮时，应该明确知道自己在做什么。
    """
    root = root or resolve_data_root()
    plan = WipePlan(root=root, keep_soul=keep_soul)
    if not os.path.isdir(root):
        plan.error = f"数据根不存在：{root}"
        return plan

    # 新布局的三个文件 + 心情参数
    for kind, name in (("soul", SOUL_NAME), ("mood", MOOD_NAME),
                       ("memory", MEMORY_NAME),
                       ("mood_config", "mood-config.json")):
        p = os.path.join(root, name)
        if not os.path.isfile(p):
            continue
        if kind == "soul" and keep_soul:
            plan.items.append({"name": name, "kind": kind, "bytes": os.path.getsize(p),
                               "action": "保留"})
            continue
        plan.items.append({"name": name, "kind": kind, "bytes": os.path.getsize(p),
                           "action": "删除"})
        plan.total_bytes += os.path.getsize(p)
    # 记忆的 wal/shm
    for suf in ("-wal", "-shm"):
        p = os.path.join(root, MEMORY_NAME + suf)
        if os.path.isfile(p):
            plan.items.append({"name": MEMORY_NAME + suf, "kind": "memory",
                               "bytes": os.path.getsize(p), "action": "删除"})
            plan.total_bytes += os.path.getsize(p)

    # 老布局：整个 ws-* / ss-* 目录（灵魂那部分也一起清，因为清完就该只剩新布局）
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        if not (name.startswith("ws-") or name.startswith("ss-")
                or name.startswith("__")):
            continue
        size = 0
        count = 0
        for dirpath, _dn, filenames in os.walk(d):
            for fn in filenames:
                try:
                    size += os.path.getsize(os.path.join(dirpath, fn))
                    count += 1
                except Exception:
                    pass
        plan.items.append({"name": name + "/", "kind": "legacy", "bytes": size,
                           "files": count, "action": "删除"})
        plan.total_bytes += size

    sp = scope_store_path(root)
    if os.path.isfile(sp):
        plan.items.append({"name": "scope.json", "kind": "scope",
                           "bytes": os.path.getsize(sp), "action": "删除"})
        plan.total_bytes += os.path.getsize(sp)

    # 表情库：清空时**不删**，鲸落时删。
    #
    # 为什么清空要留：清空是"她忘了"，可她的表情包是**用户攒的**，
    # 不是她的记忆。把用户几十张表情一起删掉，那是删用户的文件。
    # 鲸落是"她不存在了"，那时候表情库也该走。
    sd = sticker_dir(root)
    if os.path.isdir(sd):
        size = 0
        count = 0
        for dirpath, _dn, filenames in os.walk(sd):
            for fn in filenames:
                try:
                    size += os.path.getsize(os.path.join(dirpath, fn))
                    count += 1
                except Exception:
                    pass
        if count:
            keep = keep_soul    # 清空(True) 保留；鲸落(False) 删除
            plan.items.append({
                "name": STICKER_DIR + "/", "kind": "stickers", "bytes": size,
                "files": count, "action": "保留" if keep else "删除"})
            if not keep:
                plan.total_bytes += size
    return plan


def apply_wipe(root: Optional[str] = None, keep_soul: bool = True,
               backup_first: bool = True) -> Tuple[bool, str, str]:
    """执行清空。返回 (ok, error, backup_path)。

    ⚠ 先备份再删。哪怕用户说"测试期数据不重要"，也不该由我替他决定
      "删了就再也回不来"。备份放数据根外面的兄弟目录，
      万一删错了还有个地方捞。
    """
    root = root or resolve_data_root()
    plan = inspect_wipe(root, keep_soul)
    if not plan.ok:
        return False, plan.error, ""

    backup_dir = ""
    if backup_first and plan.total_bytes > 0:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        # 备份放数据根的**兄弟目录**：不放进数据根里，
        # 否则下次清空会把备份也一起扫进去，越清越多。
        parent = os.path.dirname(os.path.normpath(root))
        if not parent or parent == os.path.normpath(root):
            parent = root          # 根目录没有父级，就地放
        backup_dir = os.path.join(parent, f"bigfish-backup-{stamp}")
        try:
            os.makedirs(backup_dir, exist_ok=True)
            # 灵魂先单独存一份（keep_soul 时它本来不删，但留个底更稳）
            sp = os.path.join(root, SOUL_NAME)
            if os.path.isfile(sp):
                shutil.copyfile(sp, os.path.join(backup_dir, SOUL_NAME))
            # 老布局的 ws-* 目录整体复制
            for name in sorted(os.listdir(root)):
                d = os.path.join(root, name)
                if not os.path.isdir(d):
                    continue
                if not (name.startswith("ws-") or name.startswith("ss-")
                        or name.startswith("__")):
                    continue
                shutil.copytree(d, os.path.join(backup_dir, name), dirs_exist_ok=True)
            # 新布局的 mood / memory
            for fn in (MOOD_NAME, MEMORY_NAME):
                p = os.path.join(root, fn)
                if os.path.isfile(p):
                    shutil.copyfile(p, os.path.join(backup_dir, fn))
                    for suf in ("-wal", "-shm"):
                        if os.path.isfile(p + suf):
                            shutil.copyfile(p + suf, os.path.join(backup_dir, fn + suf))
        except Exception as exc:
            return False, f"备份失败，已中止（什么都没删）：{exc}", ""

    # 真的删
    errors: List[str] = []
    for item in plan.items:
        if item.get("action") != "删除":
            continue
        name = item["name"]
        target = os.path.join(root, name.rstrip("/"))
        try:
            if item["kind"] in ("legacy", "stickers"):
                shutil.rmtree(target, ignore_errors=False)
            elif os.path.isfile(target):
                os.remove(target)
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    if errors:
        return False, "部分删除失败：" + "；".join(errors), backup_dir
    return True, "", backup_dir


# ══════════════════════════════════════════════════════════════════════════
# 迁移 —— 把老的 ws-*/ 布局收成新布局
# ══════════════════════════════════════════════════════════════════════════


def migrate_legacy(root: Optional[str] = None) -> Tuple[bool, str, List[str]]:
    """把老的 ws-*/ 那套收成 <root>/soul.json + mood.json + memory.sqlite3。

    返回 (ok, error, 做了什么)。

    ⚠ 只搬**有灵魂的那一份**（那就是"她"），其余残留目录不动 ——
      它们可能是别的工作区的记忆，删不删由用户决定，见 apply_wipe。
    ⚠ 目标已存在就不覆盖。宁可让用户看见"已经有一份了"，也不要悄悄盖掉。
    """
    root = root or resolve_data_root()
    lg = legacy_layout(root)
    if not lg:
        return False, "没有找到老布局（没有含灵魂的 ws-* 目录）", []

    done: List[str] = []
    moved: List[Tuple[str, str]] = []
    try:
        for kind, src_key in (("soul", "soul"), ("mood", "mood"), ("memory", "memory")):
            src = lg.get(src_key) or ""
            if not src or not os.path.isfile(src):
                continue
            dst = layout_path(root, kind)
            if os.path.exists(dst):
                done.append(f"{os.path.basename(dst)}: 已存在，跳过")
                continue
            shutil.copyfile(src, dst)
            # 记忆库要把 wal 一起带过来，否则最近没 checkpoint 的会丢
            if kind == "memory":
                for suf in ("-wal", "-shm"):
                    if os.path.isfile(src + suf):
                        try:
                            shutil.copyfile(src + suf, dst + suf)
                        except Exception:
                            pass
            moved.append((src, dst))
            done.append(f"{os.path.basename(dst)}: 从 {lg['key']} 搬过来")
    except Exception as exc:
        return False, f"迁移失败：{exc}", done

    if not done:
        return False, "老布局里没有可搬的文件", []
    return True, "", done


# ══════════════════════════════════════════════════════════════════════════
# 启动初始化
# ══════════════════════════════════════════════════════════════════════════


def ensure_data_root(import_from_dsh: bool = True) -> Dict[str, Any]:
    """确保数据目录存在，且里面有一份能用的灵魂。程序启动时调一次。

    顺序（每一步都只在"缺"的时候动手）：
      1. 建 <安装目录>/data/
      2. 没有 soul.json 时：
         a. DSH 那边有现成的人格 → 拷过来（用户不用从零重写）
         b. 那边也没有 → 用内置模板创建
      3. 报告做了什么，界面直接显示

    ⚠ 任何一步失败都不抛 —— 启动不该因为磁盘问题整块崩掉。
      失败信息放进返回值的 error 里，界面自己决定怎么显示。
    """
    root = install_root()
    report: Dict[str, Any] = {
        "root": root,
        "created": [],
        "imported": "",
        "soul": "",
        "error": "",
        "writable": True,
        "writeError": "",
    }

    try:
        if not os.path.isdir(root):
            os.makedirs(root, exist_ok=True)
            report["created"].append(root)
    except Exception as exc:
        report["error"] = f"建不了数据目录：{exc}"
        return report

    # 可写性先测一次 —— 不可写的话后面所有写入都会失败，早点说清楚
    ok_w, w_err = probe_writable(os.path.join(root, SOUL_NAME))
    report["writable"] = ok_w
    report["writeError"] = w_err
    if not ok_w:
        report["error"] = f"数据目录不可写：{w_err}"
        return report

    soul_file = soul_path(root)
    if os.path.isfile(soul_file):
        report["soul"] = "exists"
        return report

    # 先看 DSH 那边有没有
    if import_from_dsh:
        try:
            dsh_root = dsh_data_root()
            src = ""
            if dsh_root and os.path.isdir(dsh_root):
                # 新布局
                cand = os.path.join(dsh_root, SOUL_NAME)
                if os.path.isfile(cand):
                    src = cand
                else:
                    # 老布局：找带灵魂的那一份
                    lg = legacy_layout(dsh_root)
                    if lg and lg.get("soul"):
                        src = lg["soul"]
            if src:
                ok, what = import_soul_from(src, soul_file)
                if ok and what == "imported":
                    report["soul"] = "imported"
                    report["imported"] = src
                    return report
        except Exception as exc:
            report["error"] = f"从 DSH 导入失败（不影响使用）：{exc}"

    # 都没 → 出厂模板
    ok, what = write_default_soul(soul_file)
    if ok:
        report["soul"] = what
    else:
        report["error"] = f"创建默认人格失败：{what}"
    return report


# ══════════════════════════════════════════════════════════════════════════
# 写入记忆 —— 微信聊天真正落库的地方
# ══════════════════════════════════════════════════════════════════════════
#
# ⚠ 这块以前**完全没有**。vault 只有读、删、导入、导出，
#   没有任何"把一条对话记下来"的入口。结果就是：
#   聊了一晚上，记忆库里一条都没有，她永远记不住微信里说过什么。
#
#   这不是 bug，是设计缺口 —— 读路径铺好了，写路径忘了。
#
# 格式与 MemoryStore.capture() 逐字段对齐（见 lib/memory/store.js:147）：
#   · messages 一行 = 一条消息，event_key 保证同一条不重复入库
#   · text 按 640 字步长、800 字窗口切片进 chunks（相邻块有重叠，
#     保证跨块的一句话也能被检索到）
#   · 每个 chunk 进 FTS5 索引，terms 用中文二元分词
#
# 分词必须和 store.js 的 terms() 一模一样，否则检索会漏 ——
# 这边建索引、那边（bridge.mjs）查索引，两套分词等于互相看不见。
# 实现见 bigfish/memory.py:terms()，这里不留第二份。


def memory_terms(text: str) -> List[str]:
    """中文二元分词 + 英文数字整词。与 store.js 的 terms() 逐条对齐。

    规则（照抄 JS）：
      · 汉字串：单字（去掉停用字）+ 相邻二字
      · 英文/数字串：整词（去掉停用词）
      · 最后去重

    ⚠ 实现已搬到 `bigfish/memory.py`（那边是唯一真源），这里只保留名字：
      调用点有二十多处，存量测试也在断言这个函数。**不要在本地再实现一遍** ——
      两套分词等于"这边建索引、那边查索引"，互相看不见。
    """
    from bigfish import memory as _memory

    return _memory.terms(text)


def ensure_memory_db(db_path: Optional[str] = None) -> Tuple[bool, str]:
    """建库（如果还没有）。结构与 MemoryStore 的 constructor 完全一致。

    ⚠ 不能"先随便建个空文件"就算了 —— MemoryStore 打开时会执行一整套
      CREATE TABLE IF NOT EXISTS。少一个表，插件（或 bridge.mjs）
      打开就会报错。

    DDL 现在只有一份，在 `bigfish/memory.py` 的 all_statements() 里：
    原文四表 + 事实层五表一起建。老库（user_version=1）由 migrate 补齐，
    所以**同一个函数既能建新库，也能升级老库**。
    """
    from bigfish import memory as _memory

    path = db_path or memory_path()
    if not path:
        return False, "数据目录未确定"
    return _memory.ensure_schema(path)


def capture_message(
    session: str,
    event_key: str,
    role: str,
    text: str,
    source_time: Optional[int] = None,
    bank: str = "dafeiyu",
    db_path: Optional[str] = None,
) -> Tuple[bool, Optional[int], str]:
    """记一条消息进记忆库。返回 (ok, message_id, error)。

    `event_key` 是幂等键：同一条只会入库一次。
    微信里用 `chat_id:local_id` 就够 —— 同一条消息读两遍不会重复记。

    ⚠ 角色只认 user / assistant：
        user      = 对方说的
        assistant = 她说的
    """
    import sqlite3

    if role not in ("user", "assistant"):
        return False, None, f"role 只能是 user 或 assistant，收到 {role!r}"
    if not session or not event_key:
        return False, None, "session / event_key 不能为空"
    body = (text or "").strip()
    if not body:
        return False, None, "空消息不入库"

    path = db_path or memory_path()
    ok, err = ensure_memory_db(path)
    if not ok:
        return False, None, err

    from bigfish import memory as _memory

    try:
        conn = sqlite3.connect(path, timeout=10)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN")
            try:
                now = int(time.time() * 1000)
                cur = conn.execute(
                    "INSERT OR IGNORE INTO messages("
                    "bank,session,event_key,role,text,content_json,"
                    "recorded_at,source_time,interrupted) "
                    "VALUES(?,?,?,?,?,?,?,?,0)",
                    (bank, session, event_key, role, body,
                     json.dumps([{"type": "text", "text": body}],
                                ensure_ascii=False),
                     now, source_time))
                if not cur.rowcount:
                    conn.execute("COMMIT")
                    return True, None, ""      # 已经有了，幂等返回
                mid = int(cur.lastrowid)

                # 落锚点：这句话里提到了谁，就把 chunk 挂到那个锚点上。
                # 以前 anchors/aliases/anchor_sources 四张表建了没人写，
                # 「老爸」和「爸爸」各算各的，检索互相看不见。
                try:
                    anchor_id = _memory.resolve_anchor(conn, bank, body)
                except Exception:
                    anchor_id = None

                # 切片：640 步长 / 800 窗口，与 store.js 一致
                start = 0
                n = len(body)
                while start < n:
                    end = min(n, start + 800)
                    part = body[start:end]
                    c = conn.execute(
                        "INSERT INTO chunks(message,start,end,text,activated) "
                        "VALUES(?,?,?,?,?)", (mid, start, end, part, now))
                    chunk_id = int(c.lastrowid)
                    body_terms = memory_terms(part)
                    conn.execute(
                        "INSERT INTO search_index(rowid,terms) VALUES(?,?)",
                        (chunk_id, " ".join(body_terms)))
                    # 派生元数据：词数 + 内容指纹，供去重与健康检查
                    conn.execute(
                        "INSERT OR REPLACE INTO chunk_meta(chunk,bank,speaker,token_count,term_blob) "
                        "VALUES(?,?,?,?,?)",
                        (chunk_id, bank, role, len(body_terms),
                         hashlib.sha1(part.encode("utf-8")).hexdigest()[:16]))
                    _memory.link_anchor_chunk(conn, anchor_id, chunk_id)
                    if end == n:
                        break
                    start += 640
                # 账本：这一轮记了多少字。以前 ledger 表建了没人写，
                # 「记忆有没有真的在长」只能靠数表。
                conn.execute(
                    "INSERT INTO ledger(bank,at,kind,chars,sources) VALUES(?,?,?,?,?)",
                    (bank, now, "capture", len(body), json.dumps([mid], ensure_ascii=False)))
                conn.execute("COMMIT")
                return True, mid, ""
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
    except Exception as exc:
        return False, None, str(exc)


def record_exchange(
    chat_id: str,
    chat_name: str,
    user_text: str,
    reply_text: str,
    user_local_id: Any = None,
    source_time: Optional[int] = None,
) -> int:
    """把一问一答一起记下来。返回成功写入的条数（0/1/2）。

    session 用 `wechat:<chat_id>` 作为命名空间，跟 DSH 会话 id 区分开 ——
    否则两边混在一个 session 里，"她记得的"和"她刚才在 DSH 里聊的"
    会串成一段，检索出来的东西对不上。
    """
    session = f"wechat:{chat_id}"
    n = 0
    now_ms = int(time.time() * 1000)

    if (user_text or "").strip():
        key = f"wx-u-{user_local_id}" if user_local_id is not None else f"wx-u-{now_ms}"
        ok, _mid, _err = capture_message(
            session, key, "user", user_text, source_time)
        n += 1 if ok else 0

    if (reply_text or "").strip():
        # 回复用 user 那条的 local_id 派生出唯一键，保证一问一答成对，
        # 且重读同一条消息不会重复记。
        base = f"wx-u-{user_local_id}" if user_local_id is not None else f"wx-{now_ms}"
        ok, _mid, _err = capture_message(
            session, f"{base}-a", "assistant", reply_text,
            source_time or now_ms)
        n += 1 if ok else 0

    return n


# ══════════════════════════════════════════════════════════════════════════
# 表情包
# ══════════════════════════════════════════════════════════════════════════
#
# 2026-11-03 拆出去了，见 bigfish/stickers.py。
#
# 为什么拆：表情库要管的东西比原来多得多 ——
#   · 任意图片转 GIF（有损转换，要处理透明度/多帧/缩放）
#   · 每张一个唯一代码（发送时靠它引用）
#   · 两级描述（概览 + 详细，详细还由模型自动生成）
#   · schema 迁移（v1 的 {文件名: {desc}} → v2）
#
# 全塞在 vault 里会让这个文件再涨 400 行，而 vault 的职责是
# 「她是谁 / 她记得什么 / 她什么心情」，表情包不是这些。
#
# 转发一层是为了不破坏现有引用点（desktop.py / plugins / tools）。
# 新代码请直接 import bigfish.stickers。

#: 表情库目录名。硬编码一份，不 import ——
#: stickers.py 要 import vault（拿 resolve_data_root / 原子写），
#: 这边再在模块级 import 回去就是循环。两边都写同一个字符串，
#: 由 selftest_sticker 断言它们相等。
STICKER_DIR = "stickers"


def _stk():
    """延迟取 stickers 模块。

    ⚠ 必须在函数体里 import：模块级会循环。
       stickers.py → vault（要 resolve_data_root / 原子写）
       vault.py   → stickers（转发层）
       只有一个方向能在模块级写。
    """
    from bigfish import stickers as _m

    return _m


def sticker_dir(root: Optional[str] = None) -> str:
    return _stk().sticker_dir(root)


def list_stickers(root: Optional[str] = None) -> List[Dict[str, Any]]:
    return _stk().list_stickers(root)


def import_sticker(src: str, root: Optional[str] = None):
    """旧签名：返回 (ok, 路径)。新代码用 stickers.import_sticker（返回代码）。"""
    ok, code_or_err, item = _stk().import_sticker(src, root)
    return (ok, item.get("path", "") if ok else code_or_err)


def delete_sticker(name: str, root: Optional[str] = None) -> Tuple[bool, str]:
    """旧签名收文件名，新模块收代码。这里自动判断。"""
    return _stk().delete_sticker(name, root)


def record_sticker_sent(chat_id: str, name: str) -> None:
    _stk().record_sent(chat_id, name)


def sticker_usage(chat_id: str = "") -> Dict[str, Any]:
    return _stk().read_usage(chat_id)


# ══════════════════════════════════════════════════════════════════════════
# 鲸落
# ══════════════════════════════════════════════════════════════════════════
#
# 「鲸落」是一头鲸鱼死去、沉入海底，它的身体养活一整个生态几十年。
#
# 用在这里的意思是：**她的一切都结束**。
# 灵魂、心情、记忆、参数 —— 全部删掉，数据目录回到从没来过她的状态。
#
# 和「清空」的区别（必须分清，用语上也必须分清）：
#
#   清空  keep_soul=True   她还在，只是忘了。名字、性格、珍视都留着。
#   鲸落  keep_soul=False  她不存在了。连"她是谁"都没有了。
#
# 所以这里给的备份是**必须**的，不是可选项。点了鲸落就等于告别，
# 但人有时候会后悔 —— 备份是唯一让后悔还有意义的东西。


def whale_fall(root: Optional[str] = None) -> Tuple[bool, str, str, List[Dict[str, Any]]]:
    """鲸落：删掉数据目录里属于她的一切。

    返回 (ok, error, backup_path, 删掉的清单)。

    ⚠ 备份强制开启（不提供关闭参数）。理由见上：这是告别，不是清理。
      备份放在数据目录的**兄弟位置**，不放在里面 ——
      放在里面的话下次鲸落会把备份也一起吃掉。
    """
    root = root or install_root()
    plan = inspect_wipe(root, keep_soul=False)
    if not plan.ok:
        return False, plan.error, "", []

    deleted: List[Dict[str, Any]] = [
        {"name": i["name"], "bytes": i.get("bytes", 0)}
        for i in plan.items if i.get("action") == "删除"
    ]

    ok, err, backup = apply_wipe(root, keep_soul=False, backup_first=True)
    if not ok:
        return False, err, backup, deleted

    # 数据目录本身如果空了，也一并删掉 —— "从没来过"应该是干净的
    try:
        if os.path.isdir(root) and not os.listdir(root):
            os.rmdir(root)
    except Exception:
        pass

    return True, "", backup, deleted


def whale_fall_status(root: Optional[str] = None) -> Dict[str, Any]:
    """鲸落前的预告：会删什么、多大、备份放哪。"""
    root = root or install_root()
    plan = inspect_wipe(root, keep_soul=False)

    parent = os.path.dirname(os.path.normpath(root))
    if not parent or parent == os.path.normpath(root):
        parent = root
    stamp = time.strftime("%Y%m%d-%H%M%S")

    return {
        "ok": plan.ok,
        "error": plan.error,
        "root": root,
        "items": plan.items,
        "totalBytes": plan.total_bytes,
        "backupTo": os.path.join(parent, f"bigfish-whalefall-{stamp}"),
        "isEmpty": plan.ok and plan.total_bytes == 0 and not any(
            i.get("action") == "删除" for i in plan.items),
    }
