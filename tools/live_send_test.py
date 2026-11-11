"""真实收发实测 —— 唯一一个会真的发消息的测试。

流程：
  1. 以管理员权限打开数据库（取密钥）
  2. 热激活 UIA，定位输入框
  3. 记录「文件传输助手」当前最新消息水位
  4. **真的发一条测试消息**
  5. 回读数据库，确认消息确实落库
  6. 报告每一步的耗时

不发群、不发给联系人，只发给你自己的文件传输助手。
发出去的内容带明确标记，方便事后识别。

用法：
    python tools/live_send_test.py            # 只发，不测监听
    python tools/live_send_test.py --listen   # 发完再监听 40 秒
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
LOG = os.path.join(ROOT, "tools", "_live_result.txt")
_out = []


def say(s=""):
    print(s, flush=True)
    _out.append(str(s))


def section(t):
    say()
    say(HR)
    say(t)
    say(HR)


def main() -> int:
    import ctypes
    is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    t0 = time.time()

    section("0. 环境")
    say(f"  管理员: {is_admin}")
    say(f"  打包版: {getattr(sys, 'frozen', False)}")

    if not is_admin:
        say()
        say("  [!] 非管理员，UIA 无法热激活，这一步做不了。")
        say("      请右键以管理员身份运行。")
        return 1

    # ── 1. 数据库 ──
    section("1. 打开数据库")
    from bigfish.wechat import WeChatReader
    t = time.time()
    try:
        r = WeChatReader().open()
    except Exception as exc:
        say(f"  [!] 失败: {exc}")
        return 1
    info = r.self_info()
    say(f"  账号: {info.get('nick_name')} ({info.get('username')})")
    say(f"  密钥: {len(r._db._keys)}/{len(r._db._db_files)}")
    say(f"  耗时: {time.time()-t:.1f}s")

    # ── 2. 记水位 ──
    section("2. 记录发送前状态")
    before = r.messages("filehelper", limit=1)
    before_id = before[0].local_id if before else 0
    before_seq = before[0].timestamp if before else 0
    say(f"  最新消息 local_id={before_id}")
    say(f"  最新时间 {time.strftime('%H:%M:%S', time.localtime(before_seq)) if before_seq else '无'}")

    # ── 3. UIA ──
    section("3. 热激活 UIA")
    from bigfish.wechat_sender import SendError, UiaSender
    t = time.time()
    try:
        sender = UiaSender()
        sender.wake()
    except SendError as exc:
        say(f"  [!] 失败 [{exc.kind}]: {exc}")
        return 1
    say(f"  热激活完成 {time.time()-t:.1f}s")
    say(f"  当前会话: {sender.current_chat()!r}")

    # ── 4. 真的发 ──
    section("4. 发送测试消息")
    stamp = time.strftime("%H:%M:%S")
    text = f"[自检 {stamp}] 这条是大肥鱼发的，收到说明收发都通。"
    say("  目标: 文件传输助手")
    say(f"  内容: {text}")

    t = time.time()
    try:
        res = sender.send("文件传输助手", text)
    except SendError as exc:
        say(f"  [!] 发送失败 [{exc.kind}]: {exc}")
        return 1
    dt = time.time() - t
    say(f"  返回: ok={res.ok}  {res.detail}")
    say(f"  耗时: {dt:.1f}s")

    if not res.ok:
        say()
        say("  发送函数返回失败，不继续。")
        return 1

    # ── 5. 回读确认 ──
    section("5. 回读数据库确认")
    found = False
    for attempt in range(10):
        time.sleep(1.0)
        try:
            r2 = WeChatReader().open()
            msgs = r2.messages("filehelper", limit=3)
        except Exception:
            continue
        for m in msgs:
            if stamp in m.content:
                found = True
                say(f"  ✓ 找到（第 {attempt+1} 次查询）")
                say(f"    local_id={m.local_id}  {m.msg_type}")
                say(f"    内容: {m.content[:60]}")
                break
        if found:
            break

    if not found:
        say("  ✗ 数据库里没找到这条消息")
        say("    可能原因：消息还在发送队列 / 内容被截断 / 发到了别处")
        say("    请手动看一眼微信里有没有收到")
    else:
        says = "消息已确认落库"
        say(f"  {says}")

    # ── 6. 监听（可选）──
    if "--listen" in sys.argv:
        section("6. 监听测试（40 秒）")
        from bigfish.watcher import MessageWatcher
        w = MessageWatcher(r._db, interval=1.0)
        w.watch("filehelper")
        say("  现在去微信给文件传输助手发一条消息")
        say("  最多等 40 秒 ...")
        got = []

        def on_msg(m):
            got.append(m)
            say(f"  [收到] {m.text[:50]!r} self={m.is_self}")

        w.on_message = on_msg
        w.start()
        deadline = time.time() + 40
        while time.time() < deadline and not got:
            time.sleep(0.5)
        w.stop()
        say()
        say(f"  共收到 {len(got)} 条")

    section("结果")
    total = time.time() - t0
    say(f"  总耗时 {total:.1f}s")
    say(f"  发送: {'成功' if res.ok else '失败'}")
    say(f"  落库: {'确认' if found else '未确认'}")
    return 0 if (res.ok and found) else 1


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        import traceback
        traceback.print_exc()
        code = 1
    finally:
        try:
            with open(LOG, "w", encoding="utf-8") as f:
                f.write("\n".join(_out))
            print(f"\n结果已写入 {LOG}")
        except OSError:
            pass
        try:
            input("\n按回车退出...")
        except EOFError:
            pass
    sys.exit(code)
