# 给 agent 的说明

> 这个文件是给**下一个接手这个项目的 agent** 看的。
> 人看的话，直接从 `README.md` 开始。

---

## 推送代码（先看这条）

**这个仓库的 git 凭证已经配好了，`git push` / 建 Release 不会弹窗。**

如果你接手时它又弹窗了（命令卡住、几十秒后超时），跑一次：

```bash
python tools/setup-git-token.py
```

令牌在 **`D:\大脑\github.txt`**（不在仓库里，也不会进仓库）。

⚠ **不要用 `git credential fill` 去取令牌** —— 它在非交互环境里会挂住等
交互。需要 token 调 GitHub API 时，直接读那个文件的第一行。
细节和排错步骤见 `.planning/git-push.md`。

---

## 这个项目是什么

微信桌面的「AI 生命」：读微信数据库拿消息，用 UIA 把回复打进输入框。
不是机器人框架，是**让一个角色在微信上过日子** —— 她会累会睡、会主动开口、
会看眼色不接话。

---

## 文档在哪

| 文件 | 内容 |
|---|---|
| `.planning/task_plan.md` | **现在做到哪、下一步做什么**（接手先看这个） |
| `.planning/progress.md` | 版本历史、每版踩过的坑 |
| `.planning/decisions.md` | 为什么这么做（D-xx 编号） |
| `.planning/architecture.md` | 模块职责、分层理由 |
| `.planning/git-push.md` | 推送凭证怎么配的 |
| `CHANGELOG.md` | 每个版本改了什么（**用户视角**） |
| `docs/` | 功能说明（生活状态 / 内在状态 / 日程与复盘 / 日志 / 主动与关系 / 接话与补一句 / 部署验证） |

---

## 改代码的习惯

- **注释写"为什么"，不写"做了什么"**。这个项目里最贵的注释是
  「以前这里是 X，因为 Y 会出问题，所以改成 Z」。
- **拿不准就闭嘴**。宁可少说一句，不要说错。
- **事实要有来源**（D-36）。她说的每件事必须能追到 `life.json` / `facts` /
  原话，不许即兴编。
- **改完跑自检**：

```bash
python tools/selftest.py                        # 源码全套（23 项）
dist/BigFish/BigFish.exe --selftest             # 打包版（要管理员）
```

- **升版本号**要三处一致：`bigfish/__init__.py` 的 `__version__`、
  `CHANGELOG.md` 顶部、提交信息。

---

## 提交习惯

- 提交信息用中文，说清"为什么"，不只说"改了什么"
- 一次提交一件事；跨版本的攒批发布另算
- 打包：`python tools/deploy.py`（会自动备份数据、覆盖程序、跑自检）
- **数据目录 `D:\大肥鱼` 是用户真实数据**，任何脚本都不许写它
