"""[bigfish] 说法层：分条 + 节奏 + 发送确认。

为什么要有这一层
================

微信不是「一次输出」的界面，是「一条一条说」的界面。现在的 `app.py` 是：

    reply = call_model(...)          # 一次生成 40~80 字
    sender.send(chat, reply)         # 一次塞进一个气泡

两个后果：

  · **一段话压成一条**。真人会先说「知道了」，再说「那明天去长坂坡吧」，
    再说「先看樱花再看枣树」。分三条，是因为**每说一句都在等对方反应**，
    不是因为她把一段话切碎了发。
  · **发出去没有回执**。UIA 的 `SendResult.ok` 只证明「回车按下去了」，
    不证明「微信收下了」。连发时第一条还没落库就发第二条，输入框里的
    残留会和下一条拼在一起——这就是「只发了后半句」的来源。

所以这一层做三件事，彼此独立、都能单独关：

  1. `split_reply()`  把一段话切成 1~N 条（纯函数，无 IO）
  2. `plan_pacing()`  给每条算一个发送前的等待（纯函数，确定性抖动）
  3. `send_parts()`   发一条、确认一条、再发下一条（IO 全部注入，可假测）

设计约束
--------

  · **不改写文本**。「明天就去长坂坡游玩」→「那明天去长坂坡玩吧」是模型的
    语言能力，不是分条器的。分条器只负责切；改写靠 prompt 里的
    「一次只说一件事」和模型自己给的 `{{split}}` 标记。
  · **不 import 微信**。这一层只认两个鸭子类型：`send(chat, text)` 与
    `read_recent(chat, limit)`。`tools/selftest_say.py` 用假对象跑，
    不需要微信、不需要 exe、不需要桌面。
  · **确认分级**。L1 = UIA 回车成功；L2 = 数据库出现自己的新行；
    L3 = L2 且内容对得上。L3 不通过只告警，不阻断——微信会改写文本
    （换行变空格、emoji 变 `[表情]`、长文拆条），拿它当门槛会误伤。
  · **确认与节奏并行**。发完第 N 条立刻开始「下一条的等待」，同时轮询
    确认；两者重叠，总时长 = Σ gap，确认不额外占时间。
"""
from __future__ import annotations

import difflib
import hashlib
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, List, Optional, Sequence, Tuple

# ══════════════════════════════════════════════════════════════════════
# 一、分条
# ══════════════════════════════════════════════════════════════════════

#: 占位符用的控制字符：正文里不会出现，切分时不会被标点规则误伤。
_PH = "\x00{}\x00"

#: 允许独立成条的短应答。分条器不判断语义，只认这张表——
#: 「好」「嗯」独立发是真人习惯；「然后」独立发是切错了。
STANDALONE = frozenset({
    "知道了", "知道", "好", "好的", "行", "嗯", "嗯嗯", "在", "在的",
    "收到", "哈哈", "哈哈哈", "是的", "对啊", "对", "没事", "晚安",
    "早安", "早", "睡了", "来了", "我来了", "稍等", "等下",
})

#: 需要整体保护的片段（按顺序匹配，先匹配到的先占位）。
_PROTECT_PATTERNS: Tuple[Tuple[str, str], ...] = (
    ("code_block", r"```.*?```"),
    ("inline_code", r"`[^`\n]{1,200}`"),
    ("url", r"https?://[^\s，。！？、）)]+"),
    ("at", r"@[^\s，。！？、,:：;；]{1,20}"),
    # 括号内不含中文的短片段 = 颜文字/表情，例如 (｡･ω･｡)、(╯°□°）╯
    ("kaomoji", r"[（(][^\u4e00-\u9fff\n]{1,16}[)）]"),
    # 数字+单位：避免「3 天」被空格或逗号切开
    ("qty", r"\d+(?:\.\d+)?\s*(?:天|号|点|分|秒|岁|个|次|块|元|月|年|小时|分钟|公里|米)"),
)

_SENTENCE_END = re.compile(r"[。！？!?…～~；;\n]+")
_COMMA = re.compile(r"[，,、]")

#: 成对引号/括号。单引号故意不收：英文撇号（don't）会把它弄乱。
_OPENERS = {
    "「": "」", "『": "』", "“": "”", "‘": "’",
    "（": "）", "(": ")", "《": "》", '"': '"',
}


def _quote_mask(text: str) -> List[bool]:
    """标记每个字符是否在成对引号/括号内。切分时跳过这些位置的标点。

    未闭合的引号会把它后面的内容全标成 True —— 保守，宁可整段不切，
    也不要把别人引用的原话拦腰截断。
    """
    mask = [False] * len(text)
    stack: List[str] = []
    for i, ch in enumerate(text):
        if stack:
            mask[i] = True
            if ch == stack[-1]:
                stack.pop()
        elif ch in _OPENERS:
            stack.append(_OPENERS[ch])
            mask[i] = True
    return mask


