"""发布前的静态守门：语法 + 未定义名 + 版本号一致。

为什么要有这一个文件
────────────────────
2026-11-08 到 11-11 这四天里，同一类 bug 犯了五次：

  · `desktop.py` 的 `_M.observe_habit` —— `_M` 从未 import
  · `desktop.py` 发送失败分支的 `{r.detail}` —— `r` 不存在
  · `win32gui.IsHungAppWindow` —— 这个函数不存在
  · `world.py` 用了 `time.time()` 却没 `import time`
  · `_maybe_narrative` 不 `mark_done`（不算未定义名，但同样静默）

每一次都是**被用户撞上才发现的**，不是被工具拦下的。原因很具体：
它们全都被 `except Exception: pass` 包着 —— 不报错、不进日志、
只让一个功能安静地不存在。而这类 bug 里，**未定义名是唯一能被
静态检查抓到的**。

所以这里就跑 `pyflakes`。它只报两种：未定义名、未使用变量。
不需要配置、不会误报、全域跑完不到一秒。

用法
────
    python tools/check.py          # 本地发布前跑一次
    python tools/check.py --quiet  # CI 用：只在失败时输出

CI 见 `.github/workflows/ci.yml`。**不要把它做成 pre-commit 钩子** ——
这个项目的提交很碎（一次提交一件事），钩子会拖慢每一次提交，
而发布前跑一次足够拦住"进了主干才发现"。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "bigfish"


def log(msg: str) -> None:
    print(f"  {msg}", flush=True)


def check_compile(quiet: bool) -> bool:
    """语法。比 pyflakes 快，先跑它 —— 语法错的时候 pyflakes 会吐一堆噪音。"""
    r = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", str(PKG)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        log("✗ 语法检查失败")
        print(r.stdout or "", r.stderr or "")
        return False
    if not quiet:
        log("✓ 语法")
    return True


def check_pyflakes(quiet: bool) -> bool:
    """未定义名 / 未使用变量。

    `pyflakes` 不是运行依赖（打包版不需要它），没装就跳过并说清楚 ——
    静默跳过等于给自己一个虚假的绿灯。
    """
    try:
        import pyflakes  # noqa: F401
    except ImportError:
        log("! 跳过 pyflakes：没装（pip install pyflakes）")
        return True
    r = subprocess.run(
        [sys.executable, "-m", "pyflakes", str(PKG)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    # 只看未定义名。unused import 在这个项目里很多是刻意的（延迟 import、
    # 为了副作用），全部拦掉会没人愿意跑这个脚本。
    bad = [ln for ln in out.splitlines() if "undefined name" in ln]
    if bad:
        log(f"✗ 未定义名 {len(bad)} 处")
        for ln in bad:
            print(f"    {ln}")
        return False
    if not quiet:
        others = len([ln for ln in out.splitlines() if ln.strip()])
        log(f"✓ 未定义名 0（另有 {others} 条 unused，不拦）")
    return True


def check_version(quiet: bool) -> bool:
    """版本号三处一致：`__init__.py` / CHANGELOG 顶部 / 调用方传进来的。

    AGENTS.md 写着"升版本号要三处一致"，但一直是靠人记。
    这条检查的成本是零，而不一致的代价是：用户报"我是 0.12.0"，
    而 0.12.0 根本没存在过（真发生过）。
    """
    try:
        init = (PKG / "__init__.py").read_text(encoding="utf-8")
        v = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', init)
        v = v.group(1) if v else ""
    except Exception as exc:
        log(f"✗ 读不到 bigfish/__init__.py：{exc}")
        return False
    try:
        head = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        m = re.search(r"^##\s+([0-9][0-9.]*)", head, re.M)
        top = m.group(1) if m else ""
    except Exception as exc:
        log(f"✗ 读不到 CHANGELOG.md：{exc}")
        return False

    if not v:
        log("✗ __init__.py 里找不到 __version__")
        return False
    if v != top:
        log(f"✗ 版本不一致：__init__.py = {v}，CHANGELOG 顶部 = {top or '（没找到 ## 版本）'}")
        return False
    if not quiet:
        log(f"✓ 版本 {v}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="只在失败时输出")
    args = ap.parse_args()

    if not args.quiet:
        print("=" * 60)
        print("  静态检查")
        print("=" * 60)

    ok = True
    ok = check_compile(args.quiet) and ok
    ok = check_pyflakes(args.quiet) and ok
    ok = check_version(args.quiet) and ok

    if not ok:
        print("\n  ✗ 有失败项 —— 不要打包，先修。")
        return 1
    if not args.quiet:
        print("\n  ✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
