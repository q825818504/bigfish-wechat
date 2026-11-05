# A-1 身份判据（谁说的这句话）

> 规格定稿：2026-11-05
> 状态：**已实施**（2026-11-05）
> 上游：P0-1 拆解 —— 自环 = **A 身份误判** + D 无共同账本。C（配置作用域）已让 A 有地方落脚。

---

## 一、要解决什么

现在的判据只有一行，在两个文件里各写了一遍：

```python
is_self = (sender == self._me) if sender else False
```

一个 bool、两个输入、没有第三个值。它有三种静默失手，**而且全都倒向"当成对方说的"**：

| 失手 | 后果 |
| --- | --- |
| `sender` 解析不出（空串） | 自己发的被当成对方 → 自环 |
| `self._me` 空串（`db.wxid` 读不到） | 每条都判成对方 → 全体误判 |
| 群里 `shard` 缺失 | 同上 |

"当成对方说的"意味着**要回复**，所以任何一次失手都是正反馈的起点。11-04 那晚就是这个形状。

另外三处已经分叉：`run_uia.py` 还停在 `allow_self and chat == "filehelper"`；
`app.py` 走 scopes；`app.py:365` 的发送确认 worker 又用同一个 `is_self` 判断"我发的那条入库了没有"。

---

## 二、决定（来自 7 轮问答）

| # | 决定 |
| --- | --- |
| 1 | **判错方向分场景**：私聊保守（宁可不回），群聊宽松（宁可多回） |
| 2 | **证据两源**：DB 的 `sender` 字段 + 群正文的 `wxid:` 前缀 |
| 3 | **每条判定留证据链**：记下用了哪条证据、结论是什么 |
| 4 | **`me` 读不到 → 拒绝启动**：不知道为什么不知道自己是谁的时候不要开口 |
| 5 | **私聊 unknown → 彻底静默**，只记日志 |
| 6 | **群聊 unknown → 当对方处理，但必须真的唤了她**（`at:` / `called:`） |
| 7 | **四处收口到一个模块**：`watcher` / `wechat` / `run_uia` / `app`+`desktop` |
| 8 | 证据链落**旁路 JSON**，只记"不干净"的判定，滚动保留 200 条（实现时定） |

---

## 三、判定模型

### 3.1 三态

```python
WHO_SELF    = "self"     # 确定是自己发的
WHO_PEER    = "peer"     # 确定是对方发的
WHO_UNKNOWN = "unknown"  # 拿不准
```

**不引入置信度分数。** 分数会诱使人去调参数，而这里没有可调的余地：
要么知道，要么不知道。判断依据用**证据列表**表达，可回溯、可审计。

### 3.2 判定顺序（`decide()`）

```
1. me 为空                     → UNKNOWN  rule=me_missing
2. sender 非空
   a. sender == me             → SELF     rule=sender_is_me
   b. sender != me             → PEER     rule=sender_is_other
3. sender 为空，尝试正文前缀
   a. 前缀是 wxid_… 形态
      · == me                  → SELF     rule=prefix_is_me
      · != me                  → PEER     rule=prefix_is_other
   b. 前缀不存在 / 不是 wxid 形态 → UNKNOWN  rule=sender_empty
```

**前缀必须严格**：只认 `^wxid_[A-Za-z0-9_-]+:`。不能用 `loopguard` 那个宽松正则
（`[A-Za-z0-9_@-]{4,}:`）—— 那个是为了剥前缀，`notes: 今天……` 也会被它吃掉。
判身份一旦把普通英文开头当成人名，就会造出新的误判。

### 3.3 放行（`admit()`）

| 结论 | 私聊 | 群聊 |
| --- | --- | --- |
| SELF | 只有 `allow_self` 才放行（自测通道） | 同左 |
| PEER | 放行 | 放行（后面还有 gate 的 @ 判定） |
| UNKNOWN | **不放行**（静默） | **只有 `woken` 才放行** |

`woken` = `GroupGate` 给出的 `at:…` 或 `called:…`。
`group-allow-all` 不算 woken —— 那是"这个群免检"，不是"有人叫她"。

### 3.4 `me` 缺失

`require_me(me)` 在启动时调用一次，缺失直接抛 `IdentityUnavailable`。
调用方（`app.py` 主入口、desktop 引擎、`run_uia`）把它变成一句人话并**拒绝启动**：

> 读不到自己账号的 wxid，没法判断哪条消息是自己发的。
> 这种情况下任何自环防护都失效，所以不启动监听。请先确认微信已登录、账号已选中。

---

## 四、模块

| 文件 | 职责 |
| --- | --- |
| `bigfish/identity.py`（新） | 纯函数：`decide()` / `admit()` / `require_me()`。**不碰 IO**，可注入。 |
| `bigfish/identity_trace.py`（新） | 旁路 JSON：只在结论不是确定值（或证据不足）时写一条。写失败静默。 |
| `tools/selftest_identity.py`（新） | 断言。 |

