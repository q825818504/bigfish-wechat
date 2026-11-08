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

    # 1) 单条：短消息（「在吗」2 字、没标点）→ 等满自适应上限 4.0s
    #    ⚠ 这条以前是"等满 2.0s"。改成自适应之后，「在吗」这种极短、
    #      没标点的消息要等 4 秒 —— 因为它几乎肯定是多条里的第一条。
    #      这正是要修的 bug：对方断句时被插话。
    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    b.feed(msg("c1", "小明", "在吗", at=0.0))
    clock.now = 3.9
    check("短消息 3.9s 还没到期", b.tick() == [], "sealed too early")
    clock.now = 4.0
    out = b.tick()
    check("短消息 4.0s 封批", len(out) == 1 and len(out[0].messages) == 1, f"{out}")
    check("reason=quiet", out[0].reason == "quiet", f"got {out[0].reason}")
    check("waited≈4.0", 3.9 <= out[0].waited <= 4.1, f"got {out[0].waited}")

    # 2) 同人连发 3 条：一批只回一次
    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    b.feed(msg("c1", "小明", "大肥鱼", at=0.0))
    clock.now = 0.5
    b.feed(msg("c1", "小明", "你在不在", at=0.5))
    clock.now = 1.0
    b.feed(msg("c1", "小明", "吃饭了没", at=1.0))
    clock.now = 4.9
    check("连发期间不封批", b.tick() == [], "sealed early")
    clock.now = 5.0
    out = b.tick()
    check("最后一条后 4s 封批",
          len(out) == 1 and len(out[0].messages) == 3, f"{out}")
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
    clock.now = 11.9
    check("max_wait 前一刻还没封", b.tick() == [], "sealed early")
    clock.now = 12.0
    out = b.tick()
    check("max_wait=12s 强制封批", len(out) == 1, f"{out}")
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
    #    这里用固定窗口（adaptive=False）：测的是"两个会话不是同一个桶"，
    #    不该被自适应的秒数变化牵着走。
    clock = Clock()
    b = L.MessageBatcher(L.ListenPolicy(adaptive=False), clock=clock)
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

    # 10) 重复推送不重复入批（同样用固定窗口）
    clock = Clock()
    b = L.MessageBatcher(L.ListenPolicy(adaptive=False), clock=clock)
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
    b = L.MessageBatcher(L.ListenPolicy(adaptive=False), clock=clock)
    b.feed(msg("c1", "小明", "大肥鱼", at=0.0))
    b.feed(msg("c1", "小明", "你在不在", at=0.5))
    clock.now = 2.5
    batch = b.tick()[0]
    text = L.batch_transcript(batch, with_time=False)
    check("带说话人", "小明：大肥鱼" in text and "小明：你在不在" in text, f"{text!r}")
    check("两行", len(text.splitlines()) == 2, f"{text!r}")


