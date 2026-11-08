"""长大体检：日程 / 睡前复盘 / 关系事件 / 感知。

跑法：
    python tools/selftest_growth.py

不调模型、不联网、不碰用户真实数据 —— 全程临时数据根。

## 为什么要单独一个文件

这四块是 P1 加的"她会变"的部分。它们有一个共同的风险：

    **编。**

· 日程编出"今天去医院" → 你问"你怎么有空回我"，人设当场塌；
· 复盘编出"我改了对他的看法" → 一周后 selfNotes 全是互相矛盾的"我现在觉得"；
· 关系事件编出"第一次一起看电影" → 她会反复提一件没发生的事；
· 感知编出天气 → 她会在晴天说"外面下雨了，带伞"。

所以这里的断言以"不许"为主：不许编、不许填满、不许覆盖手写。

## 守哪几条底线

1. `changed_view` 空是合法结果，不是失败。
2. 灵魂演化只追加、不覆盖；用户手写的条目永远不删。
3. 关系事件按 (kind, what) 去重，不覆盖、不重复。
4. 日程必须有"接昨天"的一条；没素材就不补。
5. 天气只读缓存，`render` 绝不联网。
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

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
        FAILED.append(name + (f"｜{detail}" if detail else ""))
        print(f"  {BAD_MARK} {name}")
        if detail:
            print(f"      {detail}")


def section(title: str) -> None:
    print()
    print(title)


def at(hour: int, minute: int = 0, *,
       days: int = 0) -> float:
    base = _dt.datetime.now().replace(hour=hour, minute=minute,
                                      second=0, microsecond=0)
    return (base + _dt.timedelta(days=days)).timestamp()


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="bf-growth-")
    os.environ["BIGFISH_DATA_ROOT"] = tmp

    from bigfish import bond as B
    from bigfish import life as L
    from bigfish import narrative as N
    from bigfish import plan as PLAN
    from bigfish import senses as S
    from bigfish import vault as V

    print("=" * 60)
    print("长大体检：日程 / 复盘 / 关系 / 感知")
    print(f"临时数据根：{tmp}")
    print("=" * 60)

    # ════════════════════════════════════════════════════════════
    section("[日程] 解析：宽松读，严格收")
    # ════════════════════════════════════════════════════════════

    raw = ('{"plan":[{"at":"09:30","what":"去趟超市","shareable":true,'
           '"from":"yesterday"},{"at":"14:00","what":"把书看完",'
           '"from":"self"},{"at":"20:00","what":"散步","from":"fact"}]}')
    items, err = PLAN.parse_plan(raw)
    check("正常 JSON 能解", len(items) == 3 and not err, f"{len(items)} {err}")
    check("时间补零", items[0]["at"] == "09:30", str(items[0]))
    check("from 保留", items[0]["from"] == "yesterday", str(items[0]))

    fenced = "```json\n" + raw + "\n```"
    items2, _ = PLAN.parse_plan(fenced)
    check("代码围栏能解", len(items2) == 3, str(len(items2)))

    noisy = "好的，这是今天的安排：\n" + raw + "\n希望合适。"
    items3, _ = PLAN.parse_plan(noisy)
    check("前后有废话也能解", len(items3) == 3, str(len(items3)))

    short = '{"plan":[{"at":"9:5","what":"洗衣服","from":"self"}]}'
    items4, err4 = PLAN.parse_plan(short)
    check("个位数时间能补", items4 and items4[0]["at"] == "09:05", str(items4))
    check("少于两条算不合格", bool(err4) and "至少" in err4, err4)

    bad = ('{"plan":[{"at":"25:00","what":"x"},{"at":"10:00","what":""},'
           '{"at":"11:00","what":"做饭"}]}')
    items5, err5 = PLAN.parse_plan(bad)
    check("非法时间和空内容被丢掉",
          len(items5) == 1 and items5[0]["what"] == "做饭", str(items5))

    over = '{"plan":[' + ",".join(
        '{"at":"%02d:00","what":"事%d"}' % (9 + i, i) for i in range(6)) + ']}'
    items6, _ = PLAN.parse_plan(over)
    check("最多留三条", len(items6) == 3, str(len(items6)))

    check("完全没有 JSON 返回空",
          PLAN.parse_plan("今天很好")[0] == [])

    # ════════════════════════════════════════════════════════════
    section("[日程] 必须接昨天")
    # ════════════════════════════════════════════════════════════

    only_self = [{"at": "10:00", "what": "买菜", "from": "self"},
                 {"at": "15:00", "what": "看书", "from": "self"}]
    fixed = PLAN.ensure_yesterday(list(only_self),
                                  ["14:00 把书看完（做完了）", "09:00 去趟超市"])
    check("没接昨天会补一条",
          any(x.get("from") == "yesterday" for x in fixed), str(fixed))
    check("补的内容来自昨天素材",
          any("超市" in str(x.get("what")) for x in fixed
              if x.get("from") == "yesterday"), str(fixed))
    check("补完仍按时间排序",
          fixed == sorted(fixed, key=lambda x: str(x.get("at"))), str(fixed))

    has_one = [{"at": "10:00", "what": "买菜", "from": "yesterday"}]
    same = PLAN.ensure_yesterday(list(has_one), ["09:00 去趟超市"])
    check("已经接上就不重复补", len(same) == 1, str(same))

    none_seed = PLAN.ensure_yesterday(list(only_self), [])
    check("没有素材就不硬补", len(none_seed) == 2, str(none_seed))

    # ════════════════════════════════════════════════════════════
    section("[日程] 落进 life 状态")
    # ════════════════════════════════════════════════════════════

    st = L.LifeState()
    L.set_plan(st, [
        {"at": "14:00", "what": "把书看完", "from": "yesterday"},
        {"at": "09:30", "what": "去趟超市", "from": "fact"},
        {"at": "bad", "what": "坏时间"},
        {"at": "18:00", "what": ""},
    ], day="2026-11-08")
    check("坏数据被丢掉", len(st.plan) == 2, str(st.plan))
    check("按时间排好", st.plan[0]["at"] == "09:30", str(st.plan))
    check("记下是哪天的", st.plan_day == "2026-11-08", st.plan_day)

    check("上午在做第一条",
          L.current_plan_item(st, at(10)) == "去趟超市",
          L.current_plan_item(st, at(10)))
    check("下午换成第二条",
          L.current_plan_item(st, at(15)) == "把书看完",
          L.current_plan_item(st, at(15)))
    check("还没到第一条时用第一条",
          L.current_plan_item(st, at(7)) == "去趟超市",
          L.current_plan_item(st, at(7)))

    L.refresh_doing(st, at(15))
    check("手头的事优先从日程取", st.doing == "把书看完", st.doing)

    L.mark_plan_done(st, "把书看完", now=at(16))
    prompt = L.plan_for_prompt(st, now=at(16))
    check("做完的会标出来", "做完了" in prompt, prompt[:80])
    check("提示词里不写数值", "/100" not in prompt)

    empty = L.LifeState()
    check("没日程时不注入", L.plan_for_prompt(empty, now=at(10)) == "")
    L.refresh_doing(empty, at(15))
    check("没日程时退回静态池", bool(empty.doing), empty.doing)

    # ════════════════════════════════════════════════════════════
    section("[复盘] 什么时候跑")
    # ════════════════════════════════════════════════════════════

    st = L.LifeState()
    st.day = "2026-11-08"
    st.bedtime = at(23, 20)
    st.wake_at = at(7, 40, days=1)

    check("入睡前 20 分钟 → 该跑",
          N.due(st, at(23, 0)) is True, "")
    check("下午不跑", N.due(st, at(15)) is False)
    check("睡着之后不跑",
          N.due(st, at(23, 30)) is False)

    st.narrative_day = "2026-11-08"
    check("今天跑过就不跑", N.due(st, at(23, 0)) is False)

    # ════════════════════════════════════════════════════════════
    section("[复盘] 解析：允许空")
    # ════════════════════════════════════════════════════════════

    empty_ok = ('{"summary":"今天很平常。","changed_view":[],'
                '"relation_events":[],"stage":null}')
    parsed, err = N.parse(empty_ok)
    check("空数组是合法结果", not err and parsed, err)
    check("空就是空，不补", parsed["changed_view"] == []
          and parsed["relation_events"] == [], str(parsed))

    full = ('{"summary":"他说了加班。","changed_view":[{"about":"他",'
            '"from":"不在乎我","to":"只是太累了","why":"他说加班到十点"}],'
            '"relation_events":[{"kind":"warm","what":"他第一次说累",'
            '"evidence":"今天加班到十点"}],"stage":{"name":"有点依赖",'
            '"why":"开始会等他消息"}}')
    parsed2, _ = N.parse(full)
    check("看法能解析", parsed2["changed_view"][0]["to"] == "只是太累了",
          str(parsed2["changed_view"]))
    check("事件能解析", parsed2["relation_events"][0]["kind"] == "warm",
          str(parsed2["relation_events"]))
    check("阶段能解析", (parsed2.get("stage") or {}).get("name") == "有点依赖",
          str(parsed2.get("stage")))

    bad_kind = ('{"summary":"x","changed_view":[],"relation_events":'
                '[{"kind":"whatever","what":"乱写的"}]}')
    parsed3, _ = N.parse(bad_kind)
    check("非法事件类型被丢掉", parsed3["relation_events"] == [],
          str(parsed3["relation_events"]))

    bad_stage = ('{"summary":"x","changed_view":[],"relation_events":[],'
                 '"stage":{"name":"灵魂伴侣"}}')
    parsed4, _ = N.parse(bad_stage)
    check("非法阶段被丢掉", parsed4.get("stage") is None,
          str(parsed4.get("stage")))

    check("纯文本返回空", N.parse("今天没什么")[0] == {})

    # ════════════════════════════════════════════════════════════
    section("[复盘] 写进灵魂：只追加，不覆盖")
    # ════════════════════════════════════════════════════════════

    soul_file = V.soul_path()
    V.write_default_soul(soul_file)
    soul, _e, _err = V.read_soul_file(soul_file)
    soul["selfNotes"] = ["我手写的第一条"]
    V.write_soul_file(soul_file, soul)

    changed = [{"about": "他", "from": "不在乎我", "to": "只是太累了",
                "why": "他说加班到十点"}]
    ok, n, err = V.evolve_soul(changed, "他说了加班。")
    check("灵魂写入成功", ok and n == 1, f"{ok} {n} {err}")

    soul, _e, _err = V.read_soul_file(soul_file)
    notes = soul.get("selfNotes") or []
    check("手写条目还在", "我手写的第一条" in notes, str(notes))
    check("新条目带日期前缀",
          any(str(x).startswith("（") for x in notes), str(notes))
    check("新条目写的是看法变化",
          any("只是太累了" in str(x) for x in notes), str(notes))
    check("history 记了一笔",
          any(h.get("kind") == "evolved" for h in (soul.get("history") or [])),
          str(soul.get("history"))[:120])

    before = len(notes)
    ok2, n2, _ = V.evolve_soul(changed, "又一天")
    check("同一个看法不重复记", (not ok2) or n2 == 0, f"{ok2} {n2}")

    # 上限
    for i in range(20):
        V.evolve_soul([{"about": "我", "from": "", "to": f"看法{i}",
                        "why": "测试"}])
    soul, _e, _err = V.read_soul_file(soul_file)
    notes = soul.get("selfNotes") or []
    evolved = [x for x in notes if str(x).startswith("（")]
    check("自动条目不超过上限", len(evolved) <= V.MAX_EVOLVED_NOTES,
          str(len(evolved)))
    check("手写条目一条不少", "我手写的第一条" in notes, str(notes[:3]))

    # ════════════════════════════════════════════════════════════
    section("[关系] 事件与阶段")
    # ════════════════════════════════════════════════════════════

    db = V.memory_db_path()
    from bigfish import memory as M

    M.ensure_schema(db)
    B.ensure_schema(db)
    bank, owner = "dafeiyu", "private:wechat:wxid_test"

    ok, why = B.append_event(db, bank, owner, kind="warm",
                             what="他第一次说累", evidence="今天加班到十点")
    check("事件写入", ok and why == "added", f"{ok} {why}")
    ok2, why2 = B.append_event(db, bank, owner, kind="warm",
                               what="他第一次说累")
    check("同一件事不记两条", (not ok2) and why2 == "exists", f"{ok2} {why2}")
    ok3, _ = B.append_event(db, bank, owner, kind="conflict",
                            what="为小事拌了两句")
    check("不同类型可以并存", ok3, "")

    rows = B.list_events(db, bank, owner, limit=10)
    check("事件读得回来", len(rows) == 2, str(len(rows)))

    ok, _ = B.set_stage(db, bank, owner, "有点依赖", "开始会等他消息")
    check("阶段写入", ok, "")
    check("阶段读得回来", B.get_stage(db, bank, owner) == "有点依赖",
          B.get_stage(db, bank, owner))

    B.set_stage(db, bank, owner, "熟了", "又退回一步")
    check("换阶段会覆盖旧的", B.get_stage(db, bank, owner) == "熟了",
          B.get_stage(db, bank, owner))

    ok, _ = B.append_event(db, bank, owner, kind="乱写", what="x")
    check("非法事件类型拒绝写入", not ok, "")

    rendered = B.render_for_prompt(db, bank, owner)
    check("关系卡里有阶段", "我们现在" in rendered, rendered[:100])
    check("关系卡里有事件", "发生过的" in rendered, rendered[:160])

    # ════════════════════════════════════════════════════════════
    section("[复盘] 一次调用写两处")
    # ════════════════════════════════════════════════════════════

    result = {
        "summary": "今天他说了加班。",
        "changed_view": [{"about": "他", "from": "不在乎我",
                          "to": "只是太累了", "why": "加班到十点"}],
        "relation_events": [{"kind": "promise", "what": "说好周末一起吃饭",
                             "evidence": "周末一起吃饭"}],
        "stage": {"name": "有点依赖", "why": "开始等他消息"},
    }
    receipt = N.apply_result(result, soul_path=soul_file, db=db,
                             bank=bank, owner=owner, now=at(23, 0))
    check("灵魂写了一次", receipt["notes"] == 1, str(receipt))
    check("关系事件写了一次", receipt["events"] == 1, str(receipt))
    check("阶段更新了", receipt["stage"] == "有点依赖", str(receipt))
    check("回执能读成人话", "自述" in N.summarize_receipt(receipt),
          N.summarize_receipt(receipt))

    empty_receipt = N.apply_result(
        {"summary": "", "changed_view": [], "relation_events": [],
         "stage": None}, soul_path=soul_file, db=db, bank=bank, owner=owner)
    check("什么都没变时不写", empty_receipt["notes"] == 0
          and empty_receipt["events"] == 0, str(empty_receipt))
    check("空回执说人话",
          "没什么可写" in N.summarize_receipt(empty_receipt),
          N.summarize_receipt(empty_receipt))

    # ════════════════════════════════════════════════════════════
    section("[感知] 日子 / 近期事件 / 天气缓存")
    # ════════════════════════════════════════════════════════════

    check("元旦认得出",
          "元旦" in S.calendar_line(_dt.datetime(2027, 1, 1, 12).timestamp()),
          "")
    check("普通日子不硬凑",
          S.calendar_line(_dt.datetime(2027, 1, 3, 12).timestamp()) == "", "")

    soon = _dt.datetime.now() + _dt.timedelta(days=2)
    far = _dt.datetime.now() + _dt.timedelta(days=40)
    facts = [f"{soon.month}月{soon.day}日要去面试",
             f"{far.month}月{far.day}日搬家",
             "他喜欢喝美式"]
    up = S.upcoming_from_facts(facts, horizon_days=3)
    check("近期事件抽得出来", len(up) == 1, str(up))
    check("远期事件不抽", all("搬家" not in x for x in up), str(up))
    check("事件带日期", bool(up) and "月" in up[0], str(up))

    S.save({"version": 1, "geo": {}, "weather": {}})
    check("没缓存时天气为空", S.cached_weather() == "", "")
    check("没缓存时整段不出现", S.render(facts=None) == "", S.render(facts=None))

    S.save({"version": 1, "geo": {},
            "weather": {"city": "杭州", "at": _dt.datetime.now().timestamp(),
                        "line": "12°C，多云"}})
    check("有缓存时读得回", "12°C" in S.cached_weather(), S.cached_weather())
    check("渲染带城市", "杭州" in S.render(), S.render()[:80])

    S.save({"version": 1, "geo": {},
            "weather": {"city": "杭州",
                        "at": _dt.datetime.now().timestamp() - 2 * 86400,
                        "line": "12°C，多云"}})
    check("超过一天的天气不认", S.cached_weather() == "", S.cached_weather())

    # 绝不在 render 里联网：把 TTL 内的旧值直接返回，不发请求
    S.save({"version": 1, "geo": {},
            "weather": {"city": "杭州", "at": _dt.datetime.now().timestamp(),
                        "line": "8°C，小雨"}})
    check("render 只读缓存（值不变）",
          "小雨" in S.render() and "8°C" in S.render(), S.render()[:80])

    # ════════════════════════════════════════════════════════════
    section("[日志] 清洗：只收拾格式，不改写")
    # ════════════════════════════════════════════════════════════

    from bigfish import journal as J

    fenced = "```markdown\n今天他说加班到十点。\n我问他吃没吃。\n```"
    check("去代码围栏", "```" not in J.clean(fenced)
          and "加班到十点" in J.clean(fenced), J.clean(fenced)[:60])

    opened = "好的，这是今天的日记：\n今天他说加班到十点。"
    check("去掉开场白", not J.clean(opened).startswith("好的"),
          J.clean(opened)[:40])

    long_body = "今天他说加班到十点。" * 400
    cut = J.clean(long_body)
    check("超软上限会截断", len(cut) <= J.SOFT_LIMIT + 40, str(len(cut)))
    check("截断有标记", "写到一半" in cut, cut[-30:])

    plain = "今天很安静。\n只说了两句。"
    check("普通正文原样保留", J.clean(plain) == plain, J.clean(plain))
    check("空输入返回空", J.clean("   ") == "", "")

    # ════════════════════════════════════════════════════════════
    section("[日志] 落盘：一天一篇，能用记事本打开")
    # ════════════════════════════════════════════════════════════

    ok, path, chars = J.write("2026-11-08", "今天他说加班到十点。\n我问他吃没吃。",
                              meta={"chars": 18})
    check("写成功", ok and chars > 0, f"{ok} {chars}")
    check("路径在 journal/ 下", path.endswith("2026-11-08.md"), path)

    body = J.read("2026-11-08")
    check("第一行是日期标题", body.startswith("# 2026-11-08"), body[:30])
    check("正文在", "加班到十点" in body, body[:60])
    check("元信息是注释，不挡阅读", "<!--" in body, body[-60:])

    ok2, _p, _c = J.write("2026-11-08", "重写了一版。", meta={})
    check("同一天可以覆盖", ok2 and "重写了一版" in J.read("2026-11-08"),
          J.read("2026-11-08")[:40])

    ok3, _p, _c = J.write("2026-11-09", "第二天。", meta={})
    check("另一天另起一篇", ok3 and "第二天" in J.read("2026-11-09"), "")

    check("没写过的日期读成空", J.read("2025-01-01") == "", "")

    days = J.list_days()
    check("列表新的在前", days[:2] == ["2026-11-09", "2026-11-08"], str(days[:3]))

    day_latest, text_latest = J.latest()
    check("最近一篇取得回", day_latest == "2026-11-09" and "第二天" in text_latest,
          f"{day_latest} {text_latest[:20]}")

    ok4, _p, _c = J.write("2026-11-10", "   ", meta={})
    check("空正文拒绝写入", not ok4, f"{ok4}")

    # ════════════════════════════════════════════════════════════
    section("[日志] 素材闸门：一句话也能写")
    # ════════════════════════════════════════════════════════════

    check("一句话就够", J.has_material({"talk": ["他: 吃了吗"]})[0] is True)
    check("有事实也够", J.has_material({"facts": ["他下周三面试"]})[0] is True)
    # v0.10.2：用户要求"当天啥也没发生也能写"，所以门槛改成
    # "有没有一个可以依附的今天"，不是"有没有内容"。
    check("只有日期也能写",
          J.has_material({"day": "2026-11-08"})[0] is True)
    check("只有手头的事也能写",
          J.has_material({"doing": "在看书"})[0] is True)
    check("只有日程也能写",
          J.has_material({"plan": ["09:30 去趟超市"]})[0] is True)
    check("连日期都没有才不写",
          J.has_material({"talk": [], "facts": [],
                          "relation": {}})[0] is False)
    check("只有心情、没有日期 → 不写",
          J.has_material({"mood": ["有点难过"]})[0] is False)

    _sys, user = J.build_request(day="2026-11-08", materials={
        "talk": ["他: 今天加班到十点", "我: 吃了吗"],
        "mood": ["他说没吃饭，她有点心疼"],
        "weather": "杭州 12°C 多云",
        "narrative_summary": "今天他说了加班。",
    })
    check("请求带对话", "加班到十点" in user, user[:80])
    check("请求带天气", "12°C" in user, "")
    check("请求带复盘锚点", "今天他说了加班" in user, "")
    check("提示词不限字数", "不限字数" in _sys, _sys[:80])
    check("提示词不许总结", "不要总结" in _sys, "")
    check("提示词要求第一人称", "第一人称" in _sys, "")
    check("提示词允许空白", "允许空白" in _sys, _sys[:80])
    check("提示词禁止编对话", "不许编对话" in _sys, "")

    _sys2, user2 = J.build_request(day="2026-11-09",
                                   materials={"talk": [], "plan": []})
    check("空白要明说没人说话", "没有人跟你说话" in user2, user2[:100])
    check("空日程要明说", "没有安排" in user2, user2[:140])
    check("空白不编对话的指令进请求", "没有发生过" in user2, user2[:160])

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
