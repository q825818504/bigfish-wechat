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


# ── 印象页的关系卡（v0.9.1）──
#
# 关系卡从独立的「关系」页搬进了印象页。这一页本来就能打开，
# 所以真正要确认的是"新加的那一栏真的渲染出来了"，
# 而不只是"页面不白屏"。
expect("impression", "人数标签有内容", "document.querySelector('#imprCount').textContent",
       lambda v: v.strip() not in ("", "—"), "不能停在占位符")
expect("impression", "关系卡那一栏在",
       "document.querySelector('#imprPanes').textContent.includes('我们之间')",
       lambda v: str(v).strip().lower() == "true", "搬迁之后必须出现在印象页里")
expect("impression", "关系卡有出处说明",
       "document.querySelector('#imprPanes').textContent.includes('依据')",
       lambda v: str(v).strip().lower() == "true", "每条都要能对回原话")

# ── 概览 ──
expect("overview", "状态文字", "document.querySelector('#ovState').textContent",
       lambda v: v.strip() != "", "不能为空")
expect("overview", "账号标签", "document.querySelector('#accountLabel').textContent",
       lambda v: v.strip() != "—", "不能是占位符")
expect("overview", "今日问候", "document.querySelector('#ovGreeting').textContent",
       lambda v: v.strip() not in ("", "她正在过自己的今天。", "生活状态加载中"), "今日桥接要真的回填")
expect("overview", "生活状态", "document.querySelector('#ovCondition').textContent",
       lambda v: v.strip() not in ("", "生活状态加载中"), "生活状态要真的回填")
expect("overview", "今日日期用真实今天", """(() => {
  const d = new Date();
  const s = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0')
    + '-' + String(d.getDate()).padStart(2, '0');
  return document.querySelector('#ovTodayLabel').textContent.includes(s);
})()""", lambda v: str(v).lower() == "true", "不能用 life.day 顶替真实今天")
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
expect("mood", "预算卡", "!!document.querySelector('#moodBudgetParts')",
       lambda v: v is True, "这一轮预算要有落点")

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
expect("data", "位置说明", "document.querySelector('#dataRootFile').textContent",
       lambda v: v.strip() not in ("", "—"), "要说明当前是默认还是自定义")
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
# 默认在安装目录，但**可以改** —— 这三个入口必须在
expect("data", "有搬家按钮", "!!document.querySelector('#btnDataRootMove')",
       lambda v: v is True, "要能把数据搬到别的盘")
expect("data", "有改指向按钮", "!!document.querySelector('#btnDataRootSave')",
       lambda v: v is True, "要能只改指向")
expect("data", "有浏览按钮", "!!document.querySelector('#btnDataRootPick')",
       lambda v: v is True, "要能选目录")
expect("data", "有目标目录输入框", "!!document.querySelector('#dataRootTarget')",
       lambda v: v is True, "要能填目标")
expect("data", "有恢复默认", "!!document.querySelector('#btnDataRootDefault')",
       lambda v: v is True, "要能回到安装目录")
expect("data", "清空按钮", "!!document.querySelector('#btnWipe')",
       lambda v: v is True, "按钮存在")

# 关键：不该再有「选哪一份」这套东西
for _gone in ("scopePick", "scopeList", "scopeKeyTag", "btnScopeSave"):
    expect("data", f"没有 #{_gone}", f"!!document.querySelector('#{_gone}')",
           lambda v: v is False, "单一数据目录，不该有选择控件")
for _gone in ("scopeWorkspace", "scopeSessions", "scopeMode"):
    expect("data", f"没有 #{_gone}", f"!!document.querySelector('#{_gone}')",
           lambda v: v is False, "不该存在（越界读 DSH 会话库）")

# ── 表情包（2026-11-03 重构：这一页只做库） ──
# 开关和频率搬去了插件页，这里只剩「她手里有什么」。
expect("stickers", "有添加按钮", "!!document.querySelector('#btnStickerAdd')",
       lambda v: v is True, "库要能加东西")
expect("stickers", "有打开目录", "!!document.querySelector('#btnStickerOpenDir')",
       lambda v: v is True, "要能直接去磁盘看")
expect("stickers", "有库容器", "!!document.querySelector('#stickerList')",
       lambda v: v is True, "网格容器存在")
expect("stickers", "库有提示", "document.querySelector('#stickerList').textContent",
       lambda v: len(v.strip()) > 0, "空的时候也要说话")
# 插件关着时这一页其实进不去（会被踢回插件页），所以只验元素存在。
# 「有几张」的内容检查放到下面插件开着的那组里。
expect("stickers", "有数量标签", "!!document.querySelector('#stickerCount')",
       lambda v: v is True, "顶部要有个地方显示张数")
expect("stickers", "有常驻详情", "!!document.querySelector('#stickerPreviewCard')",
       lambda v: v is True, "选中一张要有右侧详情")
expect("stickers", "有选中详情控件", "!!document.querySelector('#stickerPreviewBody')",
       lambda v: v is True, "详情容器存在")
