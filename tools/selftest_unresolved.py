"""旧账体检：冲突跨天 / 到期提醒 / 7 天过期。

跑法：
    python tools/selftest_unresolved.py

不调模型、不联网；数据库在 .tmp/ 下，不碰用户真实数据。

## 为什么值得单开

`afterglow` 的 conflict 只有 4 小时 TTL —— 它管的是"这一轮还堵着"。
但人不会 4 小时就忘：昨天那句难听的，今天还会带着。
这一层补的是"跨天的旧账"，同时守住两条边界：

1. **7 天过期**：不留永久怨气（那是另一个东西，不是人）。
2. **只作 tone**：旧账影响语气，不该变成每次开口的主题。
"""
from __future__ import annotations

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
from bigfish import proactive as P  # noqa: E402

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
    print("  旧账体检 · unresolved")
    print("=" * 60)
    print()

    now = time.time()
    tmp = os.path.join(_ROOT, ".tmp", "selftest_unresolved")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    db = os.path.join(tmp, "mem.sqlite3")
    owner = "private:wechat:x"
    B.ensure_schema(db)

    # ── 1. 写入与查询 ──
    print("── 1 · 写入与查询 ──")
    ok, why = B.append_event(db, "dafeiyu", owner, kind="unresolved",
                             what="他说了句难听的", evidence="你烦不烦",
                             at=now)
    check("写入旧账", ok, why)
    rows = B.list_events(db, "dafeiyu", owner, kind="unresolved", limit=5)
    check("按 kind 查到", len(rows) == 1, str(len(rows)))
    check("what 正确", rows and rows[0].get("what") == "他说了句难听的")

    # ── 2. 候选 ──
    print()
    print("── 2 · 主动候选 ──")
    st = P.ProactiveState()
    cand = P.candidate_unresolved(st, chat_id="c1", now=now, owner=owner,
                                  tier="owner", db_path=db)
    check("生成候选", cand is not None)
    check("source 是 unresolved", cand and cand.source == "unresolved")
    check("只作 tone", cand and cand.layer == "tone", str(cand))
    check("分数 76", cand and abs(cand.score - 76.0) < 1e-6, str(cand))
    check("anchor 与 fact/event 分开",
          cand and cand.anchor_at >= 2_000_000_000.0, str(cand.anchor_at if cand else ""))

    # 用过就不再提
    st2 = P.ProactiveState()
    st2.used_anchors = [cand.anchor_at]
    check("用过的 anchor 不再提",
          P.candidate_unresolved(st2, chat_id="c1", now=now, owner=owner,
                                 tier="owner", db_path=db) is None)

    # ── 3. 7 天过期 ──
    print()
    print("── 3 · 过期 ──")
    B.append_event(db, "dafeiyu", owner, kind="unresolved",
                   what="很久以前那次", at=now - 8 * 86400)
    rows_all = B.list_events(db, "dafeiyu", owner, kind="unresolved", limit=10)
    check("两条都在库里（不删行）", len(rows_all) == 2, str(len(rows_all)))
    cand3 = P.candidate_unresolved(P.ProactiveState(), chat_id="c1", now=now,
                                   owner=owner, tier="owner", db_path=db)
    check("8 天前的不进候选（取新的那条）",
          cand3 is not None and "很久以前" not in cand3.material.get("unresolved", ""),
          str(cand3.material if cand3 else ""))

    # ── 4. 说开就消 ──
    print()
    print("── 4 · 说开就消 ──")
    eid = int(rows[0]["id"])
    check("resolve 成功", F.resolve(db, eid) is True)
    cand4 = P.candidate_unresolved(P.ProactiveState(), chat_id="c1", now=now,
                                   owner=owner, tier="owner", db_path=db)
    check("说开后不再提", cand4 is None, str(cand4))

    # ── 5. 只作 tone：不抢主题 ──
    print()
    print("── 5 · 边界 ──")
    check("没有 owner/db 时返回 None",
          P.candidate_unresolved(P.ProactiveState(), chat_id="c1", now=now) is None)
    check("没有 tier 时返回 None",
          P.candidate_unresolved(P.ProactiveState(), chat_id="c1", now=now,
                                 owner=owner, db_path=db) is None)

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
