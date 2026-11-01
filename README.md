# 大肥鱼 × 微信

把 **大肥鱼**（DSH 灵魂人格）接进微信：读消息、想回复、发出去。

**用你自己的微信号，不是 Bot。**

---

## 当前状态

**可用。** 微信收发链路在打包版 exe 里完整跑通，真实双号对话验证过。

```
微信客户端            大肥鱼
┌─────────┐      ┌──────────────────┐
│ SQLCipher│──读──▶│ WeChatReader     │
│  数据库   │      └────────┬─────────┘
└─────────┘               │
                  ┌───────▼────────┐
                  │ MessageWatcher │  轮询发现新消息
                  └───────┬────────┘
                          │
        ┌─────────────────┼─────────────────┐
        │                 │                 │
  ┌─────▼─────┐    ┌──────▼──────┐   ┌─────▼──────┐
  │ GroupGate │    │  BigFish    │   │ UiaSender  │
  │ 群聊判定   │    │  组装+模型   │   │ 界面发送    │
  └───────────┘    └──────┬──────┘   └─────┬──────┘
                          │                │
                  ┌───────▼────────┐       │
                  │ bridge_client  │       │
                  │ 灵魂/情绪/记忆  │       │
                  └───────┬────────┘       │
                          │                │
                  ┌───────▼────────┐  ┌────▼─────┐
                  │  bridge.mjs    │  │ 控件树    │
                  │ import 原版插件 │  │ mmui::*  │
                  └────────────────┘  └──────────┘
```

---

## 前置条件

| 项 | 要求 | 为什么 |
|---|---|---|
| 微信版本 | **4.1.15.53** | UIA 热激活的 gate 地址要精确匹配 |
| 权限 | **管理员** | 热激活要往微信进程写 1 个字节 |
| 桌面 | **解锁且微信可见** | UIA 是界面操作 |
| 模型 | Magpie 网关（DSH 启动时拉起） | 或任何 OpenAI 兼容接口 |

**4.1.10.27 不支持** —— 不在上游版本表里，动态扫描和静态分析都失败过。

---

## 安装

### 方式 A：安装包

```
BigFish-0.1.0-installer/
  install.bat      双击安装
  setup.exe        安装器
  payload.zip      程序本体
  _internal/       安装器依赖
```

装到 `%LOCALAPPDATA%\Programs\BigFish`，建开始菜单和桌面快捷方式，
写注册表卸载项。

### 方式 B：便携版

解压 `BigFish-0.1.0-portable.zip` 到任意目录，双击 `BigFish.exe`。

### 方式 C：源码

```cmd
python -m venv .venv
.venv\Scripts\python.exe -m pip install --no-cache-dir setuptools wheel
.venv\Scripts\python.exe -m pip install --no-cache-dir --no-build-isolation --no-deps -e vendor/wechatauto-replica
.venv\Scripts\python.exe -m pip install --no-cache-dir ^
  uiautomation pywin32 pyperclip Pillow psutil colorama ^
  cryptography zstandard PySide6-Essentials PySide6-Addons PySide6-WebEngine numpy capstone
```

---

## 使用

### 1. 配置

复制 `config.example.json` 为 `config.json`：

```json
{
  "endpoint": "http://127.0.0.1:3425/v1/chat/completions",
  "model": "workbuddy-ai/deepseek-v4.1-flash",
  "api_key": "",

  "auto_reply": {
    "chats": ["老爸"],
    "allow_self": false,
    "interval": 1.2,
    "max_reply_chars": 300,
    "quiet_hours": [],
    "group_reply": true,
    "group_allow_all": []
  },

  "update": {
    "repo": "q825818504/bigfish-wechat",
    "enabled": true,
    "current": "0.1.0"
  }
}
```

`chats` 填要监听的会话名（私聊填昵称，群聊填群名）。

### 2. 启动

**右键 `BigFish.exe` → 以管理员身份运行**。界面打开后点右上角电源按钮。

启动后日志页会显示：

```
正在加载灵魂 ...
灵魂：大肥鱼（rev 6）
账号：四条丶
UIA 已就绪
监听：老爸 -> wxid_xxx
开始监听 1 个会话
```

