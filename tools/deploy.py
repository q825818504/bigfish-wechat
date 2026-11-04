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


def backup_data() -> None:
    src = DIST / "data"
    if not src.is_dir():
        return
    if BACKUP.exists():
        shutil.rmtree(BACKUP, ignore_errors=True)
    shutil.copytree(src, BACKUP / "data")
    cfg = DIST / "config.json"
    if cfg.is_file():
        BACKUP.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cfg, BACKUP / "config.json")
    log(f"已备份数据 → {BACKUP.relative_to(ROOT)}")


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
    except Exception:
        # 复制失败：把旧程序放回去，别留一个半新半旧的目录
        if moved_old and (DIST / "_internal").exists():
            shutil.rmtree(DIST / "_internal", ignore_errors=True)
        if moved_old and internal_old.exists():
            internal_old.rename(DIST / "_internal")
        raise

    if internal_old.exists():
        shutil.rmtree(internal_old, ignore_errors=True)
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

    backup_data()
    build()
    stage()
    deploy()
    ok = verify()

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
