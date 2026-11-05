# -*- coding: utf-8 -*-
"""共同账本 —— 我到底发过什么。

## 为什么还需要它

A-1 已经能说出"这条是我发的 / 是对方发的 / 拿不准"了。那账本还有什么用？

**它能否决 A。**

11-04 那晚不是"账本不够好"，是**判据判错了以后没有任何东西能按住它**。
A 说"这是对方说的"，于是要回复；回复又被读回来；再判成对方说的……
账本就是在这一步兜底：*你说是对方，可我 1 秒前正好发过一模一样的一句。*

## 一条线：不信"像不像"，信"我记没记过"

D-20 的教训是"回环判定不能看文字像不像"。所以这里不做相似度，
只做两个**离散**条件：

    ① 归一化后完全相等
    ② 或者：长度差 ≤ 10%，且前 12 个字相同

② 是为了容纳微信在写回时可能做的空格式改写。**10% 这条是护栏**：
它把"对方逐字复述她一整段话"挡在外面 —— 那正是 D-20 踩过的坑。

## 不持久化

和 D-9（水位不跨进程持久化）同一个理由：残留的旧账会误杀新消息。
进程重启就是一次新的对齐。

## 用法

    from bigfish.ledger import Ledger

    led = Ledger()

    h = led.note_sent(chat, text)      # ⚠ 发送【之前】记
    ok = sender.send(chat, text)
    if not ok:
        led.rollback(h)                # 没发出去，撤账
    else:
        led.confirm(h, local_id)       # 发出去了，回填库里那一行

    hit = led.lookup(chat, incoming_text)   # 这句我发过吗
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional

#: 默认时间窗。12 秒挡不住缓冲和慢网络；90 秒够宽，又不至于接到昨天的账。
DEFAULT_WINDOW = 90.0
#: 每个会话最多留多少条
DEFAULT_MAX_PER_CHAT = 200
#: 前缀比对取多少字
PREFIX_CHARS = 12
#: 长度差容忍度（相对较长者）
LENGTH_TOLERANCE = 0.10

_WS = re.compile(r"\s+")
#: 群里读回来时正文带的发送者前缀：`wxid_xxx: 内容`
_PREFIX_RE = re.compile(r"^(?:wxid_[A-Za-z0-9_\-]+):\s?")
#: 发送时自己加的 @前缀
_AT_RE = re.compile(r"^(?:@\S+\s*)+")


def normalize(text: object) -> str:
    """归一化：剥掉传输层的装饰，再把连续空白折叠成一个空格。

    剥两样东西，都是**传输留下的**、不属于内容：

      · 群里读回来时的 `wxid_xxx: ` 前缀
      · 发送时自己加的 `@某人 ` 前缀

    不剥的话，群里的回环判定整个失效（发出去的是正文，读回来多了前缀）。
    除此之外**不做**任何加工：不改标点、不做同义词、不转小写 ——
    每多做一步，就多一分把"对方的话"认成"我发的"的风险。
    """
    value = _PREFIX_RE.sub("", str(text or ""))
    value = _AT_RE.sub("", value)
    return _WS.sub(" ", value).strip()


def close_enough(a: str, b: str) -> bool:
    """两段归一化文本算不算"同一句"。

    两条任一：
      ① 完全相等
      ② 长度差 ≤ max(1, 10%)，且两边都够长（≥ PREFIX_CHARS），前 12 字相同

    `max(1, …)` 那一条是为短句留的：7 个字和 8 个字差 14%，
    按纯比例算会被判成两句，但它俩可能就是同一句。
    **短句不比前缀** —— 只差一两个字的时候，前 12 字根本没有区分力，
    拿它当依据只会误杀。
    """
    if not a or not b:
        return False
    if a == b:
        return True
    longer = max(len(a), len(b))
    if abs(len(a) - len(b)) > max(1.0, longer * LENGTH_TOLERANCE):
        return False
    if min(len(a), len(b)) < PREFIX_CHARS:
        return False
    return a[:PREFIX_CHARS] == b[:PREFIX_CHARS]


@dataclass(frozen=True)
class Entry:
    """账本里的一笔。"""

    chat: str
    text: str
    norm: str
    at: float
    local_id: int = 0
    epoch: int = 0


@dataclass(frozen=True)
class Handle:
    """`note_sent` 的收据，用于回滚或确认。

    复制一份而不是留索引：列表会被 `prune` 挪动，索引会失效。
    """

    chat: str
    norm: str
    at: float
    epoch: int


@dataclass
class Ledger:
    """纯内存账本。可注入时钟，不碰 IO。"""

    window: float = DEFAULT_WINDOW
    max_per_chat: int = DEFAULT_MAX_PER_CHAT
    prefix_chars: int = PREFIX_CHARS
    tolerance: float = LENGTH_TOLERANCE
    clock: Any = None

    _entries: Dict[str, List[Entry]] = field(default_factory=dict, repr=False)
    _epoch: int = field(default=0, repr=False)

    # ------------------------------------------------------------------
    def _now(self) -> float:
        return float(self.clock() if self.clock else __import__("time").time())

    def _entries_for(self, chat: str) -> List[Entry]:
        return self._entries.setdefault(str(chat or ""), [])

    # ------------------------------------------------------------------
    #  记
    # ------------------------------------------------------------------
    def note_sent(self, chat: object, text: object,
                  local_id: int = 0) -> Optional[Handle]:
        """记一笔"我要发这句"。**必须在真正发送之前调用。**

        晚于发送就等于没记 —— 11-04 那晚正是"记账晚于写库"，
        watcher 恰好在这一刻轮询，这条就没有账可对。
        """
        body = str(text or "")
        norm = normalize(body)
        if not norm:
            return None
        at = self._now()
        key = str(chat or "")
        bucket = self._entries_for(key)
        bucket.append(Entry(chat=key, text=body, norm=norm, at=at,
                            local_id=int(local_id or 0), epoch=self._epoch))
        if len(bucket) > max(1, self.max_per_chat):
            del bucket[: len(bucket) - self.max_per_chat]
        return Handle(chat=key, norm=norm, at=at, epoch=self._epoch)

    def rollback(self, handle: Optional[Handle]) -> bool:
        """撤销一笔（发送失败时）。

        不撤的话，对方隔一句把她的话原样说回来就会被误杀 ——
        留着一个"我没发成功"的账，比不记还糟。
        """
        if handle is None or handle.epoch != self._epoch:
            return False
        bucket = self._entries.get(handle.chat)
        if not bucket:
            return False
        for i in range(len(bucket) - 1, -1, -1):
            item = bucket[i]
            if item.norm == handle.norm and abs(item.at - handle.at) < 1e-6:
                del bucket[i]
                return True
        return False

    def rollback_text(self, chat: object, text: object) -> bool:
        """按文本撤账。给只拿得到文本的老调用方用（如 LoopGuard）。"""
        norm = normalize(text)
        if not norm:
            return False
        bucket = self._entries.get(str(chat or ""))
        if not bucket:
            return False
        for i in range(len(bucket) - 1, -1, -1):
            if bucket[i].norm == norm:
                del bucket[i]
                return True
        return False

    def confirm(self, handle: Optional[Handle], local_id: int) -> bool:
        """发送确认后回填库里那一行的 local_id。

        有了它，"这一行是我发的"就从"比文本"升级成"认主键"。
        """
        if handle is None or handle.epoch != self._epoch:
            return False
        bucket = self._entries.get(handle.chat)
        if not bucket:
            return False
        for i, item in enumerate(bucket):
            if item.norm == handle.norm and abs(item.at - handle.at) < 1e-6:
                bucket[i] = replace(item, local_id=int(local_id or 0))
                return True
        return False

    # ------------------------------------------------------------------
    #  查
    # ------------------------------------------------------------------
    def prune(self, now: Optional[float] = None) -> None:
        moment = self._now() if now is None else float(now)
        cutoff = moment - max(0.0, float(self.window))
        for key in list(self._entries):
            bucket = [e for e in self._entries[key] if e.at >= cutoff]
            if bucket:
                self._entries[key] = bucket
            else:
                self._entries.pop(key, None)

    def lookup(self, chat: object, text: object, *,
               local_id: int = 0) -> Optional[Entry]:
        """这句我发过吗？返回最近的一笔。

        先裁旧再查，所以窗口外的账不会命中 —— 那是"重启即失效"之外
        第二道防线：即使进程一直活着，旧账也不会无限期地杀人。
        """
        if local_id:
            for bucket in self._entries.values():
                for item in bucket:
                    if item.local_id and item.local_id == int(local_id):
                        return item
        norm = normalize(text)
        if not norm:
            return None
        self.prune()
        key = str(chat or "")
        bucket = list(self._entries.get(key, ()))
        # 兼容老的全局记账（chat 为空）：仍然参与匹配，但不跨会话传播
        if key:
            bucket += list(self._entries.get("", ()))
        for item in reversed(bucket):
            if close_enough(item.norm, norm):
                return item
        return None

    def seen(self, chat: object, text: object) -> bool:
        return self.lookup(chat, text) is not None

    # ------------------------------------------------------------------
    def reset(self) -> None:
        """清空，并让手上所有旧句柄失效。"""
        self._entries.clear()
        self._epoch += 1

    def snapshot(self) -> List[Dict[str, Any]]:
        """调试用：当前账目的扁平视图。"""
        out: List[Dict[str, Any]] = []
        for key, bucket in self._entries.items():
            for item in bucket:
                out.append({"chat": item.chat, "text": item.text,
                            "at": item.at, "local_id": item.local_id})
        out.sort(key=lambda x: x["at"])
        return out

    def __len__(self) -> int:
        return sum(len(b) for b in self._entries.values())
