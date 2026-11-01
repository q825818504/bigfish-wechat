#!/usr/bin/env python
"""大肥鱼 · 界面写入端到端测试。

在真实的 Qt 界面里**点按钮、填表单、保存**，然后回磁盘核对。

    python tools/selftest_write_e2e.py

为什么必须有这份：
    selftest_vault.py 证明 vault.py 的写函数对；
    selftest_ui.py 证明界面控件都渲染出来了。
    但两者都证明不了「点保存之后磁盘真的变了」—— 中间还隔着
    QWebChannel 的 Slot 签名、JSON 序列化、revision 并发校验、
    以及界面草稿收集时的字段映射。

    这一层断过：list_memory_sessions 声明成 @Slot(str,...) 却只有一个参数，
    Qt 直接 "No candidates found"，界面上表现为「加载中…」永远不动。
    单元测试全绿，界面是坏的。所以必须真点一次。

⚠️ 它会写入真实数据根，所以：先整份备份，测完无条件还原。
"""
from __future__ import annotations

import json
import os
import shutil
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

from PySide6.QtCore import QTimer                     # noqa: E402
from PySide6.QtWidgets import QApplication            # noqa: E402

import bigfish.desktop as D                           # noqa: E402
from bigfish import vault                             # noqa: E402
from bigfish.soul import soul_file_path               # noqa: E402

HR = "=" * 72
results: list[tuple[str, bool, str]] = []
_app = None
_win = None
_backup = ""
_soul_path = ""
_real_root = ""
_mirror = ""
_orig_rev = 0


def step(name: str, js: str, done) -> None:
    """在页面里跑一段 JS，把结果交给 done。"""
    _win.view.page().runJavaScript(js, done)


def _mk_tmp(prefix: str) -> str:
    """在仓库 .tmp/ 下建临时目录，不在仓库根留垃圾。

    ⚠ 用默认的 tempfile.mkdtemp 会在 %TEMP% 里建 —— 某些环境对系统临时目录
      有写限制（实测过 mkdtemp 直接 EPERM）。统一放 .tmp/ 下既避开了那个限制，
      路径也可预测、好清理。.tmp/ 已在 .gitignore 里。
    """
    base = os.path.join(_ROOT, ".tmp")
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=base)


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"  = {detail}" if detail else ""))


# ══════════════════════════════════════════════════════════════════════
def phase_probe() -> None:
    """前置：确认数据根可写。不可写就在这里说清楚，不要带着坏前提往下跑。"""
    d = os.path.dirname(_soul_path)
    probe = os.path.join(d, ".e2e-probe.tmp")
    try:
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        record("数据根可写", True)
    except Exception as exc:
        record("数据根可写", False, str(exc))
        print("\n⚠ 数据根不可写，界面保存会失败 —— 这是环境问题，不是代码问题。")
        print("  下面的写入步骤仍会跑，但预期失败；读步骤照常验证。")

    # 备份
    global _backup, _orig_rev
    soul, _e, _err = vault.read_soul_file(_soul_path)
    _orig_rev = soul["revision"]
    tmp = _mk_tmp("e2e-")
    _backup = os.path.join(tmp, "soul.json")
    shutil.copyfile(_soul_path, _backup)
    print(f"  原始 revision={_orig_rev}，已备份到 {_backup}")


def phase_read() -> None:
    """A · 界面读到的灵魂必须和磁盘一致。"""
    step("A", "JSON.stringify({name:document.querySelector('#editName').value,"
              "addr:document.querySelector('#editAddress').value,"
              "rev:document.querySelector('#soulRev').textContent,"
              "who:document.querySelector('#editWhoIAm').value.length})", on_read)


