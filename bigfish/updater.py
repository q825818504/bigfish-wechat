"""[bigfish] 自动更新 —— 查 GitHub Releases、下载、静默替换。

流程：
  1. 启动后（或手动）请求 GitHub Releases API
  2. 对比本地版本号，有新版本就下载 exe/zip 到临时目录
  3. 校验 SHA256
  4. 写一个 updater.cmd，主程序退出后由它替换文件、拉起新版
  5. 重启

为什么用「外部脚本替换」而不是进程内自我替换：
  Windows 上正在运行的 exe 不能被覆盖。必须有一个**不在程序目录里运行的**
  进程来做替换。做法是把替换步骤写成 .cmd 落到 %TEMP%，用 detached 方式启动，
  主程序退出后它才开始干活。

配置在 config.json 的 update 段：
    {
      "update": {
        "repo": "owner/dsh-bigfish-wechat",
        "enabled": true,
        "channel": "stable",
        "check_on_start": true
      }
    }
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

# 版本号真源在 bigfish/__init__.py，这里不重复定义。
# 取不到就退回 0.0.0 —— 更新检查会因此认为"有新版"，
# 但那比显示一个假的版本号安全。
try:
    from bigfish import __version__ as _PKG_VERSION
except Exception:
    _PKG_VERSION = "0.0.0"

__version__ = _PKG_VERSION

GITHUB_API = "https://api.github.com/repos/{repo}/releases/latest"
UA = f"bigfish-updater/{__version__}"


# ══════════════════════════════════════════════════════════════════
#  版本比较
# ══════════════════════════════════════════════════════════════════
def parse_version(s: str) -> tuple:
    """'v1.2.3' / '1.2.3-beta' -> (1,2,3,'beta')，便于比较。"""
    s = (s or "").strip().lstrip("vV")
    if not s:
        return (0,)
    main, _, pre = s.partition("-")
    parts = []
    for p in main.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    # 有预发布标记的排在同版本的正式版之前
    return tuple(parts) + ((0, pre) if pre else (1, ""))


def is_newer(remote: str, local: str) -> bool:
    return parse_version(remote) > parse_version(local)


# ══════════════════════════════════════════════════════════════════
#  查询
# ══════════════════════════════════════════════════════════════════
def _get_json(url: str, timeout: float = 15.0,
              etag_file: str = "") -> dict | None:
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    if etag_file and os.path.isfile(etag_file):
        try:
            with open(etag_file, "r", encoding="utf-8") as f:
                etag = f.read().strip()
            if etag:
                headers["If-None-Match"] = etag
        except Exception:
            pass

    req = urllib.request.Request(url, headers=headers)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            data = json.loads(r.read().decode("utf-8"))
            new_etag = r.headers.get("ETag")
            if etag_file and new_etag:
                try:
                    with open(etag_file, "w", encoding="utf-8") as f:
                        f.write(new_etag)
                except Exception:
                    pass
            return data
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return None
        if exc.code == 404:
            raise RuntimeError(f"仓库或 Release 不存在: {url}") from exc
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"网络不可达: {exc.reason}") from exc


def check_latest(repo: str, current: str, cache_dir: str = "") -> dict:
    """查最新版本。

    返回：
        {"has_update": bool, "latest": "1.2.3", "notes": "...",
         "assets": [{"name":..., "url":..., "size":..., "sha256":...}],
         "url": "Release 页面"}
    """
    etag_file = os.path.join(cache_dir, "release.etag") if cache_dir else ""
    data = _get_json(GITHUB_API.format(repo=repo), etag_file=etag_file)
    if data is None:
        return {"has_update": False, "latest": current, "cached": True}

    tag = data.get("tag_name") or data.get("name") or ""
    assets = []
    for a in (data.get("assets") or []):
        assets.append({
            "name": a.get("name", ""),
            "url": a.get("browser_download_url", ""),
            "size": a.get("size", 0),
            "sha256": (a.get("digest") or "").replace("sha256:", ""),
        })

    return {
        "has_update": is_newer(tag, current),
        "latest": tag.lstrip("vV"),
        "current": current,
        "notes": (data.get("body") or "")[:2000],
        "assets": assets,
        "url": data.get("html_url", ""),
        "published": data.get("published_at", ""),
    }


# ══════════════════════════════════════════════════════════════════
#  下载
# ══════════════════════════════════════════════════════════════════
def download(url: str, dest: str, expect_sha256: str = "",
             progress=None, timeout: float = 60.0) -> str:
    """下载并（可选）校验 SHA256。返回文件路径。"""
    headers = {"User-Agent": UA}
    req = urllib.request.Request(url, headers=headers)
    ctx = ssl.create_default_context()

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    h = hashlib.sha256()
    total = 0
    done = 0

    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        total = int(r.headers.get("Content-Length") or 0)
        with open(tmp, "wb") as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)

    digest = h.hexdigest()
    if expect_sha256 and digest.lower() != expect_sha256.lower():
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise RuntimeError(
            f"SHA256 不匹配，已丢弃下载文件\n"
            f"  期望 {expect_sha256}\n  实际 {digest}")

    os.replace(tmp, dest)
    return dest


# ══════════════════════════════════════════════════════════════════
#  安装（静默替换）
# ══════════════════════════════════════════════════════════════════
_UPDATER_TEMPLATE = r"""@echo off
chcp 65001 >nul 2>&1
setlocal

