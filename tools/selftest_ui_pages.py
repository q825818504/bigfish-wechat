"""界面契约：合并之后，该在一块的在一块，该消失的真的消失了。

跑法：
    python tools/selftest_ui_pages.py

不需要微信、不需要桌面。只做静态检查 —— 它检查和 app.js / index.html /
plugins 的**契约**，不验证视觉。

## 为什么需要这个文件

v0.9.1 做了一次"减法"：把「时间感知 + 身体 + 睡觉不回」合成一个插件，
把「昨天」删掉，把「关系」并进印象，把「主动」做成插件。
减法最容易出的事故是**只减了一半**：

    侧栏按钮删了，go() 里还留着 loadProactive()  → 点进别的页报错
    插件文件删了，_load_builtins 还在 import      → 整个插件系统起不来
    页面删了，后端槽位还留着                       → 没人调的死代码
    配置搬了家，老地方还在读                       → 两个真源互相打架

这些都不报错、不白屏，只是"有些开关按下去没反应"。所以每一环单独列出来。

覆盖：
  侧栏没有 proactive / bond 两个板块
  关系卡在印象页（渲染 + 三个动作 + 事件委托）
  主动是 hook 插件（登记、参数、主循环驱动）
  配置只有一个真源（world 不再读 config.json 的 proactive 段）
  已删的插件文件确实不在了
  life 同时是 prompt + gate
"""
from __future__ import annotations

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PASS = 0
FAIL = 0
FAILED: list[str] = []


def _pick_marks() -> tuple[str, str]:
    try:
        "\u2713".encode(sys.stdout.encoding or "utf-8")
        return "\u2713", "\u2717"
    except (UnicodeEncodeError, LookupError):
        return "[OK]", "[X]"


OK, BAD = _pick_marks()


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  {OK} {name}")
        return True
    FAIL += 1
    FAILED.append(name)
    print(f"  {BAD} {name}" + (f"\n      {detail}" if detail else ""))
    return False


def _read(*parts: str) -> str:
    path = os.path.join(_ROOT, *parts)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _exists(*parts: str) -> bool:
    return os.path.isfile(os.path.join(_ROOT, *parts))


