"""动态集成检查：在沙盒数据根里跑**真实模块**，验证运行时行为。

跑法：
    python tools/selftest_dynamic.py

和其余 selftest 的区别：那些测单个函数，这里把「轨迹累积 → 周叙事 →
日程结算 → 约定到期 → 主动候选 → 决策快照 → 旧账」串起来跑一遍，
用的是真实 `life` / `weekly` / `plan` / `proactive` / `prompt_trace`。

不连微信、不碰 D:\\大肥鱼、不调模型（模型响应用固定 JSON 代替）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = os.path.join(ROOT, ".tmp", "dynamic_root")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
os.environ["BIGFISH_DATA_ROOT"] = TMP

from bigfish import bond as B  # noqa: E402
from bigfish import future as F  # noqa: E402
from bigfish import life as L  # noqa: E402
from bigfish import plan as P  # noqa: E402
from bigfish import proactive as PR  # noqa: E402
from bigfish import prompt_trace as PT  # noqa: E402
from bigfish import soul as S  # noqa: E402
from bigfish import vault as V  # noqa: E402
from bigfish import weekly as W  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [X ] {name}" + (f"  ({detail})" if detail else ""))


def main() -> int:
    now = time.time()
    print("=" * 60)
    print("  动态运行检查（沙盒数据根）")
    print(f"  数据根：{TMP}")
    print("=" * 60)

    # ── 1. 轨迹累积：连续 refresh_doing 是否写 history_trace ──
    print("\n── 1 · 轨迹累积 ──")
    st = L.LifeState()
    st.day = L.day_key(now)
    st.plan_day = st.day
    st.plan = []
    st.last_tick = now - 7200
    st.energy = 80.0
    base = now - 7200
    for i in range(6):
        L.refresh_doing(st, base + i * 1200)
    check("day_trace 有内容", len(st.day_trace) > 0, str(len(st.day_trace)))
    check("history_trace 有内容", len(st.history_trace) > 0, str(len(st.history_trace)))
    check("history_trace 带 day", all("day" in x for x in st.history_trace),
          str(st.history_trace[:2]))
    check("同一天同一件事去重",
          len({(x["day"], x["doing"]) for x in st.history_trace}) == len(st.history_trace))

    # 序列化往返
    L.save(st, force=True)
    st2 = L.load()
    check("落盘后读回 history_trace", len(st2.history_trace) == len(st.history_trace),
          f"{len(st2.history_trace)} vs {len(st.history_trace)}")

    # ── 2. 周叙事：due → 素材 → 解析 → 写入 → 渲染 ──
    print("\n── 2 · 周叙事 ──")
    st3 = L.LifeState()
    st3.day = L.day_key(now)
    st3.plan_day = st3.day
    st3.weekly_day = ""
    st3.history_trace = []
    for d in range(5):
        t = now - d * 86400
        day = time.strftime("%Y-%m-%d", time.localtime(t))
        for i in range(3):
            st3.history_trace.append(
                {"at": t + i * 600, "day": day, "doing": f"第{d}天第{i}件事"})
    st3.history_trace.sort(key=lambda x: x["at"])
    check("5 天轨迹 → due", W.due(st3, now) is True)

    mats = W.build_materials(st3, now=now)
    check("素材按天聚合", len(mats["days"]) == 5, str(len(mats["days"])))
    ok, why = W.has_material(mats)
    check("素材够", ok, why)

    _sys, user = W.build_request(mats)
    check("请求含轨迹", "这七天的轨迹" in user)

    parsed, err = W.parse(json.dumps({
        "week": "这周收拾了屋子，见了卖鱼的",
        "did": ["收拾屋子", "买菜"], "met": ["卖鱼的摊子空着"],
        "mood": "有点闷", "unfinished": ["照片没整理"],
        "together": ["他加班到很晚"],
    }, ensure_ascii=False))
    check("解析成功", parsed is not None, err)

    soul_path = os.path.join(TMP, "soul.json")
    V.write_soul_file(soul_path, {
        "identity": {"name": "大肥鱼", "address": "爸", "whoIAm": "我是大肥鱼。"},
        "values": [{"id": "v1", "text": "真诚", "locked": True}],
        "traits": {"warmth": 60, "curiosity": 70, "energy": 55, "caution": 45},
        "selfNotes": [], "history": [],
    })
    ok2, werr = W.apply_result(soul_path, parsed, now=now)
    check("写入灵魂", ok2, werr)

    # load_soul() 的参数是**数据根**（内部拼 soul.json），不是文件路径。
    with open(soul_path, encoding="utf-8") as f:
        soul = S.Soul.from_dict(json.load(f), path=soul_path)
    check("week_line 渲染出来", bool(soul.week_line()), soul.week_line())
    check("week_line 带「这周」", "这周" in soul.week_line())
    check("history_lines 不串（不含这周）",
          all("这周" not in x for x in soul.history_lines()))

    W.mark_done(st3, day=time.strftime("%Y-%m-%d", time.localtime(now)))
    check("mark_done 后不再 due", W.due(st3, now) is False)

    # ── 3. 日程结算：做完 vs 没顾上 ──
    print("\n── 3 · 日程结算 ──")
    st4 = L.LifeState()
    st4.day = L.day_key(now)
    st4.plan_day = st4.day
    st4.plan = [
        {"at": "09:00", "what": "收拾屋子", "start": 9 * 60, "end": 9 * 60 + 50,
         "done": False, "touched": True},
        {"at": "13:00", "what": "整理照片", "start": 13 * 60, "end": 13 * 60 + 40,
         "done": False},
    ]
    t14 = time.mktime(time.strptime(
        time.strftime("%Y-%m-%d ", time.localtime(now)) + "14:30", "%Y-%m-%d %H:%M"))
    n = L.expire_plan(st4, t14)
    check("结算两条", n == 2, str(n))
    check("做过的算完成",
          st4.plan[0].get("done") and not st4.plan[0].get("abandoned"))
    check("没做的算没顾上", st4.plan[1].get("abandoned") is True)
    loops = L.own_loops(st4)
    check("own_loops 只出没顾上的", loops == ["整理照片"], str(loops))
    text = L.plan_for_prompt(st4, now=t14)
    check("渲染「没顾上」", "（没顾上）" in text, text[:80])

    # ── 4. 约定到期：写库 → 查询 → 候选 ──
    print("\n── 4 · 约定到期 ──")
    db = os.path.join(TMP, "memory.sqlite3")
    B.ensure_schema(db)
    owner = "private:wechat:x"
    B.append_event(db, "dafeiyu", owner, kind="promise", what="去体检",
                   at=now, due_at=now + 86400)
    rows = F.upcoming(db, "dafeiyu", owner, now=now)
    check("到期约定查得到", len(rows) == 1, str(len(rows)))
    check("due_line 说「明天」", F.due_line(rows[0], now=now).startswith("明天"),
          F.due_line(rows[0], now=now))

    # ── 5. 主动候选 + 决策快照 ──
    print("\n── 5 · 主动候选 + 决策快照 ──")
    pst = PR.ProactiveState()
    cands = PR.collect(pst, chat_id="c1", now=now, me="我",
                       life_state=st4, last_peer_at=now - 7200,
                       owner=owner, tier="owner", db_path=db)
    srcs = {c.source for c in cands}
    check("候选非空", bool(cands), str(srcs))
    check("due 进候选", "due" in srcs, str(srcs))
    check("own_loop 进候选", "own_loop" in srcs, str(srcs))

    topic, tone = PR.pick(cands)
    score = PR.combined_score(topic, tone)
    path = PT.record_decision(
        chat="c1",
        candidates=[{"source": c.source, "score": c.score,
                     "layer": c.layer, "reason": c.reason} for c in cands],
        picked={"topic": topic.source if topic else "",
                "tone": tone.source if tone else ""},
        score=score, hit=62, roll=37,
        extra={"why": "动态检查"})
    check("决策快照返回路径", bool(path), str(path))
    time.sleep(0.4)  # 后台线程写盘
    check("决策快照已落盘", bool(path) and os.path.isfile(path), str(path))
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            snap = json.load(f)
        check("快照含候选", len(snap.get("candidates") or []) > 0)
        check("快照含骰子", snap.get("roll") == 37, str(snap.get("roll")))

    # ── 6. 旧账 ──
    print("\n── 6 · 旧账 ──")
    B.append_event(db, "dafeiyu", owner, kind="unresolved",
                   what="他说了句难听的", at=now)
    cand_u = PR.candidate_unresolved(PR.ProactiveState(), chat_id="c1", now=now,
                                     owner=owner, tier="owner", db_path=db)
    check("旧账生成候选", cand_u is not None, str(cand_u))
    check("旧账只作 tone", cand_u and cand_u.layer == "tone")
    eid = None
    for r in B.list_events(db, "dafeiyu", owner, kind="unresolved", limit=5):
        eid = int(r["id"])
    check("resolve 成功", F.resolve(db, eid) is True)
    check("说开后不再提",
          PR.candidate_unresolved(PR.ProactiveState(), chat_id="c1", now=now,
                                  owner=owner, tier="owner", db_path=db) is None)

    # ── 7. 体力代价 ──
    print("\n── 7 · 体力代价 ──")
    items = [{"when": "上午", "what": "去趟超市"},
             {"when": "下午", "what": "看会儿书"}]
    out = P.layout(items, wake_at=now - 3 * 3600, bedtime=now + 8 * 3600)
    check("日程带 cost", all("cost" in x for x in out), str(out))
    check("cost 在 8–35", all(8 <= x["cost"] <= 35 for x in out),
          str([x.get("cost") for x in out]))

    # ── 出口 ──
    print()
    print("=" * 60)
    total = PASS + FAIL
    if FAIL:
        print(f"失败 {FAIL}/{total}")
        return 1
    print(f"全部通过 {PASS}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
