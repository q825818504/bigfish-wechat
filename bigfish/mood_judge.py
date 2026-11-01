"""[bigfish] 心情判断与落盘。

微信这边聊完一轮，要能算出"她此刻心情怎么样"，并写进 mood.json。

为什么这块以前是空的
--------------------
`bridge_client.load_mood_only()` 只**读**心情，`vault.reset_mood()` 只能重置。
没有任何地方真正**更新**心情 —— 所以聊一晚上，心情页永远是 0。

那是设计缺口：读路径铺好了，写路径忘了。

与插件的关系
------------
判断逻辑照抄 `dsh-bigfish/lib/mood/index.js` 的 `judge()`：
  同样是「她的回复 + 对方说的话」一起喂给模型，
  同样输出 `{moodDelta, dims, reason, confidence}` 这一坨 JSON，
  同样用 `MOOD_STEP_DIVISOR = 3` 把判定幅度缩成实际步长。

不照抄的部分：**怎么调模型**。插件走 `ctx.llm.stream()`（宿主能力），
微信这边走 `replier.call_model()`（HTTP）。输入输出格式完全一致，
所以判定结果可以互换。

⚠ 心情不是"聊一句就动 10 格"。倍数除以 3、加上半衰期衰减，
  一天下来大概在 60-85 之间浮动 —— 那是插件的实测结论
  （index.js 里 2026-10-21 的注释有完整数据），别改。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from bigfish.vault import (
    MOOD_DIMS_ALL,
    _DEFAULT_SLOT,
    _clamp,
    _atomic_write_json,
    existing_path,
    mood_path,
    read_mood_config,
)

# 判定幅度 → 实际步长。与 index.js 的 MOOD_STEP_DIVISOR 一致。
# ⚠ 试过改成 2：模拟显示心情会长期贴顶（平均 97，78% 时间 ≥95），
#   反而更"平"—— 永远满格开心等于没有情绪。别动这个 3。
MOOD_STEP_DIVISOR = 3

# 单根情绪的半衰期（分钟）。与 index.js 的 decay() 一致。
DIM_HALF_LIFE_MIN = 90

SYSTEM_PROMPT = """你在扮演一个角色，帮她判断「这一轮聊完之后，她自己的心情发生了什么变化」。

只输出一个 JSON 对象，不要任何其他文字、不要 Markdown 代码块。

JSON 格式（字段必须齐全）：
{"moodDelta":0,"dims":{"happy":0,"calm":0,"sweet":0,"heartache":0,"worry":0,"sad":0,"jealous":0},"reason":"","confidence":80}

字段说明：
- moodDelta：心情总分变化，整数，范围 -10 ~ +10。正数是变好，负数是变差。
- dims：七根情绪的增量，各自 -10 ~ +10，整数。
    happy 开心 / calm 踏实 / sweet 甜蜜 / heartache 心疼
    worry 担心 / sad 难过 / jealous 吃醋
- reason：一句话说明"是什么事让心情变了"。用第三人称，别用"我"开头。
    ≤ 40 字，要具体（"他说今天加班到十点"而不是"聊天"）。
- confidence：你对自己这个判断的可信度，0-100 的整数。

判断规则：
1. 对方诉苦/没吃饭/熬夜/身体不舒服/受委屈 → heartache(心疼) 和 worry(担心) 增加，
   moodDelta 为负。日常的累 -1~-4；明显委屈 -5~-7；很重的事 -8~-10，并额外加 sad。
2. 对方报平安/撒娇/说想你/夸她/分享好事 → happy、calm、sweet 增加，moodDelta 为正。
   普通的 +1~+4；明显的好 +5~+7；特别大的好事 +8~+10。
3. 对方提别人/夸别人 → jealous 加 1~3，moodDelta 基本不动。
4. **她自己的情绪也要算**：她的回复里如果沮丧、委屈、撒娇、嘴硬、高兴，
   那同样是她心情的一部分。只要话里有明显情绪信号，就别判全 0。
5. 纯寒暄（"在吗""嗯""好"）、没有任何情绪信息 → 全 0，confidence 给低（30 左右）。