`Incoming` / `ChatMessage` 增加两个字段（不破坏现有 `is_self`）：

```python
identity: str = ""        # self / peer / unknown（空 = 未判定，旧数据）
identity_rule: str = ""   # 哪条规则得出的
```

`is_self` 保留，语义收紧为 `identity == WHO_SELF`。

---

## 五、接入点

| 位置 | 改什么 |
| --- | --- |
| `watcher.py:261` | 调 `identity.decide()` 填 identity 三字段 |
| `wechat.py:493` | 同上（这两处重复实现要合并） |
| `app.py:_accept` | 用 `identity.admit()` 取代 `if msg.is_self and not scope(...)` |
| `app.py:365` 确认 worker | 用 `identity == SELF` 判断"我那句话入库了" |
| `desktop.py` 引擎 | 同 `_accept`；`LoopGuard` 的 L1 继续作为第二层网 |
| `run_uia.py:129` | 收口到 `admit()`，删掉写死的 `filehelper` |

---

## 六、验收

1. `me` 为空 → UNKNOWN，`rule=me_missing`
2. `sender == me` → SELF
3. `sender != me` → PEER
4. `sender` 空 + 群前缀 `wxid_me: …` → SELF
5. `sender` 空 + 群前缀 `wxid_other: …` → PEER
6. `sender` 空 + 前缀不是 wxid（`notes: …`）→ UNKNOWN
7. `sender` 空 + 无前缀 → UNKNOWN
8. 私聊 UNKNOWN → 不放行
9. 群聊 UNKNOWN + woken（`at:` / `called:`）→ 放行
10. 群聊 UNKNOWN + `group-allow-all` → **不**放行
11. SELF + `allow_self=False` → 不放行
12. SELF + `allow_self=True` → 放行
13. PEER → 放行（两种场景）
14. `require_me("")` 抛 `IdentityUnavailable`；非空原样返回
15. 证据链：每条 Verdict 至少一条 evidence；rule 非空
16. 旁路只写不干净的判定：SELF/PEER 不写盘，UNKNOWN 写
17. 旁路写失败不影响返回（注入一个会抛的 writer）
18. 滚动保留：超过上限删最旧

---

## 七、明确不做

- 不引入分数/置信度
- 不做"用内容相似度反推发送者"（那是 D 的账本，不是 A）
- 不改 `group_gate` 的 @ 判定逻辑（只消费它的 reason）
- 不动 `is_self` 的类型（仍是 bool），只新增字段

---

## 八、实施记录（2026-11-05）

### 落地

| 文件 | 改动 |
| --- | --- |
| `bigfish/identity.py`（新） | 纯函数三态判定 + 放行 + `require_me`。不碰 IO。 |
| `bigfish/identity_trace.py`（新） | 旁路证据链，只记不干净的判定，滚动 200 份。 |
| `tools/selftest_identity.py`（新） | 58 条断言。 |
| `bigfish/watcher.py` | 判据收口；`Incoming` 加 `identity` / `identity_rule`。 |
| `bigfish/wechat.py` | 同上；`ChatMessage` 加两字段。两处重复实现合并。 |
| `bigfish/app.py` | 启动闸 + `_accept` 三态放行 + 确认 worker 改问 `WHO_SELF`。 |
| `bigfish/desktop.py` | 启动闸 + 引擎准入 + 群闸门后的 woken 复核。 |
| `bigfish/run_uia.py` | 收口到 `admit()`，删掉写死的 filehelper 判据（自测通道语义保留）。 |
| `docs/身份判定.md`（新） | 面向用户。 |

### 实施时发现的第三个问题

`tools/selftest_app_wiring.py` 里那条「自己的消息不入批」**一直是假通过**：
它断言 `batcher.pending()` 为空，而 `listen.feed()` 自己就有一道
`if msg.is_self: return None` —— 那条断言其实一直在测 listen 层，
`_accept` 里的归属判断从来没被覆盖到，改坏了也不会红。

已改成直接断言 `_accept(...) is None`，并补了「sender 为空」「正常消息能过」两条。
测试从 15 条变 17 条。

### 验证

```
selftest_identity    58/58   （新）
selftest_app_wiring  17/17   （15 → 17，且修正了测错层的问题）
其余 16 个自检全绿
```

变异测试（故意改坏，确认断言咬得住）：

| 改坏什么 | 抓到 |
| --- | --- |
| sender 为空时当成对方（事故原判据） | 18 |
| me 缺失时当成对方 | 2 |
| 免检群也算"叫她" | 4 |
| UNKNOWN 私聊也放行 | 5 |
| 用宽松前缀正则（把 `notes:` 当人名） | 2 |
| `_accept` 把拿不准当对方（接线层） | 1 |

### 没做的

- 没给界面加"判定记录"面板（数据已经在落，面板以后再说）
- `listen.feed()` 里那道 `if msg.is_self` 保留着 —— 现在是第二层网，
  不是主要防线；删掉反而少了纵深