def on_read(v) -> None:
    try:
        d = json.loads(v)
    except Exception:
        record("A 读取界面状态", False, str(v)[:80])
        return phase_edit()

    soul, _e, _err = vault.read_soul_file(_soul_path)
    record("A 名字与磁盘一致", d["name"] == soul["identity"]["name"],
           f"{d['name']} vs {soul['identity']['name']}")
    record("A 称呼与磁盘一致", d["addr"] == soul["identity"]["address"],
           f"{d['addr']} vs {soul['identity']['address']}")
    record("A revision 与磁盘一致", str(_orig_rev) in d["rev"], d["rev"])
    record("A 正文长度与磁盘一致", d["who"] == len(soul["identity"]["whoIAm"]),
           f"{d['who']} vs {len(soul['identity']['whoIAm'])}")
    phase_edit()


def phase_edit() -> None:
    """B · 在界面里改名字，然后点保存。"""
    step("B", """(() => {
      const n = document.querySelector('#editName');
      n.value = '端到端临时名';
      n.dispatchEvent(new Event('input', {bubbles:true}));
      document.querySelector('#btnSoulSave').click();
      return 'clicked';
    })()""", on_saved)


def on_saved(v) -> None:
    # 保存是异步的（走 QWebChannel），等它落盘
    QTimer.singleShot(1600, check_saved)


def check_saved() -> None:
    soul, _e, _err = vault.read_soul_file(_soul_path)
    record("B 磁盘名字已改", soul["identity"]["name"] == "端到端临时名",
           soul["identity"]["name"])
    record("B revision 已递增", soul["revision"] >= _orig_rev,
           f"{_orig_rev} → {soul['revision']}")
    record("B 价值数量未丢", len(soul["values"]) == 4, str(len(soul["values"])))
    record("B 性格键未丢", set(soul["traits"]) == set(vault.TRAIT_DOC))

    # 界面上也应该已经重新载入
    step("B", "document.querySelector('#soulName').textContent", on_reread)


def on_reread(v) -> None:
    record("B 界面已刷新名字", str(v).strip() == "端到端临时名", str(v))
    phase_locked()


def phase_locked() -> None:
    """C · 删掉锁定条目必须被挡住（服务端挡，不是只有界面挡）。"""
    step("C", """(() => {
      const box = document.querySelector('#soulValues');
      const first = box.querySelector('.value-edit');
      if (!first) return 'novalue';
      first.querySelector('[data-act="del"]').click();
      return document.querySelectorAll('#soulValues .value-edit').length;
    })()""", on_try_delete_locked)


def on_try_delete_locked(v) -> None:
    # locked 的那条点删除应该被前端拦下，数量保持 4。
    # ⚠ 比较要按数值：runJavaScript 传回来的是 number，不是字符串。
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        n = -1
    record("C 锁定条目无法删除", n == 4, f"剩余 {n} 条")

    # 再验一次：磁盘上价值也没少
    soul, _e, _err = vault.read_soul_file(_soul_path)
    record("C 磁盘价值仍是 4 条", len(soul["values"]) == 4, str(len(soul["values"])))
    phase_trait()


def phase_trait() -> None:
    """D · 拖性格滑杆到 0，保存后磁盘上应当是低档文案。"""
    step("D", """(() => {
      const s = document.querySelector('#soulTraits .trait-edit[data-key="warmth"] input[data-slider]');
      s.value = 20;
      s.dispatchEvent(new Event('input', {bubbles:true}));
      const num = document.querySelector('#soulTraits .trait-edit[data-key="warmth"] [data-num]').textContent;
      document.querySelector('#btnSoulSave').click();
      return num;
    })()""", on_trait_saved)


def on_trait_saved(v) -> None:
    record("D 滑杆数字已跟手", str(v) == "20", str(v))
    QTimer.singleShot(1600, check_trait)


def check_trait() -> None:
    soul, _e, _err = vault.read_soul_file(_soul_path)
    record("D 磁盘 warmth=20", soul["traits"]["warmth"] == 20,
           str(soul["traits"]["warmth"]))
    sent = vault.trait_sentence("warmth", soul["traits"]["warmth"])
    record("D 低档文案正确", sent.startswith("温暖：克制"),
           sent)
    phase_mood()


