# 推送代码：为什么不弹窗了

> 2026-11-08 配好。换机器或新 clone 之后要重做一次，见最后一节。

## 原来的问题

git 默认走 **credential-manager（GCM）**。它是个带窗口的程序：需要凭证时
弹一个框让人选账号。

**在有屏幕、有人看着的时候这没问题。** 但 agent 跑命令的 shell 是**非交互**
的 —— 弹窗弹不出来，或者说弹出来了没人点，于是 `git push` 就挂在那儿等，
等到超时被杀。

表现：

```
$ git push origin HEAD
（卡住，90 秒后超时）
$ git push origin HEAD          ← 再试一次，可能就过了
（成功）
```

推 tag 的时候卡了四次，第五次才进去。原因不是网络，是 GCM 的窗口在
等一个永远不会来的点击。

## 现在的做法

把凭证来源从 GCM 换成**一个普通文本文件**。git 读它，不弹窗、不等待。

```
D:\大脑\github.txt          ← 令牌就放这儿，只有这一处
        ↑
tools/git_token_helper.py   ← git 通过它读（只读、不缓存、不打印）
        ↑
.git/config                 ← 只配这个仓库，动不了全局
```

### 配置长什么样

```ini
[credential]
	helper =
	helper = !"C:\...\python.exe" "D:\大脑\bigfish_repo\bigfish-wechat\tools\git_token_helper.py"
```

**第一行那个空值不能删。** git 的 `credential.helper` 是**列表**，不是单值；
直接 `--add` 自己的 helper，前面那个 GCM 还在列表里，git 会先问它 ——
照样弹窗。设成空串是官方文档里唯一能"重置列表"的写法：

> If `credential.helper` is set to the empty string, it resets the helper list.

### 为什么只改 `--local`

`--global` 会影响这台机器上所有仓库。这个配置的职责只有"大肥鱼能自动推"，
别的项目该用 GCM 还用它。

`.git/config` **永远不会被提交**，所以令牌不会跟着代码跑到 GitHub 上。

## 令牌在哪儿

按顺序找，第一个能读到的就用：

| # | 位置 |
|---|---|
| 1 | 环境变量 `BIGFISH_GITHUB_TOKEN` |
| 2 | `D:\大脑\github.txt` ← **现在用的这个** |
| 3 | `<仓库根>/../github.txt` |
| 4 | `<仓库根>/.github-token`（已在 `.gitignore`） |
| 5 | `~/.bigfish-github-token` |

只取第一行、去掉首尾空白。文件里可以只有一行 `ghp_xxxx`。

## 要 token 调 API 的时候

建 release、改 issue 之类需要 token。**不要用 `git credential fill` 去取** ——
它在非交互环境里会挂住（就是这次卡了四次的原因）。直接读文件：

```python
tk = open(r"D:\大脑\github.txt", encoding="utf-8").read().splitlines()[0].strip()
```

然后 `Authorization: token {tk}`。

## 换机器 / 新 clone

跑一次：

```bash
python tools/setup-git-token.py
```

它会：读令牌 → 清空本仓库的 helper 列表 → 指向 `git_token_helper.py` →
用 `git ls-remote` 验证一次。

**不会**改任何全局配置，**不会**把令牌写进仓库。

## 如果再弹窗了

按这个顺序查：

```bash
# 1. 本地配的 helper 被别的配置盖住了？（应该只有一行，加一个空行）
git config --local --get-all credential.helper

# 2. helper 自己还能读到令牌吗？
echo "protocol=https
host=github.com
" | python tools/git_token_helper.py get

# 3. 令牌还有效吗？
python tools/setup-git-token.py
```

第 2 步没有任何输出 = 读不到令牌文件，去看上面那张表。

## 一个反面教材

这次配之前，我试过用 `git credential fill` 从 GCM 里取令牌给 API 用 ——
在一次 shell 里成功过（拿到 40 字符），再试就挂住。

**教训**：在有窗口的凭证管理器面前，任何"读一次存起来"的思路都不可靠，
因为第二次调用时它可能改成要交互。要么一开始就走文件，要么别碰。
