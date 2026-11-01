"""[bigfish] 消息语义化。

微信 4.x 把富媒体消息（图片/表情/文件/引用/红包）都存成 XML 字符串。
直接喂给模型就是噪音。本模块把它们压成一句人能读懂的描述。

原则：
  - **保留语义**，不是原样透传
  - **不上报敏感字段**（aeskey、cdnthumburl、md5 一律丢弃）
  - 文本消息原样保留，只做换行清理
  - 认不出的类型给出类型名，不吞掉
"""
from __future__ import annotations

import re
from typing import Optional

# 微信 4.x 的 local_type 复合码：高 32 位是子类型，低 32 位是主类型。
# 这里只用到基础表；复合码由调用方先拆过再传进来。
TYPE_NAMES = {
    1: "文本",
    3: "图片",
    34: "语音",
    42: "名片",
    43: "视频",
    47: "动画表情",
    48: "位置",
    49: "文件/链接/卡片",
    50: "音视频通话",
    10000: "系统消息",
    11000: "动画表情",
}

# 单条消息渲染后的最大长度。微信里没人发长文，超长的多半是解析异常。
_MAX_TEXT = 400


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _first(patterns, text: str) -> str:
    for p in patterns:
        m = re.search(p, text, re.S | re.I)
        if m:
            v = (m.group(1) or "").strip()
            if v:
                return v
    return ""


def render(content: str, mtype: str) -> str:
    """把一条消息的原始内容压成可读文本。

    参数
        content : 数据库里的原始 content
        mtype   : 已解析好的中文类型名（如「图片」「动画表情」）
    """
    c = content or ""
    if not c:
        return f"[{mtype}]" if mtype else ""

    # ---- 纯文本：只需清理 ----
    if mtype == "文本":
        return _clean_text(c)

    # ---- 微信 XML 信封 ----
    if "<msg>" in c or c.lstrip().startswith("<?xml"):
        return _render_xml(c, mtype)

    # ---- 既不是文本也不是 XML ----
    return f"[{mtype}]" if mtype else _clean_text(c)


def _clean_text(s: str) -> str:
    # 去掉容器头残留的控制字符
    s = s.replace("\x01", "").replace("\x00", "")
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    if len(s) > _MAX_TEXT:
        s = s[:_MAX_TEXT] + "…"
    return s


def _render_xml(c: str, mtype: str) -> str:
    """解析微信富媒体 XML。只取人类关心的字段，丢弃密钥/URL/md5。"""

    # ---------- 图片 ----------
    if mtype == "图片" or "<img" in c:
        return "[图片]"

    # ---------- 动画表情 ----------
    if mtype in ("动画表情",) or "<emoji" in c:
        md5 = _first([r'md5="([0-9a-fA-F]{32})"'], c)
        return "[表情]" if md5 else "[表情]"

    # ---------- 语音 ----------
    if mtype == "语音":
        # <voicemsg voicelength="3000" ...>
        ms = _first([r'voicelength="(\d+)"'], c)
        if ms:
            try:
                sec = max(1, round(int(ms) / 1000))
                return f"[语音 {sec}秒]"
            except ValueError:
                pass
        return "[语音]"

    # ---------- 视频 ----------
    if mtype == "视频":
        ms = _first([r'playlength="(\d+)"'], c)
        if ms:
            try:
                sec = max(1, round(int(ms) / 1000))
                return f"[视频 {sec}秒]"
            except ValueError:
                pass
        return "[视频]"

    # ---------- 音视频通话 ----------
    if mtype == "音视频通话":
        t = _first([r"<msg>(.*?)</msg>", r'<voipmsg[^>]*>(.*?)</voipmsg>'], c)
        t = _strip_tags(t) or _strip_tags(c)
        return f"[通话] {t}"[:60] if t else "[通话]"

    # ---------- 位置 ----------
    if mtype == "位置" or "<location" in c:
        label = _first([r'label="([^"]*)"', r'poiname="([^"]*)"'], c)
        return f"[位置] {label}" if label else "[位置]"

    # ---------- 名片 ----------
    if mtype == "名片":
        nick = _first([r'nickname="([^"]*)"'], c)
        return f"[名片] {nick}" if nick else "[名片]"

    # ---------- 文件 / 链接 / 卡片 / 引用 ----------
    if mtype == "文件/链接/卡片" or "<appmsg" in c:
        return _render_appmsg(c)

    # ---------- 系统消息 ----------
    if mtype == "系统消息":
        t = _strip_tags(c)
        return f"[系统] {t}"[:120] if t else "[系统消息]"

    # ---------- 兜底 ----------
    t = _strip_tags(c)
    return f"[{mtype}] {t}"[:80] if t else f"[{mtype}]"


def _render_appmsg(c: str) -> str:
    """appmsg 是微信最杂的信封：文件、链接、引用、小程序、红包都在里面。"""
    title = _first([r"<title>(.*?)</title>"], c)
    title = _strip_tags(title) if title else ""

    # 引用消息：<refermsg><displayname>…</displayname><content>…</content>
    ref_name = _first([r"<refermsg>.*?<displayname>(.*?)</displayname>"], c)
    ref_cont = _first([r"<refermsg>.*?<content>(.*?)</content>"], c)
    if ref_name or ref_cont:
        ref_cont = _strip_tags(ref_cont)[:60]
        who = ref_name or "某人"
        body = f"[引用 {who}: {ref_cont}]"
        return f"{body} {title}" if title else body

    # 文件
    if "<appattach" in c or re.search(r"<type>\s*6\s*</type>", c):
        size = _first([r"<totallen>(\d+)</totallen>"], c)
        if size:
            try:
                n = int(size)
                human = (f"{n / 1024 / 1024:.1f}MB" if n >= 1024 * 1024
                         else f"{n / 1024:.0f}KB")
                return f"[文件] {title} ({human})" if title else f"[文件] {human}"
            except ValueError:
                pass
        return f"[文件] {title}" if title else "[文件]"

    # 小程序 / 视频号 / 音乐 / 红包 等，统一按卡片处理
    if title:
        return f"[卡片] {title}"
    return "[卡片]"
