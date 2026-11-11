"""道别体检：`_goodnight_busy` 不会永久为 True。

跑法：
    python tools/selftest_goodnight.py

不调模型、不联网、不碰用户真实数据。

## 为什么值得单开

`_maybe_goodnight` 里 `self._goodnight_busy = True` 之后才 `Thread.start()`。
如果 start 抛异常（线程数耗尽 / 解释器正在退出），`work()` 根本不会跑，
它内部的 `finally` 也就不会执行 —— 这个标志**永久为 True**，
道别功能从此静默失效，日志里连一行都没有。

v0.13.2 审计发现，补了 try/except 复位。这个用例就是证明它真的复位：
把 `Thread.start` 换成一个必抛的函数，再调一次。

## 守哪几条底线

1. 置位后 start 失败 → 必须复位。
2. start 成功 → 保持置位（work 还没跑完，不能重复开一轮）。
3. 已经 busy → 直接返回，不重复置位/不重复开线程。
4. 今天已道别 → 不再进入。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import life as L  # noqa: E402
from bigfish import world as W  # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []


def _pick_marks() -> tuple[str, str]:
    try:
        "\u2713".encode(sys.stdout.encoding or "utf-8")
        return "\u2713", "\u2717"
    except (UnicodeEncodeError, LookupError):
        return "[OK]", "[X]"


OK_MARK, BAD_MARK = _pick_marks()


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  {OK_MARK} {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  {BAD_MARK} {name}" + (f"  ({detail})" if detail else ""))


class _Engine:
    _last_reply_at = 0.0
    owner_id = ""
    _policy = None

    def _compose(self, **kwargs):
        return {"sys_parts": [], "user_parts": []}

    def _say_plan(self, text, policy=None):
        return [text], [0.0]


class _World(W.World):
    """只装 `_maybe_goodnight` 需要的几样东西，绕开完整引擎装配。"""

    def __init__(self) -> None:
        super().__init__(_Engine())
        self.logs: list = []

    def _life(self):
        return SimpleNamespace(asleep=False, energy=50.0, bedtime=0.0,
                               extended_minutes=0.0)

    def owner_chat(self):
        return ("chat1", "老爸")

    def _mcfg(self):
        return None

    def _log(self, text, level=""):
        self.logs.append((str(text), level))


def _patch(*, due: bool, start=None):
    """临时替换 goodnight_due 与 Thread.start，返回 restore。"""
    orig_due = L.goodnight_due
    orig_start = threading.Thread.start
    L.goodnight_due = lambda *a, **k: due
    if start is not None:
        threading.Thread.start = start

    def restore():
        L.goodnight_due = orig_due
        threading.Thread.start = orig_start

    return restore


def main() -> int:
    print("=" * 60)
    print("  道别体检 · _goodnight_busy")
    print("=" * 60)
    print()

    now = time.time()

    # ── 1. 正常置位 ──
    print("── 1 · 正常置位 ──")
    w = _World()
    restore = _patch(due=True, start=lambda self: None)  # 不执行 target
    try:
        w._maybe_goodnight(now)
    finally:
        restore()
    check("置位为 True", w._goodnight_busy is True)
    check("记了今天", w._goodnight_day == L.day_key(now), w._goodnight_day)

    # ── 2. start 抛异常 → 必须复位 ──
    print()
    print("── 2 · start 抛异常 → 复位 ──")

    def _boom(self):
        raise RuntimeError("thread pool exhausted")

    w2 = _World()
    restore = _patch(due=True, start=_boom)
    try:
        w2._maybe_goodnight(now)
    finally:
        restore()
    check("复位为 False", w2._goodnight_busy is False, str(w2._goodnight_busy))
    check("记了「线程启动失败」",
          any("线程启动失败" in t for t, _ in w2.logs), str(w2.logs))

    # ── 3. 已 busy → 不重复进入 ──
    print()
    print("── 3 · 已 busy → 不重复 ──")
    w3 = _World()
    w3._goodnight_busy = True
    calls = {"n": 0}

    def _count_start(self):
        calls["n"] += 1

    restore = _patch(due=True, start=_count_start)
    try:
        w3._maybe_goodnight(now)
    finally:
        restore()
    check("没有起新线程", calls["n"] == 0, str(calls))
    check("仍然 busy", w3._goodnight_busy is True)

    # ── 4. 今天已道别 → 不再进入 ──
    print()
    print("── 4 · 今天已道别 ──")
    w4 = _World()
    w4._goodnight_day = L.day_key(now)
    calls2 = {"n": 0}

    def _count_start2(self):
        calls2["n"] += 1

    restore = _patch(due=True, start=_count_start2)
    try:
        w4._maybe_goodnight(now)
    finally:
        restore()
    check("没有起线程", calls2["n"] == 0, str(calls2))
    check("没有置 busy", w4._goodnight_busy is False)

    # ── 5. 不该道别时不进入 ──
    print()
    print("── 5 · 不该道别 ──")
    w5 = _World()
    calls3 = {"n": 0}

    def _count_start3(self):
        calls3["n"] += 1

    restore = _patch(due=False, start=_count_start3)
    try:
        w5._maybe_goodnight(now)
    finally:
        restore()
    check("due=False 时不起线程", calls3["n"] == 0, str(calls3))
    check("没有置 busy", w5._goodnight_busy is False)

    # ── 出口 ──
    print()
    print("=" * 60)
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
