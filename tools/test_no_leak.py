#!/usr/bin/env python
"""大肥鱼 · 越界访问测试。

守一条规矩：**EXE 是独立程序，不许去翻 DSH 宿主的会话库。**

    python tools/test_no_leak.py

为什么值得单开一份：
    「数据目录」页一开始会去读 <DSH_HOME>/sessions/ 和 workspace.json，
    把 DSH 里的所有工作区标题、会话 id 列成一个下拉框。功能上"能用"，
    但那是越界 —— 用户在微信里点一下，就能看见宿主里的全部对话目录。
    而且自相矛盾：微信端本来就不跟工作区绑。

    这种事不会让测试变红（它是"多做了"而不是"做错了"），
    所以只能靠一个专门断言"不该存在什么"的测试守住。
"""
from __future__ import annotations

import os
import sys

for _s in (sys.stdout, sys.stderr):
    if _s is None:
        continue
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  ✗ {name}" + (f"  —— {detail}" if detail else ""))


def section(t: str) -> None:
    print(f"\n── {t} " + "─" * max(0, 52 - len(t)))


def read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


def _strip_py(src: str) -> str:
    """剥掉 Python 注释和所有字符串字面量之外的部分 —— 这里用于全文件扫描。

    只保留字符串字面量（因为路径就写在字符串里）+ 标识符。
    docstring 也会被算进来，所以调用方要允许"注释里提到过"的情况。
    """
    import ast

    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    parts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            parts.append(node.value)
        elif isinstance(node, ast.Attribute):
            parts.append(node.attr)
        elif isinstance(node, ast.Name):
            parts.append(node.id)
    return " ".join(parts)


def _func_code(path: str, fname: str) -> str | None:
    """取某个函数**真正会执行的代码**（不含 docstring / 注释）。

    用 AST 而不是字符串切片：
      · 切片会带进下一个函数的头部，缩进不一致，tokenize 直接抛；
      · 切片也分不清「注释里提到 workspace.json」和「代码里真的读它」。

    只收集字面量字符串和标识符。函数 docstring 是一个独立的 Constant 节点，
    通过"把它记下来再排除"来处理 —— 注意不能靠"是不是第一个语句"判断，
    因为装饰器节点也在 body[0] 之前。
    """
    import ast

    src = read(path)
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None

    for node in ast.walk(tree):
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == fname):
            continue

        # 找出这个函数自己的 docstring 节点，排除它
        doc_node = None
        if node.body and isinstance(node.body[0], ast.Expr) \
                and isinstance(node.body[0].value, ast.Constant) \
                and isinstance(node.body[0].value.value, str):
            doc_node = node.body[0].value

        parts: list[str] = []
        for child in ast.walk(node):
            if child is doc_node:
                continue
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                parts.append(child.value)        # 路径字面量要保留
            elif isinstance(child, ast.Attribute):
                parts.append(child.attr)
            elif isinstance(child, ast.Name):
                parts.append(child.id)
        return " ".join(parts)

    return None


def main() -> int:
    print("=" * 58)
    print("越界访问检查")
    print("=" * 58)

    # ── 1. 代码里不许出现 DSH 会话库路径 ──
    section("1 · 不许读 DSH 宿主数据")
    desktop = read("bigfish/desktop.py")

    # 用 AST 提取真正的代码（不含注释 / docstring），
    # 否则"注释里说以前读过 workspace.json"会被误判成违规。
    # get_scope_candidates 整个删掉了（那个函数就是越界的来源）。
    # 现在守的是：**任何**函数都不许碰 DSH 的会话/工作区数据。
    for fname in ("get_data_root", "get_scope_candidates", "get_persona",
                  "get_mood", "memory_stats"):
        body = _func_code("bigfish/desktop.py", fname)
        if body is None:
            continue          # 函数已经不存在，那就更没有越界可能
        check(f"{fname} 不读 workspace.json", "workspace.json" not in body)
        # ⚠ 只判"路径拼接"，不判 "sessions" 这个词本身：
        #   memory_stats 返回的 "sessions" 是**记忆库自己的会话计数**，
        #   和 DSH 的 sessions 目录毫无关系。词面命中会误报。
        check(f"{fname} 不拼 DSH 的 sessions 路径",
              "os.path.join" not in body or "sessions" not in body,
              "疑似还在拼 <DSH_HOME>/sessions")

    # 全局：不该出现 DSH 会话/存储目录的拼接
    for pat in ('os.path.join(home, "sessions")',
                'storages", "workspace.json'):
        check(f"全文件不含 {pat}", pat not in desktop, "残留 DSH 路径拼接")

    # ── 2. 整个桌面模块不许出现宿主的会话库路径 ──
    section("2 · 全模块扫描")
    code = _strip_py(desktop)
    for pat, why in (
        ("workspace.json", "DSH 的工作区登记表"),
        ("session.v4", "DSH 的会话文件"),
    ):
        check(f"代码里没有 {pat}", pat not in code, f"还在碰 {why}")

    # ── 3. 界面不许有工作区/会话选择控件 ──
    section("3 · 界面控件")
    html = read("ui/index.html")
    js = read("ui/app.js")
    # 不只是不该有工作区/会话控件 —— "选哪一份"这套整体都不该有。
    for elem in ("scopeWorkspace", "scopeSessions", "scopeMode",
                 "scopePick", "scopeList", "scopeKeyTag"):
        check(f"index.html 没有 #{elem}",
              f'id="{elem}"' not in html, "控件还在")
        check(f"app.js 没有引用 #{elem}",
              f"#{elem}" not in js, "JS 还在引用")

    # ── 4. 但「选哪一份」这个能力必须在 ──
    section("4 · 该有的还得有")
    check("有布局清单", 'id="layoutList"' in html)
    check("有清空按钮", 'id="btnWipe"' in html)
    check("JS 有 renderLayout", "renderLayout" in js)
    check("JS 有清空逻辑", "apply_wipe" in js)

    # ── 5. 文案不许再提工作区绑定 ──
    section("5 · 文案")
    check("界面不再说「微信这边不跟工作区绑」这种自相矛盾的话",
          "微信这边不跟工作区绑" not in html,
          "既然不提工作区，就别解释为什么不绑")
    check("界面说明的是「一个数据目录 = 一份」",
          "一份灵魂" in html or "数据目录在哪" in html,
          "要讲清楚没有「选哪一份」")

    print()
    print("=" * 58)
    total = PASS + FAIL
    print(f"结果：{PASS}/{total} 通过" + ("" if not FAIL else f"，{FAIL} 项失败"))
    if FAILED:
        for f in FAILED:
            print("  ·", f)
    print("=" * 58)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