def _ends_with_punct(s: str) -> bool:
    return bool(s) and s[-1] in "，,、。！？!?…～~；;：: \n"


@dataclass(frozen=True)
class SplitPolicy:
    """分条参数。默认值按微信私聊调，群聊可以调小 max_parts。"""

    #: 最多几条。超过就合并最短的相邻对，不是截断。
    max_parts: int = 4
    #: 单条上限（字符）。超过在最近的逗号/空格处再切。
    part_max_chars: int = 50
    #: 短于这个长度的段并入前段（白名单除外）。
    min_chars: int = 3
    #: 段超过这个长度才允许按逗号切。短段按逗号切会碎成「知道了」「明天」。
    #: 15 是实测值：8 字的「我知道了，没事」不该拆；27 字的「知道了，明天去…」该拆。
    comma_split_over: int = 15
    #: 模型可用的边界标记（单独一行或行内都认）。
    marker: str = "{{split}}"
    #: 是否保护颜文字/URL/@/代码。
    protect: bool = True
    #: 引号/括号内的标点不切（别人引用的原话不该被截断）。
    protect_quotes: bool = True
    #: 允许独立成条的短应答；传空集合表示关闭白名单。
    standalone: frozenset = STANDALONE


def _protect(text: str) -> Tuple[str, List[str]]:
    """把不可拆片段换成占位符，返回 (替换后文本, 原片段表)。"""
    saved: List[str] = []

    def repl(match: re.Match[str]) -> str:
        saved.append(match.group(0))
        return _PH.format(len(saved) - 1)

    out = text
    for _name, pattern in _PROTECT_PATTERNS:
        out = re.sub(pattern, repl, out, flags=re.S)
    return out, saved


def _restore(text: str, saved: Sequence[str]) -> str:
    def repl(match: re.Match[str]) -> str:
        index = int(match.group(1))
        return saved[index] if 0 <= index < len(saved) else match.group(0)

    return re.sub(r"\x00(\d+)\x00", repl, text)


def _split_by_marker(text: str, marker: str) -> List[str]:
    """按模型给的边界切。标记可独占一行，也可行内。"""
    if not marker or marker not in text:
        return []
    parts = [p.strip() for p in text.split(marker)]
    return [p for p in parts if p]


def _split_sentences(text: str, *, protect_quotes: bool = True) -> List[str]:
    """按句末标点切，标点留在段尾。引号内的标点不算边界。"""
    mask = _quote_mask(text) if protect_quotes else None
    out: List[str] = []
    buf = ""
    for i, char in enumerate(text):
        buf += char
        if mask is not None and mask[i]:
            continue
        if _SENTENCE_END.fullmatch(char) or char == "\n":
            # 连续标点（……、？！）跟着同一段走，不要生成以孤立标点开头的条
            out.append(buf)
            buf = ""
    if buf.strip():
        out.append(buf)
    # 合并「只有标点」的段到前一段（"……" 不该独立成条）
    merged: List[str] = []
    for seg in out:
        if merged and not re.sub(r"[\s。！？!?…～~；;，,、]+", "", seg):
            merged[-1] += seg
        else:
            merged.append(seg)
    return merged



def _split_on_commas(seg: str, *, protect_quotes: bool = True) -> List[str]:
    """按每一个逗号切，标点留在段尾。引号内的逗号不切。"""
    mask = _quote_mask(seg) if protect_quotes else None
    out: List[str] = []
    buf = ""
    for i, char in enumerate(seg):
        buf += char
        if mask is not None and mask[i]:
            continue
        if char in "，,、":
            out.append(buf)
            buf = ""
    if buf.strip():
        out.append(buf)
    return out



def _split_long(seg: str, limit: int) -> List[str]:
    """超长段在最近的逗号/空格处切；找不到就硬切。"""
    if len(seg) <= limit:
        return [seg]
    out: List[str] = []
    rest = seg
    while len(rest) > limit:
        window = rest[:limit]
        cut = -1
        for match in _COMMA.finditer(window):
            cut = match.end()
        if cut <= 0:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        out.append(rest[:cut])
        rest = rest[cut:].lstrip()
    if rest:
        out.append(rest)
    return out


