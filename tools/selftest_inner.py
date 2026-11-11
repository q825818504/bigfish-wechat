"""内在状态体检：表达预算 / 余韵 / 被无视，三块一起。

跑法：
    python tools/selftest_inner.py

不调模型、不碰微信、不读用户真实数据 —— 全程用临时数据根。

## 为什么这三块要一起测

它们是同一个回路的三个环节：

    被无视 → 情绪掉 → 余韵留下 → 下一轮预算收紧

任何一环断了，症状都一样：**她不报错，只是回得像没事发生**。
所以这里不只测"函数返回对不对"，还测"链路能不能真的走通"：

    写一条 dropped → 结算 → mood 变了 → afterglow 有了 → policy 收紧了

## 守哪几条底线

1. 内容侧的定性句**不许出现数值和情绪标签**（"心情 38""你很生气"）。
   一旦出现，模型就会演，而不是照做。
2. 余韵**不许在情绪没动的时候写**。小波动也写，她会一天到晚在情绪里。
3. 被无视只压**主动开口**的分数，不许影响回复。
4. 任何一块数据坏了都不许抛 —— 余韵丢了不该让她失语。
"""
from __future__ import annotations

import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PASS = 0
FAIL = 0
SKIP = 0
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
        FAILED.append(name + (f"｜{detail}" if detail else ""))
        print(f"  {BAD_MARK} {name}")
        if detail:
            print(f"      {detail}")


def skip(name: str, why: str) -> None:
    global SKIP
    SKIP += 1
    print(f"  - {name}（跳过：{why}）")


def at(hour: int, minute: int = 0, *, days: int = 0) -> float:
    """构造一个"某天某点"的时间戳，不依赖跑测试时的真实时刻。"""
    import datetime as _dt

    return (_dt.datetime(2026, 11, 8, hour, minute)
            + _dt.timedelta(days=days)).timestamp()


