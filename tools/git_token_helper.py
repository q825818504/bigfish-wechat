#!/usr/bin/env python3
"""git credential helper —— 让 git 用 D:\\大脑\\github.txt 里的令牌，不再弹窗。

## 为什么需要它

git 默认走 credential-manager（GCM），在**非交互环境**（agent 的 shell、
CI、后台任务）里它会弹一个窗口让人选账号。没人点，命令就挂在那儿等超时 ——
表现出来就是"push 卡住"、"推了四次才进去一次"。

这个脚本把凭证来源换成一份普通文本文件，git 直接读，不弹窗、不等待。

## git 怎么调它

    输入（stdin）   protocol=https
                    host=github.com
                    （空行结束）

    输出（stdout）  username=xxx
                    password=xxx
                    （空行结束）

第一个参数是动作（get / store / erase）。只有 get 需要输出，
store/erase 什么都不用做 —— 令牌在文件里，不由 git 管。

## 令牌从哪儿读

按顺序找，第一个能读到的就用：

    1. 环境变量 BIGFISH_GITHUB_TOKEN
    2. D:\\大脑\\github.txt          ← 现在用的这个
    3. <仓库根>/../github.txt
    4. <仓库根>/.github-token         （本地文件，已在 .gitignore）
    5. ~/.bigfish-github-token

只取第一行、去掉首尾空白。空文件或读不到就什么都不输出，
git 会退回到下一个 helper（如果还有的话）。

## 安全

· 令牌**只在**上面那几个文件里，本脚本不存、不缓存、不打印。
· 这个文件可以进仓库 —— 它不含任何秘密，只是"去哪儿找"。
· 输出完就退出，不落任何临时文件。
"""
from __future__ import annotations

import os
import sys

CANDIDATES = (
    os.path.join(os.path.expanduser("~"), ".bigfish-github-token"),
    r"D:\大脑\github.txt",
)


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_token() -> str:
    tk = (os.environ.get("BIGFISH_GITHUB_TOKEN") or "").strip()
    if tk:
        return tk.splitlines()[0].strip()

    paths = list(CANDIDATES) + [
        os.path.abspath(os.path.join(_repo_root(), "..", "github.txt")),
        os.path.join(_repo_root(), ".github-token"),
    ]
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                line = (f.readline() or "").strip()
            if line:
                return line
        except Exception:
            continue
    return ""


def main() -> int:
    action = (sys.argv[1] if len(sys.argv) > 1 else "get").strip().lower()
    if action != "get":
        # store / erase：令牌由用户自己管，不由 git 写。
        # 静默成功，别让 git 报错。
        return 0

    # 把 stdin 读完（不读的话某些 git 版本会等你）
    try:
        sys.stdin.read()
    except Exception:
        pass

    tk = find_token()
    if not tk:
        # 什么都不输出 = "我没有凭证"，git 会去问下一个 helper。
        return 0

    sys.stdout.write("username=q825818504\n")
    sys.stdout.write(f"password={tk}\n")
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
