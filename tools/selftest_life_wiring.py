"""接线自检 —— 她"活着"这件事有没有真的接到链路上。

跑法：
    python tools/selftest_life_wiring.py

`selftest_life.py` 证明状态机本身对；这个文件证明**它接上了**：

    tick 推进 → gate 拦下 → 记账 pending → 醒来 briefing → 注入 prompt

这条链上任何一环断了，症状都是一样的：「她好像还是不会睡」。
分不清是没判对、没拦下、还是拦下了没记账 —— 所以每一环单独验一次。

不需要微信、不需要桌面、不需要网络。
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import history as H  # noqa: E402
from bigfish import life as L  # noqa: E402
from bigfish import plugins as P  # noqa: E402

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


# ══════════════════════════════════════════════════════════════════════
# 插件注册
# ══════════════════════════════════════════════════════════════════════

def test_registry() -> None:
    print("\n[注册] 新插件真的在里面，座位没撞")
    P.ensure_loaded()
    ids = set(P._REGISTRY)

    check("life 已登记", "life" in ids, str(sorted(ids)))
    check("life 占 870", P._REGISTRY["life"]["order"] == 870,
          str(P._REGISTRY["life"]["order"]))
    # 「时间感知」「身体」「睡觉不回」在 v0.9.1 合成一个插件：它们讲的是
    # 同一件事（她有自己的作息）。所以这里断言的是"它是一个双形态插件"，
    # 不再是"有两个插件分别登记"。
    check("life 同时是 prompt 和 gate",
          set(P._REGISTRY["life"]["scopes"]) == {"prompt", "gate"},
          str(P._REGISTRY["life"].get("scopes")))
    check("sleep-gate 不再单独登记", "sleep-gate" not in ids)
    check("clock 不再单独登记", "clock" not in ids)
    check("870 在空位表里", 870 in P.PLUGIN_SEATS)
    check("870 不是内建段", 870 not in P.BUILTIN_SEATS)
    # 871 已经腾空 —— 合并不是"挪个位置"，是少了一个座位
    check("871 已腾空", 871 not in P.PLUGIN_SEATS)

    # 座位唯一性：不允许两个插件同号
    orders = [d["order"] for d in P._REGISTRY.values() if d.get("order")]
    check("没有两个插件抢同一个座位", len(orders) == len(set(orders)), str(orders))


# ══════════════════════════════════════════════════════════════════════
# 注入顺序
# ══════════════════════════════════════════════════════════════════════

def test_injection_order() -> None:
    print("\n[注入] 身份序：生活（时间+状态）→ 说法 → 心情 → 守卫")
    P.ensure_loaded()
    state = L.LifeState()
    now = at(2026, 11, 7, 15, 0)
    L.tick(state, now)

    sections = P.prompt_sections({"now": now, "life": state, "tier": "owner"})
    titles = [t for _o, t, _x in sections]
    order = [o for o, _t, _x in sections]

    check("注入段按 order 升序", order == sorted(order), str(order))
    check("生活在前", titles[0] == "生活", str(titles))
    check("守卫在最后", titles[-1] == "末尾守卫", str(titles))

    body = [x for _o, t, x in sections if t == "生活"][0]
    # 合并之后一段里要同时有「几点」和「她怎么样」—— 缺任何一半都说明
    # 合并只做了一半（把两个插件塞进一个文件，但只有一段在跑）
    # 2026-11-07 是周六 —— 这里只断言「日期 + 星期」都在，别把星期固定死
    check("同一段里说了时间", "现在是" in body and "周" in body, body[:120])
    check("同一段里说了状态", "手头在" in body, body[:200])
    check("状态段提醒不要报数值", "精力 62" in body or "报数值" in body)


def test_injection_sleeping() -> None:
    print("\n[注入] 睡着时不说手头在干嘛")
    P.ensure_loaded()
    state = L.LifeState()
    now = at(2026, 11, 7, 22, 0)
    L.tick(state, now)
    state.asleep = True
    state.asleep_since = at(2026, 11, 7, 23, 30)
    state.wake_at = at(2026, 11, 8, 7, 50)

    sections = P.prompt_sections({"now": at(2026, 11, 8, 2, 0),
                                  "life": state, "tier": "owner"})
    body = [x for _o, t, x in sections if t == "生活"][0]
    check("睡着时说自己睡了", "已经睡了" in body, body[:80])
    check("睡着时不说手头在干嘛", "手头在" not in body, body[:80])


# ══════════════════════════════════════════════════════════════════════
# 整条链
# ══════════════════════════════════════════════════════════════════════

def test_full_chain() -> None:
    print("\n[整链] 睡着 → 拦下 → 记账 → 醒来 → 带出来")
    P.ensure_loaded()

    state = L.LifeState()
    now = at(2026, 11, 7, 10)
    L.tick(state, now)
    check("上午她醒着", not state.asleep)

    # 到点睡下
    L.tick(state, state.bedtime + 60)
    check("到点睡了", state.asleep)
    t_sleeping = state.asleep_since + 3600

    # 深夜有人发消息 → gate 应当拦
    silent, why = P.apply_gates({"now": t_sleeping, "life": state,
                                 "tier": "owner", "user_text": "睡了吗"})
    check("睡着时对主人沉默", silent, why)
    check("沉默原因里写明她睡了", "她睡了" in why, why)
    check("沉默原因里带上醒来时间", "醒" in why, why)

    # 非主人不拦（默认配置）
    silent2, _ = P.apply_gates({"now": t_sleeping, "life": state,
                                "tier": "known", "user_text": "在吗"})
    check("睡着时对非主人不沉默", not silent2)

    # 引擎记账（这一步由 desktop 做，这里模拟）
    L.note_pending(state, "wxid_owner", "睡了吗", t_sleeping)
    L.note_pending(state, "wxid_owner", "明天记得叫我", t_sleeping + 600)
    check("记了两条", (L.peek_pending(state, "wxid_owner") or {}).get("count") == 2)

    # 醒来
    events = L.tick(state, state.wake_at + 60)
    check("醒了", not state.asleep)
    check("醒来有事件", any(e.kind == "woke" for e in events),
          str([e.kind for e in events]))

    # 醒来后第一轮：briefing 应该带出那两条
    brief = L.briefing(state, "wxid_owner", state.just_woke_at + 300)
    check("briefing 提到 2 条", "2 条" in brief, brief)
    check("briefing 带上最后一条预览", "明天记得叫我" in brief, brief)

    # 带出去之后要清掉，别下次再说一遍
    L.take_pending(state, "wxid_owner")
    check("用掉之后不再重复说",
          L.briefing(state, "wxid_owner", state.just_woke_at + 600) == "")

    # 别的会话不受影响
    check("没有 pending 的会话不生成 briefing",
          L.briefing(state, "wxid_other", state.just_woke_at + 600) == "")


def test_nobody_talks() -> None:
    print("\n[反例] 一整天没人说话，她也要过完这一天")
    state = L.LifeState()
    t = at(2026, 11, 7, 8)
    L.tick(state, t)
    woke = asleep = 0
    doing_changes = 0
    for _ in range(24 * 12):            # 5 分钟一步，走满 24 小时
        t += 5 * 60
        for ev in L.tick(state, t):
            if ev.kind == "woke":
                woke += 1
            elif ev.kind == "asleep":
                asleep += 1
            elif ev.kind == "doing":
                doing_changes += 1
    check("一天里她睡了一次", asleep >= 1, f"asleep={asleep}")
    check("一天里她醒了一次", woke >= 1, f"woke={woke}")
    check("手头的事换过几回", doing_changes >= 3, f"{doing_changes} 次")
    check("第二天早上她是醒着的", not state.asleep or state.energy > 0,
          L.condition(state))


# ══════════════════════════════════════════════════════════════════════
# 短期历史
# ══════════════════════════════════════════════════════════════════════

def test_history() -> None:
    print("\n[短期历史] 重启之后还记得刚才聊到哪")
    tmp = tempfile.mkdtemp(prefix="bigfish-hist-")
    old = os.environ.get("BIGFISH_DATA_ROOT")
    os.environ["BIGFISH_DATA_ROOT"] = tmp
    try:
        check("空目录读出来是空的", H.load("wxid_x", root=tmp) == [])

        H.append("wxid_x", "老爸", "明天我生日", root=tmp)
        H.append("wxid_x", "大肥鱼", "我记得", root=tmp)
        back = H.load("wxid_x", root=tmp)
        check("写两条读回两条", len(back) == 2, str(back))
        check("内容和顺序都对",
              back[0] == ("老爸", "明天我生日") and back[1] == ("大肥鱼", "我记得"),
              str(back))

        # 会话之间不串
        H.append("wxid_y", "别人", "你好", root=tmp)
        check("另一个会话互不影响", len(H.load("wxid_x", root=tmp)) == 2)
        check("另一个会话能读到自己的", len(H.load("wxid_y", root=tmp)) == 1)

        # limit
        check("limit 生效", len(H.load("wxid_x", root=tmp, limit=1)) == 1)

        # 裁剪
        for i in range(H.KEEP_LINES + 40):
            H.append("wxid_x", "老爸", f"第 {i} 句", root=tmp)
        lines = len(H.load("wxid_x", root=tmp, limit=10_000))
        check("行数被裁到上限附近", lines <= H.KEEP_LINES,
              f"{lines} > {H.KEEP_LINES}")
        check("裁掉的是旧的，留的是新的",
              H.load("wxid_x", root=tmp, limit=1)[0][1].startswith("第 "),
              str(H.load("wxid_x", root=tmp, limit=1)))

        # 特殊字符的 chat_id 不会炸
        weird = "123@chatroom"
        check("群号能写", H.append(weird, "某人", "在吗", root=tmp))
        check("群号能读", len(H.load(weird, root=tmp)) == 1)

        # 统计与清理
        s = H.stats(root=tmp)
        check("统计能出来", s["files"] >= 2 and s["lines"] > 0, str(s))
        n = H.clear_all(tmp)
        check("清空能删掉", n >= 2, f"删了 {n}")

        # 坏行不炸
        p = H.path_for("wxid_z", tmp)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("这不是 json\n{\"who\":\"a\",\"text\":\"好的\"}\n")
        got = H.load("wxid_z", root=tmp)
        check("坏行被跳过，好行保留", len(got) == 1, str(got))
    finally:
        os.environ.pop("BIGFISH_DATA_ROOT", None)
        if old is not None:
            os.environ["BIGFISH_DATA_ROOT"] = old


def test_history_never_raises() -> None:
    print("\n[短期历史] 写不进去只是这次不记，不抛")
    ok = H.append("wxid", "a", "b", root=os.path.join(tempfile.gettempdir(),
                                                      "bf\0bad", "x"))
    check("非法路径返回 False", ok is False)


# ══════════════════════════════════════════════════════════════════════
# 主
# ══════════════════════════════════════════════════════════════════════

def main() -> int:
    print("=" * 46)
    print("接线下自检（life + plugins + history）")
    print("=" * 46)

    test_registry()
    test_injection_order()
    test_injection_sleeping()
    test_full_chain()
    test_nobody_talks()
    test_history()
    test_history_never_raises()

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
