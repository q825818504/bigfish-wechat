"""节奏体检：改完参数，她一天到底会说几句。

跑法：
    python tools/selftest_pace.py

不需要微信、不调模型、不读数据 —— 用真实的 `hit_probability` 掷十万次骰子，
看一天说几条的分布。

## 为什么需要这个文件

"她主动说话"这件事，参数之间是**乘法**关系：掷骰间隔 × 单次命中率 ×
没中加成 × 一天的机会数。改任何一个看着都只动了一点点，乘起来却是
"一天一条"和"每天固定三条"的差别。

这个差别不会报错、不会白屏，只会让你在用了三天之后觉得"她怎么像打卡"。
所以把"节奏"本身做成一条可以失败的自检。

判据（三条，都是"人味"而不是"正确性"）：

    1. 平均落在 0.8~2.2 条    太多像骚扰，太少像提醒器
    2. 存在"一条都没有"的日子  每天都有话说，本身就不像人
    3. 越不理她越主动          常聊 < 正常 < 冷落
"""
from __future__ import annotations

import os
import random
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

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


OK, BAD = _pick_marks()


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  {OK} {name}")
        return True
    FAIL += 1
    FAILED.append(name)
    print(f"  {BAD} {name}" + (f"\n      {detail}" if detail else ""))
    return False


DAY_START, DAY_END = 9 * 60, 22 * 60 + 30
#: 日常会出现的候选分（不含重要日期那种一年两回的）
DAILY_SCORES = [58.0, 60.0, 62.0, 56.0, 72.0]
DAYS = 4000


def simulate(peer_gap_minutes: float, seed: int) -> Counter:
    """跑若干"天"。返回 {说了几条: 天数}。"""
    rng = random.Random(seed)
    out: Counter = Counter()
    for _ in range(DAYS):
        sent = 0
        last_sent = 0.0
        streak = 0
        t = float(DAY_START * 60)
        while t < DAY_END * 60 and sent < P.DEFAULT_PER_DAY:
            if last_sent and (t - last_sent) / 60.0 < P.MIN_GAP_MINUTES:
                t += P.ROLL_INTERVAL_MINUTES * 60.0
                continue
            score = rng.choice(DAILY_SCORES)
            hit, _why = P.hit_probability(
                score=score, now=t, last_sent_at=last_sent,
                last_peer_at=t - peer_gap_minutes * 60.0, miss_streak=streak)
            if hit > 0:
                if rng.randint(1, 100) <= hit:
                    sent += 1
                    last_sent = t
                    streak = 0
                else:
                    streak += 1
            t += P.ROLL_INTERVAL_MINUTES * 60.0
        out[sent] += 1
    return out


def _avg(dist: Counter) -> float:
    total = sum(dist.values()) or 1
    return sum(k * v for k, v in dist.items()) / total


def _pct(dist: Counter, k: int) -> float:
    total = sum(dist.values()) or 1
    return dist.get(k, 0) / total * 100


def main() -> int:
    print("=" * 56)
    print("节奏体检：一天说几条")
    print("=" * 56)
    print(f"  per_day={P.DEFAULT_PER_DAY}  间隔={P.MIN_GAP_MINUTES}min  "
          f"掷骰={P.ROLL_INTERVAL_MINUTES}min")

    scenes = [
        ("常聊（1 小时内聊过）", 60, 11),
        ("正常（3 小时前说过）", 180, 22),
        ("冷落（8 小时没说话）", 480, 33),
    ]
    stats = {}
    print()
    for name, gap, seed in scenes:
        d = simulate(gap, seed)
        avg = _avg(d)
        stats[name] = (d, avg)
        row = "  ".join(f"{k}条 {_pct(d, k):5.1f}%" for k in range(P.DEFAULT_PER_DAY + 1))
        print(f"  {name:22s} 平均 {avg:4.2f}   {row}")

    print()
    for name, (_d, avg) in stats.items():
        check(f"{name}：平均在 0.8~2.2 条之间", 0.8 <= avg <= 2.2,
              f"实际 {avg:.2f} —— 太多像骚扰，太少像提醒器")

    quiet, _ = stats["常聊（1 小时内聊过）"]
    check("存在「一句话都没有」的日子",
          _pct(quiet, 0) >= 1.0,
          f"常聊场景 0 条只占 {_pct(quiet, 0):.1f}% —— 每天都说话，本身就不像人")

    a = stats["常聊（1 小时内聊过）"][1]
    b = stats["正常（3 小时前说过）"][1]
    c = stats["冷落（8 小时没说话）"][1]
    check("越不理她越主动（常聊 < 正常 < 冷落）", a < b < c,
          f"{a:.2f} / {b:.2f} / {c:.2f}")

    for name, (d, _avg_) in stats.items():
        check(f"{name}：不是每天都打满额度",
              _pct(d, P.DEFAULT_PER_DAY) < 60.0,
              f"打满占 {_pct(d, P.DEFAULT_PER_DAY):.1f}% —— 天天满额就是打卡")

    # 机会数本身也要看得见：一天能掷几次，比命中率更决定节奏
    chances = int((DAY_END - DAY_START) / P.ROLL_INTERVAL_MINUTES)
    check("一天的机会数在 4~10 次之间", 4 <= chances <= 10,
          f"{chances} 次 —— 太少她永远等不到，太多加成会把额度推满")

    print()
    print("=" * 56)
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