⚠ 幅度宁小勿大。日常聊天的情绪变化是 ±1~±4，不是 ±10。
"""

USER_TEMPLATE = """**她这轮说的话**（看语气：是念叨、心疼、凶人、还是撒娇）：
{reply}

**对方这轮说**：
{user_text}

请判断她这一轮的心情变化，只输出 JSON。"""


def _parse_json_loose(raw: str) -> Optional[Dict[str, Any]]:
    """从模型输出里抠出 JSON。模型经常包 ```json 或者前后加话。"""
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        if s.endswith("```"):
            s = s[:-3]
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        d = json.loads(s[start:end + 1])
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def judge_mood(
    user_text: str,
    reply: str,
    self_name: str = "",
    owner_name: str = "对方",
) -> Optional[Dict[str, Any]]:
    """调模型判断这一轮的心情变化。失败返回 None（不抛）。

    返回值已经过归一化：
        {"moodDelta": int(-10..10),
         "dims": {七根: int(-10..10)},
         "reason": str(≤40),
         "confidence": int(0..100)}
    """
    from bigfish.replier import ModelConfig, call_model

    try:
        cfg = ModelConfig.load()
        if not cfg.ready:
            return None
    except Exception:
        return None

    facts = []
    if self_name:
        facts.append(f"她的名字是「{self_name}」")
    facts.append(f"对方称呼为「{owner_name}」")
    system = SYSTEM_PROMPT + "\n" + "；".join(facts) + "。\n"

    user = USER_TEMPLATE.format(reply=(reply or "")[:1200],
                                user_text=(user_text or "")[:1200])
    try:
        raw = call_model(cfg, system, user)
    except Exception:
        return None

    d = _parse_json_loose(raw)
    if not d:
        return None

    def _i(v: Any, lo: int, hi: int, default: int = 0) -> int:
        try:
            n = int(round(float(v)))
        except (TypeError, ValueError):
            return default
        return _clamp(n, lo, hi)

    dims_raw = d.get("dims") if isinstance(d.get("dims"), dict) else {}
    dims = {k: _i(dims_raw.get(k, 0), -10, 10) for k in MOOD_DIMS_ALL}

    return {
        "moodDelta": _i(d.get("moodDelta", 0), -10, 10),
        "dims": dims,
        "reason": str(d.get("reason") or "").strip()[:40],
        "confidence": _i(d.get("confidence", 60), 0, 100, 60),
    }


