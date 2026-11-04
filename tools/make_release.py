"""创建 GitHub Release 并上传附件。

从 .secrets/github_token.txt 读 token（不打印、不落日志）。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
TOKEN_FILE = os.path.join(ROOT, ".secrets", "github_token.txt")
REPO = "q825818504/bigfish-wechat"

#: 覆盖模式：附件已存在时先删再传。`--force` 打开。
FORCE = False


def _read_version() -> str:
    """版本号从 bigfish/__init__.py 读，不在这里硬编码。

    ⚠ 以前这文件里写死 TAG = "v0.1.0"，发 0.2/0.3 时忘了改，
      跑一次就把附件传到 0.1.0 那条 Release 上，而且看起来"成功了"。
    """
    path = os.path.join(ROOT, "bigfish", "__init__.py")
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("__version__"):
                return line.split("=")[1].strip().strip('"').strip("'")
    raise RuntimeError(f"没能从 {path} 读到 __version__")


def _changelog_body(version: str) -> str:
    """从 CHANGELOG.md 里截出这个版本的正文，用作 Release 说明。

    截法是：找到 `## <version>` 那一行，取到下一个 `## ` 之前。
    截不到就退回一句最简说明 —— 宁可说明短，也不要发一条空 Release。
    """
    path = os.path.join(ROOT, "CHANGELOG.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return f"## 大肥鱼 × 微信 v{version}"
    marker = f"## {version}"
    i = text.find(marker)
    if i < 0:
        return f"## 大肥鱼 × 微信 v{version}"
    j = text.find("\n## ", i + len(marker))
    body = text[i:j] if j > 0 else text[i:]
    # 去掉自己的 `## x.y.z` 标题行，Release 标题已经显示版本了
    lines = body.splitlines()
    if lines and lines[0].startswith("## "):
        lines = lines[1:]
    return "\n".join(lines).strip()


VERSION = _read_version()
TAG = "v" + VERSION
BODY = _changelog_body(VERSION)

# 不走系统代理（本机代理未启动）
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)


def token() -> str:
    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()


def api(path: str, method: str = "GET", data=None, raw=False):
    url = "https://api.github.com" + path
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "User-Agent": "bigfish-release",
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token()}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode()) if not raw else r.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"HTTP {exc.code} on {path}\n{detail}") from exc



#: 发布附件。路径按当前版本号拼，**不从 dist/ 取** ——
#: build_portable.py 已经把 zip 和 manifest 生成在 release/ 下，
#: dist/ 目录只放解压后的可执行文件。
RELEASE_DIR = os.path.join(ROOT, "release")
ASSETS = [
    (f"BigFish-{VERSION}-portable.zip",
     os.path.join(RELEASE_DIR, f"BigFish-{VERSION}-portable.zip")),
    (f"BigFish-{VERSION}-manifest.json",
     os.path.join(RELEASE_DIR, f"BigFish-{VERSION}-manifest.json")),
]


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    global FORCE
    FORCE = "--force" in sys.argv or "-f" in sys.argv
    print("=" * 66)
    print("  创建 GitHub Release" + ("（覆盖模式）" if FORCE else ""))
    print("=" * 66)

    # 检查附件
    for name, path in ASSETS:
        if not os.path.isfile(path):
            print(f"  [!] 缺少 {path}")
            return 1
        print(f"  {name:34s} {os.path.getsize(path)/1024/1024:7.1f} MB")

    # 已有 Release 就复用
    print("\n[1] 检查是否已有 Release")
    try:
        rel = api(f"/repos/{REPO}/releases/tags/{TAG}")
        print(f"    已存在 #{rel['id']}，复用")
    except RuntimeError as exc:
        if "404" not in str(exc):
            raise
        print("    不存在，创建中 ...")
        rel = api(f"/repos/{REPO}/releases", "POST", {
            "tag_name": TAG,
            "name": f"v{VERSION}",
            "body": BODY,
            "draft": False,
            "prerelease": False,
        })
        print(f"    创建成功 #{rel['id']}")

    upload_url = rel["upload_url"].split("{")[0]
    existing = {a["name"]: a["id"] for a in rel.get("assets", [])}

    # 上传附件
    print("\n[2] 上传附件")
    for name, path in ASSETS:
        if name in existing:
            if not FORCE:
                print(f"    {name} 已存在，跳过（要覆盖加 --force）")
                continue
            # 覆盖：GitHub 不允许同名附件并存，必须先删旧的。
            # 没有这条的时候，"改完代码重新打包再发一次"会静默保留旧包 ——
            # 版本号一样、Release 页面看着也对，但下载下来是上一版。
            aid = existing[name]
            print(f"    {name} 已存在，删除旧附件 #{aid}")
            try:
                req = urllib.request.Request(
                    f"https://api.github.com/repos/{REPO}/releases/assets/{aid}",
                    method="DELETE",
                    headers={"User-Agent": "bigfish-release",
                             "Accept": "application/vnd.github+json",
                             "Authorization": f"Bearer {token()}"})
                urllib.request.urlopen(req, timeout=60).read()
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:200]
                print(f"      ✗ 删除失败 HTTP {exc.code}: {detail}")
                return 1
            time.sleep(1.5)          # 删完立刻传偶尔会撞 422

        size = os.path.getsize(path)
        digest = sha256_of(path)
        print(f"    {name}  ({size/1024/1024:.1f} MB)")
        print(f"      sha256={digest[:20]}...")

        url = f"{upload_url}?name={urllib.request.quote(name)}"
        with open(path, "rb") as f:
            data = f.read()

        req = urllib.request.Request(url, data=data, method="POST", headers={
            "User-Agent": "bigfish-release",
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token()}",
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(data)),
        })
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                info = json.loads(r.read().decode())
            print(f"      ✓ 上传完成 {time.time()-t0:.0f}s")
            print(f"        {info.get('browser_download_url','')}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            print(f"      ✗ HTTP {exc.code}: {detail}")
            return 1

    # 汇总
    print("\n[3] 最终状态")
    rel = api(f"/repos/{REPO}/releases/tags/{TAG}")
    print(f"    标题: {rel['name']}")
    print(f"    标签: {rel['tag_name']}")
    print(f"    地址: {rel['html_url']}")
    print(f"    附件:")
    for a in rel.get("assets", []):
        print(f"      {a['name']:34s} {a['size']/1024/1024:7.1f} MB")

    print()
    print("=" * 66)
    print("  ✓ Release 发布完成")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
