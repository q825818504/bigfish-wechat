# -*- coding: utf-8 -*-
"""自环防护 —— 她不该回自己。

## 事故

2026-11-04 线上：大肥鱼在跟「老爸」私聊里，连续两分钟自问自答十几条，
停不下来。日志里她刚发出去的那句，1 秒后以「收 老爸：<同一句>」的
形态回来，她当成对方说的话又回了一遍。

查下来是四个问题叠在一起：

1. **`allow_self = True` 留在生产配置里**。
   这是文件传输助手自测用的开关，本意是「允许回应我自己发的消息」。
   它当时是全局的，于是老爸的私聊也变成了"我自己发的也算数"。

2. **记账晚于写库**。
   `send()` 里 UIA 按完回车就返回，微信异步写数据库；
   而 `remember_sent()` 在 `send()` 返回之后才执行。
   数据库先落地、watcher 恰好在这一刻轮询，这条就没有账可对 ——
   被当成对方说的。窗口很窄（1.2 秒轮询），但聊一晚上必然撞上，
   撞上一次就进入正反馈：她的回复又变成一条新的待读消息。

3. **没有轮级熔断**。
   前两层都是"精确识别"，一旦漏了就没有任何机制兜底。

4. **防护埋在 `_run()` 的闭包里**。
   4500 行的函数，`recently_sent` / `text_is_echo` / `loop_streak`
   全是局部变量，写不了测试 —— 所以这次事故没有任何用例能提前发现。

## 现在的做法

抽成独立状态机，四层闸门，任何一层单独失手都不会成环：

    L0 静音       熔断后该会话在一段时间内完全不处理
    L1 白名单     allow_self 只对 filehelper 生效（自测通道）
    L2 乐观记账   发送前先记账，失败回滚 —— 消灭竞态窗口
    L3 轮级熔断   连续 N 轮自动回复、中间没有对方消息 → 静音

L3 的判据选得很死，不会误伤：一次分条发送是**一轮**（几个 part 算一轮），
人类聊天不可能连续四轮回复之间一句对方的话都没有。

## 用法

    g = LoopGuard(allow_self=cfg.get("allow_self", False))
    v = g.admit(msg)              # 这条消息要不要处理
    if not v.allow:
        log(v.detail); return
    ...
    for part in parts:
        g.note_sent(part)         # 先记账
        r = sender.send(chat, part)
        if not r.ok:
            g.rollback_sent(part) # 没发出去，撤账
    if g.note_reply(msg.chat_id): # 这一轮回复记上；返回 True = 刚熔断
        log("自环熔断")
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

#: 允许「回应自己发的消息」的会话 —— 只有自测通道。
#: 加会话要三思：这一条就是这次事故的开关。
SELF_CHAT_IDS = frozenset({"filehelper"})

#: 读回来的群消息前缀：`wxid_xxx: ` 或 `12345678@chatroom: `
_PREFIX_RE = re.compile(r"^[A-Za-z0-9_@-]{4,}:\s?")
#: 自己加的 @：`@昵称 @昵称2 `
_AT_RE = re.compile(r"^(@\S+\s*)+")


def normalize(text: str) -> str:
    """归一化后再比对。

    读回来的文本可能比发出去的多两样东西：
      - 群消息的 `wxid_xxx: ` 前缀
      - 发送时自己加的 `@昵称 `
    不剥掉的话回环识别在群里会整个失效（实测漏拦过）。
    """
    v = (text or "").strip()
    v = _PREFIX_RE.sub("", v)
    v = _AT_RE.sub("", v)
    return v.strip()


@dataclass
class Verdict:
    """一条消息的放行结论。"""

    allow: bool
    reason: str = ""      # 机器可读的原因码：muted / self / echo / ""
    detail: str = ""      # 给人看的一句话，直接进日志

    def __bool__(self) -> bool:      # 方便 `if guard.admit(m):`
        return self.allow


@dataclass
class LoopGuard:
    """自环防护状态机。纯 Python，不碰 IO，可注入时钟。"""

    #: 允许回应自己消息吗（还要求会话在 self_chat_ids 里）
    allow_self: bool = False
    #: 哪些会话允许（自测通道）
    self_chat_ids: frozenset = SELF_CHAT_IDS

    #: 已发文本的记账窗口
    echo_window: float = 12.0
    echo_max: int = 20
    #: 前缀匹配的最短长度。太松会误杀正常对话
    #: （「那还是叫大肥鱼吧」和「那还是叫大肥鱼」开头一样）。
    echo_prefix: int = 12

    #: 连续多少轮自动回复（中间无对方消息）判定为自环
    trip: int = 4
    #: 判定自环后静音多久
    mute: float = 120.0

    #: 注入时钟，测试用
    clock: object = None

    _sent: List[Tuple[str, float]] = field(default_factory=list, repr=False)
    _streak: Dict[str, int] = field(default_factory=dict, repr=False)
    _mute: Dict[str, float] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------
    def _now(self) -> float:
        return float(self.clock() if self.clock else time.time())

    # ------------------------------------------------------------------
    # 记账
    # ------------------------------------------------------------------
    def note_sent(self, text: str) -> None:
        """记一条「我刚发出去的话」。

        ⚠ 必须在 `sender.send()` **之前**调用。见模块开头的第 2 条。
        """
        t = (text or "").strip()
        if not t:
            return
        self._sent.append((t, self._now()))
        if len(self._sent) > self.echo_max:
            del self._sent[:-self.echo_max]

    def rollback_sent(self, text: str) -> None:
        """撤销一条记账（发送失败时）。留着会误杀对方的原样复述。"""
        key = normalize(text)
        if not key:
            return
        for i in range(len(self._sent) - 1, -1, -1):
            if normalize(self._sent[i][0]) == key:
                del self._sent[i]
                return

    def _prune(self, now: float) -> None:
        cutoff = now - self.echo_window
        while self._sent and self._sent[0][1] < cutoff:
            self._sent.pop(0)

    def is_echo(self, text: str) -> bool:
        """这段文字是不是我们刚发出去的那条。

        ⚠ 单独调它没有意义 —— `admit()` 里只在 msg.is_self 为真时才用它。
          对方原样复述她的话是聊天，不是回环。
        """
        self._prune(self._now())
        t = normalize(text)
        if not t:
            return False
        for sent_text, _ in self._sent:
            n = normalize(sent_text)
            if not n:
                continue
            if t == n:
                return True
            k = min(len(n), len(t))
            if k >= self.echo_prefix and (t.startswith(n) or n.startswith(t)):
                return True
        return False

    # ------------------------------------------------------------------
    # 闸门
    # ------------------------------------------------------------------
    def muted_for(self, chat: str) -> float:
        """这个会话还剩多少秒静音（0 = 没静音）。"""
        until = self._mute.get(str(chat or ""), 0.0)
        left = until - self._now()
        return left if left > 0 else 0.0

    def admit(self, msg) -> Verdict:
        """这条消息要不要处理。

        只读 msg 的 `chat_id` / `text` / `is_self` 三个字段，
        所以 `Incoming`、`BatchMessage`、测试里的假对象都能传。
        """
        chat = str(getattr(msg, "chat_id", "") or "")
        text = str(getattr(msg, "text", "") or "").strip()

        # L0 静音
        left = self.muted_for(chat)
        if left > 0:
            return Verdict(False, "muted",
                           f"自环熔断静音中，还剩 {int(left)}s")

        if not bool(getattr(msg, "is_self", False)):
            # 真·对方说话：自环链条断了
            self._streak[chat] = 0
            return Verdict(True)

        # L1 白名单
        if not (self.allow_self and chat in self.self_chat_ids):
            return Verdict(False, "self", "自己发的，跳过")

        # L2 内容比对
        if self.is_echo(text):
            return Verdict(False, "echo", "读到自己刚发的，跳过")

        return Verdict(True)

    # ------------------------------------------------------------------
    def note_reply(self, chat: str) -> bool:
        """记一轮自动回复。返回 True 表示**刚刚**触发熔断。

        一轮 = 一条完整回复（可能分 2~4 条发出去），不是每个 part 记一次。
        """
        chat = str(chat or "")
        n = self._streak.get(chat, 0) + 1
        self._streak[chat] = n
        if n >= self.trip:
            self._mute[chat] = self._now() + self.mute
            self._streak[chat] = 0
            return True
        return False

    def note_peer(self, chat: str) -> None:
        """收到对方的消息 —— 自环链条断了，计数清零。

        `admit()` 里已经顺手清了；单独留一个公开入口，
        给「消息被别的闸门拦掉但确实是对方说的」那种场景用。
        """
        self._streak[str(chat or "")] = 0

    def un_mute(self, chat: str) -> None:
        """手动解除静音（界面上「继续聊」按钮用）。"""
        self._mute.pop(str(chat or ""), None)

    # ------------------------------------------------------------------
    def snapshot(self) -> Dict[str, object]:
        """当前状态，进日志/自检用。"""
        return {
            "allow_self": self.allow_self,
            "sent": len(self._sent),
            "streak": dict(self._streak),
            "muted": {k: round(self.muted_for(k), 1) for k in list(self._mute)},
        }
