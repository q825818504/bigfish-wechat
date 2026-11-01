#!/usr/bin/env python
"""大肥鱼 · 可写层真实数据冒烟测试。

⚠️ 这份**会碰真实数据**，所以规矩是：
   · 读：直接读，不改。
   · 写：先整份备份到临时目录，测完从备份还原。
   · 任何一步失败都必须还原，不能留下半改的灵魂。

    python tools/smoke_vault_live.py

它测的不是"函数能不能跑"，而是"面对你真实的灵魂文件，写进去再读出来
还是不是同一个东西"。单元测试用合成数据跑不出这类问题——
真实文件里有 4000 字 whoIAm、有 4 条锁定价值、有中文路径。
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

from bigfish import vault                       # noqa: E402
from bigfish.soul import Soul, soul_file_path   # noqa: E402

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
    print(f"\n── {t} " + "─" * max(0, 56 - len(t)))


def _probe_writable(target: str) -> tuple[bool, str]:
    """在目标所在目录真写一个探针文件。

    为什么不信 os.access(W_OK)：
      它在 Windows 上只查 ACL，不查「上层策略会不会在 CreateFile 时拦下来」。
      实测过：os.access 返回 True，实际 open(dst,'w') 照样 Permission denied。
      只有真写一次才知道能不能写。
    """
    d = os.path.dirname(target)
    probe = os.path.join(d, ".bigfish-write-probe.tmp")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("probe")
        os.remove(probe)
        return True, ""
    except Exception as exc:
        try:
            if os.path.exists(probe):
                os.remove(probe)
        except Exception:
            pass
        return False, str(exc)


def main() -> int:
    root = vault.resolve_data_root()
    print("=" * 62)
    print("可写层 · 真实数据冒烟")
    print(f"数据根: {root}")
    print("=" * 62)

    soul_path = soul_file_path(root)
    if not os.path.isfile(soul_path):
        print(f"灵魂文件不存在：{soul_path}")
        return 1

    tmp = _mk_tmp("smoke-")
    backup = os.path.join(tmp, "soul-backup.json")
    shutil.copyfile(soul_path, backup)
    print(f"\n已备份到 {backup}")

    # ── 关键前置：真实目录能不能写 ──
    #
    # 这一步必须先测，不能想当然。真实数据根有可能位于受策略保护的目录，
    # 此时 os.access(W_OK) 返回 True（ACL 层面确实允许），但实际创建文件被拒。
    # 以前不做这个探测，结果一整个写测试套件全部炸在 restore 上，
    # 还把用户的文件置于半改状态 —— 那才是最糟的失败方式。
    writable, probe_err = _probe_writable(soul_path)
    if not writable:
        print(f"\n⚠ 真实数据根不可写：{probe_err}")
        print("  改为在**同内容副本**上跑完整写测试（真实文件一字不动）")
        # 新布局：一个数据根就是一份，副本直接放 <mirror>/soul.json
        mirror_root = os.path.join(_ROOT, ".tmp", "live-mirror")
        shutil.rmtree(mirror_root, ignore_errors=True)
        os.makedirs(mirror_root, exist_ok=True)
        soul_path = os.path.join(mirror_root, "soul.json")
        shutil.copyfile(backup, soul_path)
        print(f"  副本：{soul_path}")

    restored = False
    try:
        # ── 读 ──
        section("读真实灵魂")
        soul, exists, err = vault.read_soul_file(soul_path)
        check("文件存在", exists is True)
        check("无解析错误", err == "", err)
        check("有名字", bool(soul["identity"]["name"]), soul["identity"]["name"])
        check("有 whoIAm", bool(soul["identity"]["whoIAm"]))
        check("whoIAm 长度 > 100", len(soul["identity"]["whoIAm"]) > 100,
              str(len(soul["identity"]["whoIAm"])))
        check("有 4 条价值", len(soul["values"]) == 4, str(len(soul["values"])))
        check("价值全部锁定", all(v["locked"] for v in soul["values"]))
        check("4 个性格键", set(soul["traits"]) == set(vault.TRAIT_DOC))
        check("性格值在 0..100",
              all(0 <= v <= 100 for v in soul["traits"].values()), str(soul["traits"]))
        orig_rev = soul["revision"]
        print(f"       revision={orig_rev}  名字={soul['identity']['name']}  "
              f"称呼={soul['identity']['address']}")

        section("渲染人格")
        s = Soul.from_dict(soul, soul_path)
        persona = s.render_persona()
        check("渲染非空", len(persona) > 200, str(len(persona)))
        check("含「我是谁」", "## 我是谁" in persona)
        check("含「我珍视的」", "## 我珍视的" in persona)
        check("含「我的性格」", "## 我的性格" in persona)
        check("含称呼", f"称呼对方「{soul['identity']['address']}」" in persona)
        check("性格段不含裸数字（会误导模型）",
              not any(f"（{v}/100）" in persona for v in soul["traits"].values()))
        print(f"       人格 {len(persona)} 字")

        # ── 写：改一处再读回来 ──
        section("写回真实灵魂（改名字 → 读回 → 还原）")
        modified = json.loads(json.dumps(soul))
        modified["identity"]["name"] = "自检临时名"
        ok, out, werr = vault.write_soul_file(soul_path, modified)
        check("写入成功", ok, werr)
        check("revision +1", out["revision"] == orig_rev + 1,
              f"{orig_rev} → {out['revision']}")

        reread, exists2, err2 = vault.read_soul_file(soul_path)
        check("读回 exists", exists2 is True)
        check("读回无错", err2 == "", err2)
        check("名字已改", reread["identity"]["name"] == "自检临时名",
              reread["identity"]["name"])
        check("whoIAm 一字未变",
              reread["identity"]["whoIAm"] == soul["identity"]["whoIAm"])
        check("价值未变", reread["values"] == soul["values"])
        check("性格未变", reread["traits"] == soul["traits"])
        check("selfNotes 未变", reread["selfNotes"] == soul["selfNotes"])

        # ── locked 保护：删掉锁定价值应当被服务端挡住 ──
        section("locked 价值保护")
        # 注意：vault 层不管这个（那是 Backend 的职责），这里验证的是
        # 归一化不会把 locked 弄丢 —— 丢了 Backend 就挡不住
        stripped = json.loads(json.dumps(soul))
        stripped["values"] = [{"id": "v1", "text": "只留一条", "locked": True}]
        ok, out, _ = vault.write_soul_file(soul_path, stripped)
        check("写成功（vault 层不拦）", ok)
        check("locked 字段保住", out["values"][0]["locked"] is True)

        # ── 坏输入 ──
        section("坏输入不炸")
        for label, bad in [
            ("None", None),
            ("空 dict", {}),
            ("traits 是列表", {"traits": [1, 2]}),
            ("values 是字符串", {"values": "abc"}),
            ("identity 是数字", {"identity": 7}),
            ("嵌套 None", {"identity": None, "values": None, "traits": None}),
        ]:
            try:
                n = vault.normalize_soul(bad)
                check(f"normalize({label}) 不抛", isinstance(n, dict) and "identity" in n)
            except Exception as exc:
                check(f"normalize({label}) 不抛", False, str(exc))

    finally:
        # ── 无论成败都还原 ──
        section("还原原始灵魂")
        try:
            shutil.copyfile(backup, soul_path)
            back, bex, berr = vault.read_soul_file(soul_path)
            ok = (bex is True and berr == ""
                  and back["identity"]["whoIAm"] == soul["identity"]["whoIAm"]
                  and back["values"] == soul["values"])
            check("已还原并通过内容校验", ok)
            check("revision 回到原值", back["revision"] == soul["revision"],
                  f"{back['revision']} vs {soul['revision']}")
            restored = True
        except Exception as exc:
            check("还原失败", False, str(exc))

        if restored:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"\n⚠ 还原失败，备份仍在：{backup}")

    # ── 真实记忆库（只读） ──
    section("真实记忆库（只读）")
    db = vault.existing_path(root, "memory")
    if os.path.isfile(db):
        work = os.path.join(_ROOT, ".tmp")
        local = vault.copy_memory_for_read(db, work)
        st = vault.memory_stats(local)
        check("能读统计", st["total"] >= 0, str(st))
        print(f"       {st['total']} 条消息 / {st['sessions']} 会话 / "
              f"{st['chunks']} 片段 / {st['anchors']} 锚点")
        page = vault.browse_messages(local, 0, 3)
        check("能分页", len(page["items"]) <= 3, str(len(page["items"])))
        sess = vault.list_sessions(local, 3)
        check("能列会话", isinstance(sess, list))
    else:
        # 清空过的数据根本来就没有记忆库 —— 这是正常状态，不是失败。
        check("记忆库为空（已清空，属正常）", True)
        print(f"       {db} 不存在")

    # ── 真实心情配置（只读） ──
    section("真实心情配置（只读）")
    cfg = vault.read_mood_config()
    check("读到 moodBase", isinstance(cfg.get("moodBase"), int), str(cfg.get("moodBase")))
    check("injectMode 合法",
          cfg.get("injectMode") in vault.INJECT_MODES, str(cfg.get("injectMode")))
    print(f"       base={cfg.get('moodBase')} mode={cfg.get('injectMode')} "
          f"decay={cfg.get('decayHalfHours')}h")

    print()
    print("=" * 62)
    total = PASS + FAIL
    print(f"结果：{PASS}/{total} 通过" + ("" if not FAIL else f"，{FAIL} 项失败"))
    if FAILED:
        for f in FAILED:
            print("  ·", f)
    print("=" * 62)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
