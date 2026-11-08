"""[bigfish] 余韵：这一轮之后还留着什么。

═══════════════════════════════════════════════════════════════════════
问题
═══════════════════════════════════════════════════════════════════════

mood.json 有分数、有七根情绪、有时间线。但它是**状态**，不是**残留**：

    上一轮你们拌了两句，mood 从 72 掉到 58，sad +6。
    下一轮你说"在吗"，她在 system 里看到的是"心情 58/100"。
    于是她用 58 分的语气，正常地回了你。

真人不是这样。真人的下一轮不是"我心情 58"，是
「刚才那句话我还记着，现在还不想先服软」——
**有方向、有对象、有具体的事**。分数表达不了这个。

余韵就是这一层：一轮结束后，留下一条**带对象和方向的短句**，
下一轮注入。它会自己过期，也会被"哄好"提前消掉。

═══════════════════════════════════════════════════════════════════════
为什么单独一个文件，不塞进 mood.json
═══════════════════════════════════════════════════════════════════════

mood.json 由 DSH 插件（Node）和微信端（Python）共同读写，字段已经定了。
往里加一个 `afterglow` 数组，等于让两边都要懂这个新结构 ——
一边没升级就会在保存时把它冲掉。

余韵是**微信这条线独有**的东西（DSH 那边没有聊天流），
它有自己的生命周期（几小时），也有自己的清除条件。
放独立文件，桥接不用动，坏了也只是少一段注入。

⚠ 这不是"新开一个板块"。面板上没有「余韵」页，它就是
930 座位那一段注入，和「生活」一样：状态是器官，插件是它的嘴。

═══════════════════════════════════════════════════════════════════════
什么时候写
═══════════════════════════════════════════════════════════════════════

**只在一轮真的结束了、而且情绪真的动了的时候写。**

调用点在 `_judge_mood_async` 里 —— 因为那里才有 before/after 的真实
差值。放在发送成功时写不了，那时心情还没判；放在下一轮开头写更不行，
那时已经错过了"她带着这个情绪回你"的时机。

判据（全部本地，不调模型）：

    conflict  对方这句里有明确的攻击/推开词，且 mood 掉了 ≥3
    hurt      情绪掉 ≥5，或 sad / heartache 单根涨 ≥4
    worry     worry 涨 ≥4（担心，不是受伤）
    warm      mood 涨 ≥4，或 sweet / happy 涨 ≥4

小波动（±1~2）不写。一天写五条余韵，等于她一直在情绪里 —— 那不是人，
那是没有情绪基线。

═══════════════════════════════════════════════════════════════════════
什么时候消
═══════════════════════════════════════════════════════════════════════

三条路，任一条先到：

    1. 对方道歉 / 示好（note_repair）—— 软消，留 30 分钟尾韵
    2. 时间到（conflict 4h / hurt 2h / worry 3h / warm 1h）
    3. 被新的同类余韵覆盖（同类只留最新一条）

软消和硬消要分开：道歉之后立刻当没发生过，她会显得没脾气；
留半小时尾韵（"气消了，但还有点别扭"），才像人。

═══════════════════════════════════════════════════════════════════════
不写什么
═══════════════════════════════════════════════════════════════════════

· 不写"她很生气"这种结论 —— 写**事情**：「他说「你烦不烦」，你还没缓过来」
· 不写数值
· 不写对方没说过的话（residue 里的引用必须来自本轮原文）
· 不替对方定性（不写"他不在乎你"，只写"他没回你"）
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


STATE_NAME = "afterglow.json"
STATE_VERSION = 1

#: 同时最多留几条。留多了会互相打架 —— 她不可能同时为三件事别扭。
MAX_ITEMS = 3

#: 各类余韵的存活时长（秒）。按"这件事多久才真的过去"定，不是按随机。
TTL = {
    "conflict": 4 * 3600,
    "hurt": 2 * 3600,
    "worry": 3 * 3600,
    "warm": 1 * 3600,
}

#: 软消之后的尾韵时长。道歉了，但"还有点别扭"要留一会儿。
RESOLVED_TAIL = 30 * 60

#: 明显在推开 / 攻击的词。宁可少判：判错一次，她会无缘无故冷你半天。
#: 所以这里只收"几乎不可能出现在正常对话里"的说法。
CONFLICT_MARKS = (
    "别烦我", "你很烦", "烦不烦", "闭嘴", "滚开", "滚吧", "别说了",
    "不想理你", "懒得理", "讨厌你", "受够", "随你便", "爱怎样怎样",
    "关你什么事", "少管我",
)

#: 道歉 / 示好。命中就软消。同样从严：只认明确的表达。
REPAIR_MARKS = (
    "对不起", "抱歉", "我错了", "别生气了", "不生气了好不好", "是我不好",
    "原谅我", "哄哄你", "我请你", "给你买", "别气了", "刚刚是我",
)


def _now_ms(now: Optional[float] = None) -> int:
    return int(float(now if now is not None else time.time()) * 1000)


def state_path(root: Optional[str] = None) -> str:
    from bigfish.vault import resolve_data_root

    return os.path.join(root or resolve_data_root(), STATE_NAME)


def _clip(text: Any, limit: int) -> str:
    s = str(text or "").strip()
    s = " ".join(s.split())
    return s[:limit]


def load(root: Optional[str] = None) -> Dict[str, Any]:
    """读状态。任何损坏都当成空 —— 余韵丢了不该影响聊天。"""
    try:
        with open(state_path(root), "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {"version": STATE_VERSION, "items": []}
    if not isinstance(raw, dict):
        return {"version": STATE_VERSION, "items": []}
    items = raw.get("items")
    if not isinstance(items, list):
        items = []
    return {"version": STATE_VERSION,
            "items": [x for x in items if isinstance(x, dict)]}


def save(state: Dict[str, Any], root: Optional[str] = None) -> bool:
    """原子写。失败返回 False，不抛 —— 余韵是加分项，不是聊天的前提。"""
    try:
        from bigfish.vault import _atomic_write_json

        path = state_path(root)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        _atomic_write_json(path, state)
        return True
    except Exception:
        return False


def _expired(item: Dict[str, Any], now: float) -> bool:
    try:
        return float(item.get("expire_at") or 0) <= now
    except (TypeError, ValueError):
        return True


def _resolved(item: Dict[str, Any], now: float) -> bool:
    """软消之后是否已经过了尾韵期。"""
    try:
        at = float(item.get("resolved_at") or 0)
    except (TypeError, ValueError):
        at = 0
    return bool(at) and now - at >= RESOLVED_TAIL


def expire(state: Dict[str, Any], *, now: Optional[float] = None) -> int:
    """清掉过期和尾韵走完的。返回清掉几条。"""
    t = float(now if now is not None else time.time())
    before = len(state.get("items") or [])
    kept = []
    for item in state.get("items") or []:
        if _expired(item, t) or _resolved(item, t):
            continue
        kept.append(item)
    state["items"] = kept
    return before - len(kept)


def active(state: Dict[str, Any], *, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """当前还有效的余韵，按强度排序（conflict > hurt > worry > warm）。"""
    t = float(now if now is not None else time.time())
    rank = {"conflict": 0, "hurt": 1, "worry": 2, "warm": 3}
    out = [x for x in (state.get("items") or [])
           if not _expired(x, t) and not _resolved(x, t)]
    out.sort(key=lambda x: (rank.get(str(x.get("kind")), 9),
                            -float(x.get("at") or 0)))
    return out


def _hit(text: str, marks) -> str:
    s = str(text or "")
    for m in marks:
        if m in s:
            return m
    return ""


def _quote_from(text: str, mark: str, limit: int = 16) -> str:
    """从原话里取一小段当引用。找不到就整句截断 —— 绝不改写。"""
    s = " ".join(str(text or "").split())
    if not s:
        return ""
    i = s.find(mark)
    if i < 0:
        return s[:limit]
    lo = max(0, i - 4)
    return s[lo:lo + limit]


def classify(*, user_text: str, mood_before: Optional[float],
             mood_after: Optional[float],
             dims_delta: Optional[Dict[str, Any]] = None,
             judged_delta: float = 0.0) -> str:
    """判这一轮该不该留余韵、留哪一类。空串 = 不留。

    `judged_delta` 是模型判出来的情绪变化（已除以 MOOD_STEP_DIVISOR），
    优先用它。原因：mood_after - mood_before 里混着"向基线回归"，
    一次很重的拌嘴也可能因为回归而看起来只掉了 1 分。
    模型的判断更接近"这件事有多重"。
    """
    try:
        before = float(mood_before) if mood_before is not None else None
    except (TypeError, ValueError):
        before = None
    try:
        after = float(mood_after) if mood_after is not None else None
    except (TypeError, ValueError):
        after = None

    actual = (after - before) if (before is not None and after is not None) else 0.0
    try:
        judged = float(judged_delta or 0.0)
    except (TypeError, ValueError):
        judged = 0.0
    # 两者取绝对值更大的那个：模型说很重、或者实际掉得很多，都算数。
    delta = judged if abs(judged) >= abs(actual) else actual
    dims = dims_delta if isinstance(dims_delta, dict) else {}

    def d(key: str) -> float:
        try:
            return float(dims.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    mark = _hit(user_text, CONFLICT_MARKS)
    if mark and delta <= -3:
        return "conflict"
    if delta <= -5 or d("sad") >= 4 or d("heartache") >= 4:
        return "hurt"
    if d("worry") >= 4:
        return "worry"
    if delta >= 4 or d("sweet") >= 4 or d("happy") >= 4:
        return "warm"
    return ""


def _residue(kind: str, *, user_text: str, mood_after: Optional[float]) -> str:
    """一句带对象的话。只描述发生过什么，不下结论。"""
    mark = _hit(user_text, CONFLICT_MARKS)
    quote = _quote_from(user_text, mark) if mark else ""
    if kind == "conflict":
        return (f"刚才他说「{quote}」，你还没缓过来。" if quote
                else "刚才那几句还堵在心里，你还没缓过来。")
    if kind == "hurt":
        return (f"他说的话让你心里沉了一下（「{quote}」）。" if quote
                else "刚才那件事让你心里沉了一下。")
    if kind == "worry":
        return "他说的事让你有点悬着心，但你没多问。"
    return "刚才那句话让你心里暖了一下，你没说出来。"


def _effect(kind: str) -> Dict[str, Any]:
    """给下一轮的预算修正。和 policy.py 的字段同名，合并时取极值。"""
    if kind == "conflict":
        return {"silence_bias": 0.22, "max_chars": -8, "max_parts": -1,
                "avoid": ["主动撒娇", "主动问候"]}
    if kind == "hurt":
        return {"silence_bias": 0.12, "max_chars": -5, "max_parts": 0,
                "avoid": ["主动提起自己的事"]}
    if kind == "worry":
        return {"silence_bias": 0.0, "max_chars": 0, "max_parts": 0,
                "avoid": []}
    return {"silence_bias": -0.04, "max_chars": 4, "max_parts": 0,
            "avoid": []}


def from_turn(*, chat_id: str, user_text: str, reply: str = "",
              mood_before: Optional[float] = None,
              mood_after: Optional[float] = None,
              dims_delta: Optional[Dict[str, Any]] = None,
              judged_delta: float = 0.0,
              now: Optional[float] = None,
              root: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """一轮结束后写一条余韵。返回写入的项；不需要写时返回 None。"""
    t = float(now if now is not None else time.time())
    kind = classify(user_text=user_text, mood_before=mood_before,
                    mood_after=mood_after, dims_delta=dims_delta,
                    judged_delta=judged_delta)
    if not kind:
        return None

    state = load(root)
    expire(state, now=t)

    # 同类只留最新一条：她不可能同时为两件小事别扭。
    state["items"] = [x for x in state.get("items") or []
                      if str(x.get("kind")) != kind]

    item = {
        "id": hashlib.sha1(f"{chat_id}|{kind}|{t}".encode("utf-8")).hexdigest()[:12],
        "kind": kind,
        "chat_id": str(chat_id),
        "residue": _residue(kind, user_text=user_text, mood_after=mood_after),
        "quote": _quote_from(user_text, _hit(user_text, CONFLICT_MARKS)) if kind in ("conflict", "hurt") else "",
        "at": t,
        "expire_at": t + TTL.get(kind, 3600),
        "resolved_at": 0.0,
        "effect": _effect(kind),
    }
    items = [x for x in state.get("items") or [] if isinstance(x, dict)]
    items.append(item)
    # 每次写完都按轻重排：不只是为了上限截断，也因为下游
    # `render(limit=2)` 取的是前两条 —— 顺序错了，她会先想起小事。
    rank = {"conflict": 0, "hurt": 1, "worry": 2, "warm": 3}
    items.sort(key=lambda x: rank.get(str(x.get("kind")), 9))
    if len(items) > MAX_ITEMS:
        items = items[:MAX_ITEMS]
    state["items"] = items
    save(state, root)
    return item


def note_repair(text: str, *, now: Optional[float] = None,
                root: Optional[str] = None) -> int:
    """对方道歉 / 示好 → 软消。返回消掉几条。

    只软消 conflict 和 hurt：担心和暖意不需要道歉来消，
    它们本来就该自然淡掉。
    """
    mark = _hit(text, REPAIR_MARKS)
    if not mark:
        return 0
    t = float(now if now is not None else time.time())
    state = load(root)
    n = 0
    for item in state.get("items") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("kind")) not in ("conflict", "hurt"):
            continue
        if float(item.get("resolved_at") or 0):
            continue
        item["resolved_at"] = t
        # 软消之后的尾韵：不删，改短，且不再有负向预算
        item["residue"] = "刚才那点别扭已经过去了，只是还有点没完全顺过来。"
        item["effect"] = {"silence_bias": 0.03, "max_chars": 0,
                          "max_parts": 0, "avoid": []}
        n += 1
    if n:
        save(state, root)
    return n


def current_effect(*, now: Optional[float] = None,
                   root: Optional[str] = None) -> Dict[str, Any]:
    """把有效余韵合成一组预算修正。多条时取最强的那个。"""
    t = float(now if now is not None else time.time())
    state = load(root)
    items = active(state, now=t)
    out: Dict[str, Any] = {"silence_bias": 0.0, "max_chars": 0,
                           "max_parts": 0, "avoid": []}
    for item in items:
        eff = item.get("effect") if isinstance(item.get("effect"), dict) else {}
        try:
            out["silence_bias"] = max(out["silence_bias"],
                                      float(eff.get("silence_bias") or 0))
            out["max_chars"] = min(out["max_chars"],
                                   int(eff.get("max_chars") or 0))
            out["max_parts"] = min(out["max_parts"],
                                   int(eff.get("max_parts") or 0))
        except (TypeError, ValueError):
            continue
        for a in eff.get("avoid") or []:
            if a and a not in out["avoid"]:
                out["avoid"].append(a)
    return out


def render(*, now: Optional[float] = None, root: Optional[str] = None,
           limit: int = 2) -> str:
    """给提示词的一段。最多两条，多了会互相打架。"""
    t = float(now if now is not None else time.time())
    items = active(load(root), now=t)[:limit]
    if not items:
        return ""
    lines = [f"- {x.get('residue')}" for x in items if x.get("residue")]
    if not lines:
        return ""
    return ("## 还留着的\n\n" + "\n".join(lines)
            + "\n\n（这是你心里的余味，不是要跟他讲的话。"
              "不用主动提，也别装作已经完全过去了。）")


def summary(*, now: Optional[float] = None, root: Optional[str] = None) -> str:
    """面板/日志用的一行。"""
    t = float(now if now is not None else time.time())
    items = active(load(root), now=t)
    if not items:
        return "没有余韵"
    return " / ".join(f"{x.get('kind')}：{x.get('residue')}" for x in items)