def test_adaptive() -> None:
    """自适应窗口 —— 这次修的就是这个。

    固定的 2 秒有个根本问题：**短消息和长消息的"说完程度"不一样**。

        「在吗」              → 极可能还有下文，该多等
        「在吗。」            → 句号收尾，说完了
        「我今天去长坂坡了，看到了樱花看到了枣树」→ 一段话说完了
        「那我先去吃饭了，」  → 逗号收尾，明显还有一句

    算法照 AstrBot 的 private_debounce（companion_v2/conversation）：
    拿字数当完整度的代理，句尾标点微调。
    """
    section("自适应窗口")
    pol = L.ListenPolicy()

    check("【在吗】等最久（≈4.0）",
          abs(L.adaptive_quiet("在吗", pol) - 4.0) < 0.01,
          f"{L.adaptive_quiet('在吗', pol)}")

    # 长消息：60 字以上直接到下限
    long_text = ("我今天去长坂坡了，看到了樱花，还看到了枣树，"
                 "回来的路上碰到了老王他家的狗" * 2) + "。"
    check("长消息等最短（1.5）",
          abs(L.adaptive_quiet(long_text, pol) - 1.5) < 0.01,
          f"{L.adaptive_quiet(long_text, pol)}")

    # 句尾标点的影响：同样 16 字，句号比逗号早、比没标点早
    body = "今天下午我们一起去长坂坡看樱花吧"
    q_none = L.adaptive_quiet(body, pol)
    q_dot = L.adaptive_quiet(body + "。", pol)
    q_comma = L.adaptive_quiet(body + "，", pol)
    check("句号 < 无标点", q_dot < q_none, f"{q_dot} vs {q_none}")
    check("逗号 > 无标点", q_comma > q_none, f"{q_comma} vs {q_none}")
    check("逗号 > 句号", q_comma > q_dot, f"{q_comma} vs {q_dot}")

    # 永远落在 [min, max] 里
    for t in ("", "。", "哈", "a" * 500, "，，，", "  "):
        v = L.adaptive_quiet(t, pol)
        check(f"边界被夹住 {t[:6]!r}",
              pol.quiet_min - 1e-9 <= v <= pol.quiet_max + 1e-9, f"{v}")

    # 关掉自适应 → 退回固定值（老行为，可回退）
    fixed = L.ListenPolicy(adaptive=False)
    check("adaptive=False 用 quiet_same_author",
          L.adaptive_quiet("在吗", fixed) == fixed.quiet_same_author,
          f"{L.adaptive_quiet('在吗', fixed)}")

    # 可见字数：空白不算，纯标点也不该是 0
    check("空白不计入", L.visible_chars(" 你 好 ") == 2,
          str(L.visible_chars(" 你 好 ")))
    check("全空白兜底为 1", L.visible_chars("   ") == 1,
          str(L.visible_chars("   ")))

    # 端到端：对方断句三连，攒成一批
    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    b.feed(msg("c1", "小明", "大肥鱼", at=0.0))
    clock.now = 1.0
    b.feed(msg("c1", "小明", "你在不在", at=1.0))
    clock.now = 2.0
    b.feed(msg("c1", "小明", "吃饭了没", at=2.0))
    clock.now = 5.5
    check("断句期间一直不封批", b.tick() == [], "sealed early")
    clock.now = 6.0
    out = b.tick()
    check("三句攒成一批（不再逐条回）",
          len(out) == 1 and len(out[0].messages) == 3, f"{out}")


def test_post_reply_grace() -> None:
    """她刚说完话，对方接着说的算同一话题。

    2026-11-08 用户现场：她回完一句，对方隔几秒补一句，她再回一条——
    看起来像两个人在互刷。根因是这种情况走的是普通窗口（4 秒），
    没有"她刚说完"这个上下文。
    """
    # 1) 她 0s 说完，对方 1s 继续说 → 等满 6s 冷却
    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    b.feed(msg("c1", "小明", "那你说说", at=1.0), replied_at=0.0)
    clock.now = 4.0
    check("冷却期内 4s 不封批", b.tick() == [], "sealed too early")
    clock.now = 6.0
    out = b.tick()
    check("冷却 6s 到期封批", len(out) == 1, f"{out}")

    # 2) 她 0s 说完，对方 5s 才说 → 冷却只剩 1s，但普通窗口（4s）更长，
    #    取两者较大值：宁可多等，也别在她刚说完就抢话
    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    b.feed(msg("c1", "小明", "那你说说", at=5.0), replied_at=0.0)
    clock.now = 8.9
    check("冷却叠加普通窗口（不提前封批）", b.tick() == [], "sealed early")
    clock.now = 9.0
    check("冷却叠加后 9s 封批", len(b.tick()) == 1, "did not seal")

    # 3) 距她说完已经 10s → 超过冷却，按普通窗口（短消息 4s）
    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    b.feed(msg("c1", "小明", "在吗", at=10.0), replied_at=0.0)
    clock.now = 12.0
    check("冷却过期后走普通窗口", b.tick() == [], "sealed too early")
    clock.now = 14.0
    check("普通窗口 4s 封批", len(b.tick()) == 1, "did not seal")

    # 4) 被 @ 时不加冷却（对方在叫你，别让人等）
    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    b.feed(msg("c1", "小明", "@大肥鱼 在吗", at=1.0, at_bot=True),
           replied_at=0.0)
    clock.now = 2.1
    check("@ 不套冷却（1s 就回）", len(b.tick()) == 1, "被冷却拖住了")


def main() -> int:
    print("=" * 66)
    print("倾听层自检（攒批 / 到期 / 硬上限 / 自适应窗口）")
    print("=" * 66)

    test_listen()
    test_adaptive()
    test_transcript()
    test_post_reply_grace()

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
