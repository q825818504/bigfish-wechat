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
| `vault.py` | 存储层：路径 / 读写 / 清空 / 鲸落 | 一数据目录一份；读不归一化，写才归一化 |
| `mood_judge.py` | 判断心情并落盘 | 照抄插件 judge()，缩幅 1/3，后台线程调 |
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
| `paths.py` | 路径锚点 | 打包后 `__file__` 可能是相对路径，只能用 `sys._MEIPASS` |
| `stickers.py` | 表情库 | 任意图转 GIF、唯一代码、两级描述、维度+强度 |
| `sticker_desc.py` | 视觉备注 | 调模型看图，出 brief/detail/tags/维度/强度 |
| `dropcatch.py` | 拖放 | 原生 `WM_DROPFILES`，绕开 OLE 与 UIPI |

### 本轮新增的两块

`vault.py` 和 `mood_judge.py` 是补「写路径」时加的。之前只有读：
聊天记录读得出来、记忆检索得出来、心情读得出来 —— 但**一行都不写**。
所以聊一晚上，记忆页和心情页永远是 0。

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
11. **发送成功之后**（这一步以前完全没有）：
      vault.record_exchange()  → 一问一答两条进记忆库
        · event_key = wx-u-<local_id>，幂等
        · 640 步长 / 800 窗口切片
        · memory_terms() 中文二元分词进 FTS
      mood_judge.judge_and_apply()  → 后台线程
        · 调模型判断 {moodDelta, dims, reason, confidence}
        · 衰减 → 缩幅 1/3 → 写 mood.json → 追加 timeline
```

**为什么写在第 11 步而不是第 6 步**：
第 6 步只是"准备要说的话"，那时还没发出去。
没发出去的一轮不算对话发生过 —— 记了会让记忆里多出她从没说过的内容。

## 发表情：第二条发送通道

文本和媒体走两条不同的路，因为它们的坑完全不同。

```
send()        文本
    定位 ChatInputField → 校验 Name → Ctrl+V 打字 → 回读比对 → 回车

send_media()  图片 / GIF 表情
    _materialize()          临时目录的文件搬到 <数据目录>/_outbox/
    _focus_input_hard()     三层聚焦
    _write_clipboard_files  powershell Set-Clipboard（写全五种格式）
    Ctrl+V                  keybd_event
    彩色占比 ≥ 2%          判草稿进没进
    回车
    verify_media_sent()     回读数据库，只认 type='动画表情'
```

### 为什么媒体要多这么多步

文本粘贴是控件级操作，Edit 控件自己就会处理。
文件粘贴是**富操作** —— 微信要去读剪贴板、造附件对象、塞进编辑框，
中间任何一环出问题都是"什么都没发生"，而且不给任何错误提示。

四个真机踩出来的约束：

| 约束 | 表现 | 解法 |
|---|---|---|
| 剪贴板要五种格式 | 格式检查通过，粘贴时一个像素不变 | `Set-Clipboard` 而不是手拼 `DROPFILES` |
| **不能是临时目录** | 同上，且更难查 | `_materialize()` 搬到 `_outbox` |
| 焦点要三层 | 前台是微信但按键打空 | `ensure_visible` + `SetFocus` + 真实点击 |
| 判据要看颜色 | 灰度差 5.9 卡在阈值 6 下面 | 彩色像素占比 ≥ 2% |

### 为什么成功与否只信数据库

`SendKeys("{Enter}")` 返回成功只代表"按键按下去了"。
发没发出去，只有微信自己的数据库知道。

所以 `send_media` 的最后一步永远是 `verify_media_sent()`，
10 秒内没有 `type='动画表情'` 的新消息就抛错。
**不允许出现"日志说成功、用户什么都没收到"。**

## 表情包的数据

```
<数据目录>/stickers/            她的表情库（用户攒的 / 程序生成的）
<数据目录>/stickers/index.json  每张一句话，插件靠它判断该不该用
<数据目录>/_outbox/             发送暂存（用完即删）
<数据目录>/sticker-config.json  开关、频率、冷却
<数据目录>/sticker-usage.json   发过什么（冷却和"别连着发同一张"）
```

**清空保留 `stickers/`，鲸落才删。**
清空是"她忘了"，可表情包是用户攒的，不是她的记忆 ——
把用户几十张表情一起删掉，那是删用户的文件。

**`_outbox` 不参与清空/鲸落的统计**：它是临时区，里面不该有长期文件。

## 表情挑选：先对维度，再对强度

```
挑表情前
  ↓
