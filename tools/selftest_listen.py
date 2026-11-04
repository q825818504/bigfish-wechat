"""倾听层回归测试：等对方说完再回。

跑法：
    python tools/selftest_listen.py

不需要微信、不需要桌面、不需要网络。假时钟直接跳时间，不 sleep。

覆盖：
  · 单条：等满 quiet_same_author 才封批
  · 同人连发：每次重置计时，一批只回一次
  · 换人：等得短一点（新话题）
  · 被 @：1 秒内必回
  · 纯图：多等（可能在连发图）
  · max_wait：再吵也要回
  · max_batch：到顶立即封批
  · 自己的消息不进批
  · 多会话互不干扰
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import listen as L  # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []


def _pick_marks() -> tuple[str, str]:
    try:
        "\u2713".encode(sys.stdout.encoding or "utf-8")
        return "\u2713", "\u2717"
    except (UnicodeEncodeError, LookupError):
        return "[OK]", "[X]"


MARK_OK, MARK_FAIL = _pick_marks()


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  {MARK_OK} {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  {MARK_FAIL} {name}  {detail}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def msg(chat: str, author: str, text: str, *, at: float,
        local_id: int = 0, msg_type: str = "文本",
        at_bot: bool = False, is_self: bool = False) -> L.BatchMessage:
    return L.BatchMessage(chat_id=chat, author=author, text=text,
                          local_id=local_id, msg_type=msg_type,
                          at_bot=at_bot, is_self=is_self, received_at=at)


def test_listen() -> None:
    section("攒批")

    # 1) 单条：等满 2.0s
    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    b.feed(msg("c1", "小明", "在吗", at=0.0))
    clock.now = 1.9
    check("1.9s 还没到期", b.tick() == [], "sealed too early")
    clock.now = 2.0
    out = b.tick()
    check("2.0s 封批", len(out) == 1 and len(out[0].messages) == 1, f"{out}")
    check("reason=quiet", out[0].reason == "quiet", f"got {out[0].reason}")
    check("waited≈2.0", 1.9 <= out[0].waited <= 2.1, f"got {out[0].waited}")

    # 2) 同人连发 3 条：一批只回一次
    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    b.feed(msg("c1", "小明", "大肥鱼", at=0.0))
    clock.now = 0.5
    b.feed(msg("c1", "小明", "你在不在", at=0.5))
    clock.now = 1.0
    b.feed(msg("c1", "小明", "吃饭了没", at=1.0))
    clock.now = 2.9
    check("连发期间不封批", b.tick() == [], "sealed early")
    clock.now = 3.0
    out = b.tick()
    check("最后一条后 2s 封批", len(out) == 1 and len(out[0].messages) == 3, f"{out}")
    check("批次里三个人称一致", out[0].authors == ["小明"], f"{out[0].authors}")

    # 3) 换人：0.8s
    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    b.feed(msg("c1", "小明", "今天去哪", at=0.0))
    clock.now = 0.5
    b.feed(msg("c1", "小红", "去长坂坡", at=0.5))
    clock.now = 1.2
    check("换人 0.7s 未到期", b.tick() == [], "sealed early")
    clock.now = 1.3
    out = b.tick()
    check("换人 0.8s 封批", len(out) == 1 and len(out[0].messages) == 2, f"{out}")

    # 4) 被 @：1.0s 必回
    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    b.feed(msg("c1", "小明", "@大肥鱼 在吗", at=0.0, at_bot=True))
    clock.now = 0.9
    check("@ 后 0.9s 未到期", b.tick() == [], "sealed early")
    clock.now = 1.0
    out = b.tick()
    check("@ 后 1.0s 封批", len(out) == 1, f"{out}")
    check("reason=mention", out[0].reason == "mention", f"got {out[0].reason}")

    # 5) 纯图：等 4s（可能在连发图）
    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    b.feed(msg("c1", "小明", "", at=0.0, msg_type="图片"))
    clock.now = 3.9
    check("纯图 3.9s 未到期", b.tick() == [], "sealed early")
    clock.now = 4.0
    out = b.tick()
    check("纯图 4.0s 封批", len(out) == 1 and out[0].reason == "media", f"{out}")

    # 6) max_wait：一直发也要回（max_batch 调大，单独测这一条规则）
    clock = Clock()
    b = L.MessageBatcher(L.ListenPolicy(max_batch=20), clock=clock)
    for i in range(10):
        clock.now = float(i)
        b.feed(msg("c1", "小明", f"第{i}句", at=float(i), local_id=i + 1))
    clock.now = 8.0
    out = b.tick()
    check("max_wait=8s 强制封批", len(out) == 1, f"{out}")
    check("reason=max_wait", out[0].reason == "max_wait", f"got {out[0].reason}")
    check("封批时已攒 10 条", len(out[0].messages) == 10, f"got {len(out[0].messages)}")

    # 7) max_batch：到顶立即封批
    clock = Clock()
    b = L.MessageBatcher(L.ListenPolicy(max_batch=3), clock=clock)
    b.feed(msg("c1", "小明", "一", at=0.0, local_id=1))
    b.feed(msg("c1", "小明", "二", at=0.4, local_id=2))
    immediate = b.feed(msg("c1", "小明", "三", at=0.8, local_id=3))
    check("第 3 条立即封批", immediate is not None and len(immediate.messages) == 3, f"{immediate}")
    check("reason=batch_full", immediate is not None and immediate.reason == "batch_full", f"{immediate}")

    # 8) 自己的消息不进批
    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    check("is_self 返回 None", b.feed(msg("c1", "我", "我说的话", at=0.0, is_self=True)) is None, "not None")
    check("is_self 不建桶", b.pending() == {}, f"{b.pending()}")

    # 9) 多会话互不干扰
    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    b.feed(msg("c1", "小明", "A", at=0.0))
    b.feed(msg("c2", "小红", "B", at=0.0))
    clock.now = 1.5
    b.feed(msg("c2", "小红", "B2", at=1.5))
    clock.now = 2.0
    out = b.tick()
    check("c1 先封批、c2 还没", len(out) == 1 and out[0].chat_id == "c1", f"{[(x.chat_id, x.reason) for x in out]}")
    clock.now = 3.5
    out = b.tick()
    check("c2 随后封批", len(out) == 1 and out[0].chat_id == "c2" and len(out[0].messages) == 2, f"{out}")

    # 10) 重复推送不重复入批
    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    b.feed(msg("c1", "小明", "一", at=0.0, local_id=7))
    b.feed(msg("c1", "小明", "一", at=0.1, local_id=7))
    clock.now = 2.0
    out = b.tick()
    check("同 local_id 不重复入批", len(out) == 1 and len(out[0].messages) == 1, f"{out}")

    # 11) flush_all
    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    b.feed(msg("c1", "小明", "还没说完", at=0.0))
    out = b.flush_all()
    check("flush_all 强制封批", len(out) == 1 and out[0].reason == "forced", f"{out}")

    # 12) drop
    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    b.feed(msg("c1", "小明", "安静时段前的消息", at=0.0))
    b.drop("c1")
    clock.now = 5.0
    check("drop 后不再封批", b.tick() == [], "still sealed")


def test_transcript() -> None:
    section("时间线")

    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    b.feed(msg("c1", "小明", "大肥鱼", at=0.0))
    b.feed(msg("c1", "小明", "你在不在", at=0.5))
    clock.now = 2.5
    batch = b.tick()[0]
    text = L.batch_transcript(batch, with_time=False)
    check("带说话人", "小明：大肥鱼" in text and "小明：你在不在" in text, f"{text!r}")
    check("两行", len(text.splitlines()) == 2, f"{text!r}")


def main() -> int:
    print("=" * 66)
    print("倾听层自检（攒批 / 到期 / 硬上限）")
    print("=" * 66)

    test_listen()
    test_transcript()

    print("\n" + "=" * 66)
    if FAIL:
        print(f"结果：{PASS}/{PASS + FAIL} 通过，{FAIL} 项失败")
        print("失败项：")
        for name in FAILED:
            print(f"  \u00b7 {name}")
        return 1
    print(f"结果：{PASS}/{PASS + FAIL} 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
