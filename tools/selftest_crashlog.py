"""闪退现场体检：日志落盘 + 换页重建的接线。

跑法：
    python tools/selftest_crashlog.py

不弹窗、不联网、不碰用户真实数据（临时数据根在 .tmp/ 下）。

## 为什么需要这个

2026-11-11 15:07 和 11-12 12:45 两次闪退，Windows 事件日志都写着
`Qt6WebEngineCore.dll / 0xc0000005 / 偏移 0xf722d3` —— **同一处指令**。
但程序自己一个字都没留下：`Backend._log()` 只 `self.log.emit()`
发到 UI 面板，进程被系统杀掉时面板随窗口一起消失。

这一层补两件事：

1. `_log()` 同时落盘到 `<数据根>/logs/app.log`（滚动 5MB）；
2. 内存看门狗改成"每 10 分钟记一行、550MB 换页"，
   且换页用 `_rebuild_view()`（新 page）而不是原地 `load()`
   —— 原地重载正是那两次崩溃的发生点。
"""
from __future__ import annotations

import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

TMP = os.path.join(_ROOT, ".tmp", "selftest_crashlog")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
os.environ["BIGFISH_DATA_ROOT"] = TMP

from bigfish import desktop as D  # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []


def _pick_marks() -> tuple[str, str]:
    try:
        "\u2713".encode(sys.stdout.encoding or "utf-8")
        return "\u2713", "\u2717"
    except (UnicodeEncodeError, LookupError):
        return "[OK]", "[X]"


OK_MARK, BAD_MARK = _pick_marks()


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  {OK_MARK} {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  {BAD_MARK} {name}" + (f"  ({detail})" if detail else ""))


def main() -> int:
    print("=" * 60)
    print("  闪退现场体检 · crashlog")
    print("=" * 60)
    print()

    log_path = os.path.join(TMP, "logs", "app.log")

    # ── 1. 落盘 ──
    print("── 1 · 日志落盘 ──")
    D._append_log("[内存] 工作集 466MB", "think")
    D._append_log("[界面] 换页重建（测试）", "err")
    check("文件已创建", os.path.isfile(log_path), log_path)
    text = open(log_path, encoding="utf-8").read() if os.path.isfile(log_path) else ""
    check("写了三行", text.count("\n") == 2, str(text.count("\n")))
    check("带时间戳", text[:19].count(":") >= 2, text[:19])
    check("带级别", "[think]" in text and "[err]" in text, text[:80])
    check("内容完整", "466MB" in text and "换页重建" in text)

    # ── 2. 滚动 ──
    print()
    print("── 2 · 滚动 ──")
    orig_max = D._LOG_MAX_BYTES
    D._LOG_MAX_BYTES = 200          # 逼它立刻超限
    try:
        D._append_log("x" * 300, "info")
        D._append_log("滚动之后的第一行", "info")
    finally:
        D._LOG_MAX_BYTES = orig_max
    check("旧日志被改名 .1", os.path.isfile(log_path + ".1"), log_path + ".1")
    new_text = open(log_path, encoding="utf-8").read() if os.path.isfile(log_path) else ""
    check("新文件只留滚动后的内容", "滚动之后的第一行" in new_text,
          new_text[:80])
    check("旧内容没丢（在 .1 里）",
          "466MB" in open(log_path + ".1", encoding="utf-8").read())

    # ── 3. 接线 ──
    print()
    print("── 3 · 换页重建的接线 ──")
    check("_rebuild_view 存在", hasattr(D.MainWindow, "_rebuild_view"))
    check("_watch_memory 存在", hasattr(D.MainWindow, "_watch_memory"))
    check("QWebEnginePage 已导入", hasattr(D, "QWebEnginePage"))
    src = open(os.path.join(_ROOT, "bigfish", "desktop.py"), encoding="utf-8").read()
    check("看门狗不再原地 load",
          "self._rebuild_view(f\"{mb:.0f}MB 超阈值\")" in src)
    check("渲染进程退出也走换页",
          'self._rebuild_view("渲染进程退出")' in src)
    check("阈值是 550", "if mb < 550:" in src)
    check("周期是 10 分钟", "10 * 60 * 1000" in src)
    check("_log 调用落盘", "_append_log(text, kind)" in src)

    shutil.rmtree(TMP, ignore_errors=True)

    # ── 出口 ──
    print()
    print("=" * 60)
    total = PASS + FAIL
    if FAIL:
        print(f"失败 {FAIL}/{total}")
        for n in FAILED:
            print(f"  - {n}")
        return 1
    print(f"全部通过 {PASS}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
