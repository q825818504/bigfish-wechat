"""[bigfish] 最小复现：剪贴板由你准备，我只做点击 + Ctrl+V + 回车。

═══════════════════════════════════════════════════════════════════════
为什么这么设计
═══════════════════════════════════════════════════════════════════════

前面几轮已经把变量删到只剩两个：

    ① 剪贴板里的内容是谁写的（我 / 系统）
    ② 点击 + Ctrl+V 这套动作对不对

用户手动验证过：资源管理器 Ctrl+C → 点聊天框 → Ctrl+V → 能发。
所以只要「剪贴板 = 用户复制的那份」，① 就被固定了。
这个脚本**完全不碰剪贴板** —— 不写、不清、不读。

剩下的全部行为就是三个动作：
    点输入框 → Ctrl+V → 回车

如果这能成 → 问题 100% 在我写剪贴板那一步（powershell 抢焦点之类）
如果这不成 → 问题在点击或按键本身

═══════════════════════════════════════════════════════════════════════
怎么用
═══════════════════════════════════════════════════════════════════════

    1. 在资源管理器里选中一张 .gif，按 Ctrl+C
    2. 立刻运行（管理员）：
           python tools/paste_only.py
    3. 看输出

脚本不会自己复制任何东西。它跑的时候剪贴板里必须已经是你要发的图。

参数：
    --chat 会话名     默认「文件传输助手」
    --no-send         只粘贴不回车（让你看一眼草稿效果）
"""
from __future__ import annotations

import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def fg_title() -> str:
    try:
        import ctypes

        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        n = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value or f"<空 hwnd={hwnd}>"
    except Exception as exc:
        return f"<读不到 {exc}>"


def read_clipboard_files():
    """只读，不改。用来确认剪贴板里到底有没有东西。"""
    try:
        import win32clipboard
        import win32con

        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_HDROP):
                return list(win32clipboard.GetClipboardData(win32con.CF_HDROP))
            fmts = []
            f = 0
            while True:
                f = win32clipboard.EnumClipboardFormats(f)
                if not f:
                    break
                try:
                    fmts.append(win32clipboard.GetClipboardFormatName(f))
                except Exception:
                    fmts.append(f"std-{f}")
            return [f"<没有文件，只有这些格式：{fmts}>"]
        finally:
            win32clipboard.CloseClipboard()
    except Exception as exc:
        return [f"<读剪贴板失败：{exc}>"]


def sig(gui, box):
    if not box:
        return None
    try:
        x0, y0, x1, y1 = box
        rel = (x0 + 4, y0 + 4, x1 - 4, y1 - 4)
        return gui._grab_screen(gui._rel_to_screen(rel)).convert("L").resize((48, 16))
    except Exception:
        return None


def diff(a, b) -> float:
    if a is None or b is None:
        return -1.0
    try:
        x = list(a.getdata())
        y = list(b.getdata())
        return sum(abs(p - q) for p, q in zip(x, y)) / max(1, len(x))
    except Exception:
        return -1.0


def db_latest(gui, chat: str):
    try:
        db = gui._get_db()
        hits = db.search_contact(chat)
        uid = hits[0]["username"] if hits else "filehelper"
        rows = db.get_messages(uid, limit=3)
        rows = sorted(rows, key=lambda r: int(r.get("sort_seq") or 0),
                      reverse=True)
        return [(str(r.get("type")), int(r.get("sort_seq") or 0))
                for r in rows], uid
    except Exception as exc:
        print(f"  读库失败：{exc}")
        return [], ""


