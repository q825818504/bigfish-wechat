"""并发体检：队列满时不丢。

跑法：
    python tools/selftest_concurrency.py

不调模型、不联网、不碰用户真实数据。

## 为什么值得单开

v0.13.2 的 `_gen_queue` 满 64 就直接丢消息，用户感知是"她没回"，
日志里只有一行。那段逻辑原来在 `desktop._run()` 的闭包里，
**没有任何用例能构造"队列满"**——所以只能靠读代码发现。

抽到 `bigfish/concurrency.py` 之后，这里用 `maxsize=1` 的队列把
三条路径都走一遍：直投 / 等位 / 落盘。

## 守哪几条底线

1. **调用方永不阻塞**：等位发生在短命线程里。
2. **等不到就落盘**：不静默丢。
3. **线程起不来也要落盘**：不能因为 spawn 失败就丢消息。
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish.concurrency import enqueue_or_spill  # noqa: E402

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


class _SyncSpawn:
    """同步 spawn：start() 直接执行 target，便于断言（不引入线程时序）。"""

    def __init__(self, *args, **kwargs):
        self._target = kwargs.get("target")

    def start(self) -> None:
        if self._target:
            self._target()


class _BoomSpawn:
    def __init__(self, *args, **kwargs):
        pass

    def start(self) -> None:
        raise RuntimeError("thread pool exhausted")


def main() -> int:
    print("=" * 60)
    print("  并发体检 · enqueue_or_spill")
    print("=" * 60)
    print()

    # ── 1. 空队列：直投 ──
    print("── 1 · 直投 ──")
    q = queue.Queue(maxsize=1)
    logs: list = []
    spilled: list = []
    r = enqueue_or_spill(q, "msg-1", log=lambda *a: logs.append(a),
                         spill=spilled.append)
    check("返回 queued", r == "queued", r)
    check("消息进了队列", q.get_nowait() == "msg-1")
    check("没有 spill", spilled == [])
    check("没有多余日志", logs == [], str(logs))

    # ── 2. 队列满 + 等不到位 → 落盘 ──
    print()
    print("── 2 · 等不到位 → 落盘 ──")
    q2 = queue.Queue(maxsize=1)
    q2.put_nowait("占位")
    spilled2: list = []
    logs2: list = []
    r2 = enqueue_or_spill(q2, "msg-2", log=lambda *a: logs2.append(a),
                          spill=spilled2.append, wait_seconds=0.01,
                          spawn=_SyncSpawn)
    check("返回 waiting", r2 == "waiting", r2)
    check("落盘收到消息", spilled2 == ["msg-2"], str(spilled2))
    check("记了「仍满」日志",
          any("仍满" in str(x) for x in logs2), str(logs2))
    check("原占位没被动", q2.get_nowait() == "占位")

    # ── 3. 队列满 + 线程起不来 → 落盘 ──
    print()
    print("── 3 · 线程起不来 → 落盘 ──")
    q3 = queue.Queue(maxsize=1)
    q3.put_nowait("占位")
    spilled3: list = []
    logs3: list = []
    r3 = enqueue_or_spill(q3, "msg-3", log=lambda *a: logs3.append(a),
                          spill=spilled3.append, spawn=_BoomSpawn)
    check("返回 spawn_failed", r3 == "spawn_failed", r3)
    check("落盘收到消息", spilled3 == ["msg-3"], str(spilled3))
    check("记了「起不来」日志",
          any("起不来" in str(x) for x in logs3), str(logs3))

    # ── 4. 队列满 + 有人消费 → 等位成功 ──
    print()
    print("── 4 · 等位成功 ──")
    q4 = queue.Queue(maxsize=1)
    q4.put_nowait("占位")
    spilled4: list = []
    r4 = enqueue_or_spill(q4, "msg-4", log=lambda *a: None,
                          spill=spilled4.append, wait_seconds=2.0)
    check("返回 waiting", r4 == "waiting", r4)
    got = q4.get(timeout=1.0)      # 消费占位，腾出位置
    check("腾位后原占位先出", got == "占位", str(got))
    got2 = q4.get(timeout=2.0)     # 等位线程应把 msg-4 放进来
    check("等位线程放进了消息", got2 == "msg-4", str(got2))
    check("等位成功不落盘", spilled4 == [], str(spilled4))

    # ── 5. 不阻塞调用方 ──
    print()
    print("── 5 · 不阻塞 ──")
    q5 = queue.Queue(maxsize=1)
    q5.put_nowait("占位")
    t0 = time.time()
    enqueue_or_spill(q5, "msg-5", log=lambda *a: None,
                     spill=lambda m: None, wait_seconds=5.0)
    dt = time.time() - t0
    check("调用立即返回（<0.5s）", dt < 0.5, f"{dt:.3f}s")

    # 收尾：把等位线程放进去的东西取走，避免守护线程悬着
    try:
        q5.get(timeout=0.2)
        q5.get(timeout=0.2)
    except Exception:
        pass

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
