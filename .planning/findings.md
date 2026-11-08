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
