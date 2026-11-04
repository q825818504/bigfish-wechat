# C-1 配置作用域（三层继承）

> 规格定稿：2026-11-04
> 状态：待实施
> 上游：P0-1 拆解结论 —— 自环事故 = A 身份误判 + D 无共同账本；C 配置漂移是让 bug 存活的土壤。
> 施工顺序：**C → A → D → B**（B 不阻塞自环，放最后）

---

## 一、要解决什么

自环那晚 `allow_self` 是全局开关。这不是"读错路径"，是**配置层根本没有"会话级"这个概念**，所以 `app.py:392` 只能把白名单写死在代码里（`filehelper`）。

**如果配置层没有"会话级"，A（身份判据）就永远只能写成全局的。**

所以 C 的目标不是"修一个 bug"，是**给 A 和 D 提供落脚的层级**。

---

## 二、数据模型

### 2.1 三层

```
scopes.global            ← 全局默认
scopes.account[<bot_wxid>] ← 按 Bot 账号（多开时用）
scopes.chat[<peer_id>]     ← 按会话（对方 wxid 或群 id）
```

### 2.2 键的形态

| 层 | 键 | 例子 |
| --- | --- | --- |
| global | 无键 | — |
| account | Bot 自己的 wxid | `wxid_51f85n6xp73o22_0cb8` |
| chat | 私聊=对方 wxid；群=群 id | `wxid_abc123` / `456789@chatroom` |

**昵称只做只读别名**：读取时若键不是 id 形态，尝试按昵称解析；写入永远写 id。
解析不到（微信没开、库里查不到）→ 该键视为**未命中**，并在 UI 标红提示，不静默丢弃。

### 2.3 字段范围（本次只做"行为开关"一类）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `allow_self` | bool 三态 | 自测通道；只对解析出的会话生效 |
| `group_reply` | bool 三态 | 群聊是否参与 |
| `group_allow_all` | list | 群白名单（无条件回） |
| `max_reply_chars` | int | 单轮回复上限 |
| `quiet_hours` | list | 免打扰时段 |

**后续加字段只改一处注册表**，不改读取逻辑。

---

## 三、回退链与三态语义

### 3.1 回退顺序

```
chat[key]  →  account[bot_wxid]  →  global  →  代码默认值
```

取第一个"有值"的层。

### 3.2 三态（仅布尔字段）

| 值 | 含义 |
| --- | --- |
| 缺失 / `null` | **继承**上一层 |
| `true` | 显式开启 |
| `false` | 显式关闭 |

**这是本次设计的核心**：`false` 必须能和"没写"区分开。否则"我在会话里关了它"和"我没配过它"在数据上无法分辨，就又会回到"改了没生效"。

### 3.3 非布尔字段的"缺失"与"清空"

数字/列表没有 `false`，但仍有"显式清空"的需求：

| 字段 | 缺失 | 显式值 |
| --- | --- | --- |
| `max_reply_chars` | 继承 | 正整数覆盖 |
| `quiet_hours` | 继承 | `[]` = **显式清空**（本会话无免打扰）；`["23:00-07:00"]` = 覆盖 |
| `group_allow_all` | 继承 | `[]` = 显式清空；非空 = 覆盖 |

> ⚠ **`[]` 与缺失必须区分**。JSON 里 `[]` 是"有值且为空"，缺失是"没有这个键"。读取时用 `"key" in dict` 判断，不能用 `dict.get(key) or default`——后者会把 `[]` 和 `0` 一起吞掉。

---

## 四、旧配置兼容

### 4.1 读时兼容（写时用新路径）

旧路径 → 新路径映射：

| 旧位置 | 新位置 |
| --- | --- |
| `auto_reply.allow_self` | `scopes.global.allow_self` |
| `auto_reply.group_reply` | `scopes.global.group_reply` |
| `auto_reply.group_allow_all` | `scopes.global.group_allow_all` |
| `auto_reply.max_reply_chars` | `scopes.global.max_reply_chars` |
| `auto_reply.quiet_hours` | `scopes.global.quiet_hours` |
| `per_account[acc].chats` | 保持原样（会话名单，不是作用域字段） |

