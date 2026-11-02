"""[bigfish] 最后一张牌：把按键方式全试一遍，一次出结论。

═══════════════════════════════════════════════════════════════════════
先说清楚之前错在哪
═══════════════════════════════════════════════════════════════════════

`paste_only.py` 那次报的是：

    区域变化：5.9（>6 表示进了东西）
    ⚠ 输入框没变化 —— Ctrl+V 没生效

**它报告的是失败。** 用户看到输入框里有缩略图，那是他自己之前贴的；
"发送出去了是表情"也是他手动点的发送按钮。

我一直把那次当成"脚本成功、只是判据误判"，于是在错误前提上
连续改了五轮：判据、队列、截图、顺序、格式、聚焦。
全部无效。

真相是：**脚本的 Ctrl+V 从来没有生效过一次。**

═══════════════════════════════════════════════════════════════════════
这个脚本测什么
═══════════════════════════════════════════════════════════════════════

剪贴板已经验证正确（用户拿脚本写的那份手动 Ctrl+V 能贴进去）。
所以唯一变量是**按键怎么送进微信**。

四种送法，每种独立测：

    A  edit.SendKeys("{Ctrl}v")     控件级投递，直接写给那个 Edit 控件
    B  keybd_event                 全局按键，跟系统键盘焦点
    C  SendInput                   全局按键，注入式
    D  PostMessage(WM_PASTE)       直接给控件发粘贴消息

每次之前都重新写一遍剪贴板，保证起点一致。
每次之后都立刻看彩色占比，>2% 就算进。

用法：
    # 管理员
    python tools/key_probe.py
"""
from __future__ import annotations

import ctypes
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

CHAT = "文件传输助手"
WM_PASTE = 0x0302


# ══════════════════════════════════════════════════════════════════════
def color_ratio(gui, box) -> float:
    """输入框彩色占比。>=2% 算有草稿。和 wechat_sender 同一套算法。"""
    if not box:
        return 0.0
    try:
        x0, y0, x1, y1 = box
        rel = (x0 + 4, y0 + 4, x1 - 4, y1 - 4)
        shot = gui._grab_screen(gui._rel_to_screen(rel)).convert("RGB")
        px = shot.load()
        w, h = shot.size
        total = colored = 0
        for yy in range(0, h, 4):
            for xx in range(0, w, 4):
                r, g, b = px[xx, yy]
                total += 1
                if (abs(r - g) > 24 or abs(g - b) > 24
                        or abs(r - b) > 24):
                    colored += 1
        return (colored / total) if total else 0.0
    except Exception:
        return 0.0


def make_gif(path: str) -> str:
    from PIL import Image, ImageDraw

    frames = []
    for i in range(3):
        im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.ellipse([20 + i * 14, 20, 236, 236], fill=(255, 210, 90, 255),
                  outline=(60, 45, 20, 255), width=4)
        frames.append(im)
    frames[0].save(path, save_all=True, append_images=frames[1:],
                   duration=150, loop=0, disposal=2)
    return path


# ══════════════════════════════════════════════════════════════════════
# 四种按键送法
# ══════════════════════════════════════════════════════════════════════

def send_uiasendkeys(edit, gui, box) -> str:
    try:
        edit.SendKeys("{Ctrl}v", waitTime=0.1)
        return "已调用 edit.SendKeys"
    except Exception as exc:
        return f"异常：{exc}"


def send_keybd(edit, gui, box) -> str:
    try:
        from wechatauto.guia import WinInput

        WinInput().key(0x56, ctrl=True)
        return "已调用 keybd_event"
    except Exception as exc:
        return f"异常：{exc}"


def send_sendinput(edit, gui, box) -> str:
    """SendInput 注入 Ctrl+V。

    和 keybd_event 的差别：SendInput 走的是现代输入管线，
    某些远程/虚拟化会话里 keybd_event 会被丢而 SendInput 不会。
    """
    try:
        u32 = ctypes.windll.user32

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", ctypes.c_ushort),
                        ("wScan", ctypes.c_ushort),
                        ("dwFlags", ctypes.c_uint32),
                        ("time", ctypes.c_uint32),
                        ("dwExtraInfo", ctypes.c_size_t)]

        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", ctypes.c_uint32), ("u", _U)]

        VK_CONTROL, VK_V = 0x11, 0x56
        KEYUP = 0x0002
        seq = [(VK_CONTROL, 0), (VK_V, 0), (VK_V, KEYUP), (VK_CONTROL, KEYUP)]
        for vk, flags in seq:
            inp = INPUT()
            inp.type = 1
            inp.u.ki.wVk = vk
            inp.u.ki.dwFlags = flags
            u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
            time.sleep(0.03)
        return "已调用 SendInput"
    except Exception as exc:
        return f"异常：{exc}"


