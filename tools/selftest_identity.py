"""身份判据回归测试 —— 11-04 自环事故的第一个根因。

跑法：
    python tools/selftest_identity.py

不需要微信、不需要桌面、不需要网络，也不写真实数据目录
（证据链那一节把落盘目录换成了临时目录）。

## 为什么有这份测试

原来的判据是 `is_self = (sender == me) if sender else False`。
它有三种静默失手，**全都倒向"当成对方说的"**：

  · sender 解析不出 → 当成对方
  · me 读不到       → 每条都当成对方
  · 群里分片缺失    → 同上

"当成对方"意味着要回复，所以任何一次失手都是正反馈的起点。
而且它返回 bool，调用方拿不到"我其实不知道"这个信息。

覆盖：
  1  me 缺失 → UNKNOWN（不是"对方"）
  2  sender 与自己相同 → SELF
  3  sender 与自己不同 → PEER
  4  群正文前缀 wxid_自己 → SELF
  5  群正文前缀 wxid_别人 → PEER
  6  前缀不是 wxid（notes: …）→ 不能当成人名
  7  sender 空且无前缀 → UNKNOWN
  8  私聊 UNKNOWN → 不回（保守）
  9  群聊 UNKNOWN + at: → 回（宽松但有门槛）
 10  群聊 UNKNOWN + called: → 回
 11  群聊 UNKNOWN + group-allow-all → **不**回（免检不算叫她）
 12  SELF + allow_self 关 → 不回
 13  SELF + allow_self 开 → 回（自测通道）
 14  PEER → 回（两种场景）
 15  require_me 空串抛 IdentityUnavailable，非空原样返回
 16  每条判定都带 rule 与 evidence
 17  大小写差异不能让"自己的"变成"对方的"
 18  证据链：干净的判定不落盘，UNKNOWN 落盘
 19  证据链：写失败不影响返回
 20  证据链：滚动保留，只留最近 N 份
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import identity as I          # noqa: E402
from bigfish import identity_trace as T    # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []

ME = "wxid_me_fixture"
PEER = "wxid_dad_fixture"


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
        print(f"  {MARK_FAIL} {name}" + (f"  <- {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n[{title}]")


# ══════════════════════════════════════════════════════════════════
#  1-7 判定
# ══════════════════════════════════════════════════════════════════

def test_decide() -> None:
    section("1 me 缺失")
    v = I.decide(PEER, "", text="你好")
    check("结论是 UNKNOWN 而不是对方", v.who == I.WHO_UNKNOWN, v.who)
    check("规则码是 me_missing", v.rule == "me_missing", v.rule)
    check("不是确定结论", v.certain is False)

    section("2/3 按 sender 字段判")
    v = I.decide(ME, ME, text="随便")
    check("sender 与自己相同 → 自己", v.who == I.WHO_SELF and v.is_self, v.who)
    check("规则码 sender_is_me", v.rule == "sender_is_me", v.rule)
    v = I.decide(PEER, ME, text="随便")
    check("sender 与自己不同 → 对方", v.who == I.WHO_PEER, v.who)
    check("规则码 sender_is_other", v.rule == "sender_is_other", v.rule)

    section("4/5 群正文前缀")
    v = I.decide("", ME, text=f"{ME}: 我发的话")
    check("前缀是自己 → 自己", v.who == I.WHO_SELF, v.who)
    check("规则码 prefix_is_me", v.rule == "prefix_is_me", v.rule)
    check("证据来源标 text_prefix",
          any(src == "text_prefix" for src, _v in v.evidence), str(v.evidence))

    v = I.decide("", ME, text=f"{PEER}: 老爸说的话")
    check("前缀是别人 → 对方", v.who == I.WHO_PEER, v.who)
    check("规则码 prefix_is_other", v.rule == "prefix_is_other", v.rule)

    section("6 前缀必须严格")
    v = I.decide("", ME, text="notes: 今天要做的事")
    check("英文单词加冒号不能当成人名", v.who == I.WHO_UNKNOWN, v.who)
    check("规则码 sender_empty", v.rule == "sender_empty", v.rule)

    v = I.decide("", ME, text="我的微信号: 123")
    check("中文前缀也不认", v.who == I.WHO_UNKNOWN, v.who)

    section("7 两样都没有")
    v = I.decide("", ME, text="")
    check("没 sender 没前缀 → UNKNOWN", v.who == I.WHO_UNKNOWN, v.who)

    section("17 大小写")
    v = I.decide(ME.upper(), ME, text="x")
    check("大写自己仍算自己（不能让自环从大小写漏过去）",
          v.who == I.WHO_SELF, v.who)
    check("证据保留原值", any(val == ME.upper() for _s, val in v.evidence),
          str(v.evidence))

    section("16 每条判定都带依据")
    samples = [
        I.decide(ME, ME, text="a"),
        I.decide(PEER, ME, text="b"),
        I.decide("", ME, text=f"{ME}: c"),
        I.decide("", ME, text="d"),
        I.decide("", "", text=""),
    ]
    check("rule 都不为空", all(s.rule for s in samples))
    check("evidence 都不为空", all(s.evidence for s in samples))


# ══════════════════════════════════════════════════════════════════
#  8-14 放行
# ══════════════════════════════════════════════════════════════════

def test_admit() -> None:
    unknown = I.decide("", ME, text="")
    own = I.decide(ME, ME, text="x")
    peer = I.decide(PEER, ME, text="x")

    section("8 私聊 UNKNOWN → 不回")
    d = I.admit(unknown, is_group=False)
    check("私聊拿不准：不处理", d.proceed is False, str(d))
    check("原因码 unknown_private", d.reason == "unknown_private", d.reason)

    section("9/10 群聊 UNKNOWN + 真的叫了她 → 回")
    d = I.admit(unknown, is_group=True, woken=True)
    check("群里被 @ 且拿不准：当对方处理", d.proceed is True, str(d))
    check("原因码 unknown_group_woken", d.reason == "unknown_group_woken")

    check("at: 算叫她", I.woken_from_reason("at:老爸") is True)
    check("called: 算叫她", I.woken_from_reason("called:大肥鱼") is True)

    section("11 免检不算叫她")
    check("group-allow-all 不算 woken",
          I.woken_from_reason("group-allow-all") is False)
    check("group-not-mentioned 不算",
          I.woken_from_reason("group-not-mentioned") is False)
    d = I.admit(unknown, is_group=True,
                woken=I.woken_from_reason("group-allow-all"))
    check("群里没叫她且拿不准：不回", d.proceed is False, str(d))
    check("原因码 unknown_group", d.reason == "unknown_group", d.reason)

    section("12/13 SELF 由 allow_self 决定")
    d = I.admit(own, is_group=False, allow_self=False)
    check("自己发的 + 没开自测：不回", d.proceed is False)
    check("原因码 self", d.reason == "self")
    d = I.admit(own, is_group=False, allow_self=True)
    check("自己发的 + 开了自测：回", d.proceed is True)
    check("原因码 self_test", d.reason == "self_test")
    d = I.admit(own, is_group=True, allow_self=True)
    check("群里自测通道同样生效", d.proceed is True)

    section("14 PEER 一律回")
    check("私聊对方：回", I.admit(peer, is_group=False).proceed is True)
    check("群聊对方：回", I.admit(peer, is_group=True).proceed is True)
    check("对方的消息不受 allow_self 影响",
          I.admit(peer, is_group=False, allow_self=False).proceed is True)


# ══════════════════════════════════════════════════════════════════
#  15 身份前置
# ══════════════════════════════════════════════════════════════════

def test_require_me() -> None:
    section("15 require_me")
    check("非空原样返回", I.require_me(" wxid_x ") == "wxid_x")
    for bad in ("", None, "   "):
        try:
            I.require_me(bad)
            check(f"{bad!r} 应该抛异常", False, "没有抛")
        except I.IdentityUnavailable as exc:
            check(f"{bad!r} 抛 IdentityUnavailable 且带人话",
                  exc.code == "identity_me_missing" and "不启动监听" in exc.reason,
                  exc.reason[:40])

    class _Reader:
        wxid = "wxid_from_reader"

    check("能从 reader 上取到", I.self_id_of(_Reader()) == "wxid_from_reader")

    class _DB:
        wxid = "wxid_from_db"

    class _Reader2:
        wxid = ""
        _db = _DB()

    check("reader 没有就退到 _db", I.self_id_of(_Reader2()) == "wxid_from_db")
    check("都没有返回空串", I.self_id_of(object()) == "")


# ══════════════════════════════════════════════════════════════════
#  18-20 证据链
# ══════════════════════════════════════════════════════════════════

def test_trace() -> None:
    section("18 只记不干净的判定")
    written: list = []
    own = I.decide(ME, ME, text="x")
    peer = I.decide(PEER, ME, text="x")
    unknown = I.decide("", ME, text="")

    check("确定的自己：不写",
          T.record(own, I.admit(own), chat_id="c", me=ME,
                   writer=written.append) is None)
    check("确定的对方：不写",
          T.record(peer, I.admit(peer), chat_id="c", me=ME,
                   writer=written.append) is None)
    T.record(unknown, I.admit(unknown), chat_id="wxid_c", me=ME,
             text="看不清的一条", writer=written.append)
    check("拿不准：写了一条", len(written) == 1, str(len(written)))

    # 没写出来时后面全部按空字典判，让每一条都干净地红，
    # 而不是在这里抛 IndexError 把整份测试带崩。
    item = written[0] if written else {}
    verdict_dict = item.get("verdict") or {}
    decision_dict = item.get("decision") or {}
    check("带判定结论", verdict_dict.get("who") == I.WHO_UNKNOWN, str(verdict_dict))
    check("带规则码", verdict_dict.get("rule") == "sender_empty", str(verdict_dict))
    check("带放行结论", decision_dict.get("reason") == "unknown_private",
          str(decision_dict))
    check("带规则版本（以后改判据能区分新旧记录）",
          item.get("rule_version") == T.RULE_VERSION, str(item.get("rule_version")))
    check("带正文摘录", str(item.get("excerpt", "")).startswith("看不清"),
          str(item.get("excerpt")))
    check("不改写配置路径", item.get("chat") == "wxid_c", str(item.get("chat")))

    section("19 写失败不影响返回")
    def boom(_payload):
        raise OSError("盘满了")

    try:
        got = T.record(unknown, I.admit(unknown), writer=boom)
        check("写失败时静默返回，不抛", got is None, str(got))
    except Exception as exc:                     # noqa: BLE001
        check("写失败时静默返回，不抛", False, f"抛了 {type(exc).__name__}")

    section("20 滚动保留")
    tmp = tempfile.mkdtemp(prefix="identity-trace-")
    try:
        T.trace_dir = lambda: tmp
        T.keep_count = lambda: 3
        for i in range(6):
            T.record(unknown, I.admit(unknown), chat_id=f"c{i}")
        left = [n for n in os.listdir(tmp) if n.endswith(".json")]
        check("只留最近 3 份", len(left) == 3, str(len(left)))
        newest = sorted(left)[-1] if left else ""
        check("留下的是最新的那几份", "c5" in newest, newest or "(一份都没写)")
        items = T.recent(2)
        check("recent 读出 2 份且新的在前",
              len(items) == 2 and "c5" in (items[0].get("_file") or ""),
              str([i.get("_file") for i in items]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    print("身份判据回归测试（bigfish/identity.py）")
    test_decide()
    test_admit()
    test_require_me()
    test_trace()

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