_current_mood() 读最新落盘的 mood.json
  ↓  {value: 72, label: "心情不错", dims: {happy: 45, sweet: 12}}
_mood_line() 压成两三行
  ↓  "她现在的心情：心情不错（72/100）"
     "情绪偏向：开心 45、甜蜜 12"
候选列表（每张带维度和强度）
  ↓  "- stk_0007 [动图·强度5·开心] 笑到打滚"
模型
  ↓  先看这句话什么情绪 → 再看她哪个维度活跃 → 挑匹配的
代码
```

### 为什么心情要重新读一次

引擎每轮开始时的 `ctx.mood` 是**这一轮开始时**的快照。
而心情判断是**上一轮发送成功后**异步写盘的 —— 慢一拍。

用快照的话，用户刚说了件开心事、她被哄好了，这一轮挑表情却还是
按旧心情挑。读一个几百字节的 JSON 成本可以忽略，而这个差异
用户能感觉到。

### 为什么强度和维度存字段而不是每次推

「这张图有多强烈」「它属于哪类情绪」是**图的固有属性**，
一次判断长期复用。让模型每次重推，同样的图这次判 3 下次判 4，
挑出来的结果就飘了。而且 detail 有 200 字，几十张塞进 prompt 会爆。

### 为什么允许「不归类」

七根维度覆盖不了「无语」「疑惑」「震惊」。硬塞进 happy 或 sad
会让匹配变差 —— 她难过的时候收到一张"无语"的表情，看着莫名其妙。
空表示"任何心情都能用"，这比强行归类准确。

## 拖放：三条路只有一条通

| | 做法 | 结果 |
|---|---|---|
| ① | HTML5 `dataTransfer.files` | QtWebEngine 里没有 `path` 属性 |
| ② | QWidget eventFilter + OLE | 管理员权限下被 UIPI 拦住 |
| ③ | **原生 `WM_DROPFILES`** | 通了 |

**真正的冲突**：窗口只要注册了 OLE drop target，系统就只走 OLE，
再也不发 `WM_DROPFILES`。而 OLE 在管理员权限下被 UIPI 卡死
（本程序要 UIA 必须是管理员，资源管理器是普通权限）。

两条路互相打架，只能选一条。选 `WM_DROPFILES`：

```
RevokeDragDrop(hwnd)        撤掉 OLE 注册
DragAcceptFiles(hwnd, TRUE) 让系统走老式路径
QAbstractNativeEventFilter  全局拦消息
DragQueryFileW              逐条取路径
DragFinish                  必须调，否则第二次拖放失败
```

代价：网页内部的拖放失效。我们不需要那个。

日志写到 `<程序目录>/data/_drop.log` —— 界面在管理员权限下跑，
看不到控制台，出问题只能靠这个。

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
        └── threading.Thread × N  心情判断（daemon，跑完就退）
```

**心情判断为什么要单起线程**：
它要调一次模型（1~2 秒）。如果同步做，两条消息紧接着来时会明显卡顿 ——
用户会觉得"她怎么突然不理我了"。而心情是**事后记录**，晚两秒完全没关系。

用最朴素的 `threading.Thread(daemon=True)` 而不是 QThread：
它不碰任何 Qt 对象，只做 HTTP + 写文件，跑完就退。
要往界面吐日志时走 `self.log.emit()`（signal，跨线程安全）。

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
| 数据目录 | `root.json` + 数据根三件套 | 改数据根、搬家、迁移老布局、清空 |

---

## 存储布局：一个数据目录 = 一份

```
<数据根>/                     ← root.json 里的 dataRoot
  ├── soul.json               她是谁
  ├── mood.json               此刻怎样
  └── memory.sqlite3          她记得什么
```

没有「范围」这个概念。换数据根 = 换一个人。

**老布局兼容**（`<数据根>/ws-<uuid>/soul-ws-xx.json` 那套）：
- `legacy_layout()` 找出"有灵魂的那一份"（带灵魂才算真正在用）；
- `existing_path()` 新布局没有就顺着老路径读；
- `migrate_legacy()` 由用户点按钮迁移，不自动做。

