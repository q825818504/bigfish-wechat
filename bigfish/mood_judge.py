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
from typing import Any, Dict, Optional, Tuple

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

#: 维度增益：判定给的是「强度」增量（-10..10），但**直接累加太钝**。
#:
#: 实测（2026-11-09，用户跑了一整天）：单次 delta 3~5 分、90 分钟半衰期，
#: 一天下来七根维度长期停在 0~10 —— 界面上看起来"几乎没有情绪"，
#: 而她本人的描述是「心疼他熬着，担心他气着」。数值和事实对不上。
#:
#: ×3 之后单次 9~15 分，稳态落在 30~50，正好是"能看出来、又不至于满格"。
#: ⚠ 这是**标定值**，不是推导值：改它只需要重跑 `selftest_mood`。
#:   心情值不走这个增益（那边有基线 70 和 /3 的步长，另有一套标定）。
DIM_GAIN = 3.0

#: 数值 → 一句人话。阈值只有这一份 ——
#: bridge_client.Mood.label 也走这里，否则心情页显示的和提示词里的会对不上。
MOOD_LABELS: Tuple[Tuple[int, str], ...] = (
    (85, "心情很好"),
    (70, "心情不错"),
    (55, "心情还行"),
    (40, "有点平淡"),
    (25, "情绪低"),
    (0, "情绪很低"),
)


def label_of(value: Optional[float]) -> str:
    """把 0..100 的心情值翻成一句人话。None 返回空串。"""
    if value is None:
        return ""
    v = float(value)
    for floor, text in MOOD_LABELS:
        if v >= floor:
            return text
    return "情绪很低"

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
    # ⚠ 不能用 `or 6`：0 是合法值（= 不注入），但 0 or 6 == 6，
    #   于是"关掉注入"会被悄悄改成"每天 6 次"。自检的「上限 0 = 不注入」钉的就是这个。
    _per_day = cfg.get("injectPerDay")
    per_day = 6 if _per_day is None else max(0, int(_per_day))

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
        # 乘 DIM_GAIN：判定给的是强度，直接加太钝（见常量处的实测说明）。
        _d = int(round(int(delta_dims.get(k, 0)) * DIM_GAIN))
        dims_cur[k] = _clamp(
            int(round(float(dims_cur.get(k) or 0) + _d)), 0, 100)
    s["dims"] = dims_cur

    s["lastReason"] = str(verdict.get("reason") or "")[:200]
    s["lastConfidence"] = int(verdict.get("confidence") or 0)
    s["updatedAt"] = now
    s["lastNotedMood"] = s["mood"]
    s["minor"] = False
    s["kind"] = "judged"

    # ── 2.5 注入文本：injectMode / injectConfidence / sigMood / sigDim ──
    #
    # 这一段以前不存在：`lastInjectedText` 只在初始化时写成空串，之后没人写。
    # 于是 desktop 的「这会儿的状态」永远拿到空字符串 —— 心情值能改表达预算
    # （policy），但从来没有一句话真的进过提示词。心情页上那四个旋钮
    # 也就成了「能填、能存、没人读」。
    #
    # 三种模式：
    #   off   —— 一个字都不注入（连上一条也撤掉）
    #   label —— 只报"现在什么心情"
    #   event —— 只报值得说的变化：心情动了 sigMood 格，或某根情绪动了 sigDim 格
    # 每天最多换 injectPerDay 次说法；超了就把上一条撤掉，别再占提示词。
    mode = str(cfg.get("injectMode") or "event")
    min_conf = _clamp(int(cfg.get("injectConfidence") or 0), 0, 100)
    sig_mood = _clamp(int(cfg.get("sigMood") or 3), 1, 30)
    sig_dim = _clamp(int(cfg.get("sigDim") or 5), 1, 100)
    today = time.strftime("%Y-%m-%d")

    head = f"{label_of(s['mood'])}（{int(s['mood'])}/100）"
    want = ""
    if mode != "off" and int(s["lastConfidence"]) >= min_conf:
        if mode == "label":
            want = head
        else:
            big_mood = abs(step) >= sig_mood
            big_dim = any(abs(int(delta_dims.get(k, 0))) >= sig_dim
                          for k in MOOD_DIMS_ALL)
            if big_mood or big_dim:
                reason = str(s["lastReason"] or "").strip()
                want = f"{head}｜{reason}" if reason else head

    if mode == "off":
        s["lastInjectedText"] = ""
        s["injectedCount"] = 0
    elif want:
        used = (int(s.get("injectedCount") or 0)
                if s.get("injectedDay") == today else 0)
        if per_day <= 0 or used >= per_day:
            s["lastInjectedText"] = ""
        else:
            s["injectedDay"] = today
            s["injectedCount"] = used + 1
            s["lastInjectedText"] = want
            s["lastInjectedAt"] = now

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


# ══════════════════════════════════════════════════════════════════════
# 独立衰减（v0.9）
# ══════════════════════════════════════════════════════════════════════