def _merge_short(segs: List[str], policy: SplitPolicy) -> List[str]:
    """过短的段并入前段；白名单里的短应答保留独立。"""
    out: List[str] = []
    for seg in segs:
        bare = seg.strip()
        if not bare:
            continue
        keep_alone = bare.strip("，,、。！？!?…～~；; ") in policy.standalone
        if out and len(bare) < policy.min_chars and not keep_alone:
            out[-1] = out[-1].rstrip() + bare
        else:
            out.append(bare)
    return out


def _balanced_groups(segs: List[str], max_parts: int) -> List[str]:
    """按字符数把相邻段合成 max_parts 组，让每组长短接近。

    不用「合并最短相邻对」：那会产出「开头 2 字 + 尾巴 50 字」，
    读起来像一句话被硬塞进两条气泡。
    """
    if max_parts <= 0:
        return ["".join(segs)] if segs else []
    if len(segs) <= max_parts:
        return list(segs)
    total = sum(len(s) for s in segs)
    target = total / max_parts
    out: List[str] = []
    current = ""
    acc = 0
    groups_left = max_parts
    for i, seg in enumerate(segs):
        if current and not _ends_with_punct(current):
            current += "，"
            acc += 1
        current += seg
        acc += len(seg)
        remaining = len(segs) - i - 1
        need = groups_left - 1
        if acc >= target and need > 0 and remaining >= need:
            out.append(current)
            current, acc = "", 0
            groups_left -= 1
    if current:
        out.append(current)
    return out


def _cap_parts(segs: List[str], max_parts: int) -> List[str]:
    """兼容旧名。"""
    return _balanced_groups(segs, max_parts)



def split_reply(text: str, policy: Optional[SplitPolicy] = None) -> List[str]:
    """把一段回复切成 1~max_parts 条。

    优先级：模型标记 > 句末标点 > 逗号（仅长段）> 硬切。
    不做任何改写——改写是模型的事，这一层只切。
    """
    policy = policy or SplitPolicy()
    raw = (text or "").strip()
    if not raw:
        return []

    protected, saved = _protect(raw) if policy.protect else (raw, [])

    # 1) 模型给了边界就听模型的
    parts = _split_by_marker(protected, policy.marker)

    # 2) 没给就按句子切
    if not parts:
        parts = _split_sentences(protected, protect_quotes=policy.protect_quotes)

    # 3) 长段再切：超过阈值且有逗号 → 按逗号全切；仍超长 → 硬切
    expanded: List[str] = []
    for seg in parts:
        if len(seg) > policy.comma_split_over and _COMMA.search(seg):
            candidates = _split_on_commas(seg, protect_quotes=policy.protect_quotes)
        else:
            candidates = [seg]
        for cand in candidates:
            expanded.extend(_split_long(cand, policy.part_max_chars))

    # 4) 短段并入、5) 条数收敛
    merged = _merge_short(expanded, policy)
    capped = _cap_parts(merged, policy.max_parts)

    # 6) 恢复占位符、清理空白
    out = [_restore(p, saved).strip() for p in capped]
    # 段尾的逗号/顿号去掉：微信里「知道了，」看着像话没说完。
    # 句号/问号/感叹号保留 —— 它们本来就是语气的一部分。
    out = [re.sub(r"[，,、]+$", "", p).strip() for p in out]
    return [p for p in out if p]


# ══════════════════════════════════════════════════════════════════════
# 二、节奏
# ══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class PacePolicy:
    """每条发送前的等待。默认值来自实测：0.8s 以下像连发，3.5s 以上像卡住。"""

    #: length = 按字数次线性增长（推荐）；fixed = 固定；random = 区间随机
    mode: str = "length"
    base: float = 0.8
    per_char: float = 0.09
    floor: float = 0.8
    cap: float = 3.5
    #: 抖动比例（0.2 = ±20%），用 sha256(seed+i) 生成，同 seed 可复现
    jitter: float = 0.2
    #: 所有间隔之和的上限；超了等比压缩（否则对方早走了）
    total_cap: float = 12.0
    #: 第一条之前的「思考」延迟
    first_min: float = 0.6
    first_max: float = 1.5
    first_per_char: float = 0.02
    seed: str = ""


def _jitter(seed: str, index: int, ratio: float) -> float:
    """确定性抖动：同 seed + 同 index 永远得到同一个系数。"""
    if ratio <= 0:
        return 1.0
    digest = hashlib.sha256(f"{seed}:{index}".encode("utf-8")).digest()
    unit = int.from_bytes(digest[:8], "big") / ((1 << 64) - 1)  # 0..1
    return 1.0 + (unit * 2.0 - 1.0) * ratio


