"""未了之事体检：做完了 / 没顾上 / 她自己的惦记。

跑法：
    python tools/selftest_own_loop.py

不调模型、不联网、不碰用户真实数据。

## 为什么值得单开

以前 `expire_plan` 只写 `done_by="elapsed"` —— 区间一过全算"做完了"。
她的日程**永远不会失败**，也就没有"想做没做"这回事。
这一层补的是：区分"做过"和"没顾上"，并让"没顾上"能被她自己想起来。

## 守哪几条底线

1. **做过才算做完**：区间内被 `doing` 命中过 → done；没命中 → abandoned。
2. **两种状态都要结算**：不能因为"没做成"就一直挂着（那会让 doing 卡死）。
3. **渲染要分开**：做完了 / 没顾上 / 还没到，三种文案不能混。
4. **只作 tone**：她自己的小事不该抢主话题，只影响语气。
"""
from __future__ import annotations

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import life as L  # noqa: E402
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


def _state(now: float) -> L.LifeState:
    st = L.LifeState()
    st.day = L.day_key(now)
    st.plan_day = L.day_key(now)
    st.plan = [
        {"at": "09:00", "what": "收拾屋子", "start": 9 * 60, "end": 9 * 60 + 50,
         "done": False, "touched": True},
        {"at": "13:00", "what": "整理照片", "start": 13 * 60, "end": 13 * 60 + 40,
         "done": False},
        {"at": "23:00", "what": "看会儿书", "start": 23 * 60, "end": 23 * 60 + 30,
         "done": False},
    ]
    return st


def main() -> int:
    print("=" * 60)
    print("  未了之事体检 · own_loop")
    print("=" * 60)
    print()

    # 2026-11-11 14:30：第一条已过（touched）、第二条已过（未 touched）、第三条未到
    now = time.mktime(time.strptime("2026-11-11 14:30", "%Y-%m-%d %H:%M"))
    st = _state(now)

    # ── 1. 结算 ──
    print("── 1 · 结算 ──")
    n = L.expire_plan(st, now)
    check("结算了两条", n == 2, str(n))
    check("做过的那条算做完",
          st.plan[0].get("done") and not st.plan[0].get("abandoned"),
          str(st.plan[0]))
    check("没做过的那条算没顾上",
          st.plan[1].get("abandoned") is True, str(st.plan[1]))
    check("没顾上的也有 done（不卡死）",
          st.plan[1].get("done") is True, str(st.plan[1]))
    check("还没到的不动", st.plan[2].get("done") is False, str(st.plan[2]))

    # ── 2. own_loops ──
    print()
    print("── 2 · own_loops ──")
    loops = L.own_loops(st)
    check("只出没顾上的", loops == ["整理照片"], str(loops))
    check("做完的不进", "收拾屋子" not in loops, str(loops))
    check("limit 生效", len(L.own_loops(st, limit=1)) == 1)

    # ── 3. 渲染 ──
    print()
    print("── 3 · 渲染 ──")
    text = L.plan_for_prompt(st, now=now)
    check("做完了", "（做完了）" in text, text[:80])
    check("没顾上", "（没顾上）" in text, text[:120])
    check("不再把没做成说成「过去了」", "（过去了）" not in text, text[:120])
    check("末尾允许承认没顾上", "没顾上就承认没顾上" in text)
    check("末尾不许把没做说成做完", "别把没做的说成做完了" in text)

    # ── 4. 主动候选 ──
    print()
    print("── 4 · 主动候选 ──")
    cand = P.candidate_own_loop(P.ProactiveState(), chat_id="c1", now=now,
                                life_state=st)
    check("生成了候选", cand is not None)
    check("source 是 own_loop", cand and cand.source == "own_loop", str(cand))
    check("只作 tone", cand and cand.layer == "tone", str(cand))
    check("分数 70", cand and abs(cand.score - 70.0) < 1e-6, str(cand))

    st2 = L.LifeState()
    st2.day = L.day_key(now)
    st2.plan = []
    check("没有未了之事 → None",
          P.candidate_own_loop(P.ProactiveState(), chat_id="c1", now=now,
                               life_state=st2) is None)

    # 一天一次
    pst = P.ProactiveState()
    pst.source_last["own_loop"] = now
    check("20 小时内不重复提",
          P.candidate_own_loop(pst, chat_id="c1", now=now + 3600,
                               life_state=st) is None)

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
