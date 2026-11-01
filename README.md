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

七个页面：

| 页面 | 内容 |
|---|---|
| 概览 | 运行状态、环境自检、收发达标、最近对话 |
| 会话 | 微信会话列表，勾选要监听的 |
| 灵魂 | 人格全文、珍视、性格数值、自知（只读） |
| 心情 | 环形进度、七维情绪、注入给她的原话 |
| 记忆 | 检索测试、结果列表 |
| 设置 | 模型、回复策略、版本与更新 |
| 日志 | 实时输出 |

灵魂、心情、记忆三页的数据来自 `dsh-bigfish` 插件本身，不是副本。

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
tools\selftest.py          11 项模块级实测
tools\selftest_ui.py       20 项界面数据
tools\selftest_engine.py   启停流程
tools\test_loop_guard.py   回环防护
tools\test_updater.py      更新逻辑
```

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
