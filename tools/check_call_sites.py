"""调用点静态检查 —— 抓"函数定义改了、调用点没跟上"。

跑法：
    python tools/check_call_sites.py

不需要微信、不需要桌面、不需要网络。

## 为什么需要它

`desktop.py` 的引擎是一个 4000 多行的函数，里面套着十几个闭包
（`handle` / `_trace_identity` / `_pick_sticker` …）。给某个闭包加一个参数，
调用它的地方可能有三四层深 —— **肉眼看不出来，跑起来才炸**。

而且炸的方式很难看：不是在启动时报错，是在**处理某条消息时**抛
`TypeError`，把引擎线程打挂。11-05 修 B-1 时就这么错过一次：
给 `_trace_identity` 加了 `decision=`，desktop 里那个定义没跟上。

这个检查把这件事提前到"改完就跑得出来"：

  · 只查**模块内定义的函数**（含嵌套闭包）
  · 关键字必须存在于该函数的参数表里
  · 位置参数个数不能超过形参个数
  · 解包调用（`*args` / `**kwargs`）一律跳过，宁漏不误报
  · 同名函数定义在多处时跳过（无法确定调的是哪个）

它不试图做类型检查，也不懂鸭子类型 —— 只回答一个问题：
**这个名字，能这么调吗？**
"""
from __future__ import annotations

import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

#: 检查这些文件（项目里闭包最多、最容易被改坏的几个）
TARGETS = (
    "bigfish/desktop.py",
    "bigfish/app.py",
    "bigfish/say.py",
    "bigfish/watcher.py",
)


def _params(fn: ast.AST) -> tuple[list[str], set[str], bool]:
    """返回 (位置形参名, 全部形参名, 是否接受 **kwargs)。"""
    args = fn.args
    positional = [a.arg for a in list(args.posonlyargs) + list(args.args)]
    names = set(positional)
    names |= {a.arg for a in args.kwonlyargs}
    names |= {a.arg for a in (args.vararg, args.kwarg) if a is not None}
    accepts_kwargs = args.kwarg is not None
    return positional, names, accepts_kwargs


def check_file(path: str) -> list[str]:
    source = open(path, encoding="utf-8").read()
    tree = ast.parse(source)

    # 收集所有函数定义（含嵌套）。同名多定义 → 视为不确定，跳过。
    defs: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.setdefault(node.name, []).append(node)

    problems: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name):
            continue
        name = node.func.id
        limited = defs.get(name)
        if not limited or len(limited) != 1:
            continue                      # 没定义 / 同名多处 → 不猜
        positional, names, accepts_kwargs = _params(limited[0])

        if any(isinstance(a, ast.Starred) for a in node.args):
            continue                      # 解包 → 数不清
        if len(node.args) > len(positional):
            problems.append(
                f"{path}:{node.lineno} 调用 {name}(...) 传了 {len(node.args)} 个"
                f"位置参数，但只定义了 {len(positional)} 个")
        for kw in node.keywords:
            if kw.arg is None:
                continue                  # **kwargs 解包
            if kw.arg not in names and not accepts_kwargs:
                problems.append(
                    f"{path}:{node.lineno} 调用 {name}(...) 用了不存在的参数 "
                    f"{kw.arg!r}（可用：{', '.join(sorted(names)) or '无'}）")
    return problems


def main() -> int:
    print("调用点静态检查")
    total = 0
    for rel in TARGETS:
        path = os.path.join(_ROOT, rel)
        if not os.path.isfile(path):
            print(f"  - 跳过（不存在）{rel}")
            continue
        found = check_file(path)
        total += len(found)
        if found:
            for item in found:
                print(f"  \u2717 {item}")
        else:
            print(f"  \u2713 {rel}")

    print(f"\n{'=' * 46}")
    if total:
        print(f"发现 {total} 处调用点与定义不符")
        return 1
    print("全部通过 0 处不符")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
