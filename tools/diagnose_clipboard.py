"""[bigfish] 诊断 3：剪贴板内容到底是谁的问题。

═══════════════════════════════════════════════════════════════════════
为什么要写这个
═══════════════════════════════════════════════════════════════════════

用户手动能发出去，代码不能。两边步骤一模一样：

    用户：资源管理器 Ctrl+C → 点输入框 → Ctrl+V → 发送
    代码：copy_files_to_clipboard() → 点输入框 → Ctrl+V → 回车

中间没有任何额外动作。唯一差别是**谁把文件写进剪贴板**。

所以我之前那条「截图吃剪贴板」的结论是错的 —— 改了顺序还是 0.0。

需要验证的只剩一件事：
    copy_files_to_clipboard 写出来的 DROPFILES，微信能不能读懂？

═══════════════════════════════════════════════════════════════════════
怎么用
═══════════════════════════════════════════════════════════════════════

脚本会分两轮，每轮之间会停下来等你：

    轮次 A  用**系统方式**写剪贴板（PowerShell 的 Set-Clipboard）
            然后粘贴 + 回车。如果这轮成了 → 微信读得懂标准格式

    轮次 B  用**我们自己的** copy_files_to_clipboard 写
            同样粘贴 + 回车。这轮是对照组

哪一轮成、哪一轮败，直接指出问题在写入函数还是别处。

用法：
    # 管理员
    python tools/diagnose_clipboard.py

⚠ 脚本每轮只做「写剪贴板 → 点输入框 → Ctrl+V → 回车」，
   和你手动做的完全一样。中间不截图（截图留到发送之后）。
"""
from __future__ import annotations

import ctypes
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
for p in (_ROOT, _ROOT + "/vendor/wechatauto-replica"):
    if p not in sys.path:
        sys.path.insert(0, p)

WHO = "文件传输助手"


# ══════════════════════════════════════════════════════════════════════
# 读剪贴板：把里面的东西翻出来看
# ══════════════════════════════════════════════════════════════════════

def dump_clipboard() -> dict:
    """读出剪贴板里 CF_HDROP 的内容，尽量还原 DROPFILES 结构。

    这是整件事的关键：光看"格式存在"没用，要看**里面的路径读出来是什么**。
    如果读出来是空列表或乱码，微信那边同样是这个结果。
    """
    out = {"formats": [], "files": [], "ok": False, "err": ""}
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
                    name = f"<标准 {fmt}>"
                out["formats"].append(name)

            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_HDROP):
                try:
                    files = win32clipboard.GetClipboardData(win32con.CF_HDROP)
                    out["files"] = list(files) if files else []
                    out["ok"] = bool(out["files"])
                except Exception as exc:
                    out["err"] = f"读 HDROP 失败：{exc}"
            else:
                out["err"] = "剪贴板里没有 CF_HDROP"
        finally:
            win32clipboard.CloseClipboard()
    except Exception as exc:
        out["err"] = f"打开剪贴板失败：{exc}"
    return out


def show_clipboard(tag: str) -> dict:
    d = dump_clipboard()
    print(f"  [{tag}] 格式：{d['formats']}")
    print(f"  [{tag}] 文件：{d['files'] or '（空）'}")
    if d["err"]:
        print(f"  [{tag}] 问题：{d['err']}")
    return d


# ══════════════════════════════════════════════════════════════════════
# 两种写入方式
# ══════════════════════════════════════════════════════════════════════

def write_system(path: str) -> bool:
    """用 PowerShell 的 Set-Clipboard 写文件列表。

    这是 Windows 自己的实现 —— 它写出来的 DROPFILES 一定是标准格式。
    用户手动 Ctrl+C 走的是资源管理器，同样出自 Windows 自己。
    所以这条路代表"标准写法"。
    """
    try:
        ps = (
            "$ErrorActionPreference='Stop';"
            f"Set-Clipboard -Path '{path}'"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps],
            capture_output=True, text=True, timeout=25)
        if r.returncode != 0:
            print(f"     Set-Clipboard 失败：{(r.stderr or '')[:160]}")
            return False
        return True
    except Exception as exc:
        print(f"     Set-Clipboard 异常：{exc}")
        return False


def write_ours(path: str) -> bool:
    """我们自己的实现（就是 wechat_sender 现在用的那个）。"""
    try:
        from wechatauto.guia import WeChatGUI

        return bool(WeChatGUI.copy_files_to_clipboard([path]))
    except Exception as exc:
        print(f"     copy_files_to_clipboard 异常：{exc}")
        return False


# ══════════════════════════════════════════════════════════════════════
# 粘贴 + 回车（和用户手动做的完全一样）
# ══════════════════════════════════════════════════════════════════════

def paste_and_send(gui, edit, label: str) -> bool:
    """点输入框 → Ctrl+V → 回车。中间不截图。

    返回是否真的发出去了（靠数据库判）。
    """
    from wechatauto.guia import WinInput

    box = None
    try:
        box = gui.get_input_box()
    except Exception:
        pass

    # 点进输入框（这一步和用户"点击聊天框"一样）
    if box:
        try:
            gui.focus_input(box)
        except Exception as exc:
            print(f"     点输入框失败：{exc}")
    time.sleep(0.3)

    # Ctrl+V —— 剪贴板是在外面写好的，这里不碰
    WinInput().key(0x56, ctrl=True)
    time.sleep(1.2)

    # 发送前看一眼
    try:
        val = edit.GetValuePattern().Value
        print(f"     粘贴后 Value：{val!r}")
    except Exception:
        pass

    # 回车
    WinInput().key(0x0D)
    time.sleep(2.0)
    return True


