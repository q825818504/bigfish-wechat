#!/usr/bin/env python3
"""一键把这个仓库的 git 凭证来源换成令牌文件（不弹窗）。

跑法：
    python tools/setup-git-token.py

它做三件事：

    1. 确认能读到令牌（`D:\\大脑\\github.txt` 或环境变量）
    2. 清空本仓库的 credential.helper 列表（**不动全局配置**）
    3. 把它指向 tools/git_token_helper.py

为什么必须先清空
────────────────
git 的 `credential.helper` 是**列表**，不是单值。直接 `--add` 一个
自己的 helper，前面那个 credential-manager 还在列表里，git 会先问它 ——
于是照样弹窗。设成空串是官方文档里唯一能"重置列表"的写法：

    If credential.helper is set to the empty string, it resets the
    helper list.

为什么只改 --local
──────────────────
`--global` 会影响这台机器上所有仓库。这个脚本的职责只是"大肥鱼这个仓库
能自动推"，别的项目该用 GCM 还用它。

配置写进 `.git/config`，那个文件**永远不会被提交**，
所以令牌不会跟着代码跑到 GitHub 上去。
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HELPER = os.path.join(HERE, "git_token_helper.py")


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, text=True,
                          capture_output=True, check=False)


def main() -> int:
    if not os.path.isfile(HELPER):
        print(f"找不到 {HELPER}")
        return 1

    # 1. 令牌读得到吗
    r = subprocess.run([sys.executable, HELPER, "get"], cwd=ROOT,
                       input="protocol=https\nhost=github.com\n\n",
                       text=True, capture_output=True)
    if "password=" not in (r.stdout or ""):
        print("读不到令牌。请把 token 放到下面任意一个位置（只放一行）：")
        print(r"    D:\大脑\github.txt")
        print("    或设置环境变量 BIGFISH_GITHUB_TOKEN")
        return 1
    print("令牌：读到了")

    # 2. 清空列表（官方文档：设成空串会重置 helper 列表）
    git("config", "--local", "credential.helper", "")
    # 3. 加自己的
    git("config", "--local", "--add", "credential.helper",
        f'!"{sys.executable}" "{HELPER}"')

    # 4. 只对 github.com 生效的话再加一条 URL 级的更精确；
    #    这里保持简单：本仓库的 remote 就是 github，够了。

    # 回读确认
    r = git("config", "--local", "--get-all", "credential.helper")
    print("credential.helper 现在是：")
    for line in (r.stdout or "").strip().splitlines():
        print("   ", line or "（空 = 重置标记）")

    # 5. 试一次只读的认证（不推东西）
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    r = subprocess.run(["git", "ls-remote", "--heads", "origin"],
                       cwd=ROOT, text=True, capture_output=True, env=env,
                       timeout=60)
    if r.returncode == 0:
        print("\n认证通过（ls-remote 拿到了远端分支）")
    else:
        print("\n认证失败：", (r.stderr or "")[:300])
        return 1

    print("\n好了。之后 push / 建 release 都会自动用这个令牌，不弹窗。")
    print("换机器或新 clone 的话，再跑一次这个脚本就行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
