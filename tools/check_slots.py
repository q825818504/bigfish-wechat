"""[bigfish] 静态检查：@Slot 声明的参数个数要和 Python 方法对得上。

═══════════════════════════════════════════════════════════════════════
为什么需要这个
═══════════════════════════════════════════════════════════════════════

Qt 的 `@Slot(str, result=str)` 声明的是**对外接口**：
JS 那边会按这个签名调。如果 Python 方法的参数个数对不上，
Qt 找不到匹配的槽，调用**静默失败**。

表现极难查：
    · 不抛异常（Qt 只是"没有这个签名"）
    · 不写日志
    · 前端 await 拿到 undefined，`JSON.parse` 抛错，被 async 吞掉
    · 用户看到的就是「按钮点了没反应」

已经犯过两次：
    2026-11-02  list_memory_sessions   @Slot(str, result=str) 但方法无参
                → 记忆页会话列表永远空白
    2026-11-03  sticker_pick_files     @Slot(str, result=str) 但方法无参
                → 「浏览…」按钮点了没反应

两次都是同一个模式：**装饰器写多了一个参数**。
Python 编译期不查，运行时也不报，只有点下去才知道。

═══════════════════════════════════════════════════════════════════════
检查什么
═══════════════════════════════════════════════════════════════════════

对每个 `@Slot(...)`：

    @Slot 里的**输入参数**个数（排除 result= / name=）
    必须等于
    Python 方法的参数个数（排除 self）

另外顺手查：`@Slot` 里写了 result= 的方法，返回值注解应该不是 None。

用法：
    python tools/check_slots.py
    python tools/check_slots.py bigfish/desktop.py
"""
from __future__ import annotations

import ast
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_TARGETS = [
    "bigfish/desktop.py",
]

#: 这些关键字是 Slot 的元信息，不算输入参数
_META_KW = ("result", "name")


def _slot_inputs(decorator: ast.expr):
    """解析 @Slot(...)，返回 (输入参数个数, 是否有 result, 是否有星号)。

    返回 None 表示这不是一个能静态判断的 Slot（比如 @Slot() 空参）。
    """
    if not isinstance(decorator, ast.Call):
        return None
    has_star = False
    n_inputs = 0
    for arg in decorator.args:
        if isinstance(arg, ast.Starred):
            has_star = True
            continue
        n_inputs += 1
    has_result = False
    for kw in decorator.keywords:
        if kw.arg == "result":
            has_result = True
        elif kw.arg in _META_KW:
            continue
    return (n_inputs, has_result, has_star)


def check_file(path: str) -> int:
    try:
        src = open(path, encoding="utf-8").read()
    except Exception as exc:
        print(f"  ✗ 读不到 {path}：{exc}")
        return 1

    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        print(f"  ✗ {path} 语法错误：{exc}")
        return 1

    problems = 0
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for fn in cls.body:
            if not isinstance(fn, ast.FunctionDef):
                continue
            for dec in fn.decorator_list:
                # @Slot(...) 或 @PySide6.QtCore.Slot(...)
                name = ""
                if isinstance(dec, ast.Call):
                    f = dec.func
                    name = getattr(f, "id", "") or getattr(f, "attr", "")
                elif isinstance(dec, (ast.Name, ast.Attribute)):
                    name = getattr(dec, "id", "") or getattr(dec, "attr", "")
                if name != "Slot":
                    continue

                info = _slot_inputs(dec) if isinstance(dec, ast.Call) else None
                if info is None:
                    continue
                n_inputs, has_result, has_star = info
                if has_star:
                    continue        # @Slot(*TYPES) 静态判不了

                # Python 方法自己的参数（排除 self / cls）
                pos = [a.arg for a in fn.args.args]
                if pos and pos[0] in ("self", "cls"):
                    pos = pos[1:]
                n_def = len(fn.args.defaults)
                n_min = len(pos) - n_def
                n_max = len(pos)
                variadic = fn.args.vararg is not None

                if variadic:
                    ok = n_inputs >= n_min
                else:
                    ok = n_min <= n_inputs <= n_max

                if not ok:
                    print(f"  ✗ {os.path.basename(path)}:{fn.lineno} "
                          f"{cls.name}.{fn.name}")
                    print(f"      @Slot 声明了 {n_inputs} 个输入参数，"
                          f"但方法接受 {n_min}~{n_max} 个")
                    print("      → JS 调用会静默失败，表现成「按钮点了没反应」")
                    problems += 1

                # result= 但方法没有返回值注解：可疑，但不一定错
                if has_result and fn.returns is None:
                    print(f"  ⚠ {os.path.basename(path)}:{fn.lineno} "
                          f"{cls.name}.{fn.name} 声明了 result= 但没有返回注解")
    return problems


def main() -> int:
    targets = sys.argv[1:] or DEFAULT_TARGETS
    targets = [t if os.path.isabs(t) else os.path.join(_ROOT, t)
               for t in targets]

    print("=" * 68)
    print("@Slot 签名检查")
    print("=" * 68)

    total = 0
    for t in targets:
        if not os.path.isfile(t):
            print(f"  · 跳过（不存在）：{t}")
            continue
        n = check_file(t)
        rel = os.path.relpath(t, _ROOT)
        if n:
            print(f"  ↑ {rel}：{n} 处")
        else:
            print(f"  ✓ {rel}")
        total += n

    print("=" * 68)
    if total:
        print(f"  ✗ 共 {total} 处签名不匹配")
    else:
        print("  ✓ 没发现问题")
    print("=" * 68)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
