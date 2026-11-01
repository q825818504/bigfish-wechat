# 架构

## 整体链路

```
微信客户端                     本程序
┌──────────┐                ┌─────────────────┐
│ SQLCipher │──解密读取──────▶│ WeChatReader    │
│  数据库    │                │ (bigfish/wechat) │
└──────────┘                └────────┬────────┘
                                     │
                            ┌────────▼────────┐
                            │ MessageWatcher  │  轮询发现新消息
                            └────────┬────────┘
                                     │
        ┌────────────────────────────┼────────────────────────────┐
        │                            │                            │
┌───────▼────────┐          ┌────────▼────────┐          ┌────────▼────────┐
│  GroupGate     │          │  BigFishApp     │          │  UiaSender      │
│  群聊触发判定   │          │  组装 + 调模型   │          │  驱动界面发送    │
└────────────────┘          └────────┬────────┘          └────────┬────────┘
                                     │                            │
                            ┌────────▼────────┐                   │
                            │  bridge_client  │                   │
                            │  灵魂/情绪/记忆  │                   │
                            └────────┬────────┘                   │
                                     │                            │
                            ┌────────▼────────┐          ┌────────▼────────┐
                            │ bridge.mjs      │          │ 控件树 (UIA)    │
                            │ import 原版插件  │          │ mmui::*         │
                            └─────────────────┘          └─────────────────┘
```

## 模块职责

| 模块 | 职责 | 关键设计 |
|---|---|---|
| `soul.py` | 灵魂数据 → 人格文本 | render-don't-hold：从数据生成，不手抄 |
| `bridge_client.py` | 调 Node 拿情绪/记忆 | 调原版模块，逻辑零偏差 |
| `bridge/bridge.mjs` | 记忆检索桥 | import `dsh-bigfish` 的 MemoryStore |
| `render.py` | 消息语义化 | XML → 可读文本，丢弃 aeskey/md5 |
| `wechat.py` | 数据库只读封装 | 不提供任何写能力 |
| `watcher.py` | 轮询监听 | 降序取最新 + 水位比较 |
| `group_gate.py` | 群聊触发判定 | 三层判据 |
| `wechat_sender.py` | UIA 发送 | 带归属校验防发错人 |
| `replier.py` | 调模型 | OpenAI 兼容 |
| `desktop.py` | Qt 界面 + 引擎 | QWebChannel 双向通信 |
| `updater.py` | 自动更新 | GitHub Releases + 外部替换脚本 |

## 为什么这样分层

### 读写分离

读取走数据库（只读、快、不需要界面、不抢焦点）。
发送走 UIA（必须操作界面，因为微信 4.x 的自绘 UI 没有别的写入接口）。

两者互不依赖，任一失败不影响另一个。

### 人格与协议分离

```
system  = 人格（灵魂）+ 这会儿的状态（情绪）
user    = 你记得的事（记忆）+ 这一轮怎么答（协议）+ 对话记录
```

人格里不放任何输出格式规则。灵魂只定义「我是谁」，不该被
「别用 Markdown」这种要求污染。

### 用 Node 而不是 Python 重写记忆检索

`dsh-bigfish` 的记忆检索有中文二元分词、半衰期衰减、FTS5 索引、
session scope 过滤。Python 重写必然产生偏差，结果是
「微信里的大肥鱼」和「DSH 里的大肥鱼」记得不一样的事。

`bridge.mjs` 直接 `import` 原版模块调用，逻辑零偏差。

## 数据流：一条消息的完整旅程

```
1. 手机发「在吗」
2. 微信写入本地 SQLCipher 数据库
3. MessageWatcher 轮询发现 sort_seq 变大
4. 取出消息，render.py 语义化
5. GroupGate 判定（私聊直接过）
6. BigFishApp 组装 prompt：
     system = soul.render_persona() + mood.injected_text
     user   = memory.recall("在吗") + 协议 + 最近对话
7. 调模型，1-2 秒返回
8. UiaSender:
     open_chat("老爸")  → 校验 current_chat
     定位 mmui::ChatInputField → 校验 Name
     写入文本 → 回车
9. 微信把消息发出去，写回数据库
10. remember_sent() 记住这条，防止被读回来时再次触发
```

## 引擎线程模型

```
主线程 (Qt)
  └── QWebEngineView   界面
  └── Backend          Slot 接口
        ↕ 信号
  QThread (EngineWorker)
        └── MessageWatcher（内部再起一个轮询线程）
        └── 模型调用（阻塞，但不影响界面）
        └── UIA 发送（阻塞）
```

界面每 2 秒轮询 `get_status`，日志通过信号推送 + 缓冲区补漏。

## 关键设计决策

见 `decisions.md`。

---

## 写层：`bigfish/vault.py`

```
            读路径                        写路径
  ┌──────────────────────┐     ┌──────────────────────────┐
  │  bridge.mjs (Node)   │     │  vault.py (Python)       │
  │  import 原版 store.js │     │  逐字段复刻 store.js 语义 │
  └──────────┬───────────┘     └────────────┬─────────────┘
             │                              │
             └──────────► 同一批文件 ◄───────┘
              soul-*.json / mood-*.json / memory-*.sqlite3
                          scope.json / root.json
```

**为什么读走 Node、写走 Python**
读要的是**分词与衰减算法**（中文二元分词、半衰期、FTS5），
在 Python 里重写必然漂移 —— 会让微信里的大肥鱼和 DSH 里的记得不一样的事。
写要的是**结构归一与原子性**，那是纯规则，逐条复刻即可，而且必须能在
没有 Node 的 exe 里跑。

**三条不能破的约束**（全部有测试钉住）

1. **原子写**：同目录 `.tmp` → `os.replace`。
   跨盘 rename 会抛；写一半断电不能留下半个灵魂文件。
2. **归一化永不抛错**：任何输入都夹成合法结构。
   未知 trait 键丢弃；`values: []` 是真实意图，**不能**偷偷补回默认值。
3. **revision 每次写盘 +1**：面板靠它检测并发。

**删除记忆的级联顺序**（踩过的坑）
`chunks.message` 是外键，`foreign_keys=ON` 时先删 messages 会抛约束错误。
正确顺序：FTS → chunks → anchor_sources/activations/groups/replays → messages。
漏掉 FTS 的表现是「检索命中了已经不存在的内容」—— 比删不掉还难查。

---

## 四页职责

| 页 | 读什么 | 写什么 |
|---|---|---|
| 灵魂 | `soul-*.json` | 同上（整卡，含 locked 保护与 revision 并发校验） |
| 心情 | `mood-*.json` + `bigfish-mood/config.json` | 配置参数 + 状态重置 |
| 记忆 | `memory-*.sqlite3` | 删消息/删会话/导出/导入/VACUUM |
| 数据目录 | `root.json` + `scope.json` + 范围目录扫描 | 改数据根、搬家、切范围 |
