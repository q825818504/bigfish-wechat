"""[bigfish] 静态检查：方法调用的参数个数对不对。

═══════════════════════════════════════════════════════════════════════
为什么需要这个
═══════════════════════════════════════════════════════════════════════

2026-11-02 连续踩了两个同类手误，都是"方法签名和调用点对不上"：

    _input_region_signature() missing 1 required positional argument: 'box'
        —— 改成 staticmethod 之后调用点没跟着改，少传一个

    _warn() takes 1 positional argument but 2 were given
        —— @staticmethod 装饰器漏了，只剩一个 msg 参数，
           而调用点写的是 self._warn(...)，等于传两个

两个都在最外层异常处理附近，一报错就把真正的失败原因盖掉，
看起来全是"媒体没能发出去"，实际上跟发送毫无关系。

这类错误 Python 编译期不查、运行时才炸，而且炸在最不该炸的地方。
所以写个静态检查，用 AST 把每个 `self.xxx(...)` / `Cls.xxx(...)`
的参数个数对一遍。

⚠ 它只查**个数**，不查类型。查类型要跑起来，那就是单元测试的事了。

用法：
    python tools/check_method_calls.py
    python tools/check_method_calls.py bigfish/wechat_sender.py
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

#: 默认检查这些文件（都是这轮反复改、容易手误的）
DEFAULT_TARGETS = [
    "bigfish/wechat_sender.py",
    "bigfish/vault.py",
    "bigfish/desktop.py",
    "bigfish/plugins/__init__.py",
    "bigfish/plugins/sticker.py",
]


def collect_methods(tree):
    """{类名: {方法名: (是否static, 必填参数个数, 全部参数个数)}}"""
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        methods = {}
        for fn in node.body:
            if not isinstance(fn, ast.FunctionDef):
                continue
            decs = set()
            for d in fn.decorator_list:
                if isinstance(d, ast.Name):
                    decs.add(d.id)
                elif isinstance(d, ast.Attribute):
                    decs.add(d.attr)
            args = fn.args
            pos = [a.arg for a in args.args]
            n_def = len(args.defaults)
            if "staticmethod" in decs:
                need = len(pos) - n_def
                total = len(pos)
            else:
                # 实例方法：第一个参数是 self，调用时不传
                need = len(pos) - 1 - n_def
                total = len(pos) - 1
            if "classmethod" in decs:
                need = len(pos) - 1 - n_def
                total = len(pos) - 1
            methods[fn.name] = {
                "static": "staticmethod" in decs,
                "need": max(0, need),
                "total": max(0, total),
                "variadic": args.vararg is not None or not pos,
            }
        out[node.name] = methods
    return out


def check_file(path: str) -> int:
    """返回问题数。"""
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

    classes = collect_methods(tree)
    if not classes:
        return 0

    problems = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        name = node.func.attr
        recv = node.func.value

        # 只看 self.xxx(...) 和 Cls.xxx(...) 两种写法
        cls = None
        is_self = False
        if isinstance(recv, ast.Name) and recv.id == "self":
            is_self = True
        elif isinstance(recv, ast.Name) and recv.id in classes:
            cls = recv.id
        if not is_self and cls is None:
            continue

        # 找到方法定义
        found = None
        if is_self:
            # self 属于哪个类不好静态推断（可能跨类），全表找唯一匹配
            hits = [(c, m) for c, ms in classes.items()
                    for k, m in ms.items() if k == name]
            if len(hits) == 1:
                found = hits[0][1]
            elif hits:
                # 多个同名：按参数个数能对上的算
                cands = [m for _c, m in hits]
                npos = len([a for a in node.args
                            if not isinstance(a, ast.Starred)])
                nkw = len(node.keywords)
                ok = [m for m in cands
                      if not m["variadic"]
                      and npos <= m["total"]
                      and (npos + nkw) >= m["need"]]
                if len(ok) == 1:
                    found = ok[0]
                else:
                    continue
        else:
            found = classes.get(cls, {}).get(name)

        if found is None or found["variadic"]:
            continue

        npos = len([a for a in node.args if not isinstance(a, ast.Starred)])
        nkw = len([k for k in node.keywords if k.arg])
        if npos > found["total"]:
            print(f"  ✗ {os.path.basename(path)}:{node.lineno} "
                  f"{('self.' if is_self else cls + '.')}{name}() "
                  f"传了 {npos} 个位置参数，但最多 {found['total']} 个"
                  f"{'（staticmethod）' if found['static'] else ''}")
            problems += 1
        elif (npos + nkw) < found["need"]:
            print(f"  ✗ {os.path.basename(path)}:{node.lineno} "
                  f"{('self.' if is_self else cls + '.')}{name}() "
                  f"只给了 {npos + nkw} 个参数，但至少需要 {found['need']} 个")
            problems += 1
    return problems


def main() -> int:
    targets = sys.argv[1:] or DEFAULT_TARGETS
    targets = [t if os.path.isabs(t) else os.path.join(_ROOT, t)
               for t in targets]

    print("=" * 68)
    print("方法调用参数检查")
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
        print(f"  ✗ 共 {total} 处参数不匹配")
    else:
        print("  ✓ 没发现问题")
    print("=" * 68)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
