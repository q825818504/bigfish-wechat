r"""日记体检：`list_days` 只认日期文件。

跑法：
    python tools/selftest_journal.py

不调模型、不联网；临时目录在 .tmp/ 下，不碰用户真实数据。

## 为什么值得单开

2026-11-12 首页显示"还没有日记"，点「打开 journal」提示
`路径不存在：...\journal\unknown.md`。根因是 `journal/` 目录里
除了 `YYYY-MM-DD.md` 还有一份 `WRITING.md`（写作指南）：

    list_days 把所有 .md 都当日记
    → 'WRITING' 按字符串倒序排在 '2026-11-11' 前面（'W' > '2'）
    → days[0] = 'WRITING'
    → path_for('WRITING') 把非数字清空成 'unknown'
    → read 读到空 → 首页"还没有日记"
    → path = unknown.md → 打开时"路径不存在"

这个用例就是钉住：**目录里的非日期文件不许进列表**。
"""
from __future__ import annotations

import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import journal as J  # noqa: E402

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


def main() -> int:
    print("=" * 60)
    print("  日记体检 · list_days")
    print("=" * 60)
    print()

    tmp = os.path.join(_ROOT, ".tmp", "selftest_journal")
    shutil.rmtree(tmp, ignore_errors=True)
    d = os.path.join(tmp, "journal")
    os.makedirs(os.path.join(d, "_drafts"), exist_ok=True)

    def touch(name: str, body: str = "正文") -> None:
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(f"# {name[:-3]}\n\n{body}\n")

    touch("2026-11-11.md", "今天写了日记")
    touch("2026-11-10.md", "昨天也写了")
    touch("WRITING.md", "这是写作指南，不是日记")
    touch("_drafts/2026-11-09.md", "草稿不该被当正式日记")
    with open(os.path.join(d, "notes.txt"), "w", encoding="utf-8") as f:
        f.write("不是 markdown")

    # ── 1. 列表 ──
    print("── 1 · 列表 ──")
    days = J.list_days(tmp, limit=10)
    check("只返回日期文件", days == ["2026-11-11", "2026-11-10"], str(days))
    check("不含 WRITING", "WRITING" not in days, str(days))
    check("不含 _drafts 里的", "2026-11-09" not in days, str(days))
    check("不含 .txt", all(x != "notes" for x in days), str(days))
    check("倒序（新的在前）", days[0] == "2026-11-11", str(days))

    # ── 2. latest 与 read ──
    print()
    print("── 2 · latest / read ──")
    day, text = J.latest(tmp)
    check("latest 是今天", day == "2026-11-11", day)
    check("latest 有正文", "今天写了日记" in text, text[:40])
    check("read 上一篇", "昨天也写了" in J.read("2026-11-10", tmp))

    # ── 3. path_for 仍然兜底 ──
    print()
    print("── 3 · path_for 兜底 ──")
    p = J.path_for("WRITING", tmp)
    check("非法 day 落 unknown.md（防御仍在）", p.endswith("unknown.md"), p)
    good = J.path_for("2026-11-11", tmp)
    check("合法 day 走日期名", good.endswith("2026-11-11.md"), good)

    # ── 4. 空目录 ──
    print()
    print("── 4 · 空目录 ──")
    empty = os.path.join(tmp, "empty")
    os.makedirs(os.path.join(empty, "journal"), exist_ok=True)
    check("没有日记 → 空列表", J.list_days(empty) == [])
    check("latest → ('', '')", J.latest(empty) == ("", ""))

    # ── 5. 只有 WRITING.md 的目录 ──
    print()
    print("── 5 · 只有写作指南 ──")
    only = os.path.join(tmp, "only_writing")
    os.makedirs(os.path.join(only, "journal"), exist_ok=True)
    with open(os.path.join(only, "journal", "WRITING.md"), "w",
              encoding="utf-8") as f:
        f.write("# 写作指南\n")
    check("只有 WRITING → 空列表（不再显示成一篇日记）",
          J.list_days(only) == [], str(J.list_days(only)))
    check("latest 也空", J.latest(only) == ("", ""))

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
