"""[bigfish] 探测：输入框附近有哪些按钮。

剪贴板路线已确认走不通（微信拒绝程序注入的文件粘贴，文字却能进）。
改走「点附件按钮 → 文件对话框」，所以先得知道按钮在哪。

这个脚本把输入框周围 200px 内的所有 UIA 控件打出来，
带 AutomationId / ClassName / Name / 矩形，用来定位那个 📁 按钮。
"""
from __future__ import annotations

import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (_ROOT, _ROOT + "/vendor/wechatauto-replica"):
    if p not in sys.path:
        sys.path.insert(0, p)


def main() -> int:
    print("=" * 72)
    print("探测输入框附近的控件")
    print("=" * 72)

    import uiautomation as auto

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

    try:
        gui.ensure_visible()
    except Exception:
        pass
    time.sleep(0.5)

    box = gui.get_input_box()
    print(f"\n输入框相对矩形：{box}")
    if box:
        print(f"输入框屏幕矩形：{gui._rel_to_screen(box)}")

    er = None
    try:
        er = edit.BoundingRectangle
        print(f"输入框控件屏幕矩形："
              f"({er.left},{er.top},{er.right},{er.bottom})")
    except Exception as exc:
        print(f"读不到控件矩形：{exc}")

    # 从输入框控件往上走，找到整个会话面板，再往下扫按钮
    print("\n" + "─" * 72)
    print("从输入框控件向上找祖先，看结构")
    print("─" * 72)
    node = edit
    for depth in range(8):
        try:
            p = node.GetParentControl()
        except Exception:
            break
        if not p:
            break
        try:
            cls = p.ClassName or ""
            aid = getattr(p, "AutomationId", "") or ""
            nm = p.Name or ""
            r = p.BoundingRectangle
            print(f"  [{depth}] {cls:42s} aid={aid!r:28s} "
                  f"name={nm[:24]!r} rect=({r.left},{r.top},{r.right},{r.bottom})")
        except Exception as exc:
            print(f"  [{depth}] <读不到：{exc}>")
            break
        node = p

    # 在输入框的兄弟/后代里找按钮
    print("\n" + "─" * 72)
    print("输入框附近所有 ButtonControl / 可点控件")
    print("─" * 72)

    try:
        parent = edit.GetParentControl()
    except Exception:
        parent = None

    seen = 0
    if parent is not None:
        def walk(ctrl, depth=0):
            global seen
            if depth > 5 or seen > 200:
                return
            try:
                children = ctrl.GetChildren()
            except Exception:
                return
            for c in children:
                seen += 1
                try:
                    cls = c.ClassName or ""
                    aid = getattr(c, "AutomationId", "") or ""
                    nm = (c.Name or "")[:40]
                    ct = c.ControlTypeName
                    r = c.BoundingRectangle
                    # 只打印按钮类和有意思的
                    if ("Button" in ct or aid or
                            cls in ("mmui::XButton", "mmui::XIconButton")):
                        print(f"  d{depth} {ct:18s} {cls:36s} "
                              f"aid={aid!r:26s} name={nm!r} "
                              f"rect=({r.left},{r.top},{r.right},{r.bottom})")
                except Exception:
                    pass
                walk(c, depth + 1)

        walk(parent)

    # 再往上一层扫（工具栏可能在输入框的父级的父级）
    print("\n" + "─" * 72)
    print("再往上两层扫一遍")
    print("─" * 72)
    try:
        gp = parent.GetParentControl() if parent else None
        if gp:
            def walk2(ctrl, depth=0):
                if depth > 4:
                    return
                try:
                    children = ctrl.GetChildren()
                except Exception:
                    return
                for c in children:
                    try:
                        ct = c.ControlTypeName
                        aid = getattr(c, "AutomationId", "") or ""
                        if "Button" in ct and aid:
                            cls = c.ClassName or ""
                            r = c.BoundingRectangle
                            print(f"  d{depth} {ct:18s} {cls:34s} "
                                  f"aid={aid!r:26s} name={(c.Name or '')[:20]!r} "
                                  f"rect=({r.left},{r.top},{r.right},{r.bottom})")
                    except Exception:
                        pass
                    walk2(c, depth + 1)

            walk2(gp)
    except Exception as exc:
        print(f"  {exc}")

    print("\n" + "=" * 72)
    print("找 aid 里带 file/folder/attach 的，或者名字是「文件」的按钮。")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
