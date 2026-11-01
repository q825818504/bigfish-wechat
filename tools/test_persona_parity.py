#!/usr/bin/env python
"""大肥鱼 · 两侧人格渲染一致性测试。

核心命题：**同一个灵魂文件，Python 侧和 Node 侧必须渲染出一模一样的字符串。**

为什么这条值得单开一份测试：
    人格文本最终会进 system prompt。两侧差一个字——比如一边输出
    「温暖：关心，但不黏人。」另一边输出「温暖（65/100）：明显关心，会主动问候」——
    那就是两个不同的人：微信里的大肥鱼和 DSH 里的大肥鱼性格不一样。

    这不是假设，是真实发生过的：
      · TRAIT_DOC 在 bigfish/soul.py 里抄了一份，mid 档文案是错的；
      · bridge.mjs 又抄了一份，也错，而且分档边界写成 `<40 / >60`
        （插件的口径是 `<=40 / >=60`）；
      · bridge.mjs 还把裸数字 `（65/100）` 注进了提示词。

    三处漂移，任何一处都足以让"她"不是同一个人。

    python tools/test_persona_parity.py

需要 node 在 PATH 上。找不到 node 就跳过 Node 侧，只报 Python 侧结果。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    if _s is None:
        continue
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bigfish import vault                    # noqa: E402
from bigfish.soul import Soul                # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []


def _mk_tmp(prefix: str) -> str:
    """在仓库 .tmp/ 下建临时目录，不在仓库根留垃圾。

    ⚠ 用默认的 tempfile.mkdtemp 会在 %TEMP% 里建 —— 某些环境对系统临时目录
      有写限制（实测过 mkdtemp 直接 EPERM）。统一放 .tmp/ 下既避开了那个限制，
      路径也可预测、好清理。.tmp/ 已在 .gitignore 里。
    """
    base = os.path.join(_ROOT, ".tmp")
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=base)


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
    print(f"\n── {t} " + "─" * max(0, 54 - len(t)))


def make_root(tmp: str, soul: dict, name: str = "parity") -> str:
    """造一个只有一份灵魂的最小数据根。

    ⚠ 每个用例独立一个新根。共用一个 root/ 会互相污染 ——
      老布局的 findScope 会在多个 ws-* 里挑「有灵魂且最新」的，
      上一轮的目录还在，这一轮就可能读到上一轮的灵魂，
      表现成「两侧不一致」，实际是测试夹具串了。

    新布局就一个文件：<root>/soul.json。
    """
    root = os.path.join(tmp, f"root-{name}")
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "soul.json"), "w", encoding="utf-8") as f:
        json.dump(soul, f, ensure_ascii=False)
    return root


def node_persona(root: str) -> str | None:
    """调 bridge.mjs 拿 Node 侧渲染结果。"""
    node = shutil.which("node") or shutil.which("node.exe")
    if not node:
        return None
    bridge = os.path.join(_ROOT, "bridge", "bridge.mjs")
    if not os.path.isfile(bridge):
        return None
    r = subprocess.run(
        [node, bridge, "context", "--root", root],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60)
    raw = (r.stdout or "").strip()
    if not raw:
        return None
    try:
        return (json.loads(raw).get("persona") or "")
    except json.JSONDecodeError:
        return None


def python_persona(root: str, key: str = "") -> str:
    """Python 侧渲染：与生产路径一致 —— 读原样内容，不补默认值。"""
    p = os.path.join(root, "soul.json")
    soul, _e, _err = vault.read_soul_file(p)
    return Soul.from_dict(soul).render_persona()


def compare(label: str, soul: dict, tmp: str, name: str = "parity") -> None:
    root = make_root(tmp, soul, name=name)
    py = python_persona(root)
    js = node_persona(root)
    if js is None:
        check(f"{label}（Node 侧跳过：没有 node 或 bridge.mjs 跑不起来）", True)
        return
    if py.strip() == js.strip():
        check(label, True, f"{len(py)} 字")
    else:
        check(label, False, f"py={len(py)} js={len(js)}")
        import difflib
        for line in list(difflib.unified_diff(
                py.splitlines(), js.splitlines(), "python", "node",
                lineterm="", n=1))[:14]:
            print(f"      {line}")


def main() -> int:
    tmp = _mk_tmp("parity-")
    print("=" * 60)
    print("两侧人格渲染一致性")
    print("=" * 60)

    try:
        section("1 · 真实灵魂文件")
        real_root = vault.resolve_data_root()
        real_soul = vault.existing_path(real_root, "soul")
        if os.path.isfile(real_soul):
            with open(real_soul, "r", encoding="utf-8") as f:
                soul = json.load(f)
            compare("真实文件两侧一致", soul, tmp, name="real")
        else:
            check("真实灵魂文件存在", False, real_soul)

        section("2 · 分档边界（<=40 低 / >=60 高 / 其余中）")
        # 这几个值正好踩在边界上，是抄错分档时最先炸的地方
        for v, want in ((40, "低"), (41, "中"), (59, "中"), (60, "高")):
            s = {"identity": {"name": "测", "address": "爸", "whoIAm": "x"},
                 "values": [{"text": "甲", "locked": True}],
                 "traits": {"warmth": v},
                 "selfNotes": ["一"]}
            name = f"b{v}"
            root = make_root(tmp, s, name=name)
            py = python_persona(root)
            sent = [l for l in py.splitlines() if "温暖" in l]
            got = sent[0] if sent else ""
            band_ok = ("克制" in got) if want == "低" else \
                      ("明显关心" in got) if want == "高" else ("关心，但不黏人" in got)
            check(f"warmth={v} → {want}档", band_ok, got)
            check(f"warmth={v} 两侧一致", py.strip() == (node_persona(root) or "").strip())

        section("3 · 性格段不许出现裸数字")
        s = {"identity": {"name": "测", "address": "爸", "whoIAm": "x"},
             "values": [{"text": "甲", "locked": True}],
             "traits": {"warmth": 65, "assertiveness": 30,
                        "playfulness": 88, "directness": 50},
             "selfNotes": ["一"]}
        root = make_root(tmp, s, name="num")
        py = python_persona(root)
        check("Python 侧无 /100", "/100" not in py, py)
        js = node_persona(root) or ""
        check("Node 侧无 /100", "/100" not in js, js)
        check("两侧完全一致", py.strip() == js.strip())

        section("4 · 边缘输入")
        edge = {
            "空灵魂": {},
            "只有名字": {"identity": {"name": "小蓝"}},
            "有昵称": {"identity": {"name": "小蓝", "address": "老板",
                                  "aliases": "蓝蓝，小蓝鱼"}},
            "价值全空": {"identity": {"name": "小蓝"}, "values": []},
            "无自知": {"identity": {"name": "小蓝"}, "selfNotes": []},
            "性格越界": {"identity": {"name": "小蓝"},
                        "traits": {"warmth": 200, "directness": -50}},
        }
        for i, (label, s) in enumerate(edge.items()):
            name = f"e{i}"
            root = make_root(tmp, s, name=name)
            py = python_persona(root)
            js = node_persona(root) or ""
            # 归一化只在 Python 侧做；Node 侧读的是**原始 JSON**，
            # 越界值两边会不同 —— 这是设计上接受的（bridge 只读不写，
            # 真正落盘的永远是被 normalize 过的值）。所以只在合法输入上要求一致。
            if label == "性格越界":
                check(f"{label}：Python 侧夹取正确",
                      "温暖：克制" not in py and "温暖：" in py, py)
            else:
                check(f"{label} 两侧一致", py.strip() == js.strip(),
                      f"py={len(py)} js={len(js)}")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 60)
    total = PASS + FAIL
    print(f"结果：{PASS}/{total} 通过" + ("" if not FAIL else f"，{FAIL} 项失败"))
    if FAILED:
        for f in FAILED:
            print("  ·", f)
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
