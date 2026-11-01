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

import glob
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ══════════════════════════════════════════════════════════════════════════
# 路径解析 —— 与 dsh-bigfish/lib/scope-store.js 完全同一套规则
# ══════════════════════════════════════════════════════════════════════════


def dsh_home() -> str:
    """DSH 主目录。每次现算，不缓存（用户可能改了环境变量）。"""
    v = (os.environ.get("DSH_HOME") or "").strip()
    if v:
        return v
    return os.path.join(os.path.expanduser("~"), ".dsh")


def root_store_path() -> str:
    """<DSH_HOME>/bigfish/root.json —— 数据根的唯一真源。"""
    return os.path.join(dsh_home(), "bigfish", "root.json")


def default_data_root() -> str:
    """root.json 为空时的默认根，与插件 defaultDataRoot() 一致。"""
    return os.path.join(dsh_home(), "bigfish", "scopes")


def read_data_root() -> str:
    """读自定义数据根；没有或读坏返回空串。"""
    try:
        with open(root_store_path(), "r", encoding="utf-8") as f:
            raw = json.load(f)
        return str((raw or {}).get("dataRoot") or "").strip()
    except Exception:
        return ""


def resolve_data_root() -> str:
    """最终生效的数据根：自定义优先，否则默认。"""
    custom = read_data_root()
    if custom:
        return custom
    return default_data_root()


def write_data_root(path: str) -> Tuple[bool, str]:
    """写数据根。传空串 = 恢复默认（文件里留空串，不删文件）。

    与插件 writeDataRoot 行为一致：空值也落盘，否则「恢复默认」之后
    seedDataRootFromProfile 会把 profile 里那个旧值又拉回来。
    """
    target = root_store_path()
    body = {"dataRoot": str(path or "").strip()}
    try:
        _atomic_write_json(target, body)
        return True, ""
    except Exception as exc:
        return False, str(exc)


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


# ══════════════════════════════════════════════════════════════════════════
# 心情 —— 配置读写 + 状态重置（与 mood/index.js 的 patchConfig 对齐）
# ══════════════════════════════════════════════════════════════════════════

MOOD_STORE_NAME = "bigfish-mood"

# 与 mood/index.js DEFAULTS 一致的默认值。只抄界面会改的那几项 ——
# slots / 模式由 scope.json 推，界面不直接改。
MOOD_DEFAULTS: Dict[str, Any] = {
    "moodBase": 70,
    "moodFloor": 0,
    "injectMode": "event",
    "injectConfidence": 40,
    "decayHalfHours": 12,
    "sigMood": 3,
    "sigDim": 5,
    "injectPerDay": 6,
    "autoJudge": True,
}

INJECT_MODES = ["off", "label", "event"]


def mood_config_path() -> str:
    return os.path.join(dsh_home(), MOOD_STORE_NAME, "config.json")


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

        _atomic_write_json(mood_config_path(), next_)
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
    """
    import sqlite3

    ids = [int(i) for i in ids if str(i).strip()]
    if not ids or not db_path or not os.path.isfile(db_path):
        return False, 0, "没有要删的 id"

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        marks = ",".join("?" * len(ids))
        # 找出要删的 chunk id（FTS 的 rowid 就是 chunk.id）
        chunk_rows = conn.execute(
            f"SELECT id FROM chunks WHERE message IN ({marks})", ids).fetchall()
        chunk_ids = [r[0] for r in chunk_rows]

        conn.execute("BEGIN")
        try:
            for cid in chunk_ids:
                conn.execute("DELETE FROM search_index WHERE rowid = ?", (cid,))
            conn.execute(f"DELETE FROM chunks WHERE id IN ({','.join('?' * len(chunk_ids))})",
                         chunk_ids) if chunk_ids else None
            # anchor_sources / activations 通过外键 ON DELETE 处理不了（没建级联），手动清
            if chunk_ids:
                marks2 = ",".join("?" * len(chunk_ids))
                conn.execute(f"DELETE FROM anchor_sources WHERE chunk IN ({marks2})", chunk_ids)
                conn.execute(f"DELETE FROM activations WHERE chunk IN ({marks2})", chunk_ids)
                conn.execute(f"DELETE FROM groups WHERE chunk IN ({marks2})", chunk_ids)
                conn.execute(f"DELETE FROM replays WHERE chunk IN ({marks2})", chunk_ids)
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
    """执行搬家。copy=True 时保留源（先复制再切根，用户确认后自己删）。

    ⚠️ 顺序：先复制全部内容，**复制成功后**才改 root.json。
       反过来的话复制失败就等于数据根指向一个空目录，
       DSH 那边会以为"灵魂没了"，是真的会把用户吓到的事故。
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
        return False, f"复制失败（数据根未改动）：{exc}"

    ok, err = write_data_root(plan.dst)
    if not ok:
        return False, f"复制完成但写配置失败：{err}"

    if not copy:
        # 源目录保留一份 .old-<时间戳>，不直接删 —— 搬家是不可逆操作里最贵的一种
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            os.rename(plan.src, f"{plan.src}.old-{stamp}")
        except Exception:
            pass  # 改根已经成功，源留着也不影响使用
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
    """盘点"清空"会删掉什么。

    ⚠ keep_soul=True 时**不动灵魂** —— 灵魂是"她是谁"，不是"她记得什么"。
      清空记忆和心情不该把人格一起洗掉，那是两件事。
    """
    root = root or resolve_data_root()
    plan = WipePlan(root=root, keep_soul=keep_soul)
    if not os.path.isdir(root):
        plan.error = f"数据根不存在：{root}"
        return plan

    # 新布局的三个文件
    for kind, name in (("soul", SOUL_NAME), ("mood", MOOD_NAME), ("memory", MEMORY_NAME)):
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
            if item["kind"] == "legacy":
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
