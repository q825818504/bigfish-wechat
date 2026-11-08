"""[插件] 生活 —— 现在几点、她醒着还是睡了、还剩多少力气、手头在做什么。

为什么三件事合在一起
────────────────────
原本是三个插件：`clock`（时间）、`life`（身体）、`sleep-gate`（睡觉不回）。
它们的数据源是同一份（`life.json` + 系统时钟），讲的是同一件事：
**她有自己的作息，现在是其中某一刻**。

拆开的代价很具体：
- 用户要在插件页里开三个开关，才能让她「像个活人」；
- 只开一个就会说出半个真相 —— 知道现在几点，但不知道自己已经睡了；
- 三个插件各自读盘、各自判时间，同一轮里可能读到三个不同的「现在」。

所以合并成一个，scope 是 `prompt+gate`：一边注入，一边判定。
关掉它就是关掉「她有作息」这一整件事，不会剩下半截。

三条自我约束
────────────
1. **只说文件里有的**。「手头在：在看书」这句背后，life.json 的
   `doing` 字段必须确实是「在看书」。不允许在这里即兴编一件新事 ——
   编出来的事第二天对不上（D-36 事实必须有真来源）。
2. **不替对方判断**。不写「这个点你还没睡」这类揣测。
3. **不当情绪使唤**。困了就说困，不要求她因此缩短回复或者装可怜 ——
   语气怎么走由她自己决定，这里只给事实。

⚠ 睡着之后的消息记在 pending 里，由引擎记账（`life.note_pending`）。
  这一层只回答「该不该沉默」，不叫醒她、不主动发消息 ——
  主动说话是另一件事，要单独走发送三态与账本。
"""
from __future__ import annotations

import time as _time
from datetime import datetime
from typing import Any, Dict, Tuple

from . import register

_WEEK = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

#: 时段划分与语气。刻意用「深夜/清晨」这类词而不是「23 点」——
#: 她要的是「现在是什么时候」，不是「服务器时间戳」。
_PERIODS = (
    (0, 5, "深夜"),
    (5, 8, "清晨"),
    (8, 11, "上午"),
    (11, 13, "中午"),
    (13, 17, "下午"),
    (17, 19, "傍晚"),
    (19, 23, "晚上"),
    (23, 24, "深夜"),
)


def _period(hour: int) -> str:
    for lo, hi, name in _PERIODS:
        if lo <= hour < hi:
            return name
    return "深夜"


def _state_of(ctx: Dict[str, Any]):
    """优先用引擎传进来的那份状态；没有才自己读盘。

    为什么优先用传进来的：引擎刚 tick 过，内存里那份是最新的；
    从磁盘读会晚一个写盘周期（最多 30 秒），可能出现
    「她说自己醒着，但其实刚睡着」的错位。
    """
    from bigfish import life as _life

    state = ctx.get("life")
    if state is not None:
        return state
    return _life.load()


def _now_of(ctx: Dict[str, Any]) -> float:
    now = ctx.get("now")
    if isinstance(now, datetime):
        return now.timestamp()
    if isinstance(now, (int, float)):
        return float(now)
    return _time.time()


# ══════════════════════════════════════════════════════════════════════
# 注入
# ══════════════════════════════════════════════════════════════════════

def _clock_part(cfg: Dict[str, Any], when: datetime) -> str:
    """时间事实。先立住处境，再谈气氛。"""
    precision = str(cfg.get("precision") or "minute")
    week = _WEEK[when.weekday()]
    if precision == "day":
        line = f"现在是 {when.year} 年 {when.month} 月 {when.day} 日，{week}。"
    else:
        line = (f"现在是 {when.year} 年 {when.month} 月 {when.day} 日 "
                f"{when.hour:02d}:{when.minute:02d}，{week}。")

    parts = ["## 现在", line]
    if cfg.get("hint", True):
        period = _period(when.hour)
        if period == "深夜":
            parts.append("已经是深夜了。对方这个点还醒着。")
        elif period == "清晨":
            parts.append("天刚亮。")
        elif period == "中午":
            parts.append("午饭时间前后。")
        elif period == "傍晚":
            parts.append("天快黑了。")
        else:
            parts.append(f"现在是{period}。")
    return "\n".join(parts)


def _render(ctx: Dict[str, Any]) -> str:
    cfg = ctx.get("config") or {}
    now_ts = _now_of(ctx)
    when = datetime.fromtimestamp(now_ts)

    from bigfish import life as _life

    state = _state_of(ctx)
    parts = [_clock_part(cfg, when)]

    # 状态那一段。没有 life.json 的时候（第一次启动）返回空串 ——
    # 硬塞一个「## 我这边」等于告诉她「你有个状态，但我不告诉你」。
    body = []
    if cfg.get("show_state", True):
        body.append(_life.render(state, now_ts))
    if cfg.get("show_debt", True) and state.sleep_debt >= 45:
        body.append(f"（这两天欠了 {state.sleep_debt:.0f} 分钟睡眠。）")

    body = [b for b in body if b and b.strip()]
    if body:
        tail = ("这是你自己的状态，不是对方说的，也不是要汇报给对方的内容。"
                "它在语气里自然露出来就行：困了就短一点、慢一点，别主动报数值，"
                "也别说「我现在精力 62」这种话。")
        parts.append("## 我这边\n\n" + "\n\n".join(body) + "\n\n" + tail)

    parts.append("（这些是系统给的事实，不是对方说的话。）")
    return "\n\n".join(p for p in parts if p.strip())