def plan_pacing(parts: Sequence[str], policy: Optional[PacePolicy] = None) -> List[float]:
    """给每条算发送前的等待秒数（第 0 条是思考延迟）。

    与确认并行：调用方应在「发送后立刻开始计时」，而不是「确认完再计时」。
    """
    policy = policy or PacePolicy()
    if not parts:
        return []

    gaps: List[float] = []
    for i, part in enumerate(parts):
        n = max(1, len(part or ""))
        if i == 0:
            gap = policy.first_min + policy.first_per_char * n
            gap = min(policy.first_max, max(policy.first_min, gap))
        elif policy.mode == "fixed":
            gap = policy.base
        elif policy.mode == "random":
            gap = policy.floor + (policy.cap - policy.floor) * (
                int.from_bytes(hashlib.sha256(f"{policy.seed}:{i}".encode()).digest()[:8], "big")
                / ((1 << 64) - 1)
            )
        else:  # length：次线性，短句不秒回、长句不拖沓
            gap = policy.base + policy.per_char * (n ** 0.8)
        gap = min(policy.cap, max(policy.floor, gap))
        gap *= _jitter(policy.seed, i, policy.jitter)
        gaps.append(round(max(0.0, gap), 3))

    total = sum(gaps)
    if policy.total_cap > 0 and total > policy.total_cap:
        scale = policy.total_cap / total
        gaps = [round(g * scale, 3) for g in gaps]
    return gaps


# ══════════════════════════════════════════════════════════════════════
# 三、发送确认（L1 / L2 / L3）
# ══════════════════════════════════════════════════════════════════════

@dataclass
class SendOutcome:
    """一次「分条发送」的结果。"""

    ok: bool
    level: str                 # L3 / L2 / L1 / failed / unconfirmed
    parts_sent: int
    total_parts: int
    detail: str = ""
    confirmed_text: str = ""
    mismatches: List[int] = field(default_factory=list)
    #: L1 ok 但库里没在 confirm_timeout 内出现的条号（strict_confirm=false 时继续发）
    unconfirmed: List[int] = field(default_factory=list)
    #: 最后一条异步确认的待办：(发送文本, 发送前 local_id 基线)
    pending: Optional[Tuple[str, int]] = None

    @property
    def complete(self) -> bool:
        return self.ok and self.parts_sent == self.total_parts


def _strip_at(text: str) -> str:
    """剥掉群聊里的 `@昵称 ` 前缀，用于内容比对。"""
    return re.sub(r"^@[^\s，。！？、,:：;；]{1,20}\s*", "", (text or "").strip())


def _normalize(text: str) -> str:
    """比对用的归一化：去 @、去空白、去零宽字符。"""
    s = _strip_at(text)
    s = s.replace("\u200b", "").replace("\ufeff", "")
    return re.sub(r"\s+", "", s)


def similar(sent: str, stored: str, *, ratio: float = 0.75) -> bool:
    """发送文本与库里文本是否算同一条。

    微信会改写：换行变空格、emoji 变 `[表情]`、长文拆条。所以
    「前 8 字包含」优先，「difflib 相似度」兜底，两者任一成立即算对上。
    """
    a, b = _normalize(sent), _normalize(stored)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 8 and a[:8] in b:
        return True
    if len(b) >= 8 and b[:8] in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= ratio


def _msg_field(msg: Any, name: str, default: Any = None) -> Any:
    """同时支持 dataclass 对象与 dict（reader 两种返回都有）。"""
    if isinstance(msg, dict):
        return msg.get(name, default)
    return getattr(msg, name, default)


