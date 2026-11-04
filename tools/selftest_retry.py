"""模型重试回归测试 —— T-1。

跑法：
    python tools/selftest_retry.py

不需要网络、不需要 key、不 sleep（退避被临时改成 0）。

覆盖：
  · 一次成功 → 只调一次
  · 500 两次后成功 → 三次，最终拿到正文
  · 400（key/模型名错）→ 不重试，一次就抛
  · 网络错误 ×3 → 抛，且正好三次
  · 空正文（推理吃光预算）→ 自动放大 max_tokens 再试
  · attempts=1 → 不重试
  · 配置没配好 → 不进入请求
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import replier as R  # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  [X] {name}  {detail}")


def _cfg():
    """一个「已配好」的配置：本地网关，不需要 key。"""
    c = R.ModelConfig()
    c.channel = "magpie"
    c.base_url = "http://127.0.0.1:3425/v1/chat/completions"
    c.status = lambda: (True, "")   # type: ignore[method-assign]
    return c


class FakeCall:
    """按脚本依次返回/抛错，记录每次调用的 max_tokens。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.max_tokens: list[int] = []

    def __call__(self, cfg, system, user):
        self.calls += 1
        self.max_tokens.append(int(getattr(cfg, "max_tokens", 0)))
        item = self.script.pop(0) if self.script else ("ok", "兜底")
        if item[0] == "ok":
            return item[1]
        raise item[1]


def _patch(fake) -> None:
    R._call_once = fake            # type: ignore[assignment]
    R.RETRY_BACKOFF = (0.0, 0.0, 0.0)  # type: ignore[assignment]


def test_once() -> None:
    print("\n== 一次成功 ==")
    fake = FakeCall([("ok", "在呢")])
    _patch(fake)
    out = R.call_model(_cfg(), "s", "u")
    check("返回正文", out == "在呢", out)
    check("只调一次", fake.calls == 1, str(fake.calls))


def test_retry_500() -> None:
    print("\n== 500 两次后成功 ==")
    err = R.ModelError("boom", status_code=500, retryable=True, kind="http")
    fake = FakeCall([("err", err), ("err", err), ("ok", "好了")])
    _patch(fake)
    out = R.call_model(_cfg(), "s", "u")
    check("最终拿到正文", out == "好了", out)
    check("调用三次", fake.calls == 3, str(fake.calls))


def test_no_retry_400() -> None:
    print("\n== 400 不重试 ==")
    err = R.ModelError("bad key", status_code=400, retryable=False, kind="http")
    fake = FakeCall([("err", err)])
    _patch(fake)
    try:
        R.call_model(_cfg(), "s", "u")
        check("应该抛错", False)
    except R.ModelError as exc:
        check("抛出原错误", "bad key" in str(exc), str(exc))
    check("只调一次", fake.calls == 1, str(fake.calls))


def test_network_exhausted() -> None:
    print("\n== 网络错误耗尽 ==")
    err = R.ModelError("断网", retryable=True, kind="network")
    fake = FakeCall([("err", err), ("err", err), ("err", err), ("ok", "不该到这")])
    _patch(fake)
    try:
        R.call_model(_cfg(), "s", "u")
        check("应该抛错", False)
    except R.ModelError as exc:
        check("抛出网络错误", "断网" in str(exc), str(exc))
    check("正好三次", fake.calls == 3, str(fake.calls))


def test_empty_content_bump() -> None:
    print("\n== 空正文自动加预算 ==")
    err = R.ModelError("只有推理", retryable=True, kind="empty_content")
    fake = FakeCall([("err", err), ("ok", "想好了")])
    _patch(fake)
    cfg = _cfg()
    base = int(cfg.max_tokens)
    out = R.call_model(cfg, "s", "u")
    check("重试后成功", out == "想好了", out)
    check("第二次预算被放大", fake.max_tokens[1] > base,
          f"{fake.max_tokens}")
    check("第一次仍是原预算", fake.max_tokens[0] == base,
          f"{fake.max_tokens}")


def test_attempts_one() -> None:
    print("\n== attempts=1 ==")
    err = R.ModelError("boom", status_code=503, retryable=True, kind="http")
    fake = FakeCall([("err", err), ("ok", "不该到这")])
    _patch(fake)
    try:
        R.call_model(_cfg(), "s", "u", attempts=1)
        check("应该抛错", False)
    except R.ModelError:
        check("抛出错误", True)
    check("只调一次", fake.calls == 1, str(fake.calls))


def test_not_ready() -> None:
    print("\n== 配置不完整 ==")
    fake = FakeCall([("ok", "不该到这")])
    _patch(fake)
    cfg = _cfg()
    cfg.status = lambda: (False, "没有 key")   # type: ignore[method-assign]
    try:
        R.call_model(cfg, "s", "u")
        check("应该抛错", False)
    except R.ModelError as exc:
        check("提示没配好", "没配好" in str(exc), str(exc))
    check("没发请求", fake.calls == 0, str(fake.calls))


def main() -> int:
    print("模型重试回归测试（bigfish/replier.py）")
    test_once()
    test_retry_500()
    test_no_retry_400()
    test_network_exhausted()
    test_empty_content_bump()
    test_attempts_one()
    test_not_ready()

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
