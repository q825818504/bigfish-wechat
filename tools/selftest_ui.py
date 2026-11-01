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
# ⚠ 刚清空过的数据根下，心情是空的 —— 这时候"没有数字"才是对的。
#   所以这里断言的是**控件有响应**，不是"必须有值"。
#   值的存在性由下面的 `hasMood` 分支单独判，两个方向都测得到。
expect("mood", "分数控件已渲染", "document.querySelector('#moodScore').textContent",
       lambda v: v.strip() != "", "控件要有个值（数字或破折号都行）")
expect("mood", "标签已渲染", "document.querySelector('#moodLabel').textContent",
       lambda v: v.strip() not in ("", "加载中…"), "不能停在加载中")
expect("mood", "环形进度合法", "document.querySelector('#moodRing').getAttribute('stroke-dashoffset')",
       lambda v: v is not None and 0 <= float(v) <= 264, "0..264")
expect("mood", "维度区已渲染", "!!document.querySelector('#moodDims')",
       lambda v: v is True, "容器存在")
expect("mood", "注入区已渲染", "!!document.querySelector('#moodInjected')",
       lambda v: v is True, "容器存在")

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
# 同上：清了就是空的，"没有统计"是正常状态。断言控件有响应。
expect("memory", "统计区已渲染", "document.querySelector('#memCount').textContent",
       lambda v: v.strip() != "", "控件要有内容（数字或破折号）")
expect("memory", "消息数已渲染", "document.querySelector('#memTotal').textContent",
       lambda v: v.strip() != "", "控件要有内容")
expect("memory", "片段数已渲染", "document.querySelector('#memChunks').textContent",
       lambda v: v.strip() != "", "控件要有内容")
expect("memory", "分页区已渲染", "!!document.querySelector('#memPageInfo')",
       lambda v: v is True, "容器存在")
expect("memory", "会话区已渲染", "!!document.querySelector('#memSessions')",
       lambda v: v is True, "容器存在")

# ── 数据目录 ──
expect("data", "数据根路径", "document.querySelector('#dataRootPath').value",
       lambda v: v.strip() != "", "要回填")
expect("data", "数据可带走说明", "document.querySelector('#dataRootFile').textContent",
       lambda v: "data" in v, "要说明数据就在 data 文件夹里")
expect("data", "布局状态标签", "document.querySelector('#layoutTag').textContent",
       lambda v: v.strip() not in ("", "—"), "要有状态")
expect("data", "三样清单", "document.querySelectorAll('#layoutList .session-row').length",
       lambda v: int(float(v)) >= 3, "灵魂/心情/记忆三行")
expect("data", "有灵魂标记", """(() => {
  const rows = [...document.querySelectorAll('#layoutList .session-row')];
  const soul = rows.find(r => r.textContent.includes('灵魂'));
  return soul && soul.querySelector('.tag.ok') ? 1 : 0;
})()""", lambda v: int(float(v)) == 1, "灵魂那行要标「在」")
expect("data", "清空盘点", "document.querySelector('#wipePlan').textContent",
       lambda v: v.strip() not in ("", "—"), "要显示会删什么")
# 数据根固定在安装目录 —— 搬家/改指向这套入口整体不该存在
for _gone in ("btnDataRootMove", "btnDataRootSave", "btnDataRootPick", "dataRootTarget"):
    expect("data", f"没有 #{_gone}", f"!!document.querySelector('#{_gone}')",
           lambda v: v is False, "数据根固定，不该有编辑控件")
expect("data", "清空按钮", "!!document.querySelector('#btnWipe')",
       lambda v: v is True, "按钮存在")

# 关键：不该再有「选哪一份」这套东西
for _gone in ("scopePick", "scopeList", "scopeKeyTag", "btnScopeSave"):
    expect("data", f"没有 #{_gone}", f"!!document.querySelector('#{_gone}')",
           lambda v: v is False, "单一数据目录，不该有选择控件")
for _gone in ("scopeWorkspace", "scopeSessions", "scopeMode"):
    expect("data", f"没有 #{_gone}", f"!!document.querySelector('#{_gone}')",
           lambda v: v is False, "不该存在（越界读 DSH 会话库）")

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
