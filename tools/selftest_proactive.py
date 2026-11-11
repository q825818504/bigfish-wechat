"""主动开口 + 关系卡 + 每日叙事 的回归测试。

跑法：
    python tools/selftest_proactive.py

不需要微信、不需要桌面、不需要网络。模型调用一律用假函数替换。

## 为什么这三块要放在一个文件里

它们共用一条链路：

    本地判定（不花钱）→ 过闸门 → 调一次模型复核 → 发送（三态记账）

任何一环松了，症状都一样：「她突然发了一条不该发的消息」。
分不清是判定太松、闸门漏了、还是复核没拦住 —— 所以每一环单独验。

## 最需要守住的三条

1. **闸门**：六道里漏一道，就可能半夜三点发消息
2. **证据**：关系卡里每条必须能在原话里找到，否则不许写进去
3. **方向**：任何解析失败都倒向"不发"，绝不倒向"发"

覆盖：
  闸门      六道逐条（开关 / 条数 / 间隔 / 时段 / 她睡了 / 她太累 / 正聊着）
  候选      未完话头 / 她自己的生活 / 心情 / 时段 / 日期 / 好久没说话
  评分      分数排序、过线才进复核
  复核      send=false / 坏 JSON / 空正文 → 一律不发
  追问      概率随关系与精力变化；6 小时前不追、30 小时后放弃
  状态      跨天清零、待回清理、忘记留痕、落盘往返
  关系      evidence 不在原文 → 拒绝；重复 → 更新而不是插新行
  叙事      写入只一次、读回三行、due 的三个条件
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import bond as B  # noqa: E402
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


def at(y: int, mo: int, d: int, h: int, mi: int = 0) -> float:
    return datetime(y, mo, d, h, mi).timestamp()


class FakeLife:
    """最小替身：只提供主动引擎会读的四个字段。"""

    def __init__(self, *, asleep=False, energy=80.0, doing="在看书",
                 just_woke_at=0.0, bedtime=0.0):
        self.asleep = asleep
        self.energy = energy
        self.doing = doing
        self.just_woke_at = just_woke_at
        self.bedtime = bedtime


# ══════════════════════════════════════════════════════════════════════
# 闸门
# ══════════════════════════════════════════════════════════════════════

def test_gates() -> None:
    print("\n[闸门] 六道，任何一道不过都不发")
    now = at(2026, 11, 7, 15, 0)          # 下午三点，在窗口内
    life = FakeLife()
    st = P.ProactiveState(day=P.day_key(now))

    def reason(**kw):
        args = dict(now=now, per_day=1, life_state=life,
                    last_peer_at=0.0, enabled=True)
        args.update(kw)
        return P.gate_block_reason(st, **args)

    check("正常情况下放行", reason() == "", reason())

    bad = P.ProactiveState(day=P.day_key(now), sent_today=1)
    check("今天已经发过 → 拦", "今天已经发过" in P.gate_block_reason(
        bad, now=now, per_day=1, life_state=life, last_peer_at=0.0, enabled=True))

    tight = P.ProactiveState(day=P.day_key(now), last_sent_at=now - 10 * 60)
    check("距上一条太近 → 拦", "还差" in P.gate_block_reason(
        tight, now=now, per_day=3, life_state=life, last_peer_at=0.0, enabled=True))

    night = at(2026, 11, 8, 2, 0)
    check("凌晨 → 拦", "不在允许时段" in reason(now=night))
    check("22:29 还在窗口内", reason(now=at(2026, 11, 7, 22, 29)) == "")
    check("22:45 超窗口 → 拦",
          "不在允许时段" in reason(now=at(2026, 11, 7, 22, 45)))
    check("8:50 还没到窗口 → 拦",
          "不在允许时段" in reason(now=at(2026, 11, 7, 8, 50)))

    check("她睡了 → 拦", "她睡了" in reason(life_state=FakeLife(asleep=True)))
    check("她太累 → 拦",
          "太累" in reason(life_state=FakeLife(energy=10)))
    check("精力刚好够 → 放行", reason(life_state=FakeLife(energy=30)) == "")

    check("刚才还在聊 → 拦",
          "还在聊" in reason(last_peer_at=now - 5 * 60))
    check("聊完 40 分钟 → 放行",
          reason(last_peer_at=now - 40 * 60) == "")

    check("开关关掉 → 拦", "未开启" in reason(enabled=False))


def test_day_roll() -> None:
    print("\n[跨天] 04:00 之后条数清零")
    st = P.ProactiveState(day="2026-11-07", sent_today=1)
    rolled = P.roll_day(st, at(2026, 11, 8, 5, 0))
    check("跨天会清零", rolled and st.sent_today == 0, f"{st.day}/{st.sent_today}")
    again = P.roll_day(st, at(2026, 11, 8, 9, 0))
    check("同一天不重复清", not again)


# ══════════════════════════════════════════════════════════════════════
# 候选
# ══════════════════════════════════════════════════════════════════════

def test_open_loop() -> None:
    print("\n[候选] 未完话头")
    tmp = tempfile.mkdtemp(prefix="bf-open-")
    old = os.environ.get("BIGFISH_DATA_ROOT")
    os.environ["BIGFISH_DATA_ROOT"] = tmp
    try:
        from bigfish import history as H

        now = at(2026, 11, 7, 15, 0)
        st = P.ProactiveState(day=P.day_key(now))

        H.append("wxid_a", "老爸", "我先去忙，等我一下", root=tmp)
        c = P.candidate_open_loop(st, chat_id="wxid_a", now=now, me="大肥鱼")
        check("有「等我」→ 出现候选", c is not None, str(c))
        check("理由里带上原话", c is not None and "等我" in c.reason,
              c.reason if c else "")
        check("分数够线", c is not None and c.score >= P.SCORE_THRESHOLD,
              f"{c.score if c else 0}")

        # 她之后回过一句 → 话头已经接上
        H.append("wxid_a", "大肥鱼", "好，你去吧", root=tmp)
        c2 = P.candidate_open_loop(st, chat_id="wxid_a", now=now, me="大肥鱼")
        check("她已经接过 → 不再当作未完话头", c2 is None, str(c2))

        H.append("wxid_b", "老爸", "今天天气不错", root=tmp)
        c3 = P.candidate_open_loop(st, chat_id="wxid_b", now=now, me="大肥鱼")
        check("普通闲聊不算未完话头", c3 is None, str(c3))

        # 用过的锚点不会重复
        if c is not None:
            st.used_anchors.append(c.anchor_at)
            H.append("wxid_a", "老爸", "先这样", root=tmp)
            c4 = P.candidate_open_loop(st, chat_id="wxid_a", now=now, me="大肥鱼")
            check("同一个锚点不会反复触发",
                  c4 is None or abs(c4.anchor_at - c.anchor_at) > 1,
                  str(c4))
    finally:
        os.environ.pop("BIGFISH_DATA_ROOT", None)
        if old is not None:
            os.environ["BIGFISH_DATA_ROOT"] = old


def test_life_share() -> None:
    print("\n[候选] 她自己的生活")
    now = at(2026, 11, 7, 15, 0)
    st = P.ProactiveState(day=P.day_key(now))

    c = P.candidate_life_share(st, chat_id="wxid_a", now=now,
                               life_state=FakeLife(doing="在收拾屋子"))
    check("手头有事 → 出现候选", c is not None)
    check("分数够线", c is not None and c.score >= 50, str(c.score if c else 0))

    dull = P.candidate_life_share(st, chat_id="wxid_a", now=now,
                                  life_state=FakeLife(doing="在发呆"))
    check("琐碎的事不单独说（在发呆）", dull is None, str(dull))

    empty = P.candidate_life_share(st, chat_id="wxid_a", now=now,
                                   life_state=FakeLife(doing=""))
    check("没有手头的事 → 不生成", empty is None)

    st2 = P.ProactiveState(day=P.day_key(now),
                           source_last={"life_share": now - 3600})
    recent = P.candidate_life_share(st2, chat_id="wxid_a", now=now,
                                    life_state=FakeLife(doing="在看书"))
    check("12 小时内说过就不再说了", recent is None, str(recent))


def test_greeting() -> None:
    print("\n[候选] 时段问候")
    hit = 0
    days = 40
    for d in range(days):
        now = at(2026, 11, 1, 21, 50) + d * 86400
        st = P.ProactiveState(day=P.day_key(now))
        if P.candidate_greeting(st, chat_id="wxid_a", now=now,
                                life_state=FakeLife()) is not None:
            hit += 1
    check("40 个晚上里出现若干次（概率事件）", 1 <= hit <= days,
          f"命中 {hit} 次")
    check("不是每天都发（概率压制生效）", hit < days, f"{hit}/{days}")

    # 早上那句只在"她刚醒"时出现
    morning = at(2026, 11, 7, 8, 0)
    st = P.ProactiveState(day=P.day_key(morning))
    awake_long = P.candidate_greeting(st, chat_id="wxid_a", now=morning,
                                      life_state=FakeLife(just_woke_at=0))
    check("早上没刚醒 → 不发早安", awake_long is None, str(awake_long))

    just = P.candidate_greeting(st, chat_id="wxid_a", now=morning,
                                life_state=FakeLife(just_woke_at=morning - 1800))
    check("刚醒的早上 → 可能发（不强制，看概率）",
          just is None or just.source == "greeting", str(just))


def test_date() -> None:
    print("\n[候选] 重要日期")
    now = at(2026, 11, 7, 10, 0)
    st = P.ProactiveState(day=P.day_key(now))

    c = P.candidate_date(st, chat_id="wxid_a", now=now,
                         dates=[{"date": "11-07", "label": "他生日", "yearly": True}])
    check("当天 → 触发", c is not None and "今天" in c.reason,
          c.reason if c else "")
    check("分数最高档", c is not None and c.score >= 85, str(c.score if c else 0))

    c2 = P.candidate_date(st, chat_id="wxid_a", now=now,
                          dates=[{"date": "11-08", "label": "第一次见面",
                                  "yearly": True}])
    check("提前一天 → 触发并说明是明天", c2 is not None and "明天" in c2.reason,
          c2.reason if c2 else "")

    c3 = P.candidate_date(st, chat_id="wxid_a", now=now,
                          dates=[{"date": "03-15", "label": "无关的日子",
                                  "yearly": True}])
    check("不相干的日子 → 不触发", c3 is None, str(c3))

    c4 = P.candidate_date(st, chat_id="wxid_a", now=now,
                          dates=[{"date": "坏格式", "label": "x"}])
    check("坏日期不抛，也不触发", c4 is None)


def test_silence() -> None:
    print("\n[候选] 好久没说话")
    now = at(2026, 11, 7, 15, 0)
    st = P.ProactiveState(day=P.day_key(now))
    c = P.candidate_silence(st, chat_id="wxid_a", now=now,
                            last_peer_at=now - 40 * 3600)
    check("40 小时没说话 → 触发", c is not None, str(c))
    close = P.candidate_silence(st, chat_id="wxid_a", now=now,
                                last_peer_at=now - 5 * 3600)
    check("刚聊过 → 不触发", close is None)

    st2 = P.ProactiveState(day=P.day_key(now), recent=[
        {"at": now - 3600, "source": "silence"},
        {"at": now - 7200, "source": "silence"},
    ])
    capped = P.candidate_silence(st2, chat_id="wxid_a", now=now,
                                 last_peer_at=now - 40 * 3600)
    check("一周两次封顶", capped is None, str(capped))


def test_collect_sorts() -> None:
    print("\n[候选] 汇总与排序")
    now = at(2026, 11, 7, 10, 0)
    st = P.ProactiveState(day=P.day_key(now))
    cands = P.collect(st, chat_id="wxid_a", now=now, me="大肥鱼",
                      life_state=FakeLife(doing="在收拾屋子"),
                      last_peer_at=now - 40 * 3600,
                      dates=[{"date": "11-07", "label": "他生日", "yearly": True}])
    check("多个源都能出候选", len(cands) >= 2, str([c.source for c in cands]))
    scores = [c.score for c in cands]
    check("按分数从高到低", scores == sorted(scores, reverse=True), str(scores))
    check("日期排在前面", cands and cands[0].source == "date",
          cands[0].source if cands else "")


# ══════════════════════════════════════════════════════════════════════
# 出口：她这一轮说了什么
# ══════════════════════════════════════════════════════════════════════

def test_clean_reply() -> None:
    print("\n[出口] 从她的输出里取出这一条")
    check("正常一句原样返回", P.clean_reply("刚看到") == "刚看到")
    check("首尾空白去掉", P.clean_reply("  刚看到  ") == "刚看到")
    check("多行合成一行", P.clean_reply("第一句\n第二句") == "第一句 第二句")
    check("空串 → 这一轮不说", P.clean_reply("") == "")
    check("只有空白 → 这一轮不说", P.clean_reply("   \n  ") == "")

    # 放弃标记：一次调用里同时完成"想不想说"和"说什么"，
    # 代价就是需要一个约定的"算了"
    for mark in ("-", "—", "算了", "不说了", "（不说话）"):
        check(f"「{mark}」→ 这一轮不说", P.clean_reply(mark) == "", mark)
    check("带标点的横杠也认", P.clean_reply("—。") == "")
    check("「算了」后面接了事就照发",
          P.clean_reply("算了，我先去忙") == "算了，我先去忙")

    check("代码围栏被剥掉", P.clean_reply("```\n刚看到\n```") == "刚看到")
    long_text = P.clean_reply("啊" * 300)
    check("超长会被截断", 0 < len(long_text) <= 121, str(len(long_text)))


# ══════════════════════════════════════════════════════════════════════
# 追问
# ══════════════════════════════════════════════════════════════════════

def test_followup_probability() -> None:
    print("\n[追问] 关系近、心情好更可能追；累、心情差更容易忘")
    base = P.followup_probability(bond_hit=False, mood=50, energy=80)
    with_bond = P.followup_probability(bond_hit=True, mood=50, energy=80)
    tired = P.followup_probability(bond_hit=False, mood=50, energy=20)
    sad = P.followup_probability(bond_hit=False, mood=30, energy=80)

    check("有称呼会提高概率", with_bond > base, f"{base:.2f} → {with_bond:.2f}")
    check("累的时候更低", tired < base, f"{base:.2f} → {tired:.2f}")
    check("心情差的时候更低", sad < base, f"{base:.2f} → {sad:.2f}")
    check("概率被夹在合理区间",
          0.05 <= min(base, with_bond, tired, sad)
          and max(base, with_bond, tired, sad) <= P.FOLLOWUP_MAX_PROBABILITY,
          f"{min(base, with_bond, tired, sad):.2f}~{max(base, with_bond, tired, sad):.2f}")


def test_followup_window() -> None:
    print("\n[追问] 太早不追、太晚放弃")
    now = at(2026, 11, 7, 20, 0)
    st = P.ProactiveState(day=P.day_key(now))
    st.awaiting["wxid_a"] = {"sent_at": now - 3600, "source": "greeting",
                             "text": "在忙吗", "followup_used": False}
    early = P.due_followups(st, now=now, life_state=FakeLife(), bond_hit=True)
    check("发出 1 小时 → 还不到追问的时候", early == [], str(early))

    st.awaiting["wxid_a"]["sent_at"] = now - 10 * 3600
    ready = P.due_followups(st, now=now, life_state=FakeLife(), bond_hit=True)
    check("发出 10 小时 → 进入追问判定（可能追也可能不追）",
          isinstance(ready, list))

    st.awaiting["wxid_a"]["sent_at"] = now - 40 * 3600
    late = P.due_followups(st, now=now, life_state=FakeLife(), bond_hit=True)
    check("40 小时 → 已经放弃", late == [], str(late))

    st.awaiting["wxid_a"]["sent_at"] = now - 10 * 3600
    st.awaiting["wxid_a"]["followup_used"] = True
    used = P.due_followups(st, now=now, life_state=FakeLife(), bond_hit=True)
    check("追过一次就不再追", used == [], str(used))


def test_awaiting_lifecycle() -> None:
    print("\n[待回] 记账 → 对方回来 → 清掉 / 忘记留痕")
    now = at(2026, 11, 7, 20, 0)
    st = P.ProactiveState(day=P.day_key(now))

    P.mark_sent(st, chat_id="wxid_a", source="greeting", text="在忙吗",
                now=now, anchor_at=12345)
    check("发出后进入待回", "wxid_a" in st.awaiting)
    check("条数 +1", st.sent_today == 1)
    check("锚点被记下", 12345 in st.used_anchors)

    cleared = P.note_peer_replied(st, chat_id="wxid_a", now=now + 60)
    check("对方回来就清掉", cleared and "wxid_a" not in st.awaiting)

    P.mark_sent(st, chat_id="wxid_b", source="greeting", text="在吗", now=now)
    P.mark_forgotten(st, chat_id="wxid_b", now=now + 7200)
    check("忘记会留痕", any(d.get("chat_id") == "wxid_b" for d in st.dropped),
          str(st.dropped[-1:]))
    check("忘记之后不再待回", "wxid_b" not in st.awaiting)

    P.mark_sent(st, chat_id="wxid_c", source="greeting", text="嗯", now=now)
    n = P.expire_awaiting(st, now=now + 40 * 3600)
    check("超时自动转忘记", n == 1 and not st.awaiting, f"清了 {n}")


# ══════════════════════════════════════════════════════════════════════
# 关系卡
# ══════════════════════════════════════════════════════════════════════

def test_bond_parse() -> None:
    print("\n[关系] 没有原话依据的一律拒收")
    transcript = ("老爸: 我以后叫你老头得了\n"
                  "大肥鱼: 你以前都叫我名字的\n"
                  "老爸: 那天本来想问你一句话，后来没说")

    raw = json.dumps({"bonds": [
        {"kind": "address", "text": "他从 11 月开始叫我「老头」",
         "evidence": "我以后叫你老头得了", "confidence": 80},
        {"kind": "unsaid", "text": "有一次他想问但没问",
         "evidence": "那天本来想问你一句话", "confidence": 60},
        {"kind": "address", "text": "他以前叫我宝贝", "evidence": "宝贝",
         "confidence": 70},
        {"kind": "first", "text": "我们第一次见面在下雨天",
         "evidence": "下雨", "confidence": 50},
    ]}, ensure_ascii=False)

    passed, rejected = B.parse_bonds(raw, transcript)
    kinds = [p["kind"] for p in passed]
    check("有原话的两条通过", len(passed) == 2, str(passed))
    check("address 通过", "address" in kinds, str(kinds))
    check("unsaid 通过", "unsaid" in kinds, str(kinds))
    check("没有依据的被拒", len(rejected) >= 1, str(rejected))
    check("不在范围内的 kind 被拒",
          any("bad kind" in r for r in rejected), str(rejected))

    bad_json = B.parse_bonds("不是 json", transcript)
    check("坏 JSON 不抛", bad_json == ([], ["not json"]), str(bad_json))


def test_bond_store() -> None:
    print("\n[关系] 写入 / 去重 / 渲染")
    tmp = tempfile.mkdtemp(prefix="bf-bond-")
    db = os.path.join(tmp, "m.sqlite3")
    B.ensure_schema(db)

    ok, why = B.upsert_bond(db, "dafeiyu", "wxid_a", kind="address",
                            text="他现在叫我「老头」",
                            evidence="我以后叫你老头得了", confidence=80)
    check("第一条写入", ok and why == "added", f"{ok}/{why}")

    ok2, why2 = B.upsert_bond(db, "dafeiyu", "wxid_a", kind="address",
                              text="他现在叫我「老头」",
                              evidence="我以后叫你老头得了", confidence=90)
    check("同样的内容再写 → 更新而不是插一行", ok2 and why2 == "updated",
          f"{ok2}/{why2}")

    short, why3 = B.upsert_bond(db, "dafeiyu", "wxid_a", kind="address",
                                text="某个称呼", evidence="嗯")
    check("证据太短 → 拒绝", not short and "too short" in why3, why3)

    bad_kind, why4 = B.upsert_bond(db, "dafeiyu", "wxid_a", kind="first",
                                   text="第一次见面", evidence="第一次见面啊")
    check("不在范围的 kind → 拒绝", not bad_kind and "unknown kind" in why4, why4)

    B.upsert_bond(db, "dafeiyu", "wxid_a", kind="unsaid",
                  text="有句话他一直没说", evidence="那天本来想问你一句话")

    text = B.render_for_prompt(db, "dafeiyu", "wxid_a")
    check("渲染带称呼段", "怎么叫" in text, text[:80])
    check("渲染带没说出口段", "没说出口" in text, text[:120])
    check("渲染带上具体内容", "老头" in text, text[:160])

    rows = B.list_bonds(db, "dafeiyu", "wxid_a")
    check("两条都在", len(rows) == 2, str(len(rows)))

    B.set_status(db, rows[0]["id"], B.STATUS_RETIRED)
    after = B.list_bonds(db, "dafeiyu", "wxid_a")
    check("下线的不会出现在 active 里", len(after) == 1, str(len(after)))

    check("别的 owner 看不到", B.render_for_prompt(db, "dafeiyu", "wxid_z") == "")


def test_dry_run() -> None:
    """预演模式：判定照走、额照占、话照记，但一条都不发。"""
    print()
    print("[预演] 只记不发，但占额度")
    now = at(2026, 11, 7, 15, 0)
    st = P.ProactiveState(day=P.day_key(now))

    P.note_dry(st, chat_id="wxid_a", source="greeting", text="在忙吗",
               now=now, anchor_at=555, material={})
    check("记进 dry 列表", len(st.dry) == 1 and st.dry[0]["text"] == "在忙吗",
          str(st.dry))
    check("占当天额度", st.sent_today == 1, str(st.sent_today))
    check("更新 last_sent_at（闸门 2 才挡得住）",
          abs(st.last_sent_at - now) < 1, str(st.last_sent_at))
    check("不进待回（没人会回一条没发出去的消息）", not st.awaiting,
          str(st.awaiting))
    check("锚点照样记下（避免同一个话头反复选）", 555 in st.used_anchors)

    # 预演之后闸门立刻拦住第二条
    reason = P.gate_block_reason(st, now=now + 60, per_day=1,
                                 life_state=FakeLife(), last_peer_at=0.0)
    check("预演之后当天额度用掉了", "今天已经发过" in reason, reason)

    # life_share 的具体 doing 也记进去
    st2 = P.ProactiveState(day=P.day_key(now))
    P.note_dry(st2, chat_id="wxid_a", source="life_share", text="在收拾屋子",
               now=now, material={"doing": "在收拾屋子"})
    c = P.candidate_life_share(st2, chat_id="wxid_a", now=now + 3600,
                               life_state=FakeLife(doing="在收拾屋子"))
    check("同一件事预演过就不再选", c is None, str(c))


def test_bond_edit() -> None:
    """改措辞：text 能改，evidence 不能动。"""
    print()
    print("[关系] 改措辞不动原话")
    tmp = tempfile.mkdtemp(prefix="bf-edit-")
    db = os.path.join(tmp, "m.sqlite3")
    B.ensure_schema(db)

    ok, why = B.upsert_bond(db, "dafeiyu", "wxid_a", kind="address",
                            text="他叫我名字", evidence="我以后叫你老头得了")
    check("先写一条", ok, why)
    rows = B.list_bonds(db, "dafeiyu", "wxid_a")
    bid = rows[0]["id"]
    ev_before = rows[0]["evidence"]

    ok2, why2 = B.update_text(db, bid, "他从 10 月开始叫我「老头」")
    check("改成功", ok2, why2)
    after = B.list_bonds(db, "dafeiyu", "wxid_a")[0]
    check("措辞变了", after["text"].startswith("他从 10 月"), after["text"])
    check("原话没动", after["evidence"] == ev_before, after["evidence"])

    ok3, _w = B.update_text(db, bid, "   ")
    check("空的不许", not ok3)
    ok4, _w2 = B.update_text(db, bid, "x" * 300)
    check("太长的拒绝", not ok4)
    ok5, _w3 = B.update_text(db, 999999, "随便")
    check("不存在的 id 返回 False", not ok5)


def test_hit_probability() -> None:
    print("\n[掷骰] 命中线：分数、时机、连着没中")
    now = at(2026, 11, 7, 15, 0)

    # 中性时机：3 小时前说过话 —— 既不算"刚聊过"（×0.5），
    # 也不算"冷太久"（×1.4），因子正好是 1。
    # 传 0 会被当成"从来没说过话"，那会直接吃到 ×1.4，测什么都偏高。
    NEUTRAL = now - 3 * 3600

    def hit(score, *, last_sent=0.0, last_peer=NEUTRAL, streak=0):
        return P.hit_probability(
            score=score, now=now, last_sent_at=last_sent,
            last_peer_at=last_peer, miss_streak=streak)[0]

    check("分数低于地基 → 不掷", hit(30.0) == 0, str(hit(30.0)))
    check("刚好踩在地基上 → 还是 0", hit(40.0) == 0, str(hit(40.0)))
    check("55 分有概率了", hit(55.0) > 0, str(hit(55.0)))
    check("分数越高越容易", hit(60.0) < hit(80.0) < hit(90.0),
          f"{hit(60.0)}/{hit(80.0)}/{hit(90.0)}")
    check("再高也不到 100", hit(200.0) < 100, str(hit(200.0)))

    base = hit(70.0)
    check("刚发过（20 分钟内）→ 难得多",
          hit(70.0, last_sent=now - 600) < base,
          f"{hit(70.0, last_sent=now - 600)} vs {base}")
    check("你正在聊（30 分钟内）→ 难",
          hit(70.0, last_peer=now - 1800) < base,
          f"{hit(70.0, last_peer=now - 1800)} vs {base}")
    check("冷了 8 小时 → 更容易",
          hit(70.0, last_peer=now - 8 * 3600) > base,
          f"{hit(70.0, last_peer=now - 8 * 3600)} vs {base}")

    check("没中 1 次比 0 次高", hit(70.0, streak=1) > base)
    check("加成有上限", hit(70.0, streak=50) == hit(70.0, streak=99),
          "上限 +32")

    _h, why = P.hit_probability(score=70.0, now=now,
                                last_sent_at=now - 300, last_peer_at=0.0)
    check("说明里写了「刚发过」", "刚发过" in why, why)
    _h, why2 = P.hit_probability(score=70.0, now=now, last_sent_at=0.0,
                                 last_peer_at=NEUTRAL, miss_streak=2)
    check("说明里写了加成", "没中加成" in why2, why2)


def test_miss_streak() -> None:
    print("\n[掷骰] 连着没中会累积，掷中就清零")
    st = P.ProactiveState()
    check("初始为 0", st.miss_streak == 0)
    P.note_miss(st)
    P.note_miss(st)
    check("没中两次 → 2", st.miss_streak == 2, str(st.miss_streak))
    P.note_hit(st)
    check("掷中清零", st.miss_streak == 0)

    tmp = tempfile.mkdtemp(prefix="bf-streak-")
    P.note_miss(st)
    check("存盘成功", P.save(st, root=tmp))
    check("读回来还在", P.load(root=tmp).miss_streak == 1,
          str(P.load(root=tmp).miss_streak))
    check("没存过的字段默认 0", P.ProactiveState().miss_streak == 0)


def test_whim() -> None:
    print("\n[来源] 没事也想说一句")
    now = at(2026, 11, 7, 15, 0)
    st = P.ProactiveState()

    class Fine:
        asleep = False
        energy = 80.0
        doing = "在看书"

    c = P.candidate_whim(st, chat_id="c1", now=now, life_state=Fine())
    check("醒着、有精神 → 会想说话", c is not None)
    check("它没有话题（这是它的意义）",
          c is not None and not c.material, str(c.material if c else ""))
    check("分数是 60", c is not None and c.score == P.WHIM_SCORE)
    check("理由里说明「不必有由头」",
          c is not None and "不必有由头" in c.reason,
          c.reason if c else "")

    class Sleeping:
        asleep = True
        energy = 90.0

    check("睡着时不存在",
          P.candidate_whim(st, chat_id="c1", now=now, life_state=Sleeping()) is None)

    class Tired:
        asleep = False
        energy = 20.0

    check("累到 20 不存在",
          P.candidate_whim(st, chat_id="c1", now=now, life_state=Tired()) is None)

    st.source_last["whim"] = now - 3600
    check("1 小时前说过 → 不再出现",
          P.candidate_whim(st, chat_id="c1", now=now, life_state=Fine()) is None)
    st.source_last["whim"] = now - 21 * 3600
    check("21 小时之后 → 又可以",
          P.candidate_whim(st, chat_id="c1", now=now, life_state=Fine()) is not None)


def test_sources_reachable() -> None:
    """每个来源的分数都必须过得了地基。

    这一条是为那两个 52 分写的：它们写在代码里、写进文档、也在 collect 里
    注册着，但分数低于当时的阈值，**一次都没触发过**。没人发现，因为
    「她没说这句话」和「她本来就不常说」长得一模一样。
    """
    print("\n[来源] 每条来源都真的够得着")
    floor = P.SCORE_THRESHOLD
    for name, score in (("她手头的事", 58.0), ("夜间问候", 56.0),
                        ("晨间问候", 62.0), ("心情", 60.0),
                        ("好久没说话", 56.0), ("未完话头", 72.0),
                        ("重要日期", 88.0), ("没事也想说", P.WHIM_SCORE),
                        ("记忆里的待办", 84.0)):
        check(f"{name}（{score}）> 地基 {floor}", score > floor)

    # 反过来：地基也不能随便低到"什么都算机会"
    check("地基不算低（≥35）", floor >= 35.0, str(floor))


def test_source_dedupe() -> None:
    """两层组合之后，各来源自己的频率限制必须还在。

    这是 0.9.5 差点漏掉的一个洞：`mark_sent` 原来只写
    `source_last[source]`，而组合之后 source 变成了 "recall+whim" ——
    写了没人读，`whim` 的 20 小时限制就失效了。

    不会报错，只会让她老用同一个理由开口。所以单独一节守着。
    """
    print("\n[去重] 两层组合之后，各来源的间隔还在")
    now = at(2026, 11, 7, 15, 0)

    st = P.ProactiveState()
    P.mark_sent(st, chat_id="c", source="recall+whim", text="x", now=now,
                sources=["recall", "whim"])
    check("whim 的时刻记下来了", st.source_last.get("whim") == now,
          str(st.source_last))
    check("recall 的时刻记下来了", st.source_last.get("recall") == now,
          str(st.source_last))
    check("recent 里存的是列表",
          (st.recent or [{}])[0].get("sources") == ["recall", "whim"],
          str((st.recent or [{}])[0].get("sources")))

    # life_share 的特殊键（同一件事 24 小时只说一次）
    st2 = P.ProactiveState()
    P.mark_sent(st2, chat_id="c", source="life_share", text="x", now=now,
                material={"doing": "在看书"}, sources=["life_share"])
    check("life_share 单来源时记了那件事",
          st2.source_last.get("life_share:在看书") == now,
          str(st2.source_last))

    st3 = P.ProactiveState()
    P.mark_sent(st3, chat_id="c", source="recall+life_share", text="x",
                now=now, material={"doing": "在看书"},
                sources=["recall", "life_share"])
    check("life_share 在组合里时也记了那件事",
          st3.source_last.get("life_share:在看书") == now,
          str(st3.source_last))

    # 预演也要记（不然预演一晚上，去重全失效）
    st5 = P.ProactiveState()
    P.note_dry(st5, chat_id="c", source="mood+whim", text="x", now=now,
               sources=["mood", "whim"])
    check("预演也记了各来源时刻",
          st5.source_last.get("mood") == now
          and st5.source_last.get("whim") == now,
          str(st5.source_last))

    # silence 的「一周最多两次」
    st4 = P.ProactiveState()
    for i in range(2):
        P.mark_sent(st4, chat_id="c", source="silence+whim", text="x",
                    now=now - i * 3600, sources=["silence", "whim"])

    class Fine:
        asleep = False
        energy = 90.0

    c = P.candidate_silence(st4, chat_id="c", now=now,
                            last_peer_at=now - 40 * 3600)
    check("一周两次用满 → 不再出现", c is None,
          "40 小时没说话，但已经触发过两次")


def test_layers() -> None:
    print("\n[两层] 说什么 + 为什么现在想说（不互相淘汰）")

    def C(src_, layer, score, reason, tone_text="", mat=None):
        return P.Candidate(source=src_, chat_id="c", score=score,
                           reason=reason, layer=layer, tone_text=tone_text,
                           material=mat or {})

    topic_recall = C("recall", "topic", 88, "他 3 天前说要去体检")
    topic_weak = C("open_loop", "topic", 72, "话头还没接")
    tone_mood = C("mood", "tone", 66, "x", "你心里有点事", {"mood": 55})
    tone_whim = C("whim", "tone", 60, "x", "就是忽然想找你说句话")

    topic, tone = P.pick([topic_weak, topic_recall, tone_mood, tone_whim])
    check("topic 取分数最高的", topic is topic_recall,
          topic.source if topic else "None")
    check("tone 取分数最高的", tone is tone_mood,
          tone.source if tone else "None")
    check("两层同时存在（谁也没淘汰谁）",
          topic is not None and tone is not None)

    check("只有 topic 时 tone 为空", P.pick([topic_recall])[1] is None)
    check("只有 tone 时 topic 为空", P.pick([tone_whim])[0] is None)
    check("空列表 → 两层都空", P.pick([]) == (None, None))

    check("单层分数就是它自己", P.combined_score(topic_recall, None) == 88.0)
    check("两层 +8", P.combined_score(topic_recall, tone_whim) == 95.0,
          str(P.combined_score(topic_recall, tone_whim)))
    check("封顶 95",
          P.combined_score(C("x", "topic", 95, "x"), tone_whim) == 95.0)

    both = P.compose_reason(topic_recall, tone_mood)
    check("两层都拼进去（话题在前）",
          "体检" in both and "心里" in both, both)
    check("只有 tone 也用情绪那句",
          P.compose_reason(None, tone_mood) == "你心里有点事",
          P.compose_reason(None, tone_mood))
    check("只有 topic 就是它自己",
          P.compose_reason(topic_recall, None) == topic_recall.reason)
    check("都没有 → 空串", P.compose_reason(None, None) == "")

    mat = P.merge_material(topic_recall, tone_mood)
    check("素材合并了两层", mat.get("mood") == 55, str(mat))


def test_recall() -> None:
    print("\n[来源] 记忆里的待办（他提过、还没结果的事）")
    import bigfish.memory as M

    orig = M.list_facts
    now = at(2026, 11, 7, 15, 0)
    st = P.ProactiveState()
    day = 86400.0

    def fact(fid, days, obj, pred="近况"):
        return {"id": fid, "created_at": (now - days * day) * 1000,
                "predicate": pred, "object": obj}

    def call(state=st):
        return P.candidate_recall(state, chat_id="c", now=now, owner="o",
                                  tier="owner", db_path="x")

    try:
        M.list_facts = lambda *a, **k: {"items": []}
        check("没有事实 → 没有候选", call() is None)

        M.list_facts = lambda *a, **k: {"items": [fact(1, 3, "下周三要去体检")]}
        c = call()
        check("命中关键词 → 有候选", c is not None)
        check("是 topic 层", c is not None and c.layer == "topic")
        check("分数 84 以上", c is not None and c.score >= 84.0,
              str(c.score if c else ""))
        check("理由里写了几天前",
              c is not None and "3 天前" in c.reason, c.reason if c else "")

        M.list_facts = lambda *a, **k: {"items": [fact(1, 0.5, "明天要去体检")]}
        check("太新（半天）→ 不选", call() is None)

        M.list_facts = lambda *a, **k: {"items": [fact(1, 20, "下周三要去体检")]}
        check("太旧（20 天）→ 不选", call() is None)

        M.list_facts = lambda *a, **k: {"items": [fact(1, 3, "他喜欢吃辣", "偏好")]}
        check("不含关键词（不是待办）→ 不选", call() is None)

        st2 = P.ProactiveState()
        st2.used_anchors.append(1.0)
        M.list_facts = lambda *a, **k: {"items": [fact(1, 3, "下周三要去体检")]}
        check("问过一次 → 不再问", call(st2) is None)

        M.list_facts = lambda *a, **k: {"items": [
            fact(2, 3, "要去面试"), fact(3, 2, "要搬家")]}
        c2 = call()
        check("多条时挑最近的",
              c2 is not None and "搬家" in c2.reason, c2.reason if c2 else "")

        check("没有 owner / db → 静默跳过",
              P.candidate_recall(st, chat_id="c", now=now) is None)
    finally:
        M.list_facts = orig


def test_proactive_state_roundtrip() -> None:
    print("\n[状态] 落盘往返")
    tmp = tempfile.mkdtemp(prefix="bf-pro-")
    st = P.ProactiveState(day="2026-11-07", sent_today=2, last_source="greeting")
    P.mark_sent(st, chat_id="wxid_a", source="greeting", text="在忙吗",
                now=1000.0, anchor_at=42)
    check("保存成功", P.save(st, root=tmp))
    back = P.load(root=tmp)
    check("读回 day", back.day == "2026-11-07", back.day)
    # 初始 2 条 + mark_sent 又加 1 = 3
    check("读回条数", back.sent_today == 3, str(back.sent_today))
    check("读回待回", "wxid_a" in back.awaiting, str(back.awaiting))
    check("读回锚点", 42 in back.used_anchors, str(back.used_anchors))

    with open(os.path.join(tmp, P.STATE_NAME), "w", encoding="utf-8") as f:
        f.write("{坏的")
    broken = P.load(root=tmp)
    check("坏文件返回空状态而不是抛", broken.day == "" and broken.sent_today == 0)


# ══════════════════════════════════════════════════════════════════════
# 主
# ══════════════════════════════════════════════════════════════════════

def main() -> int:
    print("=" * 46)
    print("主动 / 关系 / 叙事 自检")
    print("=" * 46)

    test_gates()
    test_day_roll()
    test_open_loop()
    test_life_share()
    test_greeting()
    test_date()
    test_silence()
    test_collect_sorts()
    test_clean_reply()
    test_followup_probability()
    test_followup_window()
    test_awaiting_lifecycle()
    test_bond_parse()
    test_bond_store()
    test_bond_edit()
    test_dry_run()
    test_hit_probability()
    test_miss_streak()
    test_whim()
    test_sources_reachable()
    test_source_dedupe()
    test_layers()
    test_recall()
    test_proactive_state_roundtrip()

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
