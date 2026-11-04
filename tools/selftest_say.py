"""说法层回归测试：分条、节奏、发送确认。

跑法：
    python tools/selftest_say.py

不需要微信、不需要桌面、不需要 exe、不需要网络。
发送确认用假时钟 + 假 sender/reader，整轮瞬间跑完。

覆盖的每一件事，都是"以前做不到"或"以前会静默错"的：
  · 分条：以前一条长文塞完，现在按句/逗号/模型标记切
  · 保护：以前 URL、@、颜文字会被切断
  · 白名单：以前「好」会被并进上一句，现在能独立成条
  · 节奏：以前没有间隔，现在按字数给等待且可复现
  · 确认：以前 SendResult.ok 就当发成功，现在要库里出现自己的新行
  · 并行：确认不能额外占时间，总时长应 ≈ Σ gap
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import say as S  # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []


def _pick_marks() -> tuple[str, str]:
    """GBK 控制台画不出 ✓/✗，降级成 [OK]/[X]，不让自检因为编码跑不完。"""
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


# ══════════════════════════════════════════════════════════════════════
# 一、分条
# ══════════════════════════════════════════════════════════════════════

def test_split() -> None:
    section("分条")

    # 模型给了边界：听模型的（这是用户期望的 3 条形态）
    marked = "知道了\n{{split}}\n那明天就去长坂坡玩吧\n{{split}}\n咱们先看樱花，再看枣树"
    parts = S.split_reply(marked)
    check("模型标记切成 3 条", len(parts) == 3, f"got {parts}")
    check("首条是短应答", parts[0] == "知道了", f"got {parts[0]!r}")
    check("标记本身不出现在正文", all("{{split}}" not in p for p in parts), f"got {parts}")

    # 没有标记：按逗号切（分条器不改写文本，所以是 4 条而不是 3 条）
    plain = "知道了，明天就去长坂坡游玩，先去看樱花，再去看枣树"
    parts = S.split_reply(plain)
    check("无标记按逗号切成 4 条", len(parts) == 4, f"got {parts}")
    check("「知道了」独立成条", parts[0].rstrip("，,") == "知道了", f"got {parts[0]!r}")
    check("切分不丢字", "".join(parts).replace("，", "") == plain.replace("，", ""), f"got {parts}")

    # 短句不该被逗号切碎
    short = "我知道了，没事"
    parts = S.split_reply(short)
    check("8 字短句不拆", len(parts) == 1, f"got {parts}")

    # 句末标点优先
    parts = S.split_reply("今天好累。我先睡了。明天见。")
    check("按句号切 3 条", len(parts) == 3, f"got {parts}")
    check("标点留在段尾", parts[0].endswith("。"), f"got {parts[0]!r}")

    # 保护：颜文字
    parts = S.split_reply("哈哈(｡･ω･｡)好可爱")
    check("颜文字不被切开", len(parts) == 1 and "(｡･ω･｡)" in parts[0], f"got {parts}")

    # 保护：URL
    url = "看这个 https://example.com/a?b=1&c=2 很好笑"
    parts = S.split_reply(url)
    check("URL 不被切开", len(parts) == 1 and "https://example.com/a?b=1&c=2" in parts[0], f"got {parts}")

    # 保护：@
    parts = S.split_reply("@小明 你好呀")
    check("@昵称不被切开", len(parts) == 1 and parts[0].startswith("@小明"), f"got {parts}")

    # 保护：数字+单位
    parts = S.split_reply("3 天就好了")
    check("数字与单位不被空格切开", len(parts) == 1, f"got {parts}")

    # 白名单：短应答独立成条
    parts = S.split_reply("好。我知道了")
    check("「好」独立成条", len(parts) == 2 and parts[0].rstrip("。") == "好", f"got {parts}")

    # 非白名单的短段并入前段
    parts = S.split_reply("我去。啊")
    check("非白名单短段并入前段", len(parts) == 1, f"got {parts}")

    # 超长段硬切
    parts = S.split_reply("啊" * 120)
    check("超长段切成多条", len(parts) == 3, f"got {len(parts)} parts")
    check("每条不超上限", all(len(p) <= 50 for p in parts), f"got {[len(p) for p in parts]}")

    # 条数收敛：不超过 max_parts，且各组长短接近（均衡分组）
    parts = S.split_reply("第一句话。第二句话。第三句话。第四句话。第五句话。第六句话。")
    check("6 句收敛到不超过 4 条", 2 <= len(parts) <= 4, f"got {parts}")
    check("收敛后长短均衡",
          max(map(len, parts)) - min(map(len, parts)) <= 4,
          f"{[len(p) for p in parts]}")

    # 空与纯标点
    check("空文本返回空列表", S.split_reply("") == [], "got non-empty")
    check("纯空白返回空列表", S.split_reply("   \n  ") == [], "got non-empty")

    # 连续标点不独立成条
    parts = S.split_reply("真的吗……？！我不信")
    check("连续标点跟着前一句", all(p.strip("…？！") for p in parts), f"got {parts}")

    # 自定义策略
    tight = S.SplitPolicy(max_parts=2)
    parts = S.split_reply("第一句话。第二句话。第三句话。", tight)
    check("自定义 max_parts=2 生效", len(parts) == 2, f"got {parts}")


def test_quote_and_llm() -> None:
    section("引号与 LLM 断句")

    # 引号内的逗号不切（别人引用的原话不该被截断）
    quoted = "他说「明天去长坂坡，后天回来」我答应了"
    parts = S.split_reply(quoted)
    check("引号内的逗号不切", len(parts) == 1, f"got {parts}")

    # 引号外的逗号照切
    parts = S.split_reply("他说「明天去」，我答应了，就这样定吧")
    check("引号外的逗号照切", len(parts) >= 2, f"got {parts}")

    # 括号内的逗号不切（旁白/补充）
    parts = S.split_reply("（明天去长坂坡，后天回来）这样安排最顺")
    check("括号内的逗号不切", len(parts) == 1, f"got {parts}")

    # 关掉保护后照切：证明开关真的起作用
    parts = S.split_reply(quoted, S.SplitPolicy(protect_quotes=False))
    check("关掉保护后引号内也切", len(parts) > 1, f"got {parts}")

    # 引号内的句号也不切
    parts = S.split_reply("她说「明天去。后天回。」我记住了")
    check("引号内的句号不切", len(parts) == 1, f"got {parts}")

    # LLM 断句：模型返回 3 行就用模型的
    def fake_llm(_text: str) -> str:
        return "知道了\n那明天去长坂坡吧\n先看樱花"

    long_text = "知道了，明天就去长坂坡游玩，先去看樱花，再去看枣树，顺便把上次说的那件事办了"
    parts = S.split_reply_with_llm(long_text, fake_llm, min_chars=10)
    check("LLM 断句用模型结果", len(parts) == 3 and parts[0] == "知道了", f"got {parts}")

    # LLM 抛异常 → 回退规则切分
    def boom(_text: str) -> str:
        raise RuntimeError("boom")

    parts = S.split_reply_with_llm(long_text, boom, min_chars=10)
    check("LLM 失败回退规则", len(parts) >= 1, f"got {parts}")

    # 短文本不调 LLM
    called = {"n": 0}

    def counting(_text: str) -> str:
        called["n"] += 1
        return "a\nb"

    S.split_reply_with_llm("短", counting, min_chars=60)
    check("短文本不调 LLM", called["n"] == 0, f"got {called['n']}")

    # 已带 marker 的文本不调 LLM
    S.split_reply_with_llm("一\n{{split}}\n二", counting, min_chars=1)
    check("有 marker 不调 LLM", called["n"] == 0, f"got {called['n']}")


# ══════════════════════════════════════════════════════════════════════
# 二、节奏
# ══════════════════════════════════════════════════════════════════════

def test_pace() -> None:
    section("节奏")

    parts = ["嗯", "好" * 40]
    gaps = S.plan_pacing(parts)
    check("条数与间隔数一致", len(gaps) == 2, f"got {gaps}")
    check("长句等得比短句久", gaps[1] > gaps[0], f"got {gaps}")
    check("首条在 0.6~1.8s", 0.6 <= gaps[0] <= 1.8, f"got {gaps[0]}")

    # 上限
    gaps = S.plan_pacing(["啊" * 300, "啊" * 300])
    check("单条间隔不超 cap×1.2", all(g <= 3.5 * 1.2 + 1e-6 for g in gaps), f"got {gaps}")

    # 总时长压缩
    gaps = S.plan_pacing(["啊" * 100] * 4, S.PacePolicy(total_cap=6.0))
    check("总间隔被压到 total_cap 以内", sum(gaps) <= 6.01, f"sum={sum(gaps):.2f}")

    # 确定性
    a = S.plan_pacing(["一二三", "四五六"], S.PacePolicy(seed="abc"))
    b = S.plan_pacing(["一二三", "四五六"], S.PacePolicy(seed="abc"))
    check("同 seed 结果可复现", a == b, f"{a} vs {b}")
    c = S.plan_pacing(["一二三", "四五六"], S.PacePolicy(seed="xyz"))
    check("不同 seed 抖动不同", a != c, f"{a} vs {c}")

    # fixed 模式
    gaps = S.plan_pacing(["甲", "乙", "丙"], S.PacePolicy(mode="fixed", base=1.2, jitter=0.0))
    check("fixed 模式恒定", gaps[1] == 1.2 and gaps[2] == 1.2, f"got {gaps}")

    check("空列表返回空", S.plan_pacing([]) == [], "got non-empty")


# ══════════════════════════════════════════════════════════════════════
# 三、发送确认
# ══════════════════════════════════════════════════════════════════════

class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))


@dataclass
class FakeMsg:
    local_id: int
    is_self: bool
    content: str
    at: float


@dataclass
class FakeResult:
    ok: bool
    detail: str = ""


class FakeReader:
    """按假时钟决定「这条落库了没有」。"""

    def __init__(self, clock: FakeClock, latency: float = 0.4) -> None:
        self.clock = clock
        self.latency = latency
        self.rows: list[FakeMsg] = []
        self._id = 0

    def schedule(self, text: str) -> None:
        self._id += 1
        self.rows.append(FakeMsg(self._id, True, text, self.clock.now + self.latency))

    def push_other(self, text: str) -> None:
        """模拟对方同时发消息：id 更大、is_self=False。"""
        self._id += 1
        self.rows.append(FakeMsg(self._id, False, text, self.clock.now))

    def read_recent(self, chat: str, limit: int) -> list[FakeMsg]:
        visible = [m for m in self.rows if m.at <= self.clock.now]
        visible.sort(key=lambda m: m.local_id)
        return visible[-limit:] if limit > 0 else visible


class FakeSender:
    def __init__(self, reader: FakeReader, clock: FakeClock, *,
                 fail_at: int | None = None, echo: bool = True,
                 rewrite=None) -> None:
        self.reader = reader
        self.clock = clock
        self.fail_at = fail_at
        self.echo = echo
        self.rewrite = rewrite
        self.calls: list[tuple[float, str]] = []

    def send(self, chat: str, text: str) -> FakeResult:
        self.calls.append((self.clock.now, text))
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            return FakeResult(False, "uia-boom")
        if self.echo:
            self.reader.schedule(self.rewrite(text) if self.rewrite else text)
        return FakeResult(True)


def _run(parts, gaps, sender, reader, clock, **kw):
    return S.send_parts(sender.send, reader.read_recent, "chat", parts, gaps,
                        clock=clock, sleep=clock.sleep, **kw)


def test_send() -> None:
    section("发送确认")

    # 1) 全部确认到 L3
    clock = FakeClock()
    reader = FakeReader(clock, latency=0.4)
    sender = FakeSender(reader, clock)
    out = _run(["知道了", "那明天去", "先看樱花"], [0.5, 1.0, 0.0], sender, reader, clock)
    check("三条全发", out.ok and out.parts_sent == 3, f"{out}")
    check("最高级别 L3", out.level == "L3", f"got {out.level}")
    check("sender 被调 3 次", len(sender.calls) == 3, f"got {len(sender.calls)}")

    # 2) L1 失败：立刻停，不继续发
    clock = FakeClock()
    reader = FakeReader(clock)
    sender = FakeSender(reader, clock, fail_at=1)
    out = _run(["一", "二", "三"], [0.5, 0.5, 0.0], sender, reader, clock)
    check("L1 失败标记 failed", not out.ok and out.level == "failed", f"{out}")
    check("L1 失败后不再发送", len(sender.calls) == 1, f"got {len(sender.calls)}")
    check("parts_sent=0", out.parts_sent == 0, f"got {out.parts_sent}")

    # 3) L2 超时：默认降级（继续发），strict 才停
    clock = FakeClock()
    reader = FakeReader(clock)
    sender = FakeSender(reader, clock, echo=False)
    out = _run(["一", "二"], [0.2, 0.0], sender, reader, clock, confirm_timeout=2.0)
    check("默认未确认仍发完", out.ok and out.parts_sent == 2, f"{out}")
    check("未确认条号被记录", out.unconfirmed == [1, 2], f"got {out.unconfirmed}")
    check("级别降为 L1", out.level == "L1", f"got {out.level}")

    clock = FakeClock()
    reader = FakeReader(clock)
    sender = FakeSender(reader, clock, echo=False)
    out = _run(["一", "二"], [0.2, 0.0], sender, reader, clock,
               confirm_timeout=2.0, strict_confirm=True)
    check("strict 模式未确认即停", not out.ok and out.level == "unconfirmed", f"{out}")
    check("strict 模式只发一条", len(sender.calls) == 1, f"got {len(sender.calls)}")

    # 4) 对方消息混入不能误判成功
    clock = FakeClock()
    reader = FakeReader(clock)
    sender = FakeSender(reader, clock, echo=False)
    for _ in range(3):
        reader.push_other("别人的话")
    out = _run(["一"], [0.0], sender, reader, clock,
               confirm_timeout=1.0, strict_confirm=True)
    check("对方消息不算确认", not out.ok and out.level == "unconfirmed", f"{out}")

    # 5) 群聊 @ 前缀不影响内容比对
    clock = FakeClock()
    reader = FakeReader(clock)
    sender = FakeSender(reader, clock, rewrite=lambda t: f"@小明 {t}")
    out = _run(["你好呀"], [0.0], sender, reader, clock)
    check("@ 前缀仍判 L3", out.ok and out.level == "L3", f"{out}")

    # 6) 内容对不上：只告警，继续发完
    clock = FakeClock()
    reader = FakeReader(clock)
    sender = FakeSender(reader, clock, rewrite=lambda t: "完全不同的另一句话")
    out = _run(["一", "二", "三"], [0.2, 0.2, 0.0], sender, reader, clock)
    check("内容不符仍发完", out.ok and out.parts_sent == 3, f"{out}")
    check("级别降为 L2", out.level == "L2", f"got {out.level}")
    check("三条都记了 mismatch", out.mismatches == [1, 2, 3], f"got {out.mismatches}")

    # 7) 确认与节奏并行：总耗时 ≈ Σ gap（不含最后一条）
    clock = FakeClock()
    reader = FakeReader(clock, latency=0.2)
    sender = FakeSender(reader, clock)
    gaps = [0.5, 1.0, 1.0]
    start = clock.now
    out = _run(["一", "二", "三"], gaps, sender, reader, clock)
    elapsed = clock.now - start
    check("并行确认不额外占时间", 1.4 <= elapsed <= 1.8, f"elapsed={elapsed:.2f}")

    # 8) 落库比 gap 慢：仍能在 confirm_timeout 内等到
    clock = FakeClock()
    reader = FakeReader(clock, latency=1.5)
    sender = FakeSender(reader, clock)
    out = _run(["一", "二"], [0.5, 0.0], sender, reader, clock, confirm_timeout=3.0)
    check("慢落库也能确认", out.ok and out.parts_sent == 2, f"{out}")

    # 9) 基线必须取在发送前：发送前已有 5 条历史
    clock = FakeClock()
    reader = FakeReader(clock, latency=0.3)
    for i in range(5):
        reader.schedule(f"历史{i}")
    clock.now = 10.0
    sender = FakeSender(reader, clock)
    out = _run(["新的"], [0.0], sender, reader, clock)
    check("旧消息不被当成确认", out.ok and out.level == "L3", f"{out}")

    # 10) send_text 一步到位
    clock = FakeClock()
    reader = FakeReader(clock, latency=0.2)
    sender = FakeSender(reader, clock)
    out = S.send_text(sender.send, reader.read_recent, "chat",
                      "知道了，明天就去长坂坡游玩，先去看樱花，再去看枣树",
                      clock=clock, sleep=clock.sleep)
    check("send_text 切成 4 条并全发", out.ok and out.parts_sent == 4, f"{out}")
    check("send_text 内容不丢", "".join(t for _at, t in sender.calls).replace("，", "")
          == "知道了明天就去长坂坡游玩先去看樱花再去看枣树", f"{[t for _at, t in sender.calls]}")

    # 11) 最后一条异步确认：发完立即返回，不等落库
    clock = FakeClock()
    reader = FakeReader(clock, latency=0.2)
    sender = FakeSender(reader, clock)
    out = _run(["一", "二"], [0.2, 0.0], sender, reader, clock,
               final_async_confirm=True)
    check("最后一条返回 pending", out.pending == ("二", 1), f"{out.pending}")
    check("最后一条不阻塞", clock.now < 1.0, f"elapsed={clock.now:.2f}")
    check("异步模式下仍发完两条", out.ok and out.parts_sent == 2, f"{out}")


# ══════════════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════════════

def main() -> int:
    print("=" * 66)
    print("说法层自检（分条 / 节奏 / 发送确认）")
    print("=" * 66)

    test_split()
    test_quote_and_llm()
    test_pace()
    test_send()

    print("\n" + "=" * 66)
    if FAIL:
        print(f"结果：{PASS}/{PASS + FAIL} 通过，{FAIL} 项失败")
        print("失败项：")
        for name in FAILED:
            print(f"  \u00b7 {name}")
        return 1
    print(f"结果：{PASS}/{PASS + FAIL} 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
