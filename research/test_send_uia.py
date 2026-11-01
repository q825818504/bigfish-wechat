"""验证 UIA 发送能力：给「文件传输助手」发一条消息。

为什么选文件传输助手：
  它是你自己账号内的功能，**不经过任何联系人**，发错了也只影响自己。
  这是验证发送链路最保守的方式。

流程（全部走 UIA，不做 OCR、不做坐标瞎点）：
  1. 热激活 UIA 树
  2. 用搜索框打开「文件传输助手」
  3. 定位输入框
  4. 写入文本
  5. 回车发送
  6. 回读数据库确认（verify）

只发一条，内容明确标记为测试。
"""
import ctypes
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "vendor", "wechatauto-replica"))

OUT = os.path.join(HERE, "_send_test.txt")
HR = "=" * 74
_lines = []


def say(s=""):
    print(s, flush=True)
    _lines.append(str(s))


def section(t):
    say()
    say(HR)
    say(t)
    say(HR)


TARGET = "文件传输助手"
TEXT = "【大肥鱼发送测试】如果你看到这条，说明 UIA 发送链路已打通。"


def main():
    dry = "--dry" in sys.argv

    section("0. 环境")
    say(f"  管理员: {bool(ctypes.windll.shell32.IsUserAnAdmin())}")
    say(f"  模式  : {'预演（不发）' if dry else '实发'}")
    say(f"  目标  : {TARGET}")
    say(f"  内容  : {TEXT}")

    section("1. 热激活 UIA")
    try:
        from wechatauto.uia_driver import WeChatUIA
        uia = WeChatUIA()
        say("  WeChatUIA 构造成功")
    except Exception as e:
        say(f"  [!] 失败: {e}")
        return 1

    # 触发一次需要 UIA 的操作，确保树已物化
    time.sleep(1)
    say(f"  当前会话: {uia.current_chat()!r}")

    section("2. 打开目标会话")
    try:
        ok = uia.open_chat(TARGET)
        say(f"  open_chat({TARGET!r}) -> {ok}")
        time.sleep(1)
        cur = uia.current_chat()
        say(f"  打开后当前会话: {cur!r}")
        if cur != TARGET:
            say(f"  [!] 会话名不匹配，停止以免发错地方")
            return 1
    except Exception as e:
        say(f"  [!] 失败: {type(e).__name__}: {e}")
        import traceback
        say(traceback.format_exc()[:1200])
        return 1

    section("3. 检查树是否已物化")
    try:
        say(f"  is_materialized() -> {uia.is_materialized()}")
        say(f"  window_kind()     -> {uia.window_kind()}")
        if not uia.is_materialized():
            say("  尝试 ensure_materialized() ...")
            say(f"  -> {uia.ensure_materialized(timeout=8)}")
    except Exception as e:
        say(f"  [!] {type(e).__name__}: {e}")

    if dry:
        section("4. 预演结束")
        say("  没有发送任何内容。")
        say("  输入框定位（不发送）：")
        try:
            from wechatauto import uia_driver as ud
            rect = ud.chat_input_rect(uia) if hasattr(ud, "chat_input_rect") else None
            say(f"    {rect}")
        except Exception as e:
            say(f"    无法预演输入框: {e}")
        return 0

    section("4. 发送")
    try:
        # send_text_to 内部会 open_chat + 输入 + 回车
        ok = uia.send_text_to(TEXT, TARGET)
        say(f"  send_text_to({TEXT!r}, {TARGET!r}) -> {ok}")
    except Exception as e:
        say(f"  [!] 发送失败: {type(e).__name__}: {e}")
        import traceback
        say(traceback.format_exc()[:1500])
        return 1

    section("5. 数据库回读确认")
    try:
        from wechatauto import WeChatDB
        db = WeChatDB()
        msgs = db.get_messages("filehelper", limit=3)
        hit = False
        for m in msgs:
            c = str(m.get("content") or "")
            say(f"  [{m.get('create_time')}] {c[:60]}")
            if TEXT[:20] in c:
                hit = True
        say()
        say(f"  找到本次发送的消息: {'是' if hit else '否（可能是 mtime 延迟，稍后重查）'}")
    except Exception as e:
        say(f"  回读失败: {type(e).__name__}: {e}")

    section("6. 结果")
    say("  ok=True 且回读到消息 = 发送链路完全打通")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        import traceback
        traceback.print_exc()
        code = 1
    finally:
        try:
            with open(OUT, "w", encoding="utf-8") as f:
                f.write("\n".join(_lines))
        except OSError:
            pass
        try:
            input("\n按回车退出...")
        except EOFError:
            pass
    sys.exit(code)