def main() -> int:
    chat = "文件传输助手"
    do_send = True
    for i, a in enumerate(sys.argv):
        if a == "--chat" and i + 1 < len(sys.argv):
            chat = sys.argv[i + 1]
        if a == "--no-send":
            do_send = False

    print("=" * 70)
    print("最小复现：只做 点击 + Ctrl+V" + (" + 回车" if do_send else ""))
    print("=" * 70)

    # ---- 先确认剪贴板里有东西，且是文件 ----
    files = read_clipboard_files()
    print(f"\n剪贴板：{files}")
    real = [f for f in files if os.path.isfile(f)]
    if not real:
        print("\n✗ 剪贴板里没有可用的文件。")
        print("  先在资源管理器里选中一张 .gif 按 Ctrl+C，再重新运行。")
        return 1
    print(f"  用的是：{real[0]}"
          f"（{os.path.getsize(real[0]) / 1024:.1f}KB）")

    from bigfish.wechat_sender import UiaSender

    s = UiaSender()
    try:
        s.wake()
    except Exception as exc:
        print(f"\n✗ UIA 唤醒失败：{exc}")
        return 1

    gui = s._gui()
    edit = s._chat_input_ctrl()
    if edit is None:
        print("\n✗ 拿不到输入框控件")
        return 1

    if s.current_chat() != chat:
        print(f"\n打开会话 {chat} …")
        if not s.open_chat(chat):
            print(f"✗ 打不开 {chat}")
            return 1
        time.sleep(0.6)

    # 把微信提到前台 —— 真实按键需要它
    try:
        gui.ensure_visible()
    except Exception as exc:
        print(f"（ensure_visible 失败：{exc}）")
    time.sleep(0.6)
    print(f"\n前台窗口：{fg_title()!r}")

    box = None
    try:
        box = gui.get_input_box()
    except Exception:
        pass
    print(f"输入框矩形：{box}")
    if not box:
        print("✗ 探测不到输入框，后面都会失败")
        return 1

    before_seq, uid = db_latest(gui, chat)
    print(f"基线：{before_seq[:1] if before_seq else '（空）'}  chat={uid}")

    # ══════════════════════════════════════════════════════
    print("\n── 1. 点输入框 ──")
    print(f"   前台（点击前）：{fg_title()!r}")
    try:
        ok = gui.focus_input(box)
        print(f"   focus_input → {ok}")
    except Exception as exc:
        print(f"   focus_input 异常：{exc}")
    time.sleep(0.4)
    print(f"   前台（点击后）：{fg_title()!r}")

    # 点完之后立刻拍基准 —— 只在这里截一次，之后不截
    base = sig(gui, box)

    # ══════════════════════════════════════════════════════
    print("\n── 2. Ctrl+V（剪贴板是你准备的，我不碰）──")
    from wechatauto.guia import WinInput

    WinInput().key(0x56, ctrl=True)
    time.sleep(1.3)

    try:
        val = edit.GetValuePattern().Value
        print(f"   输入框 Value：{val!r}")
    except Exception as exc:
        print(f"   读 Value 失败：{exc}")

    now = sig(gui, box)
    d = diff(base, now)
    print(f"   区域变化：{d:.1f}（>6 表示进了东西）")

    if d <= 6.0:
        print("\n   ⚠ 输入框没变化 —— Ctrl+V 没生效")
        print("   可能：焦点不在输入框 / 按键没投递到微信")
        return 1

    print("\n   ✓ 草稿进去了")

    if not do_send:
        print("\n--no-send：停在这里，去看一眼输入框里的缩略图。")
        return 0

    # ══════════════════════════════════════════════════════
    print("\n── 3. 回车发送 ──")
    WinInput().key(0x0D)
    time.sleep(2.0)

    fresh, _ = db_latest(gui, chat)
    base_seq = before_seq[0][1] if before_seq else 0
    new = [r for r in fresh if r[1] > base_seq]
    print(f"   数据库新增：{new or '（无）'}")

    print("\n" + "=" * 70)
    if new:
        print(f"  ✓ 发出去了，类型 = {new[0][0]!r}")
        if new[0][0] == "动画表情":
            print("    这是**原生表情** —— 对方可以长按添加、会动。成了。")
        elif new[0][0] == "图片":
            print("    落成了「图片」而不是「动画表情」。")
            print("    说明微信这版不把它当表情处理，路线得重新设计。")
    else:
        print("  ✗ 进了输入框但没发出去 —— 回车没生效")
        print("    下一步该查回车投递，而不是剪贴板")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
