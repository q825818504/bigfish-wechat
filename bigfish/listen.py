"""[bigfish] 倾听层：等对方说完，再回。

为什么要有这一层
================

`watcher` 每 1.2s 轮询一次，来一条就 `on_message` → 一次完整 turn。
于是对方连发三句：

    20:31:00  大肥鱼，你在不在
    20:31:01  吃饭了没
    20:31:02  我今天去长坂坡了

第一句到达时就调了模型，2 秒后回复发出去——那时对方第二句还没说完。
这不是"回得快"，是**插话**：对方的意思还没表达完，这边已经答了半截。

真人的做法是：听。等对方停下来（或者明显在等回应）再开口。

所以这一层只做一件事：**把逐条消息攒成一批，等安静了再交给下游**。
它不判断"该不该回"（那是 GroupGate 的事），也不生成内容（那是 replier 的事）。

设计约束
--------

  · **纯状态机，不起线程**。`feed()` 进、`tick()` 出，时间由调用方给。
    这样单测可以直接跳时间，不用 sleep。
  · **线程安全**。watcher 线程 `feed()`，定时器线程 `tick()`。
  · **硬上限**。`max_wait` 到了必须封批——对方一直发表情也不能无限等。
  · **封批后立刻清空**。同一会话的下一批从零开始计时。
  · **自己的消息不进批**（`is_self`）。否则她会回自己。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

#: 判"纯图片/表情"用的类型名。与 `wechat.py` 的 `msg_type` 对齐。
MEDIA_TYPES = frozenset({"图片", "表情", "语音", "视频", "文件", "动画表情"})


@dataclass(frozen=True)
class ListenPolicy:
    """等多久、等几条。默认值融合了 MaiBot 的 natural 档与微信的实际节奏。"""

    #: 同一人继续发：重置计时（他说完一句可能停 1s 再补一句）
    quiet_same_author: float = 2.0
    #: 换人说话：通常新话题，等短一点
    quiet_different_author: float = 0.8
    #: 被 @ / 叫名字：对方明确在叫你，别让他等
    quiet_mention: float = 1.0
    #: 纯图/表情：可能在连发图，多等一会
    quiet_media: float = 4.0
    #: 从第一条算起的硬上限：再吵也要回一句
    #
    # ⚠ 2026-11-08 从 12 提到 30：用户要求「最久不能超过 30 秒」。
    #   注意这个上限只在**判定说"还有下一句"**时才会真正用到 ——
    #   正常情况 6 秒左右就封批了。它守的是"对方一直分条发、模型一直说还有"
    #   那种极端情况，免得她永远不回。
    max_wait: float = 30.0
    #: 一批最多几条；到顶立即封批
    max_batch: int = 8
    #: 单条最短间隔（同一会话 0.3s 内的重复推送视为同一条）
    min_gap: float = 0.3

    # ── 自适应窗口（v0.10.5）──
    #
    # 固定的 quiet_same_author 有个根本问题：**短消息和长消息的"说完程度"不一样**。
    #
    #   「在吗」         12 字以内，极可能还有下文 → 该多等
    #   「我今天去长坂坡了，看到了樱花」  一句话说完 → 不用等
    #   「那我先去吃饭了」  」结尾没标点 → 大概还有一句
    #   「嗯。」          句号收尾 → 说完了
    #
    # AstrBot 的 private_debounce 就是这么做的（companion_v2/conversation）：
    # 拿字数当"完整度"的代理，句末标点微调。它的原文：
    #   "a long message is usually a finished thought, while a very short
    #    one is often the first bubble of a multi-part message"
    #
    #: 短消息/没说完时的等待上限
    #
    # ⚠ 2026-11-08 用户实测反馈：4 秒仍然太短 ——
    #   「我稍微犹豫一点发，她就已经回复了」。真人发微信经常是
    #   「在吗」→ 想 3 秒 → 「问你个事」→ 又停 4 秒 → 正文。
    #   4 秒会把这种节奏切成两轮，她回一次，对方再发，又回一次。
    #   6 秒覆盖绝大多数"犹豫一下再补一句"。长句/有句号的消息仍走
    #   quiet_min（1.5s），所以说完一句的回复速度没变慢。
    quiet_max: float = 6.0
    #: 长消息/明确说完时的等待下限
    quiet_min: float = 1.5

    #: 关掉自适应就退回固定 quiet_same_author（老行为）
    adaptive: bool = True

    # ── 回复后冷却（2026-11-08 加）──
    #
    # 她刚回完一句，对方接着说 —— 那是**同一个话题在继续**，不该马上再回一次。
    # 以前这种情况按普通窗口（4 秒）走：对方隔 5 秒补一句，她就再回一条，
    # 两个人变成一问一答地刷屏（用户原话：「没有将它合并看待」）。
    # 现在：她说完之后 `post_reply_grace` 秒内到达的消息，等满这个冷却再封批。
    post_reply_grace: float = 6.0

    # ── 攒批判定：到点问一次模型「他说完了吗」（0.11.1）──
    #
    # 固定窗口只能猜。用户的原话：
    #   「最短是 6 秒，然后超过 6 秒的时候，需要模型判断是否对方可能
    #     还有下一句话没说完，如果有继续等待，最久不能超过 30 秒」。
    #
    # 所以流程变成：安静窗口到点（≤ quiet_max）→ 问模型 →
    #   more=true  再等 judge_extra 秒（最多问 judge_max_rounds 次）
    #   more=false 立即封批
    # 硬上限仍是 max_wait（30 秒），模型说多少次"还有"也越不过它。
    #
    # ⚠ 判定需要 `MessageBatcher(judge=...)` 传入执行器才会启用；
    #   不传就是老行为（到点直接封批），测试和命令行都走那条路。
    #: 最短等待。不管自适应算出多少，**都不能早于这个时间封批**。
    #: 用户原话：「底线限制，最短是 6 秒」。0 = 不设下限（只走自适应）。
    batch_min_wait: float = 6.0
    judge_enabled: bool = True
    #: 判定"还有下一句"之后，再等多久
    judge_extra: float = 8.0
    #: 一批最多问几次（防止对方一直发、模型一直说还有，把预算烧光）
    judge_max_rounds: int = 3
    #: 单次判定超时。超时按"说完了"处理 —— 让人等一句不值得
    judge_timeout: float = 6.0


#: 句末标点：说完了
_END_COMPLETE = ("。", "！", "？", "!", "?", "\n", "~", "～")
#: 句中停顿：明显还有下文
_END_PARTIAL = ("，", ",", "、", "；", ";", "：", ":", "…", "...", "—")


def visible_chars(text: str) -> int:
    """非空白字符数。

    不按 len() 算 —— 空白和纯标点不该让一句「在吗」显得像长句。
    最少返回 1，避免全空白时除零。
    """
    return max(1, sum(1 for c in str(text or "") if not c.isspace()))


def adaptive_quiet(text: str, policy: "ListenPolicy") -> float:
    """这条消息之后该安静等多久。

    算法照 AstrBot 的 `quiet_seconds()`：在 [quiet_min, quiet_max] 里取一个值，
    字数越多等越短（长句通常是完整想法），再按句尾标点微调。

    ⚠ 不要把它写死成"一律等 4 秒" —— 对方说完整一段话时等 4 秒是呆。
      也不要一律等 1.5 秒 —— 「在吗」之后那句「吃饭了没」就会被插话。
    """
    floor = max(0.0, float(policy.batch_min_wait or 0.0))
    if not policy.adaptive:
        return max(policy.quiet_same_author, floor)

    lo, hi = policy.quiet_min, policy.quiet_max
    if hi <= lo:
        return lo

    span = hi - lo
    # 60 字以上算"说完了"：再长也不减了，避免长文回得比短消息还急
    ratio = min(1.0, visible_chars(text) / 60.0)
    quiet = hi - span * ratio

    raw = str(text or "").rstrip()
    if raw.endswith(_END_COMPLETE):
        quiet -= 0.2          # 句号/问号收尾 → 说完了，少等
    elif raw.endswith(_END_PARTIAL):
        quiet += 0.3          # 逗号/省略号收尾 → 还有下文，多等
    else:
        quiet += 0.2          # 完全没标点 → 大概率还有一句，多等

    # 下限最后夹一次：`batch_min_wait` 是用户设的"最短等到几秒"，
    # 比自适应上限还长时以它为准（他明确要求等更久）。
    return max(floor, min(hi, max(lo, quiet)))


@dataclass
class BatchMessage:
    """进批的一条消息。字段与 `wechat.ChatMessage` 对齐，但不依赖它。"""

    chat_id: str
    author: str
    text: str
    chat_name: str = ""
    local_id: int = 0
    msg_type: str = "文本"
    at_bot: bool = False
    is_self: bool = False
    received_at: float = 0.0

    @property
    def sender_name(self) -> str:
        """给 `build_prompt` 用：它按 Incoming 的字段名取发送人。"""
        return self.author

    @property
    def is_bare_media(self) -> bool:
        return self.msg_type in MEDIA_TYPES and not (self.text or "").strip()


@dataclass
class Batch:
    """封批结果。`reason` 用来解释"为什么现在回"，日志和调试都要看它。"""

    chat_id: str
    messages: List[BatchMessage]
    reason: str
    waited: float
    at: float

    @property
    def authors(self) -> List[str]:
        seen: List[str] = []
        for m in self.messages:
            if m.author not in seen:
                seen.append(m.author)
        return seen


@dataclass
class _Bucket:
    messages: List[BatchMessage] = field(default_factory=list)
    first_at: float = 0.0
    last_at: float = 0.0
    deadline: float = 0.0
    reason: str = "quiet"
    #: 已经问过几次「他说完了吗」。对方再发消息会清零。
    judged: int = 0
    #: 正在等判定结果（后台线程在跑）。期间 tick 不许抢封。
    awaiting: bool = False


class MessageBatcher:
    """按会话攒批。`feed()` 进消息，`tick()` 取到期批次。"""

    def __init__(
        self,
        policy: Optional[ListenPolicy] = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        allow_self_for: Optional[Callable[[str], bool]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        judge: bool = False,
    ) -> None:
        self.policy = policy or ListenPolicy()
        self._clock = clock
        self._allow_self_for = allow_self_for
        self._on_log = on_log
        #: 是否启用「到点问一次模型」。判定本身在调用方的后台线程里跑
        #: （见 desktop 的 `_judge_one`），这里只记一个开关 ——
        #: 攒批器保持纯状态机，不自己发 HTTP。
        #: 默认 False：命令行入口和自检走老行为（到点直接封批）。
        self._judge = bool(judge)
        self._lock = threading.Lock()
        self._buckets: Dict[str, _Bucket] = {}
        #: 上一次为哪个会话打过「自己发的消息被读回来」的日志（monotonic 时刻）
        self._self_skip_at: Dict[str, float] = {}

    def _note_self_skip(self, msg: BatchMessage) -> None:
        """她刚发出去的那句被 watcher 读回来 —— 正常闭环，不要每条都喊。

        为什么会有这条：她发完 → 微信异步写库（实测 10~12 秒）→ watcher 轮询
        读到 → 进 feed。这时 `is_self=True`，**必须丢掉**，否则她会回自己
        （2026-11-04 那次自问自答就是这样起来的）。

        以前每个会话每次都打一行，「跳过」和上一条「收」挨在一起，
        看起来像"对方的消息被跳过了"（2026-11-08 用户当场这么问）。
        现在同一会话 120 秒内只打第一行，并且带上她的原话 ——
        真出问题（她自己给自己发）时仍然看得见。
        """
        if not self._on_log:
            return
        now = self._clock()
        last = self._self_skip_at.get(msg.chat_id)
        if last is not None and now - last < 120.0:
            return
        self._self_skip_at[msg.chat_id] = now
        text = (msg.text or "").strip().replace("\n", " ")[:24]
        who = msg.chat_name or msg.chat_id
        self._on_log(
            f"[跳过] {who}：她自己刚发的「{text}」被读回来，属正常闭环"
            f"（allow_self 未开，不再重复提示）")

    # ------------------------------------------------------------------
    def feed(self, msg: BatchMessage,
             replied_at: Optional[float] = None) -> Optional[Batch]:
        """收一条消息。返回非 None 表示这一批已经满了、需要立刻处理。

        `replied_at` 是"她上一次在这个会话说话"的 monotonic 时刻 ——
        传进来才会启用回复后冷却（见 ListenPolicy.post_reply_grace）。
        """
        if msg.is_self:
            # 自己的消息默认不进批（否则她会回自己）。只有 allow_self
            # 明确放行的会话（filehelper 自测通道）才继续。
            allowed = bool(self._allow_self_for
                           and self._allow_self_for(msg.chat_id))
            if not allowed:
                self._note_self_skip(msg)
                return None
        now = self._clock()
        at = msg.received_at or now

        with self._lock:
            bucket = self._buckets.get(msg.chat_id)
            if bucket is None:
                bucket = _Bucket(first_at=at, last_at=at)
                self._buckets[msg.chat_id] = bucket

            # 同一会话 0.3s 内的重复推送（水位回退/重连）不重复入批
            if bucket.messages and at - bucket.last_at < self.policy.min_gap:
                if msg.local_id and any(m.local_id == msg.local_id for m in bucket.messages):
                    return None

            prev_author = bucket.messages[-1].author if bucket.messages else ""
            # 首条没有「上一位」，按同一人算：独立一条消息也要等满 quiet_same_author，
            # 否则「大肥鱼」刚到就 0.8s 开跑，后面那句「你在不在」还是会被插话。
            same_author = (not prev_author) or msg.author == prev_author
            bucket.messages.append(msg)
            bucket.last_at = at

            # 等待时长按「这条消息的性质」决定：
            #   同一个人继续发 → 自适应窗口（字数 + 句尾标点，见 adaptive_quiet）
            #   换人说话       → 短等（新话题）
            #   被 @           → 缩短（对方明确在叫你）
            #   纯图           → 延长（可能在连发图）
            # 后两者优先于自适应：那是明确的信号，不该被字数盖过去。
            if same_author:
                quiet = adaptive_quiet(msg.text, self.policy)
            else:
                quiet = self.policy.quiet_different_author
            if msg.at_bot:
                quiet = min(quiet, self.policy.quiet_mention)
            elif msg.is_bare_media:
                quiet = max(quiet, self.policy.quiet_media)

            # 她刚说完话：对方接着说的这一串算同一话题，等满冷却再封批。
            # 被 @ 时不加 —— 那是明确在叫她，让人等着才奇怪。
            if (not msg.at_bot) and replied_at:
                since = at - float(replied_at)
                grace = float(self.policy.post_reply_grace or 0.0)
                if 0 <= since < grace:
                    quiet = max(quiet, grace - since)

            deadline = at + quiet
            # 硬上限：永远不超过第一条 + max_wait
            deadline = min(deadline, bucket.first_at + self.policy.max_wait)
            bucket.deadline = deadline
            bucket.reason = "mention" if msg.at_bot else ("media" if msg.is_bare_media else "quiet")
            # 对方又说话了：上一轮判定的结论作废，重新计次。
            # 不重置的话，"他说完了" 的旧结论会按住后面新来的消息。
            bucket.awaiting = False
            bucket.judged = 0

            if len(bucket.messages) >= self.policy.max_batch:
                return self._seal_locked(msg.chat_id, bucket, "batch_full", now)
        return None

    # ------------------------------------------------------------------
    def _should_judge(self, bucket: _Bucket, now: float) -> bool:
        """到点了，但还该不该问一次模型「他说完了吗」。"""
        # 没接线（命令行入口 / 自检 / 老调用方）就一律不问，到点直接封。
        # ⚠ 这一行不能少：`judge` 默认 False，漏了它就会变成
        #   "谁都没接判定，但谁都不敢封批" —— 对方等 30 秒才收到回复。
        if not self._judge:
            return False
        p = self.policy
        if not p.judge_enabled:
            return False
        if bucket.judged >= max(0, int(p.judge_max_rounds)):
            return False
        # 顶到硬上限就别问了 —— 问完也没时间等
        if now >= bucket.first_at + float(p.max_wait) - 1e-6:
            return False
        return True

    def tick(self, now: Optional[float] = None) -> List[Batch]:
        """取所有到期的批次。调用方应每 100~200ms 调一次。"""
        now = self._clock() if now is None else now
        out: List[Batch] = []
        with self._lock:
            for chat_id in list(self._buckets):
                bucket = self._buckets[chat_id]
                if not bucket.messages:
                    self._buckets.pop(chat_id, None)
                    continue
                if now >= bucket.deadline:
                    # 正在等判定 / 还该问一次模型：别抢封，交给 due() + verdict()
                    if bucket.awaiting or self._should_judge(bucket, now):
                        continue
                    reason = bucket.reason
                    if now - bucket.first_at >= self.policy.max_wait - 1e-6:
                        reason = "max_wait"
                    out.append(self._seal_locked(chat_id, bucket, reason, now))
        return out

    # ------------------------------------------------------------------
    def due(self, now: Optional[float] = None) -> List[Tuple[str, List[BatchMessage]]]:
        """到点、且还该问模型的批次。**只报名，不阻塞。**

        调用方（desktop 的 `_batch_loop`）拿到之后要放到后台线程里跑
        `judge(chat_id, messages)`，再回来调 `verdict()`。
        这里顺手把桶标成 awaiting —— 所以同一个会话不会有两个判定在飞。
        """
        now = self._clock() if now is None else now
        out: List[Tuple[str, List[BatchMessage]]] = []
        with self._lock:
            for chat_id, bucket in list(self._buckets.items()):
                if not bucket.messages or bucket.awaiting:
                    continue
                if now < bucket.deadline:
                    continue
                if not self._should_judge(bucket, now):
                    continue
                bucket.awaiting = True
                out.append((chat_id, list(bucket.messages)))
        return out

    def verdict(self, chat_id: str, more: bool,
                now: Optional[float] = None) -> Optional[Batch]:
        """判定结果回来了：还有下一句就再等，说完了就立刻封批。

        返回非 None 表示这一批可以处理了。
        """
        now = self._clock() if now is None else now
        with self._lock:
            bucket = self._buckets.get(chat_id)
            if bucket is None or not bucket.awaiting:
                # 判定期间对方又说话了（feed 已把 awaiting 清掉）：旧结论作废
                return None
            bucket.awaiting = False
            if not more:
                return self._seal_locked(chat_id, bucket, "judged", now)

            bucket.judged += 1
            cap = bucket.first_at + float(self.policy.max_wait)
            nxt = min(now + max(1.0, float(self.policy.judge_extra)), cap)
            if nxt <= now + 1e-6:
                return self._seal_locked(chat_id, bucket, "max_wait", now)
            bucket.deadline = nxt
            bucket.reason = "judged_wait"
            if self._on_log:
                self._on_log(
                    f"[倾听] 判定他还有下一句，再等 {nxt - now:.0f} 秒"
                    f"（第 {bucket.judged} 次，上限 {self.policy.max_wait:.0f} 秒）")
            return None

    def set_policy(self, policy: ListenPolicy) -> None:
        """换一份策略。面板改完立即生效，已攒的批次不受影响。"""
        with self._lock:
            self.policy = policy

    def _seal_locked(self, chat_id: str, bucket: _Bucket, reason: str, now: float) -> Batch:
        batch = Batch(
            chat_id=chat_id,
            messages=list(bucket.messages),
            reason=reason,
            waited=max(0.0, now - bucket.first_at),
            at=now,
        )
        bucket.messages = []
        bucket.first_at = 0.0
        bucket.last_at = 0.0
        bucket.deadline = 0.0
        bucket.reason = "quiet"
        bucket.judged = 0
        bucket.awaiting = False
        self._buckets.pop(chat_id, None)
        return batch

    # ------------------------------------------------------------------
    def flush_all(self, now: Optional[float] = None) -> List[Batch]:
        """强制封批（退出前/切号前调用），不等 deadline。"""
        now = self._clock() if now is None else now
        out: List[Batch] = []
        with self._lock:
            for chat_id in list(self._buckets):
                bucket = self._buckets[chat_id]
                if bucket.messages:
                    out.append(self._seal_locked(chat_id, bucket, "forced", now))
        return out

    def pending(self) -> Dict[str, int]:
        """调试用：每个会话当前攒了几条。"""
        with self._lock:
            return {k: len(v.messages) for k, v in self._buckets.items() if v.messages}

    def drop(self, chat_id: str) -> None:
        """丢掉某会话的未处理批次（例如进入安静时段）。"""
        with self._lock:
            self._buckets.pop(chat_id, None)


def batch_transcript(batch: Batch, *, with_time: bool = True) -> str:
    """把一批消息拼成「带说话人的时间线」，喂给模型用。

    不做简单拼接：模型需要看到「同一个人连说了三句」，
    而不是「三条独立消息」——后者会让她分别回三次。
    """
    lines: List[str] = []
    for m in batch.messages:
        text = (m.text or "").strip()
        if not text:
            continue
        if with_time:
            stamp = time.strftime("%H:%M", time.localtime(m.received_at or batch.at))
            lines.append(f"[{stamp}] {m.author or '对方'}：{text}")
        else:
            lines.append(f"{m.author or '对方'}：{text}")
    return "\n".join(lines)