def send_parts(
    send: Callable[[str, str], Any],
    read_recent: Callable[[str, int], Sequence[Any]],
    chat: str,
    parts: Sequence[str],
    gaps: Sequence[float],
    *,
    confirm_timeout: float = 3.0,
    poll_interval: float = 0.25,
    strict_confirm: bool = False,
    final_async_confirm: bool = False,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    log: Optional[Callable[[str], None]] = None,
    on_confirmed: Optional[Callable[[int, Any], None]] = None,
) -> SendOutcome:
    """逐条发送，每条至少确认到 L2 才发下一条。

    参数全部注入，便于单测：
      send(chat, text) -> 有 .ok 属性的对象，或 bool
      read_recent(chat, limit) -> 最近消息（需 .is_self / .local_id / .content）
      clock / sleep -> 假时钟可让测试瞬间跑完
    """
    say = log or (lambda _m: None)
    parts = list(parts)
    if not parts:
        return SendOutcome(True, "L3", 0, 0, "nothing-to-send")

    sent = 0
    mismatches: List[int] = []
    unconfirmed: List[int] = []
    last_level = "L1"
    last_confirmed = ""

    for index, part in enumerate(parts):
        # 基线必须在发送前取：发送后再取，自己这条已经在里面了
        base_msgs = list(read_recent(chat, 1))
        base_id = max((int(_msg_field(m, "local_id", 0) or 0) for m in base_msgs), default=0)

        result = send(chat, part)
        ok = bool(_msg_field(result, "ok", result))
        if not ok:
            detail = str(_msg_field(result, "detail", "") or "")
            say(f"say: part-{index + 1} L1-failed {detail}".strip())
            return SendOutcome(False, "failed", sent, len(parts),
                               detail or "send returned ok=False", last_confirmed,
                               mismatches, unconfirmed)

        is_last = index == len(parts) - 1
        if is_last and final_async_confirm:
            sent += 1
            say(f"say: part-{index + 1} deferred-confirm（最后一条不等落库）")
            return SendOutcome(True, "L1", sent, len(parts),
                               "last part deferred to background confirm",
                               last_confirmed, mismatches, unconfirmed,
                               pending=(part, base_id))

        sent_at = clock()
        # 最后一条没有「下一条」要等，只等确认；中间的 gap 同时充当确认窗口
        gap = float(gaps[index]) if index < len(parts) - 1 and index < len(gaps) else 0.0
        deadline = sent_at + max(gap, confirm_timeout)
        confirmed = None

        # 确认与节奏并行：gap 在走，轮询也在走
        while True:
            if confirmed is None:
                for msg in read_recent(chat, 6):
                    if not bool(_msg_field(msg, "is_self", False)):
                        continue
                    if int(_msg_field(msg, "local_id", 0) or 0) <= base_id:
                        continue
                    confirmed = msg
                    break
            now = clock()
            if confirmed is not None and now >= sent_at + gap:
                break
            if now >= deadline:
                break
            sleep(poll_interval)

        if confirmed is None:
            if strict_confirm:
                say(f"say: part-{index + 1} unconfirmed（strict，停止后续，等了 {max(gap, confirm_timeout):.1f}s）")
                return SendOutcome(False, "unconfirmed", sent + 1, len(parts),
                                   "L1 ok but no is_self row within confirm_timeout",
                                   last_confirmed, mismatches, unconfirmed)
            # L1 已经证明回车按下去了；落库慢不该让话说一半。
            unconfirmed.append(index + 1)
            sent += 1
            say(f"say: part-{index + 1} unconfirmed（L1 ok，继续发）")
            continue

        stored = str(_msg_field(confirmed, "content", "") or "")
        if similar(part, stored):
            last_level = "L3"
        else:
            last_level = "L2"
            mismatches.append(index + 1)
            say(f"say: part-{index + 1} content-mismatch（库中={stored[:24]!r}）")
        last_confirmed = stored
        sent += 1
        if on_confirmed is not None:
            on_confirmed(index, confirmed)

    return SendOutcome(True, last_level, sent, len(parts),
                       "all parts confirmed", last_confirmed, mismatches, unconfirmed)


def send_text(
    send: Callable[[str, str], Any],
    read_recent: Callable[[str, int], Sequence[Any]],
    chat: str,
    text: str,
    *,
    split_policy: Optional[SplitPolicy] = None,
    pace_policy: Optional[PacePolicy] = None,
    **kwargs: Any,
) -> SendOutcome:
    """一步到位：切 → 算节奏 → 逐条确认发送。"""
    parts = split_reply(text, split_policy)
    gaps = plan_pacing(parts, pace_policy)
    return send_parts(send, read_recent, chat, parts, gaps, **kwargs)


def split_reply_with_llm(
    text: str,
    llm: Callable[[str], str],
    *,
    policy: Optional[SplitPolicy] = None,
    min_chars: int = 60,
) -> List[str]:
    """模型没给 marker、且文本够长时，让模型自己断一次；失败回退规则切分。

    `llm(text) -> str`：返回用换行分隔的短句。**默认不启用**——
    多一次模型调用，延迟和成本都翻倍，只在「回复长且没标记」时值得。
    """
    policy = policy or SplitPolicy()
    raw = (text or "").strip()
    if not raw:
        return []
    if policy.marker in raw or len(raw) <= min_chars:
        return split_reply(raw, policy)
    try:
        out = llm(raw) or ""
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if len(lines) == 1 and policy.marker in lines[0]:
            lines = [p.strip() for p in lines[0].split(policy.marker) if p.strip()]
        if len(lines) >= 2:
            lines = _merge_short(lines, policy)
            lines = _balanced_groups(lines, policy.max_parts)
            return [p for p in lines if p]
    except Exception:
        pass
    return split_reply(raw, policy)
