"""[bigfish] 主动开口 —— 她想起你的时候，会真的发一条过来。

═══════════════════════════════════════════════════════════════════════
这是整个项目风险最高的一块
═══════════════════════════════════════════════════════════════════════

0.4.1 那次自环事故（她跟自己聊到天亮）之后，优先级第一条就是
「能自己跑飞的问题，比回得好不好更重要」。

主动开口正好是"能自己跑飞"的典型：它是**唯一一条没有对方消息作为起点**
的发送路径。一旦它出问题，没有人能靠"不发消息"来止损。

所以这里的设计原则只有一条：

    **宁可一整天不发，也不能发出一条不该发的。**

具体到实现：
  · 六道硬闸门在最前面，任何一道不过就直接返回，不生成、不调模型
  · 全本地判定，只有**过了全部闸门**才调一次模型
  · 发送复用 `sending` 三态 + `loopguard` 记账 + 独立 outbox 记录，
    结果未知**绝不重发**
  · 每天条数上限、最小间隔、时段窗口三重限制

═══════════════════════════════════════════════════════════════════════
六个来源
═══════════════════════════════════════════════════════════════════════

    未完话头  他说「等我忙完」之后再没说话 —— 隔一段时间她回来接
    她自己的生活 她手头的事变了，值得说一句
    心情      她今天的情绪有明显变化，想说
    时段      刚醒的早上、准备睡的晚上
    重要日期  生日、纪念日（配置里手填，不猜）
    好久没说话 36 小时以上没对话

每个源都是**本地算分**，不调模型。分数过线才进入复核。

═══════════════════════════════════════════════════════════════════════
发出去之后：追问 or 忘记
═══════════════════════════════════════════════════════════════════════

真人发了一条没人回，有两种反应，而且两种都常见：

    追问   隔几小时再问一句（"在忙？"）—— 只追一次，追完就不追了
    忘记   就这么过去了，下次说话时像没发生过

所以这里不是"必追"也不是"必不追"，是**掷一次骰子**，
概率由关系和心情决定：关系近、心情好 → 更可能追问；
累、心情差、关系一般 → 更容易忘。

追问只允许一次（`followup_used`）。追完不管有没有回，都不再追。
忘了的那条会进 `dropped`，留着给"下次说话时要不要提"用 —— 但不主动提。

═══════════════════════════════════════════════════════════════════════
素材从哪来
═══════════════════════════════════════════════════════════════════════

**只能来自已经发生的事实**：`life.doing`、`mood.lastReason`、
`history` 里的原话、`config.important_dates`。

不允许在提示词里写"你可以自由发挥"。这是 D-36 在主动层的落地 ——
她主动说的事，事后必须能在某个文件里查到出处。
"""
from __future__ import annotations

import json
import os
import random
import re
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

STATE_NAME = "proactive.json"
STATE_VERSION = 1

# ══════════════════════════════════════════════════════════════════════
# 闸门参数
# ══════════════════════════════════════════════════════════════════════

#: 一天最多几条。默认 3 —— 1 条不像人，像每日提醒。
#: 这是**硬上限**，实际会比它少（掷骰 + 时机因子会压掉大半）。
DEFAULT_PER_DAY = 3
MAX_PER_DAY = 6

#: 两条主动之间至少隔多久（分钟）。
#: 90 分钟配合一天 1 条是合理的；一天 3 条还留 90，就永远用不满。
MIN_GAP_MINUTES = 45

#: 多久掷一次骰子（分钟）。
#:
#: ⚠ 这一条不能省。MIN_GAP 只在上次**发出去**之后才生效 —— 没掷中的时候
#:   它是 0，引擎每分钟都会重掷一次。结果是开机十分钟就把一天的额度发完，
#:   然后一整天沉默。掷了就记，不管中没中。
#:
#: 120 分钟 = 窗口内一天 6~7 次机会。这个数比"命中率"更能决定节奏：
#: 20 次机会时，哪怕单次只有 6%，加上没中加成也会天天打满额度。
ROLL_INTERVAL_MINUTES = 120

#: 允许发送的窗口（小时，24 小时制浮点）
WINDOW_START = 9.0
WINDOW_END = 22.5

#: 最近多少分钟内对方说过话 → 算"正在聊"，不发
BUSY_MINUTES = 30

#: 她精力低于这个值就不主动（累到不想说话）
MIN_ENERGY = 22.0

#: 分数下限。低于它命中线为 0 —— 连掷骰都不掷。
#:
#: ⚠ 这个数现在是**概率的地基**，不是开关。以前写成"过 55 就发"，
#:   于是同一个分数永远同一个结果，也不再有 55 分以下的来源（这正是
#:   life_share 和夜间问候长期失效的原因）。
SCORE_THRESHOLD = 40.0
#: 命中率曲线的三个折点。
#:
#: 40 分以下完全没机会；40–75 是"日常想说话"的区间，命中率从 0 缓升到 12%；
#: 75 分以上是"有话头没接 / 今天是他生日"，一路升到 60%。
#:
#: 一天 6~7 次机会，日常段 25% 起步，配没中加成，期望约 2 条。
#: 方差是故意的：有时一条、有时三条、偶尔一条都没有 —— 那才像人。
HIT_BASE_MID = 75.0
HIT_BASE_CEIL = 92.0
HIT_MID_RATE = 0.25
HIT_HIGH_RATE = 0.70

#: 命中线总上限。永远留 5% 的"今天不说了"。
HIT_MAX = 0.95

#: 连着没中一次加多少 / 最多加到多少
MISS_BONUS_STEP = 0.08
MISS_BONUS_CAP = 0.32

#: 时机因子：刚发过 → 更难；你正在聊 → 更难；冷太久 → 更容易
TIMING_JUST_SENT_MINUTES = 20.0
TIMING_JUST_SENT_FACTOR = 0.15
TIMING_RECENT_SENT_MINUTES = 45.0
TIMING_RECENT_SENT_FACTOR = 0.40
TIMING_CHATTING_MINUTES = 90.0
TIMING_CHATTING_FACTOR = 0.50
TIMING_COLD_HOURS = 6.0
TIMING_COLD_FACTOR = 1.40

#: 记忆里的待办：多久之前的算"还记得"，多久之后的算"已经过去了"
RECALL_MIN_DAYS = 1.5
RECALL_MAX_DAYS = 10.0

#: 什么算"待发生的事"。命中这些词的事实才会被捞出来问一句。
#:
#: 刻意选得窄：宁可漏掉，也不要拿"他喜欢吃辣"当由头去问
#: （那不是待办，那是画像，问出来像查户口）。
RECALL_KEYWORDS = (
    "体检", "复查", "手术", "看病", "挂号", "住院", "牙", "打针", "疫苗",
    "面试", "考试", "答辩", "笔试", "复试", "录取",
    "入职", "离职", "搬家", "出差", "旅行", "出发", "报名", "签约",
    "开庭", "见客户", "谈", "面谈", "生日", "结婚", "纪念日", "约会",
)

#: 「没事也想说一句」：什么条件下才会出现
WHIM_MIN_ENERGY = 45.0        # 累到不想动的时候不该想起谁
WHIM_MIN_HOURS = 20.0         # 两次之间至少隔这么久，否则它最烦人
WHIM_SCORE = 60.0

# ══════════════════════════════════════════════════════════════════════
# 来源参数
# ══════════════════════════════════════════════════════════════════════

#: 未完话头：他最后一句里有这些词，之后没再说话
OPEN_LOOP_PATTERNS = re.compile(
    r"(等我|回头|晚点|一会儿|等会|明天|下周|改天|先这样|我去忙|先忙|待会说|"
    r"回来再说|稍后|等我一下|马上回来)")

#: 未完话头：隔多久才去接（小时）。太短像催，太长已经忘了。
OPEN_LOOP_MIN_HOURS = 2.0
OPEN_LOOP_MAX_HOURS = 30.0

#: 她自己的生活：距上次分享至少多久（小时）
LIFE_SHARE_MIN_HOURS = 12.0

