"""生成安装包 —— 不依赖 Inno Setup。

产物：
    dist/BigFish-<version>-setup.exe     自解压安装器（双击安装）
    dist/BigFish-<version>-portable.zip   便携版（解压即用）

自解压安装器的原理：
    把一个 .zip 和一段安装逻辑拼成一个 exe。
    运行 exe 时，它会：
      1. 解压到 %LOCALAPPDATA%\\Programs\\BigFish
      2. 创建开始菜单和桌面快捷方式
      3. 写入卸载信息到注册表
      4. 启动程序

    整个安装器是**本仓库的 Python 打成的 exe**（PyInstaller），
    不依赖第三方工具链。

为什么不做成 MSI：
    MSI 需要 WiX 工具链，同样要装东西。自解压 exe 对用户来说体验
    差别不大（双击、下一步、完成），而部署成本低得多。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DIST = os.path.join(ROOT, "dist")
PAYLOAD = os.path.join(DIST, "BigFish")
VERSION = "0.1.0"

APP_NAME = "BigFish"
APP_DISPLAY = "大肥鱼"
APP_ID = "BigFish_WeChat"


# ══════════════════════════════════════════════════════════════
#  安装器源码（会被打成 exe 放进安装包里）
# ══════════════════════════════════════════════════════════════
INSTALLER_SRC = r'''# -*- coding: utf-8 -*-
"""大肥鱼 安装程序。"""
import json
import os
import shutil
import sys
import tempfile
import zipfile

APP_NAME = "BigFish"
APP_DISPLAY = "大肥鱼"
APP_ID = "BigFish_WeChat"
VERSION = __VERSION__
PAYLOAD = "payload.zip"


def target_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Programs", APP_NAME)


def make_shortcut(path, target, workdir, icon):
    """用 PowerShell 建 .lnk，不需要 pywin32。"""
    ps = (
        "$W = New-Object -ComObject WScript.Shell;"
        f"$S = $W.CreateShortcut('{path}');"
        f"$S.TargetPath = '{target}';"
        f"$S.WorkingDirectory = '{workdir}';"
        f"$S.IconLocation = '{icon}';"
        "$S.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   capture_output=True, creationflags=0x08000000)


def install():
    import subprocess

    # payload.zip 与 setup.exe 同目录；PyInstaller onefile 会把自身解压到
    # 临时目录运行，所以 sys.argv[0] 指向临时路径，不能用。
    # 用 sys.executable 的原始位置（onefile 下它是临时路径）——
    # 正确做法是读取 PyInstaller 注入的 _MEIPASS 之外的环境，
    # 但更简单可靠的是：让外层 bat 把目录传进来。
    here = os.environ.get("BIGFISH_SETUP_DIR") or os.path.dirname(
        os.path.abspath(sys.argv[0]))
    payload = os.path.join(here, PAYLOAD)
    if not os.path.isfile(payload):
        # 退而求其次：当前工作目录
        payload = os.path.join(os.getcwd(), PAYLOAD)
    if not os.path.isfile(payload):
        raise FileNotFoundError(
            f"找不到 payload.zip。\n"
            f"请确保 setup.exe 和 payload.zip 在同一个文件夹里。\n"
            f"已尝试: {here}")

    dst = target_dir()
    print(f"安装到: {dst}")
    os.makedirs(dst, exist_ok=True)

    print("解压文件 ...")
    with zipfile.ZipFile(payload) as z:
        z.extractall(dst)

    exe = os.path.join(dst, f"{APP_NAME}.exe")
    icon = os.path.join(dst, "ui", "icon.ico")

    print("创建快捷方式 ...")
    start_menu = os.path.join(
        os.environ.get("APPDATA", ""),
        "Microsoft", "Windows", "Start Menu", "Programs")
    os.makedirs(start_menu, exist_ok=True)
    make_shortcut(os.path.join(start_menu, f"{APP_DISPLAY}.lnk"),
                  exe, dst, icon)

    desktop = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
    if os.path.isdir(desktop):
        make_shortcut(os.path.join(desktop, f"{APP_DISPLAY}.lnk"),
                      exe, dst, icon)

    print("写入卸载信息 ...")
    uninst = os.path.join(dst, "uninstall.cmd")
    sm_lnk = os.path.join(start_menu, f"{APP_DISPLAY}.lnk")
    # 卸载脚本用 GBK 写（cmd 在中文 Windows 上默认按 GBK 读），
    # 但先把内容拼好再写，避免源码里直接出现非 UTF-8 字面量。
    lines = [
        "@echo off",
        f'rmdir /s /q "{dst}"',
        f'del /f /q "{sm_lnk}"',
        'reg delete "HKCU\\Software\\Microsoft\\Windows'
        '\\CurrentVersion\\Uninstall\\' + APP_ID + '" /f',
    ]
    with open(uninst, "w", encoding="gbk", errors="replace") as f:
        f.write("\r\n".join(lines) + "\r\n")

    ps = (
        "$k = 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion"
        "\\Uninstall\\" + APP_ID + "';"
        "New-Item -Path $k -Force | Out-Null;"
        f"Set-ItemProperty -Path $k -Name DisplayName -Value '{APP_DISPLAY}';"
        f"Set-ItemProperty -Path $k -Name DisplayVersion -Value '{VERSION}';"
        f"Set-ItemProperty -Path $k -Name InstallLocation -Value '{dst}';"
        f"Set-ItemProperty -Path $k -Name DisplayIcon -Value '{exe}';"
        f"Set-ItemProperty -Path $k -Name UninstallString -Value '{uninst}'"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   capture_output=True, creationflags=0x08000000)

    print("完成。")
    return exe


def main():
    print("=" * 56)
    print(f"  {APP_DISPLAY} 安装程序  v{VERSION}")
    print("=" * 56)
    print()
    try:
        exe = install()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print()
        print(f"安装失败: {exc}")
        input("按回车退出...")
        return 1

    print()
    ans = input("现在启动？(Y/n) ").strip().lower()
    if ans != "n":
        import subprocess
        subprocess.Popen([exe], cwd=os.path.dirname(exe))
    return 0


if __name__ == "__main__":
    import subprocess
    sys.exit(main())
'''


def build_portable_zip() -> str:
    """把 dist/BigFish 打成便携版 zip。"""
    out = os.path.join(DIST, f"BigFish-{VERSION}-portable.zip")
    if os.path.isfile(out):
        os.unlink(out)

    print(f"  打包便携版: {out}")
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for base, dirs, files in os.walk(PAYLOAD):
            for f in files:
                p = os.path.join(base, f)
                rel = os.path.relpath(p, PAYLOAD)
                z.write(p, rel)
                n += 1
    size = os.path.getsize(out) / 1024 / 1024
    print(f"     {n} 个文件, {size:.1f} MB")
    return out


def build_installer(zip_path: str) -> str:
    """用 PyInstaller 把安装器源码打成 exe，并配一个启动脚本。

    不做「zip 附加到 exe 尾部」这种自解压——PyInstaller 的 onefile
    本身就在 exe 尾部存了一个归档，再往后面追加东西会破坏它的引导逻辑
    （实测会导致 "Failed to load Python DLL"）。

    改用最稳的形态：**安装器 exe + payload.zip 放在同一目录**，
    外面套一个 .bat 双击入口。用户视角仍然是「双击一个文件安装」。
    """
    work = os.path.join(ROOT, "build", "installer")
    os.makedirs(work, exist_ok=True)

    py = os.path.join(work, "installer.py")
    with open(py, "w", encoding="utf-8") as f:
        f.write(INSTALLER_SRC.replace("__VERSION__", f'"{VERSION}"'))

    print("  编译安装器 ...")
    # 用 onedir 而不是 onefile：
    # onefile 每次运行要把自身解压到 %TEMP%，某些受限环境会失败
    # （实测 "Could not create temporary directory!"）。
    # onedir 直接就地运行，不需要写临时目录。
    r = subprocess.run([
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onedir", "--console",
        "--name", "setup",
        "--distpath", work,
        "--workpath", os.path.join(work, "w"),
        "--specpath", work,
        py,
    ], capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-2000:])
        print(r.stderr[-2000:])
        raise RuntimeError("安装器编译失败")

    setup_dir = os.path.join(work, "setup")

    # 组装安装目录
    out_dir = os.path.join(DIST, f"BigFish-{VERSION}-installer")
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    # 把 onedir 产物整个拷进去
    for name in os.listdir(setup_dir):
        src = os.path.join(setup_dir, name)
        dst = os.path.join(out_dir, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)

    shutil.copy2(zip_path, os.path.join(out_dir, "payload.zip"))

    # 双击入口（纯 ASCII，避免 cmd 把 UTF-8 当 GBK）
    bat = os.path.join(out_dir, "install.bat")
    with open(bat, "w", encoding="ascii", newline="\r\n") as f:
        f.write("@echo off\r\n")
        f.write("chcp 65001 >nul 2>&1\r\n")
        f.write('cd /d "%~dp0"\r\n')
        f.write('set "BIGFISH_SETUP_DIR=%~dp0"\r\n')
        f.write('"%~dp0setup.exe"\r\n')
        f.write("pause\r\n")

    total = sum(os.path.getsize(os.path.join(out_dir, n))
                for n in os.listdir(out_dir))
    print(f"      {out_dir}  ({total/1024/1024:.1f} MB)")
    return out_dir


def build_manifest() -> str:
    """生成文件清单与校验，自动更新用它判断完整性。"""
    out = os.path.join(DIST, f"BigFish-{VERSION}-manifest.json")
    files = {}
    for base, dirs, fs in os.walk(PAYLOAD):
        for f in fs:
            p = os.path.join(base, f)
            rel = os.path.relpath(p, PAYLOAD).replace("\\", "/")
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            files[rel] = {"size": os.path.getsize(p), "sha256": h.hexdigest()}
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"version": VERSION, "files": files}, f,
                  ensure_ascii=False, indent=2)
    print(f"  清单: {out}（{len(files)} 个文件）")
    return out


def main() -> int:
    if not os.path.isdir(PAYLOAD):
        print(f"找不到打包产物: {PAYLOAD}")
        print("请先运行 packaging\\build.bat")
        return 1

    print("=" * 60)
    print(f"  生成安装包 v{VERSION}")
    print("=" * 60)

    print("\n[1/3] 便携版")
    z = build_portable_zip()

    print("\n[2/3] 清单")
    build_manifest()

    print("\n[3/3] 安装器")
    exe = build_installer(z)

    print()
    print("=" * 60)
    print("  产物：")
    for f in sorted(os.listdir(DIST)):
        if f.startswith("BigFish-"):
            p = os.path.join(DIST, f)
            print(f"    {f:44s} {os.path.getsize(p)/1024/1024:7.1f} MB")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
