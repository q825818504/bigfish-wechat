#!/usr/bin/env python3
"""把当前分支推到 GitHub —— 直接把令牌放进请求头，不依赖凭证管理器。

跑法：
    python tools/push.py                # 推到当前分支的同名远端分支
    python tools/push.py --branch main  # 指定远端分支
    python tools/push.py --dry-run      # 只看要推什么，不真推

为什么不用 `git push`
─────────────────────
2026-11-11 实测：`git push origin HEAD` 走了系统凭证管理器（GCM），
它没把令牌交出去、一直重试到 21 秒超时，报出来是
「Failed to connect to github.com:443」—— 看起来像网络断了，
实际是凭证流程卡住。同一次里直接用 `http.extraHeader` 塞令牌，
一次就通。

所以这个脚本永远走那条路：**不碰 credential.helper、不碰 GCM**。

令牌从哪来
──────────
1. 环境变量 `BIGFISH_GITHUB_TOKEN`
2. `D:\\大脑\\github.txt` 的第一行（这个文件不在仓库里，也不会进仓库）

令牌**不会**出现在输出里，任何失败信息都先把它抹掉再打印。
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys

#: 令牌文件。仓库外，绝不进版本库。
TOKEN_FILE = r"D:\大脑\github.txt"

#: 远端。写死是故意的：这个脚本只服务这一个仓库，
#: 从 `git remote` 里读反而多一层"万一用户改了"的意外。
REMOTE = "https://github.com/q825818504/bigfish-wechat.git"


def _token() -> str:
    env = os.environ.get("BIGFISH_GITHUB_TOKEN", "").strip()
    if env:
        return env
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            return f.readline().strip()
    except Exception:
        return ""


def _scrub(text: str, token: str) -> str:
    """把令牌从任何输出里抹掉。日志会被人贴到 issue 里。"""
    if token and len(token) > 8:
        text = text.replace(token, "***")
    return text


def git(*args: str, token: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace")


def main() -> int:
    dry = "--dry-run" in sys.argv
    branch = ""
    for i, a in enumerate(sys.argv):
        if a == "--branch" and i + 1 < len(sys.argv):
            branch = sys.argv[i + 1]

    token = _token()
    if not token:
        print("找不到令牌。放这两处之一：")
        print(f"  1. 环境变量 BIGFISH_GITHUB_TOKEN")
        print(f"  2. {TOKEN_FILE} 的第一行")
        return 2

    # 当前分支
    cur = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if not cur or cur == "HEAD":
        print("当前不在任何分支上（detached HEAD），先 checkout 一个分支。")
        return 2
    target = branch or cur

    # 待推送的提交。
    # ⚠ 要分清三种情况：① 无差异 ② 无法比较（远端没这分支）③ 有待推内容。
    #   以前把 ① 和 ② 混在一起，已同步时也会报"远端还没有 main"。
    rng = f"origin/{target}..HEAD"
    probe = git("log", "--oneline", rng)
    if probe.returncode != 0:
        pending = git("log", "--oneline", "-5").stdout.strip()
        print(f"远端还没有 {target}（或本地没 fetch 到），将推送最近这些：")
    else:
        pending = probe.stdout.strip()
        if not pending:
            print(f"已经是最新：origin/{target} 与本地一致，没有待推送的提交。")
            return 0
        print(f"待推送到 origin/{target}：")

    for line in pending.split("\n"):
        print(f"  {line}")

    if dry:
        print("\n（--dry-run，没有真推）")
        return 0

    # 带上令牌推。用 extraHeader 而不是 URL 里拼，避免令牌出现在 ps/日志
    auth = "x-access-token:" + token
    b64 = base64.b64encode(auth.encode()).decode()
    res = subprocess.run(
        ["git", "-c", f"http.extraHeader=Authorization: Basic {b64}",
         "push", REMOTE, f"HEAD:{target}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")

    out = _scrub((res.stdout or "") + (res.stderr or ""), token)
    print()
    for line in out.strip().split("\n"):
        if line.strip():
            print("  " + line.strip())

    if res.returncode == 0:
        print(f"\n✓ 已推送到 origin/{target}")
        return 0

    low = out.lower()
    if "401" in out or "authentication" in low or "permission" in low:
        print("\n✗ 认证失败：令牌可能过期或被吊销。")
        print("  重新生成一个 classic token（勾 repo 权限），写回令牌文件。")
    elif "could not resolve" in low or "timed out" in low or "connect" in low:
        print("\n✗ 连不上 GitHub。这次是真的网络问题：")
        print("  开代理，或设 BIGFISH_GITHUB_TOKEN 后重试。")
    else:
        print("\n✗ 推送失败，看上面的输出。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
