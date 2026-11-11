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

    # 1) 单条：短消息（「在吗」2 字、没标点）→ 等满自适应上限
    #    ⚠ 2026-11-08 上限从 4.0 提到 6.0：用户实测「稍微犹豫一下再发，
    #      她就已经回复了」。这里不写死数字，直接读 policy 的值 ——
    #      调参时不该改测试。
    _q = L.ListenPolicy().quiet_max
    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    b.feed(msg("c1", "小明", "在吗", at=0.0))
    clock.now = _q - 0.1
    check(f"短消息 {_q - 0.1:.1f}s 还没到期", b.tick() == [], "sealed too early")
    clock.now = _q
    out = b.tick()
    check(f"短消息 {_q:.1f}s 封批", len(out) == 1 and len(out[0].messages) == 1, f"{out}")
    check("reason=quiet", out[0].reason == "quiet", f"got {out[0].reason}")
    check(f"waited≈{_q:.1f}", _q - 0.1 <= out[0].waited <= _q + 0.1,
          f"got {out[0].waited}")

    # 2) 同人连发 3 条：一批只回一次
    _q2 = L.adaptive_quiet("吃饭了没", L.ListenPolicy())
    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    b.feed(msg("c1", "小明", "大肥鱼", at=0.0))
    clock.now = 0.5
    b.feed(msg("c1", "小明", "你在不在", at=0.5))
    clock.now = 1.0
    b.feed(msg("c1", "小明", "吃饭了没", at=1.0))
    clock.now = 1.0 + _q2 - 0.1
    check("连发期间不封批", b.tick() == [], "sealed early")
    clock.now = 1.0 + _q2
    out = b.tick()
    check(f"最后一条后 {_q2:.1f}s 封批",
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

    # 5) 纯图：等 max(自适应, quiet_media) —— 可能在连发图
    _pol5 = L.ListenPolicy()
    _q_media = max(L.adaptive_quiet("", _pol5), _pol5.quiet_media)
    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    b.feed(msg("c1", "小明", "", at=0.0, msg_type="图片"))
    clock.now = _q_media - 0.1
    check(f"纯图 {_q_media - 0.1:.1f}s 未到期", b.tick() == [], "sealed early")
    clock.now = _q_media
    out = b.tick()
    check(f"纯图 {_q_media:.1f}s 封批",
          len(out) == 1 and out[0].reason == "media", f"{out}")

    # 6) max_wait：一直发也要回（max_batch 调大，单独测这一条规则）
    #    ⚠ 2026-11-08 上限 12 → 30，这里读 policy 的值，不写死。
    _mw = L.ListenPolicy().max_wait
    clock = Clock()
    b = L.MessageBatcher(L.ListenPolicy(max_batch=20), clock=clock)
    # 每 3 秒补一句：普通窗口（6s）永远不会先到期，逼出 max_wait
    for i in range(10):
        clock.now = float(i * 3)
        b.feed(msg("c1", "小明", f"第{i}句", at=float(i * 3), local_id=i + 1))
    clock.now = _mw - 0.1
    check(f"max_wait 前一刻还没封（{_mw - 0.1:.0f}s）", b.tick() == [], "sealed early")
    clock.now = _mw
    out = b.tick()
    check(f"max_wait={_mw:.0f}s 强制封批", len(out) == 1, f"{out}")
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

    # 8.1) allow_self 明确放行的会话要进批（filehelper 自测通道）
    b2 = L.MessageBatcher(clock=clock,
                          allow_self_for=lambda c: c == "filehelper")
    b2.feed(msg("filehelper", "我", "自己发的测试", at=1.0, is_self=True))
    check("allow_self 放行后进批", "filehelper" in b2.pending(), f"{b2.pending()}")
    b3 = L.MessageBatcher(clock=clock,
                          allow_self_for=lambda c: c == "filehelper")
    b3.feed(msg("c1", "我", "别的会话", at=1.0, is_self=True))
    check("allow_self 只放行指定会话", b3.pending() == {}, f"{b3.pending()}")

    # 8.2) 跳过日志：同会话 120 秒内只打一行，且要带她的原话。
    #      以前每条自己的消息都打一行「自己发的消息」，和上一条「收」挨在一起，
    #      看起来像"对方的消息被跳过了"（2026-11-08 用户当场这么问）。
    logs: list[str] = []
    clock4 = Clock()
    b4 = L.MessageBatcher(clock=clock4, on_log=logs.append)
    for i in range(3):
        b4.feed(msg("c9", "老爸", f"她说的第{i}句", at=1.0 + i, is_self=True))
        clock4.now += 1.0
    check("跳过日志同会话只打一行", len(logs) == 1, f"{logs}")
    check("跳过日志带她的原话", logs and "她说的第0句" in logs[0], f"{logs}")
    check("跳过日志不再说「allow_self 没对这个会话开」",
          logs and "没对这个会话开" not in logs[0], f"{logs}")
    # 另一个会话各算各的
    b4.feed(msg("c10", "小红", "另一句", at=9.0, is_self=True))
    check("换个会话重新打一行", len(logs) == 2, f"{logs}")
    # 过了 120 秒再打
    clock4.now = 200.0
    b4.feed(msg("c9", "老爸", "很久以后", at=200.0, is_self=True))
    check("超过 120 秒再打一行", len(logs) == 3, f"{logs}")

    # 9) 多会话互不干扰
    #    这里用固定窗口（adaptive=False）：测的是"两个会话不是同一个桶"，
    #    不该被自适应的秒数变化牵着走。
    clock = Clock()
    b = L.MessageBatcher(L.ListenPolicy(adaptive=False, batch_min_wait=0.0), clock=clock)
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
    b = L.MessageBatcher(L.ListenPolicy(adaptive=False, batch_min_wait=0.0), clock=clock)
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
    b = L.MessageBatcher(L.ListenPolicy(adaptive=False, batch_min_wait=0.0), clock=clock)
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
    # 这里测的是**算法本身**（字数/标点怎么调秒数），所以把
    # batch_min_wait 关掉 —— 默认 6 秒的下限会把所有结果夹成 6.0，
    # 相对关系就测不出来了。下限的行为另有专门用例。
    pol = L.ListenPolicy(batch_min_wait=0.0)

    check(f"【在吗】等最久（≈{pol.quiet_max:.1f}）",
          abs(L.adaptive_quiet("在吗", pol) - pol.quiet_max) < 0.01,
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
    fixed = L.ListenPolicy(adaptive=False, batch_min_wait=0.0)
    check("adaptive=False 用 quiet_same_author",
          L.adaptive_quiet("在吗", fixed) == fixed.quiet_same_author,
          f"{L.adaptive_quiet('在吗', fixed)}")

    # 可见字数：空白不算，纯标点也不该是 0
    check("空白不计入", L.visible_chars(" 你 好 ") == 2,
          str(L.visible_chars(" 你 好 ")))
    check("全空白兜底为 1", L.visible_chars("   ") == 1,
          str(L.visible_chars("   ")))

    # 端到端：对方断句三连，攒成一批
    _q3 = L.adaptive_quiet("吃饭了没", L.ListenPolicy())
    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    b.feed(msg("c1", "小明", "大肥鱼", at=0.0))
    clock.now = 1.0
    b.feed(msg("c1", "小明", "你在不在", at=1.0))
    clock.now = 2.0
    b.feed(msg("c1", "小明", "吃饭了没", at=2.0))
    clock.now = 2.0 + _q3 - 0.5
    check("断句期间一直不封批", b.tick() == [], "sealed early")
    clock.now = 2.0 + _q3
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
    _polg = L.ListenPolicy()
    _qg = max(L.adaptive_quiet("那你说说", _polg), _polg.post_reply_grace - 1.0)
    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    b.feed(msg("c1", "小明", "那你说说", at=1.0), replied_at=0.0)
    clock.now = 1.0 + _qg - 0.1
    check(f"冷却期内 {_qg - 0.1:.1f}s 不封批", b.tick() == [], "sealed too early")
    clock.now = 1.0 + _qg
    out = b.tick()
    check(f"冷却 {_qg:.1f}s 到期封批", len(out) == 1, f"{out}")

    # 2) 她 0s 说完，对方 5s 才说 → 冷却只剩 1s，但普通窗口（4s）更长，
    #    取两者较大值：宁可多等，也别在她刚说完就抢话
    _qg2 = max(L.adaptive_quiet("那你说说", _polg), _polg.post_reply_grace - 5.0)
    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    b.feed(msg("c1", "小明", "那你说说", at=5.0), replied_at=0.0)
    clock.now = 5.0 + _qg2 - 0.1
    check("冷却叠加普通窗口（不提前封批）", b.tick() == [], "sealed early")
    clock.now = 5.0 + _qg2
    check(f"冷却叠加后 {5.0 + _qg2:.1f}s 封批", len(b.tick()) == 1, "did not seal")

    # 3) 距她说完已经 10s → 超过冷却，按普通窗口（自适应上限）
    _qg3 = L.adaptive_quiet("在吗", _polg)
    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    b.feed(msg("c1", "小明", "在吗", at=10.0), replied_at=0.0)
    clock.now = 10.0 + _qg3 - 0.1
    check("冷却过期后走普通窗口", b.tick() == [], "sealed too early")
    clock.now = 10.0 + _qg3
    check(f"普通窗口 {_qg3:.1f}s 封批", len(b.tick()) == 1, "did not seal")

    # 4) 被 @ 时不加冷却（对方在叫你，别让人等）
    clock = Clock()
    b = L.MessageBatcher(clock=clock)
    b.feed(msg("c1", "小明", "@大肥鱼 在吗", at=1.0, at_bot=True),
           replied_at=0.0)
    clock.now = 2.1
    check("@ 不套冷却（1s 就回）", len(b.tick()) == 1, "被冷却拖住了")


def test_batch_judge() -> None:
    """到点问一次模型：他说完了没有。

    用户的原话：「底线限制，最短是 6 秒，然后超过 6 秒的时候，
    需要模型判断是否对方可能还有下一句话没说完，如果有继续等待，
    最久不能超过 30 秒」。这里把四条都钉住。
    """
    section("攒批判定（到点问模型）")

    # 0) 不启用判定 = 老行为：到点就封
    clock = Clock()
    b = L.MessageBatcher(L.ListenPolicy(batch_min_wait=6.0), clock=clock)
    b.feed(msg("c1", "小明", "在吗", at=0.0))
    clock.now = 6.0
    check("没启用判定时到点就封", len(b.tick()) == 1, "did not seal")
    check("没启用判定时 due 为空", b.due() == [], f"{b.due()}")

    # 1) 启用判定：到点先不封，报名等结论
    clock = Clock()
    b = L.MessageBatcher(L.ListenPolicy(batch_min_wait=6.0, judge_max_rounds=1),
                         clock=clock, judge=True)
    b.feed(msg("c1", "小明", "在吗", at=0.0))
    clock.now = 5.9
    check("未到点不报名", b.due() == [], f"{b.due()}")
    clock.now = 6.0
    check("到点不直接封", b.tick() == [], "sealed too early")
    due = b.due()
    check("到点报名判定", len(due) == 1 and due[0][0] == "c1", f"{due}")
    check("同一会话不重复报名", b.due() == [], f"{b.due()}")
    check("等判定期间 tick 也不封", b.tick() == [], "sealed while awaiting")

    # 2) 判定「还有下一句」→ 再等 judge_extra
    clock.now = 6.5
    check("more=true 时不封", b.verdict("c1", True) is None, "sealed")
    clock.now = 6.5 + 8.0 - 0.1
    check("延长期间不封", b.tick() == [], "sealed early")
    clock.now = 6.5 + 8.0
    check("延长到期后封批", len(b.tick()) == 1, "did not seal")

    # 3) 判定「说完了」→ 立刻封
    clock = Clock()
    b = L.MessageBatcher(L.ListenPolicy(batch_min_wait=6.0), clock=clock,
                         judge=True)
    b.feed(msg("c1", "小明", "在吗", at=0.0))
    clock.now = 6.0
    b.due()
    out = b.verdict("c1", False)
    check("more=false 立刻封", out is not None and len(out.messages) == 1, f"{out}")
    check("封批理由 judged", out is not None and out.reason == "judged", f"{out}")

    # 4) 硬上限：一直说「还有」也越不过 max_wait
    clock = Clock()
    pol = L.ListenPolicy(batch_min_wait=6.0, judge_extra=8.0,
                         judge_max_rounds=9, max_wait=30.0)
    b = L.MessageBatcher(pol, clock=clock, judge=True)
    b.feed(msg("c1", "小明", "在吗", at=0.0))
    # 每一轮都等满 judge_extra 再判：deadline = 上次判定时刻 + 8
    # （14.0 那次其实没到 deadline，due() 不报 —— 结果就是一个假的
    #  "verdict 但根本没判定在跑"，会把这一批提前封掉。用 14.5 / 23.0 才是
    #  真的"一直说还有"。）
    for _t in (6.0, 14.5, 23.0):
        clock.now = _t
        b.due()
        clock.now = _t + 0.5
        b.verdict("c1", True)          # 一直说"还有"
    clock.now = 29.9
    check("没到 30 秒时还在等", b.tick() == [], "sealed early")
    clock.now = 30.0
    out = b.tick()
    check("30 秒硬上限强制封批", len(out) == 1, f"{out}")
    check("封批理由 max_wait",
          out and out[0].reason == "max_wait", f"{out and out[0].reason}")

    # 5) 判定期间对方又说话 → 旧结论作废，但**这一批不能悬着**
    #
    #    ⚠ 2026-11-11 改：以前这里断言「旧判定回来被忽略」，也就是
    #      verdict() 返回 None、桶继续等下一轮。线上那次就是在这条路上静默了
    #      两分半 —— 每次判定都会在"提交完立刻被新消息作废"的窗口里打转，
    #      对方连发了四五句，这边一次都没回，日志还显示"判定 说完了"。
    #      现在：结论过时就直接按"说完了"封批（reason=judged_stale），
    #      话一句都不丢，只是这一轮不再重新问。
    clock = Clock()
    b = L.MessageBatcher(L.ListenPolicy(batch_min_wait=6.0), clock=clock,
                         judge=True)
    b.feed(msg("c1", "小明", "在吗", at=0.0))
    clock.now = 6.0
    b.due()
    clock.now = 6.2
    b.feed(msg("c1", "小明", "问你个事", at=6.2))
    stale = b.verdict("c1", False)
    check("旧判定回来立刻封批（不丢话）",
          stale is not None and len(stale.messages) == 2, f"{stale}")
    check("封批理由 judged_stale",
          stale is not None and stale.reason == "judged_stale",
          f"{stale and stale.reason}")
    check("两条都在这一批里",
          stale is not None and [m.text for m in stale.messages] == ["在吗", "问你个事"],
          f"{stale and [m.text for m in stale.messages]}")
    check("桶已清空", b.pending() == {}, f"{b.pending()}")

    # 6) 从没提交过判定就调 verdict → 安静忽略（调用方的错，不该封别人的批）
    clock = Clock()
    b = L.MessageBatcher(L.ListenPolicy(batch_min_wait=6.0), clock=clock,
                         judge=True)
    b.feed(msg("c1", "小明", "在吗", at=0.0))
    clock.now = 10.0
    check("没判定在跑时 verdict 被忽略", b.verdict("c1", False) is None,
          "phantom verdict sealed the batch")
    check("那一批还在", b.pending().get("c1") == 1, f"{b.pending()}")

    # 7) 判定线程卡死（请求发出去了，结论永远不回来）→ 必须自己解冻
    #
    #    2026-11-11 线上就是这个：21:52:16 提交的判定卡在本地模型服务上，
    #    `awaiting` 一直 True，tick() 和 due() 全都绕开这个会话，
    #    她整整两分半没理人，日志一个字都没打。这条测试守的就是它。
    logs: list[str] = []
    clock = Clock()
    pol = L.ListenPolicy(batch_min_wait=6.0, judge_timeout=6.0)
    b = L.MessageBatcher(pol, clock=clock, judge=True, on_log=logs.append)
    for i, t in enumerate((0.0, 1.0, 2.0)):
        b.feed(msg("c1", "小明", f"第{i}句", at=t, local_id=i + 1))
    clock.now = 10.0
    due = b.due()
    check("提交了判定", len(due) == 1, f"{due}")
    check("卡住前 tick 不封批", b.tick() == [], "sealed while awaiting")
    # judge_timeout + 2 = 8 秒之内还算正常，过了就必须解冻
    clock.now = 17.0
    check("8 秒内不解冻", b.tick() == [], "thawed too early")
    clock.now = 18.5
    out = b.tick()
    check("判定卡死 8 秒后强制封批", len(out) == 1, f"{out}")
    check("解冻封批一条不丢", out and len(out[0].messages) == 3,
          f"{out and len(out[0].messages)}")
    check("解冻打了一行日志",
          any("没回音" in s for s in logs), f"{logs}")
    check("解冻后桶已清空", b.pending() == {}, f"{b.pending()}")

    # 6) 最短等待对长消息也生效（用户要的是「底线 6 秒」）
    pol = L.ListenPolicy(batch_min_wait=6.0)
    long_text = ("我今天去长坂坡了，看到了樱花，还看到了枣树，"
                 "回来的路上碰到了老王他家的狗，它一直冲我叫。" * 2)
    check("长消息也至少等 6 秒",
          abs(L.adaptive_quiet(long_text, pol) - 6.0) < 0.01,
          f"{L.adaptive_quiet(long_text, pol)}")
    pol_off = L.ListenPolicy(batch_min_wait=0.0)
    check("下限设 0 时长消息回到 1.5 秒",
          abs(L.adaptive_quiet(long_text, pol_off) - 1.5) < 0.01,
          f"{L.adaptive_quiet(long_text, pol_off)}")
    check("下限调大能超过自适应上限",
          abs(L.adaptive_quiet("在吗", L.ListenPolicy(batch_min_wait=10.0)) - 10.0) < 0.01,
          f"{L.adaptive_quiet('在吗', L.ListenPolicy(batch_min_wait=10.0))}")


def main() -> int:
    print("=" * 66)
    print("倾听层自检（攒批 / 到期 / 硬上限 / 自适应窗口）")
    print("=" * 66)

    test_listen()
    test_adaptive()
    test_transcript()
    test_post_reply_grace()
    test_batch_judge()

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
