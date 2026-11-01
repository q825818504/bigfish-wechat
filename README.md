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
| **数据目录** | 看她住哪、三个文件在不在、清空。**位置固定，不可改** |
| 设置 | 模型、回复策略、版本与更新 |
| 日志 | 实时输出 |

### 她住在程序旁边

数据目录**固定在安装目录下**，不跟 DSH 共用：

```
<安装目录>/data/
  ├── soul.json          她是谁
  ├── mood.json          此刻怎样
  └── memory.sqlite3     她记得什么
```

第一次启动时自动建。缺哪个补哪个：

1. `soul.json` 不存在 → 先看 DSH 那边有没有现成的人格，有就**导入一次**
2. 那边也没有 → 用内置模板创建（出厂的大肥鱼）

之后两边各管各的。在微信里改人格，DSH 那边不会跟着变；反过来也一样。

**想带走她**：整个 `data` 文件夹拷走就行。

> ⚠️ 为什么不做隐式联动：两个程序共写一份文件，谁改了谁没改根本分不清，
> 互相覆盖是迟早的事。要同步就手动拷，至少你知道自己做了什么。

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

## 防回环

文件传输助手这类「自己对自己」的会话，回复会被读回来再触发回复。
四层防护：

| 层 | 机制 |
|---|---|
| 1 | `allow_self` 默认 false |
| 2 | 内容比对（20 秒窗口） |
| 3 | 归一化剥掉 `wxid_xxx:` 和 `@昵称` |
| 4 | 频率熔断（90 秒 4 条） |

日志里会看到 `[回环拦截]` 或 `[熔断]`。

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
tools\selftest.py            11 项模块级实测
tools\selftest_ui.py         46 项界面数据
tools\selftest_engine.py     启停流程
tools\test_loop_guard.py     回环防护
tools\test_updater.py        更新逻辑

tools\selftest_vault.py      129 项可写层（临时目录，不碰真实数据）
tools\smoke_vault_live.py    39 项真实数据读写（自动备份还原）
tools\selftest_write_e2e.py  23 项 界面→Slot→磁盘 全链路
tools\test_persona_parity.py 18 项 两侧人格逐字节一致
tools\test_no_leak.py        20 项 越界访问（不碰 DSH 会话库）
```

后三个是针对写层的。分层的原因：

- `selftest_vault.py` 证明**归一化规则**对（夹取、默认值、revision、locked）；
- `smoke_vault_live.py` 证明**你真实那几份文件**读写不坏（4000 字 whoIAm、
  4 条锁定价值、中文路径）；
- `selftest_write_e2e.py` 证明**点保存之后磁盘真的变了**。

第三层不能省。曾经有过一次：`list_memory_sessions` 声明成
`@Slot(str, result=str)` 却只有一个参数，Qt 直接
`No candidates found`，界面上表现为「加载中…」永远不动。
前两个测试全绿，界面是坏的。

`test_persona_parity.py` 守的是另一件事：**同一个灵魂文件，
Python 侧和 Node 侧必须渲染出逐字节相同的人格文本**。
两侧曾经因为抄错的 trait 文案和"读路径擅自补默认值"而分裂 ——
682 字 vs 3 字，等于两个不同的人。

---

## 已知限制

| 项 | 说明 |
|---|---|
| 微信重启后失联 | 要手动重启程序 |
| 群聊未做真实 @ 验证 | 可能漏回 |
| 自动更新未端到端验证 | 需要真实 Release |
| 语音/表情包发送 | 未实现 |
| 记忆写回 | 微信对话还没写进 DSH 记忆库 |

详细进度见 `.planning/progress.md`，设计决策见 `.planning/decisions.md`。

---

## 致谢

- [fanyuantaier/wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica) — 数据库解密与 UIA 驱动，Apache-2.0
- [dsh-bigfish](https://github.com/q825818504/dsh-bigfish) — 灵魂、情绪、记忆插件

## 许可

自有代码 MIT。`vendor/` 遵循上游 Apache-2.0。
