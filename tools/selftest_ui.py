"""界面数据自检 —— 检查每个页面是否真的填上了数据。

不是「页面能加载」，而是「页面上那个元素里有没有内容」。

做法：
  起真实的 MainWindow，等界面就绪后，逐页切换，
  用 JavaScript 读 DOM 里关键元素的实际文本，判断是否为空/未加载。
"""
from __future__ import annotations

import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import bigfish.desktop as D  # noqa: E402

HR = "=" * 72
checks = []      # (页面, 元素, JS 表达式, 判定函数)
failures = []


def expect(page, label, js, ok_fn, desc=""):
    checks.append((page, label, js, ok_fn, desc))


# ── 概览 ──
expect("overview", "状态文字", "document.querySelector('#ovState').textContent",
       lambda v: v.strip() != "", "不能为空")
expect("overview", "账号标签", "document.querySelector('#accountLabel').textContent",
       lambda v: v.strip() != "—", "不能是占位符")
expect("overview", "自检项数", "document.querySelectorAll('#checkGrid .check-item').length",
       lambda v: int(float(v)) >= 6, "至少 6 项")
expect("overview", "自检有结果", """(() => {
  const items = [...document.querySelectorAll('#checkGrid .check-item')];
  return items.filter(i => !i.querySelector('.check-val').textContent.includes('检测中')).length;
})()""", lambda v: int(float(v)) >= 6, "不能停在「检测中」")

# ── 灵魂 ──
expect("soul", "名字", "document.querySelector('#soulName').textContent",
       lambda v: v.strip() not in ("", "—"), "要有名字")
expect("soul", "称呼", "document.querySelector('#soulAddress').textContent",
       lambda v: v.strip() not in ("", "—"), "要有称呼")
expect("soul", "我是谁", "document.querySelector('#soulWhoIAm').textContent.length",
       lambda v: int(float(v)) > 100, "正文要够长")
expect("soul", "珍视条数", "document.querySelectorAll('#soulValues .value-item').length",
       lambda v: int(float(v)) >= 4, "至少 4 条")
expect("soul", "性格格数", "document.querySelectorAll('#soulTraits .trait-item').length",
       lambda v: int(float(v)) >= 4, "至少 4 项")
expect("soul", "自知条数", "document.querySelectorAll('#soulNotes .note-item').length",
       lambda v: int(float(v)) >= 3, "至少 3 条")

# ── 心情 ──
expect("mood", "分数", "document.querySelector('#moodScore').textContent",
       lambda v: v.strip() not in ("", "—"), "要有数字")
expect("mood", "标签", "document.querySelector('#moodLabel').textContent",
       lambda v: v.strip() not in ("", "加载中…"), "要有标签")
expect("mood", "环形进度", "document.querySelector('#moodRing').getAttribute('stroke-dashoffset')",
       lambda v: v is not None and float(v) < 264, "进度要动过")
expect("mood", "维度条数", "document.querySelectorAll('#moodDims .dim-row').length",
       lambda v: int(float(v)) >= 6, "至少 6 维")
expect("mood", "注入原文", "document.querySelector('#moodInjected').textContent.length",
       lambda v: int(float(v)) > 10, "要显示注入文本")

# ── 记忆 ──
expect("memory", "库统计", "document.querySelector('#memCount').textContent",
       lambda v: v.strip() not in ("", "—"), "要有统计")

# ── 设置 ──
expect("settings", "接口地址", "document.querySelector('#cfgEndpoint').value",
       lambda v: v.strip() != "", "要回填")
expect("settings", "模型ID", "document.querySelector('#cfgModel').value",
       lambda v: v.strip() != "", "要回填")
expect("settings", "版本号", "document.querySelector('#aboutVersion').textContent",
       lambda v: v.strip() not in ("", "—"), "要显示版本")

# ── 会话（需要点击加载）──
expect("chats", "加载按钮", "!!document.querySelector('#btnLoadChats')",
       lambda v: v is True, "按钮存在")


class Runner:
    def __init__(self, app, win):
        self.app = app
        self.win = win
        self.i = 0
        self.current_page = None

    def start(self):
        QTimer.singleShot(3000, self.next)

    def next(self):
        if self.i >= len(checks):
            self.finish()
            return
        page, label, js, ok_fn, desc = checks[self.i]
        self.i += 1

        # 切页
        self.win.view.page().runJavaScript(
            f"document.querySelector('.nav-item[data-page=\"{page}\"]').click()")

        def read():
            self.win.view.page().runJavaScript(
                js, lambda v: self.verdict(page, label, v, ok_fn, desc))

        QTimer.singleShot(900, read)

    def verdict(self, page, label, value, ok_fn, desc):
        try:
            good = ok_fn(value)
        except Exception as exc:
            good = False
            value = f"{value!r} ({exc})"
        tag = "OK  " if good else "FAIL"
        shown = str(value)[:60].replace("\n", " ")
        print(f"  [{tag}] {page:9s} {label:10s} = {shown}")
        if not good:
            failures.append((page, label, value, desc))
        self.next()

    def finish(self):
        print()
        print(HR)
        if failures:
            print(f"  ✗ {len(failures)} 项不符")
            for page, label, value, desc in failures:
                print(f"     {page}/{label}: {value!r} —— 期望 {desc}")
        else:
            print("  ✓ 界面数据全部正常")
        print(HR)
        self.app.quit()


def main() -> int:
    app = QApplication(sys.argv)
    win = D.MainWindow()
    win.resize(1280, 860)
    win.show()

    runner = Runner(app, win)

    def on_load(ok):
        print(HR)
        print("界面数据自检")
        print(HR)
        print(f"  loadFinished: {ok}")
        print()
        if ok:
            QTimer.singleShot(2500, runner.start)
        else:
            print("  页面加载失败")
            app.quit()

    win.view.loadFinished.connect(on_load)
    QTimer.singleShot(60000, app.quit)
    app.exec()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
