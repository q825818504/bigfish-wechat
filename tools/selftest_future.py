"""期待账本体检：日期解析 / 到期查询 / 兑现。

跑法：
    python tools/selftest_future.py

不调模型、不联网；数据库在 .tmp/ 下，不碰用户真实数据。

## 守哪几条底线

1. **解析不出就不记**：没有明确日期的句子返回 0，不猜。
2. **不许造日子**："过几天""月底""有空的时候"都不是期限。
3. **到期才提醒**：窗口外（太早/太晚）都不返回。
4. **兑现不删行**：resolve 只改 status，时间线留着。
"""
from __future__ import annotations

import datetime as _dt
import os
import shutil
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import bond as B  # noqa: E402
from bigfish import future as F  # noqa: E402

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


def _d(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def main() -> int:
    print("=" * 60)
    print("  期待账本体检 · future")
    print("=" * 60)
    print()

    # 固定一个"现在"：2026-11-11（周三）10:00
    now = _dt.datetime(2026, 11, 11, 10, 0, 0).timestamp()

    # ── 1. 相对日 ──
    print("── 1 · 相对日 ──")
    check("明天", _d(F.parse_due("明天去体检", now)) == "2026-11-12",
          _d(F.parse_due("明天去体检", now)))
    check("后天", _d(F.parse_due("后天回来", now)) == "2026-11-13")
    check("大后天", _d(F.parse_due("大后天", now)) == "2026-11-14")
    check("今天", _d(F.parse_due("今天下午", now)) == "2026-11-11")
    check("大后天优先于后天", _d(F.parse_due("大后天", now)) == "2026-11-14")

    # ── 2. 周几 ──
    print()
    print("── 2 · 周几 ──")
    # 2026-11-11 是周三；下周三 = 11-18
    check("下周三", _d(F.parse_due("下周三体检", now)) == "2026-11-18",
          _d(F.parse_due("下周三体检", now)))
    check("下周日", _d(F.parse_due("下星期日", now)) == "2026-11-22",
          _d(F.parse_due("下星期日", now)))
    check("周三（今天就是）", _d(F.parse_due("周三", now)) == "2026-11-11")
    check("周五（未来最近）", _d(F.parse_due("周五", now)) == "2026-11-13")
    check("星期三（全称）", _d(F.parse_due("星期三", now)) == "2026-11-11")

    # ── 3. 绝对日期 ──
    print()
    print("── 3 · 绝对日期 ──")
    check("11月20日", _d(F.parse_due("11月20日", now)) == "2026-11-20")
    check("12月3号", _d(F.parse_due("12月3号", now)) == "2026-12-03")
    check("2026-12-01", _d(F.parse_due("2026-12-01", now)) == "2026-12-01")
    # 中文里没有词边界：这三种写法都真实出现过，`\b` 会全部漏掉
    check("11-20 面试（带空格）", _d(F.parse_due("11-20 面试", now)) == "2026-11-20")
    check("12-03号（后接中文）", _d(F.parse_due("12-03号", now)) == "2026-12-03")
    check("约在11-20（前接中文）", _d(F.parse_due("约在11-20", now)) == "2026-11-20")
    check("1月3日（已过，顺延明年）",
          _d(F.parse_due("1月3日", now)) == "2027-01-03",
          _d(F.parse_due("1月3日", now)))

    # ── 4. 不猜 ──
    print()
    print("── 4 · 不猜 ──")
    for s in ("过几天", "月底", "有空的时候", "改天", "", "他说他会来"):
        check(f"「{s or '空'}」→ 0", F.parse_due(s, now) == 0.0, str(F.parse_due(s, now)))

    # ── 5. 到期查询 ──
    print()
    print("── 5 · 到期查询 ──")
    tmp = os.path.join(_ROOT, ".tmp", "selftest_future")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    db = os.path.join(tmp, "mem.sqlite3")
    B.ensure_schema(db)

    ok, why = B.append_event(
        db, "dafeiyu", "private:wechat:x", kind="promise",
        what="去体检", evidence="下周三要去体检", at=now,
        due_at=now + 86400)
    check("写入带 due_at 的约定", ok, why)

    rows = F.upcoming(db, "dafeiyu", "private:wechat:x", now=now)
    check("24 小时内能查到", len(rows) == 1, str(len(rows)))
    check("what 正确", rows and rows[0].get("what") == "去体检")

    far = F.upcoming(db, "dafeiyu", "private:wechat:x", now=now + 3 * 86400)
    check("过了窗口查不到", far == [], str(far))

    early = F.upcoming(db, "dafeiyu", "private:wechat:x", now=now - 3 * 86400)
    check("太早查不到", early == [], str(early))

    # 无期限的约定不进 upcoming
    B.append_event(db, "dafeiyu", "private:wechat:x", kind="promise",
                   what="以后一起去", at=now, due_at=0)
    rows2 = F.upcoming(db, "dafeiyu", "private:wechat:x", now=now)
    check("无期限不进 upcoming", len(rows2) == 1, str(len(rows2)))

    # ── 6. 渲染 ──
    print()
    print("── 6 · 渲染 ──")
    line = F.due_line(rows[0], now=now)
    check("明天：...", line.startswith("明天"), line)
    check("含 what", "去体检" in line, line)

    line_today = F.due_line({"what": "体检", "due_at": int(now * 1000)}, now=now)
    check("今天：...", line_today.startswith("今天"), line_today)

    check("空 what → 空串", F.due_line({"what": "", "due_at": 0}, now=now) == "")

    # ── 7. 兑现 ──
    print()
    print("── 7 · 兑现 ──")
    eid = int(rows[0]["id"])
    check("resolve 成功", F.resolve(db, eid) is True)
    rows3 = F.upcoming(db, "dafeiyu", "private:wechat:x", now=now)
    check("兑现后不再提醒", rows3 == [], str(rows3))
    # 行还在（时间线）—— 用原始 SQL 查总数：list_events 只返回 active，
    # 兑现后的行按设计就不在里面了。
    try:
        from bigfish.memory import connect as _conn

        _c = _conn(db, read_only=True)
        _n = _c.execute("SELECT COUNT(*) FROM bond_events").fetchone()[0]
        _c.close()
    except Exception as exc:
        _n = -1
        print(f"    (查库失败：{exc})")
    check("行没被删", _n >= 2, str(_n))

    shutil.rmtree(tmp, ignore_errors=True)

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
