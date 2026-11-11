"""[bigfish] 抓界面里的 JS 错误。

exe 没有开发者工具，界面出问题只能靠猜。
这个脚本用 QWebEngineView 加载真实界面，把所有 console 消息
（含未捕获异常）打出来，然后模拟点击表情包页的按钮。

用法：
    python tools/ui_console.py
"""
from __future__ import annotations

import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import bigfish.desktop as D  # noqa: E402


class Collector:
    def __init__(self):
        self.msgs = []

    def __call__(self, level, msg, line, source):
        # level: 0=info 1=warning 2=error
        tag = {0: "INFO", 1: "WARN", 2: "ERR "}.get(level, str(level))
        self.msgs.append((tag, msg, line, source))
        print(f"  [{tag}] {msg}"
              f"{f'  ({os.path.basename(source)}:{line})' if source else ''}")


def main() -> int:
    app = QApplication(sys.argv)
    win = D.MainWindow()
    win.resize(1280, 860)

    col = Collector()
    win.view.page().javaScriptConsoleMessage = (
        lambda level, msg, line, src: col(level, msg, line, src))

    win.show()

    steps = []

    def run(js, label, delay=600):
        steps.append((js, label, delay))

    # ① 先看 stickers.js 里几个关键函数在不在
    run("typeof loadStickers", "typeof loadStickers")
    run("typeof openStickerAdd", "typeof openStickerAdd")
    run("typeof openModal", "typeof openModal")
    run("typeof syncPluginNav", "typeof syncPluginNav")
    run("!!window.S", "!!window.S")
    run("!!(window.S && S.bridge)", "S.bridge 就绪")

    # ② 切到表情包页
    run("document.querySelector('.nav-item[data-page=\"stickers\"]').click()", "切页")

    # ③ 按钮在不在、有没有绑事件
    run("""(() => {
      const b = document.querySelector('#btnStickerAdd');
      if (!b) return 'no-button';
      return 'exists, hidden=' + b.hidden;
    })()""", "添加按钮状态")

    # ④ 直接调用函数，看会不会抛
    run("""(() => {
      try { openStickerAdd(); return 'called, modal=' +
        (document.getElementById('stickerAddModal').className || '(空)');
      } catch (e) { return 'EXC: ' + e.message; }
    })()""", "直接调 openStickerAdd")

    # ⑤ 再调后端，看返回值
    run("""(async () => {
      try {
        const r = await S.bridge.sticker_open_dir();
        return 'open_dir -> ' + String(r).slice(0, 80);
      } catch (e) { return 'EXC: ' + e.message; }
    })()""", "调 sticker_open_dir")

    idx = [0]

    def nxt():
        if idx[0] >= len(steps):
            QTimer.singleShot(800, finish)
            return
        js, label, delay = steps[idx[0]]
        idx[0] += 1

        def got(v):
            print(f"  → {label} = {v}")
            QTimer.singleShot(delay, nxt)

        win.view.page().runJavaScript(js, got)

    def finish():
        print()
        print("=" * 66)
        errs = [m for m in col.msgs if m[0].strip() == "ERR"]
        if errs:
            print(f"  ✗ 抓到 {len(errs)} 条 JS 错误：")
            for tag, msg, line, src in errs[:10]:
                print(f"     {msg}  ({os.path.basename(src)}:{line})")
        else:
            print("  ✓ 没有 JS 错误")
        print("=" * 66)
        app.quit()

    def on_load(ok):
        print("=" * 66)
        print("界面 JS 诊断")
        print("=" * 66)
        print(f"  loadFinished: {ok}")
        print()
        QTimer.singleShot(2500, nxt)

    win.view.loadFinished.connect(on_load)
    QTimer.singleShot(90000, app.quit)
    app.exec()
    return 0


if __name__ == "__main__":
    sys.exit(main())