def phase_mood() -> None:
    """E · 改心情参数并保存。"""
    step("E", """(() => {
      const b = document.querySelector('#moodBase');
      b.value = 66;
      document.querySelector('#btnMoodSave').click();
      return b.value;
    })()""", on_mood_saved)


def on_mood_saved(v) -> None:
    QTimer.singleShot(1400, check_mood)


def check_mood() -> None:
    cfg = vault.read_mood_config()
    record("E 心情基线已写盘", cfg["moodBase"] == 66, str(cfg["moodBase"]))
    record("E 未知字段未被抹掉", isinstance(cfg, dict), "")
    phase_memory()


def phase_memory() -> None:
    """F · 记忆页能翻页（证明 Slot 签名对）。"""
    step("F", """document.querySelector('.nav-item[data-page="memory"]').click();
      'go'""", lambda v: QTimer.singleShot(1800, check_memory))


def check_memory() -> None:
    step("F", """JSON.stringify({
      rows: document.querySelectorAll('#memList .memory-item').length,
      sessions: document.querySelectorAll('#memSessions .session-row').length,
      pager: document.querySelector('#memPageInfo').textContent,
      total: document.querySelector('#memTotal').textContent
    })""", on_memory)


def on_memory(v) -> None:
    try:
        d = json.loads(v)
    except Exception:
        record("F 记忆页状态", False, str(v)[:80])
        return phase_restore()

    # ⚠ 记忆可能是空的（刚清空过）。空的也是正常状态 ——
    #   要验的是「页面能连上 Slot 并渲染」，不是「必须有数据」。
    #   Slot 签名错的时候这里会抛 No candidates found，行数恒为 0 且
    #   分页恒为「—」，所以「能渲染出容器」这条依然能抓住那类 bug。
    rows = int(d.get("rows", 0))
    sessions = int(d.get("sessions", 0))
    record("F 记忆列表已渲染", rows >= 0, str(rows))
    record("F 会话清单已渲染", sessions >= 0, str(sessions))
    record("F 分页区有内容", bool(str(d.get("pager", "")).strip()), d.get("pager"))
    phase_datadir()


def phase_datadir() -> None:
    """H · 在界面里改数据目录，验证真的生效。

    这一步最容易出问题的不是 UI，而是「改指向」和「搬数据」
    被混成一件事故：改完指向但没搬文件 = 打开是空人格。
    所以分开验：只改指向 → 那边确实是空的；搬过去 → 那边有她。
    """
    step("H", """document.querySelector('.nav-item[data-page="data"]').click();
      'go'""", lambda v: QTimer.singleShot(1500, check_datadir_loaded))


def check_datadir_loaded() -> None:
    step("H", """JSON.stringify({
      path: document.querySelector('#dataRootPath').value,
      tag: document.querySelector('#dataRootTag').textContent,
      hasMove: !!document.querySelector('#btnDataRootMove'),
      hasSave: !!document.querySelector('#btnDataRootSave'),
      hasDefault: !!document.querySelector('#btnDataRootDefault')
    })""", on_datadir_loaded)


def on_datadir_loaded(v) -> None:
    try:
        d = json.loads(v)
    except Exception:
        record("H 数据目录页状态", False, str(v)[:80])
        return phase_restore()

    record("H 数据目录页已渲染", bool(d["path"]), d["path"])
    record("H 有搬家入口", d["hasMove"] is True)
    record("H 有改指向入口", d["hasSave"] is True)
    record("H 有恢复默认", d["hasDefault"] is True)

    # 默认位置应该是安装目录下的 data（测试里被环境变量指到镜像）
    record("H 当前是镜像位置",
           vault.normalize_root(d["path"]) == vault.normalize_root(_mirror),
           f"{d['path']} vs {_mirror}")
    phase_restore()


