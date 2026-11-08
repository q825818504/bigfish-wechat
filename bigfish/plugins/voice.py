# -*- coding: utf-8 -*-
"""声音插件：开关与「侧边栏声音页」的入口。

它自己不合成、不发送 —— 合成在 `bigfish/voice.py`，发送在投递层。
插件只负责回答一件事：**这个能力开没开**。开关打开后，界面才会
在侧边栏显示「声音」板块，那边配置 API 地址、Key 和参数。
"""
from __future__ import annotations

from typing import Any, Dict

from . import register


def tick(ctx: Dict[str, Any]) -> None:
    """hook 形态的占位：真正的合成由回复链路按需调用，不靠轮询。"""
    return None


def render(ctx: Dict[str, Any]) -> str:
    """只在会自动出声的模式下，告诉模型可以写标记。

    标记写不写、写哪个，模型自己判断；系统只负责把它删掉再去合成。
    """
    try:
        from bigfish.voice import VoiceSettings
        from bigfish.voice_policy import VoicePolicy

        s = VoiceSettings.from_config()
        p = VoicePolicy.from_config(s.policy)
        if p.mode not in ("auto", "probability", "always"):
            return ""
        if not s.api_key or not s.voice:
            return ""
        return (
            "## 这一轮要不要出声\n\n"
            "对方可能听到这条回复的语音。你可以在正文最前面写 "
            "`[[voice]]`（更适合被听见：短、有情绪、像当面说）或 "
            "`[[text]]`（更适合看：代码、链接、表格、长说明、清单）。\n"
            "标记不会显示给对方，也不会被念出来。拿不准就什么都别写，"
            "系统会按场景和概率决定。"
        )
    except Exception:
        return ""


register(
    "voice",
    "声音",
    "让她用自己的声音说话；配置在侧边栏「声音」页",
    "hook+prompt",
    order=895,
    default=False,
    tick=tick,
    render=render,
    why="不开也能聊天，只是她永远只有文字；开了之后，一轮回复可以变成一段"
        "用她自己音色念出来的声音。",
    affects=[
        "多一次声音模型请求（会消耗对方积分）",
        "音频落 <数据目录>/voice/，同句会走缓存",
        "配置里会保存 API 地址与 Key",
    ],
    limits=[
        "不改她的措辞，只决定怎么念",
        "语音失败不影响文字，也不会自动重试超时",
        "不会在群里默认说话（可在声音页打开）",
    ],
)