def baseline_of(*, sleep_debt: Any = 0.0, energy: Any = 70.0,
                discomfort: str = "", weather: str = "") -> float:
    """今天的情绪**底子**。

    为什么需要这个：心情以前永远往 `moodBase`（70）衰减 —— 也就是说
    睡差了、身上难受、连着阴三天、冻得手僵，底子都是 70。
    于是"实时心情"在多数时候显示的是**衰减后的余数**，不是"她今天
    本来是什么底子"。

    人不是这样：底子低的时候一件小事就够烦，底子高的时候同样的事
    笑笑就过去了。事件仍然由 `judge_mood` 加在上面，这里只决定地基。

    参数全是"今天的状态"，不是"刚才发生了什么"：睡眠债、精力、
    身上那点不舒服、天气。任何一项取不到都用中性值 —— 底子算错
    比没有底子更糟。
    """
    base = 70.0
    try:
        debt = max(0.0, float(sleep_debt or 0.0))
    except (TypeError, ValueError):
        debt = 0.0
    # 欠觉：每小时 -3，封顶 -12。
    # 系数按 DEBT_CAP_MINUTES=240 反推：欠满 4 小时就是 -12，
    # 再欠也只是"低"，不该一路掉到底。
    base -= min(12.0, debt / 60.0 * 3.0)

    try:
        e = float(energy)
    except (TypeError, ValueError):
        e = 70.0
    if e < 25:
        base -= 8.0
    elif e < 40:
        base -= 5.0
    elif e < 55:
        base -= 2.0
    elif e > 85:
        base += 3.0

    # 身上那点不舒服（眼睛干、腰酸、头沉）每天都有一点，
    # 它压的是底子，不是"这一轮的心情"。
    if str(discomfort or "").strip():
        base -= 4.0

    w = str(weather or "")
    if any(k in w for k in ("雨", "雪", "阴", "雾", "霾")):
        base -= 3.0
    elif any(k in w for k in ("晴", "阳光")):
        base += 2.0

    # 下界 25：底子再低也不该让她"永远在低情绪"——那会变成演。
    # 上界 92：留一点余地给事件，不然一件好事直接顶满，之后全是回落。
    return max(25.0, min(92.0, base))


def decay_only(seed: str = "dafeiyu", now_ms: Optional[int] = None,
               baseline: Optional[float] = None) -> bool:
    """只做自然衰减，不应用任何判定。**不调模型。**

    存在的理由很具体：衰减以前只写在 `judge_and_apply` 里 ——
    也就是说**只有聊天时才衰减**。

    后果：你三天不说话，她的心情还停在上次那个点。三天里她不会
    从生气里缓过来，也不会从高兴里平静下来。情绪没有"过去"这个动作，
    只有"被下一句话覆盖"。

    挂到 life 的 tick 上（每 10 分钟一次）之后，
    「情绪会自己平复」这条常识第一次真的成立。

    返回是否真的写了盘（无变化就不写）。
    """
    import json
    import math
    import time as _t

    from bigfish.vault import (
        MOOD_DIMS_ALL, _DEFAULT_SLOT, _clamp, _atomic_write_json,
        existing_path, mood_path, read_mood_config,
    )

    p = existing_path(None, "mood") or mood_path(None)
    now = int(now_ms if now_ms is not None else _t.time() * 1000)

    try:
        with open(p, "r", encoding="utf-8") as f:
            store = json.load(f)
        if not isinstance(store, dict):
            return False
    except Exception:
        return False

    moods = store.get("moods")
    if not isinstance(moods, dict) or not moods:
        return False
    slot_key = store.get("lastActive") or next(iter(moods), _DEFAULT_SLOT)
    s = moods.get(slot_key)
    if not isinstance(s, dict):
        return False

    cfg = read_mood_config()
    base = float(_clamp(int(cfg["moodBase"]), 0, 100))
    # 有身体/睡眠/天气推出来的底子就用它，没有就退回配置里的常数。
    # 这是"心情有基线"唯一的落点 —— 事件加在上面，衰减往这里靠。
    if baseline is not None:
        try:
            base = float(_clamp(int(round(float(baseline))), 0, 100))
        except (TypeError, ValueError):
            pass
    half = _clamp(int(cfg.get("decayHalfHours") or 12), 1, 168)

    last_decay = int(s.get("lastDecayAt") or now)
    age_min = max(0.0, (now - last_decay) / 60000.0)
    if age_min < 1:
        return False

    km = 1 - math.exp(-(math.log(2) * age_min) / (half * 60))
    ke = 1 - math.exp(-(math.log(2) * age_min) / DIM_HALF_LIFE_MIN)

    cur = float(s.get("mood", base))
    new_mood = cur + (base - cur) * km
    dims_cur = s.get("dims") if isinstance(s.get("dims"), dict) else {}
    for k in MOOD_DIMS_ALL:
        dims_cur[k] = max(0.0, float(dims_cur.get(k) or 0) * (1 - ke))

    s["mood"] = round(new_mood, 2)
    s["dims"] = dims_cur
    s["lastDecayAt"] = now
    s["kind"] = "decayed"
    moods[slot_key] = s
    store["moods"] = moods

    try:
        _atomic_write_json(p, store)
        return True
    except Exception:
        return False
