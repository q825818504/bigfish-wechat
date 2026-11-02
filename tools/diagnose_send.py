"""[bigfish] 诊断 2：回车之后，输入框里到底还有什么。

上一轮 `live_sticker_test` 的结果：

    send_media 返回成功，耗时 3.3s
    数据库新增 0 条
    用户肉眼确认：什么都没发出去

但同一套代码在 `diagnose_paste.py` 里，粘贴后像素检测是 True
（缩略图确实画出来了）。矛盾在哪？

这个脚本把 **粘贴之后** 和 **回车之后** 的状态都拍下来，
并且把图存到磁盘 —— 因为"草稿在不在"这件事，看图最准，
读 Value、数彩色像素都是间接推断。

它做四件事，一步一步缩小范围：

    1. 粘贴 → 截图 → 读 Value        （草稿到底进没进框）
    2. 回车 → 截图 → 读 Value        （发完之后框里还剩什么）
    3. 换个手法：粘贴 → 等 1 秒 → 点发送按钮 → 截图
    4. 回读数据库，看哪一步真的落了库

用法：
    python tools/diagnose_send.py

⚠ 会真的发送（这就是目的）。发到文件传输助手，安全。
   截的图留在 <临时目录>/bigfish-send-diag-*/，脚本会把路径打出来。
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

WHO = "文件传输助手"


def show(label, value):
    if isinstance(value, str):
        esc = value.replace("\ufffc", "<FFFC>").replace("\n", "\\n")
        print(f"  {label}: {esc!r}")
    else:
        print(f"  {label}: {value!r}")


def make_gif(path, size=256, frames=4):
    """小一点的图 —— 缩略图更小、渲染更快，先排除"图太大没画出来"。"""
    from PIL import Image, ImageDraw

    imgs = []
    for i in range(frames):
        im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        step = size // 8
        d.rectangle([i * step, i * step, size - 1 - i * step, size - 1 - i * step],
                    fill=(255, 200 - i * 40, i * 40, 255),
                    outline=(0, 0, 0, 255), width=3)
        imgs.append(im)
    imgs[0].save(path, save_all=True, append_images=imgs[1:],
                 duration=150, loop=0, disposal=2)
    return path


def snap(gui, tag, outdir):
    """截整个微信窗口，存盘，返回路径。

    ⚠ 这里踩过一个坑，写下来免得再踩：
      ``gui._grab_screen(box)`` 的 box 是**屏幕绝对坐标**，
      不是窗口相对坐标。第一版我传了 ``(0, 0, render_w, render_h)``
      就以为截的是微信，实际截的是屏幕左上角 —— 用户看到的是他桌面上的
      PowerShell，跟微信一个像素都没关系，于是整轮排查都建在错误前提上。

      正确写法是 ``gui._rel_to_screen((0, 0, w, h))``。
    """
    try:
        rect = gui._rel_to_screen((0, 0, gui.render_w, gui.render_h))
        shot = gui._grab_screen(rect)
        path = os.path.join(outdir, f"{tag}.png")
        shot.save(path)
        print(f"     截图（屏幕 {rect}）→ {path}")
        return path
    except Exception as exc:
        print(f"     截图失败：{exc}")
        return ""


def foreground_title() -> str:
    """当前前台窗口标题。用来确认"提到前面的到底是不是微信"。"""
    try:
        import ctypes

        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        n = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value or f"<空标题 hwnd={hwnd}>"
    except Exception as exc:
        return f"<读不到 {exc}>"


def db_latest(gui, who=WHO):
    """回读这个会话最新的几条，返回 [(类型, seq)]。"""
    try:
        db = gui._get_db()
        hits = db.search_contact(who)
        uid = hits[0]["username"] if hits else "filehelper"
        rows = db.get_messages(uid, limit=5)
        rows = sorted(rows, key=lambda r: int(r.get("sort_seq") or 0),
                      reverse=True)
        return [(str(r.get("type")), int(r.get("sort_seq") or 0))
                for r in rows[:3]], uid
    except Exception as exc:
        print(f"     数据库读取失败：{exc}")
        return [], ""


def main() -> int:
    print("=" * 72)
    print("发送诊断：回车之后到底发生了什么")
    print("=" * 72)

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

    if s.current_chat() != WHO:
        if not s.open_chat(WHO):
            print(f"✗ 打不开会话 {WHO}")
            return 1
        time.sleep(0.6)

    # 把微信提到前台再截图 —— 否则截到的可能是盖在上面的别的窗口。
    # 打印前台标题，让"提到前面的到底是不是微信"这件事可验证。
    try:
        gui.ensure_visible()
    except Exception as exc:
        print(f"（ensure_visible 失败：{exc}）")
    time.sleep(0.8)
    print(f"\n当前前台窗口：{foreground_title()!r}")
    print(f"微信窗口屏幕坐标：{gui._rel_to_screen((0, 0, gui.render_w, gui.render_h))}")

    outdir = tempfile.mkdtemp(prefix="bigfish-send-diag-")
    print(f"\n截图目录：{outdir}")

    tmpdir = tempfile.mkdtemp(prefix="bigfish-send-gif-")
    gif = make_gif(os.path.join(tmpdir, "d.gif"))

    def val():
        try:
            return edit.GetValuePattern().Value
        except Exception as exc:
            return f"<读不到 {type(exc).__name__}>"

    def box():
        try:
            return gui.get_input_box()
        except Exception:
            return None

    try:
        before, uid = db_latest(gui)
        print(f"\n── 发送前 ──")
        print(f"  最近三条：{before}")

        # ══════════════════════════════════════════════════
        print("\n── 步骤 1：粘贴 ──")
        from wechatauto.guia import WeChatGUI

        ok = WeChatGUI.copy_files_to_clipboard([gif])
        print(f"  写剪贴板: {ok}")
        time.sleep(0.3)

        edit.SetFocus()
        time.sleep(0.2)
        edit.SendKeys("{Ctrl}a{Delete}", waitTime=0.1)
        time.sleep(0.2)
        edit.SendKeys("{Ctrl}v", waitTime=0.1)
        time.sleep(1.5)

        show("粘贴后 Value", val())
        b = box()
        print(f"  输入框矩形: {b}")
        if b:
            try:
                print(f"  彩色像素草稿（方差大=可能是误判）: "
                      f"{gui._input_box_has_color_draft(b, wait=2.0)}")
                print(f"  深色像素有字                      : "
                      f"{gui._input_box_has_text(b)}")
            except Exception as exc:
                print(f"  像素检测失败：{exc}")
        snap(gui, "1-粘贴后", outdir)

        # ══════════════════════════════════════════════════
        print("\n── 步骤 2a：回车发送（UIA SendKeys 路线）──")
        edit.SendKeys("{Enter}", waitTime=0.05)
        time.sleep(2.0)
        show("回车后 Value", val())
        snap(gui, "2a-回车后", outdir)
        after_a, _ = db_latest(gui)
        print(f"  数据库：{after_a}")
        sent_a = after_a and before and after_a[0][1] > before[0][1]
        print(f"  → 发了新消息？{'是' if sent_a else '否'}")

        # ══════════════════════════════════════════════════
        if not sent_a:
            print("\n── 步骤 2b：换成点「发送」按钮 ──")
            print("  （UIA SendKeys 投递的回车可能没进到微信的输入框）")
            edit.SetFocus()
            time.sleep(0.2)
            edit.SendKeys("{Ctrl}a{Delete}", waitTime=0.1)
            time.sleep(0.3)
            # 重新写一次剪贴板 —— 上一步可能已经把它消耗/清空了
            WeChatGUI.copy_files_to_clipboard([gif])
            time.sleep(0.3)
            edit.SendKeys("{Ctrl}v", waitTime=0.1)
            time.sleep(1.5)
            show("重新粘贴后 Value", val())
            b = box()
            if b:
                try:
                    print(f"  彩色像素草稿: {gui._input_box_has_color_draft(b, wait=2.0)}")
                    print(f"  深色像素有字: {gui._input_box_has_text(b)}")
                except Exception:
                    pass
            snap(gui, "2b-重新粘贴", outdir)

            clicked = False
            try:
                # ⚠ 不能直接调 click_send()：它先查 _pending_text_ok(pending)，
                #   而 _pending_text 只有 input_text() 走过才会被设上。
                #   我们这条路是直接塞的剪贴板文件，没经过 input_text，
                #   所以 pending 永远是 None，click_send 会立刻返回 False
                #   —— 看起来像"发送按钮没反应"，实际是前置检查没过。
                box_now = gui.get_input_box()
                print(f"  _input_box_has_text({box_now}) → "
                      f"{gui._input_box_has_text(box_now)}")
                clicked = gui.click_send()
                print(f"  click_send() → {clicked}")
            except Exception as exc:
                print(f"  click_send 失败：{exc}")
            time.sleep(2.0)
            snap(gui, "2b-点发送后", outdir)
            after_b, _ = db_latest(gui)
            print(f"  数据库：{after_b}")
            sent_b = after_b and before and after_b[0][1] > before[0][1]
            print(f"  → 发了新消息？{'是' if sent_b else '否'}")

        print("\n" + "=" * 72)
        print("结论怎么读：")
        print("  粘贴后 Value 空 + 截图里看得见缩略图 → 草稿在，判据错")
        print("  粘贴后截图也空                      → 粘贴根本没进去")
        print("  回车无效但点发送有效                → 回车投递有问题")
        print(f"\n看图：{outdir}")
        print("=" * 72)
    finally:
        try:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
