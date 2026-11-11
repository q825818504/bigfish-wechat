"""[bigfish] 并发小工具：队列满时**不丢**。

═══════════════════════════════════════════════════════════════════════
为什么单独一个模块
═══════════════════════════════════════════════════════════════════════

这段逻辑原来埋在 `desktop._run()` 的闭包里（`_enqueue_gen`）。闭包没法在
测试里构造"队列满"——于是 v0.13.2 的"满 64 直接丢消息"只能靠读代码发现，
没有任何用例能证明它真的不丢了。

抽出来之后，`tools/selftest_concurrency.py` 可以直接用 `maxsize=1` 的队列
把三条路径都走一遍：直投 / 等位 / 落盘。

═══════════════════════════════════════════════════════════════════════
约定
═══════════════════════════════════════════════════════════════════════

· 调用方**永不阻塞**：等位发生在短命线程里。
· 等位上限 8 秒（一个生成轮次的上限）；仍满就交给 `spill` 落盘。
· 任何一步失败都不抛：调用方是主循环，不能因为队列满而停。
"""
from __future__ import annotations

import queue
import threading
from typing import Any, Callable, Optional

#: 队列满时在后台等多久（秒）。8 秒 ≈ 一次生成轮次。
GEN_WAIT_SECONDS = 8.0


def enqueue_or_spill(q: "queue.Queue", item: Any, *,
                     log: Optional[Callable[..., None]] = None,
                     spill: Optional[Callable[[Any], None]] = None,
                     wait_seconds: float = GEN_WAIT_SECONDS,
                     spawn: Optional[Callable[..., Any]] = None) -> str:
    """把 item 放进队列；满时后台等位；仍满则 spill。

    返回：
        "queued"        直接入队
        "waiting"       起了等位线程（结果异步）
        "spawn_failed"  等位线程起不来，已 spill
    """
    def _log(msg: str, level: str = "") -> None:
        if log is None:
            return
        try:
            log(msg, level)
        except TypeError:
            try:
                log(msg)
            except Exception:
                pass
        except Exception:
            pass

    def _do_spill() -> None:
        if spill is None:
            return
        try:
            spill(item)
        except Exception:
            pass

    try:
        q.put_nowait(item)
        return "queued"
    except queue.Full:
        pass

    def _wait() -> None:
        try:
            q.put(item, timeout=wait_seconds)
            _log("[并发] 生成队列曾满，这条等了位", "think")
        except queue.Full:
            _log(f"[并发] 生成队列 {wait_seconds:.0f} 秒仍满，落盘待恢复", "err")
            _do_spill()

    try:
        maker = spawn or threading.Thread
        maker(target=_wait, daemon=True, name="bf-gen-wait").start()
        return "waiting"
    except Exception as exc:
        _log(f"[并发] 等位线程起不来，落盘：{exc}", "err")
        _do_spill()
        return "spawn_failed"
