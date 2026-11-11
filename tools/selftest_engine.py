"""启停流程自检 —— 真实走一遍「设置监听 → 启动引擎 → 停止引擎」。

⚠️ 关键：QThread 必须有 Qt 事件循环才能调度。
   上一版测试只 QApplication([]) 不开 exec()，线程永远不启动，
   误报成「启动失败」—— 是测试写法的问题，不是代码 bug。
   这里用 QTimer 驱动整个流程，最后 app.exec() 收尾。
"""
from __future__ import annotations

import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import bigfish.desktop as D  # noqa: E402

HR = "=" * 72
fails = []


def ok(label, cond, extra=""):
    print(f"  [{'OK  ' if cond else 'FAIL'}] {label}  {extra}")
    if not cond:
        fails.append(label)


class Flow:
    def __init__(self, app):
        self.app = app
        self.b = D.Backend(None)
        self.logs = []
        self.b.log_message.connect(lambda t, k: self.logs.append((k, t)))
        self.step = 0

    def start(self):
        QTimer.singleShot(200, self.s1)

    # 1. 未选会话
    def s1(self):
        print("\n1. 未选会话时启动")
        self.b.watching = []
        # ⚠ 必须连**配置兜底**一起断掉。`start_engine` 里有一段
        #   "配置里明明有，就别让用户去取消再勾一次"的自动恢复 ——
        #   只清 self.watching 的话，它会把会话从配置里捡回来，于是
        #   "没选会话"这一条永远测不到（测试里跑着跑着就 dry 启动了）。
        self.b._watching_from_config = lambda: []
        r = json.loads(self.b.start_engine())
        ok("返回明确错误", r.get("ok") is not True and "error" in r, f"-> {r}")
        st = json.loads(self.b.get_status())
        ok("状态仍是未启动", st["running"] is False)
        QTimer.singleShot(200, self.s2)

    # 2. 选会话后启动
    def s2(self):
        print("\n2. 选了会话后启动")
        self.b.watching = ["filehelper"]
        r = json.loads(self.b.start_engine())
        ok("启动返回 ok", r.get("ok") is True, f"-> {r}")
        self.wait_running(0)

    def wait_running(self, n):
        if n > 30:
            st = json.loads(self.b.get_status())
            ok("状态变为运行中", False, f"超时 running={st['running']}")
            self.s4()
            return
        st = json.loads(self.b.get_status())
        if st["running"]:
            ok("状态变为运行中", True, f"({n*0.3:.1f}s)")
            self.s3()
            return
        QTimer.singleShot(300, lambda: self.wait_running(n + 1))

    # 3. 账号
    def s3(self):
        print("\n3. 账号显示")
        st = json.loads(self.b.get_status())
        ok("账号名非空", bool(st.get("account")), f"-> {st.get('account')!r}")
        ok("不是占位符", st.get("account") not in ("—", ""))
        ok("有引擎日志", len(self.logs) > 0, f"{len(self.logs)} 条")
        QTimer.singleShot(300, self.s4)

    # 4. 重复启动
    def s4(self):
        print("\n4. 重复启动")
        r = json.loads(self.b.start_engine())
        ok("不会起第二个引擎", r.get("ok") is True)
        QTimer.singleShot(300, self.s5)

    # 5. 停止
    def s5(self):
        print("\n5. 停止引擎")
        self.b.stop_engine()
        QTimer.singleShot(1500, self.s6)

    def s6(self):
        st = json.loads(self.b.get_status())
        ok("状态回到未启动", st["running"] is False, f"running={st['running']}")
        ok("worker 已释放", self.b.worker is None)
        QTimer.singleShot(300, self.s7)

    # 6. 再启动
    def s7(self):
        print("\n6. 再次启动")
        self.b.watching = ["filehelper"]
        r = json.loads(self.b.start_engine())
        ok("能再次启动", r.get("ok") is True, f"-> {r}")
        self.wait2(0)

    def wait2(self, n):
        if n > 30:
            ok("再次运行中", False)
            self.done()
            return
        st = json.loads(self.b.get_status())
        if st["running"]:
            ok("再次运行中", True)
            self.b.stop_engine()
            QTimer.singleShot(800, self.done)
            return
        QTimer.singleShot(300, lambda: self.wait2(n + 1))

    def done(self):
        print()
        print(HR)
        if fails:
            print(f"  ✗ {len(fails)} 项失败: {fails}")
        else:
            print("  ✓ 启停流程全部正常")
        print(HR)
        self.app.quit()


def main() -> int:
    app = QApplication(sys.argv)
    print(HR)
    print("启停流程自检")
    print(HR)
    f = Flow(app)
    QTimer.singleShot(200, f.start)
    QTimer.singleShot(90000, app.quit)
    app.exec()
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
