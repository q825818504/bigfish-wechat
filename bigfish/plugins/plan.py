"""[插件] 日程 —— 她今天打算做什么。

为什么是 872
────────────
870「生活」回答"此刻"：现在几点、醒着还是睡了、手头在做什么。
872 回答"今天"：还没到的那几件事。
两者紧挨着，因为她读的时候是连着读的——"现在是下午三点，我在收拾屋子，
晚上打算把上次没看完的书看完"。

为什么和「生活」拆开
────────────────────
「生活」是器官（作息、精力、睡眠），关掉它只是她不说；
日程是**她的安排**，关掉它她照样按日程走，只是不再告诉你。

更实际的理由：日程要调一次模型生成（每天醒来一次），
把它塞进「生活」会让「关掉生活插件」变成"关掉她的作息"——
那两件事不该共用一个开关。

⚠ 关掉这个插件不会让她停止按日程做事
────────────────────────────────────
`refresh_doing` 仍然从 life.plan 取当前时段的事，
关闭的只是"她会不会说出来"。这和「生活」的分层完全一致。
"""
from __future__ import annotations

import time as _time
from typing import Any, Dict

from . import register


def _now_of(ctx: Dict[str, Any]) -> float:
    now = ctx.get("now")
    if isinstance(now, float):
        return now
    if isinstance(now, int):
        return float(now)
    return _time.time()


def _render(ctx: Dict[str, Any]) -> str:
    try:
        from bigfish import life as _life

        state = ctx.get("life")
        if state is None:
            state = _life.load()
        return _life.plan_for_prompt(state, now=_now_of(ctx))
    except Exception:
        # 日程读不出来不该让她失语。空串 = 这一段不出现。
        return ""


register(
    "plan",
    "日程",
    "她今天打算做什么——醒来时排两三件事，其中一件接着昨天。",
    "prompt",
    order=872,
    default=True,
    why=(
        "没有它的时候，她只有「手头在做什么」：在看书、在收拾屋子。\n"
        "那是**采样**，不是**经历**——今天和昨天没有区别，\n"
        "因为你问不出「你今天打算干嘛」，她也说不出「我下午要去趟超市」。\n"
        "打开后，她每天醒来会排 2~3 件事，其中至少一件接昨天：\n"
        "昨天没看完的书、昨天提过要去的地方。\n"
        "于是她第一次有了「今天」和「等会儿」。"
    ),
    affects=[
        "每天醒来后生成一次，2~3 条，时间在 08:00–22:00",
        "至少一条接昨天（昨天没做完的事、昨天说过的事）",
        "「手头在做什么」优先从日程里取，静态池降级成兜底",
        "她可以主动分享日程里的事（只分享标了「可以提」的）",
    ],
    limits=[
        "不排会议、面试、就医、长途出行——那会引出「那你怎么有空回我」",
        "不排以你为中心的事（等你消息、想你），那是心情不是日程",
        "不编造材料里没有的重要事件；没材料就排普通日常",
        "生成失败就用旧的静态池，不会让她没有生活",
        "关掉只是她不说，日程照常影响她手头在做什么",
    ],
    render=_render,
)