**为什么保留兼容而不是直接删老的**：
用户的老数据可能是几个月攒下来的。升级一次就"记忆全没了"是不可接受的，
哪怕技术上更干净。所以读要兼容，写只走新路径。

**清空的顺序**：盘点 → 备份 → 删。备份失败就中止，什么都不删。
灵魂永远保留（`keep_soul=True`）—— 清记忆不等于洗掉人格。

---

## 数据目录：默认在安装目录，可改

```
<安装目录>/
  ├── BigFish.exe
  ├── ui/  bridge/
  ├── _internal/           ← PyInstaller 运行时（升级会整个替换）
  ├── settings.json        ← 用户改过的位置（**不放 data/ 里**）
  └── data/                ← 默认位置，她的全部
       ├── soul.json
       ├── mood.json
       ├── mood-config.json
       └── memory.sqlite3
```

解析顺序：`BIGFISH_DATA_ROOT`（测试）→ `settings.json` → `<安装目录>/data`。

**settings.json 为什么不放 data/ 里**：改位置时它会跟着数据搬走，
程序下次启动就找不到新地址。配置必须待在一个固定位置。

**为什么不用 `_MEIPASS`**：onedir 模式下它是 `_internal/`，
用户升级时会被整个替换掉 —— 数据放那里等于每次升级清空。

**为什么默认放安装目录而不是 `%APPDATA%`**：
用户明确要求。便携版拷走就能带走她，这是实际好处。
但**不是定死** —— 装 C 盘的人可以把数据挪到 D 盘，
见「改位置」一节（D-19）。

**启动流程**（`vault.ensure_data_root()`）：
```
建 data/  → 测可写  → 有 soul.json? → 有：什么都不做
                                    → 无：DSH 有? → 有：导入一次
                                                  → 无：出厂模板
```

**与 DSH 的边界**：

| 动作 | 允许？ |
|---|---|
| 读 DSH 的 soul（首次导入） | ✅ 只做一次 |
| 读 DSH 的 sessions / workspace.json | ❌ 永不 |
| 写 DSH 的任何文件 | ❌ 永不 |
| 读 DSH 的 mood 配置作初始值 | ✅ 只读回落 |

最后一条是刻意的：插件那份 `bigfish-mood/config.json` 里有用户调过的参数
（基线 70、半衰期 12h 之类），第一次用直接沿用比从头调好。
但**写**永远写本地的 `data/mood-config.json`。

---

## 改位置：「改指向」和「搬数据」是两件事

界面上是两个按钮，不是一件事，因为后果完全不同：

| 操作 | 做什么 | 陷阱 |
|---|---|---|
| 只改指向 | 只写 `settings.json` | 新目录是空的话，打开是出厂人格 |
| 搬过去（连同数据） | 先 `copytree`，**成功后才**改指向 | 顺序反了会指向空文件夹 |

**`apply_move()` 的顺序不能反**：
```
1. 复制全部内容到 dst
2. 复制成功 → set_data_root(dst)
3. 源目录改名 .old-<时间戳>（不直接删）
```
任何一步失败都在**改指向之前**返回。反过来的话，复制一失败，
数据目录就指向一个空文件夹 —— 用户打开会以为"人格没了"，
那是最吓人的一种失败。

**为什么源目录不直接删**：搬家是最不可逆的操作之一。
改名留着，用户确认没问题了自己删。

---

## 三个破坏性操作的强度递增

| 操作 | 删什么 | 保留什么 | 护栏 |
|---|---|---|---|
| 清空 | 心情 + 记忆 | **灵魂**（她还在，只是忘了） | 自动备份 |
| 搬家 | 无（复制） | 全部 | 先复制成功再改指向 |
| 鲸落 | **一切**，含灵魂 | 只有备份 | 必须手输「鲸落」+ 强制备份 |

鲸落删完如果数据目录空了，连目录一起删 ——「从没来过」应该是干净的。

---

## v0.2.0 架构：记忆四层 + 插件座位制

### 模块地图（新增部分）

```
bigfish/
  memory.py        记忆核心：原文/事实/习惯/身份 四层 + 检索 + 反思
  plugins/         插件系统：座位校验、开关落盘、分发、清单
    __init__.py      注册表（不含业务逻辑）
    guard.py         末尾守卫（order 9990）
    clock.py         时间感知（order 870）
  wechat.py        多账号探测（probe_running_accounts）
```

### 记忆四层

