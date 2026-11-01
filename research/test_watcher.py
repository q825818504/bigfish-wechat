"""单独测试 watcher 能不能抓到新消息。

不启动 UIA、不调模型，只验证「读数据库发现新消息」这一段。

流程：
  1. 加载数据库
  2. 建 watcher，监听指定会话（水位从 0 开始，历史消息被 prefill 过滤）
  3. 轮询 40 秒
  4. 这期间你去微信发消息，应该能看到 [捕获] 输出
"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "vendor", "wechatauto-replica"))

from bigfish.watcher import MessageWatcher  # noqa: E402
from wechatauto import WeChatDB             # noqa: E402

HR = "=" * 74


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "文件传输助手"
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 40

    print(HR)
    print("watcher 独立测试")
    print(HR)
    print(f"  目标: {target}")
    print(f"  时长: {seconds} 秒")

    print("\n  打开数据库 ...")
    db = WeChatDB()
    print(f"  账号: {db.account}")

    w = MessageWatcher(db, interval=1.0)
    uid = w.watch(target)
    print(f"  监听会话: {uid}")
    print(f"  水位初始: {w._wm.get(uid)}")
    print(f"  prefill 条数: {len(w._prefill.get(uid, set()))}")
    print()
    print(HR)
    print(f"  现在去微信给「{target}」发一条消息")
    print(HR)

    got = []

    def on_msg(m):
        got.append(m)
        print(f"\n[捕获] {m.chat_name} / {m.sender_name or '(无)'}")
        print(f"    sort_seq={m.sort_seq}  local_id={m.local_id}")
        print(f"    type={m.msg_type}  is_self={m.is_self}")
        print(f"    text={m.text!r}")

    w.on_message = on_msg
    w.start()

    t0 = time.time()
    try:
        while time.time() - t0 < seconds:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n中断")
    finally:
        w.stop()

    print()
    print(HR)
    print(f"  共捕获 {len(got)} 条")
    if not got:
        print("  没有捕获到消息。可能原因：")
        print("    1. 这期间你没发消息")
        print("    2. 消息的 sort_seq 没超过水位")
        print("    3. 被 prefill 当成历史过滤了")
    print(HR)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        try:
            input("\n按回车退出...")
        except EOFError:
            pass