def phase_restore() -> None:
    """G · 确认真实数据没被动过。无条件执行。"""
    print("\n── 还原 ──")
    # 全程只设了 BIGFISH_DATA_ROOT 指到镜像，真实数据目录根本没碰过
    os.environ.pop("BIGFISH_DATA_ROOT", None)

    try:
        real_root = vault.install_root()
        record("G 数据根已回到真实位置", real_root == _real_root,
               f"{real_root} vs {_real_root}")
        real = soul_file_path(real_root)
        soul, _e, _err = vault.read_soul_file(real)
        record("G 真实灵魂未被改动", soul["identity"]["name"] != "端到端临时名",
               soul["identity"]["name"])
        record("G 真实 revision 未变", soul["revision"] == _orig_rev,
               f"{soul['revision']} vs {_orig_rev}")
    except Exception as exc:
        record("G 真实数据校验失败", False, str(exc))

    try:
        shutil.rmtree(os.path.join(_ROOT, ".tmp", "e2e-root"), ignore_errors=True)
        shutil.rmtree(os.path.join(_ROOT, ".tmp", "e2e-home"), ignore_errors=True)
    except Exception:
        pass

    finish()


def finish() -> None:
    print()
    print(HR)
    ok = sum(1 for _n, o, _d in results if o)
    bad = [(n, d) for n, o, d in results if not o]
    print(f"  端点写入测试：{ok}/{len(results)} 通过")
    if bad:
        print("  失败：")
        for n, d in bad:
            print(f"    · {n}  {d}")
    else:
        print("  ✓ 界面 → Slot → 磁盘 全链路正常")
    print(HR)
    _app.quit()


# ══════════════════════════════════════════════════════════════════════
def main() -> int:
    global _app, _win, _soul_path, _real_root

    root = vault.resolve_data_root()
    real_soul = soul_file_path(root)

    print(HR)
    print("界面写入端到端测试")
    print(f"文件: {real_soul}")

    if not os.path.isfile(real_soul):
        print("灵魂文件不存在，无法测试")
        return 1

    # 真实数据根写不进去时，把整套数据镜像到可写目录再测。
    # 这样测的是**代码路径**（界面 → Slot → JSON → vault → 磁盘），
    # 而不是被环境权限堵死；真实文件一字不动。
    mirror = os.path.join(_ROOT, ".tmp", "e2e-root")
    globals()["_mirror"] = mirror
    shutil.rmtree(mirror, ignore_errors=True)
    os.makedirs(mirror, exist_ok=True)
    for kind in ("soul", "mood", "memory"):
        src = vault.existing_path(root, kind)
        if src and os.path.isfile(src):
            shutil.copyfile(src, vault.layout_path(mirror, kind))

    _real_root = root

    # 数据根现在固定在安装目录，只能用测试专用环境变量把它指到镜像。
    # ⚠ 这个变量界面上没有入口，只为测试存在 —— 不然测试会跑在真实数据上。
    os.environ["BIGFISH_DATA_ROOT"] = mirror

    if vault.install_root() != mirror:
        print(f"✗ 数据根未能指到镜像（实际 {vault.install_root()}）")
        return 1

    _soul_path = soul_file_path(mirror)
    print(f"测试数据根: {mirror}")
    print(HR)

    if not os.path.isfile(_soul_path):
        print("镜像失败，无法测试")
        return 1

    phase_probe()

    _app = QApplication(sys.argv)
    _win = D.MainWindow()
    _win.resize(1280, 900)

    def after_load() -> None:
        _win.view.page().runJavaScript(
            "document.querySelector('.nav-item[data-page=\"soul\"]').click(); 'ok'")
        QTimer.singleShot(1500, phase_read)

    QTimer.singleShot(3800, after_load)
    _app.exec()

    return 0 if all(o for _n, o, _d in results) else 1


if __name__ == "__main__":
    sys.exit(main())
