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

# ── 灵魂（可写） ──
expect("soul", "名字", "document.querySelector('#soulName').textContent",
       lambda v: v.strip() not in ("", "—"), "要有名字")
expect("soul", "称呼", "document.querySelector('#soulAddress').textContent",
       lambda v: v.strip() not in ("", "—"), "要有称呼")
expect("soul", "我是谁", "document.querySelector('#soulWhoIAm').textContent.length",
       lambda v: int(float(v)) > 100, "正文要够长")
expect("soul", "珍视条数", "document.querySelectorAll('#soulValues .value-edit').length",
       lambda v: int(float(v)) >= 4, "至少 4 条")
expect("soul", "性格滑杆数", "document.querySelectorAll('#soulTraits .trait-edit').length",
       lambda v: int(float(v)) >= 4, "至少 4 项")
expect("soul", "自知条数", "document.querySelectorAll('#soulNotes .note-edit').length",
       lambda v: int(float(v)) >= 3, "至少 3 条")

# 可写控件必须真的回填了值（空值 = 保存会把它清掉）
expect("soul", "名字输入框回填", "document.querySelector('#editName').value",
       lambda v: v.strip() != "", "输入框要有值")
expect("soul", "称呼输入框回填", "document.querySelector('#editAddress').value",
       lambda v: v.strip() != "", "输入框要有值")
expect("soul", "我是谁回填", "document.querySelector('#editWhoIAm').value.length",
       lambda v: int(float(v)) > 100, "文本域要有正文")
expect("soul", "字数统计", "document.querySelector('#whoCount').textContent",
       lambda v: int(float(v)) > 100, "要跟着文本域算")
expect("soul", "锁定标记", """(() => {
  const rows = [...document.querySelectorAll('#soulValues .value-edit')];
  return rows.filter(r => r.querySelector('.icon-btn.is-locked')).length;
})()""", lambda v: int(float(v)) >= 1, "至少一条标着已锁定")
expect("soul", "滑杆初值", """(() => {
  const s = document.querySelector('#soulTraits .trait-edit input[data-slider]');
  return s ? s.value : '';
})()""", lambda v: str(v).strip().isdigit(), "滑杆要有数值")
expect("soul", "性格描述句", """(() => {
  const d = document.querySelector('#soulTraits .trait-usage');
  return d ? d.textContent.length : 0;
})()""", lambda v: int(float(v)) > 3, "要有行为描述")
expect("soul", "注入预览", "document.querySelector('#soulPreview').textContent.length",
       lambda v: int(float(v)) > 100, "要能预览人格文本")
expect("soul", "保存按钮", "!!document.querySelector('#btnSoulSave')",
       lambda v: v is True, "按钮存在")

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

# 参数表单回填
expect("mood", "基线回填", "document.querySelector('#moodBase').value",
       lambda v: str(v).strip() != "", "要有值")
expect("mood", "平衰回填", "document.querySelector('#moodDecay').value",
       lambda v: str(v).strip() != "", "要有值")
expect("mood", "额度回填", "document.querySelector('#moodPerDay').value",
       lambda v: str(v).strip() != "", "要有值")
expect("mood", "注入方式回填", "document.querySelector('#moodInjectMode').value",
       lambda v: v.strip() in ("off", "label", "event"), "要是三者之一")
expect("mood", "保存按钮", "!!document.querySelector('#btnMoodSave')",
       lambda v: v is True, "按钮存在")

# ── 记忆 ──
expect("memory", "库统计", "document.querySelector('#memCount').textContent",
       lambda v: v.strip() not in ("", "—"), "要有统计")
expect("memory", "消息总数", "document.querySelector('#memTotal').textContent",
       lambda v: v.strip() not in ("", "—"), "要有数字")
expect("memory", "片段数", "document.querySelector('#memChunks').textContent",
       lambda v: v.strip() not in ("", "—"), "要有数字")
expect("memory", "分页信息", "document.querySelector('#memPageInfo').textContent",
       lambda v: "/" in v or "检索" in v, "要显示范围")
expect("memory", "会话清单", "document.querySelectorAll('#memSessions .session-row').length",
       lambda v: int(float(v)) >= 1, "至少一行")

# ── 数据目录 ──
expect("data", "数据根路径", "document.querySelector('#dataRootPath').value",
       lambda v: v.strip() != "", "要回填")
expect("data", "配置文件说明", "document.querySelector('#dataRootFile').textContent",
       lambda v: "root.json" in v, "要说明落盘位置")
expect("data", "份键", "document.querySelector('#scopeKeyTag').textContent",
       lambda v: v.strip() not in ("", "—", "未确定"), "要显示当前用哪一份")
expect("data", "选择下拉", "document.querySelector('#scopePick').options.length",
       lambda v: int(float(v)) >= 1, "至少有一项")
expect("data", "下拉项不是加载中", """(() => {
  const s = document.querySelector('#scopePick');
  const t = s.options.length ? s.options[0].textContent : '';
  return t.includes('加载中') ? 0 : 1;
})()""", lambda v: int(float(v)) == 1, "选项要真的填好")
expect("data", "下拉显示的是名字不是工作区标题", """(() => {
  const s = document.querySelector('#scopePick');
  if (!s.value) return 0;
  const opt = [...s.options].find(o => o.value === s.value);
  // 名字从灵魂里读；没写人格时显示「还没写人格」
  return opt && (opt.textContent.includes('灵魂') || opt.textContent.includes('人格')) ? 1 : 0;
})()""", lambda v: int(float(v)) == 1, "选项文案要对")
expect("data", "份列表", "document.querySelectorAll('#scopeList .session-row').length",
       lambda v: int(float(v)) >= 1, "至少要列出一份")
expect("data", "有当前标记", """(() => {
  return document.querySelectorAll('#scopeList .is-current').length;
})()""", lambda v: int(float(v)) == 1, "当前那份要标出来")
expect("data", "搬家按钮", "!!document.querySelector('#btnDataRootMove')",
       lambda v: v is True, "按钮存在")

# 关键：不许把 DSH 的工作区/会话列出来
expect("data", "没有工作区下拉", "!!document.querySelector('#scopeWorkspace')",
       lambda v: v is False, "不该存在（越界读 DSH 会话库）")
expect("data", "没有会话 id 输入框", "!!document.querySelector('#scopeSessions')",
       lambda v: v is False, "不该存在")
expect("data", "没有模式切换", "!!document.querySelector('#scopeMode')",
       lambda v: v is False, "不该存在")

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