rem 由大肥鱼自动更新生成。等主程序退出后替换文件并重启。
set "WAIT_PID=%(pid)d"
set "SRC=%(src)s"
set "DST=%(dst)s"
set "EXE=%(exe)s"
set "LOG=%(log)s"

echo [%%date%% %%time%%] updater start >> "%%LOG%%"

rem 等旧进程退出（最多 60 秒）
set /a N=0
:wait
tasklist /FI "PID eq %%WAIT_PID%%" 2>nul | find "%%WAIT_PID%%" >nul
if errorlevel 1 goto ready
set /a N+=1
if %%N%% GEQ 60 goto ready
timeout /t 1 /nobreak >nul
goto wait

:ready
echo [%%date%% %%time%%] old process gone, copying >> "%%LOG%%"

rem 备份
if exist "%%DST%%" (
    move /y "%%DST%%" "%%DST%%.old" >> "%%LOG%%" 2>&1
)

rem 替换
move /y "%%SRC%%" "%%DST%%" >> "%%LOG%%" 2>&1
if errorlevel 1 (
    echo [%%date%% %%time%%] copy failed, rollback >> "%%LOG%%"
    if exist "%%DST%%.old" move /y "%%DST%%.old" "%%DST%%" >> "%%LOG%%" 2>&1
    exit /b 1
)

echo [%%date%% %%time%%] updated, restarting >> "%%LOG%%"
start "" "%%EXE%%"
del /f /q "%%DST%%.old" 2>nul
echo [%%date%% %%time%%] done >> "%%LOG%%"
(goto) 2>nul & del "%%~f0"
"""


def schedule_replace(downloaded: str, target: str,
                     log_path: str = "") -> str:
    """写一个 updater.cmd 并 detached 启动，返回脚本路径。

    调用方应在返回后立即退出主程序 —— 脚本会等它结束再替换。
    """
    tmpdir = tempfile.gettempdir()
    log_path = log_path or os.path.join(tmpdir, "bigfish_update.log")
    script = os.path.join(tmpdir, f"bigfish_updater_{int(time.time())}.cmd")

    body = _UPDATER_TEMPLATE % {
        "pid": os.getpid(),
        "src": downloaded,
        "dst": target,
        "exe": target,
        "log": log_path,
    }
    with open(script, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(body)

    flags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        ["cmd.exe", "/c", script],
        creationflags=flags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return script


# ══════════════════════════════════════════════════════════════════
#  顶层封装
# ══════════════════════════════════════════════════════════════════
class Updater:
    """给界面用的一层薄封装。"""

    def __init__(self, repo: str, current: str, cache_dir: str,
                 enabled: bool = True):
        self.repo = repo
        self.current = current
        self.cache_dir = cache_dir
        self.enabled = enabled
        os.makedirs(cache_dir, exist_ok=True)

    def check(self) -> dict:
        if not self.enabled or not self.repo:
            return {"has_update": False, "disabled": True}
        try:
            return check_latest(self.repo, self.current, self.cache_dir)
        except Exception as exc:
            return {"has_update": False, "error": str(exc)}

    def pick_asset(self, info: dict) -> dict | None:
        """从 Release 里挑出要下载的文件。

        优先 .exe（安装包），其次 zip；找不到返回 None。
        """
        assets = info.get("assets") or []
        for ext in (".exe", ".zip"):
            for a in assets:
                if a["name"].lower().endswith(ext):
                    return a
        return None

    def download_and_schedule(self, info: dict, target_exe: str,
                              progress=None) -> str:
        a = self.pick_asset(info)
        if not a:
            raise RuntimeError("这个 Release 里没有可用的安装包")

        dest = os.path.join(self.cache_dir, a["name"])
        download(a["url"], dest, a.get("sha256") or "", progress=progress)
        return schedule_replace(dest, target_exe)


def read_update_config(config_path: str) -> dict:
    """从 config.json 读 update 段。"""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return (json.load(f).get("update") or {})
    except Exception:
        return {}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="大肥鱼自动更新")
    ap.add_argument("--repo", required=True, help="owner/repo")
    ap.add_argument("--current", default=__version__)
    ap.add_argument("--check", action="store_true", help="只检查")
    args = ap.parse_args()

    info = check_latest(args.repo, args.current,
                        os.path.join(tempfile.gettempdir(), "bf_update"))
    print(json.dumps(info, ensure_ascii=False, indent=2))
