"""年表体检：`Soul.history_lines()` —— 她能不能读自己的以前。

跑法：
    python tools/selftest_history.py

不调模型、不联网、不碰用户真实数据（只构造内存里的 soul dict）。

## 为什么单独一个文件

这是 T-1a 的最小切口：`soul.history` 里每天被复盘写一条 `day_summary`，
但在此之前**没有任何注入路径** —— 她"有年表但不读年表"。
把年表塞进提示词有两个风险，所以断言以"不许"为主：

1. **不许和 selfNotes 重复**：`evolved` 已经渲染在「我自己知道的事」里，
   年表再渲染一遍就是同一句话说两次，占预算还像复读。
2. **不许超预算**：8 条 / 250 字是硬上限，超了会挤掉记忆与对话。

## 守哪几条底线

1. 空年表 → 返回空列表，调用方整段不出现（不写"我没什么可说的"）。
2. 只有 evolved → 返回空（重复的不要）。
3. 输出顺序是**旧 → 新**（读起来像时间线，不是倒序流水账）。
4. 脏数据（None / 字符串 / 缺 text）跳过，不抛异常。
5. `render_persona()` 不包含年表（Python/Node 一致性靠这条）。
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish.soul import Soul  # noqa: E402

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


def _soul(history) -> Soul:
    return Soul.from_dict({"identity": {"name": "大肥鱼"}, "history": history})


def main() -> int:
    print("=" * 60)
    print("  年表体检 · Soul.history_lines")
    print("=" * 60)
    print()

    # ── 1. 空与脏 ──
    print("── 1 · 空与脏 ──")
    check("空年表 → 空列表", _soul([]).history_lines() == [])
    check("缺 history 字段 → 空列表", Soul.from_dict({}).history_lines() == [])
    check("history 不是 list → 空列表", Soul.from_dict({"history": "x"}).history_lines() == [])

    dirty = [None, "str", 42, {"kind": "day_summary"}, {"kind": "day_summary", "text": "  "}]
    check("脏条目跳过、不抛", _soul(dirty).history_lines() == [])

    # ── 2. 只取 day_summary ──
    print()
    print("── 2 · 只取 day_summary ──")
    only_evolved = [
        {"kind": "evolved", "day": "11-09", "about": "工作", "from": "烦", "to": "算了"},
        {"kind": "evolved", "day": "11-10", "about": "称呼", "from": "芊芊", "to": "大肥鱼"},
    ]
    check("只有 evolved → 空（不和 selfNotes 重复）", _soul(only_evolved).history_lines() == [])

    mixed = only_evolved + [{"kind": "day_summary", "day": "11-11", "text": "做了顿饭"}]
    lines = _soul(mixed).history_lines()
    check("混着 evolved 时只出 1 条", len(lines) == 1, str(lines))
    check("带日期前缀", lines and "11-11" in lines[0], str(lines))
    check("带「那天」", lines and "那天" in lines[0], str(lines))

    # ── 3. 顺序：旧 → 新 ──
    print()
    print("── 3 · 顺序 ──")
    three = [
        {"kind": "day_summary", "day": "11-09", "text": "第一天"},
        {"kind": "day_summary", "day": "11-10", "text": "第二天"},
        {"kind": "day_summary", "day": "11-11", "text": "第三天"},
    ]
    lines = _soul(three).history_lines()
    check("三条都出来", len(lines) == 3, str(lines))
    check("旧的在最前", "11-09" in lines[0], str(lines))
    check("新的在最后", "11-11" in lines[-1], str(lines))

    # ── 4. 上限 ──
    print()
    print("── 4 · 上限 ──")
    many = [{"kind": "day_summary", "day": f"10-{i + 1:02d}", "text": f"第{i}天"}
            for i in range(20)]
    lines = _soul(many).history_lines()
    check("最多 8 条", len(lines) == 8, f"{len(lines)}")
    check("取的是最近 8 天", "10-13" in lines[0] and "10-20" in lines[-1], str(lines))

    long_text = [{"kind": "day_summary", "day": "11-11", "text": "长" * 200}]
    lines = _soul(long_text).history_lines()
    check("单条压到 40 字以内", lines and len(lines[0]) <= 60, str(len(lines[0]) if lines else 0))

    many_long = [{"kind": "day_summary", "day": f"10-{i + 1:02d}", "text": "长" * 60}
                 for i in range(10)]
    lines = _soul(many_long).history_lines()
    total = sum(len(x) for x in lines)
    check("总预算 ≤ 250 字", total <= 250, f"{total}")

    # ── 5. 人格一致性：年表不进 render_persona ──
    print()
    print("── 5 · 不进人格 ──")
    s = _soul(three)
    persona = s.render_persona()
    check("render_persona 不含「那天」", "那天" not in persona, persona[:80])
    check("render_persona 不含「## 以前」", "## 以前" not in persona, persona[:80])

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
