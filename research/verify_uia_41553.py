"""用上游 WeChatUIA 验证 4.1.15.53 的热激活（提权运行）。

不再自己拼字节——上游 already 有一整套：
  - 版本表里有 4.1.15.53 -> 0x0B3959D8
  - 动态扫描作为后备
  - 写字节 -> 回读 UIA 树 -> 校验 -> 失败还原

本脚本只做两件事：
  1. 调 WeChatUIA，看它能不能把树拉起来
  2. 报告锚点（搜索框/输入框/发送按钮）能不能定位

不发送任何消息，不改微信任何文件。
"""
import ctypes
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "vendor", "wechatauto-replica"))

OUT = os.path.join(HERE, "_uia_41553.txt")
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
    say(f"  管理员: {bool(ctypes.windll.shell32.IsUserAnAdmin())}")
    say(f"  Python: {sys.version.split()[0]}")

    section("1. 导入上游 UIA 驱动")
    try:
        from wechatauto.uia_driver import WeChatUIA
        from wechatauto import accessibility as acc
        say("  导入成功")
        tbl = acc.QACCESSIBLE_ACTIVE_RVA_BY_VERSION
        say(f"  版本表条目: {len(tbl)} 个")
        for k, v in tbl.items():
            mark = "  ← 你的版本" if k == "4.1.15.53" else ""
            say(f"    {k}: 0x{v:08X}{mark}")
    except Exception as e:
        say(f"  [!] 导入失败: {type(e).__name__}: {e}")
        import traceback
        say(traceback.format_exc()[:1200])
        return 1

    section("2. 构造 WeChatUIA 并尝试物化")
    say("  这一步会调用上游的热激活（写 gate 字节 + 回读校验）。")
    say()
    import time
    t0 = time.time()
    try:
        uia = WeChatUIA()
        say(f"  WeChatUIA() 构造成功  {time.time()-t0:.1f}s")
    except Exception as e:
        say(f"  [!] 构造失败: {type(e).__name__}: {e}")
        import traceback
        say(traceback.format_exc()[:1500])
        return 1

    section("3. describe_layout()")
    try:
        info = uia.describe_layout()
        for k, v in (info or {}).items():
            say(f"  {k}: {str(v)[:120]}")
    except Exception as e:
        say(f"  [!] 失败: {type(e).__name__}: {e}")

    section("4. 独立检查 UIA 树")
    try:
        import uiautomation as auto
        hwnd = ctypes.windll.user32.FindWindowW("Qt51514QWindowIcon", None)
        say(f"  FindWindow(Qt51514QWindowIcon) -> {hwnd}")
        if hwnd:
            root = auto.ControlFromHandle(hwnd)
            say(f"  根节点 ClassName: {getattr(root,'ClassName',None)!r}")
            say(f"  根节点 Name     : {getattr(root,'Name',None)!r}")
            # 数 mmui 节点
            found = []
            def walk(c, d=0):
                if d > 4 or len(found) >= 20:
                    return
                try:
                    for ch in c.GetChildren():
                        cn = getattr(ch, "ClassName", "") or ""
                        if "mmui" in cn.lower():
                            found.append((d, cn, (getattr(ch,"Name","") or "")[:36]))
                            if len(found) >= 20:
                                return
                        walk(ch, d + 1)
                except Exception:
                    pass
            walk(root)
            say(f"  mmui 节点数: {len(found)}")
            for d, c, n in found[:15]:
                say(f"    {'  '*d}{c}  {n!r}")
    except Exception as e:
        say(f"  [!] 失败: {type(e).__name__}: {e}")

    section("5. 锚点定位")
    for name in ("search_box_rect", "get_input_box", "current_chat"):
        fn = getattr(uia, name, None)
        if not fn:
            say(f"  {name}: 不存在")
            continue
        try:
            r = fn()
            say(f"  {name}(): {r}")
        except Exception as e:
            say(f"  {name}(): 失败 {type(e).__name__}: {e}")

    section("6. 结论")
    say("  根节点变成 mmui::MainWindow = 热激活成功")
    say("  还是 Qt51514QWindowIcon     = 失败")
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
