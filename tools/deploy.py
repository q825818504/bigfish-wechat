"""一键部署：强杀运行中的 BigFish → 打包 → 覆盖程序文件 → 自检。

用法：
    python tools/deploy.py              # 打包并覆盖到 dist/BigFish
    python tools/deploy.py --no-kill    # 不杀进程（调试用）
    python tools/deploy.py --dry-run    # 只打印会做什么

设计约束：
  · **只覆盖程序**（exe / _internal / ui / bridge），`data/` 与 `config.json` 一律不动。
  · 覆盖前把 data 备份到 data-backup/，覆盖前把 _internal 改名留底，失败能回滚。
  · 强杀进程是刻意的：GUI 没有"优雅退出"的命令行入口，不杀就会文件占用。
    SQLite 是 WAL 模式，强杀后下次打开会自动恢复，不会丢已提交的事务。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "BigFish"
BUILD_OUT = ROOT / "dist_build"
BACKUP = ROOT / "data-backup"
SPEC = ROOT / "packaging" / "bigfish.spec"

#: 只替换这些；data/ config.json settings.json 永远保留
PROGRAM_ITEMS = ("BigFish.exe", "_internal", "ui", "bridge")


def log(msg: str) -> None:
    print(f"  {msg}", flush=True)


def kill_running() -> int:
    """强杀 BigFish。返回被杀的进程数（0 表示本来没在跑）。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq BigFish.exe"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).stdout or ""
    except Exception:
        return 0
    if "BigFish.exe" not in out:
        return 0
    log("发现运行中的 BigFish，强制结束 ...")
    subprocess.run(["taskkill", "/F", "/IM", "BigFish.exe"],
                   capture_output=True)
    time.sleep(1.0)
    return 1


def data_root() -> Path:
    """真正的数据目录。

    数据根不一定在 `dist/BigFish/data` —— `settings.json` 的 `dataRoot`
    可以把它指到任何地方（例如 `D:\\大肥鱼`）。备份与指纹校验都得以它为准，
    否则会出现"备份了一个空目录，真实记忆没被保护"的假安全感。
    """
    for p in (DIST / "settings.json", ROOT / "settings.json"):
        try:
            if p.is_file():
                d = json.loads(p.read_text(encoding="utf-8"))
                root = str(d.get("dataRoot") or "").strip()
                if root:
                    return Path(root)
        except Exception:
            pass
    return DIST / "data"


def fingerprint(root: Path) -> dict:
    """数据指纹：文件数 + 总字节 + 关键文件 (mtime,size)。

    打包只该换程序，不该动数据。前后对不上就说明有东西越界了。
    """
    out = {"root": str(root), "files": 0, "bytes": 0, "key": {}}
    if not root.is_dir():
        return out
    for dirpath, _dirs, files in os.walk(root):
        for n in files:
            p = Path(dirpath) / n
            try:
                st = p.stat()
            except OSError:
                continue
            out["files"] += 1
            out["bytes"] += st.st_size
    for name in ("memory.sqlite3", "memory.sqlite3-wal", "state.json"):
        for p in root.rglob(name):
            try:
                st = p.stat()
                out["key"][str(p.relative_to(root))] = (int(st.st_mtime), st.st_size)
            except OSError:
                pass
    return out


def backup_data() -> None:
    """把真实数据目录备份到 data-backup/（只读复制，不移动）。"""
    root = data_root()
    if not root.is_dir():
        log(f"数据目录不存在，跳过备份：{root}")
        return
    if BACKUP.exists():
        shutil.rmtree(BACKUP, ignore_errors=True)
    try:
        shutil.copytree(root, BACKUP / "data")
    except Exception as exc:
        # 备份失败要让人看见：后面还要覆盖程序，不能让用户以为有备份
        log(f"⚠ 数据备份失败：{exc}")
        raise
    cfg = DIST / "config.json"
    if cfg.is_file():
        BACKUP.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cfg, BACKUP / "config.json")
    log(f"已备份数据 → {BACKUP.relative_to(ROOT)}（源：{root}）")


def build() -> None:
    log("开始打包（约 30~40 秒）...")
    if BUILD_OUT.exists():
        shutil.rmtree(BUILD_OUT, ignore_errors=True)
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--distpath", str(BUILD_OUT), "--workpath", str(ROOT / "build"),
           str(SPEC)]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        tail = "\n".join((r.stdout or "").splitlines()[-15:])
        print(tail, flush=True)
        raise SystemExit(f"打包失败（退出码 {r.returncode}）")
    log("打包完成")


def stage() -> None:
    """把外部资源补到构建产物里（spec 已含 ui/bridge，这里补配置与说明）。"""
    out = BUILD_OUT / "BigFish"
    for name in ("config.example.json", "README.md"):
        src = ROOT / name
        if src.is_file():
            shutil.copy2(src, out / name)
    log("已补外部资源")


