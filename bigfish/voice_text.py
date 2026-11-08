#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""voice_text.py —— 把「屏幕上那句话」变成「能念的那句话」。

TTS 不是读屏器。直接把模型输出丢进去，会出现这些事：

    原文：@老爸 好呀～那明天见 😊 https://example.com/x
    念成：「at 老爸 好呀 波浪号 那明天见 笑脸 https 冒号 斜杠斜杠 …」

    原文：**记住了**（笑）
    念成：「星号星号记住了星号星号 括号笑」

大肥鱼的正文里天然带这些东西：群里第一条有 `@昵称`、说法层会插
`{{split}}`、模型爱用 markdown 和颜文字。所以进 TTS 之前必须清洗。

这个模块只做**不改语义**的清洗：
  · 删掉不该出声的（标记、URL、代码、表情符号）
  · 把符号念法换成人话（`&` → 和，`%` → 百分之，`~` → 停顿）
  · 按引擎上限切段（长句一次念完会被截断或喘不上气）

它**不改写**措辞 —— 「明天去长坂坡」不会变成「明天去长坂坡玩吧」。
改写是模型的事，念出来的是她说的话。
"""

from __future__ import annotations

import re
import sys
import unicodedata
from typing import Iterable, List, Sequence

try:  # 控制台默认 GBK，emoji 会炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

#: 微信 / markdown / 说法层的结构标记，一律不念
_MARKUP = [
    (re.compile(r"\{\{\s*split\s*\}\}", re.I), "，"),   # 说法层的分条标记
    (re.compile(r"\[\[[^\]]{1,40}\]\]"), ""),          # [[sticker:xx]] 之类
    (re.compile(r"```.*?```", re.S), "，"),             # 代码块
    (re.compile(r"`([^`]*)`"), r"\1"),                  # 行内代码
    (re.compile(r"\*\*([^*]*)\*\*"), r"\1"),            # 粗体
    (re.compile(r"__([^_]*)_ _"), r"\1"),
    (re.compile(r"(?m)^\s{0,3}#{1,6}\s*"), ""),         # 标题号
    (re.compile(r"(?m)^\s*[-*+]\s+"), ""),              # 列表符号
    (re.compile(r"<[^>\n]{1,80}>"), ""),                # XML/HTML 标签
]

#: URL 念出来毫无意义，换成「链接」两个字，保留「我说了个链接」这个信息
_URL = re.compile(
    r"(?:https?://|www\.)[^\s，。！？、；：）)】\]]+", re.I)

#: 邮箱同理
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

#: 微信 @：群里第一条会带，念出来像机器
_AT = re.compile(r"@\S{1,30}")

#: 方括号表情：[微笑] [捂脸]
_BRACKET_EMOJI = re.compile(r"\[[^\[\]\n]{1,8}\]")

#: 颜文字：(｡･ω･｡) (´▽｀) 之类
_KAOMOJI = re.compile(r"[（(][^（）()\n]{0,12}[·ω´`▽°口﹏∀≧≦╯╰ノﾉ丿][^（）()\n]{0,12}[）)]")

#: 舞台提示：（笑）（叹气）（小声）—— 模型写给人看的，念出来像旁白。
#: 只删「短 + 命中动作词」的括号，不碰正常括号内容（如「（明天）」）。
_STAGE = re.compile(
    r"[（(][^（）()\n]{0,6}(?:笑|叹气|沉默|小声|认真|无奈|委屈|兴奋|点头|摇头|"
    r"顿了顿|想了想|哽咽|嘟囔|嘀咕|轻笑|苦笑|挑眉|歪头)[^（）()\n]{0,6}[）)]")

#: 连续标点。「！！！」念成三个感叹号很难听
_REPEAT_PUNCT = re.compile(r"([，。！？、；：,.!?;:~～…—\-])\1{1,}")

#: 百分号：数字后面才是「百分之」，单独一个 % 在中文里几乎不出现
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")

#: 空白
_WS = re.compile(r"[ \t\u3000]+")

#: 句末标点（切段用）
_SENT_END = "。！？!?…\n"

#: 逗号级停顿（长句二次切分用）
_COMMA = "，、；：,;:"


def _strip_emoji(text: str) -> str:
    out: List[str] = []
    for ch in text:
        cat = unicodedata.category(ch)
        # So = Symbol other（😊🐟），Cs = 代理项，Cf = 零宽连接符
        if cat in ("So", "Cs", "Cf"):
            continue
        out.append(ch)
    return "".join(out)


#: Breeze 中文音频标签（34 个）。清洗时必须保护，否则会被当成 [微笑] 删掉。
AUDIO_TAGS = frozenset({
    "[笑]", "[轻笑]", "[咯咯笑]",
    "[哭声]", "[抽泣]", "[呜咽]", "[呻吟]", "[哀号]",
    "[叹气]", "[惊呼]", "[吸气]", "[呼气]", "[喘息]",
    "[耳语]", "[喊叫]", "[惨叫]", "[唱歌]", "[哼歌]", "[结巴]", "[停顿]",
    "[清嗓]", "[咳嗽]", "[吸鼻子]",
    "[咂嘴]", "[啧嘴]",
    "[打哈欠]", "[打喷嚏]", "[打嗝]", "[打饱嗝]", "[吞咽]", "[干呕]",
    "[闷哼]", "[冷笑]", "[哼鼻子]",
})

_TAG_TOKEN = "\x00TAG{}\x00"


def _protect_tags(text: str) -> tuple[str, List[str]]:
    saved: List[str] = []

    def repl(match: re.Match[str]) -> str:
        tag = match.group(0)
        if tag in AUDIO_TAGS:
            saved.append(tag)
            return _TAG_TOKEN.format(len(saved) - 1)
        return tag

    return re.sub(r"\[[^\[\]\n]{1,8}\]", repl, text), saved


def _restore_tags(text: str, saved: Sequence[str]) -> str:
    for i, tag in enumerate(saved):
        text = text.replace(_TAG_TOKEN.format(i), tag)
    return text


def clean_for_tts(text: str, *, keep_links: bool = False) -> str:
    """清洗一句/一段要念的话。幂等，不抛异常。"""
    if not text:
        return ""
    s = str(text)
    s, _saved_tags = _protect_tags(s)

    s = _EMAIL.sub("邮箱", s)
    s = _PERCENT.sub(r"百分之\1", s)
    s = _URL.sub("链接" if keep_links else "", s)
    for pattern, repl in _MARKUP:
        s = pattern.sub(repl, s)

    s = _AT.sub("", s)              # @昵称 整个删掉：称呼由声音自己带
    s = _BRACKET_EMOJI.sub("", s)   # [微笑]
    s = _KAOMOJI.sub("", s)         # (｡･ω･｡)
    s = _STAGE.sub("", s)           # （笑）（小声）
    s = _strip_emoji(s)             # 😊🐟

    # 符号 → 人话
    s = (s.replace("&", "和").replace("%", "百分之")
          .replace("~", "，").replace("～", "，")
          .replace("——", "，").replace("--", "，"))

    s = _REPEAT_PUNCT.sub(r"\1", s)
    s = _WS.sub(" ", s)
    s = re.sub(r"\s*\n\s*", "。", s)
    s = re.sub(r"。{2,}", "。", s)
    s = re.sub(r"[，。]{2,}", "，", s)
    s = s.strip(" ，。、；：,.;:")
    return _restore_tags(s.strip(), _saved_tags)


def split_for_tts(text: str, max_chars: int = 120) -> List[str]:
    """按句末标点切段，超长的再按逗号切，最后硬切。

    为什么要切：多数引擎单次请求有 200~500 字上限；而且一整段念下来
    换气点会丢。切成 1~3 句一段，既避开上限，也让停顿自然。
    """
    s = (text or "").strip()
    if not s:
        return []
    if len(s) <= max_chars:
        return [s]

    chunks: List[str] = []
    buf = ""
    for ch in s:
        buf += ch
        if ch in _SENT_END and len(buf) >= max_chars * 0.4:
            chunks.append(buf.strip())
            buf = ""
    if buf.strip():
        chunks.append(buf.strip())

    out: List[str] = []
    for chunk in chunks:
        if len(chunk) <= max_chars:
            out.append(chunk)
            continue
        seg = ""
        for ch in chunk:
            seg += ch
            if ch in _COMMA and len(seg) >= max_chars * 0.5:
                out.append(seg.strip())
                seg = ""
        if seg.strip():
            out.append(seg.strip())

    final: List[str] = []
    for piece in out:
        while len(piece) > max_chars:
            cut = piece.rfind("，", 0, max_chars)
            cut = cut if cut > max_chars * 0.3 else max_chars
            final.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if piece:
            final.append(piece)
    return [p for p in final if p]


def join_for_tts(parts: Sequence[str], *, sep: str = "，") -> str:
    """把分条合成一段（一次念完时用）。去掉重复标点。"""
    cleaned = [clean_for_tts(p) for p in parts if p and p.strip()]
    text = sep.join(cleaned)
    return re.sub(r"[，。]{2,}", "，", text).strip("，。 ")


def selfcheck() -> List[str]:
    """返回失败项。空 = 全过。给 selftest 用。"""
    failures: List[str] = []

    def expect(raw: str, must_not: Iterable[str], must_have: Iterable[str] = ()):
        got = clean_for_tts(raw)
        for bad in must_not:
            if bad and bad in got:
                failures.append(f"{raw!r} 里还留着 {bad!r} → {got!r}")
        for good in must_have:
            if good not in got:
                failures.append(f"{raw!r} 应该包含 {good!r} → {got!r}")

    expect("@老爸 好呀～那明天见", ["@", "～"], ["好呀", "那明天见"])
    expect("**记住了**（笑）", ["**", "(", "笑"], ["记住了"])
    expect("看这个 https://a.b/c 挺好", ["http", "a.b"], ["看这个", "挺好"])
    expect("{{split}}我先说一句", ["{{", "split"], ["我先说一句"])
    expect("[[sticker:开心]]晚安", ["[[", "sticker"], ["晚安"])
    expect("好耶😊🐟", ["😊", "🐟"], ["好耶"])
    expect("[微笑]在吗", ["[", "微笑"], ["在吗"])
    expect("今天100%确定", ["%"], ["百分之"])
    expect("A&B", ["&"], ["A和B"])

    chunks = split_for_tts("一" * 300, max_chars=50)
    if not chunks or any(len(c) > 50 for c in chunks):
        failures.append(f"切段失败：{[len(c) for c in chunks]}")
    if "".join(chunks) != "一" * 300:
        failures.append("切段丢字了")
    return failures


if __name__ == "__main__":
    samples = [
        "@老爸 好呀～那明天见 😊 https://example.com/x",
        "**记住了**（笑）{{split}}明天别忘了带伞",
        "今天100%确定，A&B 两家都涨价了[微笑]",
    ]
    for s in samples:
        print("原：", s)
        print("念：", clean_for_tts(s))
        print()
    bad = selfcheck()
    print("自检：", "全过" if not bad else "\n".join(bad))
