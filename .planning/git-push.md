# 推送代码：为什么不弹窗了

> 2026-11-08 配好。换机器或新 clone 之后要重做一次，见最后一节。
>
> **2026-11-11 更新：现在优先用 `python tools/push.py`，不再依赖凭证管理器。**

## 一个更省事的路径（2026-11-11 之后）

```bash
python tools/push.py            # 推到当前分支
python tools/push.py --dry-run  # 只看要推什么
```

它把令牌直接写进 `http.extraHeader Authorization: Basic <base64>`，
**完全不碰 credential.helper**。

### 为什么加了这条路

2026-11-11 推 0.13.0 时踩了一次：

```
$ git push origin HEAD
fatal: unable to access '...': Failed to connect to github.com:443
       after 21103 ms: Could not connect to server
```

看起来是网络断了。于是去查：`api.github.com` 200、`baidu.com` 200、
`ssh git@github.com` 能握手（`Permission denied (publickey)`）——
全都不像断网。

**真相是凭证管理器没把令牌交出去**，git 一直重试到超时，
把"认证卡住"报成了"连不上"。同一次里改用：

```bash
git -c http.extraHeader="Authorization: Basic $(echo -n 'x-access-token:<token>' | base64 -w0)" \
    push https://github.com/... HEAD:main
```

一次就通。

**教训**：`Failed to connect ... 443` 这个错误在 Windows + GCM 环境下
**不能直接当成网络问题**。先试 `tools/push.py`，通了就是凭证的事。

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

---

## 2026-11-11 补：两种失败要分清楚

同一天里连着撞了两种，症状都是"推不上去"，处置完全不同：

| 报错 | 真因 | 处置 |
|---|---|---|
| `![remote rejected] ... refusing to allow a Personal Access Token to create or update workflow .github/workflows/... without workflow scope` | 令牌有 `repo` 没有 `workflow`。GitHub 不允许这类 token 动 `.github/workflows/` 下的文件 | 去 github.com/settings/tokens 给令牌勾 `workflow`，更新 `D:\大脑\github.txt`。**这一条和网络无关**，别去查代理 |
| `fatal: unable to access ...: Failed to connect to github.com:443 after 21078 ms` | **真的连不上**。实测 `api.github.com:443` 通（0.1s）而 `github.com:443` 超时（8s）—— 典型域名阻断，不是网络全断 | 走系统代理。本机 `127.0.0.1:7899`（注册表 `ProxyServer` 里那个），`HTTPS_PROXY=http://127.0.0.1:7899 python tools/push.py` |

**怎么快速分辨**：先跑一个 `api.github.com` 的请求。

- 通了 + push 报 workflow → **权限问题**，查令牌 scope；
- 通了 + push 报 connect → **域名阻断**，挂代理；
- 都不通 → 网络真断了。

`tools/push.py` 自己会分开报这两种（"连不上 GitHub" vs 打印 remote rejected），
所以**先看它的诊断，再动手**，不要一上来就怀疑凭证 —— 那是 11-11 早些时候
踩过的坑（凭证问题也会报成 connect error，但那次是凭证管理器卡住，而这次
把令牌放进请求头之后仍然 connect failed，才是真的网络）。

### 现状备忘

2026-11-11 22:59 推 `72d7bf8` 到 `origin/main` 成功，走的是
`HTTPS_PROXY=http://127.0.0.1:7899`。

`.github/workflows/ci.yml` **还在工作区、没进仓库** —— 等令牌补上
`workflow` 权限后再单独提交这一个文件。`tools/check.py` 已经进了仓库，
手动跑完全可用。
