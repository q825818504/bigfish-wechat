# -*- coding: utf-8 -*-
"""VB-CABLE 虚拟声卡：包校验 + 一键安装。

为什么要内置驱动
----------------
语音气泡的路线是「本程序 → CABLE Input → VB-CABLE → CABLE Output → 微信麦克风」。
用户不该为了这个去官网找 Pack45、解压、找安装程序。

包来自官方 VB-CABLE Driver Pack45，按 whale_girl 的 31 个文件 SHA256 清单校验。
只校验、不修改；安装调用官方 `VBCABLE_Setup_x64.exe -i -h`（Chocolatey 同款静默参数）。
驱动是 VB-Audio Donationware，`readme.txt` 随包保留。
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import paths
from . import voice_audio_out as _audio


def _candidates(name: str) -> List[Path]:
    out: List[Path] = []
    try:
        out.append(Path(paths.res_path(name)))
    except Exception:
        pass
    try:
        out.append(Path(paths.app_path(name)))
    except Exception:
        pass
    out.append(Path(__file__).resolve().parents[1] / "vendor" / name)
    return out


def _first(name: str) -> Optional[Path]:
    for p in _candidates(name):
        if p.exists():
            return p
    return None


def package_dir() -> Optional[Path]:
    p = _first("vb-cable")
    return p if p and p.is_dir() else None


def _manifest() -> Dict[str, str]:
    p = _first("vb-cable-pack45.json")
    if not p:
        return {}
    try:
        raw = json.loads(p.read_text("utf-8"))
        return {str(k): str(v) for k, v in raw.items()}
    except Exception:
        return {}


def package_status() -> Dict[str, Any]:
    """不安装，只回答：包在不在、完不完整、声卡装没装。"""
    d = package_dir()
    manifest = _manifest()
    missing: List[str] = []
    bad: List[str] = []
    if d is None:
        return {"ok": False, "installed": _audio.cable_installed(),
                "dir": "", "missing": ["vb-cable 目录"], "bad": [],
                "setup": ""}
    for name, want in manifest.items():
        f = d / name
        if not f.is_file():
            missing.append(name)
            continue
        try:
            got = hashlib.sha256(f.read_bytes()).hexdigest()
        except OSError:
            bad.append(name)
            continue
        if got.lower() != want.lower():
            bad.append(name)
    setup = d / "VBCABLE_Setup_x64.exe"
    if not setup.is_file():
        missing.append("VBCABLE_Setup_x64.exe")
    return {
        "ok": not missing and not bad and setup.is_file(),
        "installed": _audio.cable_installed(),
        "dir": str(d),
        "setup": str(setup) if setup.is_file() else "",
        "missing": missing,
        "bad": bad,
        "files": len(manifest),
        "donation": "https://vb-audio.com/Cable/",
    }


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def install() -> Dict[str, Any]:
    """一键安装。返回 started/already/exit_code 三种状态之一。

    非管理员会弹 UAC；ShellExecuteW 只负责拉起，退出码拿不到，
    所以返回 started=True，前端轮询设备列表确认。
    """
    st = package_status()
    if st["installed"]:
        return {"ok": True, "already": True, "message": "VB-CABLE 已经安装"}
    if not st["ok"]:
        return {"ok": False, "error": "驱动包不完整：" + "；".join(
            (st.get("missing") or []) + (st.get("bad") or []))}

    setup = Path(st["setup"])
    args = "-i -h"
    if _is_admin():
        # 不阻塞 GUI：启动后立即返回，前端轮询设备列表确认。
        try:
            subprocess.Popen([str(setup), "-i", "-h"], cwd=str(setup.parent),
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return {"ok": True, "started": True, "admin": True,
                    "message": "安装程序已启动，等待驱动生效"}
        except Exception as exc:
            return {"ok": False, "error": f"安装失败：{exc}"}

    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", str(setup), args, str(setup.parent), 1)
        if int(rc) <= 32:
            return {"ok": False, "error": f"无法启动安装程序（ShellExecute={rc}）"}
        return {
            "ok": True, "started": True,
            "message": "已弹出 UAC，请允许安装；装完可能需要重启电脑",
        }
    except Exception as exc:
        return {"ok": False, "error": f"启动安装程序失败：{exc}"}


def open_sound_settings() -> Dict[str, Any]:
    try:
        os.startfile("ms-settings:sound")
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