def db_before(gui) -> int:
    try:
        db = gui._get_db()
        hits = db.search_contact(WHO)
        uid = hits[0]["username"] if hits else "filehelper"
        rows = db.get_messages(uid, limit=1)
        return int(rows[0]["sort_seq"]) if rows else 0
    except Exception:
        return 0


def db_after(gui, before: int):
    try:
        db = gui._get_db()
        hits = db.search_contact(WHO)
        uid = hits[0]["username"] if hits else "filehelper"
        rows = db.get_messages(uid, limit=5)
        rows = sorted(rows, key=lambda r: int(r.get("sort_seq") or 0),
                      reverse=True)
        fresh = [r for r in rows if int(r.get("sort_seq") or 0) > before]
        return [(str(r.get("type")), int(r.get("sort_seq") or 0))
                for r in fresh], uid
    except Exception as exc:
        print(f"     读库失败：{exc}")
        return [], ""


def make_gif(path: str) -> str:
    from PIL import Image, ImageDraw

    frames = []
    for i in range(3):
        im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.ellipse([20 + i * 10, 20, 236, 236], fill=(255, 210, 90, 255),
                  outline=(60, 45, 20, 255), width=4)
        frames.append(im)
    frames[0].save(path, save_all=True, append_images=frames[1:],
                   duration=150, loop=0, disposal=2)
    return path


def main() -> int:
    print("=" * 72)
    print("剪贴板诊断：谁写的内容微信读得懂")
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
            print(f"✗ 打不开 {WHO}")
            return 1
        time.sleep(0.6)
    try:
        gui.ensure_visible()
    except Exception:
        pass
    time.sleep(0.5)

    tmpdir = tempfile.mkdtemp(prefix="bigfish-clip-")
    gif = make_gif(os.path.join(tmpdir, "clip.gif"))
    print(f"\n素材：{gif}（{os.path.getsize(gif)} 字节）")

    results = {}
    try:
        for tag, writer, desc in (
                ("A", write_system, "PowerShell Set-Clipboard（系统标准写法）"),
                ("B", write_ours, "copy_files_to_clipboard（我们自己的）")):
            print("\n" + "─" * 72)
            print(f"轮次 {tag}：{desc}")
            print("─" * 72)

            # ① 先清干净
            try:
                import win32clipboard

                win32clipboard.OpenClipboard()
                win32clipboard.EmptyClipboard()
                win32clipboard.CloseClipboard()
            except Exception:
                pass
            time.sleep(0.2)

            ok = writer(gif)
            print(f"  写入结果：{ok}")
            time.sleep(0.3)

            # ② 立刻把剪贴板内容翻出来看 —— 这是核心证据
            dump = show_clipboard(f"{tag} 写入后")

            # ③ 粘贴 + 发送（和你手动做的完全一样）
            base = db_before(gui)
            paste_and_send(gui, edit, tag)
            fresh, _uid = db_after(gui, base)

            sent = bool(fresh)
            results[tag] = {
                "writer": desc,
                "clipboard_files": dump["files"],
                "sent": sent,
                "type": fresh[0][0] if fresh else "",
            }
            print(f"  → 发出去了？{'是' if sent else '否'}"
                  f"{('，类型 ' + fresh[0][0]) if fresh else ''}")

            time.sleep(1.5)

        print("\n" + "=" * 72)
        print("汇总")
        print("=" * 72)
        for tag, r in results.items():
            print(f"\n  轮次 {tag}：{r['writer']}")
            print(f"    剪贴板里读到的文件：{r['clipboard_files'] or '（空）'}")
            print(f"    发出去：{'是' if r['sent'] else '否'}"
                  f"{('（' + r['type'] + '）') if r['type'] else ''}")

        a, b = results.get("A", {}), results.get("B", {})
        print("\n" + "─" * 72)
        if a.get("sent") and not b.get("sent"):
            print("  ★ 系统写法成功、我们的失败")
            print("    → 问题在 copy_files_to_clipboard 的 DROPFILES，改写入函数")
            print(f"      系统写出的文件列表：{a['clipboard_files']}")
            print(f"      我们写出的文件列表：{b['clipboard_files']}")
        elif a.get("sent") and b.get("sent"):
            print("  ★ 两种写法都成功 —— 那粘贴路线是通的")
            print("    → 回头看 wechat_sender 里多做了哪一步")
        elif not a.get("sent") and not b.get("sent"):
            print("  ★ 两种都失败 —— 问题不在剪贴板")
            print("    → 粘贴动作本身有问题，和你手动做的还有别处不同")
        else:
            print("  ★ 我们的成功、系统的失败（少见）")
            print("    → 反而是好事，说明现有实现能用在某些路径上")
        print("─" * 72)
    finally:
        try:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
