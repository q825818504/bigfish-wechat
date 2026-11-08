#!/usr/bin/env python
"""大肥鱼 · 每轮观测记录自检。

    python tools/selftest_turns.py

`data/turns.jsonl` 一行一轮：等了多久、想了多久、排了多久、发了多久。
它是排查"她为什么慢"的唯一依据，所以格式要稳、裁剪要狠、失败要静默。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bigfish import turns  # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  ✗ {name}" + (f"  —— {detail}" if detail else ""))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="bf_turns_", dir=os.path.join(_ROOT, ".tmp"))
    try:
        turns.record(_root=tmp, chat="c1", chat_name="老爸", outcome="sent",
                     wait_ms=1200, gen_ms=4300, queue_ms=50, send_ms=6000,
                     parts=2, sticker="after")
        rows = turns.recent(5, root=tmp)
        check("写了一条", len(rows) == 1, str(rows))
        if rows:
            r = rows[0]
            check("字段都在",
                  r.get("chat") == "c1" and r.get("outcome") == "sent"
                  and r.get("gen_ms") == 4300 and r.get("parts") == 2,
                  str(r))
            check("没有 None 字段", all(v is not None for v in r.values()), str(r))
            check("带 ts", bool(r.get("ts")), str(r))

        # 缺字段时不写 None（观测不该被空值撑爆）
        turns.record(_root=tmp, chat="c2", outcome="silent", parts=None)
        rows = turns.recent(5, root=tmp)
        check("None 字段被省略", len(rows) == 2 and "parts" not in rows[1],
              str(rows[-1]))

        # 裁剪：超过 KEEP_LINES 只留最后那些
        old = turns.KEEP_LINES
        turns.KEEP_LINES = 5
        try:
            for i in range(12):
                turns.record(_root=tmp, chat=f"c{i}", outcome="sent")
            rows = turns.recent(50, root=tmp)
            check("超过上限会裁剪", len(rows) == 5, f"{len(rows)}")
            check("留的是最新的", rows[-1].get("chat") == "c11",
                  str(rows[-1]))
        finally:
            turns.KEEP_LINES = old

        # 文件不存在时 recent 返回空列表，不抛
        empty = tempfile.mkdtemp(prefix="bf_turns_empty_",
                                 dir=os.path.join(_ROOT, ".tmp"))
        try:
            p = turns.path(empty)
            check("空目录 recents 为空", turns.recent(3) == [] or not os.path.isfile(p))
        finally:
            shutil.rmtree(empty, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILED:
        print("失败项：" + "、".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
