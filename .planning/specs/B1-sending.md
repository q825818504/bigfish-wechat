# B-1 发送信封（发没发出去，不靠猜）

> 规格定稿：2026-11-05
> 状态：**已实施**（2026-11-05）
> 上游：P0-1 拆解里的 B。A/D 已完成 —— 那晚 B 其实没出错（消息确实发了），
> 但"发没发"一直是靠一个 bool 和一堆异常猜的。

---

## 一、现状

```python
class SendResult:
    ok: bool

raise SendError(msg, kind="...")     # 28 处
```

- **成功**：`ok=True`
- **失败**：抛 `SendError`，`kind` 只用来写日志

也就是说：**"确定没发出去"和"不知道发没发"共用同一个异常。**

而底层库其实早就把这个信号埋好了，只是没人读：

```python
# vendor/uia_driver.py:1291
try:
    # SendKeys 抛异常也可能已经投递回车，调用方不能据 False 重新发送。
    self._send_attempted = True
    e.SendKeys("{Enter}", waitTime=0.05)
except Exception:
    return False
```

`_send_attempted` 在**按回车之前**置真，返回 False 后仍然保留。
`guia.py` 里已经有完整先例（2054/2107/2118/2126 行都在查它并拒绝重发），
只有 `wechat_sender.UiaSender` 这条路没接。

---

## 二、决定（来自 4 轮问答）

| # | 决定 |
| --- | --- |
| 1 | **按异常发生的步骤划界线**：前置检查失败 = 确定没发；回车前后不可分辨 = 未知 |
| 2 | 未知时**停住**，后面的分条不发 |
| 3 | 未知时**保留账**（可能已发出，读回来要能认出来） |
| 4 | 未知**告诉用户**，不是只在日志里一行 |
| 5 | 本轮**只做发送信封**，不清查全仓库的静默异常 |

---

## 三、模型

```python
STATE_SENT    = "sent"       # 确定发出去了
STATE_FAILED  = "failed"     # 确定没发出去
STATE_UNKNOWN = "unknown"    # 不知道（回车可能已投递）
```

### 3.1 判定表

| 情况 | 结论 | 依据 |
| --- | --- | --- |
| `send_text_to` 返回 True | SENT | 库自己说成功 |
| 返回 False 且 `_send_attempted` 为真 | **UNKNOWN** | 回车可能已经投递 |
| 返回 False 且 `_send_attempted` 为假 | FAILED | 卡在回车前（内容没落进输入框） |
| 抛异常且 `_send_attempted` 为真 | **UNKNOWN** | 同上 |
| 抛 `SendError(kind=...)` 且 kind 不是 `send` | FAILED | 前置检查：桌面锁 / 窗口没了 / 打不开会话 / 名字不匹配 / 空内容 |
| 其它意外异常 | UNKNOWN | 不知道发生在哪一步，按最坏算 |

**默认偏向 UNKNOWN**：当一个错误无法定位到"回车之前"时，一律按未知处理。
理由：把"可能发了"当成"没发"，代价是重复发送或漏防自环；
把"没发"当成"可能发了"，代价只是这一轮不再往下发。

### 3.2 未知的后果（三条同时成立）

1. **不重试**（这一轮就此为止）
2. **不撤账**（账本保留，读回来能认）
3. **告诉用户**：日志一行 + 界面/控制台一条明确提示

---

## 四、落地

| 文件 | 改动 |
| --- | --- |
| `bigfish/sending.py`（新） | 三态常量 + `Outcome` + `classify()` + 人话 |
| `bigfish/wechat_sender.py` | `SendResult.attempted`；`SendError.attempted`；send 尾部读取 `_send_attempted` |
| `bigfish/say.py` | `SendOutcome.state`；未知时停住、**不撤账**、回传 state |
| `bigfish/app.py` | 未知时打一条明确日志；仍是"停住不发后续" |
| `bigfish/desktop.py` | 同上，并 emit 到界面 |
| `tools/selftest_sending.py`（新） | 断言 |

---

## 五、验收

1. `ok=True` → SENT
2. `ok=False` + attempted=True → UNKNOWN
3. `ok=False` + attempted=False → FAILED
4. `SendError(kind="send")` → UNKNOWN
5. `SendError(kind="desktop"/"window"/"open"/"identity"/"empty")` → FAILED
6. 未知的 kind → UNKNOWN（保守）
7. 非 SendError 的异常 → UNKNOWN
8. 只有 FAILED 才允许撤账；UNKNOWN **不撤**
9. UNKNOWN 的 `retryable` 为 False
10. 人话文案能区分三种（用户看得懂"可能已发出"）
11. `say.send_parts`：UNKNOWN 时停止后续分条，且 `outcome.state == "unknown"`
12. `say.send_parts`：FAILED 时撤账（账本里没有这一条）

---

## 六、明确不做

- 不清查 `desktop.py` 的 33 处静默异常
- 不改 `group_gate` / `identity` / `ledger`
- 不做自动重试（未知永远不重试）
- 不做"事后回查库里有没有"（那需要更多往返，且 UIA 写库延迟不确定）

---

## 七、实施记录（2026-11-05）

### 落地

| 文件 | 改动 |
| --- | --- |
| `bigfish/sending.py`（新） | 三态常量 + `Outcome` + `classify()` + 人话 |
| `bigfish/wechat_sender.py` | `SendResult.attempted` / `SendError.attempted`；send 尾部读 `_send_attempted` |
| `bigfish/say.py` | `SendOutcome.state` / `.unknown`；三态处理，未知**不撤账** |
| `bigfish/app.py` | 未知时打一条明确提示（不是"失败"） |
| `bigfish/desktop.py` | 同上，emit 到界面日志 |
| `tools/selftest_sending.py`（新） | 22 条断言 |
| `tools/selftest_say.py` | 65 → 71（新增三态集成 6 条） |

### 关键发现：底层早就埋好了分界线

`vendor/uia_driver.py` 里：

```python
try:
    # SendKeys 抛异常也可能已经投递回车，调用方不能据 False 重新发送。
    self._send_attempted = True
    e.SendKeys("{Enter}", waitTime=0.05)
except Exception:
    return False
```

`_send_attempted` 在**按回车之前**置真，返回 False 后仍然保留。
`guia.py`（另一个驱动）里已经完整用了它（2054/2107/2118/2126 行都在查它并拒绝重发），
只有 `wechat_sender.UiaSender` 这条路没接。**不是缺能力，是没接线。**

### 判定表（最终）

| 情况 | 结论 |
| --- | --- |
| `ok=True` | SENT |
| `ok=False` + `attempted=True` | UNKNOWN |
| `ok=False` + `attempted=False` | FAILED |
| `SendError(kind="send")` | UNKNOWN |
| `SendError(kind∈前置集合)` 且 `attempted=False` | FAILED |
| 认不出的 kind / 普通异常 | UNKNOWN（保守） |

### 验证

```
selftest_sending   22/22  （新）
selftest_say       65 → 71
其余 18 个自检 + Qt 全绿
变异测试            4 处改坏，分别被抓 7/2/2/1 条
```

### 没做

- 没清查 `desktop.py` 的 33 处静默异常（本次范围外）
- 没做"事后回查库里有没有"（UIA 写库延迟不确定，回查结论不可靠）
