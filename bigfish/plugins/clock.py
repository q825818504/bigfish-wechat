"""[插件] 时间感知 —— 让她知道自己现在几点。

为什么需要它
────────────
这一轮 prompt 里原本**完全没有时间信息**（persona + 心情 + 记忆 + 事实 + 对话）。
所以她不知道现在是凌晨三点，也不知道今天星期几。

结果：凌晨两点你说「睡了」，她回一句精神饱满的长段落；
周一早上你说「又要上班」，她当成第一次听说。

这是最便宜的「她在过日子」信号 —— 不改行为、不调模型、不读任何数据，
只是把「现在」告诉她。

⚠ 只注入时间事实，不替她判断该不该说话。
   「这个点对方还没睡」这类语气提示是可关的（hint 开关），默认开。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

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


def _render(ctx: Dict[str, Any]) -> str:
    cfg = ctx.get("config") or {}
    now = ctx.get("now")
    if not isinstance(now, datetime):
        now = datetime.now()

    precision = str(cfg.get("precision") or "minute")
    week = _WEEK[now.weekday()]
    if precision == "day":
        line = f"现在是 {now.year} 年 {now.month} 月 {now.day} 日，{week}。"
    else:
        line = (f"现在是 {now.year} 年 {now.month} 月 {now.day} 日 "
                f"{now.hour:02d}:{now.minute:02d}，{week}。")

    parts = ["## 现在", line]
    if cfg.get("hint", True):
        period = _period(now.hour)
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
    parts.append("（这是系统给的时间，不是对方说的话。）")
    return "\n".join(parts)


register(
    "clock",
    "时间感知",
    "让她知道自己现在几点、星期几——凌晨两点的回复不该和下午三点一样。",
    "prompt",
    order=870,
    default=True,
    why=(
        "这一轮 prompt 里原本完全没有时间信息（人格 + 心情 + 记忆 + 事实 + 对话），"
        "所以她不知道现在是凌晨三点，也不知道今天星期几。\n"
        "结果很具体：凌晨两点你说「睡了」，她回一段精神饱满的长文；"
        "周一早上你说「又要上班」，她当成第一次听说。\n"
        "这是最便宜的「她在过日子」信号——不改行为、不调模型、不读任何数据，"
        "只是把「现在」告诉她。"
    ),
    affects=[
        "她说话的松紧会跟着时段走：深夜更短，白天更展开",
        "深夜/清晨会多一句语气提示（可以在下面关掉）",
        "这一段排在她「此刻的心情」之前——先知道处境，再谈气氛",
    ],
    limits=[
        "不替她判断该不该回你。那是「判定」类插件的事，这里只给事实",
        "不读记忆、不读聊天记录，只用系统时钟",
        "不写任何文件，关掉即消失，不留痕迹",
    ],
    config={
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
    },
    render=_render,
)
