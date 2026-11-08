#!/usr/bin/env python
"""大肥鱼 · 发送收尾段体检。

    python tools/selftest_send_tail.py

═══════════════════════════════════════════════════════════════════════
为什么需要这一份
═══════════════════════════════════════════════════════════════════════
2026-11-08 线上出现过一次"微信里明明收到了，日志说这一轮一条都没发出去"。

根因不是发送失败，是**缩进塌陷**：

    if sent_any:
        ...                       # 成功后的收尾
    try:                          # ← b56f51f 在这里插了"写短期历史"
        ...
    except Exception:
        pass
        self.reply_count += 1     # ← 这些本来是 20 空格缩进，
        self._log(f"[发] ...")    #    掉进了 except 块，永远不执行
        ...
    else:
        self._log("[发] 这一轮一条都没发出去")

于是正常路径（history 写成功、不抛异常）每轮都走 `else`：
日志说没发出去、记忆库不入库、心情不判断、`[发]` 成功日志永远不出现。
而 Python 不报错 —— 这段代码语法完全合法。这比崩溃难查十倍。

这份自检守两条：

  1. `if sent_any:` 里必须有收尾（成功日志 / 记忆库 / 记账），
     `else` 里只能有"这一轮一条都没发出去"。
  2. 整个 bigfish/ 不许出现 `except: pass` 后面还跟着语句 ——
     那正是塌陷留下的形状。

为什么用 AST 而不是正则：缩进塌陷恰好是"语法合法、结构错误"，
只有语法树能看出某个语句到底属于 if 还是属于 except。
"""
from __future__ import annotations

import ast
import io
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
_PKG = os.path.join(_ROOT, "bigfish")

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


def _calls(node: ast.AST, attr: str):
    """收集 node 子树里所有 x.<attr>(...) 的调用。"""
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
            if sub.func.attr == attr:
                out.append(sub)
    return out


def _bare_calls(node: ast.AST, name: str):
    """收集 node 子树里所有 name(...) 的直接调用（remember_sent 这类）。"""
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            if sub.func.id == name:
                out.append(sub)
    return out


def _texts(call: ast.Call) -> str:
    """把调用的常量/字面参拼起来，用来认日志文案。"""
    buf = []
    for arg in list(call.args) + [kw.value for kw in call.keywords]:
        for sub in ast.walk(arg):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                buf.append(sub.value)
    return " ".join(buf)


def test_sent_any_block() -> None:
    """handle() 里的 if sent_any / else 结构。"""
    path = os.path.join(_PKG, "desktop.py")
    tree = ast.parse(io.open(path, encoding="utf-8").read(), path)

    def _find(name):
        return [n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == name]

    # P1+P2 之后发送段搬进了 _deliver（生成与投递分离）。
    # 找不到 _deliver 时退回 handle —— 这样自检不绑死某一次重构的形状。
    targets = _find("_deliver") or _find("handle")
    check("找得到投递函数（_deliver 或 handle）", bool(targets),
          "desktop.py 里既没有 _deliver 也没有 handle")
    if not targets:
        return

    blocks = []
    for fn in targets:
        for sub in ast.walk(fn):
            if (isinstance(sub, ast.If) and isinstance(sub.test, ast.Name)
                    and sub.test.id == "sent_any"):
                blocks.append(sub)
    check("投递函数里有 if sent_any", bool(blocks))

    if not blocks:
        return
    blk = blocks[0]

    body_log = " ".join(_texts(c) for c in _calls(blk, "_log")
                        if c in blk.body or any(c is x for x in ast.walk(blk)))
    # 只有 if 的 body（不含 orelse）里的日志才算"成功日志"
    body_calls = []
    for stmt in blk.body:
        body_calls.extend(_calls(stmt, "_log"))
    body_text = " ".join(_texts(c) for c in body_calls)
    else_calls = []
    for stmt in blk.orelse:
        else_calls.extend(_calls(stmt, "_log"))
    else_text = " ".join(_texts(c) for c in else_calls)

    check("成功日志 [发] 在 if 里（不是 else）",
          "[发]" in body_text,
          f"if 分支里没有 [发] 成功日志；实际: {body_text!r}")
    check("记忆库入库在 if 里",
          bool(_calls(blk, "record_exchange")),
          "if sent_any 里找不到 record_exchange")
    body_bare = []
    for stmt in blk.body:
        body_bare.extend(_bare_calls(stmt, "remember_sent"))
    check("回环账本 remember_sent 在 if 里",
          bool(body_bare),
          "if sent_any 里找不到 remember_sent")
    check("else 只打「这一轮一条都没发出去」",
          "一条都没发出去" in else_text,
          f"else 分支里没有这句话；实际: {else_text!r}")
    check("「一条都没发出去」不在 if 分支里",
          "一条都没发出去" not in body_text,
          "成功分支里出现失败文案 —— 又塌陷了")
    del body_log


