"""把一段长话切成若干条能真正发出去的语音。

放这里而不是 voice.py：它只跟"文本怎么断"有关，不碰引擎、不碰配置，
单独一个模块最方便直接测。
"""
from __future__ import annotations

from typing import List

#: 一条语音的字数预算。
#:
#: 微信语音上限 60 秒，代码里 >50 秒就退回文字。按中文 4.5 字/秒算，
#: 200 字约 44 秒 —— 留出标点停顿和情绪拉长音的余量。
VOICE_PART_BUDGET = 200

#: 一条消息最多拆几段。
#:
#: 再多就是"她对着你念稿子"，不是说话。剩下的并进最后一段；
#: 真到 400 字以上（TTS 单次上限）就只能发文字了。
VOICE_PART_MAX = 3

_SENT_END = "。！？!?～~"
_SOFT = "，,、；;：:"


def _find_cut(s: str, budget: int) -> int:
    """在 s[:budget] 里找一个"读起来不突兀"的切点。返回切点下标。"""
    window = s[:budget]
    # ① 句末标点：最好，切完是一句完整的话
    for ch in _SENT_END:
        idx = window.rfind(ch)
        if idx >= budget // 3:
            return idx + 1
    # ② 逗号顿号：次好，至少是一个完整分句
    best = -1
    for ch in _SOFT:
        idx = window.rfind(ch)
        if idx > best:
            best = idx
    if best >= budget // 3:
        return best + 1
    # ③ 空格（英文文本常见）
    idx = window.rfind(" ")
    if idx >= budget // 3:
        return idx + 1
    # ④ 实在没有：硬切。这种情况只会在"一口气 200 字不带标点"时发生，
    #    多半是模型抽风；硬切虽然难看，总比整段发不出去强。
    return budget


def split_for_voice(text: str, *, budget: int = VOICE_PART_BUDGET,
                    max_parts: int = VOICE_PART_MAX) -> List[str]:
    """切成 1~max_parts 段。永远不丢内容：剩下的都并进最后一段。

    为什么允许最后一段超预算：语音的物理上限是 50 秒，但**丢掉半句话**
    比"这条长一点"糟得多 —— 用户听到的是内容，不是秒数。
    真超到 TTS 单次上限（400 字）时，调用方会收到失败并改发文字。
    """
    s = str(text or "").strip()
    if not s:
        return []
    if len(s) <= budget:
        return [s]

    out: List[str] = []
    rest = s
    while rest and len(out) < max_parts:
        if len(rest) <= budget:
            out.append(rest)
            rest = ""
            break
        cut = _find_cut(rest, budget)
        piece = rest[:cut].strip()
        rest = rest[cut:].strip()
        if piece:
            out.append(piece)
    if rest and out:
        # 还有剩下的：并进最后一段，不丢内容
        out[-1] = (out[-1] + rest).strip()
    return out or [s]