### 3. 自检

```cmd
BigFish.exe --selftest      检查 7 项
BigFish.exe --live-send     真的发一条到文件传输助手
BigFish.exe --version       版本号
```

---

## 界面

八个页面，**灵魂 / 心情 / 记忆 三个都能写**：

| 页面 | 做什么 |
|---|---|
| 概览 | 运行状态、环境自检、收发达标、最近对话 |
| 会话 | 微信会话列表，勾选要监听的 |
| **灵魂** | 改名字、称呼、昵称、我是谁、珍视条目（可锁）、性格滑杆、自知。带注入预览 |
| **心情** | 环形进度、七维情绪、注入原文；改基线/下限/半衰期/日额度/可信度门槛/注入方式 |
| **记忆** | 检索、分页浏览、按角色过滤；删单条、删会话、导出/导入备份、压缩回收 |
| **数据目录** | 看/改她住哪、搬家、恢复默认、清空、**鲸落** |
| 设置 | 模型、回复策略、版本与更新 |
| 日志 | 实时输出 |

### 她住在哪

**默认在安装目录下的 `data\`**，可以改：

```
<安装目录>/data/          ← 默认
  ├── soul.json          她是谁
  ├── mood.json          此刻怎样
  └── memory.sqlite3     她记得什么

<安装目录>/settings.json  ← 你改过的位置记在这里
```

为什么 `settings.json` 不放在 `data/` 里：放在里面的话，换目录时这份配置
会跟着搬走，程序就找不到新地址了。

**改位置有两种**，界面上分开做：

| 按钮 | 做什么 | 什么时候用 |
|---|---|---|
| 搬过去（连同数据） | 把现有数据复制到新目录，然后指过去 | 最常见。换盘、备份 |
| 只改指向 | 只改配置，不搬文件 | 新目录里已经有数据（比如从旧机器拷来的） |

搬家的顺序是「先复制完，复制成功了才改指向」——
反过来一旦复制失败，数据目录就指向一个空文件夹，打开会以为人格丢了。

**恢复默认**：指回 `<安装目录>/data`，原来那个目录不会被删。

第一次启动时如果目标目录是空的，会自动建。缺哪个补哪个：

1. `soul.json` 不存在 → 先看 DSH 那边有没有现成的人格，有就**导入一次**
2. 那边也没有 → 用内置模板创建（出厂的大肥鱼）

> ⚠️ 为什么不做与 DSH 的隐式联动：两个程序共写一份文件，谁改了谁没改
> 根本分不清，互相覆盖是迟早的事。要同步就手动拷，至少你知道自己做了什么。

### 写入语义与插件对齐

虽然文件是各自的了，但格式和规则**逐字段对齐**插件的 `/dsh-bigfish/soul`：
revision 并发校验、locked 价值保护、原子写、归一化永不抛错。

所以 `data/soul.json` 拷回 DSH 也能直接用。

### 数据目录写不进去怎么办

「数据目录」页启动时会在目录里真写一个探针文件。
不可写就红字预警：用管理员重开，或者把程序挪到有写权限的位置。

> ⚠️ 别信 `os.access(W_OK)`。Windows 上它只查 ACL，不查上层策略会不会
> 在创建文件那一刻拦下来 —— 实测过返回 `True` 但 `open(...,'w')` 照样
> `Permission denied`。只有真写一次才算数。

### 不读 DSH 的任何会话数据

微信只有一个账号一个身份，不存在「这个工作区的大肥鱼」和「那个工作区的
大肥鱼」—— 她只有一个。

所以这里**不读 DSH 的会话库**（`sessions/`、`workspace.json` 一概不碰）。
唯一的跨程序动作是第一次启动时导入一次灵魂，之后就再也没有瓜葛。

> ⚠️ 早期版本这里会去扫 `<DSH_HOME>/sessions/` 和 `workspace.json`，
> 把 DSH 里的全部工作区标题和会话 id 列出来。那是越界 ——
> EXE 是独立程序，不该给微信端一个看宿主对话目录的窗口。
> `tools/test_no_leak.py` 用 AST 断言这种代码不会再长出来。

---

## 三条硬限制

### 必须管理员权限

UIA 热激活要往 `Weixin.dll` 里一个「无障碍开关」字节写 `1`。

```
普通权限 OpenProcess(PROCESS_VM_READ) → err=5 ACCESS_DENIED
管理员权限                            → OK
```

写的是 1 个字节的运行时标志位，不注入代码、不挂 DLL、不改任何文件、
微信重启就恢复原状。

### 必须桌面解锁

UIA 是操作界面。锁屏 = 没有界面 = 所有发送失败。

### 会抢焦点

发送瞬间微信窗口会跳到最前面。

---

## 记忆与心情是怎么产生的

每轮对话**发送成功之后**，做三件事，全部在后台：

1. **原文入库** —— 一问一答两条，`event_key` 幂等，同一条读两遍不会重复记。
   切片、分词、FTS 索引都与 `dsh-bigfish` 插件的 `MemoryStore.capture()` 一致，
   所以两边建出来的库可以互相打开。顺带把这句话里提到的人挂到锚点上
   （`anchors` / `aliases`），「老爸」「爸爸」「爸」从此是同一个人。
2. **整理成认识（事实层）** —— 有 8 条新消息 / 10 分钟的限频闸门，
   攒够了才调一次模型，把原文提炼成
   `anchor + predicate + object + confidence` 的事实。
   这部分和原文分开存，因为它是**可被纠正的**：
   同一主体同一谓词出现不同内容时，旧的标 `superseded` 并指向新的，
   **两行都留着** —— 「她以前以为是 X」以后还查得到。
   忘掉也不是删除，是一条 `forget` 事件，原文照旧。
3. **判断心情** —— 把「她说的」和「对方说的」一起喂给模型，
   输出 `{moodDelta, dims, reason, confidence}`，缩幅 1/3 后写进 `mood.json`，
   同时追加一条 timeline。

注入提示词时也是两条腿：

| 段 | 内容 | 来源 |
|---|---|---|
| `## 你记得的事` | 她**听过**的原话 | `search_index` 词法检索 |
| `## 你认定的事` | 她**知道**的结论 | `facts` 里 `status=active` 的行 |

