# 大肥鱼 × 微信

把 **大肥鱼**（DSH 灵魂人格）接进微信：读消息、想回复、发出去。

**用你自己的微信号，不是 Bot。**

---

## 她是什么

不是「一个接了微信的 AI」，是「同一条鱼，多了一个说话的地方」。

```
微信消息 ──▶ 数据库 ──▶ 大肥鱼 ──▶ UIA ──▶ 微信回复
                        │
                        ├── 人格：DSH 灵魂文件（729 字）
                        ├── 情绪：mood 状态 + DSH 生成的注入文本
                        ├── 记忆：dsh-bigfish 原版 MemoryStore.recall()
                        └── 模型：Magpie 本地网关
```

**人格、情绪、记忆三样全部来自 `dsh-bigfish` 插件本身**，不是复制的副本。记忆检索用 Node 调原版模块，分词、半衰期衰减、FTS5 索引全部原样——所以微信里和 DSH 里，她记得的是同一批事。

---

## 前置条件

| 项 | 要求 |
|---|---|
| 微信版本 | **4.1.15.53**（UIA 热激活的 gate 地址在这个版本验证过） |
| Python | 3.12+，本仓库自带 `.venv` |
| 权限 | **管理员**（热激活要写 1 个字节到微信进程） |
| 桌面 | **解锁且微信窗口可见** |
| 模型 | Magpie 网关（DSH 启动时自动拉起） |

---

## 快速开始

```cmd
:: 1. 配置要监听的会话
copy config.example.json config.json
:: 编辑 config.json 里 auto_reply.chats

:: 2. 双击
tools\start.bat
```

`start.bat` 会让你选 **Dry（只生成不发送）** 或 **Live（真发）**。

**第一次一定先用 Dry。** 确认她说得像，再切 Live。

---

## 配置说明

`config.json`：

```json
{
  "endpoint": "http://127.0.0.1:3425/v1/chat/completions",
  "model": "workbuddy-ai/deepseek-v4.1-flash",
  "api_key": "",

  "auto_reply": {
    "chats": ["文件传输助手", "小麟"],
    "allow_self": true,
    "interval": 1.2,
    "max_reply_chars": 300,
    "quiet_hours": []
  }
}
```

| 字段 | 说明 |
|---|---|
| `chats` | 要监听的会话。私聊写昵称，群聊写群名 |
| `allow_self` | 是否处理自己发的消息。文件传输助手测试时必须 `true` |
| `interval` | 轮询间隔秒。越小越灵敏，越费 CPU |
| `max_reply_chars` | 回复长度上限，超了截断 |
| `quiet_hours` | 安静时段（0-23 的整点），这些时段不回复 |

换模型：把 `endpoint` / `model` / `api_key` 改成你要的。任何 OpenAI 兼容接口都行。

---

## 三条硬限制

### 1. 必须管理员权限

UIA 热激活要做一件事：往 `Weixin.dll` 里一个「无障碍开关」字节写 `1`。

```
普通权限 OpenProcess(PROCESS_VM_READ) → err=5 ACCESS_DENIED
管理员权限                            → OK
```

**不写内存就驱动不了微信界面。** 这是 UIA 路线的固有代价。

写的是 1 个字节的运行时标志位，不注入代码、不挂 DLL、不改任何文件、微信重启就恢复原状。

### 2. 必须桌面解锁

UIA 是操作界面。锁屏 = 没有界面 = 所有发送失败。

程序会检测，锁屏时明确报错而不是静默失败。

### 3. 会抢焦点

发送瞬间，微信窗口会跳到最前面。这是真实操作界面的代价。

---

## 已知问题

| 问题 | 说明 |
|---|---|
| **群聊没有 @ 过滤** | 监听活跃群会不停回话。建议先只监听私聊 |
| 微信不能收到托盘 | 窗口不可见时 UIA 找不到控件 |
| 重启后不补历史 | 关掉期间的消息不会补回复（故意的，避免被旧消息刷屏） |
| 单实例 | 同一个号不要开两个进程，会抢窗口 |

---

## 架构

```
bigfish/
├── app.py            正式入口
├── soul.py           人格渲染（render-don't-hold）
├── bridge_client.py  调 Node 侧拿情绪+记忆
├── render.py         消息语义化（XML → 可读文本）
├── wechat.py         数据库只读封装
├── watcher.py        轮询监听新消息
├── wechat_sender.py  UIA 发送（带归属校验）
└── replier.py        调模型

bridge/
└── bridge.mjs        灵魂/情绪/记忆桥接（import dsh-bigfish 原版模块）

vendor/
└── wechatauto-replica/   微信 4.x 数据库解密 + UIA 驱动（上游 1.2.9）

research/                   研究阶段的探测脚本（保留备查）
```

### 人格与协议分离

```
system  = 人格（灵魂）+ 这会儿的状态（情绪）
user    = 你记得的事（记忆）+ 这一轮怎么答（协议）+ 对话记录
```

**人格里不放任何输出格式规则。** 灵魂只定义「我是谁」，不该被「别用 Markdown」这种要求污染。

### 消息水位

```python
rows = get_messages(uid, limit=50)     # 降序，取最新
for r in rows:
    if r.sort_seq <= watermark: break
    if (shard, local_id) in prefill: continue
    → 新消息
```

**不用 `db.get_new_messages`**：它是 `ORDER BY sort_seq ASC LIMIT n`，返回的是**最老**的 n 条。水位从 0 时永远追不到最新。

水位**不跨进程持久化**：每次启动重新对齐到当前最新。这是踩过坑之后的决定——残留水位曾把新消息挡在外面。

---

## 验证过的数据

2026-11-01，微信 4.1.15.53，账号 `wxid_74pumkyi8vs422`：

```
数据库密钥   18/18
UIA 热激活   gate 0x0B3959D8，一次成功
输入框定位   mmui::ChatInputField，Name == 当前会话
发送测试     文件传输助手，数据库回读确认
监听测试     连续 3 条消息全部捕获，水位正确推进
完整对话     3 轮，2 秒内响应
```

一段真实输出：

> 爸～又叫我大肥鱼，腮帮子都鼓起来了
>
> 那……叫我鱼鱼也行啊，听着没那么像在称体重

---

## 致谢

- [fanyuantaier/wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica) — 数据库解密与 UIA 驱动，Apache-2.0
- [dsh-bigfish](https://github.com/q825818504/dsh-bigfish) — 灵魂、情绪、记忆插件

## 许可

自有代码 MIT。`vendor/` 遵循上游 Apache-2.0。
