"""用 GitHub API 推送（`github.com:443` 被阻断时的备用通道）。

跑法：
    python tools/push_api.py              # 推送 origin/main..HEAD 的全部提交
    python tools/push_api.py --dry-run    # 只列出要推什么

═══════════════════════════════════════════════════════════════════════
为什么需要它
═══════════════════════════════════════════════════════════════════════

2026-11-12 起，这台机器上 `github.com:443` 长期不通（`curl` 返回 000），
但 **`api.github.com` 一直是 200**。`tools/push.py` 走的是 git 协议
（github.com），于是每次报 "Failed to connect ... 21095ms"，
看起来像网络全断，其实只是那一个域名被挡。

这里用 Git Data API 把本地 commit **逐字节重建**：

    blob（内容 sha 与本地天然一致）
      → tree（base_tree = 父 commit 的 tree）
      → commit（原 author / committer / message / 时间戳）
      → PATCH refs/heads/main

只要元数据全对，重建出的 commit sha 与本地**完全相同** ——
所以不会制造"内容一样、历史不同"的分叉。任一环节对不上就停下，
绝不 force。

═══════════════════════════════════════════════════════════════════════
令牌
═══════════════════════════════════════════════════════════════════════

优先级：`BIGFISH_GITHUB_TOKEN` → `D:\\大脑\\github.txt` → `.secrets/github_token.txt`
（前三者都只在本地，不进版本库。令牌文件里允许写成「github：ghp_xxx」，
会自动用正则取出 `ghp_` / `github_pat_` 那一段。）
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "q825818504/bigfish-wechat"
DRY = "--dry-run" in sys.argv or "-n" in sys.argv

TOKEN_SOURCES = (
    os.environ.get("BIGFISH_GITHUB_TOKEN", ""),
    # D:\大脑\github.txt —— 仓库在 D:\大脑\bigfish_repo\bigfish-wechat，
    # 所以"仓库外两级"才是那个位置
    os.path.join(os.path.dirname(os.path.dirname(ROOT)), "github.txt"),
    os.path.join(ROOT, ".secrets", "github_token.txt"),
)


def token() -> str:
    for src in TOKEN_SOURCES:
        if not src:
            continue
        try:
            if os.path.isfile(src):
                raw = open(src, encoding="utf-8-sig", errors="replace").read()
            else:
                raw = src
            m = re.search(r"(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})", raw)
            if m:
                return m.group(1)
        except Exception:
            continue
    raise RuntimeError("找不到令牌：设 BIGFISH_GITHUB_TOKEN，"
                       "或写到 D:\\大脑\\github.txt 第一行")


def git(*args, binary: bool = False):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[:300])
    return r.stdout if binary else r.stdout.decode("utf-8")


def api(path: str, method: str = "GET", body=None, tok: str = ""):
    url = "https://api.github.com" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + tok,
        "Accept": "application/vnd.github+json",
        "User-Agent": "bigfish-push-api",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from None


def _who(sig: str) -> dict:
    """`Alime <alime@local> 1762875000 +0800` → API 的 author 结构。"""
    name_email, ts, tz = sig.rsplit(" ", 2)
    name, email = name_email.rsplit(" <", 1)
    tz_min = (int(tz[:3]) * 60 + int(tz[3:])) if len(tz) == 5 else 0
    dt = datetime.fromtimestamp(int(ts),
                                tz=timezone(timedelta(minutes=tz_min)))
    return {"name": name, "email": email.rstrip(">"), "date": dt.isoformat()}


def push_one(sha: str, tok: str) -> str:
    raw = git("cat-file", "commit", sha)
    head, message = raw.split("\n\n", 1)
    tree = ""
    parents = []
    author_sig = committer_sig = ""
    for line in head.splitlines():
        if line.startswith("tree "):
            tree = line[5:]
        elif line.startswith("parent "):
            parents.append(line[7:])
        elif line.startswith("author "):
            author_sig = line[7:]
        elif line.startswith("committer "):
            committer_sig = line[10:]

    print(f"\n=== {sha[:10]} {message.splitlines()[0][:56]}")
    changes = git("diff-tree", "-r", "--no-commit-id", "--name-status",
                  sha).strip().splitlines()
    entries = []
    for row in changes:
        parts = row.split("\t")
        status, path = parts[0], parts[-1]
        if status.startswith("D"):
            entries.append({"path": path, "mode": "100644", "type": "blob",
                            "sha": None})
            print(f"  D  {path}")
            continue
        content = git("show", f"{sha}:{path}", binary=True)
        if DRY:
            print(f"  {status}  {path}  ({len(content)} B)")
            entries.append({"path": path, "mode": "100644", "type": "blob",
                            "sha": "0" * 40})
            continue
        blob = api(f"/repos/{REPO}/git/blobs", "POST",
                   {"content": base64.b64encode(content).decode(),
                    "encoding": "base64"}, tok)
        entries.append({"path": path, "mode": "100644", "type": "blob",
                        "sha": blob["sha"]})
        print(f"  {status}  {path}")

    if DRY:
        return sha

    parent_tree = api(f"/repos/{REPO}/git/commits/{parents[0]}", tok=tok)["tree"]["sha"]
    new_tree = api(f"/repos/{REPO}/git/trees", "POST",
                   {"base_tree": parent_tree, "tree": entries}, tok)
    if new_tree["sha"] != tree:
        raise RuntimeError(f"tree 不一致（{new_tree['sha'][:10]} != {tree[:10]}），"
                           "停下，不制造分叉")
    commit = api(f"/repos/{REPO}/git/commits", "POST", {
        "message": message, "tree": new_tree["sha"], "parents": parents,
        "author": _who(author_sig), "committer": _who(committer_sig),
    }, tok)
    if commit["sha"] != sha:
        raise RuntimeError(f"commit sha 不一致（{commit['sha'][:10]} != {sha[:10]}）")
    api(f"/repos/{REPO}/git/refs/heads/main", "PATCH",
        {"sha": commit["sha"], "force": False}, tok)
    print("  ✓ 已推送")
    return commit["sha"]


def main() -> int:
    tok = "" if DRY else token()
    branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch != "main":
        print(f"[!] 当前分支是 {branch}，这个工具只推 main")
        return 1
    pending = [x for x in git("rev-list", "--reverse",
                              "origin/main..HEAD").split() if x]
    if not pending:
        print("没有待推送的提交（origin/main 已是最新）")
        return 0
    print(f"待推送 {len(pending)} 个提交：")
    for c in pending:
        print("  " + git("log", "-1", "--format=%h %s", c).strip())
    if DRY:
        print("\n（--dry-run，没有真推）")
        return 0

    for c in pending:
        push_one(c, tok)

    remote = api(f"/repos/{REPO}/git/ref/heads/main", tok=tok)["object"]["sha"]
    local = git("rev-parse", "HEAD").strip()
    print(f"\n远端 main: {remote[:12]}")
    print(f"本地 HEAD: {local[:12]}")
    ok = remote == local
    print("结果：" + ("一致 ✅" if ok else "不一致 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