def main() -> int:
    print("=" * 52)
    print("界面契约：减法之后每一环都对得上")
    print("=" * 52)

    html = _read("ui", "index.html")
    js = _read("ui", "app.js")
    desktop = _read("bigfish", "desktop.py")
    world = _read("bigfish", "world.py")

    # ── 1. 侧栏不再多两个板块 ──
    print("\n[结构] 侧栏回到 11 个板块")
    check("没有 proactive 导航",
          'data-page="proactive"' not in html)
    check("没有 bond 导航",
          'data-page="bond"' not in html)
    check("没有 proactive section",
          'data-page="proactive"' not in html)
    check("没有 bond section",
          'data-page="bond"' not in html)

    navs = re.findall(r'class="nav-item[^"]*" data-page="([a-z]+)"', html)
    pages = re.findall(r'class="page[^"]*" data-page="([a-z]+)"', html)
    missing = [n for n in navs if n not in pages]
    check("每个导航都有落地页", not missing, f"缺：{missing}")
    check("导航 11 个", len(navs) == 11, f"{len(navs)} 个：{navs}")
    check("section 也是 11 个", len(pages) == 11, f"{len(pages)} 个")

    # ── 2. 主动页整页撤干净 ──
    print("\n[清理] 主动页的每一环都不在了")
    check("没有 loadProactive", "loadProactive" not in js)
    check("没有 renderProactive", "renderProactive" not in js)
    check("没有 saveProactive", "saveProactive" not in js)
    check("没有 addProactiveDate", "addProactiveDate" not in js)
    check("go() 不再分流 proactive", "page === 'proactive'" not in js)
    check("PAGE_TITLES 没有 proactive", "proactive:" not in js)
    check("后端没有 proactive_status 槽位",
          "def proactive_status" not in desktop)
    check("后端没有 proactive_save 槽位",
          "def proactive_save" not in desktop)

    # ── 3. 关系卡搬进印象页 ──
    print("\n[印象] 关系卡在这一页里，动作都能用")
    check("有 buildBondBlock", re.search(r"function\s+buildBondBlock\s*\(", js) is not None)
    check("印象页调用了它", "buildBondBlock(p) +" in js)
    check("没有 loadBond 残留", "loadBond" not in js)
    check("没有 renderBondPanes 残留", "renderBondPanes" not in js)
    check("「改」按钮在", "data-bondedit" in js)
    check("「下线」按钮在", "data-bondretire" in js)
    check("「删除」按钮在", "data-bonddel" in js)
    check("三个动作走事件委托",
          "data-bondedit" in js and "dataset.bondretire" in js
          and "dataset.bonddel" in js)
    check("startBondEdit 有实现",
          re.search(r"function\s+startBondEdit\s*\(", js) is not None)
    check("编辑走 bond_update", "action: 'edit'" in js)
    check("改完刷新的是印象页", "await loadImpression()" in js)
    check("每条都摆出「依据」", "依据：" in js)

    # 后端：印象页数据带关系卡
    check("后端给每个人带 bonds", 'p["bonds"] = _bonds_of' in desktop)
    check("_bonds_of 有实现", "def _bonds_of(" in desktop)
    check("提示词里并进印象段", "_bond_block(_owner)" in desktop)
    check("关系卡不再单独占一段",
          '"bond"' not in desktop.split("def t_plugins")[0].split("_load_builtins")[0])

    # ── 4. 主动是插件 ──
    print("\n[主动] 它是一张插件卡，不是一个页面")
    check("plugins/proactive.py 存在", _exists("bigfish", "plugins", "proactive.py"))
    try:
        from bigfish import plugins as P

        P.ensure_loaded()
        ids = set(P._REGISTRY)
        check("proactive 已登记", "proactive" in ids, str(sorted(ids)))
        if "proactive" in ids:
            d = P._REGISTRY["proactive"]
            check("它是 hook 类", d.get("scope") == "hook", str(d.get("scope")))
            check("它默认开着", bool(d.get("default")))
            keys = set((d.get("config") or {}).keys())
            check("参数里有 dry_run", "dry_run" in keys, str(keys))
            check("参数里有 per_day", "per_day" in keys, str(keys))
            check("参数里有 important_dates", "important_dates" in keys, str(keys))
            check("有 tick 实现", d.get("tick") is not None)
        check("life 同时是 prompt 和 gate",
              set(P._REGISTRY["life"].get("scopes") or []) == {"prompt", "gate"},
              str(P._REGISTRY["life"].get("scopes")))
        for gone in ("clock", "sleep-gate", "journal", "bond"):
            check(f"{gone} 不再是独立插件", gone not in ids)
    except Exception as exc:
        check("插件系统能起来", False, str(exc))

    # 主循环真的会踢它
    print("\n[接线] 主循环每 5 秒踢一次 hook")
    check("主循环调 run_hooks", "run_hooks(" in desktop)
    check("传的是 world 和 now", '"world": self._world' in desktop)
    check("world 有 tick_active", "def tick_active(" in world)
    _tick_body = world.split("def tick_active(")[0].split("def tick(")[-1]
    check("tick 只做心情（不含主动）", "self.tick_active(" not in _tick_body)

    # ── 5. 配置只有一个真源 ──
    print("\n[配置] 只有一个地方能改主动")
    check("world 读插件配置", '_P.config_of("proactive")' in world)
    check("world 不再读 config.json 的 proactive 段",
          'get("proactive")' not in world)
    check("per_day 的 0 不会被吃掉", "max(0, int(cfg.get(\"per_day\"" in world)
    check("重要日子会解析", "parse_dates_text" in world)
    check("解析函数在 proactive 模块里",
          "def parse_dates_text(" in _read("bigfish", "proactive.py"))

    # ── 6. 删掉的东西真删了 ──
    print("\n[删除] 被合并/取消的模块文件不在了")
    for name in ("clock.py", "sleep_gate.py", "journal.py", "bond.py"):
        check(f"plugins/{name} 已删",
              not _exists("bigfish", "plugins", name))
    check("bigfish/journal.py 已删", not _exists("bigfish", "journal.py"))
    check("_load_builtins 不再 import 它们",
          "import clock" not in _read("bigfish", "plugins", "__init__.py")
          and "import journal" not in _read("bigfish", "plugins", "__init__.py"))

    # ── 7. JS 引用的 id 都在 HTML 里 ──
    print("\n[对账] 印象页引用的 id 都存在")
    ids_html = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
    for name in ("imprTabs", "imprPanes", "imprCount"):
        check(f"#{name} 在 HTML 里", name in ids_html)
    # 评语框和按钮是 renderImprPanes 动态渲染的，不在静态 HTML 里
    for name in ("imprNote", "btnSaveNote"):
        check(f"#{name} 由 JS 渲染", ("'#" + name + "'") in js)
    check("印象页没有引用已删的 bond 容器",
          "bondTabs" not in js and "bondPanes" not in js)

    print(f"\n{'=' * 52}")
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
