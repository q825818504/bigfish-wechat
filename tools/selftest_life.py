"""生活状态回归测试 —— 她到底会不会过日子。

跑法：
    python tools/selftest_life.py

不需要微信、不需要桌面、不需要网络。时间全靠构造 epoch，一秒都不 sleep。

## 为什么需要这个文件

`bigfish/life.py` 是第一个「不靠消息也会推进」的模块。
它一旦写错，症状和以前所有 bug 都不一样：

  · 她永远不睡 —— 凌晨三点还在，像客服
  · 她永远在睡 —— 白天叫不应，像死了
  · 你一整天不说话，她中午就睡了 —— 那是回声，不是人
  · 她一天换八次「在做什么」—— 问两句就自相矛盾
  · 她熬夜没有代价 —— 那就不是身体，是开关

所以这里的用例不是「函数返回值对不对」，而是**这些症状会不会回来**。

覆盖：
  节律：每天睡点/睡长不同、都在合理区间
  "一天"由醒来推动：醒着跨午夜不算新的一天
  入睡：到点睡；陪伴延长；上限后照样睡
  醒来：到点醒、精力按睡眠时长与债结算
  代价：熬夜 → 债 → 次日更晚醒、起床精力更低
  精力：醒着掉、睡着回、聊天额外掉、超时加速掉
  手头的事：同一天同一段稳定，换段才换
  午睡：只在下午、只一天一次
  睡着期间的消息：记账、可查、取走即清、过期清理
  冷启动：凌晨启动时知道自己在睡
  落盘：往返一致；坏 JSON 不抛
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import life as L  # noqa: E402

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


def at(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute).timestamp()


def fresh(year=2026, month=11, day=7, hour=10,
          minute=0) -> tuple[L.LifeState, float]:
    """一份刚冷启动、醒着的状态。"""
    state = L.LifeState()
    now = at(year, month, day, hour, minute)
    L.tick(state, now)
    return state, now


def hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


# ══════════════════════════════════════════════════════════════════════
# 节律
# ══════════════════════════════════════════════════════════════════════

def test_rhythm() -> None:
    print("\n[节律] 每天的睡点不一样，但都在合理区间")
    beds, durations = [], []
    for d in range(1, 15):
        state = L.LifeState()
        L.plan_day(state, at(2026, 11, d, 10))
        beds.append(hhmm(state.bedtime))
        durations.append((state.wake_at - state.bedtime) / 3600)

    check("14 天里至少有 5 种不同的睡点", len(set(beds)) >= 5,
          f"实际：{sorted(set(beds))}")

    minutes = []
    for d in range(1, 15):
        state = L.LifeState()
        L.plan_day(state, at(2026, 11, d, 10))
        m = L._minutes_of_day(state.bedtime)
        minutes.append(m)
    lo, hi = min(minutes), max(minutes)
    check("睡点全部落在 23:20±45 分钟内",
          23 * 60 + 20 - 45 <= lo and hi <= 23 * 60 + 20 + 45,
          f"区间 {lo / 60:.1f}h ~ {hi / 60:.1f}h")

    check("睡眠时长都在 6~10 小时之间",
          all(6.0 <= h <= 10.0 for h in durations),
          f"{min(durations):.1f}~{max(durations):.1f} 小时")

    check("醒来时刻总是晚于入睡时刻",
          all(s.wake_at > s.bedtime
              for s in [(lambda x: (L.plan_day(x, at(2026, 11, 5, 10)), x)[1])(L.LifeState())]))


def test_day_advances_on_wake() -> None:
    print("\n[一天] 由醒来推进，不由时钟推")
    state, now = fresh(2026, 11, 7, 10)
    old_day = state.day
    check("冷启动就定好了这一天", old_day == "2026-11-07", old_day)

    L.tick(state, at(2026, 11, 7, 22, 0))
    check("当天内 day 不变", state.day == old_day)

    L.tick(state, state.bedtime + 60)
    check("入睡时 day 仍是她醒着的那天", state.day == old_day,
          f"{old_day} → {state.day}")

    L.tick(state, state.wake_at + 60)
    check("醒来之后才进入新的一天", state.day != old_day,
          f"{old_day} → {state.day}")
    check("新的一天是醒来那天", state.day == "2026-11-08", state.day)


def test_cold_start() -> None:
    print("\n[冷启动] 凌晨启动时她知道自己在睡")
    night = L.LifeState()
    L.tick(night, at(2026, 11, 8, 2, 0))
    check("凌晨 2 点冷启动 → 判为睡着", night.asleep,
          f"asleep={night.asleep} bed={hhmm(night.bedtime)}")
    check("入睡时刻落在昨天而不是今晚", night.bedtime < at(2026, 11, 8, 2, 0),
          hhmm(night.bedtime))
    check("醒来时刻在未来", night.wake_at > at(2026, 11, 8, 2, 0),
          hhmm(night.wake_at))

    morning = L.LifeState()
    L.tick(morning, at(2026, 11, 8, 10, 0))
    check("上午 10 点冷启动 → 判为醒着", not morning.asleep)

    deep = L.LifeState()
    L.tick(deep, at(2026, 11, 8, 1, 30))
    check("凌晨 1:30 冷启动 → 判为睡着", deep.asleep,
          f"bed={hhmm(deep.bedtime)} wake={hhmm(deep.wake_at)}")

    # 23:50 的判定取决于当天抖出来的睡点，但必须自洽：
    # 说她睡着，就必须已经过了入睡点；说她醒着，就必须还没到。
    evening = L.LifeState()
    evening_now = at(2026, 11, 8, 23, 50)
    L.tick(evening, evening_now)
    consistent = (evening.asleep and evening.bedtime <= evening_now) or \
                 ((not evening.asleep) and evening.bedtime > evening_now)
    check("23:50 的判定和入睡点自洽", consistent,
          f"asleep={evening.asleep} bed={hhmm(evening.bedtime)}")


# ══════════════════════════════════════════════════════════════════════
# 睡与醒
# ══════════════════════════════════════════════════════════════════════

def test_fall_asleep() -> None:
    print("\n[入睡] 到点自己睡，不靠对方消息")
    state, _ = fresh(2026, 11, 7, 10)
    target = state.bedtime + 60
    events = L.tick(state, target)
    check("到入睡点就睡了", state.asleep,
          f"bedtime={hhmm(state.bedtime)} now={hhmm(target)} energy={state.energy:.0f}")
    check("入睡有事件", any(e.kind == "asleep" for e in events),
          str([e.kind for e in events]))
    check("睡着后不再说手头在干嘛",
          "手头在" not in L.render(state, target))


def test_never_sleeps_early() -> None:
    print("\n[反例] 一整天没人说话 ≠ 她中午就睡")
    state, now = fresh(2026, 11, 7, 9)
    t = now
    for _ in range(12):          # 9:00 → 21:00，全程无人说话
        t += 3600
        L.tick(state, t)
    check("21:00 她仍然醒着", not state.asleep,
          f"energy={state.energy:.0f} slot={state.doing_slot}")
    L.tick(state, state.bedtime + 60)
    check("到点之后才睡", state.asleep)


def test_wake() -> None:
    print("\n[醒来] 睡够了自己醒，精力回升")
    state, _ = fresh(2026, 11, 7, 10)
    L.tick(state, state.bedtime + 60)
    low = state.energy
    events = L.tick(state, state.wake_at + 60)
    check("到点醒了", not state.asleep)
    check("醒了有事件", any(e.kind == "woke" for e in events))
    check("醒来后精力高于睡前", state.energy > low, f"{low:.0f} → {state.energy:.0f}")
    check("醒来后精力在合理范围", 40 <= state.energy <= 100, f"{state.energy:.0f}")
    check("刚醒会被记住", state.just_woke_at > 0)


def test_debt() -> None:
    print("\n[代价] 熬夜要还，第二天起得更晚、精力更低")
    normal, _ = fresh(2026, 11, 7, 10)
    L.tick(normal, normal.bedtime + 60)
    normal_wake = normal.wake_at
    L.tick(normal, normal.wake_at + 60)
    normal_energy = normal.energy

    late, _ = fresh(2026, 11, 7, 10)
    L.tick(late, at(2026, 11, 8, 3, 0))       # 硬撑到凌晨 3 点
    check("熬夜最终会睡（不会永远醒着）", late.asleep,
          f"asleep={late.asleep} energy={late.energy:.0f}")
    check("熬夜记了睡眠债", late.sleep_debt > 0, f"debt={late.sleep_debt:.0f}")
    check("撑到 3 点精力明显下降", late.energy < 55, f"{late.energy:.0f}")

    L.tick(late, late.wake_at + 60)
    check("第二天醒来更晚", late.wake_at > normal_wake,
          f"正常 {hhmm(normal_wake)} / 熬夜 {hhmm(late.wake_at)}")
    check("第二天起床精力更低", late.energy < normal_energy,
          f"正常 {normal_energy:.0f} / 熬夜 {late.energy:.0f}")
    check("债衰减但没清零", 0 < late.sleep_debt < 240, f"{late.sleep_debt:.0f}")


def test_extend() -> None:
    print("\n[陪聊] 可以多撑一会儿，但有硬上限")
    state, _ = fresh(2026, 11, 7, 22)
    near = state.bedtime - 20 * 60
    L.tick(state, near)
    ok = L.try_extend(state, near)
    check("入睡前有人在聊 → 会延长", ok and state.extended_minutes > 0,
          f"extended={state.extended_minutes}")

    # 紧接着再来一条，不该续 —— 否则五句"在吗"就把一晚额度烧完
    check("刚延长过不会每条消息都续",
          not L.try_extend(state, near + 60),
          f"extended={state.extended_minutes}")

    t = near
    for _ in range(20):
        t += 10 * 60
        if not L.try_extend(state, t):
            break
    check("延长不会超过上限", state.extended_minutes <= L.EXTEND_CAP_MINUTES,
          f"{state.extended_minutes}")
    check("撞上限后不再延长", not L.try_extend(state, t + 3600))

    day_state, day_now = fresh(2026, 11, 7, 14)
    check("白天不触发延长", not L.try_extend(day_state, day_now))

    L.tick(state, state.bedtime + state.extended_minutes * 60 + 60)
    check("延长用完之后仍然会睡", state.asleep)


# ══════════════════════════════════════════════════════════════════════
# 精力
# ══════════════════════════════════════════════════════════════════════

def test_energy() -> None:
    print("\n[精力] 醒着掉、睡着回、聊天更累、超时更快")
    a, now = fresh(2026, 11, 7, 9)
    start = a.energy
    L.tick(a, now + 6 * 3600)
    check("醒 6 小时精力下降", a.energy < start, f"{start:.0f} → {a.energy:.0f}")
    drop_plain = start - a.energy

    b, now2 = fresh(2026, 11, 7, 9)
    start2 = b.energy
    L.tick(b, now2 + 6 * 3600, talk_turns=40)
    check("同样 6 小时，聊得多掉得更多", (start2 - b.energy) > drop_plain,
          f"聊天 {(start2 - b.energy):.1f} / 安静 {drop_plain:.1f}")

    c, _ = fresh(2026, 11, 7, 10)
    L.tick(c, c.bedtime + 60)
    before = c.energy
    L.tick(c, c.wake_at + 60)
    check("睡一觉之后精力回升", c.energy > before + 10, f"{before:.0f} → {c.energy:.0f}")

    d, now3 = fresh(2026, 11, 7, 22, 0)
    d.bedtime = now3 + 70 * 60          # 70 分钟后该睡
    before_overtime = d.energy
    L.tick(d, now3 + 100 * 60)          # 已经超时 30 分钟
    over = before_overtime - d.energy
    plain = L.ENERGY_AWAKE_DRAIN * (100 / 60)
    check("超时之后掉得更快", over > plain + 0.5,
          f"超时 {over:.1f} / 纯基础 {plain:.1f}")
    check("精力不会掉到下限以下", d.energy >= L.ENERGY_FLOOR - 0.01, f"{d.energy}")


def test_dozing_events() -> None:
    print("\n[困] 跨过阈值报一次，不会每 5 秒刷一条")
    state, now = fresh(2026, 11, 7, 22)
    state.energy = L.DOZING_THRESHOLD + 0.5
    e1 = L.tick(state, now + 3600)
    e2 = L.tick(state, now + 7200)
    check("掉下阈值会报 dozing", "dozing" in [e.kind for e in e1],
          str([e.kind for e in e1]))
    check("之后不再重复报", "dozing" not in [e.kind for e in e2],
          str([e.kind for e in e2]))


# ══════════════════════════════════════════════════════════════════════
# 手头的事
# ══════════════════════════════════════════════════════════════════════

def test_doing() -> None:
    print("\n[手头] 同一天同一段稳定，换了时段才换")
    a = L.LifeState()
    a.day = "2026-11-07"
    L.refresh_doing(a, at(2026, 11, 7, 15, 10))
    first = a.doing
    b = L.LifeState()
    b.day = "2026-11-07"
    L.refresh_doing(b, at(2026, 11, 7, 15, 50))
    check("同一段里两次算出来同一件事", first == b.doing,
          f"{first!r} vs {b.doing!r}")
    check("真的取到了一条具体的事（不是兜底）", first != "在忙自己的事", first)

    a.doing_slot = ""
    L.refresh_doing(a, at(2026, 11, 7, 7, 30))
    check("换时段会换事", a.doing != first, f"{first!r} → {a.doing!r}")

    c = L.LifeState()
    c.day = "2026-11-08"
    L.refresh_doing(c, at(2026, 11, 8, 15, 10))
    check("换天算得出来", bool(c.doing), c.doing)

    ok, labels = True, set()
    for h in range(24):
        label, items = L._slot_for(at(2026, 11, 7, h, 0))
        labels.add(label)
        if not items or label == "别的时候":
            ok = False
    check("24 个整点都有具体时段", ok, str(sorted(labels)))
    check("时段覆盖不少于 8 档", len(labels) >= 8, str(sorted(labels)))


def test_doing_missing() -> None:
    print("\n[手头] 没写到的就不说")
    state, now = fresh(2026, 11, 7, 10)
    state.doing = ""
    check("doing 为空时不出现「手头在」", "手头在" not in L.render(state, now))


# ══════════════════════════════════════════════════════════════════════
# 午睡
# ══════════════════════════════════════════════════════════════════════

def test_nap() -> None:
    print("\n[午睡] 只在下午、只一天一次")
    hit = 0
    base = datetime(2026, 11, 1, 8, 0)
    for d in range(40):
        ts = (base + timedelta(days=d)).timestamp()
        state = L.LifeState()
        L.plan_day(state, ts)
        state.asleep = False
        state.energy = 50.0
        state.last_tick = ts
        state.doing = "在发呆"
        state.doing_slot = "上午"
        t = ts
        for _ in range(7):
            t += 3600
            if any(e.kind == "nap_start" for e in L.tick(state, t)):
                hit += 1
                break
    check("40 天里出现过午睡（概率事件）", hit >= 1, f"命中 {hit} 次")
    check("午睡比例不夸张（≤ 25/40）", hit <= 25, f"{hit}/40")

    morning = L.LifeState()
    L.plan_day(morning, at(2026, 11, 7, 8))
    morning.asleep = False
    morning.energy = 40
    morning.last_tick = at(2026, 11, 7, 8)
    L.tick(morning, at(2026, 11, 7, 9, 0))
    check("上午不会午睡", not morning.asleep)


def test_nap_is_not_a_new_day() -> None:
    """午睡不是"新的一天" —— 这条是模拟跑一周时抓出来的真 bug。

    第一版午睡走的是主睡眠的醒来分支：plan_day → 推进 day、重排日程、
    衰减睡眠债。于是 13:00 睡 19 分钟、醒、债被还掉一截、精力还低 →
    13:25 又睡。一天在午睡里循环了八次，债从 208 一路还到 2。
    """
    print("\n[午睡] 不连环、不还债、不推进天")

    state = L.LifeState()
    L.plan_day(state, at(2026, 11, 7, 8))
    state.asleep = False
    state.energy = 45.0
    state.sleep_debt = 90.0
    state.last_tick = at(2026, 11, 7, 8)
    state.doing = "在发呆"
    state.doing_slot = "上午"

    day_before = state.day
    debt_before = state.sleep_debt
    bed_before = state.bedtime

    # 走到某天下午，如果触发午睡就记下来
    t = at(2026, 11, 7, 12, 30)
    started = False
    for _ in range(60):          # 12:30 → 17:30，5 分钟一步
        t += 5 * 60
        for ev in L.tick(state, t):
            if ev.kind == "nap_start":
                started = True
    if not started:
        # 那天没命中（18% 概率），换一天继续找
        for d in range(2, 30):
            state = L.LifeState()
            L.plan_day(state, at(2026, 11, d, 8))
            state.asleep = False
            state.energy = 45.0
            state.sleep_debt = 90.0
            state.last_tick = at(2026, 11, d, 8)
            day_before = state.day
            debt_before = state.sleep_debt
            bed_before = state.bedtime
            t = at(2026, 11, d, 12, 30)
            for _ in range(60):
                t += 5 * 60
                for ev in L.tick(state, t):
                    if ev.kind == "nap_start":
                        started = True
                        break
                if started:
                    break
            if started:
                break

    check("能在 29 天内找到一次午睡（否则这条测不到）", started)
    if not started:
        return

    # 午睡醒来
    energy_at_nap = state.energy
    t = state.wake_at + 60
    L.tick(state, t)
    nap_energy = state.energy
    check("午睡会醒", not state.asleep)
    check("午睡不推进「新的一天」", state.day == day_before,
          f"{day_before} → {state.day}")
    check("午睡不重排日程", state.bedtime == bed_before,
          f"{bed_before} → {state.bedtime}")
    check("午睡不还睡眠债", state.sleep_debt == debt_before,
          f"{debt_before} → {state.sleep_debt}")

    # 继续往前推 3 小时，不该再睡（今天已经午睡过）
    naps = 0
    for _ in range(36):
        t += 5 * 60
        for ev in L.tick(state, t):
            if ev.kind == "nap_start":
                naps += 1
    check("同一天不会连环午睡", naps == 0, f"又睡了 {naps} 次")
    check("午睡那一刻精力回升", nap_energy > energy_at_nap,
          f"{energy_at_nap:.0f} → {nap_energy:.0f}")


# ══════════════════════════════════════════════════════════════════════
# 睡着期间的消息
# ══════════════════════════════════════════════════════════════════════

def test_pending() -> None:
    print("\n[睡着时的消息] 记账、可查、取走即清")
    state, now = fresh(2026, 11, 7, 10)
    L.note_pending(state, "wxid_a", "睡了吗", now)
    L.note_pending(state, "wxid_a", "在吗", now + 60)

    item = L.peek_pending(state, "wxid_a")
    check("累计条数", bool(item) and item["count"] == 2, str(item))
    check("保留最后一条预览", bool(item) and item["preview"] == "在吗", str(item))

    brief = L.briefing(state, "wxid_a", now + 3600)
    check("能生成醒来用的素材", "2 条" in brief, brief)

    got = L.take_pending(state, "wxid_a")
    check("取走能拿到", got is not None)
    check("取走即清", L.peek_pending(state, "wxid_a") is None)
    check("没有 pending 时素材为空", L.briefing(state, "wxid_a", now) == "")

    # 过期清理
    L.note_pending(state, "wxid_c", "很久以前", now)
    dropped = L.expire_pending(state, now + 40 * 3600)
    check("超过 30 小时的 pending 会被清掉", dropped == 1, f"清了 {dropped}")
    check("清完就不再有", L.peek_pending(state, "wxid_c") is None)


# ══════════════════════════════════════════════════════════════════════
# 渲染
# ══════════════════════════════════════════════════════════════════════

def test_render() -> None:
    print("\n[渲染] 只说她状态里确实有的")
    state, now = fresh(2026, 11, 7, 15, 30)
    text = L.render(state, now)
    check("醒着时说明醒着", "我醒着" in text, text)
    check("带上时间", "15:30" in text, text)
    check("带上手头的事", "手头在" in text, text)
    check("不出现「对方」这类对话内容", "对方" not in text.lower())

    state.asleep = True
    state.asleep_since = at(2026, 11, 7, 23, 40)
    sleeping = L.render(state, at(2026, 11, 8, 2, 0))
    check("睡着时说睡了", "已经睡了" in sleeping, sleeping)
    check("睡着时不说手头在干嘛", "手头在" not in sleeping)

    state.asleep = False
    state.energy = 8
    check("强撑时会说自己撑不住",
          "耗完" in L.render(state, now) or "短一些" in L.render(state, now))

    state2, _ = fresh(2026, 11, 7, 10)
    state2.extended_minutes = 30
    check("延长中会说出来", "撑着" in L.render(state2, at(2026, 11, 7, 23, 50)))

    state3, _ = fresh(2026, 11, 7, 10)
    state3.sleep_debt = 90
    check("欠债会说出来", "睡得不太够" in L.render(state3, now))


def test_condition() -> None:
    print("\n[一句话] 给日志和界面用")
    state, now = fresh(2026, 11, 7, 10)
    check("醒着", L.condition(state).startswith("醒着"), L.condition(state))
    state.energy = 20
    check("有点困", "有点困" in L.condition(state), L.condition(state))
    state.energy = 8
    check("强撑", "强撑" in L.condition(state), L.condition(state))
    state.asleep = True
    state.asleep_since = now
    check("睡着", "睡着" in L.condition(state), L.condition(state))


# ══════════════════════════════════════════════════════════════════════
# 落盘
# ══════════════════════════════════════════════════════════════════════

def test_roundtrip() -> None:
    print("\n[落盘] 往返一致；坏文件不抛")
    old = os.environ.get("BIGFISH_DATA_ROOT")
    tmp = tempfile.mkdtemp(prefix="bigfish-life-")
    os.environ["BIGFISH_DATA_ROOT"] = tmp
    try:
        state = L.LifeState()
        L.plan_day(state, at(2026, 11, 7, 10))
        state.energy = 63.5
        state.doing = "在看书"
        state.doing_slot = "晚上"
        L.note_pending(state, "wxid_b", "晚安", at(2026, 11, 7, 23, 0))
        check("写盘成功", L.save(state, force=True, root=tmp))

        back = L.load(root=tmp)
        check("读回来 day 一致", back.day == state.day, f"{back.day}")
        check("读回来精力一致", abs(back.energy - 63.5) < 0.01, f"{back.energy}")
        check("读回来手头的事一致", back.doing == "在看书", back.doing)
        check("读回来 pending 一致", L.peek_pending(back, "wxid_b") is not None)

        with open(os.path.join(tmp, L.STATE_NAME), "w", encoding="utf-8") as f:
            f.write("{ 这不是 json")
        broken = L.load(root=tmp)
        check("坏 JSON 返回空状态而不是抛", broken.day == "" and not broken.asleep)

        with open(os.path.join(tmp, L.STATE_NAME), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "energy": "很高", "pending": "no",
                       "bedtime": "明天"}, f)
        weird = L.load(root=tmp)
        check("手改坏的字段被兜住",
              isinstance(weird.energy, float) and isinstance(weird.pending, dict)
              and isinstance(weird.bedtime, float),
              f"{type(weird.energy)} / {type(weird.pending)} / {type(weird.bedtime)}")

        # 无变化不重复写
        wrote = L.save(back, force=False, root=tmp)
        check("无变化时不重复写盘", wrote is False)
    finally:
        os.environ.pop("BIGFISH_DATA_ROOT", None)
        if old is not None:
            os.environ["BIGFISH_DATA_ROOT"] = old


def test_save_never_raises() -> None:
    print("\n[落盘] 数据根不可写时返回 False 而不是抛")
    state, _ = fresh(2026, 11, 7, 10)
    ok = L.save(state, force=True,
                root=os.path.join(tempfile.gettempdir(), "bigfish\0bad", "x"))
    check("非法路径不抛，返回 False", ok is False)


# ══════════════════════════════════════════════════════════════════════
# 主
# ══════════════════════════════════════════════════════════════════════

def test_fingerprint() -> None:
    """指纹漏字段 = 「改了但没写盘」。

    这个 bug 特别难查：内存里一切正常，一重启就回到旧值，
    而日志上看不出任何异常。所以每条"会变且值得写"的字段都要单独验一遍。
    """
    print("\n[指纹] 值得写盘的变化，指纹必须跟着变")
    st = L.LifeState()
    L.plan_day(st, at(2026, 11, 7, 8, 0))
    fp0 = L._fingerprint(st)

    L.note_pending(st, "chat_a", "你好", at(2026, 11, 7, 9, 0))
    fp1 = L._fingerprint(st)
    check("记一条 pending → 指纹变", fp1 != fp0)

    # ⚠ 关键：同一个会话**再记一条**，键没变，只有 count 变了。
    #   只取 `sorted(pending)`（键）的话这里指纹不变 → save 跳过 →
    #   count 永远停在 1，她醒来只会说"你找过我"，说不出"找了三回"。
    L.note_pending(st, "chat_a", "在吗", at(2026, 11, 7, 9, 5))
    fp2 = L._fingerprint(st)
    check("同一会话再记一条（count 变）→ 指纹也变", fp2 != fp1,
          "只取 pending 的键会让这里不变")

    st.extended_at = 12345.0
    fp3 = L._fingerprint(st)
    check("熬夜加时的时刻也进指纹", fp3 != fp2)

    st.sleep_debt = 30.0
    check("睡眠债也进指纹", L._fingerprint(st) != fp3)

    st2 = L.LifeState()
    L.plan_day(st2, at(2026, 11, 7, 8, 0))
    st2.pending = {"chat_a": {"count": 3, "first_at": 1.0,
                              "last_at": 2.0, "preview": "x"}}
    st3 = L.LifeState()
    L.plan_day(st3, at(2026, 11, 7, 8, 0))
    st3.pending = {"chat_a": {"count": 1, "first_at": 1.0,
                              "last_at": 2.0, "preview": "x"}}
    check("内容不同 → 指纹不同（不是只看键）",
          L._fingerprint(st2) != L._fingerprint(st3))


def main() -> int:
    print("=" * 46)
    print("生活状态自检（bigfish/life.py）")
    print("=" * 46)

    test_fingerprint()
    test_rhythm()
    test_day_advances_on_wake()
    test_cold_start()
    test_fall_asleep()
    test_never_sleeps_early()
    test_wake()
    test_debt()
    test_extend()
    test_energy()
    test_dozing_events()
    test_doing()
    test_doing_missing()
    test_nap()
    test_nap_is_not_a_new_day()
    test_pending()
    test_render()
    test_condition()
    test_roundtrip()
    test_save_never_raises()

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
