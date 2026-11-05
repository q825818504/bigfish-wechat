# -*- coding: utf-8 -*-
"""身份判据 —— 这句话到底是谁说的。

## 为什么单独一个模块

2026-11-04 自环事故里，第一个根因是"把角色自己发的话当成了对方说的"。
追下去会发现判据只有一行，而且在两个文件里各写了一遍：

    is_self = (sender == self._me) if sender else False

一个 bool、两个输入、没有第三个值。它有三种静默失手：

  · `sender` 解析不出（空串）→ 当成对方说的
  · `self._me` 空串（db.wxid 读不到）→ 每条都当成对方说的
  · 群里分片缺失导致 `sender` 为空 → 同上

**三种失手全都倒向"对方说的"**，而"对方说的"意味着要回复 ——
所以任何一次失手都是正反馈的起点。

更要命的是它返回 bool：调用方拿不到"我其实不知道"这个信息，
只能二选一，于是每一次不确定都被迫变成一个确定的错误答案。

## 三态，不给分数

    WHO_SELF     确定是自己发的
    WHO_PEER     确定是对方发的
    WHO_UNKNOWN  拿不准

**不引入置信度分数。** 分数会诱使人去调参数，而这里没有可调的余地：
要么知道，要么不知道。判断依据用**证据列表**表达 —— 可回溯、可审计，
出事时能回答"你当时凭什么这么判"。

## 判据顺序

    1. me 为空                       → UNKNOWN  me_missing
    2. sender 非空
       a. 与 me 相同                 → SELF     sender_is_me
       b. 与 me 不同                 → PEER     sender_is_other
    3. sender 为空，读正文前缀（群消息形如 `wxid_xxx: 内容`）
       a. 前缀是 wxid 形态
          · 与 me 相同              → SELF     prefix_is_me
          · 与 me 不同              → PEER     prefix_is_other
       b. 没有前缀 / 前缀不是 wxid   → UNKNOWN  sender_empty

前缀必须**严格**：只认 `^wxid_[A-Za-z0-9_-]+:`。
`loopguard` 里那个宽松正则（`[A-Za-z0-9_@-]{4,}:`）是用来剥前缀的，
`notes: 今天……` 也会被它吃掉；拿它判身份会造出新的误判。

## 放行（这是策略，不是判据）

判出"是谁"之后要不要回，分场景（见 `.planning/specs/A1-identity.md`）：

| 结论 | 私聊 | 群聊 |
| --- | --- | --- |
| SELF | 只有 allow_self 才放行（自测通道） | 同左 |
| PEER | 放行 | 放行（后面还有 @ 判定） |
| UNKNOWN | **不放行**（静默） | **只有 woken 才放行** |

私聊保守、群聊宽松：私聊里自环的代价是刷屏，群聊里漏回的代价是"她今天没理我"。

`woken` 取自 `GroupGate` 的 reason：`at:…` 或 `called:…`。
`group-allow-all` **不算** woken —— 那是"这个群免检"，不是"有人叫她"。

## 用法

    from bigfish import identity

    me = identity.require_me(reader.wxid)          # 空就直接抛，别硬跑
    v  = identity.decide(msg.sender_name, me, text=msg.raw_content,
                         chat_id=msg.chat_id, is_group=is_group)
    d  = identity.admit(v, is_group=is_group, woken=woken,
                        allow_self=cfg.scope("allow_self", chat_id))
    if not d.proceed:
        log(d.reason); return

⚠ 本模块**不碰 IO**（不读库、不写文件、不写日志），可以随便注入、
  也可以在没有微信的机器上跑测试。证据落盘在 `identity_trace.py`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple


# ══════════════════════════════════════════════════════════════════
#  结论
# ══════════════════════════════════════════════════════════════════

WHO_SELF = "self"
WHO_PEER = "peer"
WHO_UNKNOWN = "unknown"

WHO_LABELS = {
    WHO_SELF: "自己",
    WHO_PEER: "对方",
    WHO_UNKNOWN: "拿不准",
}

#: 群消息正文里的发送者前缀。**只认 wxid**，见模块开头第 3 条。
_PREFIX_RE = re.compile(r"^(wxid_[A-Za-z0-9_\-]+):\s?")


class IdentityUnavailable(RuntimeError):
    """拿不到自己的身份，不能继续。

    不是"降级"，是"停止"。不知道自己是谁的时候开口，就是在赌
    "这条不是我自己发的"——11-04 那晚赌输了。
    """

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class Verdict:
    """一条消息的归属判定。"""

    who: str
    rule: str
    #: ((来源, 值), …)。来源是 `sender` / `text_prefix` / `me`。
    evidence: Tuple[Tuple[str, str], ...] = ()
    detail: str = ""

    @property
    def is_self(self) -> bool:
        return self.who == WHO_SELF

    @property
    def certain(self) -> bool:
        """是不是一个"知道"的结论（UNKNOWN 之外都算）。"""
        return self.who != WHO_UNKNOWN

    @property
    def label(self) -> str:
        return WHO_LABELS.get(self.who, self.who)

    def as_dict(self) -> dict:
        return {"who": self.who, "rule": self.rule, "detail": self.detail,
                "evidence": [list(e) for e in self.evidence]}


@dataclass(frozen=True)
class Decision:
    """要不要处理这条消息。

    ⚠ `__bool__` 等于 `proceed`，所以**不要**用 `decision or 默认值` 兜底：
      否决场景下 proceed 恰好是 False，会被当成"没给"，兜底值顶上来，
      于是留痕里写成了别的结论（11-05 真踩过）。一律用
      `decision if decision is not None else ...`。
    """

    proceed: bool
    reason: str
    detail: str = ""

    def __bool__(self) -> bool:
        return self.proceed


# ══════════════════════════════════════════════════════════════════
#  身份前置
# ══════════════════════════════════════════════════════════════════

def require_me(me: object, *, where: str = "") -> str:
    """取自己的 wxid，取不到就抛。

    空串不是"暂时没读到"，是"所有判据都已失效"。
    调用方应该在启动阶段调它，把异常变成一句人话并拒绝启动。
    """
    value = str(me or "").strip()
    if value:
        return value
    spot = f"（{where}）" if where else ""
    raise IdentityUnavailable(
        "identity_me_missing",
        f"读不到自己账号的 wxid{spot}。这种状态下没法判断哪条消息是自己发的，"
        f"所有自环防护都会失效，所以不启动监听。"
        f"请先确认微信已登录、并在「会话」页选定账号。")


def self_id_of(obj: object) -> str:
    """从 reader / db 这类对象上尽力取自己的 wxid。取不到返回空串。

    不抛异常 —— 判断"取没取到"交给 `require_me`。
    """
    for attr in ("wxid", "_wxid"):
        try:
            value = getattr(obj, attr, "")
        except Exception:
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
    db = getattr(obj, "_db", None)
    if db is not None:
        try:
            value = getattr(db, "wxid", "")
        except Exception:
            value = ""
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# ══════════════════════════════════════════════════════════════════
#  判定
# ══════════════════════════════════════════════════════════════════

def _same(a: str, b: str) -> bool:
    # wxid 一般是小写；大小写不同不该让一条"自己的"消息变成"对方的"，
    # 所以比较时忽略大小写，但证据里保留原值。
    return a.strip().lower() == b.strip().lower()


def _by_id(value: str, me: str, *, source: str,
           rule_self: str, rule_peer: str) -> Verdict:
    evidence = ((source, value), ("me", me))
    if _same(value, me):
        return Verdict(WHO_SELF, rule_self, evidence, "发送者就是自己")
    return Verdict(WHO_PEER, rule_peer, evidence, "发送者不是自己")


def decide(sender: object, me: object, *, text: object = "",
           chat_id: object = "", is_group: bool = False) -> Verdict:
    """判定一条消息是谁发的。纯函数，不碰 IO。

    `sender` 是数据库里解析出的发送者 wxid（解析不出就是空串）。
    `text` 用**原始内容**（`raw_content`）：渲染后的正文会被压成
    `[图片]` 之类，前缀就没了。
    """
    me_s = str(me or "").strip()
    sender_s = str(sender or "").strip()
    body = str(text or "")

    if not me_s:
        # 最坏的一种：所有比较都失去意义
        return Verdict(WHO_UNKNOWN, "me_missing",
                       (("me", ""), ("sender", sender_s)),
                       "读不到自己的 wxid，无法判断发送者")

    if sender_s:
        return _by_id(sender_s, me_s, source="sender",
                      rule_self="sender_is_me", rule_peer="sender_is_other")

    match = _PREFIX_RE.match(body)
    if match:
        return _by_id(match.group(1), me_s, source="text_prefix",
                      rule_self="prefix_is_me", rule_peer="prefix_is_other")

    return Verdict(WHO_UNKNOWN, "sender_empty",
                   (("sender", ""), ("me", me_s)),
                   "库里的发送者为空，正文也没有 wxid 前缀")


def for_message(msg: object, me: object) -> Verdict:
    """按 `Incoming` / `ChatMessage` 这类的字段判定。

    优先用 `raw_content`（含前缀的原文），其次 `text` / `content`。
    """
    def field(*names):
        for name in names:
            try:
                value = getattr(msg, name, "")
            except Exception:
                continue
            if isinstance(value, str) and value:
                return value
        return ""

    chat = field("chat_id")
    is_group = str(chat).endswith("@chatroom")
    return decide(
        field("sender_name", "sender"),
        me,
        text=field("raw_content", "text", "content"),
        chat_id=chat,
        is_group=is_group,
    )


# ══════════════════════════════════════════════════════════════════
#  放行
# ══════════════════════════════════════════════════════════════════

def woken_from_reason(reason: object) -> bool:
    """`GroupGate` 的 reason 里，哪些算"真的叫了她"。

    `group-allow-all` 不算 —— 那表示"这个群不用 @ 也回"，
    不是"有人冲她说话"。拿它当 woken 会让宽松策略变成"群里全放行"。
    """
    text = str(reason or "")
    return text.startswith("at:") or text.startswith("called:")


def admit(verdict: Verdict, *, is_group: bool = False, woken: bool = False,
          allow_self: bool = False) -> Decision:
    """判出归属之后，要不要处理这条消息。"""
    if verdict.who == WHO_PEER:
        return Decision(True, "peer", "对方说的")

    if verdict.who == WHO_SELF:
        if allow_self:
            return Decision(True, "self_test", "自测通道：允许回应自己")
        return Decision(False, "self", "自己发的，不回")

    # 拿不准
    if is_group and woken:
        return Decision(
            True, "unknown_group_woken",
            "群里拿不准是谁发的，但这条真的叫了她，按对方处理")
    return Decision(False, "unknown_group" if is_group else "unknown_private",
                    "拿不准是谁发的，不回")


# ══════════════════════════════════════════════════════════════════
#  一步到位（调用方最常用的组合）
# ══════════════════════════════════════════════════════════════════

def judge(sender: object, me: object, *, text: object = "",
          chat_id: object = "", woken_reason: object = "",
          allow_self: bool = False) -> Tuple[Verdict, Decision]:
    """判归属 + 定放行，一次拿到两个结果。

    调用方现在要的几乎总是这两样，分开调容易漏掉 `is_group` 的推导。
    """
    chat = str(chat_id or "")
    is_group = chat.endswith("@chatroom")
    verdict = decide(sender, me, text=text, chat_id=chat, is_group=is_group)
    decision = admit(verdict, is_group=is_group,
                     woken=woken_from_reason(woken_reason),
                     allow_self=allow_self)
    return verdict, decision