> ⚠️ 这三件事早期**全都没有**。读路径铺得很完整，写路径一行没有，
> 结果是聊一晚上记忆里一条都没有、心情页永远是 0，而且记错了只能删。
> `tools/selftest_engine_fixes.py` 与 `tools/selftest_memory.py` 现在钉住它们。

### 记忆库结构

```
<数据根>/memory.sqlite3
  ├─ messages / chunks / search_index    原文层（权威，删了就是没听过）
  ├─ anchors / aliases / anchor_sources  谁是谁（别名归一）
  ├─ facts / fact_events                 她认定的事（派生，可纠正、可遗忘）
  └─ chunk_meta / reflection_state       派生元数据与反思水位
```

**派生层可以随时重建**：记忆页的「修索引」按钮走 `memory.rebuild_derived()`，
原文一行不动，只重排 FTS 与锚点关联。体检行会告诉你「索引缺几个片段 /
有没有孤儿」，检查失败时明确说读不到，不会显示成 0。

### 检索

查询先过分词再进 FTS5 MATCH（以前整句直接丢进去，中文没有空格，
整句被当成一个 token，几乎一条都命不中），用 `bm25()` 排序，
再做一次 MMR 去冗余，最后按 `maxChars` **填满预算**而不是硬截断。
只读打开（`mode=ro`），**不再把整个库拷到临时目录**。

---
## 防回环

文件传输助手这类「自己对自己」的会话，回复会被读回来再触发回复。
判据是**两条独立证据，缺一不可**：

| 层 | 机制 |
|---|---|
| 1 | `allow_self` 默认 false —— 自己发的消息直接跳过 |
| 2 | `msg.is_self` 为真 **且** 内容匹配刚发出去的那条 → 才判回环 |
| 3 | 归一化剥掉 `wxid_xxx:` 和 `@昵称` 前缀 |
| 4 | 连发熔断：30 秒内自动回 6 条才停 |

