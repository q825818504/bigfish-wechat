"""用上游自己的方式定位输入框。

之前用递归遍历找不到，是因为控件可能在 UIA 的虚拟化列表里，
或者层级超过我设的深度限制。上游用的是
    win.EditControl(AutomationId="chat_input_field")
直接按条件搜索，不需要遍历。

本脚本照抄上游的做法，验证输入框能不能拿到。
"""
import ctypes
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "vendor", "wechatauto-replica"))

OUT = os.path.join(HERE, "_input_probe2.txt")
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
    section("0. 初始化")
    from wechatauto.uia_driver import WeChatUIA, CHAT_INPUT_AID, CHAT_INPUT_AIDS
    import uiautomation as auto

    say(f"  CHAT_INPUT_AID = {CHAT_INPUT_AID!r}")
    uia = WeChatUIA()
    say(f"  ensure_window -> {uia.ensure_window(wake=True)}")
    time.sleep(0.8)
    say(f"  is_materialized -> {uia.is_materialized()}")

    section("1. 打开会话")
    ok = uia.open_chat("文件传输助手")
    say(f"  open_chat -> {ok}")
    say(f"  current_chat -> {uia.current_chat()!r}")
    time.sleep(1)

    section("2. 找主窗口（用上游的方式）")
    hwnd = ctypes.windll.user32.FindWindowW("Qt51514QWindowIcon", None)
    if not hwnd:
        hwnd = ctypes.windll.user32.FindWindowW(None, "微信")
    say(f"  hwnd = {hwnd}")
    win = auto.ControlFromHandle(hwnd)
    say(f"  ClassName = {getattr(win,'ClassName',None)!r}")

    section("3. 按 AutomationId 搜索输入框（上游做法）")
    found = None
    for aids in ([CHAT_INPUT_AID], list(CHAT_INPUT_AIDS)):
        for aid in aids:
            try:
                e = win.EditControl(AutomationId=aid)
                if e and e.Exists(0, 0):
                    found = (aid, e)
                    say(f"  EditControl(AutomationId={aid!r}) 找到")
                    break
            except Exception as ex:
                say(f"  EditControl({aid!r}) 异常: {type(ex).__name__}: {ex}")
        if found:
            break

    if not found:
        say("  按 EditControl 没找到，试 Control(searchFromControl=win) 宽搜")
        try:
            for ct in ("EditControl", "DocumentControl", "TextControl"):
                try:
                    c = getattr(win, ct)()
                    if c and c.Exists(0, 0):
                        say(f"  {ct}() 命中: "
                            f"Class={getattr(c,'ClassName',None)!r} "
                            f"AID={getattr(c,'AutomationId','')!r}")
                except Exception:
                    pass
        except Exception as ex:
            say(f"  宽搜异常: {ex}")

    if found:
        aid, e = found
        section("4. 输入框详情")
        say(f"  AutomationId = {aid!r}")
        say(f"  ClassName    = {getattr(e,'ClassName',None)!r}")
        say(f"  Name         = {getattr(e,'Name',None)!r}")
        say(f"  ControlType  = {getattr(e,'ControlTypeName',None)!r}")
        try:
            r = e.BoundingRectangle
            say(f"  Rect         = ({r.left},{r.top},{r.right},{r.bottom})  "
                f"大小 {r.right-r.left}x{r.bottom-r.top}")
        except Exception as ex:
            say(f"  Rect 读取失败: {ex}")
        try:
            say(f"  IsEnabled    = {e.IsEnabled}")
        except Exception:
            pass

        section("5. 尝试读取/写入空内容（不发送）")
        try:
            vp = e.GetValuePattern()
            say(f"  ValuePattern 可用，当前值: {vp.Value!r}")
        except Exception as ex:
            say(f"  ValuePattern 不可用: {type(ex).__name__}")

        section("6. 结论")
        say("  输入框定位成功，send_text_to 应该能用。")
    else:
        section("6. 结论")
        say("  按 AutomationId 也没找到输入框。")
        say("  可能这个版本的 ChatInputField 用了别的 AutomationId。")
        say("  下一步：把 ChatMasterView 以下的完整子树 dump 出来。")

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
