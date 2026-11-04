"""说法层演示：直接看效果，不做断言。

跑法：
    python tools/demo_say.py
    python tools/demo_say.py --text "你想试的话"

不碰微信、不碰桌面、不调模型。假时钟 + 假 reader/sender，整轮瞬间跑完。
想验收正确性请跑 selftest_*.py；这个脚本只负责让你**看见**行为变了。
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import listen as L  # noqa: E402
from bigfish import say as S    # noqa: E402

for _s in (sys.stdout, sys.stderr):
    if _s is not None:
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

HR = "═" * 62


def title(n: str, text: str) -> None:
    print(f"\n【{n}】{text}")
    print("─" * 62)


def show_parts(parts, *, prefix: str = "  ") -> None:
    for i, p in enumerate(parts, 1):
        print(f"{prefix}{'①②③④⑤⑥⑦⑧'[i - 1] if i <= 8 else i} {p}　（{len(p)} 字）")


# ══════════════════════════════════════════════════════════════════════
# 1 / 2：分条与节奏
# ══════════════════════════════════════════════════════════════════════

def demo_split(text: str) -> None:
    title("1", "分条：一段话怎么变成几条")
    parts = S.split_reply(text)
    print(f"输入：{text}\n")
    print(f"输出：{len(parts)} 条")
    show_parts(parts)

    gaps = S.plan_pacing(parts, S.PacePolicy(seed="demo", jitter=0.0))
    total = sum(gaps)
    print(f"\n【2】节奏：每条发出去之前等多久（合计 {total:.1f}s）")
    print("─" * 62)
    for i, (p, g) in enumerate(zip(parts, gaps), 1):
        label = "思考" if i == 1 else "停顿"
        print(f"  {i} {label} {g:.2f}s → {p}")
    print(f"\n  合计 {total:.1f}s（真人打字差不多这个量级；"
          f"超过 12s 会自动等比压缩）")


def demo_marker() -> None:
    title("3", "模型给了分条标记：听模型的")
    text = "知道了\n{{split}}\n那明天就去长坂坡玩吧\n{{split}}\n咱们先看樱花，再看枣树"
    print(f"模型输出：\n{text}\n")
    print(f"切出来：{len(S.split_reply(text))} 条")
    show_parts(S.split_reply(text))
    print("\n  这就是期望的形态：不是把一段话剪开，是她本来就这么说。")


def demo_quotes() -> None:
    title("4", "引号保护：别人引用的原话不拦腰截断")
    cases = [
        ("他说「明天去长坂坡，后天回来」我答应了", "引号内逗号不切"),
        ("他说「明天去」，我答应了，就这样定吧", "引号外照切"),
        ('（明天去长坂坡，后天回来）这样最顺', "括号内不切"),
    ]
    for text, note in cases:
        parts = S.split_reply(text)
        print(f"  {note}")
        print(f"    输入：{text}")
        print(f"    输出：{len(parts)} 条")
        show_parts(parts, prefix="      ")
        print()


# ══════════════════════════════════════════════════════════════════════
# 5：攒批
# ══════════════════════════════════════════════════════════════════════

class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))


def demo_batch() -> None:
    title("5", "攒批：连发三句只回一次")
    clock = Clock()
    b = L.MessageBatcher(clock=clock)

    arrivals = [
        (0.0, "大肥鱼"),
        (0.5, "你在不在"),
        (1.0, "吃饭了没"),
    ]
    print("  时间线：")
    for at, text in arrivals:
        clock.now = at
        b.feed(L.BatchMessage(chat_id="c1", author="小麟", text=text,
                              local_id=int(at * 10) + 1, received_at=at))
        print(f"    {at:5.1f}s  收到「{text}」"
              + ("（重置 2s 计时）" if at > 0 else ""))

    sealed_at = None
    for step in range(0, 60):
        clock.now = step * 0.1
        out = b.tick()
        if out:
            sealed_at = clock.now
            batch = out[0]
            break

    if sealed_at is None or not batch:
        print("  [!] 没有封批，逻辑异常")
        return

    print(f"\n    {sealed_at:5.1f}s  安静到期 → 封批")
    print(f"            {len(batch.messages)} 条 / 等了 {batch.waited:.1f}s / 原因：{batch.reason}")
    print("\n  到这一刻才调模型、才回话。")
    print("  旧逻辑：0.0s 收到「大肥鱼」就开跑，1 秒后回复，")
    print("          那时「你在不在」还没发出来 —— 就是插话。")


# ══════════════════════════════════════════════════════════════════════
# 6：发送确认
# ══════════════════════════════════════════════════════════════════════

@dataclass
class Row:
    local_id: int
    is_self: bool
    content: str
    at: float


class Reader:
    def __init__(self, clock: Clock, latency: float = 0.4) -> None:
        self.clock = clock
        self.latency = latency
        self.rows: list[Row] = []
        self._id = 0

    def messages(self, chat: str, limit: int = 20):
        visible = [r for r in self.rows if r.at <= self.clock.now]
        visible.sort(key=lambda r: r.local_id)
        return visible[-limit:]

    def schedule(self, text: str) -> None:
        self._id += 1
        self.rows.append(Row(self._id, True, text, self.clock.now + self.latency))


class Sender:
    def __init__(self, reader: Reader, clock: Clock, latency: float = 0.4) -> None:
        self.reader = reader
        self.clock = clock
        self.latency = latency
        self.steps: list[str] = []

    def send(self, who: str, text: str):
        self.reader.schedule(text)
        return type("R", (), {"ok": True, "detail": ""})()


def demo_send() -> None:
    title("6", "发送确认：发一条、确认一条，再发下一条")
    clock = Clock()
    reader = Reader(clock, latency=0.4)
    sender = Sender(reader, clock)

    parts = ["知道了", "明天就去长坂坡游玩", "先去看樱花"]
    gaps = [0.6, 1.2, 0.0]

    marks = {"t": {}, "text": {}}

    def on_confirmed(i, msg):
        marks["t"][i] = clock.now
        marks["text"][i] = getattr(msg, "content", "")

    start = clock.now
    out = S.send_parts(sender.send, reader.messages, "小麟", parts, gaps,
                       clock=clock, sleep=clock.sleep,
                       final_async_confirm=False,
                       on_confirmed=on_confirmed)
    elapsed = clock.now - start

    for i, p in enumerate(parts):
        t = marks["t"].get(i)
        line = f"  [发] {i + 1} {p}"
        if t is None:
            print(line + "　（未确认）")
        else:
            print(line + f"　→ 库里确认 @ {t:.2f}s")
    print(f"\n  结果：{out.level} / 发了 {out.parts_sent} 条 / 总耗时 {elapsed:.1f}s")
    print("  L1 = 回车成功；L2 = 库里出现自己的新行；L3 = 内容也对得上。")


# ══════════════════════════════════════════════════════════════════════
# 7：对照
# ══════════════════════════════════════════════════════════════════════

def demo_compare() -> None:
    title("7", "旧行为 vs 新行为")
    print("  旧：")
    print("    1 条长文 / 一次模型 / 没有间隔 / 发出去就当成功")
    print("    对方连发三句 → 第一句到就开跑 → 插话")
    print("\n  新：")
    print("    最多 4 条短句 / 一次模型 / 条间 0.6~3.5s / 每条确认落库")
    print("    对方连发三句 → 等最后一句后 2s → 一次理解 → 分条回")
    print("\n  开关（config.json）：")
    print('    say.enabled=false     → 退回整段发送')
    print('    listen.enabled=false  → 退回逐条直回')
    print('    say.strict_confirm    → true 时库里没确认就停（默认 false，继续发）')


def main() -> int:
    ap = argparse.ArgumentParser(description="大肥鱼 · 说法层演示")
    ap.add_argument("--text", default="", help="只演示这一段的分条")
    args = ap.parse_args()

    print(HR)
    print(" 大肥鱼 · 说法层演示（本地模拟，不碰微信）")
    print(HR)

    if args.text:
        demo_split(args.text)
    else:
        demo_split("知道了，明天就去长坂坡游玩，先去看樱花，再去看枣树")
        demo_marker()
        demo_quotes()
        demo_batch()
        demo_send()
        demo_compare()

    print("\n" + HR)
    print(" 以上全部用假时钟与假 reader/sender，不发真实消息。")
    print(" 真机验证：重启后连发三句，看日志")
    print("   [收] 小麟：3 条 / 等 2.0s / quiet")
    print("   [发] ✓ → 小麟：3 条 / L3")
    print(HR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