> ⚠️ **对方复述她的话不算回环。** 早期版本只看文字像不像，
> 「那还是叫大肥鱼吧」被原样复述回来，被判成"我们自己发的"，
> 连拦两次还触发熔断 —— 把正常聊天硬生生掐了。
> 现在对方发来的消息**永远不做内容比对**：他复述是聊天，不是回环。

日志里会看到 `[回环]` 或 `[熔断]`。

---

## 清空 vs 鲸落

两件事，界面和用语都分开了：

| | 清空 | 鲸落 |
|---|---|---|
| 灵魂 | 保留 —— 她还在，只是忘了 | **删掉** |
| 心情 / 记忆 / 参数 | 删 | 删 |
| 结果 | 她还在 | **她不存在了** |

鲸落必须手输「鲸落」两个字，且强制备份。

名字来自鲸鱼死亡后沉入海底 —— 身体养活一整个生态几十年。
她的一切腐朽成别的东西，但她本人不再存在。这是告别，不是清理。

---

## 群聊

默认**只在被 @ 或叫名字时回**。三层判据：

1. `\x01` 占位符（微信标记 @ 的位置）
2. 群成员表比对
3. 昵称精确匹配

`group_allow_all` 里列出的群无条件回。

---

## 打包

```cmd
packaging\build.bat                  打包 exe
.venv\Scripts\python.exe packaging\make_installer.py    生成安装包
```

产物在 `dist/`：

```
BigFish/                    便携目录
BigFish-0.1.0-portable.zip  便携版
BigFish-0.1.0-installer/    安装器
BigFish-0.1.0-manifest.json 文件清单与校验
```

---

## 自检

```cmd
tools\selftest.py              11 项模块级实测
tools\selftest_ui.py           46 项界面数据
tools\selftest_engine.py       启停流程
tools\test_loop_guard.py        回环防护（旧）
tools\test_updater.py           更新逻辑

tools\selftest_vault.py        148 项 归一化 / 读写 / 存储布局 / 鲸落
tools\smoke_vault_live.py      37 项 真实数据读写（自动备份还原）
tools\selftest_write_e2e.py    28 项 界面→Slot→磁盘 全链路
tools\test_persona_parity.py    18 项 两侧人格逐字节一致
tools\test_no_leak.py           30 项 越界访问（不碰 DSH 会话库）
tools\selftest_engine_fixes.py 60 项 回环判定 / 记忆写入 / 心情写入 / 鲸落
tools\selftest_memory.py          71 项 事实 / 纠正 / 遗忘 / 检索 / 级联 / 迁移
```

后面这批分层的原因是：

- `selftest_vault.py` 证明**规则**对（夹取、默认值、revision、locked、布局、删干净）；
- `smoke_vault_live.py` 证明**你真实那几份文件**读写不坏；
- `selftest_write_e2e.py` 证明**点保存之后磁盘真的变了**；
- `test_persona_parity.py` 证明**两侧渲染逐字节一致**（同一份人格不能出两个人）；
- `selftest_engine_fixes.py` 证明**聊天之后记忆和心情真的被写进去**。
- `selftest_memory.py` 证明**她不仅记得住，还能改**：纠正不删旧、遗忘不删原文、中文整句能命中、删消息不留幽灵。

最后一条最容易漏：单元测试全绿、界面也正常，但就是没人写记忆 ——
那是一个"缺了一块"的问题，不是"某块坏了"，只能靠真实调用来发现。

---


## 已知限制

| 项 | 说明 |
|---|---|
| 微信重启后失联 | 要手动重启程序 |
| 群聊未做真实 @ 验证 | 可能漏回 |
| 自动更新未端到端验证 | 需要真实 Release |
| 语音/表情包发送 | 未实现 |
| 记忆写回 DSH | 微信对话写进本地库，但**不自动合并**回 DSH 那份（两边各管各的，要合并请手动导） |

详细进度见 `.planning/progress.md`，设计决策见 `.planning/decisions.md`。

---

## 致谢

- [fanyuantaier/wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica) — 数据库解密与 UIA 驱动，Apache-2.0
- [dsh-bigfish](https://github.com/q825818504/dsh-bigfish) — 灵魂、情绪、记忆插件

## 许可

自有代码 MIT。`vendor/` 遵循上游 Apache-2.0。
