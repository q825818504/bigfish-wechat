"""记忆降级回归测试 —— T-3。

跑法：
    python tools/selftest_degrade.py

不需要网络、不需要微信。用临时库，不碰真实 data。

覆盖：
  · 正常命中 → level="ok"
  · 查询没命中 → 仍然是 "ok"（没命中不是降级）
  · 库不存在 → ("empty", [])
  · 库损坏（垃圾字节）→ "recent" 或 "empty"，**绝不抛异常**
  · recent_chunks：按时间倒序，score 递减
  · bridge_client 在桥接失败时用本地库兜底，并给出 degraded 标记
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import bridge_client as BC  # noqa: E402
from bigfish import memory as M  # noqa: E402
from bigfish import vault as V  # noqa: E402

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


def _make_db(path: str) -> None:
    """建一个只有两条原文的最小库。"""
    ok, err = M.ensure_schema(path)
    assert ok, err
    conn = M.connect(path)
    try:
        for i, (txt, t) in enumerate([("我最近牙疼", 1000), ("我上周拔过智齿", 2000)]):
            conn.execute(
                "INSERT INTO messages (bank, session, event_key, role, text,"
                " content_json, recorded_at, source_time) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("dafeiyu", "s1", f"e{i}", "user", txt, "{}", t, t))
            mid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO chunks (message, start, end, text, activated) "
                "VALUES (?,?,?,?,?)", (mid, 0, len(txt), txt, 0))
            cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            # FTS 的 terms 必须是分词后的形式（与 memory.fts_query 的 bigram 对齐）
            grams = " ".join(txt[i:i + 2] for i in range(max(1, len(txt) - 1)))
            conn.execute("INSERT INTO search_index (rowid, terms) VALUES (?,?)",
                         (cid, f"{txt} {grams}"))
        conn.commit()
    finally:
        conn.close()


def test_ok(tmp: str) -> None:
    print("\n== 正常命中 ==")
    db = os.path.join(tmp, "memory.sqlite3")
    _make_db(db)
    items, level = M.search_with_fallback(db, "dafeiyu", "牙疼")
    check("level=ok", level == "ok", level)
    check("有命中", len(items) > 0, str(len(items)))
    check("返回带 score", bool(items and "score" in items[0]), str(items[:1]))


def test_miss(tmp: str) -> None:
    print("\n== 没命中 ==")
    db = os.path.join(tmp, "memory.sqlite3")
    items, level = M.search_with_fallback(db, "dafeiyu", "量子力学")
    check("level 仍是 ok", level == "ok", level)
    check("空结果", items == [], str(items))


def test_missing(tmp: str) -> None:
    print("\n== 库不存在 ==")
    items, level = M.search_with_fallback(
        os.path.join(tmp, "nope.sqlite3"), "dafeiyu", "牙疼")
    check("level=empty", level == "empty", level)
    check("空结果", items == [], str(items))


def test_broken(tmp: str) -> None:
    print("\n== 库损坏 ==")
    bad = os.path.join(tmp, "broken.sqlite3")
    with open(bad, "wb") as f:
        f.write(b"not a sqlite file" * 100)
    try:
        items, level = M.search_with_fallback(bad, "dafeiyu", "牙疼")
        check("没抛异常", True)
        check("降级标记", level in ("recent", "empty"), level)
        check("结果为空", items == [], str(items))
    except Exception as exc:  # 不该发生
        check("没抛异常", False, repr(exc))


def test_recent(tmp: str) -> None:
    print("\n== recent_chunks ==")
    db = os.path.join(tmp, "memory.sqlite3")
    items = M.recent_chunks(db, "dafeiyu", limit=10)
    check("有两条", len(items) == 2, str(len(items)))
    check("时间倒序", items[0]["time"] >= items[1]["time"],
          f"{items[0]['time']} vs {items[1]['time']}")
    check("score 递减", items[0]["score"] >= items[1]["score"],
          f"{items[0]['score']} vs {items[1]['score']}")
    check("文本完整", items[0]["text"] == "我上周拔过智齿", items[0]["text"])


def test_bridge_fallback(tmp: str) -> None:
    print("\n== 桥接失败 → 本地兜底 ==")
    db = os.path.join(tmp, "memory.sqlite3")
    orig_path = V.memory_db_path
    orig_bridge = BC._load_context_bridge

    V.memory_db_path = lambda *a, **k: db        # type: ignore[assignment]
    # 模拟桥接挂掉：返回一个带 warning、没有记忆的空上下文
    def dead_bridge(**kw):
        ctx = BC.Context()
        ctx.warnings.append("桥接超时")
        return ctx

    BC._load_context_bridge = dead_bridge        # type: ignore[assignment]
    try:
        ctx = BC.load_context(query="牙疼")
        check("degraded=recent", ctx.degraded == "recent", ctx.degraded)
        check("兜底有记忆", bool(ctx.memory_text), ctx.memory_text[:40])
        check("记录了原因", any("兜底" in w for w in ctx.warnings),
              str(ctx.warnings))

        # 桥接正常但没命中 → 不降级
        def ok_bridge(**kw):
            c = BC.Context()
            c.memory_text = '[{"role":"user","text":"旧话"}]'
            return c

        BC._load_context_bridge = ok_bridge      # type: ignore[assignment]
        ctx2 = BC.load_context(query="牙疼")
        check("正常时不降级", ctx2.degraded == "", ctx2.degraded)

        # 桥接失败 + 本地库也没有 → empty
        V.memory_db_path = lambda *a, **k: os.path.join(tmp, "nope.sqlite3")  # type: ignore
        BC._load_context_bridge = dead_bridge    # type: ignore[assignment]
        ctx3 = BC.load_context(query="牙疼")
        check("全挂 → empty", ctx3.degraded == "empty", ctx3.degraded)
    finally:
        V.memory_db_path = orig_path            # type: ignore[assignment]
        BC._load_context_bridge = orig_bridge    # type: ignore[assignment]


def main() -> int:
    print("记忆降级回归测试（bigfish/memory.py + bridge_client.py）")
    tmp = tempfile.mkdtemp(prefix="bigfish-degrade-")
    try:
        test_ok(tmp)
        test_miss(tmp)
        test_missing(tmp)
        test_broken(tmp)
        test_recent(tmp)
        test_bridge_fallback(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

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
