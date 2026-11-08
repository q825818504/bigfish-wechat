"""接话体检：「这一句接不接」「要不要补一句」，以及它们和别的东西的冲突。

跑法：
    python tools/selftest_afterthought.py

不需要微信、不调模型。重点不在"能不能补"，在**补一句会不会和别的事打架**：

    你说话了 —— 她补的那句会和新消息撞车
    她睡了   —— 睡着的消息不该发（和 sleep-gate 的规则一致）
    拖太久   —— 语境已经过去，这时补更像自言自语
    预演中   —— 只该进日志

这四条任何一条漏了，都会出现"她说了句莫名其妙的话"，而且事后查不出来。
"""
from __future__ import annotations

import os
import random
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import desktop as D  # noqa: E402
from bigfish import say as S  # noqa: E402

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


# ══════════════════════════════════════════════════════════════════════
# C：这一句接不接
# ══════════════════════════════════════════════════════════════════════

def test_silent() -> None:
    print("\n[接话] 什么时候不说话")
    for mark in ("-", "—", "算了", "不说了", "（不说话）"):
        check(f"「{mark}」= 不接", S.is_silent(mark), mark)
    check("带标点的横杠也认", S.is_silent("—。"))
    for normal in ("嗯", "刚看到", "在忙吗", "知道了，你早点睡"):
        check(f"「{normal}」= 正常回", not S.is_silent(normal), normal)
    check("「算了，我先去忙」照发", not S.is_silent("算了，我先去忙"))


# ══════════════════════════════════════════════════════════════════════
# A：拆出补的那句
# ══════════════════════════════════════════════════════════════════════

def test_split() -> None:
    print("\n[补一句] 从输出里拆")
    m, a = S.split_afterthought("哦\n\n补：吃了吗")
    check("拆出主回复", m == "哦", repr(m))
    check("拆出补的那句", a == "吃了吗", repr(a))

    m2, a2 = S.split_afterthought("知道了，你早点睡")
    check("没有「补：」时整段是主回复", m2 == "知道了，你早点睡" and a2 == "")

    m3, a3 = S.split_afterthought("哦\n\n补：-")
    check("补的那句写 - 等于不补", a3 == "" and m3 == "哦")

    m4, a4 = S.split_afterthought("嗯\n补：那你先忙")
    check("单换行也认", a4 == "那你先忙", repr(a4))

    m5, a5 = S.split_afterthought("哦\n补：吃了吗\n补：早点睡")
    check("只取第一句「补」", a5 == "吃了吗", repr(a5))


# ══════════════════════════════════════════════════════════════════════
# 六道门槛
# ══════════════════════════════════════════════════════════════════════

class Always:
    """掷骰永远中。"""
    def random(self):
        return 0.0


class Never:
    """掷骰永远不中。"""
    def random(self):
        return 0.99


def test_gates() -> None:
    print("\n[补一句] 六道门槛（生成宽松，发送严格）")
    now = 1_800_000_000.0

    ok, why = S.allow_afterthought("哦", ["哦"], now=now, rng=Always())
    check("短句 + 单条 + 不是问句 → 补", ok, why)

    ok, why = S.allow_afterthought("", ["哦"], now=now, rng=Always())
    check("主回复空 → 不补", not ok)

    ok, why = S.allow_afterthought("知道了，你早点睡明天还上班", ["知道了，你早点睡明天还上班"],
                                   now=now, rng=Always())
    check("主回复够长 → 不补（说完了）", not ok, why)
    check("理由写清了字数", "字" in why, why)

    ok, why = S.allow_afterthought("嗯", ["嗯", "好"], now=now, rng=Always())
    check("已经分了 2 条 → 不补", not ok, why)

    ok, why = S.allow_afterthought("吃了吗", ["吃了吗"], now=now, rng=Always())
    check("以问句结尾 → 不补（球踢出去了）", not ok, why)
    ok, why = S.allow_afterthought("你吃了吗", ["你吃了吗"], now=now, rng=Always())
    check("以「吗」结尾 → 不补", not ok, why)

    ok, why = S.allow_afterthought("哦", ["哦"], now=now,
                                   last_after_at=now - 3600, rng=Always())
    check("1 小时前补过 → 不补", not ok, why)
    ok, why = S.allow_afterthought("哦", ["哦"], now=now,
                                   last_after_at=now - 7 * 3600, rng=Always())
    check("7 小时前补过 → 可以补", ok, why)

    ok, why = S.allow_afterthought("哦", ["哦"], now=now, recent_count=2, rng=Always())
    check("最近 2 条内补过 → 不补", not ok, why)
    ok, why = S.allow_afterthought("哦", ["哦"], now=now, recent_count=3, rng=Always())
    check("隔了 3 条 → 可以补", ok, why)

    ok, why = S.allow_afterthought("哦", ["哦"], now=now, rng=Never())
    check("过完所有门槛还要掷骰", not ok and "掷骰" in why, why)