# ══════════════════════════════════════════════════════════════════════
# 判定
# ══════════════════════════════════════════════════════════════════════

def _gate(ctx: Dict[str, Any]) -> Tuple[bool, str]:
    """返回 (是否沉默, 原因)。原因会进日志 —— 沉默必须可解释。

    唯一依据是 `life.is_asleep()`，也就是引擎 tick 出来的那份状态，
    不是按「现在几点」临时算的。她的作息每天在抖，熬夜还会推后；
    在这里自己算一遍等于造第二个真源，两边早晚对不上。
    """
    from bigfish import life as _life

    state = _state_of(ctx)
    now = _now_of(ctx)
    if not _life.is_asleep(state, now):
        return False, ""

    cfg = ctx.get("config") or {}

    # 只回主人（可配置）—— 睡着时被陌生人叫醒最不像人
    if cfg.get("only_owner", True):
        tier = str(ctx.get("tier") or "")
        if tier and tier != "owner":
            return False, ""

    since = ""
    try:
        if state.asleep_since:
            since = datetime.fromtimestamp(state.asleep_since).strftime("%H:%M")
    except Exception:
        since = ""

    reason = f"她睡了{f'（{since} 入睡）' if since else ''}"
    if cfg.get("show_wake_at", True):
        try:
            wake = datetime.fromtimestamp(state.wake_at).strftime("%H:%M")
            reason += f"，大约 {wake} 醒"
        except Exception:
            pass
    return True, reason


register(
    "life",
    "生活",
    "让她知道自己现在几点、醒着还是睡了、手头在做什么——而不是永远待机的客服。",
    "prompt+gate",
    order=870,
    default=True,
    why=(
        "这些问题原本一个都不存在：\n"
        "· 提示词里完全没有时间 —— 凌晨两点你说「睡了」，她回一段精神饱满的长文；"
        "周一早上你说「又要上班」，她当成第一次听说。\n"
        "· 更没有「她自己此刻怎么样」—— 所以她永远在线，永远是同一个电量。\n"
        "· 「她睡了」也从来不是一个事实，只是人格里的一句话：模型会说「我困了」，"
        "然后照样秒回。\n"
        "合起来才是「她在过日子」。有它：同一个凌晨三点，她可能刚睡下（不回，"
        "醒来补一句），也可能撑了两小时（回得很短，还说自己困）。"
    ),
    affects=[
        "深夜的回复会变短，因为她真的困",
        "睡着期间的消息不会回，醒来第一句会带一句「我刚看到」",
        "熬夜之后第二天她会起得晚、白天没什么精神",
        "她说话的松紧会跟着时段走：深夜更短，白天更展开",
    ],
    limits=[
        "不替她判断该不该说别的话——同目录的其他判定插件各管各的",
        "不写任何她状态文件里没有的事（不会即兴编「我在做饭」）",
        "不调模型、不联网、不读聊天记录，只读 life.json 和系统时钟",
        "睡着时不叫醒她、不主动发消息（主动开口是另一件事）",
        "关掉之后她照样会回你，但会显得没有昼夜、没有累的时候",
    ],
    config={
        "weather_city": {
            "type": "text",
            "label": "所在城市（用来查天气）",
            "note": "填中文城市名，比如「杭州」「成都」。留空就不查天气。\n"
                    "只把这个地名发给 Open-Meteo（免费、无需注册），"
                    "不发聊天内容、不发任何账号信息。\n"
                    "查到的结果缓存 30 分钟，她会在合适的时候顺口提一句，"
                    "不会变成天气播报。",
            "default": "",
            "maxLength": 40,
        },
        "precision": {
            "type": "select",
            "label": "时间精度",
            "note": "只给日期，她不会盯着分钟说话；给到分钟，临时安排更准。",
            "default": "minute",
            "options": [
                {"value": "minute", "label": "到分钟（03:20，周一）"},
                {"value": "day", "label": "只给日期（3 月 5 日，周一）"},
            ],
        },
        "hint": {
            "type": "boolean",
            "label": "附带时段语气",
            "note": "深夜/清晨/中午这类提示。关掉只留时间事实。",
            "default": True,
        },
        "show_state": {
            "type": "boolean",
            "label": "说出自己的状态",
            "note": "醒着还是睡了、还剩多少力气、手头在做什么。关掉只留时间。",
            "default": True,
        },
        "show_debt": {
            "type": "boolean",
            "label": "说出睡眠不足",
            "note": "欠了 45 分钟以上才提。关掉就只体现在精神状态里，不明说。",
            "default": True,
        },
        "only_owner": {
            "type": "boolean",
            "label": "只对主人安静",
            "note": "打开：睡着时只对主人沉默，其他人照常回。关掉：对所有会话都沉默。",
            "default": True,
        },
        "show_wake_at": {
            "type": "boolean",
            "label": "日志里报醒来时间",
            "note": "只影响日志，不影响她说什么。",
            "default": True,
        },
    },
    render=_render,
    gate=_gate,
)
