"""[bigfish] 此刻的状态 → 这一轮的表达预算。

═══════════════════════════════════════════════════════════════════════
为什么需要这一层
═══════════════════════════════════════════════════════════════════════

v0.9.7 之前，心情和精力只做两件事：写进 system 给模型看、影响主动开口的
概率。于是出现一个很具体的割裂：

    她"心情 38/100"，但回你的话还是四条、每段 40 字、节奏一样快。

也就是说，状态是**被描述**的，不是**被使用**的。真人的状态不是一句话，
是一个预算：心情差的时候，同样一件事她只会说一句；精力低的时候，
她不会给你写三段。

这一层就干这件事：把 mood / energy / 时段翻成四个数和一个倾向句。
模型拿到的不是"你心情 38"，而是"这一轮你话不多"——后者它才演得对。

═══════════════════════════════════════════════════════════════════════
为什么只给预算，不给情绪标签
═══════════════════════════════════════════════════════════════════════

试过直接写「你现在心情很低落，说话要冷淡」。结果是：

    · 她每一轮都在演"低落"，连你讲笑话时也低落；
    · 数值一回升，语气立刻切换，像换了个人；
    · 最糟的是她会把"低落"说出来（"我今天心情不好"），
      而真人不会每轮汇报自己的情绪。

预算不会演。它只约束"能说多少"，说什么仍然由她自己决定 ——
心情差的时候她可能只回一个"嗯"，也可能突然说一句很重的话，
两种都真实，因为预算只砍长度，不规定内容。

═══════════════════════════════════════════════════════════════════════
四个数
═══════════════════════════════════════════════════════════════════════

    max_parts            这一轮最多分几条
    max_chars            整轮正文上限（不是每段）
    silence_bias         给模型"可以不接"的许可强度（0~1，进提示词）
    must_reply_after     连续不接几次之后强制回一句

`silence_bias` 不是代码掷骰。她要不要不接，仍然由模型输出 `-` 决定 ——
这一层只改变"允许她沉默"的程度。理由和 proactive 一样：
**沉默必须是她的选择，不是系统的丢弃。** 代码掷骰决定不回，
用户看到的是"她明明想了却没发"，那是 bug 的味道。

═══════════════════════════════════════════════════════════════════════
边界
═══════════════════════════════════════════════════════════════════════

· 读不到 mood / life 时用中性默认值（不抛、不报错）——
  状态坏了不该让她失语。
· 数值永远不进提示词。tone_line 是定性的，且不出现"心情/精力/数值"。
· 这一层不写盘、不调模型、不依赖引擎对象，纯函数，可离线测。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, Optional


#: 中性默认值。读不到状态时用它 —— 不是"开心"，是"没什么特别"。
NEUTRAL_MOOD = 70.0
NEUTRAL_ENERGY = 70.0

#: 连续不接几次后强制回。心情好时更容易放过自己，心情差时多给她一次机会。
MUST_REPLY_AFTER_BRIGHT = 2
MUST_REPLY_AFTER_DIM = 3


@dataclass(frozen=True)
class ResponsePolicy:
    """一轮的表达预算。字段含义见模块文档。"""

    max_parts: int
    max_chars: int
    silence_bias: float
    must_reply_after: int
    #: 进 system 的一句倾向描述。空串 = 不加这一段。
    tone_line: str = ""
    #: 日志/快照用的一句话，说清为什么是这个预算。
    reason: str = ""

    def clamp_parts(self, configured: int) -> int:
        """和插件里的 max_parts 取小。用户配的更松时，状态仍然说了算。"""
        try:
            configured = int(configured)
        except (TypeError, ValueError):
            configured = self.max_parts
        return max(1, min(configured, self.max_parts))

    def clamp_chars(self, configured: int) -> int:
        """和会话层的 max_reply_chars 取小。0 表示没配。"""
        try:
            configured = int(configured)
        except (TypeError, ValueError):
            configured = 0
        if configured <= 0:
            return self.max_chars
        return max(8, min(configured, self.max_chars))


def _num(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _dim(dims: Optional[Dict[str, Any]], key: str) -> float:
    if not isinstance(dims, dict):
        return 0.0
    return max(0.0, _num(dims.get(key), 0.0))


def _hour(now: Optional[float]) -> int:
    """当前小时。没给 now 就不做深夜判断 —— 纯函数不该自己去读时钟。"""
    if now is None:
        return -1
    try:
        import time as _t

        return int(_t.localtime(float(now)).tm_hour)
    except Exception:
        return -1


def tone_of(mood: float, dims: Optional[Dict[str, Any]]) -> str:
    """一句定性的表达倾向。

    刻意**不写情绪词**（"难过""开心"），只写怎么说。
    写"你有点难过"她会演；写"一句能说完的就别分两条"她会照做。

    ⚠ 维度的量纲是 **0..100**（`mood_judge` 用 -10..10 的增量累加后
      clamp 到 0..100）。这里以前按 0..10 判（`sad >= 4`），于是
      攒过两轮之后 `sad >= 4` 恒真 —— `mood < 55` 时她永远在说
      "这一轮你没什么兴致"，不管实际是 5 还是 50。
      阈值要和写入方同一个量纲，否则这个函数输出的是常数不是情绪。
    """
    sad = _dim(dims, "sad")
    worry = _dim(dims, "worry")
    sweet = _dim(dims, "sweet")
    happy = _dim(dims, "happy")
    heartache = _dim(dims, "heartache")

    if mood < 35:
        # 只写"能说多少"，"可以不接"交给 silence_hint ——
        # 两处都写会变成同一句话说两遍，既占 token 又像在反复叮嘱。
        return "这一轮你话不多。能一句说完的，不要拆成两条。"
    if mood < 55:
        if sad >= 40 or heartache >= 40:
            return "这一轮你没什么兴致，句子短一点就行。"
        if worry >= 40:
            return "这一轮你有点悬着心，话会往那件事上靠，但不用反复说。"
        return "这一轮你不太想展开，简短回应就好。"
    if mood >= 80 and (sweet >= 40 or happy >= 50):
        return "这一轮你整个人是松的，可以多说半句，但别变成一串。"
    return ""


def response_policy(
    *,
    mood: Any = None,
    dims: Optional[Dict[str, Any]] = None,
    energy: Any = None,
    now: Optional[float] = None,
    recent_parts: int = 0,
    recent_replies: int = 0,
) -> ResponsePolicy:
    """算这一轮的表达预算。

    分档的依据不是"心理学"，是**可感知的差异**：
    心情 80 和 70 说话其实一样，心情 80 和 32 才明显不一样。
    所以只切三刀（35 / 55 / 80），避免出现"每天都不一样"的抖动。

    精力只做减法：低精力只会让她更短，不会让她更长。
    深夜同理 —— 困了的人不会突然话多。

    `recent_parts` / `recent_replies` 是**跨轮配额**：最近 90 秒内
    她已经发了几条、回了几轮。单轮看每轮都合理，连起来就是刷屏 ——
    用户实测过「我连发两条，她第一轮 3 条，第二轮又 3 条 = 一次 6 条」。
    所以预算要记得自己刚说过什么。
    """
    m = _num(mood, NEUTRAL_MOOD)
    e = _num(energy, NEUTRAL_ENERGY)

    if m < 35:
        parts, chars, bias, after = 1, 14, 0.35, MUST_REPLY_AFTER_DIM
        why = "心情很低"
    elif m < 55:
        parts, chars, bias, after = 2, 22, 0.18, MUST_REPLY_AFTER_DIM
        why = "心情偏低"
    elif m < 80:
        parts, chars, bias, after = 3, 30, 0.08, MUST_REPLY_AFTER_BRIGHT
        why = "心情平常"
    else:
        parts, chars, bias, after = 4, 40, 0.03, MUST_REPLY_AFTER_BRIGHT
        why = "心情不错"

    if e < 30:
        parts = 1
        chars = max(8, chars - 6)
        bias = min(0.6, bias + 0.10)
        why += "，精力低"
    elif e < 50:
        chars = max(8, chars - 3)
        why += "，有点累"

    hour = _hour(now)
    if 0 <= hour < 6:
        parts = 1
        chars = max(8, chars - 4)
        bias = min(0.6, bias + 0.10)
        why += "，深夜"
    elif hour >= 23:
        parts = min(parts, 2)
        bias = min(0.6, bias + 0.06)
        why += "，很晚了"

    # ── 跨轮配额：刚说过话就少说 ──
    #
    # 这三档只收紧、不放宽，所以不会出现"聊得越久她越能说"。
    # 阈值按"一次对话能忍多少条"定：6 条已经很吵，3 条开始要收。
    try:
        _rp = max(0, int(recent_parts or 0))
        _rr = max(0, int(recent_replies or 0))
    except (TypeError, ValueError):
        _rp, _rr = 0, 0

    if _rp >= 6:
        parts = 1
        chars = min(chars, 16)
        why += "，刚连着说了几句"
    elif _rp >= 3:
        # 她已经说了 3 条，对方又发一句 —— 一句接住就够了。
        # 这里给 2 条的话，两轮就是 5 条，用户照样觉得在刷屏。
        parts = 1
        chars = min(chars, 24)
        why += "，刚说过话"

    if _rr >= 2:
        parts = 1
        why += "，连着几轮都在回"

    return ResponsePolicy(
        max_parts=parts,
        max_chars=chars,
        silence_bias=round(bias, 3),
        must_reply_after=after,
        tone_line=tone_of(m, dims),
        reason=why,
    )


def silence_hint(bias: float) -> str:
    """把 silence_bias 翻成提示词里的一句话。

    只在 bias 足够高时才出现 —— 每轮都提醒"你可以不接"，
    她会把不接当成一种权利，然后频繁用。
    """
    if bias >= 0.30:
        return ("这一轮你真的可以不接。对方这句如果没什么可回的，"
                "只输出 `-` 就行，不用找话。")
    if bias >= 0.15:
        return "这一轮可以短一点；实在没什么可接的，输出 `-` 也不算失礼。"
    return ""


def describe(policy: ResponsePolicy) -> str:
    """日志用。例：max4/40 chars, silence 0.08, force@2（心情平常）"""
    return (f"max{policy.max_parts}/{policy.max_chars} chars, "
            f"silence {policy.silence_bias:.2f}, force@{policy.must_reply_after}"
            f"（{policy.reason}）")


def apply_afterglow(policy: ResponsePolicy,
                    effect: Optional[Dict[str, Any]]) -> ResponsePolicy:
    """把余韵的修正叠到预算上。

    取极值而不是相加：两条余韵（还在气头上 + 心里悬着）同时存在时，
    `silence_bias` 相加会溢出到"她基本不理你"，那过头了。
    所以三个数都往同一个方向取最紧的那个，只有正向的暖意会放宽一点。

    `max_chars` 是增量（负数=更短），和 `max_parts` 一起都能叠，
    因为它们本身就是"还剩多少余量"。
    """
    if not isinstance(effect, dict):
        return policy
    try:
        bias_add = float(effect.get("silence_bias") or 0.0)
        chars_add = int(effect.get("max_chars") or 0)
        parts_add = int(effect.get("max_parts") or 0)
    except (TypeError, ValueError):
        return policy
    return replace(
        policy,
        max_parts=max(1, policy.max_parts + parts_add),
        max_chars=max(8, policy.max_chars + chars_add),
        silence_bias=round(min(0.6, max(0.0, policy.silence_bias + bias_add)), 3),
    )
