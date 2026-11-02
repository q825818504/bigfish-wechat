"""[bigfish] 对照：临时目录 vs 正常目录，同一套粘贴动作。

═══════════════════════════════════════════════════════════════════════
这个脚本要验证的假设
═══════════════════════════════════════════════════════════════════════

已经确定的事实：

    文字能输入进微信输入框        ✓（上沿、中心都试过）
    文件粘贴程序注入全部失败      ✗（SendKeys/keybd_event/SendInput/WM_PASTE）
    用户手动 Ctrl+V 文件          ✓ 成功，收到的是原生表情

但有一个差别一直没被当成变量：

    用户成功的那些次，剪贴板里的路径是
        D:\\大脑\\表情测试\\动图_512.gif          ← 正常目录
    脚本失败的那些次，路径是
        C:\\Users\\...\\AppData\\Local\\Temp\\bigfish-xxx\\k.gif   ← 临时目录

**微信可能拒绝从临时目录粘贴文件。**

这解释了所有矛盾：
    · 文字能进 —— 不涉及文件路径
    · 用户手动成功 —— 正常目录
    · 脚本全失败 —— 临时目录

═══════════════════════════════════════════════════════════════════════
测法
═══════════════════════════════════════════════════════════════════════

同一张图，复制到两个位置，用**完全相同**的粘贴代码各试一次：

    A  正常目录（D:\\大脑\\表情测试\\）
    B  临时目录（%TEMP%\\bigfish-pathtest-xxx\\）

只有路径不同，其他全一样。哪一组能进，答案就出来了。

用法：
    # 管理员
    python tools/path_probe.py
"""
from __future__ import annotations

import os
import shutil
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

#: 正常目录：和用户手动测试时一样的那个
NORMAL_DIR = r"D:\大脑\表情测试"


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


def db_latest(gui):
    try:
        db = gui._get_db()
        hits = db.search_contact(CHAT)
        uid = hits[0]["username"] if hits else "filehelper"
        rows = db.get_messages(uid, limit=3)
        rows = sorted(rows, key=lambda r: int(r.get("sort_seq") or 0),
                      reverse=True)
        return [(str(r.get("type")), int(r.get("sort_seq") or 0))
                for r in rows], uid
    except Exception:
        return [], ""


def try_paste(s, gui, edit, box, path: str, tag: str) -> dict:
    """清空 → 聚焦 → 写剪贴板 → Ctrl+V → 检查。"""
    from wechatauto.guia import WinInput

    print(f"\n{'─' * 70}")
    print(f"  {tag}")
    print(f"  路径：{path}")
    print(f"{'─' * 70}")

    if not os.path.isfile(path):
        print(f"     ✗ 文件不存在")
        return {"tag": tag, "ok": False, "why": "文件不存在"}

    try:
        edit.SendKeys("{Ctrl}a{Delete}", waitTime=0.1)
    except Exception:
        pass
    time.sleep(0.35)

    try:
        edit.SetFocus()
    except Exception:
        pass
    if box:
        try:
            gui.focus_input(box)
        except Exception:
            pass
    time.sleep(0.3)

    ok = s._write_clipboard_files(path)
    time.sleep(0.25)
    got = s._clipboard_files()
    print(f"     剪贴板：{got[:1] if got else '（空）'}")
    if not got:
        return {"tag": tag, "ok": False, "why": "剪贴板空"}

    WinInput().key(0x56, ctrl=True)
    time.sleep(1.4)

    ratio = color_ratio(gui, box)
    ok = ratio >= 0.02
    print(f"     彩色占比：{ratio:.1%}  {'✓ 进去了' if ok else '✗ 空的'}")

    return {"tag": tag, "ok": ok, "ratio": ratio, "path": path}


def main() -> int:
    print("=" * 72)
    print("对照：临时目录 vs 正常目录")
    print("=" * 72)

    # 准备两个位置
    os.makedirs(NORMAL_DIR, exist_ok=True)
    p_normal = os.path.join(NORMAL_DIR, "_pathtest.gif")
    make_gif(p_normal)

    tmpdir = tempfile.mkdtemp(prefix="bigfish-pathtest-")
    p_temp = os.path.join(tmpdir, "p.gif")
    shutil.copy2(p_normal, p_temp)

    print(f"\nA 正常目录：{p_normal}")
    print(f"B 临时目录：{p_temp}")

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
    print(f"\n输入框：{box}")

    results = []
    try:
        # 先测正常目录
        results.append(try_paste(s, gui, edit, box, p_normal, "A · 正常目录"))

        if results[-1]["ok"]:
            # 进去了就发出去，确认是表情
            base, _ = db_latest(gui)
            base_seq = base[0][1] if base else 0
            from wechatauto.guia import WinInput

            WinInput().key(0x0D)
            time.sleep(2.0)
            after, _ = db_latest(gui)
            fresh = [r for r in after if r[1] > base_seq]
            if fresh:
                print(f"     → 发出去了，类型 = {fresh[0][0]!r}")
                results[-1]["type"] = fresh[0][0]
            time.sleep(1.0)

        # 再测临时目录
        results.append(try_paste(s, gui, edit, box, p_temp, "B · 临时目录"))

        if results[-1]["ok"]:
            base, _ = db_latest(gui)
            base_seq = base[0][1] if base else 0
            from wechatauto.guia import WinInput

            WinInput().key(0x0D)
            time.sleep(2.0)
            after, _ = db_latest(gui)
            fresh = [r for r in after if r[1] > base_seq]
            if fresh:
                print(f"     → 发出去了，类型 = {fresh[0][0]!r}")
                results[-1]["type"] = fresh[0][0]
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
        try:
            os.remove(p_normal)
        except Exception:
            pass

    print("\n" + "=" * 72)
    print("结论")
    print("=" * 72)
    for r in results:
        print(f"  {'✓' if r['ok'] else '✗'} {r['tag']:16s} "
              f"{r.get('ratio', 0):6.1%}  "
              f"{r.get('type', '') or r.get('why', '')}")

    a = results[0] if results else {}
    b = results[1] if len(results) > 1 else {}
    print("\n" + "─" * 70)
    if a.get("ok") and not b.get("ok"):
        print("  ★ 正常目录能进、临时目录不能")
        print("    → 根因是路径。微信拒绝从临时目录粘贴文件。")
        print("    → 方案：发表情前把文件复制到数据目录下的临时位置")
        print("       （比如 <数据目录>/stickers/_outbox/），而不是 %TEMP%")
    elif a.get("ok") and b.get("ok"):
        print("  ★ 两个都能进 —— 那之前的失败是别的原因")
        print("    → 可能是那几次脚本恰好碰上微信状态不对")
    elif not a.get("ok") and not b.get("ok"):
        print("  ★ 两个都不能进 —— 路径不是原因")
        print("    → 那就是微信拒绝程序注入的文件粘贴，跟路径无关")
    else:
        print("  ★ 临时目录能进、正常目录不能（反常，记录下来）")
    print("─" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
