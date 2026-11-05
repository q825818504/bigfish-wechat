# -*- coding: utf-8 -*-
"""发送信封 —— 发没发出去，不靠猜。

## 为什么要把"不知道"单独拿出来

以前发送只有两种结果：`SendResult(ok=True)`，或者抛 `SendError`。
可真实世界有三种：

    · 确定发出去了
    · 确定没发出去
    · **不知道**（回车可能已经按下去了）

第三种被塞进第二种里，代价是两条路都走错：

  · 当成失败 → 调用方可能重发，对方收到两遍
  · 当成失败 → 账本撤掉这一笔，于是读回来时没人认得，**自环从这里开始**

底层库其实早就看得清这件事：

    # vendor/uia_driver.py
    try:
        # SendKeys 抛异常也可能已经投递回车，调用方不能据 False 重新发送。
        self._send_attempted = True
        e.SendKeys("{Enter}", waitTime=0.05)
    except Exception:
        return False

`_send_attempted` 在**按回车之前**置真，返回 False 之后仍然保留 ——
它就是"到底走到哪一步了"的现成分界线，只是我们一直没读。

## 界线画在哪

    前置检查失败  → FAILED   桌面锁了 / 窗口没了 / 打不开会话 / 名字不匹配
    回车前后不明  → UNKNOWN  粘贴完了，回车抛了，或者库说没成功但已经按过回车

拿不准的时候一律按 **UNKNOWN** 处理。理由：把"可能发了"当成"没发"，
代价是重复发送和漏防自环；把"没发"当成"可能发了"，代价只是这一轮不再往下说。

## 用法

    from bigfish import sending

    try:
        r = sender.send(who, text)
    except SendError as exc:
        out = sending.from_error(exc)
    else:
        out = sending.from_result(r)

    if out.unknown:
        keep_ledger(); tell_the_user(out.detail); return   # 不重试、不撤账
    if out.failed:
        rollback_ledger(); return
"""

from __future__ import annotations

from dataclasses import dataclass


STATE_SENT = "sent"
STATE_FAILED = "failed"
STATE_UNKNOWN = "unknown"

STATE_LABELS = {
    STATE_SENT: "已发出",
    STATE_FAILED: "没发出去",
    STATE_UNKNOWN: "结果未知（可能已发出）",
}

#: 这些 kind 一定发生在"回车之前"——确定没发出去。
PRE_SEND_KINDS = frozenset({
    "empty",      # 内容为空
    "desktop",    # 桌面锁定 / 不可见
    "window",     # 微信窗口不见了
    "open",       # 打不开会话
    "identity",   # 会话名 / 输入框归属校验失败
    "init",       # UIA 初始化失败
    "import",     # 依赖导入失败
    "privilege",  # 权限不足
    "auth",       # iLink 未登录
    "context",    # iLink 缺 context_token
    "media",      # 媒体文件问题
    "sticker",    # 表情素材问题
})

#: 这个 kind 代表"发送调用本身炸了"——走到这一步就说不准了。
SEND_KIND = "send"


@dataclass(frozen=True)
class Outcome:
    """一次发送的结论。

    ⚠ `__bool__` 等于 `sent`，别用 `outcome or 默认值` 兜底 ——
      失败/未知的 outcome 都是 falsy，会被兜底值顶掉。
    """

    state: str
    code: str = ""
    detail: str = ""

    @property
    def sent(self) -> bool:
        return self.state == STATE_SENT

    @property
    def failed(self) -> bool:
        return self.state == STATE_FAILED

    @property
    def unknown(self) -> bool:
        return self.state == STATE_UNKNOWN

    @property
    def label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    @property
    def retryable(self) -> bool:
        """能不能重试。

        未知**永远不能** —— 重试一次就可能让对方收到两遍。
        """
        return self.state == STATE_FAILED

    @property
    def keep_ledger(self) -> bool:
        """账本要不要留着这一笔。

        未知要留：它可能已经发出去了，读回来得认得出来。
        """
        return self.state != STATE_FAILED

    def __bool__(self) -> bool:
        return self.sent

    def as_dict(self) -> dict:
        return {"state": self.state, "code": self.code, "detail": self.detail}


# ══════════════════════════════════════════════════════════════════
#  判定
# ══════════════════════════════════════════════════════════════════

def from_result(result: object) -> Outcome:
    """从 `SendResult` 这类"调用正常返回"的结果判定。

    只有 `ok=False` 且 `attempted=True`（回车可能已投递）才算未知。

    兼容三种返回形态（`say.send_parts` 的 `send` 是注入进来的）：
      · `SendResult` 这类带 `.ok` 的对象
      · dict（`{"ok": True}`）
      · 裸 bool —— 老调用方和测试里很常见。
        这里必须认它，否则 `True` 会被当成失败（曾经就是这么错的）。
    """
    if isinstance(result, bool):
        ok = result
    elif isinstance(result, dict):
        ok = bool(result.get("ok", False))
    else:
        ok = bool(getattr(result, "ok", False))

    if ok:
        return Outcome(STATE_SENT, "ok", "")
    attempted = bool(getattr(result, "attempted", False))
    detail = str(getattr(result, "detail", "") or "")
    if attempted:
        return Outcome(STATE_UNKNOWN, "enter_maybe_delivered",
                       detail or "回车可能已经按下去了")
    return Outcome(STATE_FAILED, "not_sent",
                   detail or "没走到回车这一步")


def from_error(error: object) -> Outcome:
    """从异常判定。

    `SendError.kind` 落在 `PRE_SEND_KINDS` 里 → 确定没发；
    是 `send` 或认不出的 → 按未知处理。
    """
    kind = str(getattr(error, "kind", "") or "")
    detail = str(error) if error is not None else ""
    attempted = bool(getattr(error, "attempted", False))

    if kind in PRE_SEND_KINDS and not attempted:
        return Outcome(STATE_FAILED, kind or "failed", detail)

    if kind == SEND_KIND or attempted:
        return Outcome(STATE_UNKNOWN, "send_maybe_done", detail)

    # 认不出的错误：不知道发生在哪一步，按最坏算
    return Outcome(STATE_UNKNOWN, kind or "unclassified", detail)


def classify(result: object = None, error: object = None) -> Outcome:
    """一步到位：给了 error 走错误判定，否则走结果判定。"""
    if error is not None:
        return from_error(error)
    if result is not None:
        return from_result(result)
    return Outcome(STATE_UNKNOWN, "no_result", "既没有结果也没有错误")


# ══════════════════════════════════════════════════════════════════
#  给用户看的话
# ══════════════════════════════════════════════════════════════════

def human(outcome: Outcome, *, who: str = "") -> str:
    """一条用户看得懂的说明。未知要说得像"不确定"，不能像"失败"。"""
    target = f"给「{who}」的" if who else ""
    if outcome.sent:
        return f"{target}消息已发出"
    if outcome.failed:
        return f"{target}消息没发出去：{outcome.detail or outcome.code}"
    return (f"{target}消息结果未知 —— 可能已经发出去了，所以不会重发。"
            f"（{outcome.detail or outcome.code}）")
