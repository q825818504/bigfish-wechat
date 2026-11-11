"""共同账本回归测试（D-1）。

跑法：
    python tools/selftest_ledger.py

不需要微信、不需要桌面、不需要网络、不写任何文件。

## 为什么有这份测试

账本是整个自环防护里**唯一有"否决权"**的东西：判据说"这是对方说的"，
账本可以说"但我一秒前刚发过这句"。它能救回 11-04 那晚的场景，
但认错的时候代价是把对方的真话当成自己发的 —— 直接静默。

所以这份测试守两件事：

  · 该认的必须认（相等 / 长度接近的前缀 / 群里带前缀的读回）
  · **不该认的绝不能认**（对方续写、对方复述长句、窗口外的旧账）

覆盖：
  1  记账后能查到（同会话）
  2  别的会话查不到
  3  精确相等命中
  4  归一化：多余空白、首尾空白不影响命中
  5  群里读回带 `wxid_xxx: ` 前缀也能命中
  6  长度差 > 10% 的前缀不命中（对方续写）
  7  长度差 ≤ 10% 且够长 → 命中
  8  短句不比前缀（只差一两个字时不误判）
  9  rollback 之后查不到
 10  rollback_text 之后查不到
 11  confirm 回填 local_id，且能按 id 查到
 12  prune 之后超窗口的查不到
 13  空文本不记账
 14  条数上限生效，丢最旧的
 15  lookup 不修改账本
 16  reset 后清空且旧句柄失效
 17  被否决时必须能拿到那笔账（给证据链用）
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish.ledger import Ledger, close_enough, normalize  # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \u2713 {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  \u2717 {name}" + (f"  <- {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n[{title}]")


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def tick(self, seconds: float) -> None:
        self.t += seconds


CHAT = "wxid_dad"
OTHER = "wxid_mom"


def fresh(**kw):
    clock = kw.pop("clock", None) or Clock()
    return Ledger(clock=clock, **kw), clock


# ══════════════════════════════════════════════════════════════════
def test_basic() -> None:
    section("1/3 记账与命中")
    led, _ = fresh()
    led.note_sent(CHAT, "我今天有点累")
    hit = led.lookup(CHAT, "我今天有点累")
    check("刚记的能查到", hit is not None)
    check("查到的是同一句", hit and hit.text == "我今天有点累")
    check("没记过的查不到", led.lookup(CHAT, "我明天有点累") is None)

    section("2 会话隔离")
    check("别的会话查不到", led.lookup(OTHER, "我今天有点累") is None)
    led.note_sent(OTHER, "我在别的会话说的话")
    check("另一个会话自己记得", led.lookup(OTHER, "我在别的会话说的话") is not None)
    check("不会串到本会话", led.lookup(CHAT, "我在别的会话说的话") is None)

    section("4 归一化")
    led2, _ = fresh()
    led2.note_sent(CHAT, "  今天  天气\n不错  ")
    check("首尾空白 + 连续空白都能命中",
          led2.lookup(CHAT, "今天 天气 不错") is not None)
    check("归一化只做空白折叠",
          normalize("  a\n\nb  ") == "a b", normalize("  a\n\nb  "))

    section("5 群里读回带前缀")
    led3, _ = fresh()
    led3.note_sent("123@chatroom", "我先走了")
    check("读回带 wxid 前缀也能命中",
          led3.lookup("123@chatroom", "wxid_me: 我先走了") is not None)
    check("读回带自己加的 @ 也能命中",
          led3.lookup("123@chatroom", "@老爸 我先走了") is not None)


def test_matching() -> None:
    section("6/7/8 匹配的边界")
    base = "今天下午我们一起去长坂坡看樱花吧"
    check("完全相等 → 认", close_enough(base, base))
    check("只差一个空格（归一化后相等）→ 认",
          close_enough(normalize(base), normalize(base + " ")))
    check("凭空多出七个字 → 不认",
          not close_enough(base, base + "然后吃烧烤"))

    long_a = "今天下午我们一起去长坂坡看樱花顺便带点吃的回来晚上煮面" * 2
    check("长句只差一个字 → 认", close_enough(long_a, long_a[:-1]))
    check("长句多出 30% → 不认",
          not close_enough(long_a, long_a + long_a[:len(long_a) // 3]))

    check("短句只差一个字 → 不比前缀（不认）",
          not close_enough("好的", "好的吧"))
    check("短句完全相等 → 认", close_enough("好的", "好的"))
    check("空串 → 不认", not close_enough("", "x"))


def test_rollback() -> None:
    section("9/10 撤账")
    led, _ = fresh()
    h = led.note_sent(CHAT, "没发出去的话")
    check("记上了", led.lookup(CHAT, "没发出去的话") is not None)
    check("撤账成功", led.rollback(h) is True)
    check("撤账后查不到", led.lookup(CHAT, "没发出去的话") is None)
    check("重复撤账返回 False", led.rollback(h) is False)

    led2, _ = fresh()
    led2.note_sent(CHAT, "另一句没发出去的")
    check("按文本撤账",
          led2.rollback_text(CHAT, "另一句没发出去的") is True)
    check("按文本撤账后查不到",
          led2.lookup(CHAT, "另一句没发出去的") is None)


def test_confirm() -> None:
    section("11 确认与主键")
    led, _ = fresh()
    h = led.note_sent(CHAT, "发出去的一句")
    check("确认成功", led.confirm(h, 9527) is True)
    check("回填了 local_id", led.lookup(CHAT, "发出去的一句").local_id == 9527)
    hit = led.lookup(CHAT, "完全不相干的文字", local_id=9527)
    check("按 local_id 也能查到（不必比文本）", hit is not None and hit.local_id == 9527)


def test_window() -> None:
    section("12 窗口")
    led, clock = fresh(window=90.0)
    led.note_sent(CHAT, "很久以前说的话")
    clock.tick(30)
    check("30 秒内还算数", led.lookup(CHAT, "很久以前说的话") is not None)
    clock.tick(120)
    check("超过窗口就查不到", led.lookup(CHAT, "很久以前说的话") is None)

    led2, _ = fresh()
    led2.note_sent(CHAT, "一句")
    led2.prune(now=10 ** 9)
    check("prune 到很远的将来会清空", len(led2) == 0, str(len(led2)))


def test_limits() -> None:
    section("13/14 边界")
    led, _ = fresh()
    check("空文本不记账", led.note_sent(CHAT, "   ") is None)
    check("空文本查不到", led.lookup(CHAT, "") is None)

    led2, _ = fresh(max_per_chat=3)
    for i in range(6):
        led2.note_sent(CHAT, f"第 {i} 句")
    check("条数上限生效", len(led2) == 3, str(len(led2)))
    check("留下的是最新的", led2.lookup(CHAT, "第 5 句") is not None)
    check("最旧的被丢了", led2.lookup(CHAT, "第 0 句") is None)

    section("15 查询无副作用")
    led3, _ = fresh()
    led3.note_sent(CHAT, "一句话")
    before = led3.snapshot()
    led3.lookup(CHAT, "一句话")
    led3.lookup(CHAT, "不相干")
    check("lookup 不修改账本", led3.snapshot() == before)

    section("16 reset")
    led4, _ = fresh()
    h = led4.note_sent(CHAT, "重置前的账")
    led4.reset()
    check("reset 后清空", len(led4) == 0)
    check("旧句柄失效（不能再确认）", led4.confirm(h, 1) is False)
    check("旧句柄失效（不能撤账）", led4.rollback(h) is False)

    section("17 否决时能拿到那笔账")
    led5, _ = fresh()
    led5.note_sent(CHAT, "她一秒前发的这句")
    hit = led5.lookup(CHAT, "她一秒前发的这句")
    check("拿到账目对象（证据链要写文本与时间）",
          hit is not None and hit.text == "她一秒前发的这句" and hit.at > 0)


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    print("共同账本回归测试（bigfish/ledger.py）")
    test_basic()
    test_matching()
    test_rollback()
    test_confirm()
    test_window()
    test_limits()

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