def test_except_pass_shape() -> None:
    """except 块里 pass 之后还有语句 —— 塌陷的形状。"""
    bad = []
    for dirpath, dirnames, filenames in os.walk(_PKG):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                tree = ast.parse(io.open(path, encoding="utf-8").read(), path)
            except SyntaxError as exc:
                bad.append(f"{path}: 语法错误 {exc}")
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler):
                    for stmt in node.body[:-1]:
                        if isinstance(stmt, ast.Pass):
                            bad.append(f"{os.path.relpath(path, _ROOT)}:{stmt.lineno}"
                                       " except 块里 pass 后仍有语句")
    check("没有 except-pass 塌陷", not bad,
          "\n".join(bad[:5]) + ("\n..." if len(bad) > 5 else ""))


def test_split_invariants() -> None:
    """生成与投递必须分开（P1+P2 的地基）。

    这条守的是并发改造的前提：
      · 发送只发生在 `_deliver` 里（唯一碰 UIA 的线程）
      · 挑图只发生在 `handle` 里（生成阶段，可以并行）
      · `UiaSender.send/send_media` 带串行锁（补一句 / 主动消息也走它）
    """
    path = os.path.join(_PKG, "desktop.py")
    tree = ast.parse(io.open(path, encoding="utf-8").read(), path)
    fns = {}

    def walk_fns(node):
        for sub in ast.walk(node):
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fns.setdefault(sub.name, sub)

    walk_fns(tree)
    deliver = fns.get("_deliver")
    handle = fns.get("handle")
    check("_deliver 存在（投递阶段）", deliver is not None)

    def _has_attr_call(fn, attr):
        if fn is None:
            return False
        return any(isinstance(s, ast.Call)
                   and isinstance(s.func, ast.Attribute)
                   and s.func.attr == attr
                   for s in ast.walk(fn))

    def _sends(fn):
        if fn is None:
            return False
        for s in ast.walk(fn):
            if (isinstance(s, ast.Call) and isinstance(s.func, ast.Attribute)
                    and s.func.attr == "send"
                    and isinstance(s.func.value, ast.Attribute)
                    and s.func.value.attr == "sender"):
                return True
        return False

    if deliver is not None and handle is not None:
        check("发送只在 _deliver 里", _sends(deliver) and not _sends(handle),
              f"deliver_sends={_sends(deliver)} handle_sends={_sends(handle)}")
        check("挑图只在生成阶段",
              _has_attr_call(handle, "_pick_sticker")
              and not _has_attr_call(deliver, "_pick_sticker"))
    check("有 gen worker", fns.get("_gen_worker") is not None)
    check("有 send worker", fns.get("_send_worker") is not None)

    # UiaSender 的两个写方法是唯一出口，必须带锁
    ws_path = os.path.join(_PKG, "wechat_sender.py")
    ws = ast.parse(io.open(ws_path, encoding="utf-8").read(), ws_path)
    uia = [n for n in ast.walk(ws)
           if isinstance(n, ast.ClassDef) and n.name == "UiaSender"]
    check("找到 UiaSender", bool(uia))
    if uia:
        locked = set()
        for node in uia[0].body:
            if isinstance(node, ast.FunctionDef) and node.name in ("send", "send_media"):
                decs = [d.id for d in node.decorator_list if isinstance(d, ast.Name)]
                if "_serialized_send" in decs:
                    locked.add(node.name)
        check("send / send_media 都带串行锁",
              locked == {"send", "send_media"}, str(locked))


def main() -> int:
    print("=" * 60)
    print("发送收尾段体检")
    print("=" * 60)
    test_sent_any_block()
    test_except_pass_shape()
    test_split_invariants()
    print()
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILED:
        print("失败项：" + "、".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