#: 这些"手头的事"不值得单独说一句（太琐碎 / 太像汇报）
LIFE_SHARE_DULL = (
    "在发呆", "躺着不太想动", "有点困了", "在关灯", "在想要吃什么",
    "起来喝了口水", "有点睡不着", "还没睡，躺着刷手机", "躺着看手机",
)

#: 心情：变化幅度到多少才值得说
MOOD_SHARE_MIN_DELTA = 8
#: 心情：多久之内算"新鲜"
MOOD_SHARE_MAX_HOURS = 6.0

#: 时段问候：早上、晚上各一个窗口。
#:
#: 窗口必须落在发送窗（9:00–22:30）里面 —— 以前夜间窗口写到 23:00，
#: 那半小时永远被闸门拦掉，等于白写。
#: ⚠ 窗口必须比掷骰间隔（2 小时）宽，否则永远撞不上那一次机会 ——
#:   以前早上 7:30–10:00 里有 1.5 小时还被发送窗拦掉，只剩 1 小时。
GREETING_WINDOWS = ((7.5, 11.0, "morning"), (20.5, 22.5, "night"))
#: 时段问候的概率（命中窗口也要掷骰，不然每天准点）
GREETING_PROBABILITY = 0.45
#: 时段问候两次之间至少隔多久（小时）
GREETING_MIN_HOURS = 20.0

#: 好久没说话：超过多久算"好久"
SILENCE_MIN_HOURS = 36.0
#: 好久没说话：一周最多几次
SILENCE_WEEKLY_CAP = 2

#: 追问：发出后多久才考虑追问（小时）
FOLLOWUP_AFTER_HOURS = 6.0
#: 追问：最多等到多久就放弃（小时）
FOLLOWUP_GIVE_UP_HOURS = 30.0
#: 追问基础概率
FOLLOWUP_BASE_PROBABILITY = 0.38
#: 追问概率上限
FOLLOWUP_MAX_PROBABILITY = 0.62

# ── 被无视（p0-1）──────────────────────────────────────────────────
#
# 以前"发了没人回"只记进 dropped，没有任何代码读它 ——
# 于是她发十条没人回，第十一条的分数、概率、语气一模一样。
# 真人有这个回路：没回应 → 收敛 → 换方式 → 再没回应 → 干脆不问。
#
# 这几个数决定"多久算被无视、收敛多重、什么时候开口问一句"。
#: 主动消息发出后多久没回，算一次"被无视"（小时）。
#: 比追问的 6 小时宽容：追问是"她还想说"，被无视是"她承认没接上"。
NEGLECT_AFTER_HOURS = 8.0
#: 被无视的累计上限。到 3 就是"她基本不再主动找你了"，只剩追问和 check_in。
NEGLECT_MAX = 3
#: 每次被无视，主动分数乘多少（1 级 / 2 级 / 3 级）。
NEGLECT_PENALTY = (1.0, 0.75, 0.50, 0.30)
#: 惩罚至少持续多久（小时）。不设这个，一次"他回来了"就把气全消了。
NEGLECT_PENALTY_HOURS = 6.0
#: 第几级开始允许问"你最近很忙吗"。2 = 连着两批没回之后。
CHECK_IN_MIN_LEVEL = 2
#: 同一个问题多久之内只问一次（小时）。
CHECK_IN_COOLDOWN_HOURS = 24.0

#: 状态里留多少条发送记录
KEEP_RECENT = 60
KEEP_DROPPED = 40


# ══════════════════════════════════════════════════════════════════════
# 数据
# ══════════════════════════════════════════════════════════════════════

@dataclass
class Candidate:
    """一个"想说话"的理由。分数由本地算，不进模型。"""

    source: str
    chat_id: str
    score: float
    reason: str                  # 给复核看的一句话
    material: Dict[str, Any] = field(default_factory=dict)
    #: 这条候选的来源时刻（用于去重，避免同一个话头反复被选中）
    anchor_at: float = 0.0
    #: topic = "说什么"（有具体内容）；tone = "为什么现在想说"（只有动机）。
    #: 两层各取一个，**一起**交给模型 —— 它们不是竞争关系。
    layer: str = "tone"
    #: tone 专用：一句情绪化的话，拼到 topic 的 reason 后面。
    #: 例：「好几天没怎么说话了，你有点想他」
    tone_text: str = ""


@dataclass
class ProactiveState:
    version: int = STATE_VERSION
    day: str = ""
    sent_today: int = 0
    last_sent_at: float = 0.0
    last_source: str = ""
    #: chat_id -> {sent_at, source, text, followup_used}
    awaiting: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: source -> 上次触发时刻
    source_last: Dict[str, float] = field(default_factory=dict)
    #: 已经用过的话头锚点（防止同一个"等我忙完"反复触发）
    used_anchors: List[float] = field(default_factory=list)
    #: 发出去没人回、她也没追问的
    dropped: List[Dict[str, Any]] = field(default_factory=list)
    #: 最近发过的
    recent: List[Dict[str, Any]] = field(default_factory=list)
    #: 预演模式记下的「本来会发什么」。真发的时候不用这个。
    dry: List[Dict[str, Any]] = field(default_factory=list)
    #: 连着掷骰没中的次数。掷中就清零 —— 这是「沉默会累积」的唯一实现。
    #: 存盘：重启之后不该装作从来没努力过。
    miss_streak: int = 0
    #: 上次**掷骰**的时刻（中没中都算）。没有它，引擎每分钟都会重掷。
    last_roll_at: float = 0.0
    # ── 被无视（p0-1）──
    #: 0~3。每有一批主动消息被彻底放下就 +1，对方出现一次就 -1。
    #: 它只压缩**主动开口**的分数与频率，不影响回复 ——
    #: 她不会因为被无视就不回你，那是两件事。
    neglect_level: int = 0
    #: 惩罚至少持续到什么时候（epoch 秒）。过点之前不接受"他回来了"的赎回。
    penalty_until: float = 0.0
    #: 上一次问"你最近很忙吗"的时刻。同一个问题一晚只问一次。
    last_check_in_at: float = 0.0


# ══════════════════════════════════════════════════════════════════════
# 路径与持久化
# ══════════════════════════════════════════════════════════════════════

def state_path(root: Optional[str] = None) -> str:
    from bigfish.vault import resolve_data_root

    return os.path.join(root or resolve_data_root(), STATE_NAME)