# ══════════════════════════════════════════════════════════════════════
# 冲突：补一句和别的事打架
# ══════════════════════════════════════════════════════════════════════

class FakeSender:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, chat_name: str, text: str):
        self.sent.append((chat_name, text))
        return {"ok": True}


class FakeLife:
    def __init__(self, asleep: bool = False):
        self.asleep = asleep


def make_engine(*, sender=None, dry=False, life=None):
    """只造 _flush_after 会用到的那几样。"""
    e = D.EngineWorker.__new__(D.EngineWorker)
    e._after = {}
    e._peer_seq = {"c1": 5}
    e.dry = dry
    e.sender = sender
    e._life = life
    e.soul = None
    e.reply_count = 0
    e._talk_turns = 0
    e._last_after_at = 0.0
    e._replies_since_after = 5
    e.logs = []
    e._log = lambda text, kind="": e.logs.append(text)   # noqa: E731
    return e


def queue(e, *, at: float, sent_at: float, peer_seq: int = 5) -> None:
    e._after = {"text": "吃了吗", "at": at, "chat_id": "c1",
                "chat_name": "老爸", "sent_at": sent_at, "peer_seq": peer_seq}


def test_flush_conflicts() -> None:
    print("\n[冲突] 补一句和别的事打架时，先保别的")
    now = 1_800_000_000.0

    # 正常
    s = FakeSender()
    e = make_engine(sender=s)
    queue(e, at=now - 1, sent_at=now - 10)
    e._flush_after(now)
    check("到点了就发", len(s.sent) == 1, str(s.sent))
    check("发的是补的那句", s.sent and s.sent[0][1] == "吃了吗")
    check("发完清空队列", not e._after)
    check("记了补一句的时刻", e._last_after_at == now)

    # 1. 对方说话了
    s = FakeSender()
    e = make_engine(sender=s)
    queue(e, at=now - 1, sent_at=now - 10, peer_seq=4)
    e._peer_seq["c1"] = 5       # 这个会话期间又收到一条
    e._flush_after(now)
    check("对方说话了 → 不发", not s.sent, str(s.sent))
    check("说明原因", any("对方已经说话" in x for x in e.logs), str(e.logs))

    # 2. 她睡了
    s = FakeSender()
    e = make_engine(sender=s, life=FakeLife(asleep=True))
    queue(e, at=now - 1, sent_at=now - 10)
    e._flush_after(now)
    check("她睡了 → 不发", not s.sent, str(s.sent))
    check("说明原因", any("她睡了" in x for x in e.logs), str(e.logs))

    # 3. 拖太久
    s = FakeSender()
    e = make_engine(sender=s)
    queue(e, at=now - 1, sent_at=now - 200)
    e._flush_after(now)
    check("超过 90 秒 → 不发", not s.sent, str(s.sent))
    check("说明原因", any("拖太久" in x for x in e.logs), str(e.logs))

    # 4. 预演
    s = FakeSender()
    e = make_engine(sender=s, dry=True)
    queue(e, at=now - 1, sent_at=now - 10)
    e._flush_after(now)
    check("预演 → 不发", not s.sent, str(s.sent))
    check("预演写进日志", any("本来会补" in x for x in e.logs), str(e.logs))

    # 5. UIA 没就绪
    e = make_engine(sender=None)
    queue(e, at=now - 1, sent_at=now - 10)
    e._flush_after(now)
    check("UIA 未就绪 → 不发", not e._after)
    check("说明原因", any("UIA" in x for x in e.logs), str(e.logs))

    # 6. 还没到点
    s = FakeSender()
    e = make_engine(sender=s)
    queue(e, at=now + 10, sent_at=now)
    e._flush_after(now)
    check("没到点 → 留着不发", not s.sent and bool(e._after), str(e._after))

    # 7. 队列空时什么都不做
    s = FakeSender()
    e = make_engine(sender=s)
    e._flush_after(now)
    check("没有待发的 → 什么都不做", not s.sent)


def main() -> int:
    print("=" * 56)
    print("接话体检：不接 / 补一句 / 冲突")
    print("=" * 56)
    test_silent()
    test_split()
    test_gates()
    test_flush_conflicts()

    print()
    print("=" * 56)
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
