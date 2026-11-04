"""自环防护回归测试 —— 2026-11-04 事故的护栏。

跑法：
    python tools/selftest_loopguard.py

不需要微信、不需要桌面、不需要网络。假时钟直接跳时间，不 sleep。

## 事故

2026-11-04 20:27~20:30，大肥鱼在「老爸」私聊里自问自答十几条停不下来。
根因四条：allow_self 泄漏到生产会话、记账晚于写库、没有轮级熔断、
防护埋在闭包里没有测试。这个文件就是为了让第一条和第三条永远回不来。

覆盖：
  · 归一化：群前缀 / @昵称
  · 生产默认：自己发的直接丢
  · allow_self 只对 filehelper 生效（事故修复的核心）
  · 乐观记账：发了再读回 → echo
  · 回滚：没发出去的不该记着
  · 对方复述她的话 → 绝不能误杀
  · 记账窗口过期
  · 轮级熔断：连续 4 轮无对方消息 → 静音
  · 分条的多 part 只算一轮（不误伤）
  · 中间有对方消息 → 计数清零
  · 静音到期自动恢复
  · 【核心】事故复现：allow_self 开着、记账全部失手，看她还能不能成环
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import loopguard as LG  # noqa: E402

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
    def __init__(self, t: float = 1000.0) -> None:
        self.now = t

    def __call__(self) -> float:
        return self.now

    def tick(self, dt: float) -> None:
        self.now += dt


class Msg:
    """最小假消息：只带 LoopGuard 会读的三个字段。"""

    def __init__(self, chat: str, text: str, is_self: bool) -> None:
        self.chat_id = chat
        self.text = text
        self.is_self = is_self


def guard_with(clock: Clock, **kw) -> LG.LoopGuard:
    kw.setdefault("clock", clock)
    return LG.LoopGuard(**kw)


# ══════════════════════════════════════════════════════════════════
def test_normalize() -> None:
    section("归一化")
    check("纯文本原样", LG.normalize("你好") == "你好")
    check("剥群前缀", LG.normalize("wxid_abc123: 你好") == "你好")
    check("剥群号前缀", LG.normalize("12345678@chatroom: 你好") == "你好")
    check("剥 @", LG.normalize("@老爸 你好") == "你好")
    check("剥多个 @", LG.normalize("@老爸 @老妈 你好") == "你好")
    check("剥两者", LG.normalize("wxid_abc123: @老爸 你好") == "你好")
    check("空串", LG.normalize("") == "")
    check("只有空白", LG.normalize("   ") == "")


def test_self_gate() -> None:
    section("L1 白名单：allow_self 只对 filehelper 生效")
    clock = Clock()

    # 生产默认：关着
    g = guard_with(clock)
    v = g.admit(Msg("wxid_dad", "我发的", True))
    check("默认关：自己发的被拒", not v.allow and v.reason == "self", f"{v}")

    # 事故复现的开关：开着
    g = guard_with(clock, allow_self=True)
    v = g.admit(Msg("wxid_dad", "我发的", True))
    check("开着 + 私聊：仍然被拒（事故修复）",
          not v.allow and v.reason == "self", f"{v}")

    v = g.admit(Msg("54268332003@chatroom", "我发的", True))
    check("开着 + 群聊：仍然被拒",
          not v.allow and v.reason == "self", f"{v}")

    # 自测通道还能用
    v = g.admit(Msg("filehelper", "我发的", True))
    check("开着 + 文件传输助手：放行", v.allow, f"{v}")

    # 对方的消息永远放行
    v = g.admit(Msg("wxid_dad", "老爸说的话", False))
    check("对方消息：放行", v.allow, f"{v}")


def test_echo() -> None:
    section("L2 乐观记账")
    clock = Clock()
    g = guard_with(clock, allow_self=True)

    g.note_sent("知道了")
    v = g.admit(Msg("filehelper", "知道了", True))
    check("发过再读回 → echo", not v.allow and v.reason == "echo", f"{v}")

    # 回滚
    g2 = guard_with(clock, allow_self=True)
    g2.note_sent("没发出去的话")
    g2.rollback_sent("没发出去的话")
    v = g2.admit(Msg("filehelper", "没发出去的话", True))
    check("回滚后不再 echo", v.allow, f"{v}")

    # 关键：对方复述
    g3 = guard_with(clock, allow_self=True)
    g3.note_sent("行路难，欲渡黄河冰塞川")
    v = g3.admit(Msg("wxid_dad", "行路难，欲渡黄河冰塞川", False))
    check("对方原样复述 → 放行（不能误杀）", v.allow, f"{v}")

    # 前缀匹配的边界
    g4 = guard_with(clock, allow_self=True)
    g4.note_sent("那还是叫大肥鱼")
    v = g4.admit(Msg("filehelper", "那还是叫大肥鱼吧", True))
    check("短于 12 字的前缀不判 echo", v.allow, f"{v}")

    g5 = guard_with(clock, allow_self=True)
    g5.note_sent("今天下午我们一起去长坂坡看樱花吧")
    v = g5.admit(Msg("filehelper", "今天下午我们一起去长坂坡看樱花吧然后吃烧烤", True))
    check("长文本前缀命中 → echo", not v.allow and v.reason == "echo", f"{v}")

    # 窗口过期
    g6 = guard_with(clock, allow_self=True, echo_window=12.0)
    g6.note_sent("很久以前说的话")
    clock.tick(13.0)
    v = g6.admit(Msg("filehelper", "很久以前说的话", True))
    check("超出窗口 → 不再 echo", v.allow, f"{v}")


def test_trip() -> None:
    section("L3 轮级熔断")
    clock = Clock()
    g = guard_with(clock, trip=4, mute=120.0)

    check("1 轮不熔断", g.note_reply("c1") is False)
    check("2 轮不熔断", g.note_reply("c1") is False)
    check("3 轮不熔断", g.note_reply("c1") is False)
    check("4 轮熔断", g.note_reply("c1") is True)

    v = g.admit(Msg("c1", "对方说的话", False))
    check("熔断后对方消息也被静音",
          not v.allow and v.reason == "muted", f"{v}")

    clock.tick(121.0)
    v = g.admit(Msg("c1", "对方说的话", False))
    check("静音到期自动恢复", v.allow, f"{v}")

    # 中间有对方消息 → 清零
    g2 = guard_with(clock, trip=4)
    g2.note_reply("c2")
    g2.note_reply("c2")
    g2.admit(Msg("c2", "老爸说话", False))       # 对方介入
    check("对方介入后 1 轮不熔断", g2.note_reply("c2") is False)
    check("对方介入后 2 轮不熔断", g2.note_reply("c2") is False)

    # 分条不误伤：一轮 reply 发 3 个 part，note_sent 三次但 note_reply 一次
    g3 = guard_with(clock, trip=4)
    for _ in range(10):
        g3.note_sent("第一句")
        g3.note_sent("第二句")
        g3.note_sent("第三句")
    check("分条记账不触发熔断", g3.muted_for("c3") == 0.0)

    # 多会话隔离
    g4 = guard_with(clock, trip=2)
    g4.note_reply("a")
    g4.note_reply("a")            # a 熔断
    check("a 已静音", g4.muted_for("a") > 0)
    check("b 不受影响", g4.muted_for("b") == 0.0)
    v = g4.admit(Msg("b", "另一边的消息", False))
    check("b 仍能正常收", v.allow, f"{v}")


def test_incident_replay() -> None:
    section("事故复现：2026-11-04 那晚")
    # 场景完全照着日志搭：
    #   allow_self 开着（线上就是 True），私聊「老爸」，她分 3 条发出去，
    #   每条都被 watcher 读回来当成对方说的。
    clock = Clock()
    g = guard_with(clock, allow_self=True, trip=4, mute=120.0)

    rounds = 0
    for _ in range(30):                      # 模拟 30 个轮询周期
        # 读回一条自己的消息
        v = g.admit(Msg("wxid_dad", "拔剑四顾心茫然……", True))
        if v.allow:
            # 她回了 —— 分 3 条发
            for part in ("拔剑四顾心茫然……", "这句我背得还行吧", "嗯"):
                g.note_sent(part)
            g.note_reply("wxid_dad")
            rounds += 1
        clock.tick(1.2)

    check("事故场景下只回了 0 轮（L1 直接拦住）", rounds == 0, f"rounds={rounds}")
    check("没有进入熔断（根本不需要）",
          g.muted_for("wxid_dad") == 0.0, f"{g.snapshot()}")

    # 第二场景：假设 L1 被判错（allow_self 也放行了私聊），
    # 且 L2 记账全部失手 —— 只剩熔断兜底。
    clock2 = Clock()
    g2 = guard_with(clock2, trip=4, mute=120.0)
    g2.self_chat_ids = frozenset({"wxid_dad"})     # 人为放开
    g2.allow_self = True

    rounds2 = 0
    tripped_at = None
    for i in range(20):
        # L2 失手：读回来的文本和记账文本永远对不上
        v = g2.admit(Msg("wxid_dad", f"回读第{i}条", True))
        if v.allow:
            g2.note_sent(f"完全不匹配的文本{i}")
            if g2.note_reply("wxid_dad"):
                tripped_at = rounds2 + 1
            rounds2 += 1
        clock2.tick(1.2)

    check("L1/L2 全失手时，熔断在第 4 轮掐住",
          tripped_at == 4, f"tripped_at={tripped_at}")
    check("总共只回了 4 轮就停", rounds2 <= 5, f"rounds2={rounds2}")


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    print("自环防护回归测试（bigfish/loopguard.py）")
    test_normalize()
    test_self_gate()
    test_echo()
    test_trip()
    test_incident_replay()

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
