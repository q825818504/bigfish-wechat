"""真机测试：把「说法」跑一遍，只发给「文件传输助手」。

跑法：
    python tools/live_test_say.py           # 默认只检查环境 + 预览，不发
    python tools/live_test_say.py --send    # 真发，会再问一次 yes

安全约束（写死在代码里，不接受参数）：
  · 目标只能是「文件传输助手」
  · 不加 --send 绝不发送
  · 加 --send 还要手输 yes

前置：管理员权限、桌面解锁、微信已登录且窗口可见。
"""
from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

for _s in (sys.stdout, sys.stderr):
    if _s is not None:
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

#: 唯一允许的发送目标。不要改成参数——测试脚本不该能发给别人。
TARGET = "文件传输助手"

SAMPLE = "知道了，明天就去长坂坡游玩，先去看樱花，再去看枣树"


def main() -> int:
    ap = argparse.ArgumentParser(description="说法层真机测试（只发文件传输助手）")
    ap.add_argument("--send", action="store_true", help="真的发送（默认只预览）")
    ap.add_argument("--text", default=SAMPLE, help="要发的内容")
    args = ap.parse_args()

    print("=" * 62)
    print(" 说法层真机测试")
    print("=" * 62)

    # ---- 1) 环境 ----
    from bigfish.wechat_sender import UiaSender, desktop_available, is_admin

    admin = is_admin()
    desk = desktop_available()
    print(f"  管理员     : {admin}")
    print(f"  桌面可用   : {desk}")
    if args.send and (not admin or not desk):
        print("\n  [!] 发送需要管理员权限 + 解锁的桌面。")
        print("      先不加 --send 跑一遍预览。")
        return 1

    # ---- 2) 分条预览 ----
    from bigfish.say import PacePolicy, SplitPolicy, plan_pacing, split_reply

    from bigfish import plugins as P

    P.ensure_loaded()
    cfg = P.config_of("say")
    policy = SplitPolicy(
        max_parts=int(cfg.get("max_parts", 4)),
        part_max_chars=int(cfg.get("part_max_chars", 50)),
        protect_quotes=bool(cfg.get("quote_protect", True)),
    )
    parts = split_reply(args.text, policy)
    gaps = plan_pacing(parts, PacePolicy(
        seed="live-test",
        per_char=float(cfg.get("per_char", 0.09)),
        cap=float(cfg.get("cap", 3.5)),
        total_cap=float(cfg.get("total_cap", 12.0)),
    ))

    print(f"\n  目标       : {TARGET}（写死，不可改）")
    print(f"  输入       : {args.text}")
    print(f"  切成       : {len(parts)} 条 / 合计 {sum(gaps):.1f}s")
    for i, (p, g) in enumerate(zip(parts, gaps), 1):
        print(f"    {i}. 等 {g:.2f}s → {p}")

    if not args.send:
        print("\n  以上只是预览，没有发送。")
        print("  确认目标无误后加 --send 真发。")
        return 0

    # ---- 3) 二次确认 ----
    print(f"\n  即将把上面 {len(parts)} 条发给「{TARGET}」。")
    try:
        answer = input("  确认请输入 yes：").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  已取消。")
        return 1
    if answer != "yes":
        print("  已取消（没有发送）。")
        return 1

    # ---- 4) 打开微信 ----
    from bigfish.wechat import WeChatReader

    print("\n  打开微信数据库 ...")
    try:
        reader = WeChatReader().open()
    except Exception as exc:
        print(f"  [!] 数据库打不开: {exc}")
        print("      确认微信已登录、版本 4.1.15.53。")
        return 1

    try:
        sender = UiaSender()
        sender.wake()
        print(f"  UIA        : 就绪（当前会话 {sender.current_chat()!r}）")
    except Exception as exc:
        print(f"  [!] UIA 初始化失败: {exc}")
        return 1

    # ---- 5) 发送 ----
    from bigfish.say import send_parts

    print("\n  开始发送 ...")
    marks: dict[int, float] = {}

    def on_confirmed(i, _msg):
        marks[i] = 0.0

    outcome = send_parts(
        sender.send,
        reader.messages,
        TARGET,
        parts,
        gaps,
        log=lambda m: print(f"    {m}"),
        strict_confirm=False,
        final_async_confirm=False,
        on_confirmed=on_confirmed,
    )

    print("\n" + "=" * 62)
    print(f"  结果       : {outcome.level} / 发出 {outcome.parts_sent}/{outcome.total_parts} 条")
    if outcome.mismatches:
        print(f"  内容不符   : {outcome.mismatches}（微信可能改写了文本，属正常）")
    if outcome.unconfirmed:
        print(f"  未确认     : {outcome.unconfirmed}（L1 成功但库里没查到，可能落库慢）")
    print(f"  去微信看   : 「{TARGET}」里应该有 {len(parts)} 条，间隔 0.6~1.5 秒")
    print("=" * 62)
    return 0 if outcome.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