def send_wmpaste(edit, gui, box) -> str:
    """直接给输入框控件发 WM_PASTE。

    这条路绕过键盘完全 —— 粘贴是控件自己处理的消息。
    如果它能成，说明焦点/按键都不是问题，只要消息送到就行。
    """
    try:
        hwnd = getattr(edit, "NativeWindowHandle", 0)
        if not hwnd:
            return "拿不到控件句柄"
        r = ctypes.windll.user32.PostMessageW(int(hwnd), WM_PASTE, 0, 0)
        return f"PostMessage(WM_PASTE) → {bool(r)} (hwnd={hwnd})"
    except Exception as exc:
        return f"异常：{exc}"


METHODS = [
    ("A · edit.SendKeys", send_uiasendkeys),
    ("B · keybd_event", send_keybd),
    ("C · SendInput", send_sendinput),
    ("D · WM_PASTE", send_wmpaste),
]


# ══════════════════════════════════════════════════════════════════════
def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="bigfish-keyprobe-")
    gif = make_gif(os.path.join(tmpdir, "k.gif"))

    print("=" * 70)
    print("按键送法对照：同一份剪贴板，四种送法")
    print("=" * 70)
    print(f"图片：{gif}")

    from bigfish.wechat_sender import UiaSender

    s = UiaSender()
    try:
        s.wake()
    except Exception as exc:
        print(f"✗ UIA 唤醒失败：{exc}")
        return 1

    gui = s._gui()
    edit = s._chat_input_ctrl()
    if edit is None:
        print("✗ 拿不到输入框控件")
        return 1

    if s.current_chat() != CHAT:
        if not s.open_chat(CHAT):
            print(f"✗ 打不开 {CHAT}")
            return 1
        time.sleep(0.6)
    try:
        gui.ensure_visible()
    except Exception:
        pass
    time.sleep(0.5)

    results = []
    try:
        for name, fn in METHODS:
            print(f"\n{'─' * 70}")
            print(f"  {name}")
            print(f"{'─' * 70}")

            # 清空输入框
            try:
                edit.SendKeys("{Ctrl}a{Delete}", waitTime=0.1)
            except Exception:
                pass
            time.sleep(0.35)

            box = None
            try:
                box = gui.get_input_box()
            except Exception:
                pass

            # 聚焦（三种都做，排除聚焦这个变量）
            try:
                edit.SetFocus()
            except Exception:
                pass
            time.sleep(0.1)
            if box:
                try:
                    gui.focus_input(box)
                except Exception:
                    pass
            time.sleep(0.25)

            # 写剪贴板 —— 每次都重写，保证起点一致
            s._write_clipboard_files(gif)
            time.sleep(0.25)
            got = s._clipboard_files()
            print(f"     剪贴板：{got[:1] if got else '（空！）'}")
            if not got:
                print("     ⚠ 剪贴板是空的，这一轮结果无效")

            # 送按键
            note = fn(edit, gui, box)
            print(f"     {note}")
            time.sleep(1.3)

            ratio = color_ratio(gui, box)
            ok = ratio >= 0.02
            print(f"     彩色占比：{ratio:.1%}  {'✓ 进去了' if ok else '✗ 空的'}")

            results.append((name, ok, ratio, note))

            if ok:
                # 进去了就发出去，顺便确认是表情
                try:
                    from wechatauto.guia import WinInput

                    base = 0
                    db = gui._get_db()
                    hits = db.search_contact(CHAT)
                    uid = hits[0]["username"] if hits else "filehelper"
                    rows = db.get_messages(uid, limit=1)
                    base = int(rows[0]["sort_seq"]) if rows else 0

                    WinInput().key(0x0D)
                    time.sleep(2.0)
                    rows = sorted(db.get_messages(uid, limit=3),
                                  key=lambda r: int(r.get("sort_seq") or 0),
                                  reverse=True)
                    fresh = [r for r in rows
                             if int(r.get("sort_seq") or 0) > base]
                    if fresh:
                        print(f"     → 发出去了，类型 = {fresh[0].get('type')!r}")
                        results[-1] = (name, ok, ratio,
                                       note + f" → {fresh[0].get('type')}")
                    else:
                        print("     → 没发出去（回车无效）")
                except Exception as exc:
                    print(f"     → 发送检查失败：{exc}")

                print("\n   ★ 这一种能进。后面的不用再试了。")
                break

            time.sleep(0.6)
    finally:
        try:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

    print("\n" + "=" * 70)
    print("汇总")
    print("=" * 70)
    for name, ok, ratio, note in results:
        print(f"  {'✓' if ok else '✗'} {name:22s} {ratio:6.1%}   {note}")

    good = [r for r in results if r[1]]
    print("\n" + "─" * 70)
    if good:
        print(f"  用 {good[0][0]} 能粘贴。改 wechat_sender 用它。")
    else:
        print("  四种全失败 —— 说明问题不在按键方式。")
        print("  下一步该放弃剪贴板路线，改走「+ 菜单 → 文件对话框」。")
    print("─" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
