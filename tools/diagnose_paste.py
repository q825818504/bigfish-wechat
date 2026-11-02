"""[bigfish] 诊断：微信粘贴走哪条路、草稿到底有没有出现。

`live_sticker_test.py` 报了「UIA 粘贴 3 次仍未检测到附件草稿」。
但这句话有两个可能，必须分开验证才能知道问题在哪：

    A. 粘贴根本没成功（剪贴板没写进去 / 输入框没吃到 Ctrl+V）
    B. 粘贴成功了，但**检测方法**看不见草稿

B 的可能性不低：guia 的 UIA 草稿判据是「输入框 Value 里出现 U+FFFC」，
而微信 4.1 的 `mmui::ChatInputField` 到底暴不暴露 Value、暴露出来是什么，
没人验过。如果它不暴露，`_uia_has_draft` 那边有个 `except: return True`
本该兜住 —— 但日志说"3 次仍未检测到"，说明它**读到了值且值里没有 FFFC**。

这个脚本做四件事，每一步都打印原始读数，不下结论：

    1. 打印当前环境（管理员/UIA/窗口句柄）
    2. 纯文本粘贴一次，看 Value 长什么样 —— 校准基准
    3. 写 CF_HDROP，确认剪贴板里真的有文件
    4. 粘贴，每 0.3 秒读一次 Value 并打印，连读 5 秒
    5. 用像素检测（guia 的坐标路线判据）交叉验证

用法：
    python tools/diagnose_paste.py            # 发到文件传输助手
    python tools/diagnose_paste.py --chat 小麟

⚠ 不会真的发送。所有粘贴之后都清空输入框，不留草稿。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (_ROOT, _ROOT + "/vendor/wechatauto-replica"):
    if p not in sys.path:
        sys.path.insert(0, p)


def show(label, value):
    """把原始读数连类型一起打出来 —— 值本身比结论有用。"""
    if isinstance(value, str):
        esc = value.replace("\ufffc", "<FFFC>").replace("\n", "\\n")
        print(f"  {label}: {esc!r}  (长度 {len(value)})")
    else:
        print(f"  {label}: {value!r}")


def main() -> int:
    who = "文件传输助手"
    for i, a in enumerate(sys.argv):
        if a == "--chat" and i + 1 < len(sys.argv):
            who = sys.argv[i + 1]

    print("=" * 72)
    print(f"粘贴通道诊断 → {who}")
    print("=" * 72)

    from bigfish.wechat_sender import UiaSender, desktop_available, is_admin

    print("\n── 1. 环境 ──")
    print(f"  管理员      : {is_admin()}")
    print(f"  桌面解锁    : {desktop_available()}")

    s = UiaSender()
    try:
        s.wake()
    except Exception as exc:
        print(f"  ✗ UIA 唤醒失败：{exc}")
        return 1
    print(f"  UIA 就绪    : {s.ready}")
    print(f"  当前会话    : {s.current_chat()!r}")

    if s.current_chat() != who:
        print(f"  打开会话 {who} …")
        if not s.open_chat(who):
            print("  ✗ 打不开会话")
            return 1
        time.sleep(0.5)
        print(f"  当前会话    : {s.current_chat()!r}")

    edit = s._chat_input_ctrl()
    if edit is None:
        print("\n  ✗ 拿不到输入框控件（UIA 树里没有 chat_input_field）")
        print("    → 这条路走不通，只能依赖坐标路线")
        return 1
    print(f"  输入框控件  : {edit.ControlTypeName}  "
          f"aid={getattr(edit, 'AutomationId', '')!r}")

    def read_value():
        try:
            return edit.GetValuePattern().Value
        except Exception as exc:
            return f"<读不到: {type(exc).__name__}>"

    print("\n── 2. 基准：空白输入框读什么 ──")
    show("初始 Value", read_value())

    print("\n── 3. 基准：纯文本粘贴 ──")
    from wechatauto.guia import WeChatGUI

    # 先把文本写剪贴板
    import ctypes

    def set_text(text):
        try:
            u32 = ctypes.windll.user32
            k32 = ctypes.windll.kernel32
            u32.OpenClipboard.argtypes = [ctypes.c_void_p]
            u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
            k32.GlobalAlloc.restype = ctypes.c_void_p
            k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
            k32.GlobalLock.restype = ctypes.c_void_p
            k32.GlobalLock.argtypes = [ctypes.c_void_p]
            raw = text.encode("utf-16-le") + b"\x00\x00"
            if not u32.OpenClipboard(None):
                return False
            u32.EmptyClipboard()
            h = k32.GlobalAlloc(0x0042, len(raw))
            dst = k32.GlobalLock(h)
            ctypes.memmove(dst, raw, len(raw))
            k32.GlobalUnlock(h)
            u32.SetClipboardData(13, h)   # CF_UNICODETEXT
            u32.CloseClipboard()
            return True
        except Exception as exc:
            print(f"  写剪贴板失败：{exc}")
            return False

    set_text("诊断文本ABC")
    time.sleep(0.2)
    edit.SetFocus()
    edit.SendKeys("{Ctrl}a{Delete}", waitTime=0.1)
    edit.SendKeys("{Ctrl}v", waitTime=0.1)
    time.sleep(0.8)
    show("文本粘贴后 Value", read_value())
    edit.SendKeys("{Ctrl}a{Delete}", waitTime=0.1)
    time.sleep(0.3)
    show("清空后 Value", read_value())

    print("\n── 4. 剪贴板：CF_HDROP 有没有写进去 ──")
    tmpdir = tempfile.mkdtemp(prefix="bigfish-diag-")
    gif = os.path.join(tmpdir, "diag.gif")
    try:
        from PIL import Image

        frames = [Image.new("RGBA", (64, 64), (255, 0, 0, 255)),
                  Image.new("RGBA", (64, 64), (0, 255, 0, 255))]
        frames[0].save(gif, save_all=True, append_images=frames[1:],
                       duration=150, loop=0)
        print(f"  造了 {gif}  ({os.path.getsize(gif)} 字节)")

        ok = WeChatGUI.copy_files_to_clipboard([gif])
        print(f"  copy_files_to_clipboard → {ok}")
        if not ok:
            print("  ✗ 剪贴板写入失败 —— 问题在写入，不在粘贴")
            return 1
        time.sleep(0.3)
        # 回读剪贴板确认
        try:
            import win32clipboard  # type: ignore

            win32clipboard.OpenClipboard()
            has = win32clipboard.IsClipboardFormatAvailable(
                win32clipboard.CF_HDROP)
            win32clipboard.CloseClipboard()
            print(f"  剪贴板里有 CF_HDROP : {has}")
        except Exception as exc:
            print(f"  （回读剪贴板跳过：{exc}）")

        print("\n── 5. 粘贴文件，每 0.3s 读一次 Value，连读 5 秒 ──")
        edit.SetFocus()
        edit.SendKeys("{Ctrl}a{Delete}", waitTime=0.1)
        time.sleep(0.2)
        edit.SendKeys("{Ctrl}v", waitTime=0.1)

        seen_fffc = False
        for i in range(17):
            time.sleep(0.3)
            v = read_value()
            marker = ""
            if isinstance(v, str) and "\ufffc" in v:
                seen_fffc = True
                marker = "   ← 有 U+FFFC！"
            if i % 2 == 0 or marker:
                show(f"t+{(i + 1) * 0.3:.1f}s", v)
                if marker:
                    print(marker)
            if seen_fffc and i > 6:
                break

        print(f"\n  5 秒内出现 U+FFFC : {seen_fffc}")

        print("\n── 6. 交叉验证：像素检测（guia 坐标路线用的判据）──")
        try:
            gui = WeChatGUI()
            box = gui.get_input_box()
            print(f"  输入框矩形 : {box}")
            colored = gui._input_box_has_color_draft(box, wait=4.0)
            print(f"  彩色像素草稿 : {colored}")
        except Exception as exc:
            print(f"  像素检测跳过：{exc}")

        print("\n── 7. 清空，不留草稿 ──")
        edit.SetFocus()
        edit.SendKeys("{Ctrl}a{Delete}", waitTime=0.1)
        time.sleep(0.3)
        show("清空后 Value", read_value())

        print("\n" + "=" * 72)
        print("怎么读这份结果：")
        print("  文本粘贴后 Value 有字 → 控件暴露 Value，通道正常")
        print("  彩色的 True + Value 无 FFFC → **粘贴成功，是判据错了**")
        print("  彩色的 False + Value 空     → 粘贴真的没进去")
        print("=" * 72)
    finally:
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
