# v0.11.0 发布审计发现

## 仓库与发布

- 远端：`https://github.com/q825818504/bigfish-wechat.git`
- 当前分支：`main`
- 现有 tag 最高：`v0.9.7`；代码版本 `0.10.6`
- 发布脚本：`tools/make_release.py`，读取 `.secrets/github_token.txt`，
  通过 GitHub API 创建 Release 并上传 `release/BigFish-<version>-portable.zip`
  与 `-manifest.json`
- 便携包脚本：`tools/build_portable.py`，显式跳过 `data/`、`config.json`、
  `settings.json`、`plugins.json`

## 敏感面

- `data-backup/config.json` 含真实模型 Key 与语音 Key，已被 `.gitignore` 忽略。
- `config.example.json` 含真实音色 ID，需要占位符化。
- `docs/声音.md` 含同一音色 ID，需要占位符化。
- `config.json`、`data-backup/`、`dist/`、`.tmp/`、`release/`、`.secrets/`
  均不得进入提交。
- `vendor/vb-cable/` 是官方 VB-CABLE Pack45，31 文件哈希清单通过；
  `readme.txt` 保留，属于 Donationware。
- `vendor/vb-cable-pack45.zip` 是重复的下载缓存，不需要进仓库。

## 发布风险

1. GitHub Release 附件 200MB 级，上传可能超时；脚本超时 900s。
2. 若 Release 已存在同名 tag，脚本会复用；需要确认 tag 指向本次 commit。
3. 发布包必须再次解压抽查，不能只信 `build_portable.py` 的跳过逻辑。
4. `gh` CLI 不存在，不能用 `gh release create`；统一走 API 脚本。


## 结论（2026-11-08 22:1x）

- 0.11.0 便携包 343 文件 / 200.4 MB；
- zip 审计：无 `config.json`、`settings.json`、`plugins.json`、`data/`、
  `wechatauto_db/`、`keys.json`、`.sqlite3`、`.wav`；
- 示例音色 ID 为 `YOUR_VOICE_ID`，示例 Key 为空；
- 目录中的 `data-backup/`、`dist/`、`release/`、`.tmp/`、`.secrets/`
  均被忽略，不能进入提交。


## 发布结果

- Release URL: https://github.com/q825818504/bigfish-wechat/releases/tag/v0.11.0
- Tag object: `84876780560b2be1677e84c1b6849c8a6b499073`
- Tag peeled commit: `a08945ae9d03189935f7d86655bb63297141d7f6`
- Zip SHA-256: `f637edb8c8a4b087f0564cd6c3310b9a743a8e7506d738f8c4194602ab42b561`
- Zip 343 files / 210,170,682 bytes
- 远端 manifest 已包含 `wechatauto_db/`、`keys.json` 等排除项。

---

## 9 条候选问题核实（2026-11-08 23:xx，逐条读码 + 实证）

上一轮口头列的 9 条「planning 挂着没做的小事」，逐条回到代码里核了一遍。
**结论：6 条仍在，1 条已实测（只是文档过时），2 条不成立（其中 1 条是我上一轮误报）。**

### 仍存在（6）

