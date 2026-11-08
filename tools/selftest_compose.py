"""契约体检：主动开口和正常回复**必须是同一个人的口吻**。

跑法：
    python tools/selftest_compose.py

不需要微信、不调模型。用假的引擎对象喂 `EngineWorker._compose()`，
比较"回消息"和"主动开口"两种模式拼出来的东西。

## 为什么需要这个文件

0.9.2 之前，主动开口用的是另一套 prompt（`build_judge_request`）：
人格 + 8 条历史，没有记忆、没有心情、没有生活状态。

同一件事的两种说法，人格密度完全不同 —— 用户会感觉到"她主动说的那句
不像她"。这种漂移不会报错，只会让人觉得"主动说话这个功能有点假"。

所以这里守一条底线：

    **两种模式的 system 段必须完全一样。**
    差别只允许出现在 user 段最后那条「## 这一轮」的指令上。

哪一天有人给正常回复加了一段、忘了给主动加，这一项就会红。
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import desktop as D  # noqa: E402
from bigfish import soul as S  # noqa: E402

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


class FakeEngine:
    """只给 _compose 真正会用到的那几样。"""

    def __init__(self, soul):
        self.soul = soul
        self._life = None
        self.mcfg = None
        self.reader = None
        self.owner_id = ""
        self.logs: list[str] = []

    def _log(self, text: str, kind: str = "") -> None:
        self.logs.append(text)

    def _peer_name_parts(self, chat_id, chat_name, sender_name=""):
        # 用真实实现：称呼规则本身也是"她是谁"的一部分，
        # 假一个回去，这条自检就测不到「对四条丶却叫爸」那类错。
        return D.EngineWorker._peer_name_parts(
            self, chat_id, chat_name, sender_name)


def main() -> int:
    print("=" * 56)
    print("拼装体检：主动开口 vs 正常回复")
    print("=" * 56)

    try:
        soul = S.load_soul()
    except Exception as exc:
        print(f"\n  跳过：读不到人格（{exc}）")
        return 0
    if not soul:
        print("\n  跳过：没有 soul.json")
        return 0

    e = FakeEngine(soul)
    kw = dict(chat_id="wxid_selftest", chat_name="测试对象", sender_name="",
              incoming="你今天吃了吗", is_group=False,
              hist=[("测试对象", "你今天吃了吗"), ("我", "吃了")],
              owner_raw="wxid_selftest", is_owner_chat=True,
              owner_id="wxid_selftest")

    print("\n[正常] 回消息")
    a = D.EngineWorker._compose(e, **kw)
    print("\n[主动] 先开口")
    b = D.EngineWorker._compose(
        e, **{**kw, "incoming": "",
              "proactive_reason": "没什么具体的事，就是忽然想找他说句话"})

    print()
    check("两种模式都能拼出来",
          bool(a["sys_parts"]) and bool(b["sys_parts"]),
          f"{len(a['sys_parts'])} / {len(b['sys_parts'])}")
    check("没有异常被 log 下来", not [x for x in e.logs if "失败" in x or "错误" in x],
          str(e.logs))

    # ── 核心：system 必须一模一样 ──
    check("system 段数相同", len(a["sys_parts"]) == len(b["sys_parts"]),
          f"{len(a['sys_parts'])} vs {len(b['sys_parts'])}")
    same = (a["sys_parts"] == b["sys_parts"])
    check("system 内容逐段相同（人格/记忆/心情/生活/说法/守卫都在）", same,
          "第 "
          + ",".join(str(i) for i, (x, y) in enumerate(zip(a["sys_parts"], b["sys_parts"]))
                     if x != y)
          + " 段不一样")

    # ── 正常回复该有的东西 ──
    ua = "\n".join(a["user_parts"])
    ub = "\n".join(b["user_parts"])
    check("正常模式让「回一条消息」", "回一条消息" in ua)
    check("正常模式没有「先开口」", "自己想先开口" not in ua)

    # ── 主动该有的东西 ──
    check("主动模式让「先开口」", "自己想先开口" in ub)
    check("主动模式没有「回一条消息」", "回一条消息" not in ub)
    check("主动模式带上了起因", "忽然想找他说句话" in ub,
          ub[:120])

    # ── 记忆/印象这些 user 段两边都该有 ──
    check("两种模式都带「最近的对话」",
          "最近的对话" in ua and "最近的对话" in ub)
    check("system 里有人格", any("你是「" in s for s in a["sys_parts"]),
          a["sys_parts"][0][:50] if a["sys_parts"] else "")
    # 插件段：生活（时间）/ 守卫 必须出现（说法能不能出现取决于说话长度）
    check("system 里有「现在」（生活插件 870）",
          any("## 现在" in s for s in a["sys_parts"]),
          str([s[:20] for s in a["sys_parts"]]))
    check("system 里有末尾守卫（9990）",
          any("资料" in s and "要求" in s for s in a["sys_parts"]),
          str([s[:20] for s in a["sys_parts"]]))

    # ── 称呼必须按会话走 ──
    #
    # 2026-11-08 用户发现：四条丶问「你主人是谁」，心情日志写成
    # 「爸突然问她主人是谁」。根因是 soul.address（她对主人的称呼）
    # 被当成所有人的称呼。这三条守的就是它。
    from bigfish import memory as _M

    e.owner_id = _M.owner_of_private("wxid_selftest")
    addr = (soul.address or "").strip()
    if addr:
        check("主人私聊用她平时叫他的那个称呼",
              e._peer_name_parts("wxid_selftest", "老爸", "") == addr,
              e._peer_name_parts("wxid_selftest", "老爸", ""))
    check("其他人用会话名，不是「爸」",
          e._peer_name_parts("wxid_other", "四条丶", "") == "四条丶",
          e._peer_name_parts("wxid_other", "四条丶", ""))
    check("群聊用说话人",
          e._peer_name_parts("123@chatroom", "某个群", "四条丶") == "四条丶",
          e._peer_name_parts("123@chatroom", "某个群", "四条丶"))
    if addr:
        # 人格段里那句「称呼对方「爸」」必须被换掉，否则她会对着四条丶叫爸
        c = D.EngineWorker._compose(
            e, **{**kw, "chat_id": "wxid_other", "chat_name": "四条丶",
                  "is_owner_chat": False, "owner_id": e.owner_id})
        head = c["sys_parts"][0] if c["sys_parts"] else ""
        check("人格段里的称呼跟着会话换",
              f"称呼对方「{addr}」" not in head and "四条丶" in head,
              head[:80])

    # ── 出口 ──
    from bigfish import proactive as P

    check("她说「-」就是不说话", P.clean_reply("-") == "")
    check("一句话原样取出", P.clean_reply("刚看到") == "刚看到")

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