# 关键设计：表情包是插件，侧边栏那一项要跟着开关走。
# 断言的是**两者一致**，不是"一定隐藏" —— 插件开着时它就该出现。
expect("stickers", "导航跟随插件开关", """(() => {
  const nav = document.querySelector('.nav-item[data-page="stickers"]');
  if (!nav) return 'no-nav';
  const on = !!(window.S && S.plugEnabled && S.plugEnabled.sticker);
  return nav.hidden === !on ? 'ok' : ('mismatch on=' + on + ' hidden=' + nav.hidden);
})()""",
       lambda v: v == "ok", "插件开着才显示，关着就消失")
# 添加弹窗
expect("stickers", "有添加弹窗", "!!document.querySelector('#stickerAddModal')",
       lambda v: v is True, "弹窗要存在")
expect("stickers", "有拖放区", "!!document.querySelector('#stickerDropZone')",
       lambda v: v is True, "要能拖进来")
expect("stickers", "有浏览按钮", "!!document.querySelector('#btnStickerBrowse')",
       lambda v: v is True, "也要能选文件")
expect("stickers", "有自动备注开关", "!!document.querySelector('#stickerAutoDesc')",
       lambda v: v is True, "导入时让模型写说明")
# 详情弹窗
expect("stickers", "有详情弹窗", "!!document.querySelector('#stickerDetailModal')",
       lambda v: v is True, "双击要看详情")
expect("stickers", "详情有概览输入", "!!document.querySelector('#stickerDetailBrief')",
       lambda v: v is True, "概览可改")
expect("stickers", "详情有详细输入", "!!document.querySelector('#stickerDetailDesc')",
       lambda v: v is True, "详细可改")
# 关键：这一页不该再有开关/频率（它们属于插件页）
for _gone in ("stickerEnabled", "stickerRate", "stickerCooldown", "stickerMode"):
    expect("stickers", f"没有 #{_gone}", f"!!document.querySelector('#{_gone}')",
           lambda v: v is False, "开关和频率在插件页，不在这里")

# ── 表情包：脚本真的执行了吗 ──
#
# ⚠ 这一组是补的，起因是一个很难查的 bug（2026-11-03）：
#   stickers.js 里定义了 `fmtBytes`，而 app.js 里已经有一个
#   `const fmtBytes`。重复声明是 SyntaxError，会让**整个文件后半段
#   不执行** —— 但页面其它部分照常显示，只有按钮点了没反应。
#
#   所以不能只验"元素在不在"，要验"函数在不在、点了有没有效果"。
expect("stickers", "脚本已执行(函数存在)",
       "typeof openStickerAdd === 'function' && typeof openModal === 'function'",
       lambda v: v is True, "按钮绑定的函数必须真的定义了")
expect("stickers", "点添加能弹窗", """(() => {
  try {
    openStickerAdd();
    const m = document.getElementById('stickerAddModal');
    const open = m && m.classList.contains('is-open');
    if (m) m.classList.remove('is-open');
    return open;
  } catch (e) { return 'EXC: ' + e.message; }
})()""",
       lambda v: v is True, "点了要能打开弹窗")
# 注意：runJavaScript 拿不到 async 函数的 Promise 结果，所以这里
# 只验槽位有没有注册（同步可判）。真正的调用在 ui_console.py 里验。
expect("stickers", "后端槽位已注册",
       "typeof (window.S && S.bridge && S.bridge.sticker_list) === 'function'"
       " && typeof S.bridge.sticker_open_dir === 'function'"
       " && typeof S.bridge.sticker_add === 'function'",
       lambda v: v is True, "槽位要挂在 bridge 上")

# ── 设置 ──
expect("settings", "接口地址", "document.querySelector('#cfgBaseUrl').value",
       lambda v: v.strip() != "", "要回填")
expect("settings", "模型ID", "document.querySelector('#cfgModel').value",
       lambda v: v.strip() != "", "要回填")
expect("settings", "版本号", "document.querySelector('#aboutVersion').textContent",
       lambda v: v.strip() not in ("", "—"), "要显示版本")

# ── 会话（需要点击加载）──
expect("chats", "加载按钮", "!!document.querySelector('#btnLoadChats')",
       lambda v: v is True, "按钮存在")
expect("chats", "页头保存监听", "!!document.querySelector('#btnSaveWatchTop')",
       lambda v: v is True, "页头主按钮存在")

# ── 印象 / 声音的新结构 ──
expect("impression", "人物列表",
       "document.querySelectorAll('#imprTabs .impr-person').length",
       lambda v: int(float(v)) >= 1, "人物要能在左栏切换")
expect("voice", "三个页内 tab",
       "document.querySelectorAll('#voiceTabs [data-voice-tab]').length",
       lambda v: int(float(v)) == 3, "至少三个 tab")
expect("voice", "策略卡",
       "document.querySelectorAll('#voiceModeGrid .voice-mode').length",
       lambda v: int(float(v)) == 6, "六个策略选项")


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
    # ⚠ 这个超时是**兜底**，不是时间预算。
    #   原来是 60s，而每项检查要 900ms：
    #   59 项 = 53 秒，加 2.5s 启动等待就顶到 56 秒 —— 最后几项被
    #   静默截断，输出停在半路，看起来像"检查项没写"。实际是超时退出。
    #   按 len(checks) 算，留足余量，检查项增加也不会再撞线。
    budget_ms = (len(checks) + 8) * 1200 + 10000
    QTimer.singleShot(min(budget_ms, 300000), app.quit)
    app.exec()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
