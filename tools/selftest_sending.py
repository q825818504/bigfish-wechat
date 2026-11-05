"""发送信封回归测试（B-1）。

跑法：
    python tools/selftest_sending.py

不需要微信、不需要桌面、不需要网络。

## 为什么有这份测试

以前只有"成功"和"失败"两种结果。可真实世界有三种，第三种是
**"不知道"**：粘贴完了、回车可能按下去了、然后抛了个异常。

把它当成失败，会同时犯两个错：

  · 调用方可能重发 → 对方收到两遍
  · 账本撤掉这一笔 → 读回来没人认得 → **自环从这里开始**

判据来自底层库自己的标记 `_send_attempted`（按回车之前置真）。
这份测试守的是那张判定表：

    前置检查失败（桌面锁 / 打不开会话 / 名字不匹配 / 空内容）→ 确定没发
    走到发送那一步又炸了，或者说不准                          → 未知
    拿不准的时候                                              → 一律未知

覆盖：
  1  ok=True → 已发出
  2  ok=False + attempted=True → 未知
  3  ok=False + attempted=False → 没发出去
  4  SendError(kind="send") → 未知
  5  前置检查的 kind → 没发出去
  6  认不出的 kind → 未知（保守）
  7  非 SendError 的普通异常 → 未知
  8  只有"没发出去"才允许撤账
  9  "未知"永远不可重试
 10  三句话能区分清楚（用户看得懂"可能已发出"）
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import sending as S  # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \u2713 {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  \u2717 {name}" + (f"  <- {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n[{title}]")


class Result:
    def __init__(self, ok: bool, attempted: bool = False, detail: str = ""):
        self.ok = ok
        self.attempted = attempted
        self.detail = detail


class Err:
    def __init__(self, msg: str, kind: str = "", attempted: bool = False):
        self.kind = kind
        self.attempted = attempted
        self.msg = msg

    def __str__(self) -> str:
        return self.msg


# ══════════════════════════════════════════════════════════════════
def test_result() -> None:
    section("1/2/3 按结果判定")
    out = S.from_result(Result(True))
    check("ok=True → 已发出", out.sent and out.state == S.STATE_SENT, out.state)
    check("已发出不该重试（已经发了）", out.retryable is False)

    out = S.from_result(Result(False, attempted=True))
    check("ok=False + 回车可能已投递 → 未知", out.unknown, out.state)
    check("未知的机器码", out.code == "enter_maybe_delivered", out.code)

    out = S.from_result(Result(False, attempted=False))
    check("ok=False + 没走到回车 → 没发出去", out.failed, out.state)


def test_error() -> None:
    section("4/5/6 按异常判定")
    out = S.from_error(Err("发送失败: 窗口没了", kind="send"))
    check("kind=send → 未知", out.unknown, out.state)

    for kind in ("desktop", "window", "open", "identity", "empty",
                 "init", "privilege", "auth"):
        out = S.from_error(Err("x", kind=kind))
        if not out.failed:
            check(f"kind={kind} → 没发出去", False, out.state)
            break
    else:
        check("前置检查的错误 → 没发出去", True)

    out = S.from_error(Err("奇怪的东西", kind="who_knows"))
    check("认不出的 kind → 未知（保守）", out.unknown, out.state)

    out = S.from_error(RuntimeError("裸异常"))
    check("非 SendError 的异常 → 未知", out.unknown, out.state)

    # 前置 kind 但 attempted=True：底层说回车可能按了，那就不能当失败
    out = S.from_error(Err("x", kind="desktop", attempted=True))
    check("kind 看着像前置、但 attempted=True → 未知", out.unknown, out.state)

    section("7 classify 一步到位")
    check("给 result 走结果判定",
          S.classify(result=Result(True)).sent)
    check("给 error 走错误判定",
          S.classify(error=Err("x", kind="send")).unknown)
    check("都没给 → 未知（不假装成功）",
          S.classify().unknown, S.classify().state)


def test_raw_returns() -> None:
    section("8 兼容裸返回值")
    check("send 返回 True → 已发出", S.classify(result=True).sent)
    check("send 返回 False → 没发出去", S.classify(result=False).failed)
    check("send 返回 dict{'ok':True} → 已发出",
          S.classify(result={"ok": True}).sent)
    check("send 返回 dict{'ok':False} → 没发出去",
          S.classify(result={"ok": False}).failed)
    check("什么都没返回 → 未知（不假装成功）", S.classify(result=None).unknown)


def test_sender_resets_attempted() -> None:
    """底层 `_send_attempted` 是上一轮留下的，必须先清掉。

    它只在 `send_text()` 里被重置；如果这次连 `send_text` 都没进
    （比如打不开会话就返回了），读到的就是**上一次**的 True ——
    那会把"确定没发"误判成"未知"，于是账本留着、这一轮白白断掉。
    """
    section("9 发送器先清旧标记")
    from bigfish import wechat_sender as W

    class Uia:
        _send_attempted = True          # 上一轮留下的

        def send_text_to(self, text, who):
            return False                # 这次根本没走到 send_text

    class Sender(W.UiaSender):
        def __init__(self):
            self._uia = Uia()

        def wake(self):
            pass

        def current_chat(self):
            return "谁"

        def open_chat(self, who):
            return True

        def _input_name(self):
            return "谁"

    old_desktop = W.desktop_available
    old_visible = W.wechat_window_visible
    W.desktop_available = lambda: True
    W.wechat_window_visible = lambda: True
    try:
        r = Sender().send("谁", "内容", verify_identity=False)
        check("结果确实没发出去", r.ok is False)
        check("没发出去的 attempted 必须是 False（不是上一轮的旧值）",
              r.attempted is False, str(r.attempted))
    finally:
        W.desktop_available = old_desktop
        W.wechat_window_visible = old_visible


def test_consequences() -> None:
    section("8/9 后果")
    sent = S.from_result(Result(True))
    failed = S.from_result(Result(False, attempted=False))
    unknown = S.from_result(Result(False, attempted=True))

    check("只有'没发出去'才撤账", failed.keep_ledger is False)
    check("'未知'要留账（否则读回来没人认）", unknown.keep_ledger is True)
    check("'已发出'当然留账", sent.keep_ledger is True)

    check("'未知'永远不可重试", unknown.retryable is False)
    check("'没发出去'可以重试", failed.retryable is True)

    section("10 人话")
    ok = S.human(sent, who="老爸")
    bad = S.human(failed, who="老爸")
    unsure = S.human(unknown, who="老爸")
    check("三种说法两两不同", len({ok, bad, unsure}) == 3, f"{ok}|{bad}|{unsure}")
    check("未知不能说成失败", "未知" in unsure and "没发出去" not in unsure, unsure)
    check("未知要说明不会重发", "不会重发" in unsure, unsure)
    check("失败要说清原因", "没发出去" in bad, bad)


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    print("发送信封回归测试（bigfish/sending.py）")
    test_result()
    test_error()
    test_raw_returns()
    test_sender_resets_attempted()
    test_consequences()

    print(f"\n{'=' * 46}")
    total = PASS + FAIL
    if FAIL:
        print(f"失败 {FAIL}/{total}")
        for n in FAILED:
            print(f"  - {n}")
        return 1
    print(f"全部通过 {PASS}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
