"""检查文件传输助手会话里的输入框控件。

预演时 chat_input_rect 返回 None，需要确认是：
  a) 那个辅助函数适用范围有限（不影响 send_text_to）
  b) 输入框真的找不到（那发送必然失败）

做法：直接遍历 UIA 树，把所有 Edit / 可输入控件列出来。
"""
import ctypes
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "vendor", "wechatauto-replica"))

OUT = os.path.join(HERE, "_input_probe.txt")
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


def main():
    section("0. 环境")
    from wechatauto.uia_driver import WeChatUIA

    uia = WeChatUIA()
    say("  WeChatUIA 构造成功")

    # 确保树物化
    say(f"  ensure_window(wake=True) -> {uia.ensure_window(wake=True)}")
    time.sleep(1)
    say(f"  is_materialized() -> {uia.is_materialized()}")

    section("1. 打开文件传输助手")
    ok = uia.open_chat("文件传输助手")
    say(f"  open_chat -> {ok}, 当前会话 = {uia.current_chat()!r}")
    time.sleep(1)

    section("2. 遍历控件树，找所有可编辑控件")
    import uiautomation as auto

    hwnd = ctypes.windll.user32.FindWindowW("Qt51514QWindowIcon", None)
    if not hwnd:
        hwnd = ctypes.windll.user32.FindWindowW(None, "微信")
    say(f"  主窗口 hwnd = {hwnd}")
    root = auto.ControlFromHandle(hwnd)
    say(f"  根节点: {getattr(root,'ClassName',None)!r} / "
        f"{getattr(root,'Name',None)!r}")

    edits = []
    allnodes = []

    def walk(c, depth=0, path=""):
        if depth > 12:
            return
        try:
            for ch in c.GetChildren():
                cn = getattr(ch, "ClassName", "") or ""
                nm = getattr(ch, "Name", "") or ""
                ct = getattr(ch, "ControlTypeName", "") or ""
                aid = ""
                try:
                    aid = ch.AutomationId or ""
                except Exception:
                    pass
                rec = (depth, cn, nm[:40], ct, aid[:40])
                allnodes.append(rec)
                if "Edit" in ct or "input" in cn.lower() or "Edit" in cn:
                    edits.append(rec)
                walk(ch, depth + 1, path + "/" + cn)
        except Exception:
            pass

    walk(root)

    say(f"  总节点数: {len(allnodes)}")
    say()
    say("  === 可编辑控件 ===")
    if not edits:
        say("    没有找到 Edit / input 控件")
    for d, cn, nm, ct, aid in edits:
        say(f"    depth={d} CalcControlType={ct}")
        say(f"      ClassName={cn!r}")
        say(f"      Name={nm!r}")
        say(f"      AutomationId={aid!r}")

    section("3. 树里所有 mmui:: 节点（前 40 个）")
    mmui = [n for n in allnodes if "mmui" in n[1].lower()]
    say(f"  共 {len(mmui)} 个")
    for d, cn, nm, ct, aid in mmui[:40]:
        say(f"    {'  '*min(d,6)}{cn}  {nm!r}")

    section("4. 尝试上游自己的输入框定位")
    try:
        from wechatauto import uia_driver as ud
        fns = [n for n in dir(ud) if "input" in n.lower()]
        say(f"  uia_driver 里 input 相关: {fns}")
        for n in fns:
            f = getattr(ud, n)
            if callable(f):
                try:
                    say(f"    {n}() -> {f()}")
                except Exception as e:
                    say(f"    {n}() 失败: {type(e).__name__}: {str(e)[:80]}")
    except Exception as e:
        say(f"  [!] {e}")

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