def apply_mood_delta(
    verdict: Dict[str, Any],
    base: Optional[int] = None,
    floor: Optional[int] = None,
) -> Tuple[bool, str]:
    """把一次判定结果写进 mood.json。返回 (ok, error)。

    做的事与 index.js 的 applyDelta 一致：
      1. 心情向基线方向衰减（按半衰期）
      2. 心情 += moodDelta / MOOD_STEP_DIVISOR
      3. 七根情绪各 += delta，然后向 0 衰减
      4. 追加一条 timeline（记忆星图靠它给历史记忆中着色）
    """
    root = None
    try:
        from bigfish.vault import install_root
        root = install_root()
    except Exception:
        pass

    p = existing_path(root, "mood") if root else ""
    if not p:
        p = mood_path(root)

    cfg = read_mood_config()
    b = _clamp(int(base if base is not None else cfg["moodBase"]), 0, 100)
    fl = _clamp(int(floor if floor is not None else cfg["moodFloor"]), 0, 100)
    half = _clamp(int(cfg.get("decayHalfHours") or 12), 1, 168)
    per_day = max(0, int(cfg.get("injectPerDay") or 6))

    now = int(time.time() * 1000)

    # 读现有状态；没有就造一份空的
    store: Dict[str, Any]
    try:
        with open(p, "r", encoding="utf-8") as f:
            store = json.load(f)
        if not isinstance(store, dict):
            store = {}
    except Exception:
        store = {}

    if not isinstance(store.get("moods"), dict) or not store["moods"]:
        store = {"version": 2, "moods": {}, "lastActive": _DEFAULT_SLOT}

    slot_key = store.get("lastActive") or next(iter(store["moods"]), _DEFAULT_SLOT)
    if slot_key not in store["moods"]:
        store["moods"][slot_key] = {
            "mood": float(b),
            "dims": {k: 0.0 for k in MOOD_DIMS_ALL},
            "lastReason": "",
            "lastConfidence": 0,
            "injectedFor": 0,
            "lastInjectedText": "",
            "lastInjectedAt": 0,
            "saidAt": [],
            "lastNotedMood": b,
            "minor": False,
            "kind": "",
            "updatedAt": now,
            "lastDecayAt": now,
            "timeline": [],
        }
    s = store["moods"][slot_key]

    # ── 1. 自然衰减 ──
    last_decay = int(s.get("lastDecayAt") or now)
    age_min = max(0.0, (now - last_decay) / 60000.0)
    if age_min >= 1:
        import math

        km = 1 - math.exp(-(math.log(2) * age_min) / (half * 60))
        s["mood"] = float(s.get("mood", b)) + (b - float(s.get("mood", b))) * km
        ke = 1 - math.exp(-(math.log(2) * age_min) / DIM_HALF_LIFE_MIN)
        dims_cur = s.get("dims") if isinstance(s.get("dims"), dict) else {}
        for k in MOOD_DIMS_ALL:
            dims_cur[k] = max(0.0, float(dims_cur.get(k) or 0) * (1 - ke))
        s["dims"] = dims_cur
        s["lastDecayAt"] = now

    # ── 2. 应用判定 ──
    step = int(round(int(verdict.get("moodDelta") or 0) / MOOD_STEP_DIVISOR))
    cur = float(s.get("mood", b))
    s["mood"] = _clamp(int(round(cur + step)), max(fl, 0), 100)

    dims_cur = s.get("dims") if isinstance(s.get("dims"), dict) else {}
    delta_dims = verdict.get("dims") or {}
    for k in MOOD_DIMS_ALL:
        dims_cur[k] = _clamp(
            int(round(float(dims_cur.get(k) or 0) + int(delta_dims.get(k, 0)))), 0, 100)
    s["dims"] = dims_cur

    s["lastReason"] = str(verdict.get("reason") or "")[:200]
    s["lastConfidence"] = int(verdict.get("confidence") or 0)
    s["updatedAt"] = now
    s["lastNotedMood"] = s["mood"]
    s["minor"] = False
    s["kind"] = "judged"

    # ── 3. 时间线 ──
    tl = s.get("timeline") if isinstance(s.get("timeline"), list) else []
    tl.append({
        "at": now,
        "mood": int(s["mood"]),
        "delta": step,
        "conf": s["lastConfidence"],
        "kind": "judged",
        "reason": s["lastReason"],
        "dims": {k: int(round(v)) for k, v in dims_cur.items()},
        "dimsDelta": {k: int(delta_dims.get(k, 0)) for k in MOOD_DIMS_ALL},
    })
    if len(tl) > 2000:
        tl = tl[-2000:]
    s["timeline"] = tl

    store["moods"][slot_key] = s
    store["lastActive"] = slot_key

    try:
        _atomic_write_json(p, store)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def judge_and_apply(
    user_text: str,
    reply: str,
    self_name: str = "",
    owner_name: str = "对方",
) -> Dict[str, Any]:
    """判断 + 落盘，一步到位。返回一个报告给日志用。

    任何一步失败都不抛 —— 心情是增强项，不能因为它把聊天搞挂。
    """
    out: Dict[str, Any] = {
        "ok": False,
        "verdict": None,
        "error": "",
        "mood": None,
    }
    v = judge_mood(user_text, reply, self_name, owner_name)
    if not v:
        out["error"] = "模型没给出可用的判断"
        return out
    out["verdict"] = v

    ok, err = apply_mood_delta(v)
    out["ok"] = ok
    if not ok:
        out["error"] = err
        return out

    try:
        st = read_mood_state_now()
        if st:
            out["mood"] = st.get("mood")
    except Exception:
        pass
    return out


def read_mood_state_now() -> Optional[Dict[str, Any]]:
    """读当前心情槽位（不触发衰减写盘）。"""
    from bigfish.vault import read_mood_state as _read

    store = _read()
    if not store:
        return None
    moods = store.get("moods")
    if not isinstance(moods, dict) or not moods:
        return None
    key = store.get("lastActive") or next(iter(moods))
    return moods.get(key) or moods.get(next(iter(moods)))
