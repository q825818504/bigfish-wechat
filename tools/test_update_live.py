"""用真实 GitHub Release 测自动更新。

之前的 test_updater.py 用的是本地模拟服务，验证的是逻辑。
这个脚本打真实的 GitHub API，验证的是「线上那条链路」。

检查：
  1. 能否查到 v0.1.0
  2. 版本比较是否正确（0.0.9 应该看到更新，0.1.0 不应该）
  3. 能否从 Release 里挑出正确的附件
  4. 能否真的下载（只下 manifest，不下 160MB 的包）
  5. SHA256 是否和资产 digest 一致
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bigfish import updater  # noqa: E402

REPO = "q825818504/bigfish-wechat"
HR = "=" * 66
fails = []

# 清掉代理
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)


def ok(label, cond, extra=""):
    print(f"  [{'OK  ' if cond else 'FAIL'}] {label}  {extra}")
    if not cond:
        fails.append(label)


def main() -> int:
    print(HR)
    print("  真实 GitHub Release 自动更新测试")
    print(HR)

    cache = os.path.join(ROOT, ".tmp", "release_test")
    os.makedirs(cache, exist_ok=True)

    # ── 1. 查最新版本 ──
    print("\n[1] 查询线上 Release")
    t0 = time.time()
    try:
        info = updater.check_latest(REPO, "0.0.9", cache)
    except Exception as exc:
        ok("查询成功", False, str(exc))
        return 1
    print(f"    耗时 {time.time()-t0:.1f}s")

    ok("查询成功", not info.get("error"), info.get("error", ""))
    ok("识别到有新版", info.get("has_update") is True, str(info.get("has_update")))
    ok("版本号是 0.1.0", info.get("latest") == "0.1.0",
       f"-> {info.get('latest')}")

    assets = info.get("assets") or []
    ok("附件有 3 个", len(assets) == 3, f"-> {len(assets)}")
    for a in assets:
        print(f"      {a['name']:34s} {a['size']/1024/1024:7.1f} MB")

    # ── 2. 版本比较 ──
    print("\n[2] 版本比较（当前已是 0.1.0）")
    info2 = updater.check_latest(REPO, "0.1.0", cache)
    ok("同版本不报更新", info2.get("has_update") is False)

    # ── 3. 挑附件 ──
    print("\n[3] 选择要下载的附件")
    up = updater.Updater(REPO, "0.0.9", cache)
    picked = up.pick_asset(info)
    ok("挑到执行文件", picked is not None, picked["name"] if picked else "无")
    if picked:
        ok("优先选 exe 或 zip",
           picked["name"].lower().endswith((".exe", ".zip")),
           picked["name"])

    # ── 4. 真的下载（只下最小的 manifest）──
    print("\n[4] 下载最小的附件验证链路")
    manifest = next((a for a in assets if a["name"].endswith(".json")), None)
    if not manifest:
        ok("找到 manifest", False)
        return 1

    dest = os.path.join(cache, manifest["name"])
    if os.path.isfile(dest):
        os.unlink(dest)

    t0 = time.time()
    try:
        updater.download(manifest["url"], dest, manifest.get("sha256") or "")
        ok("下载成功", True, f"{time.time()-t0:.1f}s")
    except Exception as exc:
        ok("下载成功", False, str(exc))
        return 1

    # ── 5. 内容校验 ──
    print("\n[5] 内容校验")
    ok("文件大小一致",
       os.path.getsize(dest) == manifest["size"],
       f"{os.path.getsize(dest)} vs {manifest['size']}")

    h = hashlib.sha256()
    with open(dest, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    actual = h.hexdigest()
    ok("SHA256 一致", actual == manifest.get("sha256"),
       f"{actual[:16]}...")

    # manifest 内容能解析
    try:
        with open(dest, "r", encoding="utf-8") as f:
            mf = json.load(f)
        ok("manifest 可解析", True, f"版本 {mf.get('version')}, "
                                f"{len(mf.get('files', {}))} 个文件")
    except Exception as exc:
        ok("manifest 可解析", False, str(exc))

    print()
    print(HR)
    if fails:
        print(f"  ✗ {len(fails)} 项失败: {fails}")
    else:
        print("  ✓ 线上自动更新链路全部正常")
    print(HR)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
