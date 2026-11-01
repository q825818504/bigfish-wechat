"""watcher 调试版：打印线程状态、每轮轮询结果。

用来查明「为什么消息进了水位，却没走回调」。
"""
import os
import sys
import threading
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
    target = sys.argv[1] if len(sys.argv) > 1 else "filehelper"
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 45

    print(HR)
    print("watcher 调试版")
    print(HR)

    db = WeChatDB()
    print(f"  账号: {db.account}")

    w = MessageWatcher(db, interval=1.0)
    uid = w.watch(target)
    print(f"  监听: {uid}")
    print(f"  水位: {w._wm.get(uid)}")
    print(f"  prefill: {len(w._prefill.get(uid, set()))}")

    count = [0]

    def on_msg(m):
        count[0] += 1
        print(f"\n[回调#{count[0]}] {m.chat_name} / {m.sender_name!r}")
        print(f"    seq={m.sort_seq} lid={m.local_id} self={m.is_self}")
        print(f"    text={m.text!r}", flush=True)

    w.on_message = on_msg

    print()
    print("  启动 watcher 线程 ...")
    w.start()
    print(f"  线程对象: {w._thread}")
    print(f"  线程存活: {w._thread.is_alive() if w._thread else None}")
    print(f"  当前线程数: {threading.active_count()}")
    print()
    print(HR)
    print(f"  现在去微信发消息，持续 {seconds} 秒")
    print(HR, flush=True)

    t0 = time.time()
    last_report = t0
    try:
        while time.time() - t0 < seconds:
            time.sleep(0.5)
            # 每 5 秒报告一次线程状态和水位
            if time.time() - last_report >= 5:
                last_report = time.time()
                alive = w._thread.is_alive() if w._thread else False
                print(f"  [状态] 线程存活={alive}  "
                      f"水位={w._wm.get(uid)}  已收={count[0]}",
                      flush=True)
    except KeyboardInterrupt:
        print("\n中断")
    finally:
        w.stop()

    print()
    print(HR)
    print(f"  总共回调 {count[0]} 条")
    print(f"  最终水位: {w._wm.get(uid)}")
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
