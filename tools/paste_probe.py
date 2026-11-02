"""[bigfish] 粘贴探针：把「谁写剪贴板」这一个变量单独拎出来测。

═══════════════════════════════════════════════════════════════════════
背景：为什么写这个
═══════════════════════════════════════════════════════════════════════

真机结果已经确认了两件事：

    paste_only.py（用户手动 Ctrl+C 准备剪贴板）  → 粘贴成功，收到表情
    send_media（自己用 powershell 写剪贴板）      → 输入框彩色占比 0.0%

同一张图、同一个点击、同一个 Ctrl+V。所以差别在**谁写剪贴板**。

但又不能只信这个结论 —— 今天已经因为"格式存在 ≠ 微信读得懂"
连续判断错四轮。所以这个脚本把两种写法**并排跑**：

    组 A  用 PowerShell Set-Clipboard 写   → 粘贴 → 报告
    组 B  用 copy_files_to_clipboard 写    → 粘贴 → 报告

每组都打印剪贴板的**完整格式列表**，不只看 CF_HDROP 在不在。

═══════════════════════════════════════════════════════════════════════
用法（你不需要记任何 PowerShell 命令）
═══════════════════════════════════════════════════════════════════════

    # 管理员
    python tools/paste_probe.py

    # 想指定别的图
    python tools/paste_probe.py "D:\\大脑\\表情测试\\动图_512.gif"

脚本会自己准备图片（默认用一张刚生成的 4 帧动图），自己写剪贴板，
自己粘贴，自己回读数据库。全程不用你动手。

⚠ 会真的往「文件传输助手」发图。这是为了确认结果，不是副作用。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CHAT = "文件传输助手"


# ══════════════════════════════════════════════════════════════════════
# 剪贴板：读格式、两种写法
# ══════════════════════════════════════════════════════════════════════

def clip_dump() -> dict:
    """把剪贴板内容全读出来：格式列表 + 文件路径。只读。"""
    out = {"formats": [], "files": [], "err": ""}
    try:
        import win32clipboard
        import win32con

        win32clipboard.OpenClipboard()
        try:
            fmt = 0
            while True:
                fmt = win32clipboard.EnumClipboardFormats(fmt)
                if not fmt:
                    break
                try:
                    name = win32clipboard.GetClipboardFormatName(fmt)
                except Exception:
                    name = f"std-{fmt}"
                out["formats"].append(name)
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_HDROP):
                out["files"] = list(win32clipboard.GetClipboardData(
                    win32con.CF_HDROP) or [])
        finally:
            win32clipboard.CloseClipboard()
    except Exception as exc:
        out["err"] = str(exc)
    return out


def write_powershell(path: str) -> bool:
    safe = path.replace("'", "''")
    ps = ("$ErrorActionPreference='Stop';"
          f"Set-Clipboard -Path '{safe}'")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps],
                           capture_output=True, text=True, timeout=20,
                           creationflags=getattr(subprocess,
                                                 "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            print(f"     Set-Clipboard rc={r.returncode} "
                  f"{(r.stderr or '')[:150]}")
            return False
        return True
    except Exception as exc:
        print(f"     Set-Clipboard 异常：{exc}")
        return False


def write_ours(path: str) -> bool:
    try:
        from wechatauto.guia import WeChatGUI

        return bool(WeChatGUI.copy_files_to_clipboard([path]))
    except Exception as exc:
        print(f"     copy_files_to_clipboard 异常：{exc}")
        return False


# ══════════════════════════════════════════════════════════════════════
# 粘贴与验证
# ══════════════════════════════════════════════════════════════════════

def color_ratio(gui, box) -> float:
    """输入框里彩色像素占比。>=2% 就算有草稿。

    和 wechat_sender._input_region_signature 同一套算法 ——
    那边是唯一真源，这里复制一份是为了在探针里能独立打数字，
    改那边记得同步这里（或者干脆改成 import，但探针不该依赖内部细节）。
    """
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


def latest(gui, chat=CHAT):
    try:
        db = gui._get_db()
        hits = db.search_contact(chat)
        uid = hits[0]["username"] if hits else "filehelper"
        rows = db.get_messages(uid, limit=3)
        rows = sorted(rows, key=lambda r: int(r.get("sort_seq") or 0),
                      reverse=True)
        return [(str(r.get("type")), int(r.get("sort_seq") or 0))
                for r in rows]
    except Exception:
        return []


def run_round(tag: str, writer, gif: str, gui, edit) -> dict:
    """一轮：清空 → 写 → 贴 → 报告。返回结果字典。"""
    from wechatauto.guia import WinInput

    print(f"\n{'─' * 68}")
    print(f"  组 {tag}")
    print(f"{'─' * 68}")

    # 清空输入框
    try:
        edit.SendKeys("{Ctrl}a{Delete}", waitTime=0.1)
    except Exception:
        pass
    time.sleep(0.4)

    # 定位输入框并聚焦
    box = None
    try:
        box = gui.get_input_box()
    except Exception:
        pass
    if box:
        try:
            gui.focus_input(box)
        except Exception as exc:
            print(f"     focus_input 失败：{exc}")
    time.sleep(0.3)

    # 写剪贴板（这一轮唯一的变量）
    ok = writer(gif)
    print(f"     写入：{'成功' if ok else '失败'}")
    time.sleep(0.25)

    dump = clip_dump()
    print(f"     格式：{dump['formats']}")
    print(f"     文件：{dump['files'][:1] if dump['files'] else '（空）'}")

    # 写完之后前台还是不是微信
    fg = ""
    try:
        from bigfish.wechat_sender import UiaSender

        fg = UiaSender._foreground_title()
    except Exception:
        pass
    print(f"     前台：{fg!r}")

    # 粘贴
    WinInput().key(0x56, ctrl=True)
    time.sleep(1.3)

    ratio = color_ratio(gui, box)
    print(f"     粘贴后彩色占比：{ratio:.1%}"
          f"（阈值 2%，{'有草稿' if ratio >= 0.02 else '空的'}）")

    # 发送
    base = latest(gui)[0][1] if latest(gui) else 0
    WinInput().key(0x0D)
    time.sleep(2.0)
    fresh = [r for r in latest(gui) if r[1] > base]

    print(f"     数据库新增：{fresh or '（无）'}")
    return {
        "tag": tag,
        "formats": dump["formats"],
        "files": dump["files"],
        "ratio": ratio,
        "sent": bool(fresh),
        "type": fresh[0][0] if fresh else "",
    }


def main() -> int:
    gif = sys.argv[1] if len(sys.argv) > 1 else ""
    tmpdir = ""
    if not gif:
        from PIL import Image

        tmpdir = tempfile.mkdtemp(prefix="bigfish-probe-")
        gif = os.path.join(tmpdir, "probe.gif")
        frames = []
        for i in range(4):
            im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
            from PIL import ImageDraw

            d = ImageDraw.Draw(im)
            d.ellipse([20 + i * 12, 20, 236, 236],
                      fill=(255, 210, 90, 255), outline=(60, 45, 20, 255),
                      width=4)
            frames.append(im)
        frames[0].save(gif, save_all=True, append_images=frames[1:],
                       duration=150, loop=0, disposal=2)

    print("=" * 68)
    print("粘贴探针：两种写法并排测")
    print("=" * 68)
    print(f"图片：{gif}（{os.path.getsize(gif) / 1024:.1f}KB）")

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
        results.append(run_round("A · PowerShell Set-Clipboard",
                                 write_powershell, gif, gui, edit))
        time.sleep(1.5)
        results.append(run_round("B · copy_files_to_clipboard",
                                 write_ours, gif, gui, edit))
    finally:
        if tmpdir:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n" + "=" * 68)
    print("汇总")
    print("=" * 68)
    for r in results:
        print(f"\n  组 {r['tag']}")
        print(f"    格式   ：{r['formats']}")
        print(f"    彩色占比：{r['ratio']:.1%}")
        print(f"    发出去  ：{'是' if r['sent'] else '否'}"
              f"{('（' + r['type'] + '）') if r['type'] else ''}")

    a = results[0] if results else {}
    b = results[1] if len(results) > 1 else {}
    print("\n" + "─" * 68)
    if a.get("sent") and not b.get("sent"):
        print("  用 PowerShell 能发，用旧的不能")
        print("  → send_media 里已经在用 PowerShell 了，那问题在别处")
        print("    下一步查：send_media 里 Ctrl+V 之前多做了什么")
    elif a.get("sent") and b.get("sent"):
        print("  两种都能发 —— 那 send_media 的失败跟写剪贴板无关")
    elif not a.get("sent") and not b.get("sent"):
        print("  两种都发不出去 —— 问题不在写剪贴板")
        print("  但你手动 Ctrl+C 能成，说明差别在别的地方")
        print("  下一步查：你手动时和执行脚本时，微信状态有什么不同")
    else:
        print("  旧写法能发、PowerShell 不能（少见，但记录下来）")
    print("─" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