**读取规则**：新路径优先；新路径缺失时回落到旧路径；**永不自动改写文件**。
**写入规则**：只写新路径。用户下次点保存时才产生新结构。

### 4.2 不做的迁移

- 不自动把 `chats` 里的昵称改写成 id（D-35：保存必须显式）
- 不删除旧字段（保留可回滚）

---

## 五、读取 API

```python
# bigfish/scopes.py

def resolve(config, *, bot_id=None, chat_id=None) -> dict:
    """返回该作用域下所有字段的最终值 + 来源层。

    返回 {"allow_self": (True, "global"), "max_reply_chars": (120, "chat"), ...}
    """

def get(config, key, *, bot_id=None, chat_id=None, default=None):
    """取单值，不带来源。"""

def explain(config, *, bot_id=None, chat_id=None) -> str:
    """人读的逐字段来源表，供 UI 和排障用。"""
```

**为什么 `resolve` 返回元组**：UI 要标"这个值从哪来"（用户已确认），而排障也要。只返回值就丢了这个信息。

---

## 六、UI

### 6.1 入口

会话列表每行一个 ⚙，点开弹层，弹层内含本次五个字段。

### 6.2 每个字段的展示

- 字段名 + 输入控件
- 控件旁小字：`继承自全局` / `继承自账号` / `本会话`
- 未覆盖时控件显示**继承来的值**（灰字），改动后变黑
- 提供「恢复继承」按钮（把该键从 chat 层删掉）

### 6.3 旧昵称键提示

若 `chats` 或 `scopes.chat` 的键是昵称形态：
- 该行显示黄色提示：「这是昵称键，微信改名后会失效，建议重选」
- **不自动改写**，用户点保存才写 id

---

## 七、生效时机

**每轮重读**：处理消息时重新读一次 config.json。

理由：永不出"改了不生效"，代价是一次 JSON 解析（<1ms，`interval` 1.2s 轮询下可忽略）。

不做信号/事件通知——多一条链路就多一处可能失效。

---

## 八、验收（selftest 断言，不联网）

必须覆盖的分支：

1. **三层回退**：chat 有值 → 用 chat；chat 缺失 → 用 account；都缺 → 用 global
2. **三态**：`false` 覆盖 global 的 `true`；`null` 等同缺失；缺失继承
3. **空列表**：`quiet_hours: []` 覆盖 global 的非空列表（不被 `or` 吞掉）
4. **零值**：`max_reply_chars: 0` 不被当成缺失（边界）
5. **昵称别名**：昵称键能命中；解析不到时不命中且不抛异常
6. **id 优先**：同一会话同时被昵称和 id 命中时，id 赢
7. **旧路径兼容**：只有 `auto_reply.allow_self` 时，`resolve` 能读到
8. **新路径优先**：新旧都有且冲突时，新路径赢
9. **不写文件**：读取过程不产生任何文件写入（用 mtime 断言）
10. **explain 正确**：来源层标注与实际回退结果一致

---

## 九、明确不做

- 不做第四层（角色/人格）
- 不做 `say.*` / `listen.*` / `llm.*` 的下沉（下一轮）
- 不做配置变更通知
- 不做自动迁移写文件
- 不做跨账号会话合并

---

## 十、风险

| 风险 | 缓解 |
| --- | --- |
| `dict.get(k) or default` 吞掉 `[]` / `0` | 一律用 `k in d` 判断，selftest 第 3、4 条守 |
| 昵称解析需要微信在运行 | 解析失败视为未命中，不抛异常，UI 标提示 |
| 弹层改了但 UI 没刷新 | 保存后重新 `resolve` 一次并回填 |
| 旧配置里 `chats` 混着昵称和 id | 保持原样；只读别名层兼容，不强制统一 |