def deploy() -> None:
    src = BUILD_OUT / "BigFish"
    DIST.mkdir(parents=True, exist_ok=True)
    internal_old = DIST / "_internal.old"
    if internal_old.exists():
        shutil.rmtree(internal_old, ignore_errors=True)

    # ⚠ 必须在覆盖 _internal **之前**把旧配置读出来。
    #   第一次写这段时读在 copytree 之后 —— 那时文件已经被新包顶掉了，
    #   于是"迁移"永远什么都不做，而且不报错（2026-11-04 实测踩）。
    legacy: dict = {}
    legacy_path = DIST / "_internal" / "config.json"
    if legacy_path.is_file():
        try:
            raw = json.loads(legacy_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                legacy = raw
        except Exception:
            legacy = {}

    moved_old = False
    if (DIST / "_internal").exists():
        (DIST / "_internal").rename(internal_old)
        moved_old = True

    try:
        shutil.copytree(src / "_internal", DIST / "_internal")
        shutil.copy2(src / "BigFish.exe", DIST / "BigFish.exe")
        for name in ("ui", "bridge"):
            if (src / name).is_dir():
                shutil.copytree(src / name, DIST / name, dirs_exist_ok=True)
        # PyInstaller 6 把 ui/ 和 bridge/ 打进 _internal/，而程序找 bridge
        # 时 **exe 同级优先**（见 bridge_client._find_bridge）。只刷 _internal
        # 等于没刷：生效的永远是 exe 旁边那份几个月前的旧文件。
        # 两份一起刷，才保证"新打包的资源和程序看到的资源"是同一份。
        for name in ("ui", "bridge"):
            fresh = src / "_internal" / name
            if not fresh.is_dir():
                continue
            target = DIST / name
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(fresh, target)
    except Exception:
        # 复制失败：把旧程序放回去，别留一个半新半旧的目录
        if moved_old and (DIST / "_internal").exists():
            shutil.rmtree(DIST / "_internal", ignore_errors=True)
        if moved_old and internal_old.exists():
            internal_old.rename(DIST / "_internal")
        raise

    # 配置属于用户，归 app_root()/config.json（exe 旁边），不归 _internal。
    # 早先的版本把 CONFIG_PATH 指到了 _internal，用户的设置写在那儿 ——
    # 这里把那份救到 exe 旁边，再把残留改名，别让两套并存。
    live = DIST / "config.json"
    if legacy:
        try:
            cur = json.loads(live.read_text(encoding="utf-8")) if live.is_file() else {}
            if not isinstance(cur, dict):
                cur = {}
            if isinstance(legacy.get("llm"), dict) and not isinstance(cur.get("llm"), dict):
                merged = dict(cur)
                merged.update(legacy)
                live.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                                encoding="utf-8")
                log("旧配置已从 _internal 迁到 exe 旁边")
        except Exception as exc:
            log(f"配置迁移失败（不影响程序）：{exc}")

    if internal_old.exists():
        shutil.rmtree(internal_old, ignore_errors=True)

    # 外部文档/示例配置也要跟着新包走。只更新 _internal 的话，
    # 用户打开 exe 旁边那份 config.example.json 会看到几个月前的字段，
    # 误以为新功能没打包进来。
    for name in ("config.example.json", "README.md"):
        src = ROOT / name
        if src.is_file():
            shutil.copy2(src, DIST / name)

    log("程序文件已覆盖（data / config 未动）")


def verify() -> bool:
    exe = DIST / "BigFish.exe"
    if not exe.is_file():
        return False
    log("跑打包版自检 ...")
    r = subprocess.run([str(exe), "--selftest"], cwd=str(DIST),
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    text = (r.stdout or "") + (r.stderr or "")
    for line in text.splitlines():
        if "说法" in line or "插件" in line or "通过" in line:
            print(f"    {line.strip()}", flush=True)
    # UIA 在非管理员下必然失败，不算部署失败
    return "通过" in text


def main() -> int:
    ap = argparse.ArgumentParser(description="打包并部署 BigFish")
    ap.add_argument("--no-kill", action="store_true", help="不结束运行中的进程")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划")
    args = ap.parse_args()

    print("=" * 60)
    print("  BigFish 一键部署")
    print(f"  目标：{DIST}")
    print("=" * 60)

    if args.dry_run:
        log("将执行：kill → build → stage → deploy → verify")
        return 0

    killed = 0 if args.no_kill else kill_running()
    if killed:
        log("进程已结束")

    root = data_root()
    fp_before = fingerprint(root)
    backup_data()
    build()
    stage()
    deploy()
    # 打包/覆盖这一段不该碰数据；在跑自检之前比对，避免把"程序运行"算进来
    fp_after = fingerprint(root)
    data_ok = fp_before == fp_after
    if data_ok:
        log(f"数据未变：{root}（{fp_after['files']} 文件 / {fp_after['bytes']} 字节）")
    else:
        log(f"⚠ 数据发生变化：{root}（打包过程不该动它，请检查）")
        log(f"   打包前 {fp_before['files']} 文件 / {fp_before['bytes']} 字节")
        log(f"   打包后 {fp_after['files']} 文件 / {fp_after['bytes']} 字节")

    ok = verify() and data_ok

    shutil.rmtree(BUILD_OUT, ignore_errors=True)
    print()
    print("=" * 60)
    print("  部署完成" if ok else "  部署完成（自检有失败项，看上面输出）")
    print(f"  启动：{DIST / 'BigFish.exe'}（管理员）")
    print("  数据：未动；备份在 data-backup/")
    print("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