def load(root: Optional[str] = None) -> ProactiveState:
    try:
        with open(state_path(root), "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return ProactiveState()
    if not isinstance(raw, dict):
        return ProactiveState()
    st = ProactiveState()
    for key, value in raw.items():
        if hasattr(st, key):
            try:
                setattr(st, key, value)
            except Exception:
                pass
    for name in ("awaiting", "source_last"):
        if not isinstance(getattr(st, name), dict):
            setattr(st, name, {})
    for name in ("used_anchors", "dropped", "recent", "dry"):
        if not isinstance(getattr(st, name), list):
            setattr(st, name, [])
    try:
        st.last_sent_at = float(st.last_sent_at or 0.0)
    except Exception:
        st.last_sent_at = 0.0
    try:
        st.sent_today = int(st.sent_today or 0)
    except Exception:
        st.sent_today = 0
    # 惩罚字段也要收敛：老存档里没有这三项，手改过的 state.json 里可能是
    # 字符串。`penalty()` 直接拿它和 now 比大小，类型不对会抛，
    # 而那条路没有 try —— 一次脏值就能让主动开口整段失效。
    try:
        st.neglect_level = max(0, min(3, int(st.neglect_level or 0)))
    except Exception:
        st.neglect_level = 0
    for _name in ("penalty_until", "last_check_in_at"):
        try:
            setattr(st, _name, float(getattr(st, _name) or 0.0))
        except Exception:
            setattr(st, _name, 0.0)
    return st


def save(st: ProactiveState, root: Optional[str] = None) -> bool:
    try:
        from bigfish.vault import _atomic_write_json

        _atomic_write_json(state_path(root), {
            "version": st.version, "day": st.day, "sent_today": st.sent_today,
            "last_sent_at": st.last_sent_at, "last_source": st.last_source,
            "awaiting": st.awaiting, "source_last": st.source_last,
            "used_anchors": st.used_anchors[-40:],
            "dropped": st.dropped[-KEEP_DROPPED:],
            "recent": st.recent[-KEEP_RECENT:],
            "dry": st.dry[-40:],
            # 连着没中的次数要存盘。不存的话每次重启都回到 0 ——
            # 她会永远停在"从没努力过"的状态，加成永远不起作用。
            "miss_streak": int(getattr(st, "miss_streak", 0) or 0),
            "last_roll_at": float(getattr(st, "last_roll_at", 0.0) or 0.0),
            # ── 被无视（p0-1）的三个字段 ──
            #
            # ⚠ 这三个以前没存。`miss_streak`（掷骰没中）存了，
            #   但"被彻底放下"的惩罚没存 —— 于是重启一次，
            #   neglect_level 归 0、penalty_until 归 0，
            #   她满血回来继续找你。
            #
            #   症状是"她会往回收"这个卖点只在同一次运行里成立：
            #   冷落三天、重启一次、又从"你最近很忙吗"开始。
            #   惩罚必须活过重启，否则它不是性格，只是内存里的临时变量。
            "neglect_level": int(getattr(st, "neglect_level", 0) or 0),
            "penalty_until": float(getattr(st, "penalty_until", 0.0) or 0.0),
            "last_check_in_at": float(getattr(st, "last_check_in_at", 0.0) or 0.0),
        })
        return True
    except Exception:
        return False


def day_key(now: float) -> str:
    return (datetime.fromtimestamp(now) - timedelta(hours=4)).strftime("%Y-%m-%d")


def roll_day(st: ProactiveState, now: float) -> bool:
    """跨天清零。返回是否跨了。"""
    key = day_key(now)
    if st.day == key:
        return False
    st.day = key
    st.sent_today = 0
    return True


def stable_anchor(*parts: Any) -> float:
    """跨进程稳定的去重锚点。

    ⚠ 以前用的是内置 `hash()`。它对 str 每次进程启动都带随机 salt
    （PYTHONHASHSEED），所以同一句话今天算出 A、明天算出 B ——
    `used_anchors` 里存的那 40 个历史值跨重启全部失配，
    去重实际上只在单次运行内有效。

    而它唯一的用途恰恰是**跨天**：同一个生日、同一个话头别问第二遍。
    于是症状是"重启之后她可以把同一个生日再问一次"，
    只在重启后出现，最容易被当成"模型今天不听话"。

    sha1 前 6 字节 ≈ 281 万亿种取值，撞车概率远低于一天几十条的量级。
    """
    raw = "\x1f".join(str(p) for p in parts).encode("utf-8")
    return float(int.from_bytes(hashlib.sha1(raw).digest()[:6], "big") % 10_000_000)


# ══════════════════════════════════════════════════════════════════════
# 闸门
# ══════════════════════════════════════════════════════════════════════

def in_window(now: float) -> bool:
    """现在是否在允许她主动开口的时间窗内。

    单独抽出来是因为**追问也要用它**。追问同样是一句不请自来的话，
    但它以前走的是另一条路（`due_followups`），只查间隔和概率，
    完全不查时段 —— 于是 21:00 发出去、他没回的那条，会在 03:00
    追一句「你怎么没回我」，她睡着照发。闸门管的不是「主动消息」，
    管的是「所有她先开口的话」，两个入口必须共用同一套判断。
    """
    t = datetime.fromtimestamp(now)
    return WINDOW_START <= (t.hour + t.minute / 60) <= WINDOW_END


def gate_block_reason(st: ProactiveState, *, now: float, per_day: int,
                      life_state: Any, last_peer_at: float,
                      enabled: bool = True) -> str:
    """六道硬闸门。返回空串 = 放行；返回非空 = 拦截原因（进日志）。

    这一层**不生成任何文字**，只看条件。所以它可以在 60 秒粒度上
    反复调用而不花一分钱。
    """
    if not enabled:
        return "未开启"

    # 1. 今天够了吗
    per = max(0, min(MAX_PER_DAY, int(per_day or 0)))
    if st.sent_today >= per:
        return f"今天已经发过 {st.sent_today} 条（上限 {per}）"

    # 2. 距上一条太近
    if st.last_sent_at and (now - st.last_sent_at) < MIN_GAP_MINUTES * 60:
        left = MIN_GAP_MINUTES - (now - st.last_sent_at) / 60
        return f"距上一条还差 {left:.0f} 分钟"

    # 3. 时段
    if not in_window(now):
        return f"不在允许时段（{WINDOW_START:.0f}:00–{WINDOW_END:.0f}:30）"

    # 4. 她醒着吗
    if life_state is not None:
        if getattr(life_state, "asleep", False):
            return "她睡了"
        energy = float(getattr(life_state, "energy", 100) or 0)
        if energy < MIN_ENERGY:
            return f"她太累了（精力 {energy:.0f}）"

    # 5. 对方正在聊 —— 刚聊过就不必再开新话头
    if last_peer_at and (now - last_peer_at) < BUSY_MINUTES * 60:
        return f"刚才还在聊（{int((now - last_peer_at) / 60)} 分钟前）"

    return ""


# ══════════════════════════════════════════════════════════════════════
# 候选：九个源
# ══════════════════════════════════════════════════════════════════════

def _recent_lines(chat_id: str, limit: int = 12) -> List[Tuple[str, str]]:
    try:
        from bigfish import history as _H

        return _H.load(chat_id, limit=limit)
    except Exception:
        return []


def _hours_since(ts: float, now: float) -> float:
    if not ts:
        return 1e9
    return max(0.0, (now - ts) / 3600.0)


def candidate_open_loop(st: ProactiveState, *, chat_id: str, now: float,
                        me: str, last_peer_at: float = 0.0) -> Optional[Candidate]:
    """未完话头：他最后一句里有"等我/回头/明天"，之后没再说话。"""
    lines = _recent_lines(chat_id, 14)
    if not lines:
        return None
    # 找最后一条对方的话
    last_peer = None
    for who, text in reversed(lines):
        if who and who != me:
            last_peer = (who, text)
            break
    if last_peer is None:
        return None
    text = last_peer[1]
    if not OPEN_LOOP_PATTERNS.search(text):
        return None

    # 后面还有没有再说话？最后一条如果是她说的，说明话头已经接过了
    if lines and lines[-1][0] == me:
        return None

    anchor = stable_anchor(text)     # 稳定的锚点，去重用
    if any(abs(a - anchor) < 1 for a in st.used_anchors):
        return None

    # 时间窗：太短像催，太长已经忘了。
    #
    # ⚠ 这里以前是 `hours = _hours_since(now - 1, now)` —— 算了个 1 秒然后
    #   扔掉，两个常量一次都没用上。后果是"你三天前说的回头聊"今天还能
    #   当理由，因为话头没接过就永远是候选。
    hours = _hours_since(last_peer_at, now) if last_peer_at else 0.0
    if hours and not (OPEN_LOOP_MIN_HOURS <= hours <= OPEN_LOOP_MAX_HOURS):
        return None

    # 越久越容易忘，分数跟着降一点 —— 但过了 MAX 就不提了
    score = 72.0 if hours < 12 else 66.0
    if "等我" in text or "回头" in text:
        score += 6
    return Candidate(
        source="open_loop", chat_id=chat_id, score=min(95.0, score), layer="topic",
        reason=(f"他 {hours:.0f} 小时前说的是「{text[:24]}」，之后没再说话，"
                "这个话头还没接上" if hours else
                f"他最后说的是「{text[:24]}」，之后没再说话，这个话头还没接上"),
        material={"peer_text": text},
        anchor_at=anchor)


def candidate_life_share(st: ProactiveState, *, chat_id: str, now: float,
                         life_state: Any) -> Optional[Candidate]:
    """她自己的生活：手头的事值得说一句。"""
    if life_state is None:
        return None
    doing = str(getattr(life_state, "doing", "") or "").strip()
    if not doing or doing in LIFE_SHARE_DULL:
        return None
    if _hours_since(st.source_last.get("life_share", 0), now) < LIFE_SHARE_MIN_HOURS:
        return None
    # 同一件事一天内只算一次 —— 否则「在收拾屋子」一整天反复冒出来，
    # 她看起来就像卡在同一个动作里。
    if _hours_since(st.source_last.get(f"life_share:{doing}", 0), now) < 24.0:
        return None
    # 刚说过同一件事就跳过
    for item in st.recent[-6:]:
        if doing and doing in str(item.get("text") or ""):
            return None
    return Candidate(
        # 58：高于 40 的地基、低于「有话头没接」（72）—— 它该是备选，
        # 不该是主角。但原来写 52 时它是**永远出局**，连备选都不是。
        source="life_share", chat_id=chat_id, score=58.0, layer="tone",
        reason=f"她手头的事是「{doing}」，这件事还没跟他说过",
        tone_text=f"你手头在{doing}",
        material={"doing": doing})


def candidate_mood(st: ProactiveState, *, chat_id: str, now: float) -> Optional[Candidate]:
    """心情：有明显变化，想说。"""
    try:
        from bigfish.vault import read_mood_state

        raw = read_mood_state()
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return None
    moods = raw.get("moods")
    if not isinstance(moods, dict) or not moods:
        return None
    slot_key = raw.get("lastActive") or next(iter(moods), "")
    s = moods.get(slot_key)
    if not isinstance(s, dict):
        return None

    reason = str(s.get("lastReason") or "").strip()
    if not reason:
        return None
    try:
        updated = float(s.get("updatedAt") or 0) / 1000.0
    except Exception:
        updated = 0.0
    if _hours_since(updated, now) > MOOD_SHARE_MAX_HOURS:
        return None

    try:
        cur = float(s.get("mood") or 0)
        noted = float(s.get("lastNotedMood") or cur)
    except Exception:
        cur = noted = 0.0
    if abs(cur - noted) < MOOD_SHARE_MIN_DELTA:
        return None
    if _hours_since(st.source_last.get("mood", 0), now) < 18:
        return None

    return Candidate(
        source="mood", chat_id=chat_id,
        score=60.0 + min(10.0, abs(cur - noted)), layer="tone",
        reason=f"她这会儿心情动了一下（{reason[:24]}），还没跟他说过",
        tone_text=f"你心里有点事（{reason[:20]}），有点想跟他说说话",
        material={"reason": reason, "mood": cur})


def candidate_greeting(st: ProactiveState, *, chat_id: str, now: float,
                       life_state: Any) -> Optional[Candidate]:
    """时段：刚醒的早上 / 准备睡的晚上。"""
    dt = datetime.fromtimestamp(now)
    hour = dt.hour + dt.minute / 60.0
    label = ""
    for lo, hi, name in GREETING_WINDOWS:
        if lo <= hour <= hi:
            label = name
            break
    if not label:
        return None
    if _hours_since(st.source_last.get("greeting", 0), now) < GREETING_MIN_HOURS:
        return None
    # 确定性掷骰：按 (day, label) 固定，同一天同一窗口只掷一次
    rng = random.Random(f"{st.day}:{label}:greet")
    if rng.random() > GREETING_PROBABILITY:
        return None

    just_woke = False
    if life_state is not None:
        woke_at = float(getattr(life_state, "just_woke_at", 0) or 0)
        just_woke = bool(woke_at) and (now - woke_at) < 3 * 3600

    if label == "morning" and not just_woke:
        return None        # 早上那句只在"她刚醒"时才像真的

    return Candidate(
        source="greeting", chat_id=chat_id,
        # 夜间问候 56：深夜想说话是常事，但不必比晨间更主动
        score=56.0 if label == "night" else 62.0,
        reason=("她刚醒，想说他一句" if label == "morning"
                else "快到睡觉的点了，她想说一声"),
        material={"window": label})


def parse_dates_text(text: Any) -> List[Dict[str, Any]]:
    """把插件里填的多行文本解析成 candidate_date 认的结构。

    为什么用文本而不是结构化列表：插件配置面板原生的输入只有
    开关/数字/单选/时间/文本五种，没有"列表"。为了一个一年用两次的字段
    去扩展面板，代价是每个插件都要重新适配一遍。

    接受这些写法（一行一条，# 开头是注释）：
        12-25 她的生日       每年重复
        2027-03-08 搬家      只此一次
        12-25|她的生日       竖线也行
    日期认不出来的行直接跳过 —— 宁可少提醒一次，不要在她嘴里说出一个
    解析出来的怪日期。
    """
    out: List[Dict[str, Any]] = []
    if isinstance(text, list):          # 兼容老配置（存的就是结构化的）
        return [d for d in text if isinstance(d, dict)]
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        raw, sep, label = line.partition("|")
        if not sep:
            parts = line.split(None, 1)
            raw = parts[0] if parts else ""
            label = parts[1] if len(parts) > 1 else ""
        raw, label = raw.strip(), label.strip()
        if not raw or not label:
            continue
        yearly = len(raw) <= 5          # MM-DD 是每年，YYYY-MM-DD 是一次性的
        fmt = "%m-%d" if yearly else "%Y-%m-%d"
        try:
            datetime.strptime(raw, fmt)
        except Exception:
            continue
        out.append({"date": raw, "label": label[:24], "yearly": yearly})
    return out


def candidate_date(st: ProactiveState, *, chat_id: str, now: float,
                   dates: List[Dict[str, Any]]) -> Optional[Candidate]:
    """重要日期：配置里手填的。提前一天 + 当天。"""
    if not dates:
        return None
    today = datetime.fromtimestamp(now).date()
    for item in dates:
        if not isinstance(item, dict):
            continue
        raw = str(item.get("date") or "").strip()
        label = str(item.get("label") or "").strip()
        if not raw or not label:
            continue
        for delta in (0, 1):          # 0 = 今天，1 = 提前一天提醒
            try:
                if item.get("yearly", True):
                    target = datetime.strptime(raw, "%m-%d").date().replace(
                        year=today.year)
                    if target < today - timedelta(days=200):
                        target = target.replace(year=today.year + 1)
                else:
                    target = datetime.strptime(raw, "%Y-%m-%d").date()
            except Exception:
                continue
            # delta=0 是"今天"，delta=-1 是"提前一天"
            want = today + timedelta(days=delta)
            if target != want:
                continue
            anchor = stable_anchor(label, target.isoformat())
            if any(abs(a - anchor) < 1 for a in st.used_anchors):
                continue
            when = "今天" if delta == 0 else "明天"
            return Candidate(
                source="date", chat_id=chat_id, score=88.0, layer="topic",
                reason=f"{when}是「{label}」",
                material={"label": label, "when": when},
                anchor_at=anchor)
    return None


def candidate_silence(st: ProactiveState, *, chat_id: str, now: float,
                      last_peer_at: float) -> Optional[Candidate]:
    """好久没说话。一周最多两次。"""
    if not last_peer_at:
        return None
    hours = _hours_since(last_peer_at, now)
    if hours < SILENCE_MIN_HOURS:
        return None
    week_ago = now - 7 * 86400
    # source 可能是 "silence+whim" 这种组合，所以用"包含"而不是相等
    used = sum(1 for r in st.recent
               if "silence" in str(r.get("source") or "")
               and float(r.get("at") or 0) >= week_ago)
    if used >= SILENCE_WEEKLY_CAP:
        return None
    # 越久越想说，但不会无限涨
    score = 56.0 + min(18.0, (hours - SILENCE_MIN_HOURS) / 24.0 * 6)
    return Candidate(
        source="silence", chat_id=chat_id, score=score, layer="tone",
        reason=f"已经 {hours:.0f} 小时没说过话了",
        tone_text=f"已经 {hours:.0f} 小时没怎么说话了，你有点想他",
        material={"hours": hours})


def candidate_whim(st: ProactiveState, *, chat_id: str, now: float,
                   life_state: Any) -> Optional[Candidate]:
    """没事也想说一句 —— 唯一一个**没有话题**的来源。

    其他六个都能被解释：有话头没接、有事没说、有日子要提、太久没聊。
    但真人主动找你的绝大多数时候解释不了 —— 就是忽然想起来了。

    没有这一条，"她主动说话"永远是一份待办清单在执行。
    有了它，才有"她没事也会想你"。

    约束：醒着、精力 ≥45、两次之间至少 20 小时 ——
    不加约束的话它会变成最烦人的那个来源。
    """
    if life_state is None:
        return None
    if bool(getattr(life_state, "asleep", False)):
        return None
    try:
        energy = float(getattr(life_state, "energy", 100.0) or 0.0)
    except Exception:
        energy = 100.0
    if energy < WHIM_MIN_ENERGY:
        return None
    if _hours_since(st.source_last.get("whim", 0), now) < WHIM_MIN_HOURS:
        return None
    return Candidate(
        source="whim", chat_id=chat_id, score=WHIM_SCORE, layer="tone",
        reason="没什么具体的事，就是忽然想找他说句话 —— 这种时候不必有由头",
        tone_text="没什么具体的事，就是忽然想找他说句话",
        material={})


def candidate_recall(st: ProactiveState, *, chat_id: str, now: float,
                     owner: str = "", tier: str = "",
                     db_path: str = "") -> Optional[Candidate]:
    """他提过、但还没结果的事 —— 主动开口最有分量的一个来源。

    和 `open_loop` 的分工：

        open_loop  抓话头。你说「等我忙完」，之后没再说话。
        recall     抓待办。你说「下周三要去体检」，之后一直没提结果。

    后者才是真人主动开口最常有的由头。不是"你没回我"，是"我记得你有件事"。

    过滤条件：
      · 1.5–10 天前（太新的还热着不用问，太旧的已经过去了）
      · 命中 RECALL_KEYWORDS（体检/面试/搬家这类"有结果"的事）
      · 没被用过（anchor 用 fact id，问过一次就不再问）
    """
    if not (owner and tier and db_path):
        return None
    try:
        from bigfish import memory as M

        # ⚠ status 必须传。`list_facts` 收到空串 = **不过滤**，
        #   于是 forgotten（她已经忘掉的）和 superseded（被新事实推翻的）
        #   也会被捞出来问一句 —— 问的是一件"她已经不认"的事。
        data = M.list_facts(db_path, "dafeiyu", owner=owner, tier=tier,
                            status=M.ACTIVE, limit=60)
    except Exception:
        return None

    best = None
    best_score = 0.0
    for it in (data or {}).get("items") or []:
        if not isinstance(it, dict):
            continue
        try:
            fid = int(it.get("id") or 0)
            created = float(it.get("created_at") or 0) / 1000.0
        except Exception:
            continue
        if not fid or not created:
            continue
        days = (now - created) / 86400.0
        if not (RECALL_MIN_DAYS <= days <= RECALL_MAX_DAYS):
            continue
        text = f"{it.get('predicate') or ''} {it.get('object') or ''}"
        if not any(k in text for k in RECALL_KEYWORDS):
            continue
        if any(abs(float(a) - float(fid)) < 1 for a in st.used_anchors):
            continue
        # 越近越想说，但不会盖过"今天是他生日"（88 分）
        score = 84.0 + max(0.0, 7.0 - days)
        if score > best_score:
            best_score = score
            best = (fid, days, str(it.get("object") or "")[:28])

    if not best:
        return None
    fid, days, obj = best
    return Candidate(
        source="recall", chat_id=chat_id, score=best_score, layer="topic",
        reason=f"他 {days:.0f} 天前说过「{obj}」，之后一直没提结果",
        material={"recalled": obj, "days": days},
        anchor_at=float(fid))


def candidate_due(st: ProactiveState, *, chat_id: str, now: float,
                  owner: str = "", tier: str = "",
                  db_path: str = "") -> Optional[Candidate]:
    """到期/临近的约定 —— "明天体检"，今天该提一句（T-2）。

    和 recall 的分工：
        recall  他提过、1.5–10 天没结果 → "后来怎么样了？"
        due     有明确日期、快到了     → "明天就是了吧？"

    分数 86：高于 recall(84)，低于生日(88) —— 有日期的约定比模糊的惦记更该说。
    提过一次就不再提：anchor 用 `event_id + 1e9`，与 fact id 分开命名空间。
    """
    if not (owner and tier and db_path):
        return None
    try:
        from bigfish import future as F

        rows = F.upcoming(db_path, "dafeiyu", owner, now=now)
    except Exception:
        return None

    for row in rows:
        try:
            eid = int(row.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if not eid:
            continue
        anchor = float(eid) + 1_000_000_000.0
        if any(abs(float(a) - anchor) < 1 for a in st.used_anchors):
            continue
        line = F.due_line(row, now=now)
        if not line:
            continue
        return Candidate(
            source="due", chat_id=chat_id, score=86.0, layer="topic",
            reason=f"你们说好的事快到了：{line}",
            material={"due": line, "event_id": eid},
            anchor_at=anchor)
    return None


def candidate_own_loop(st: ProactiveState, *, chat_id: str, now: float,
                       life_state: Any = None) -> Optional[Candidate]:
    """她今天想做没做成的事 —— 她自己的未了之事（T-3）。

    和 open_loop / recall 的区别：
        open_loop  对方的话头（"等我忙完"）
        recall     对方提过的事（"下周三体检"）
        own_loop   **她自己**想做的事（"想收拾屋子，没顾上"）

    只作 tone（情绪底色），不作 topic：她自己的小事不必拿来当话题，
    但它会影响这一轮的语气 —— 和"今天有点烦"同一个位置。
    """
    if life_state is None:
        return None
    try:
        from bigfish import life as L

        loops = L.own_loops(life_state, limit=3)
    except Exception:
        return None
    if not loops:
        return None
    # 一天最多一次：反复说"我没收拾屋子"就变成自责表演了。
    if _hours_since(st.source_last.get("own_loop", 0), now) < 20:
        return None
    what = str(loops[0]).strip()
    if not what:
        return None
    return Candidate(
        source="own_loop", chat_id=chat_id, score=70.0, layer="tone",
        reason=f"今天想{what}，没顾上",
        material={"own_loop": what},
        tone_text=f"今天想{what}，没顾上")


def candidate_unresolved(st: ProactiveState, *, chat_id: str, now: float,
                         owner: str = "", tier: str = "",
                         db_path: str = "") -> Optional[Candidate]:
    """没解开的别扭 —— 跨天的旧账（T-5）。

    `afterglow` 的 conflict 只有 4 小时：它管的是"这一轮还堵着"。
    但人不会 4 小时就忘 —— 昨天你说话难听，今天她会带着这点旧账，
    开口时语气不一样，甚至提一句"上次那事"。

    7 天后自动过期：不留永久怨气（那是另一个东西，不是人）。
    只作 tone：旧账影响语气，不该变成每次开口的主题。
    """
    if not (owner and tier and db_path):
        return None
    try:
        from bigfish import bond as B

        rows = B.list_events(db_path, "dafeiyu", owner,
                             kind="unresolved", limit=5)
    except Exception:
        return None

    for row in rows:
        try:
            at = float(row.get("at") or 0) / 1000.0
            eid = int(row.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if not at or (now - at) > 7 * 86400:
            continue
        what = " ".join(str(row.get("what") or "").split())[:40]
        if not what:
            continue
        anchor = float(eid) + 2_000_000_000.0
        if any(abs(float(a) - anchor) < 1 for a in st.used_anchors):
            continue
        return Candidate(
            source="unresolved", chat_id=chat_id, score=76.0, layer="tone",
            reason=f"上次那点别扭还没说开：{what}",
            material={"unresolved": what},
            tone_text="心里还有点没过去的事", anchor_at=anchor)
    return None


def collect(st: ProactiveState, *, chat_id: str, now: float, me: str,
            life_state: Any, last_peer_at: float,
            dates: Optional[List[Dict[str, Any]]] = None,
            owner: str = "", tier: str = "",
            db_path: str = "") -> List[Candidate]:
    """跑一遍六个源。单个源抛错不影响其他。"""
    out: List[Candidate] = []

    def add(fn, *args, **kwargs):
        try:
            c = fn(*args, **kwargs)
            if c is not None:
                out.append(c)
        except Exception:
            pass

    add(candidate_open_loop, st, chat_id=chat_id, now=now, me=me,
        last_peer_at=last_peer_at)
    add(candidate_life_share, st, chat_id=chat_id, now=now, life_state=life_state)
    add(candidate_mood, st, chat_id=chat_id, now=now)
    add(candidate_greeting, st, chat_id=chat_id, now=now, life_state=life_state)
    add(candidate_date, st, chat_id=chat_id, now=now, dates=dates or [])
    add(candidate_silence, st, chat_id=chat_id, now=now, last_peer_at=last_peer_at)
    add(candidate_whim, st, chat_id=chat_id, now=now, life_state=life_state)
    add(candidate_recall, st, chat_id=chat_id, now=now, owner=owner,
        tier=tier, db_path=db_path)
    add(candidate_due, st, chat_id=chat_id, now=now, owner=owner,
        tier=tier, db_path=db_path)
    add(candidate_own_loop, st, chat_id=chat_id, now=now,
        life_state=life_state)
    add(candidate_unresolved, st, chat_id=chat_id, now=now, owner=owner,
        tier=tier, db_path=db_path)
    # p0-1：连着两批没回之后的那句"你最近很忙吗"。
    # 它排在最后 add，但分数 78 会自己排上去。
    add(candidate_check_in, st, chat_id=chat_id, now=now)

    out.sort(key=lambda c: -c.score)
    return out


# ── 两层：说什么 / 为什么现在想说 ────────────────────────────────────
#
# 原来这里是"七个来源抢一个"：各算一个分，排序取最高，剩下的全扔。
# 于是"我在收拾屋子"（58）和"我今天有点烦"（62）同时成立时，只会说出
# 前一件事 —— 因为只能带一条理由进模型。
#
# 真人不是这样。真人会说"今天有点烦，在收拾屋子" —— 两件事叠在一起。

def pick(cands: List[Candidate]) -> Tuple[Optional[Candidate], Optional[Candidate]]:
    """从候选里挑出（主话题, 情绪底色）。两层各取最高的一个，不互相淘汰。"""
    topic: Optional[Candidate] = None
    tone: Optional[Candidate] = None
    for c in cands:
        if getattr(c, "layer", "tone") == "topic":
            if topic is None or c.score > topic.score:
                topic = c
        else:
            if tone is None or c.score > tone.score:
                tone = c
    return topic, tone


def combined_score(topic: Optional[Candidate],
                   tone: Optional[Candidate]) -> float:
    """两层合起来的分数。

    "有事可说" + "有话想说" 比单独任何一件都更容易开口，所以 +8。
    但上限还是 95 —— 再想也不该变成必发。
    """
    base = max(topic.score if topic else 0.0, tone.score if tone else 0.0)
    bonus = 8.0 if (topic and tone) else 0.0
    return min(95.0, base + bonus)


def compose_reason(topic: Optional[Candidate],
                   tone: Optional[Candidate]) -> str:
    """把两层拼成一句人话，交给模型当"起因"。

    这一句话直接决定她会说什么。拼错的结果是她说出两件不相干的事。
    """
    if topic and tone:
        tail = (tone.tone_text or tone.reason).strip()
        return f"{topic.reason}。{tail}。"
    if topic:
        return topic.reason
    if tone:
        # 只有基调时也要用情绪化的那句 —— 不然她开口就是干巴巴的
        # 「已经 40 小时没说过话了」，而不是「有点想你了」。
        return (tone.tone_text or tone.reason).strip()
    return ""


def merge_material(topic: Optional[Candidate],
                   tone: Optional[Candidate]) -> Dict[str, Any]:
    """两层素材合并。tone 先放，topic 后放（同键时 topic 覆盖）。"""
    out: Dict[str, Any] = {}
    if tone:
        out.update(tone.material or {})
    if topic:
        out.update(topic.material or {})
    return out


# ══════════════════════════════════════════════════════════════════════
# 掷骰：这一轮要不要开口
# ══════════════════════════════════════════════════════════════════════

def _base_rate(score: float) -> float:
    """分数 → 单次掷骰的基础命中率（还没乘时机因子）。

    分两段的原因：日常想说话和"有话头没接"不是同一件事。
    前者一天可能出现二十次，后者一天就那一两次 —— 用同一条直线，
    要么日常太吵，要么话头永远接不上。
    """
    s = float(score)
    if s <= SCORE_THRESHOLD:
        return 0.0
    if s < HIT_BASE_MID:
        return ((s - SCORE_THRESHOLD) / (HIT_BASE_MID - SCORE_THRESHOLD)
                * HIT_MID_RATE)
    span = max(1.0, HIT_BASE_CEIL - HIT_BASE_MID)
    return min(HIT_HIGH_RATE,
               HIT_MID_RATE + (s - HIT_BASE_MID) / span
               * (HIT_HIGH_RATE - HIT_MID_RATE))

def hit_probability(*, score: float, now: float, last_sent_at: float,
                    last_peer_at: float, miss_streak: int = 0) -> Tuple[int, str]:
    """这一轮掷骰的命中线（0–100），以及一句能写进日志的说明。

    为什么不是「分数过线就发」
    ────────────────────────
    确定性阈值让同样条件下永远同样结果 —— 于是"她主动说话"变成一条规则，
    而不是一个人的选择。真实的人在同一天、同样的心情下，可能想说也可能不想。

    三层：

        base    分数越高越想说（SCORE_THRESHOLD 以下为 0，连骰子都不掷）
        timing  刚发过 ×0.15 / 刚发过不久 ×0.4 / 你正在聊 ×0.5 / 冷了 6 小时 ×1.4
        miss    连着没中 +8%/次，最多 +32

    返回 0 表示"这一轮连掷都不掷" —— 调用方据此跳过，不要写成 `p > 0` 才掷，
    那样日志里会全是"掷出 87 / 命中 0"的噪音。
    """
    base = _base_rate(score)

    since_sent = (now - last_sent_at) / 60.0 if last_sent_at else 1e9
    # None = 还没有对话记录。不能拿 1e9 当分钟数再除 60：
    # 那会算出「16666667 小时没说话」，启动时日志直接变成笑话。
    since_peer = (now - last_peer_at) / 60.0 if last_peer_at else None

    factor, why = 1.0, ""
    if since_sent < TIMING_JUST_SENT_MINUTES:
        factor, why = TIMING_JUST_SENT_FACTOR, "刚发过"
    elif since_sent < TIMING_RECENT_SENT_MINUTES:
        factor, why = TIMING_RECENT_SENT_FACTOR, "刚发过不久"
    elif since_peer is not None and since_peer < TIMING_CHATTING_MINUTES:
        factor, why = TIMING_CHATTING_FACTOR, "你正在聊"
    elif since_peer is not None and since_peer > TIMING_COLD_HOURS * 60:
        factor, why = TIMING_COLD_FACTOR, f"已经 {since_peer / 60:.0f} 小时没说话"

    bonus = min(MISS_BONUS_CAP, max(0, int(miss_streak or 0)) * MISS_BONUS_STEP)
    p = min(HIT_MAX, base * factor + bonus)
    hit = max(0, min(100, int(round(p * 100))))

    bits = [why] if why else []
    if bonus > 0:
        bits.append(f"没中加成 +{int(round(bonus * 100))}")
    if not base:
        bits.append("分数不够，不掷")
    return hit, "，".join(bits)

def note_miss(st: ProactiveState) -> None:
    """掷骰没中。连着没中的次数累积成下一次的加成。

    这一条存在的意义：沉默不该无限自我强化。没有它，条件最差的那几天
    会连着不说话，而且第二天概率一模一样 —— 她越来越像一个坏掉的功能。
    """
    st.miss_streak = min(99, int(getattr(st, "miss_streak", 0) or 0) + 1)

def note_hit(st: ProactiveState) -> None:
    """掷中了。计数器清零 —— 不然她会越说越频繁，直到停不下来。"""
    st.miss_streak = 0

def roll_due(st: ProactiveState, now: float) -> bool:
    """现在该不该掷骰子。ROLL_INTERVAL_MINUTES（120 分钟）内掷过就不再掷
    —— 中没中都算。

    别把它读成 40 分钟：那是 MIN_GAP（两条**发出去**之间至少隔多久），
    两件事。这里管的是"多久给一次机会"。
    """
    last = float(getattr(st, "last_roll_at", 0.0) or 0.0)
    if last <= 0:
        return True
    return (now - last) >= ROLL_INTERVAL_MINUTES * 60

def note_roll(st: ProactiveState, now: float) -> None:
    """记下"刚掷过"。必须在掷完之后立刻调 —— 不然同一分钟会连掷。"""
    st.last_roll_at = float(now)

# ══════════════════════════════════════════════════════════════════════
# 从她的话里取出这一条
# ══════════════════════════════════════════════════════════════════════

#: 她说「算了」的表示法。
#:
#: 为什么不用 JSON：0.9.1 之前是 `{"send":true,"text":"..."}` 的协议，
#: 但那套协议逼着 prompt 只能写"判断要不要发"，写不了"以她的身份说话" ——
#: 于是主动消息带不上记忆和心情。改成正文之后一次调用同时完成
#: "想不想说"和"说什么"，代价是要有一个约定的放弃标记。
SILENT_MARKS = ("-", "—", "——", "（不说话）", "(不说话)", "算了", "不说了")


def clean_reply(raw: str) -> str:
    """把她这一轮的输出收拾成一条能发的话。返回空串 = 这一轮不说。

    倒向"不发"的所有情况：空、放弃标记、只剩标点、长到不像微信。
    """
    text = (raw or "").strip()
    # 去掉代码围栏（模型偶尔会把整段包起来）
    m = re.match(r"\A```[A-Za-z0-9_+.-]*[ \t]*\r?\n?(.*?)\r?\n?```[ \t]*\Z",
                 text, re.S)
    if m:
        text = m.group(1).strip()
    if not text:
        return ""
    if text.strip("。．.，,~～ ") in SILENT_MARKS:
        return ""
    # 一句话只留第一段：她主动开口不该一次说三件事
    text = " ".join(text.split())
    if len(text) > 120:
        text = text[:120].rstrip() + "…"
    return text


# ══════════════════════════════════════════════════════════════════════
# 追问 or 忘记
# ══════════════════════════════════════════════════════════════════════

def followup_probability(*, bond_hit: bool, mood: float, energy: float) -> float:
    """她会不会追问。关系近、心情好、有精神 → 更可能。

    这个函数存在的意义不是"算得准"，是让两种反应都**有可能发生**。
    一个永远追问的角色像催债，一个永远不追问的角色像不在乎。
    """
    p = FOLLOWUP_BASE_PROBABILITY
    if bond_hit:
        p += 0.14
    if mood >= 62:
        p += 0.06
    elif mood <= 40:
        p -= 0.10
    if energy <= 30:
        p -= 0.08
    return max(0.08, min(FOLLOWUP_MAX_PROBABILITY, p))


def due_followups(st: ProactiveState, *, now: float, life_state: Any,
                  bond_hit: bool = False) -> List[Tuple[str, Dict[str, Any]]]:
    """哪些会话该考虑追问了。返回 [(chat_id, awaiting_item)]。

    ⚠ 追问也要过闸门。以前这里只查「间隔够不够」和「到没到时间」，
    于是 21:00 发出去的那条会在 03:00 追问 —— 凌晨三点问「你怎么没回我」，
    而且她还睡着。这不是"执着"，是骚扰，也是这个系统最容易被卸载的原因。

    所以先跑一遍和主动开口同一套的硬闸门（睡着 / 太累 / 不在时段），
    再谈概率。概率只决定"要不要"，闸门决定"能不能"。
    """
    out: List[Tuple[str, Dict[str, Any]]] = []

    # ── 硬闸门：睡着 / 太累 / 不在时段，一条都不追 ──
    if life_state is not None:
        if getattr(life_state, "asleep", False):
            return []
        if float(getattr(life_state, "energy", 100) or 0) < MIN_ENERGY:
            return []
    if not in_window(now):
        return []

    try:
        from bigfish.vault import read_mood_state

        raw = read_mood_state() or {}
        moods = raw.get("moods") if isinstance(raw, dict) else {}
        slot = (raw.get("lastActive") if isinstance(raw, dict) else "") or ""
        cur = 0.0
        if isinstance(moods, dict) and slot in moods:
            cur = float((moods[slot] or {}).get("mood") or 0)
    except Exception:
        cur = 0.0
    energy = float(getattr(life_state, "energy", 100) or 100) if life_state else 100

    for chat_id, item in list(st.awaiting.items()):
        if not isinstance(item, dict):
            continue
        if item.get("followup_used"):
            continue
        sent_at = float(item.get("sent_at") or 0)
        hours = _hours_since(sent_at, now)
        # 追问不占 per_day（它是回应，不是新开话题），但必须守"两条之间
        # 至少隔多久" —— 否则「主动发一条」+「2 小时后追问」会连着来，
        # 用户看到的是刷屏。
        if st.last_sent_at and (now - st.last_sent_at) < MIN_GAP_MINUTES * 60:
            continue
        if hours < FOLLOWUP_AFTER_HOURS:
            continue
        if hours > FOLLOWUP_GIVE_UP_HOURS:
            continue
        p = followup_probability(bond_hit=bond_hit, mood=cur, energy=energy)
        rng = random.Random(f"{st.day}:{chat_id}:{int(sent_at)}")
        if rng.random() <= p:
            out.append((chat_id, item))
    return out


def _touch_sources(st: ProactiveState, sources: List[str], now: float,
                   material: Optional[Dict[str, Any]] = None) -> None:
    """把用到的每一个来源的时刻都记下来。

    ⚠ 必须**逐个**写。两层组合之后 source 会变成 "recall+whim"，
    如果只写这个组合键，`candidate_whim` 读的 `source_last["whim"]`
    永远是空的 —— 它的 20 小时限制就失效了。
    这个 bug 很安静：不会报错，只会让她老用同一个理由开口。
    """
    for s in sources:
        if s:
            st.source_last[s] = now
    if "life_share" in sources and isinstance(material, dict):
        doing = str(material.get("doing") or "")
        if doing:
            st.source_last[f"life_share:{doing}"] = now


def mark_sent(st: ProactiveState, *, chat_id: str, source: str, text: str,
              now: float, anchor_at: float = 0.0,
              material: Optional[Dict[str, Any]] = None,
              sources: Optional[List[str]] = None) -> None:
    """记一条发出去的。**必须在真正发送成功之后调**。

    `sources` 是这一条用到的全部来源（两层组合时有两个）。
    不传就退回 `[source]`，老调用点不用改。
    """
    used = [s for s in (sources or [source]) if s]
    st.sent_today += 1
    st.last_sent_at = now
    st.last_source = source
    _touch_sources(st, used, now, material)
    st.awaiting[chat_id] = {
        "sent_at": now, "source": source, "text": text[:80],
        "followup_used": False,
    }
    st.recent.append({"at": now, "chat_id": chat_id, "source": source,
                      "sources": used, "text": text[:80]})
    if anchor_at:
        st.used_anchors.append(anchor_at)
        st.used_anchors = st.used_anchors[-40:]


def note_dry(st: ProactiveState, *, chat_id: str, source: str, text: str,
             now: float, anchor_at: float = 0.0,
             material: Optional[Dict[str, Any]] = None,
             sources: Optional[List[str]] = None) -> None:
    """预演模式：记下「本来会发什么」，但**不发**。

    为什么要占额度：不占的话一整晚都在预演，看到的是"她每分钟都想说话"，
    那看不出真实频次。占额度才能回答"如果我开着她一天会说几句"。

    为什么不进 awaiting：那条消息根本没出去，不可能有人回，
    放在待回里会让追问逻辑以为"发了没回"。
    """
    used = [s for s in (sources or [source]) if s]
    st.sent_today += 1
    st.last_sent_at = now
    st.last_source = source
    _touch_sources(st, used, now, material)
    st.dry.append({"at": now, "chat_id": chat_id, "source": source,
                   "sources": used, "text": text[:80]})
    if anchor_at:
        st.used_anchors.append(anchor_at)
        st.used_anchors = st.used_anchors[-40:]


def note_peer_replied(st: ProactiveState, *, chat_id: str, now: float) -> bool:
    """对方回来了。清掉待追问，并让"被无视"的等级回落一级。

    回落是一级一级的：他出现一次，她从"基本不主动"回到"还会试试"，
    但不会立刻满血 —— 连着两次没回，一句"在吗"消不完。

    `penalty_until` 之前不赎回：她还在那个劲儿里，不会因为一条消息翻篇。
    这就是余韵（管语气）和这里（管主动频率）分工的地方。
    """
    had = st.awaiting.pop(chat_id, None) is not None
    level = int(getattr(st, "neglect_level", 0) or 0)
    if level > 0:
        if now >= float(getattr(st, "penalty_until", 0) or 0):
            st.neglect_level = level - 1
        return True
    return had


def mark_followed_up(st: ProactiveState, *, chat_id: str, now: float,
                     text: str) -> None:
    item = st.awaiting.get(chat_id)
    if not isinstance(item, dict):
        return
    item["followup_used"] = True
    item["followup_at"] = now
    item["followup_text"] = text[:80]
    st.awaiting[chat_id] = item
    st.last_sent_at = now
    st.recent.append({"at": now, "chat_id": chat_id, "source": "followup",
                      "text": text[:80]})


def mark_forgotten(st: ProactiveState, *, chat_id: str, now: float) -> None:
    """她忘了。记进 dropped，但不主动提。

    `reacted=False` 是 p0-1 加的：这条记录会先被情绪层消费一次
    （她心里沉一下、主动收敛），消费完才置 True。
    以前没有任何代码读 dropped，所以"发了没人回"对她没有任何影响。
    """
    item = st.awaiting.pop(chat_id, None)
    if not isinstance(item, dict):
        return
    st.dropped.append({
        "at": now, "chat_id": chat_id,
        "source": str(item.get("source") or ""),
        "text": str(item.get("text") or "")[:80],
        "reacted": False,
    })


def expire_neglect(st: ProactiveState, *, now: float,
                   hours: float = NEGLECT_AFTER_HOURS) -> int:
    """超过 NEGLECT_AFTER_HOURS 还没回、也没追问的，转成"被无视"。

    为什么比 FOLLOWUP_GIVE_UP_HOURS（30h）短这么多：
    追问是"她还想说"，等 30 小时是为了给足对方出现的窗口；
    被无视是"她承认这次没接上"，8 小时已经足够一个白天了。
    两个判据分开，才不会出现"她等了一个整天还装作没事"。
    """
    n = 0
    for chat_id in list(st.awaiting):
        item = st.awaiting.get(chat_id)
        if not isinstance(item, dict):
            st.awaiting.pop(chat_id, None)
            n += 1
            continue
        if _hours_since(float(item.get("sent_at") or 0), now) >= hours:
            mark_forgotten(st, chat_id=chat_id, now=now)
            n += 1
    return n


def unreacted_dropped(st: ProactiveState) -> List[Dict[str, Any]]:
    """还没被情绪层消费过的"没回"记录。"""
    out = []
    for item in st.dropped or []:
        if isinstance(item, dict) and not item.get("reacted"):
            out.append(item)
    return out


def note_neglect_reacted(st: ProactiveState, items: List[Dict[str, Any]], *,
                         now: float) -> int:
    """消费掉这批"没回"，并让被无视的等级 +1。返回新等级。"""
    for item in items:
        if isinstance(item, dict):
            item["reacted"] = True
    level = min(NEGLECT_MAX, int(getattr(st, "neglect_level", 0) or 0) + 1)
    st.neglect_level = level
    st.penalty_until = max(float(getattr(st, "penalty_until", 0) or 0),
                           now + NEGLECT_PENALTY_HOURS * 3600)
    return level


def penalty(st: ProactiveState, *, now: float) -> float:
    """主动开口的分数系数。1.0 = 没有惩罚。

    为什么乘在分数上、而不是直接不给候选：
    乘分数会同时降命中率和排序位次，但**不会把话说死** ——
    哪天真有很重的事（重要日期、未完话头），分数够高还是能穿过去。
    如果直接返回空候选，她会连"你上次说要体检"都不提了，
    那不是收敛，那是坏掉。
    """
    level = int(getattr(st, "neglect_level", 0) or 0)
    if level <= 0:
        return 1.0
    idx = max(0, min(len(NEGLECT_PENALTY) - 1, level))
    return float(NEGLECT_PENALTY[idx])


def candidate_check_in(st: ProactiveState, *, chat_id: str,
                       now: float) -> Optional[Candidate]:
    """连着两批没回之后，她可能会开口问一句"你最近是不是很忙"。

    这是 p0-1 里唯一一次"她要一个答案"的主动。语气克制：
    不是质问、不是撒娇、不是最后通牒 —— 是试探。
    真人第三次被放鸽子才会开口问，前两次只会自己往回收。

    分数给 78：高到能穿过日常竞争，但低于重要日期（88），
    所以她不会因为你生日那天也先问"你忙吗"。
    """
    level = int(getattr(st, "neglect_level", 0) or 0)
    if level < CHECK_IN_MIN_LEVEL:
        return None
    last = float(getattr(st, "last_check_in_at", 0) or 0)
    if last and (now - last) < CHECK_IN_COOLDOWN_HOURS * 3600:
        return None
    recent = st.dropped[-1] if st.dropped else {}
    silent = _hours_since(float(recent.get("at") or 0), now) if recent else 0.0
    reason = (f"你前面主动说了 {level + 1} 次，他一次都没接。"
              f"你不是在质问，是想确认一下他最近是不是真的很忙。")
    return Candidate(
        source="check_in", chat_id=chat_id, score=78.0, reason=reason,
        layer="tone",
        tone_text="你有点拿不准他是不是不想理你了，但还是愿意先问一句",
        material={"neglect_level": level, "silent_hours": round(silent, 1)},
        anchor_at=float(recent.get("at") or 0))


def expire_awaiting(st: ProactiveState, *, now: float) -> int:
    """超时没回、也没追问的，一律转 dropped。返回清掉几条。"""
    n = 0
    for chat_id in list(st.awaiting):
        item = st.awaiting.get(chat_id)
        if not isinstance(item, dict):
            st.awaiting.pop(chat_id, None)
            n += 1
            continue
        if _hours_since(float(item.get("sent_at") or 0), now) > FOLLOWUP_GIVE_UP_HOURS:
            mark_forgotten(st, chat_id=chat_id, now=now)
            n += 1
    return n


# ══════════════════════════════════════════════════════════════════════
# 对外一句话
# ══════════════════════════════════════════════════════════════════════

def summary(st: ProactiveState, *, now: float) -> str:
    wait = 0
    if st.awaiting:
        wait = len(st.awaiting)
    return (f"今天已发 {st.sent_today} 条"
            f"，待回 {wait} 条"
            f"，上次 {datetime.fromtimestamp(st.last_sent_at).strftime('%H:%M') if st.last_sent_at else '从未'}")