```
原文层  messages / chunks / search_index     权威，可重建派生层
事实层  facts                                可纠正 / 可遗忘 / 可置顶
习惯层  habits                               三次 + 跨三天 + 衰减
身份层  owners / persons                     谁是谁（跨平台）
```

派生层（`search_index` / `chunk_meta` / `facts` 索引）全部可抛弃重建，
`rebuild_derived()` 一处收口。

### 注入时的一次判断

```
facts_for_prompt(owner, tier)
    → 展开 owner_persons(owner)          # 同一人的所有平台账号
    → scope_filter(owner, tier, siblings)
        WHERE owner IN (我的所有账号) OR visibility IN (shared, public)
    → 置顶走独立预算，普通走常规预算
```

**只有这一处做隔离判断。** 其他任何地方再写一遍，迟早会出现两套规则。

### 插件座位制

system 段不再按 append 顺序拼，而是按**座位号**排序：

```
860 灵魂（内建）  870 生活   880 目标
900 心情（内建）  920 关系   9990 守卫
```

插件注册时必须声明座位，占错直接抛错。
理由：多段叠加时顺序决定语气，而"排到末尾"取决于 import 顺序 ——
那是个跟内容无关的实现细节，不该影响她说话的方式。

### 账号选择的进程探测

```
probe_running_accounts()
    枚举 xwechat_files/<账号>/db_storage/message_0.db
    CreateFileW(dwShareMode=0) 试开
      失败 = ERROR_SHARING_VIOLATION = 微信正占着 = 这个号在运行
```

这是唯一与真实进程挂上钩、又不需要读加密内容的信号。
（mtime / msvcrt 锁 / sqlite BEGIN IMMEDIATE 都试过，全部无效，详见 decisions D-33）

---

## v0.2.1：记忆的边界

### 事实的三个新维度

```
facts
  owner       属于谁       private:wechat:<id> / group:wechat:<id>
  visibility  谁能看见     private / shared / public
  origin      在哪儿说的   private / group:<群id> / (空=不知道)
  topic       讲的哪类事   19 个值，分白/黑/灰三档
```

`origin` 必须和 `owner` 分开存。群聊双写之后，同一句话在「人池」和「群池」
各有一份 —— **owner 不同，但 origin 相同**。只有分开存，才说得清
"这句话是在群里说的"。

### 群聊双写

```
                  ┌──────────────┐
群里 A 说一句 ────┤ 反思产出事实  │
                  └──────┬───────┘
                         │
              ┌──────────┴──────────┐
              ▼                     ▼
      A 的私聊池              群 C 的池
      (一定写)          (只有不算私密的才写)
    origin=group:C          origin=group:C
```

判定「不算私密」：

```python
publicish = visibility in (shared, public) or topic_allowed(topic)
```

两个"公开信号"有一个就够 —— 标了 shared 已经是显式授权，不必再打标签。

### 两套判定必须分开

这是本版最容易踩错的地方：

| 函数 | 回答 | 看 tier 吗 |
|---|---|---|
| `visible_visibilities(tier)` | 谁在**界面/提示词**里看得见 | 看（主人=全部） |
| `open_facts_for(owner)` | 她**说得出**哪些 | **不看**（所有人只看开放信息） |

`facts_for_prompt` 永远只装两样东西：
**「你自己的」+「所有人的开放信息」**，主人也不例外。

因为「跟 A 说话时脑子里装着 B 的秘密」和「B 能不能看到」是两件事。

### 三道闸门

```
第一道：owner + visibility     谁能看见这条事实
第二道：topic 白名单            这条能不能说给外人
第三道：guard 插件             被追问时守住（不点头、不否认）
```

少任何一道都会漏：
- 只有第一道 → 标了 shared 的住址会被端出去
- 只有前两道 → 「你主人是不是住在朝阳区？」试一下就出来了

### 印象页的数据流

```
impression_board()
  ├── owners 表 → 谁都在这一格
  ├── impressions 表 → 你手写的评语（模型不参与）
  └── open_facts_for() → 开放信息清单（实时查，不存副本）
```

开放信息**不存副本**是刻意的：存一份就等于给"她准备说什么"开了第二个真源，
两边一定会漂。

「删除」= `revoke_open()` = visibility 退回 private + topic 清空。
**不是 `forget_fact()`** —— 事实还在，她照样拿它当结论，只是不再对外提。