def section(title: str) -> None:
    print()
    print(title)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="bf-inner-")
    os.environ["BIGFISH_DATA_ROOT"] = tmp

    from bigfish import afterglow as A
    from bigfish import policy as P
    from bigfish import proactive as PRO

    print("=" * 60)
    print("内在状态体检：预算 / 余韵 / 被无视")
    print(f"临时数据根：{tmp}")
    print("=" * 60)

    # ════════════════════════════════════════════════════════════
    section("[预算] 心情 → 条数与字数")
    # ════════════════════════════════════════════════════════════

    bright = P.response_policy(mood=90, dims={"happy": 8}, energy=80,
                               now=at(15))
    check("心情 90 → 可以分 4 条", bright.max_parts == 4, str(bright))
    check("心情 90 → 40 字上限", bright.max_chars == 40, str(bright))

    low = P.response_policy(mood=30, dims={"sad": 6}, energy=70, now=at(15))
    check("心情 30 → 只发 1 条", low.max_parts == 1, str(low))
    check("心情 30 → 14 字上限", low.max_chars == 14, str(low))
    check("心情 30 → 不接许可更高", low.silence_bias >= 0.35, str(low))
    check("心情 30 → 强制回阈值放宽到 3", low.must_reply_after == 3, str(low))

    mid = P.response_policy(mood=60, dims={}, energy=70, now=at(15))
    check("心情 60 → 2~3 条", mid.max_parts in (2, 3), str(mid))

    # 精力只做减法
    tired = P.response_policy(mood=90, dims={}, energy=20, now=at(15))
    check("精力 20 → 条数压到 1", tired.max_parts == 1, str(tired))
    check("精力 20 → 字数比同心情少", tired.max_chars < bright.max_chars,
          f"{tired.max_chars} vs {bright.max_chars}")

    night = P.response_policy(mood=90, dims={}, energy=80, now=at(2))
    check("凌晨 2 点 → 条数压到 1", night.max_parts == 1, str(night))
    check("凌晨 2 点 → 不接许可更高", night.silence_bias > bright.silence_bias,
          str(night))

    # ════════════════════════════════════════════════════════════
    section("[注意力] 允许没听清（走神许可）")
    # ════════════════════════════════════════════════════════════
    #
    # 她一直「永远切题、永远完整」—— 那是整套系统里最像 AI 的地方。
    # 这一节守的是：累、深夜、连着回了好几轮时，可以只接半句；
    # 状态好的时候，不给这个许可（一个天天走神的人是傻子）。

    check("状态好时不许走神", bright.attention_line == "", bright.attention_line)
    check("精力 20 → 给了走神许可", bool(tired.attention_line),
          tired.attention_line)
    check("凌晨 2 点 → 给了走神许可", bool(night.attention_line),
          night.attention_line)

    groggy = P.response_policy(mood=90, dims={"happy": 8}, energy=80,
                               now=at(15), recent_replies=5)
    check("连着回 5 轮 → 强许可",
          "可以不回答那个问题" in groggy.attention_line, groggy.attention_line)
    check("许可里不写情绪词（她会演）",
          "难过" not in groggy.attention_line
          and "累" not in groggy.attention_line.split("——")[0],
          groggy.attention_line)
    check("走神许可和语气描述是两件事",
          groggy.attention_line != groggy.tone_line, "")

    # ════════════════════════════════════════════════════════════
    section("[注意力] 被打断：话说到一半会停")
    # ════════════════════════════════════════════════════════════
    #
    # 以前她分条连发时，对方插一句，她照样把第 3、4 条发完 ——
    # 那不是"把话说完整"，那是没在听。

    from bigfish import say as SAY

    own_only = [{"local_id": 10, "is_self": True},
                {"local_id": 11, "is_self": True}]
    with_peer = own_only + [{"local_id": 12, "is_self": False}]

    def _recent(rows):
        return lambda chat, n: rows

    check("只有自己的消息 → 不算被打断",
          SAY._peer_spoke("c", _recent(own_only), 10) is False)
    check("对方插了一句 → 算被打断",
          SAY._peer_spoke("c", _recent(with_peer), 10) is True)
    check("插话在基线之前 → 不算",
          SAY._peer_spoke("c", _recent(with_peer), 12) is False)
    check("没有基线时不判（第一条必须发出去）",
          SAY._peer_spoke("c", _recent(with_peer), 0) is False)

    def _boom(chat, n):
        raise RuntimeError("读不出来")

    check("读不出来时不误判（宁可继续发）",
          SAY._peer_spoke("c", _boom, 10) is False)

    # ════════════════════════════════════════════════════════════
    section("[收尾] 说了「我先去忙了」就真的停")
    # ════════════════════════════════════════════════════════════
    #
    # `-`（这一轮不接）只表示"这句不回"，下一轮对方再来她照样说话。
    # 而"我先去忙了"是**结束这段对话** —— 说完了还主动找他，就是打自己的脸。

    check("「我先去忙了」算收尾", P.is_farewell("我先去忙了") is True)
    check("「那我去睡了」算收尾", P.is_farewell("那我去睡了") is True)
    check("「明天见」算收尾", P.is_farewell("明天见") is True)
    check("问句不算（「你睡了？」是关心）",
          P.is_farewell("你睡了？") is False)
    check("普通回复不算", P.is_farewell("好") is False)
    check("空的不算", P.is_farewell("") is False)

    # ════════════════════════════════════════════════════════════
    section("[心情] 底子：睡差了底子就低")
    # ════════════════════════════════════════════════════════════
    #
    # 以前心情永远往 70 衰减，所以"实时心情"在多数时候显示的是衰减后的
    # 余数 —— 睡差了、身上难受、连着阴三天，底子都是 70。
    # 底子低的时候一件小事就够烦，底子高的时候同样的事笑笑就过了。

    from bigfish import mood_judge as MJ

    _good = MJ.baseline_of(sleep_debt=0, energy=88,
                           discomfort="", weather="18°C，晴")
    _bad = MJ.baseline_of(sleep_debt=240, energy=20,
                          discomfort="头有点沉", weather="5°C，雨")
    check("状态好 → 底子高", _good > 72, f"{_good:.1f}")
    check("状态差 → 底子低", _bad < 50, f"{_bad:.1f}")
    check("底子有下界（再差也不演）", _bad >= 25, f"{_bad:.1f}")
    check("底子有上界（留余地给事件）", _good <= 92, f"{_good:.1f}")
    check("欠觉越多底子越低",
          MJ.baseline_of(sleep_debt=240) < MJ.baseline_of(sleep_debt=0),
          f"{MJ.baseline_of(sleep_debt=240):.1f} vs {MJ.baseline_of(sleep_debt=0):.1f}")
    check("身上难受压底子",
          MJ.baseline_of(discomfort="眼睛有点干") < MJ.baseline_of(discomfort=""),
          "")
    check("阴雨天比晴天低",
          MJ.baseline_of(weather="12°C，雨") < MJ.baseline_of(weather="18°C，晴"),
          "")
    check("取不到值时退回中性", 55 <= MJ.baseline_of(energy=None) <= 80,
          f"{MJ.baseline_of(energy=None):.1f}")

    # ════════════════════════════════════════════════════════════
    section("[口头禅] 她的用词会漂")
    # ════════════════════════════════════════════════════════════
    #
    # 她的用词以前是固定的（soul 只读渲染，不学习），八个月后说话和今天
    # 一模一样。这一节守的是：某个词连着几条消息都在，才算"老爱说"。

    _rows = [("我", "确实有点累"), ("他", "嗯"), ("我", "确实不想动"),
             ("他", "哦"), ("我", "确实今天不想说话")]
    _tics = SAY.recent_tics(_rows, min_msgs=3)
    check("三条消息都有「确实」→ 认出来了",
          _tics and _tics[0][0] == "确实", str(_tics))
    check("只统计她自己说的",
          SAY.recent_tics([("他", "确实确实确实"), ("他", "确实"),
                           ("他", "确实")], min_msgs=3) == [],
          "")
    check("一条消息里重复十遍不算老爱说",
          SAY.recent_tics([("我", "确实" * 10)], min_msgs=3) == [], "")
    check("凑不够就不硬凑", SAY.recent_tics([], min_msgs=3) == [], "")
    check("停用词不进口头禅",
          SAY.recent_tics([("我", "这个东西就是这样")] * 3, min_msgs=3) == [],
          str(SAY.recent_tics([("我", "这个东西就是这样")] * 3, min_msgs=3)))

    # ════════════════════════════════════════════════════════════
    section("[预算] 跨轮配额：刚说过话就少说（0.11.1）")
    # ════════════════════════════════════════════════════════════
    #
    # 用户实测：「我连发两条，她第一轮 3 条、第二轮又 3 条 = 一次 6 条」。
    # 单轮看每轮都合理，连起来就是刷屏。预算必须记得自己刚说过什么。

    same = P.response_policy(mood=90, dims={"happy": 8}, energy=80, now=at(15))
    check("不传配额时还是 4 条", same.max_parts == 4, str(same))

    few = P.response_policy(mood=90, dims={"happy": 8}, energy=80, now=at(15),
                            recent_parts=3)
    check("刚发过 3 条 → 只发 1 条", few.max_parts == 1, str(few))
    check("刚发过 3 条 → 字数收紧", few.max_chars <= 24, str(few))

    many = P.response_policy(mood=90, dims={"happy": 8}, energy=80, now=at(15),
                             recent_parts=6)
    check("刚发过 6 条 → 只发 1 条", many.max_parts == 1, str(many))
    check("刚发过 6 条 → 字数更紧", many.max_chars <= 16, str(many))

    rounds = P.response_policy(mood=90, dims={"happy": 8}, energy=80, now=at(15),
                               recent_replies=2)
    check("连着回了两轮 → 只发 1 条", rounds.max_parts == 1, str(rounds))

    # 只收紧、不放宽：心情很低时配额不该把 1 条变多
    still_low = P.response_policy(mood=30, dims={"sad": 6}, energy=70, now=at(15),
                                  recent_parts=0, recent_replies=0)
    check("配额不放宽低心情", still_low.max_parts == 1, str(still_low))
    check("脏配额不崩", P.response_policy(recent_parts="abc",
                                          recent_replies=None).max_parts >= 1,
          str(P.response_policy(recent_parts="abc")))

    # ════════════════════════════════════════════════════════════
    section("[预算] 读不到状态时不抛、不夸张")
    # ════════════════════════════════════════════════════════════

    unknown = P.response_policy()
    check("没有状态 → 中性 3 条",
          unknown.max_parts == 3 and unknown.max_chars == 30, str(unknown))
    check("没有状态 → 不接许可很低", unknown.silence_bias <= 0.10, str(unknown))

    # ════════════════════════════════════════════════════════════
    section("[预算] 定性句：不许出现数值和情绪标签")
    # ════════════════════════════════════════════════════════════

    banned = ("心情", "精力", "情绪值", "/100", "数值")
    for name, pol in (("低", low), ("中", mid), ("高", bright), ("累", tired)):
        text = pol.tone_line
        bad = [w for w in banned if w in text]
        check(f"{name}档的倾向句不报数值/不点情绪", not bad, f"命中 {bad}：{text}")
        check(f"{name}档的倾向句没有阿拉伯数字",
              not any(ch.isdigit() for ch in text), text)

    # ════════════════════════════════════════════════════════════
    section("[预算] clamp：面板配的再松，状态说了算")
    # ════════════════════════════════════════════════════════════

    check("clamp_parts 取小", low.clamp_parts(4) == 1, str(low.clamp_parts(4)))
    check("clamp_parts 面板更严时听面板", bright.clamp_parts(2) == 2)
    check("clamp_chars(0) 用预算", low.clamp_chars(0) == 14)
    check("clamp_chars 面板更严时听面板", bright.clamp_chars(20) == 20)
    check("clamp_chars 不会低于 8", low.clamp_chars(3) >= 8)

    # ════════════════════════════════════════════════════════════
    section("[余韵] 什么时候留")
    # ════════════════════════════════════════════════════════════

    check("小波动不留", A.classify(user_text="今天吃了面", mood_before=70,
                                   mood_after=71) == "")
    check("挨骂 + 掉了 4 分 → conflict",
          A.classify(user_text="你烦不烦", mood_before=70, mood_after=66) == "conflict")
    check("挨骂但没掉分 → 不记 conflict",
          A.classify(user_text="你烦不烦", mood_before=70, mood_after=70) != "conflict")
    check("掉了 6 分没有冲突词 → hurt",
          A.classify(user_text="我先睡了", mood_before=70, mood_after=64) == "hurt")
    check("sad 涨 5 → hurt",
          A.classify(user_text="嗯", mood_before=70, mood_after=69,
                     dims_delta={"sad": 5}) == "hurt")
    check("worry 涨 5 → worry",
          A.classify(user_text="我在医院", mood_before=70, mood_after=70,
                     dims_delta={"worry": 5}) == "worry")
    check("涨了 5 分 → warm",
          A.classify(user_text="想你了", mood_before=70, mood_after=75) == "warm")
    check("模型说很重、实际只掉 1 → 按模型",
          A.classify(user_text="算了", mood_before=70, mood_after=69,
                     judged_delta=-6) == "hurt")

    # ════════════════════════════════════════════════════════════
    section("[余韵] 写入 / 覆盖 / 上限")
    # ════════════════════════════════════════════════════════════

    A.save({"version": 1, "items": []})
    item = A.from_turn(chat_id="wxid_a", user_text="你烦不烦", reply="……",
                       mood_before=70, mood_after=66, now=at(14))
    check("冲突写进去了", bool(item) and item["kind"] == "conflict",
          str(item))
    check("原文进了引用", "你烦不烦" in (item or {}).get("quote", ""),
          str((item or {}).get("quote")))
    check("由头里没有数值",
          not any(ch.isdigit() for ch in (item or {}).get("residue", "")),
          str((item or {}).get("residue")))

    state = A.load()
    check("落盘能读回一条", len(state["items"]) == 1, str(len(state["items"])))

    A.from_turn(chat_id="wxid_a", user_text="随你便", reply="嗯",
                mood_before=66, mood_after=62, now=at(15))
    state = A.load()
    check("同类只留最新一条", len(state["items"]) == 1, str(len(state["items"])))
    check("最新那条来自第二次", "随你便" in state["items"][0]["quote"],
          str(state["items"][0]["quote"]))

    A.from_turn(chat_id="wxid_a", user_text="想你了", reply="嗯",
                mood_before=70, mood_after=76, now=at(16))
    A.from_turn(chat_id="wxid_a", user_text="我在医院", reply="怎么了",
                mood_before=70, mood_after=70, dims_delta={"worry": 5},
                now=at(17))
    A.from_turn(chat_id="wxid_b", user_text="你烦不烦", reply="……",
                mood_before=70, mood_after=64, now=at(18))
    state = A.load()
    check("最多留 3 条", len(state["items"]) <= 3, str(len(state["items"])))
    check("留下来的优先是重的",
          state["items"][0]["kind"] in ("conflict", "hurt"),
          str([x["kind"] for x in state["items"]]))

    # ════════════════════════════════════════════════════════════
    section("[余韵] 预算修正 / 哄好 / 过期")
    # ════════════════════════════════════════════════════════════

    A.save({"version": 1, "items": []})
    A.from_turn(chat_id="wxid_a", user_text="你烦不烦", reply="……",
                mood_before=70, mood_after=66, now=at(14))
    eff = A.current_effect(now=at(14))
    check("冲突会收紧预算", eff["silence_bias"] >= 0.20, str(eff))
    check("冲突会砍字数", eff["max_chars"] <= -8, str(eff))
    check("冲突会要求避开撒娇", "主动撒娇" in eff["avoid"], str(eff))

    n = A.note_repair("对不起，刚刚是我不好", now=at(15))
    check("道歉能软消", n == 1, str(n))
    eff2 = A.current_effect(now=at(15))
    check("软消之后预算放松", eff2["silence_bias"] < eff["silence_bias"],
          f"{eff2['silence_bias']} vs {eff['silence_bias']}")
    text = A.render(now=at(15))
    check("软消之后由头变软", "别扭" in text or "过去" in text, text[:80])

    check("尾韵期还在", bool(A.active(A.load(), now=at(15))))
    check("尾韵期过了就清", not A.active(A.load(), now=at(16)))
    check("非道歉不消", A.note_repair("今天天气不错", now=at(15)) == 0)

    A.save({"version": 1, "items": []})
    A.from_turn(chat_id="wxid_a", user_text="你烦不烦", reply="……",
                mood_before=70, mood_after=66, now=at(14))
    check("冲突 4 小时内还在",
          bool(A.active(A.load(), now=at(17, 30))))
    check("冲突 4 小时后没了",
          not A.active(A.load(), now=at(19)))

    # ════════════════════════════════════════════════════════════
    section("[余韵] 坏文件 / 渲染")
    # ════════════════════════════════════════════════════════════

    with open(A.state_path(), "w", encoding="utf-8") as f:
        f.write("{ not json")
    check("坏文件读成空", A.load()["items"] == [])
    check("坏文件渲染成空串", A.render(now=at(14)) == "")

    A.save({"version": 1, "items": []})
    A.from_turn(chat_id="wxid_a", user_text="我在医院", reply="怎么了",
                mood_before=70, mood_after=70, dims_delta={"worry": 5},
                now=at(14))
    rendered = A.render(now=at(14))
    check("渲染带标题", "还留着的" in rendered, rendered[:60])
    check("渲染不提数值", "/100" not in rendered and "心情" not in rendered)

    # ════════════════════════════════════════════════════════════
    section("[被无视] 记录 → 等级 → 折扣")
    # ════════════════════════════════════════════════════════════

    st = PRO.ProactiveState()
    st.awaiting["wxid_a"] = {"sent_at": at(9), "source": "greeting",
                             "text": "早", "followup_used": False}
    n = PRO.expire_neglect(st, now=at(20))
    check("8 小时没回 → 转成被无视", n == 1, str(n))
    check("dropped 里有一条", len(st.dropped) == 1, str(st.dropped))
    check("新记录默认未被消费", st.dropped[0]["reacted"] is False,
          str(st.dropped[0]))
    check("消费前能取出来", len(PRO.unreacted_dropped(st)) == 1)

    level = PRO.note_neglect_reacted(st, PRO.unreacted_dropped(st), now=at(20))
    check("等级 +1", level == 1, str(level))
    check("消费后不再返回", PRO.unreacted_dropped(st) == [])
    check("惩罚有截止时间", st.penalty_until > at(20), str(st.penalty_until))
    check("1 级打 75 折", abs(PRO.penalty(st, now=at(20)) - 0.75) < 1e-6,
          str(PRO.penalty(st, now=at(20))))

    st.neglect_level = 2
    check("2 级打 5 折", abs(PRO.penalty(st, now=at(20)) - 0.50) < 1e-6)
    st.neglect_level = 3
    check("3 级打 3 折", abs(PRO.penalty(st, now=at(20)) - 0.30) < 1e-6)
    st.neglect_level = 9
    check("等级有上限", PRO.penalty(st, now=at(20)) >= 0.30)

    # ════════════════════════════════════════════════════════════
    section("[被无视] 那句「你最近很忙吗」")
    # ════════════════════════════════════════════════════════════

    st = PRO.ProactiveState()
    st.dropped = [{"at": at(9), "chat_id": "wxid_a", "source": "greeting",
                   "text": "早", "reacted": True}]
    st.neglect_level = 1
    check("1 级还不问", PRO.candidate_check_in(st, chat_id="wxid_a",
                                              now=at(21)) is None)

    st.neglect_level = 2
    cand = PRO.candidate_check_in(st, chat_id="wxid_a", now=at(21))
    check("2 级会问一句", cand is not None and cand.source == "check_in")
    check("分数高到能穿过去", cand is not None and cand.score >= 70,
          str(getattr(cand, "score", None)))
    check("理由里没有数值", cand is not None and "2" not in cand.reason,
          getattr(cand, "reason", ""))

    st.last_check_in_at = at(20)
    check("24 小时内不重复问",
          PRO.candidate_check_in(st, chat_id="wxid_a", now=at(21)) is None)
    check("过了 24 小时可以再问",
          PRO.candidate_check_in(st, chat_id="wxid_a",
                                now=at(20) + 25 * 3600) is not None)

    # ════════════════════════════════════════════════════════════
    section("[被无视] 他回来之后")
    # ════════════════════════════════════════════════════════════

    st = PRO.ProactiveState()
    st.neglect_level = 2
    st.penalty_until = at(15)
    PRO.note_peer_replied(st, chat_id="wxid_a", now=at(14))
    check("惩罚期内不赎回", st.neglect_level == 2, str(st.neglect_level))
    PRO.note_peer_replied(st, chat_id="wxid_a", now=at(16))
    check("过了惩罚期回落一级", st.neglect_level == 1, str(st.neglect_level))

    # ════════════════════════════════════════════════════════════
    section("[道别] 到点才说，而且只在你还在的时候")
    # ════════════════════════════════════════════════════════════

    from bigfish import life as L

    st = L.LifeState()
    st.bedtime = at(23, 20)
    st.asleep = False
    check("睡前 15 分钟、刚聊完 → 该说",
          L.goodnight_due(st, at(23, 5), at(22, 45)) is True)
    check("下午不在窗口里",
          L.goodnight_due(st, at(15), at(14, 50)) is False)
    check("40 分钟没互动就不说 —— 对空气道别很怪",
          L.goodnight_due(st, at(23, 5), at(22, 0)) is False)
    check("从来没互动过就不说",
          L.goodnight_due(st, at(23, 5), 0.0) is False)
    st.asleep = True
    check("已经睡了就不说",
          L.goodnight_due(st, at(23, 5), at(22, 45)) is False)
    st.asleep = False
    st.bedtime = 0
    check("没有入睡点时不说",
          L.goodnight_due(st, at(23, 5), at(22, 45)) is False)

    # ════════════════════════════════════════════════════════════
    section("[日记] 跟着她的入睡点，不跟固定钟点")
    # ════════════════════════════════════════════════════════════

    jst = L.LifeState()
    jst.bedtime = at(22, 48)      # 她今天 22:48 睡（23:20 抖出来的）
    jst.asleep = False
    check("睡前 28 分钟还不到", L.journal_due(jst, at(22, 20)) is False)
    check("睡前 10 分钟该写", L.journal_due(jst, at(22, 38)) is True)
    check("入睡那一刻还在窗口内",
          L.journal_due(jst, at(22, 48)) is True)

    jst.asleep = True
    check("提前困倒了也能补写（睡后 40 分钟）",
          L.journal_due(jst, at(23, 28)) is True)
    check("睡后超过 2 小时不补",
          L.journal_due(jst, at(1, 0, days=1)) is False)

    jst.asleep = False
    jst.bedtime = 0
    check("没有入睡点就不写", L.journal_due(jst, at(23, 0)) is False)

    # ════════════════════════════════════════════════════════════
    section("[链路] 被无视 → 余韵 → 预算收紧")
    # ════════════════════════════════════════════════════════════

    A.save({"version": 1, "items": []})
    A.from_turn(chat_id="wxid_a", user_text="你先忙吧", reply="好",
                mood_before=70, mood_after=64, now=at(20))
    base = P.response_policy(mood=70, dims={}, energy=70, now=at(20))
    eff = A.current_effect(now=at(20))
    merged = P.apply_afterglow(base, eff)
    check("链路：余韵会收预算", merged.max_chars <= base.max_chars, str(merged))
    check("链路：余韵提高不接许可",
          merged.silence_bias >= base.silence_bias, str(merged))
    check("链路：条数不会掉到 0", merged.max_parts >= 1, str(merged))

    check("链路：余韵在提示词里有落点",
          "还留着的" in A.render(now=at(20)))

    # ── 出口 ──
    print()
    print("=" * 60)
    total = PASS + FAIL
    if FAIL:
        print(f"失败 {FAIL}/{total}")
        for n in FAILED:
            print(f"  - {n}")
        return 1
    print(f"全部通过 {PASS}/{total}"
          + (f"（跳过 {SKIP}）" if SKIP else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