| # | 问题 | 证据 | 危害 |
|---|---|---|---|
| 1 | `chunk_hits` 命中加权恒不生效 | `memory.py:1659` 检索用 `connect(read_only=True)` → `mode=ro`；`_bump_chunk_hits`（1827）在同一连接上 INSERT，报 `attempt to write a readonly database`，被 `except: pass` 吞（1844）。**实证脚本**：复制真实库跑两次 `search_chunks`，`chunk_hits` 仍 0 行 | 只影响排序微调（+0.15 权重永不生效），检索仍能出结果 |
| 2 | 心情页 5 个旋钮是假旋钮 | UI 有控件且能存：`moodConf` / `moodSigMood` / `moodSigDim` / `moodInjectMode` / `moodAutoJudge`（`index.html:454-483`）。Python 侧：`injectConfidence` / `sigMood` / `sigDim` / `autoJudge` 全仓只有 `vault.py` 定义与归一化，**零行为读取**；`injectMode` 只在自检 `desktop.py:7209` 被 assert。心情注入实际只读 `ctx.mood.injected_text`（`desktop.py:1035`），判定无条件跑（`_judge_mood_async`，1868） | 用户调了没反应，属于「配置能填、代码没人读」 |
| 3 | 快照面板只显示总字数 | 写盘时标题被置空：`desktop.py:2658` `_trace_sections = [(int(o), "", len(t)) ...]`；UI 只拼 `it.user_chars`（`app.js:3022`），无按段展开 | 「她这轮看到了什么」看不出哪段吃掉了预算 |
| 4 | 设置页改完不保存就切页会丢 | `go()`（`app.js:61`）直接切页，不查任何未保存状态；声音页 `loadVoice()`、模型页都重填 DOM。`dirty` 只做在监听列表（`watchDirty`）与会话作用域（`scopeDirty`） | 改一半切页，输入静默丢失 |
| 6 | `Backend.account` 一个字段存两种东西 | `desktop.py:3627` 存昵称 `nick_name`；`3707` `_peek_account()` 可能返回 `wxid_*` 目录名；`3881` 存 `reader._db.account`。该字段又被 `get_status` 当账号展示（3740），并被 `_scope_ctx` 当 `bot_id` 兜底（3973） | 昵称态下 `bot_id` 与 `config.accounts` 的键（目录名）不一致，作用域可能落回全局层 |
| 7 | 「要不要发」表情仍是掷骰子 | `_pick_sticker`（`desktop.py:935-946`）只有 `random.random() > rate`；模型标记不参与。语音已有 `[[voice]]`/`[[text]]` 手柄，表情没有 | 模型想发/不想发都表达不了；用户明确要时已跳过概率（925） |

### 已实测，文档过时（1）—— 我上一轮判断错了

| # | 原说法 | 实际情况 |
|---|---|---|
| 8 | 语音气泡真实窗口链路未实测 | **真机已经实操过，并且修了 3 个只有真机才会暴露的 bug**（`CHANGELOG.md` 0.11.0「修复」段）：<br>① 语音成功后 `name 'hist' is not defined`（`_deliver` 与 `handle` 是兄弟闭包）；<br>② 语音与文字前台行为不一致（`wake()` 只保证可见，气泡还要前台）→ 改成 `force_show_wechat` + `open_chat`；<br>③ 主动置前 + 气泡失败分级回退。<br>这三条不可能靠离线自检发现。<br>**剩下的只是文档没跟上**：`docs/声音.md:174` 还写着「当前机器没装 VB-CABLE」，`CHANGELOG.md:39` 还写着「仍需在装有 VB-CABLE 的机器上实测」。本机驱动已装（`CABLE In 16 Ch` / `CABLE Output`，状态 OK），`dist/BigFish/config.json` 已是 `send_as=bubble` |

### 不成立（2）

| # | 原说法 | 实际情况 |
|---|---|---|
| 5 | `set_data_root` 运行中只改指向、不重启引擎 | **路径是每轮现查的**：`resolve_data_root()` → `read_settings()` 每次读盘，无缓存（`vault.py:98`）；引擎内 40+ 处都是 `V.memory_db_path()` 现查（`desktop.py:466/490/...`）。改完下一轮就落到新目录。**残留一点**：已加载进内存的 `soul` / `life` / 心情对象不会重载，要重启才彻底 |
| 9 | `tools/setup-git-token.py` 不存在 | **我上一轮误报**。文件在：`tools/setup-git-token.py`（另有 `git_token_helper.py`）。`task_plan.md:5` 与 `AGENTS.md:15` 的引用是对的 |

### 核实方法

- 读码为主，第 1 条另有实证脚本 `D:\大脑\.tmp\probe_chunk_hits.py`（复制真实库跑检索，再数 `chunk_hits`）。
- 第 8 条用 `Get-PnpDevice -Class AudioEndpoint` + `Win32_SoundDevice` 查驱动，用 `dist/BigFish/wechatauto_logs` 查真实运行痕迹。
- 全部结论以当前 HEAD `24937ed` 为准。
