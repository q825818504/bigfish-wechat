"""提权下调用上游的 accessibility 模块，让 mmui 控件树物化。

为什么不自研扫描：
    上游 accessibility.py 已经实现了
      扫 RVA → 写字节 → 回读校验 → 失败还原 的完整闭环，
    并且做过真机六臂对照验证。自己重写一遍只会引入偏差。

本脚本做的事：
    1. 确认当前是管理员
    2. 调上游 _wake_accessibility 的等价路径（accessibility 模块）
    3. 报告物化结果：根节点是 mmui::MainWindow 还是 Qt 外壳
    4. 顺便探测 key 控件（搜索框 / 输入框 / 发送按钮）能否定位

**只有校验通过才会保留那个字节**（上游实现里写死的行为）。
"""
import ctypes
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "vendor", "wechatauto-replica"))

OUT = os.path.join(HERE, "_uia_probe.txt")
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
    section("0. 权限")
    is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    say(f"  IsUserAnAdmin: {is_admin}")
    if not is_admin:
        say("  [!] 不是管理员，热激活必然失败（VM_READ/WRITE 都会被拒）")
        return 1

    section("1. 加载上游 accessibility 模块")
    try:
        from wechatauto import accessibility as acc
        say("  导入成功")
    except Exception as e:
        say(f"  [!] 导入失败: {type(e).__name__}: {e}")
        return 1

    # 看看这个模块暴露了什么
    names = [n for n in dir(acc) if not n.startswith("__")]
    public = [n for n in names if not n.startswith("_")]
    say(f"  公开接口: {public}")
    private = [n for n in names if n.startswith("_") and "wake" in n.lower()]
    say(f"  唤醒相关: {private}")

    section("2. 检查当前 UIA 状态（写之前）")
    try:
        import uiautomation as auto
        win = auto.ControlFromHandle(
            ctypes.windll.user32.FindWindowW("Qt51514QWindowIcon", None))
        if win:
            say(f"  根节点 Name: {win.Name!r}")
            say(f"  根节点 ClassName: {win.ClassName!r}")
        else:
            say("  没找到 Qt 窗口")
    except Exception as e:
        say(f"  探测失败: {type(e).__name__}: {e}")

    section("3. 调用上游的热激活")
    say("  这会往 Weixin.dll 写 1 个字节（gate），写完回读校验。")
    say("  校验失败会自动还原。失败的候选不会留下来。")
    say()
    t0 = time.time()
    try:
        # 上游的入口名可能是 wake / _wake_accessibility，两种都试
        fn = None
        for cand in ("wake", "wake_accessibility", "_wake_accessibility",
                     "activate", "ensure"):
            f = getattr(acc, cand, None)
            if callable(f):
                fn = f
                say(f"  找到入口: acc.{cand}")
                break
        if fn is None:
            say("  [!] 没找到可调用入口，列出全部函数：")
            for n in names:
                obj = getattr(acc, n)
                if callable(obj) and not n.startswith("__"):
                    say(f"      {n}{'' if not hasattr(obj,'__doc__') else ''}")
            return 1
        result = fn()
        say(f"  返回: {result!r}")
        say(f"  耗时: {time.time()-t0:.1f}s")
    except Exception as e:
        import traceback
        say(f"  [!] 调用失败: {type(e).__name__}: {e}")
        say(traceback.format_exc()[:1500])
        return 1

    section("4. 检查 UIA 状态（写之后）")
    try:
        import uiautomation as auto
        win = auto.ControlFromHandle(
            ctypes.windll.user32.FindWindowW("Qt51514QWindowIcon", None))
        if win:
            say(f"  根节点 Name: {win.Name!r}")
            say(f"  根节点 ClassName: {win.ClassName!r}")
            # 找 mmui 节点
            found = []
            def walk(ctrl, depth=0):
                if depth > 3:
                    return
                try:
                    for child in ctrl.GetChildren():
                        cn = getattr(child, "ClassName", "") or ""
                        if "mmui" in cn.lower():
                            found.append((depth, cn, child.Name[:40]))
                            if len(found) > 15:
                                return
                        walk(child, depth + 1)
                except Exception:
                    pass
            walk(win)
            say(f"  mmui 节点: {len(found)} 个")
            for d, c, n in found[:15]:
                say(f"    {'  '*d}{c}  {n!r}")
    except Exception as e:
        say(f"  探测失败: {type(e).__name__}: {e}")

    section("5. 结论")
    say("  根节点变成 mmui::MainWindow = 热激活成功，控件树可用")
    say("  还是 Qt51514QWindowIcon     = 失败（或 gate 已还原）")
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
