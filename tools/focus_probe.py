"""[bigfish] 判定：输入框到底有没有焦点。

═══════════════════════════════════════════════════════════════════════
为什么先测这个
═══════════════════════════════════════════════════════════════════════

四种按键送法全失败，包括控件级 edit.SendKeys —— 它直接写给那个
Edit 控件，理论上不依赖任何焦点。

但文件粘贴失败 ≠ 按键没送到。还有一个可能：
**输入框根本没有文本焦点**，微信收到了 Ctrl+V 也不知道往哪儿贴。

区分方法极简单：**打几个字试试。**

    文字能进   → 焦点没问题，是微信不接受程序发起的文件粘贴
    文字也不能进 → 焦点有问题，修点击

═══════════════════════════════════════════════════════════════════════
测三件事
═══════════════════════════════════════════════════════════════════════

    1. 点输入框**上沿**（现在代码用的位置）→ 打文字 → 进不进
    2. 点输入框**中心**（人手点的位置）  → 打文字 → 进不进
    3. 用第 2 步的点击方式 → Ctrl+V → 图进不进

用法：
    # 管理员
    python tools/focus_probe.py
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

CHAT = "文件传输助手"


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


def color_ratio(gui, box) -> float:
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


def read_value(edit) -> str:
    try:
        return edit.GetValuePattern().Value or ""
    except Exception as exc:
        return f"<读不到 {type(exc).__name__}>"


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="bigfish-focus-")
    gif = make_gif(os.path.join(tmpdir, "f.gif"))

    print("=" * 70)
    print("判定：输入框有没有焦点")
    print("=" * 70)

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

    box = gui.get_input_box()
    print(f"\n输入框矩形（相对）：{box}")
    if not box:
        print("✗ 探测不到输入框")
        return 1
    x0, y0, x1, y1 = box
    print(f"  上沿点：({(x0 + x1) // 2}, {y0 + 22})   ← 现在代码用的")
    print(f"  中心点：({(x0 + x1) // 2}, {(y0 + y1) // 2})   ← 人手点的")

    results = {}
    try:
        # ══════════════════════════════════════════════════
        print("\n" + "─" * 70)
        print("  测试 1：点上沿 → 打文字")
        print("─" * 70)

        try:
            edit.SendKeys("{Ctrl}a{Delete}", waitTime=0.1)
        except Exception:
            pass
        time.sleep(0.3)

        cx, cy = (x0 + x1) // 2, y0 + 22
        try:
            gui.wx_click(gui.origin_x + cx, gui.origin_y + cy)
            print(f"     点击 ({cx},{cy})")
        except Exception as exc:
            print(f"     点击失败：{exc}")
        time.sleep(0.5)

        # 用控件级 SendKeys 打文字 —— 如果这个都不进，控件本身有问题
        try:
            edit.SendKeys("ABC测试", waitTime=0.05)
        except Exception as exc:
            print(f"     SendKeys 失败：{exc}")
        time.sleep(0.6)

        v1 = read_value(edit)
        ok1 = "ABC测试" in v1
        print(f"     输入框内容：{v1!r}")
        print(f"     {'✓ 文字进去了' if ok1 else '✗ 文字没进去'}")
        results["上沿+文字"] = ok1

        # ══════════════════════════════════════════════════
        print("\n" + "─" * 70)
        print("  测试 2：点中心 → 打文字")
        print("─" * 70)

        try:
            edit.SendKeys("{Ctrl}a{Delete}", waitTime=0.1)
        except Exception:
            pass
        time.sleep(0.3)

        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        try:
            gui.wx_click(gui.origin_x + cx, gui.origin_y + cy)
            print(f"     点击 ({cx},{cy})")
        except Exception as exc:
            print(f"     点击失败：{exc}")
        time.sleep(0.5)

        try:
            edit.SendKeys("XYZ测试", waitTime=0.05)
        except Exception as exc:
            print(f"     SendKeys 失败：{exc}")
        time.sleep(0.6)

        v2 = read_value(edit)
        ok2 = "XYZ测试" in v2
        print(f"     输入框内容：{v2!r}")
        print(f"     {'✓ 文字进去了' if ok2 else '✗ 文字没进去'}")
        results["中心+文字"] = ok2

        # ══════════════════════════════════════════════════
        print("\n" + "─" * 70)
        print("  测试 3：点中心 → Ctrl+V（贴图）")
        print("─" * 70)

        try:
            edit.SendKeys("{Ctrl}a{Delete}", waitTime=0.1)
        except Exception:
            pass
        time.sleep(0.35)

        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        try:
            gui.wx_click(gui.origin_x + cx, gui.origin_y + cy)
        except Exception:
            pass
        time.sleep(0.4)

        s._write_clipboard_files(gif)
        time.sleep(0.25)
        print(f"     剪贴板：{s._clipboard_files()[:1]}")

        from wechatauto.guia import WinInput

        WinInput().key(0x56, ctrl=True)
        time.sleep(1.4)

        ratio = color_ratio(gui, box)
        ok3 = ratio >= 0.02
        print(f"     彩色占比：{ratio:.1%}  "
              f"{'✓ 图进去了' if ok3 else '✗ 空的'}")
        results["中心+贴图"] = ok3

        # ══════════════════════════════════════════════════
        print("\n" + "=" * 70)
        print("结论")
        print("=" * 70)
        for k, v in results.items():
            print(f"  {'✓' if v else '✗'} {k}")

        print("\n" + "─" * 70)
        if results.get("上沿+文字") or results.get("中心+文字"):
            print("  文字能进 → 输入框有焦点，控件本身正常。")
            if results.get("中心+贴图"):
                print("  贴图也成 → 问题是**点击位置**。改点中心。")
            else:
                print("  但贴图不成 → **微信拒绝程序发起的文件粘贴**。")
                print("  这条路走不通，必须换「文件对话框」方案。")
        else:
            print("  文字都进不去 → 输入框根本没有文本焦点。")
            print("  先解决点击/聚焦，再谈粘贴。")
        print("─" * 70)

        # 清理，别留草稿
        try:
            edit.SendKeys("{Ctrl}a{Delete}", waitTime=0.1)
        except Exception:
            pass
    finally:
        try:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
