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
import ssl
import subprocess
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

#: 默认更新源。
#:
#: 「能不能检查更新」不该取决于用户有没有手改 JSON —— 以前 `update.repo`
#: 默认是空串，于是点「检查」只会得到一句「未配置更新仓库」。
#: fork 的人可以在 config.json 里填自己的仓库覆盖它。
DEFAULT_REPO = "q825818504/bigfish-wechat"

#: 检查结果短缓存（秒）。手动点「检查」时走缓存，免得 GitHub 限流
#: （未认证 60 次/小时，一次误点连点几下就没了）。
CHECK_TTL = 300


def normalize_repo(s: str) -> str:
    """把用户可能填的各种形式收拾成 `owner/repo`。

        https://github.com/a/b       → a/b
        https://github.com/a/b.git   → a/b
        a/b.git                      → a/b
        a/b/                         → a/b
    """
    v = (s or "").strip()
    if not v:
        return ""
    for pre in ("https://github.com/", "http://github.com/",
                "github.com/", "git@github.com:"):
        if v.lower().startswith(pre):
            v = v[len(pre):]
            break
    v = v.strip("/")
    if v.endswith(".git"):
        v = v[:-4]
    # 只保留前两段（后面可能是 /releases 之类）
    parts = [p for p in v.split("/") if p]
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return v


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
        if exc.code == 403:
            # GitHub 未认证 API 是 60 次/小时。撞上之后原始报错只有
            # "HTTP 403"，用户完全看不出是限流还是仓库没权限。
            remaining = (exc.headers.get("X-RateLimit-Remaining") or "")
            reset = (exc.headers.get("X-RateLimit-Reset") or "")
            if remaining.strip() == "0" and reset.strip().isdigit():
                mins = max(1, int((int(reset) - time.time()) / 60) + 1)
                raise RuntimeError(
                    f"GitHub 查询次数用完了（每小时 60 次），"
                    f"约 {mins} 分钟后恢复。") from exc
            raise RuntimeError(
                "HTTP 403：GitHub 拒绝了这次查询（限流，或者仓库是私有的）"
            ) from exc
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"网络不可达: {exc.reason}") from exc


def _last_file(cache_dir: str) -> str:
    return os.path.join(cache_dir, "last.json") if cache_dir else ""


def _save_last(cache_dir: str, info: dict) -> None:
    """把上一次成功的结果存下来。

    用途只有一个：限流或断网时还能显示"上次查到的最新版是哪个"，
    而不是甩一句「HTTP 403」让用户以为程序坏了。
    """
    p = _last_file(cache_dir)
    if not p:
        return
    try:
        os.makedirs(cache_dir, exist_ok=True)
        payload = dict(info)
        payload["_at"] = time.time()
        payload.pop("notes", None)      # 说明太长，没必要缓存
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception:
        pass


def _load_last(cache_dir: str) -> dict:
    p = _last_file(cache_dir)
    if not p or not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def check_latest(repo: str, current: str, cache_dir: str = "") -> dict:
    """查最新版本。

    返回：
        {"has_update": bool, "latest": "1.2.3", "notes": "...",
         "assets": [{"name":..., "url":..., "size":..., "sha256":...}],
         "url": "Release 页面"}
    """
    repo = normalize_repo(repo) or DEFAULT_REPO

    # ① 刚查过就用上次的结果 —— 用户连点「检查」不该把限额打光
    last = _load_last(cache_dir)
    if last and (time.time() - float(last.get("_at") or 0)) < CHECK_TTL:
        cached = dict(last)
        cached.pop("_at", None)
        cached.update({"cached": True, "repo": repo, "current": current})
        return cached

    etag_file = os.path.join(cache_dir, "release.etag") if cache_dir else ""
    try:
        data = _get_json(GITHUB_API.format(repo=repo), etag_file=etag_file)
    except RuntimeError as exc:
        # ② 限流/断网：有上次的结果就先顶一下，并说清楚是旧的
        if last:
            stale = dict(last)
            stale.pop("_at", None)
            stale.update({"stale": True, "stale_reason": str(exc),
                          "repo": repo, "current": current})
            return stale
        raise

    if data is None:
        # 304 = 服务器说"和上次一样"。这时拿不到最新号，只能说没新版，
        # 但把 repo 带回去，界面能显示"查的是哪个仓库"。
        if last:
            cached = dict(last)
            cached.pop("_at", None)
            cached.update({"cached": True, "repo": repo, "current": current})
            return cached
        return {"has_update": False, "latest": current, "cached": True,
                "current": current, "repo": repo}

    tag = data.get("tag_name") or data.get("name") or ""
    assets = []
    for a in (data.get("assets") or []):
        assets.append({
            "name": a.get("name", ""),
            "url": a.get("browser_download_url", ""),
            "size": a.get("size", 0),
            "sha256": (a.get("digest") or "").replace("sha256:", ""),
        })

    result = {
        "has_update": is_newer(tag, current),
        "latest": tag.lstrip("vV"),
        "current": current,
        "repo": repo,
        "notes": (data.get("body") or "")[:2000],
        "assets": assets,
        "url": data.get("html_url", ""),
        "published": data.get("published_at", ""),
        "name": data.get("name") or tag,
    }
    _save_last(cache_dir, result)
    return result


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
        self.repo = normalize_repo(repo) or DEFAULT_REPO
        self.current = current
        self.cache_dir = cache_dir
        self.enabled = enabled
        os.makedirs(cache_dir, exist_ok=True)

    def check(self) -> dict:
        if not self.enabled:
            return {"has_update": False, "disabled": True}
        try:
            return check_latest(self.repo, self.current, self.cache_dir)
        except Exception as exc:
            return {"has_update": False, "error": str(exc), "repo": self.repo}

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
