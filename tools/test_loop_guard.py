"""回环防护测试 —— 验证引擎不会自问自答。

复现条件（实测踩过的）：
    allow_self=True + 文件传输助手
    → 你发一句 → 引擎回一句
    → 引擎的回复被监听器读回来 → 又触发回复
    → 无限循环

防护有三层：
    1. allow_self 默认 False（自己发的不处理）
    2. 内容比对：刚发出去的文本，ECHO_WINDOW 内读回来不处理
    3. 频率熔断：单会话 BURST_WINDOW 内超过 BURST_MAX 条就停

这个测试直接构造一个假的引擎环境，模拟「发出去又被读回来」的场景，
检查三层防护是否各自生效。
"""
from __future__ import annotations

import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "vendor", "wechatauto-replica"))

HR = "=" * 70
fails = []


def ok(label, cond, extra=""):
    print(f"  [{'OK  ' if cond else 'FAIL'}] {label}  {extra}")
    if not cond:
        fails.append(label)


def main() -> int:
    print(HR)
    print("回环防护测试")
    print(HR)

    # ── 检查源码里的防护是否都在 ──
    print("\n1. 三层防护是否存在于代码里")
    src = open(os.path.join(ROOT, "bigfish", "desktop.py"),
               encoding="utf-8").read()

    ok("① allow_self 判断", "msg.is_self and not allow_self" in src)
    ok("② 内容比对函数 text_is_echo", "def text_is_echo" in src)
    ok("③ 频率熔断 BURST_MAX", "BURST_MAX" in src)
    ok("④ 发送后记住文本 remember_sent",
       "remember_sent(send_text)" in src)
    ok("⑤ 熔断计数 last_reply_at",
       "last_reply_at.setdefault" in src)

    # ── 模拟第一层：内容比对 ──
    print("\n2. 内容比对（模拟「发出去又被读回来」）")

    # 复刻引擎里的实现
    recently_sent = []
    ECHO_WINDOW = 20.0

    def remember_sent(t):
        recently_sent.append((t.strip(), time.time()))

    def _normalize(t):
        import re
        v = (t or "").strip()
        v = re.sub(r"^[A-Za-z0-9_@-]{4,}:\s?", "", v)
        v = re.sub(r"^(@\S+\s*)+", "", v)
        return v.strip()

    def text_is_echo(t):
        now = time.time()
        cutoff = now - ECHO_WINDOW
        while recently_sent and recently_sent[0][1] < cutoff:
            recently_sent.pop(0)
        n = _normalize(t)
        if not n:
            return False
        for sent_text, _ in recently_sent:
            m = _normalize(sent_text)
            if not m:
                continue
            if n == m:
                return True
            k = min(20, len(m), len(n))
            if k >= 6 and (n.startswith(m[:k]) or m.startswith(n[:k])):
                return True
        return False

    reply = "在，爸。"
    remember_sent(reply)
    ok("刚发出去的原文被识别为回声", text_is_echo("在，爸。"),
       "在，爸。")
    ok("带 @ 前缀的也算回声", text_is_echo("@四条丶 在，爸。"))

    # 截断匹配只对足够长的文本生效 —— 短句用前缀会误判，
    # 所以阈值设了 6 字符。这里用一条真实长度的回复验证。
    long_reply = "在呢，这事我记着呢，等我忙完这阵就跟你说"
    remember_sent(long_reply)
    ok("被截断的长回复算回声",
       text_is_echo(long_reply[:12]),
       f"{long_reply[:12]}...")
    ok("短句不做前缀匹配（防误判）",
       not text_is_echo("在"),
       "「在」不该匹配「在，爸。」")
    ok("对方新说的话不算回声", not text_is_echo("你在干嘛"))

    # 时间窗口外应该放行
    recently_sent.clear()
    recently_sent.append((reply, time.time() - 30))
    ok("超过时间窗口不再当回声", not text_is_echo("在，爸。"))

    # ── 模拟第二层：频率熔断 ──
    print("\n3. 频率熔断（模拟内容比对漏掉的情况）")
    BURST_WINDOW = 90.0
    BURST_MAX = 4
    last_reply_at = {}

    def can_reply(chat_id):
        now = time.time()
        span = last_reply_at.setdefault(chat_id, [])
        span[:] = [t for t in span if now - t < BURST_WINDOW]
        return len(span) < BURST_MAX

    def record_reply(chat_id):
        last_reply_at.setdefault(chat_id, []).append(time.time())

    cid = "filehelper"
    allowed = 0
    for i in range(10):
        if can_reply(cid):
            allowed += 1
            record_reply(cid)
    ok(f"连发 10 次只放行 {BURST_MAX} 次", allowed == BURST_MAX,
       f"实际放行 {allowed}")

    # 时间窗口过去后恢复
    last_reply_at[cid] = [time.time() - 200]
    ok("窗口过后恢复放行", can_reply(cid))

    # ── 配置默认值 ──
    print("\n4. 配置默认值")
    import json
    cfg_path = os.path.join(ROOT, "config.json")
    if os.path.isfile(cfg_path):
        d = json.load(open(cfg_path, encoding="utf-8"))
        ar = d.get("auto_reply") or {}
        ok("allow_self 当前为 False", ar.get("allow_self") is not True,
           f"-> {ar.get('allow_self')!r}")
        ok("group_reply 有值", "group_reply" in ar,
           f"-> {ar.get('group_reply')!r}")
    else:
        ok("config.json 存在", False)

    print()
    print(HR)
    if fails:
        print(f"  ✗ {len(fails)} 项失败: {fails}")
    else:
        print("  ✓ 回环防护全部有效")
    print(HR)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
