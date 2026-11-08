"""[bigfish] 她的生活状态：醒着还是睡着、还剩多少力气、手头在做什么。

═══════════════════════════════════════════════════════════════════════
为什么这是一个「器官」，不是插件
═══════════════════════════════════════════════════════════════════════

灵魂（860）、心情（900）、生活（871）是同一层的东西。
插件可以关 —— 关掉时间感知，时间照样流逝；
但她不会因为你在插件页关了一个开关，就不睡觉了。

所以分三层，任何一层坏掉，其余照常：

    bigfish/life.py               状态机本身（这个文件）
    bigfish/plugins/life.py       它的「注入面」—— 可关，关掉只是她不说
    bigfish/plugins/sleep_gate.py 它的「判定面」—— 可关，关掉只是她睡着也回

═══════════════════════════════════════════════════════════════════════
节律按她自己的生物钟走，**不按对方最后一条消息推**
═══════════════════════════════════════════════════════════════════════

这是设计时被明确纠正的一点。当时的想法是：

    「她最后一条消息之后 40 分钟她也睡」—— 听起来贴心，实际是荒谬的。

反例：你早上 9 点发了一条，然后一整天没说话。
按那个算法，她 9:40 就该睡了 —— 她的一天从早上 9 点 40 结束？
晚上 11 点你发消息，她"刚醒"？这不是人，是回声。

所以是**两个时钟**：

    主时钟（她的）：23:20 ± 42 分钟入睡，睡 8 小时 20 分 ± 35 分钟。
                   每天不一样，像真的作息。你一整天不说话，她照样过这一天。

    扰动（你的）：你深夜还在聊 → 她陪你多撑一会儿（每次 18~38 分钟，
                 一晚最多 96 分钟）。撑不住就"聊着聊着睡着了"，
                 第二天才说 —— 而不是永远精神饱满地陪你到天亮。

睡眠债也在这个模型里：昨晚熬到 2 点 → 今天醒得晚、白天精力上限低、
晚上更早犯困。熬一次是真实代价，不是换了一天白干。

═══════════════════════════════════════════════════════════════════════
"一天"由睡眠驱动，不由时钟推算
═══════════════════════════════════════════════════════════════════════

踩过的坑：最早想用「凌晨 4 点前算昨天」来切天。但她的起床时刻是浮动的
（07:40 ± 35 + 晚醒最多 100 分钟 → 最晚可以 09:55 才起）。任何固定分界线
都会在某个时间段和真实状态打架：

    05:00 启动 → 按 4 点切，她算"今天"，于是被判成醒着，可她刚睡下两小时。
    07:00 启动 → 她已经"新的一天"了，但 wake_at 是 08:10，还在睡。

所以：**`day` 只在醒来那一刻推进**，其余时间不猜。
冷启动（没有状态文件）才用 `_guess_asleep()` 按"现在像不像睡觉的点"猜一次，
猜完立刻落盘，之后永远以状态为准。

═══════════════════════════════════════════════════════════════════════
三个变量
═══════════════════════════════════════════════════════════════════════

    awake    睡着 / 醒着 / 刚醒
    energy   0-100。醒着缓慢掉，睡着慢慢回；熬夜掉得更快。
    doing    手头在做什么 —— **只有这里写了的，她才可以说出来**。

`doing` 为什么必须稳定：
    如果每次算一遍，同一小时里她一会儿在洗碗、一会儿在看书，
    用户问一句「你在干嘛」，两次答案不一样，人设就崩了。
    所以按 `day + 时段` 做确定性取样，同一天同一段永远同一个答案。
    这是 D-36「事实必须有真来源」在生活层的对应物：
    她讲出来的事，必须能在这个文件里找到。

═══════════════════════════════════════════════════════════════════════
写盘策略
═══════════════════════════════════════════════════════════════════════

tick 每 5 秒一次，但**只在状态真的变了才写**（入睡 / 醒来 / 换段 / 跨天 /
精力跨越阈值）。否则 5 秒一次 JSON 写盘，SSD 会先死。

状态文件：`<数据根>/life.json`，和 mood.json / soul.json 同级。
写失败不抛 —— 她的生活不该因为磁盘满就停下来。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

STATE_NAME = "life.json"
STATE_VERSION = 1

# ══════════════════════════════════════════════════════════════════════
# 节律参数
# ══════════════════════════════════════════════════════════════════════

#: 目标入睡（一天中的分钟数）
BEDTIME_BASE = 23 * 60 + 20          # 23:20

#: 基础睡眠时长（分钟）。醒来时刻 = 入睡时刻 + 这个值。
#:
#: 用"时长"而不是"固定醒来钟点"：这样入睡晚了她自然醒得晚，
#: 不需要在两处分别算，也不会出现"睡 3 小时就起床"的情况。
SLEEP_BASE_MINUTES = 8 * 60 + 20     # 8h20m

#: 每天抖多少（分钟，双向）
#:
#: 一个 23:20:00 准点上床的角色，两天就会被看出是程序。
#: 42 分钟是拍的量级：足够看出"今天早一点/今天晚一点"，
#: 又不至于某天 22:30 就睡、某天 00:30 才睡，让人觉得作息混乱。
BEDTIME_JITTER = 42
SLEEP_JITTER = 35

#: 睡眠时长上下限（防手改配置 / 防极端抖动）
SLEEP_MIN_MINUTES = 6 * 60
SLEEP_MAX_MINUTES = 10 * 60

#: 陪伴延长：对方还在聊，她可以多撑一会儿
EXTEND_STEP_MIN = 18
EXTEND_STEP_MAX = 38
EXTEND_CAP_MINUTES = 96              # 一晚最多延长 96 分钟
EXTEND_QUIET_MINUTES = 12            # 12 分钟没人说话 → 不再延长，正常去睡
EXTEND_THROTTLE_MINUTES = 8          # 两条消息之间至少隔 8 分钟才再续一次
EXTEND_WINDOW_MINUTES = 40           # 入睡前 40 分钟才进入"要不要延长"的区间

#: 睡眠债（分钟）。每欠 1 小时，第二天晚醒 30 分钟，上限 100 分钟。
DEBT_WAKE_PER_HOUR = 0.5
DEBT_WAKE_CAP_MINUTES = 100
DEBT_CAP_MINUTES = 240               # 债本身封顶 4 小时（再多也没意义）
DEBT_DECAY = 0.55                    # 每天只还掉 45%

#: 精力曲线（每小时）
ENERGY_AWAKE_DRAIN = 3.4             # 醒着的基础消耗
ENERGY_TALK_DRAIN = 0.22             # 每回一轮额外消耗（"聊天也累"）
ENERGY_OVERTIME_DRAIN = 3.2          # 超过入睡点还撑着，额外消耗
ENERGY_SLEEP_GAIN = 11.5             # 睡着每小时恢复
ENERGY_FLOOR = 6.0                   # 不会真的归零（归零等于失能）
ENERGY_WAKE_BASE = 88.0              # 睡够的起床精力
ENERGY_WAKE_DEBT_PENALTY = 0.35      # 每 1 分钟债扣多少起床精力
ENERGY_WAKE_FLOOR = 42.0             # 起床精力的下限
ENERGY_FRESH = 78.0                  # 冷启动时假设的精力
ENERGY_NAP_PER_MINUTE = 0.42         # 午睡每分钟恢复多少（20 分钟 ≈ +8）

#: 午睡（只在下午，概率性）
NAP_START_MIN = 13 * 60              # 13:00 之后才可能
NAP_END_MIN = 16 * 60                # 16:00 之后不再睡
NAP_PROBABILITY = 0.18               # 一天 18% 的概率午睡
NAP_MINUTES_MIN = 18
NAP_MINUTES_MAX = 42

#: 精力阈值
DOZING_THRESHOLD = 28.0              # 低于这个开始"困"
SPENT_THRESHOLD = 12.0               # 低于这个是"强撑"

#: 状态写盘的最小间隔（秒）—— 变了才写，别 5 秒一次
SAVE_MIN_INTERVAL = 30.0

#: 冷启动时"多久没动过就算很久"（小时）
RESUME_GAP_HOURS = 26.0


# ══════════════════════════════════════════════════════════════════════
# 手头在做什么
# ══════════════════════════════════════════════════════════════════════
#
# 每条是 (描述, 权重)。权重决定同一天里这件事出现的相对概率。
# 小时按 24 小时制；跨夜时段（如 23 → 28）表示到次日 04:00。
#
# 写这些条目时的三条自我约束：
#   1. 都是**不需要持续注意**的日常事 —— 她随时能停下来聊天。
#      不写"在开会""在医院"这种会让人问"那你怎么有空回我"的事。
#   2. 不写需要对方配合的事（"在等你""在想你"）—— 那是话，不是生活。
#   3. 不写会引出一串追问的具体承诺（"在给你做生日礼物"）——
#      P0 阶段她记不住后续，说了就穿帮。
DOING_SLOTS: Tuple[Tuple[float, float, str, Tuple[Tuple[str, int], ...]], ...] = (
    (4, 7, "凌晨", (
        ("还没睡，躺着刷手机", 40),
        ("起来喝了口水", 30),
        ("有点睡不着", 30),
    )),
    (7, 9, "清晨", (
        ("刚洗漱完", 35),
        ("在煮点东西", 25),
        ("把窗帘拉开了", 20),
        ("在阳台站了会儿", 20),
    )),
    (9, 11, "上午", (
        ("在收拾屋子", 25),
        ("在看点东西", 25),
        ("在弄昨天没弄完的事", 25),
        ("泡了杯茶", 25),
    )),
    (11, 13, "中午", (
        ("在做饭", 30),
        ("刚吃完", 30),
        ("在等外卖", 20),
        ("在想要吃什么", 20),
    )),
    (13, 15, "午后", (
        ("在看剧", 30),
        ("趴着歇了会儿", 25),
        ("在整理照片", 20),
        ("在窗边坐着", 25),
    )),
    (15, 17, "下午", (
        ("在写点东西", 25),
        ("在听歌", 25),
        ("下楼买了点东西", 25),
        ("在发呆", 25),
    )),
    (17, 19, "傍晚", (
        ("在做饭", 30),
        ("收拾厨房", 25),
        ("在外面走一圈", 25),
        ("刚洗完澡", 20),
    )),
    (19, 21, "晚上", (
        ("在刷手机", 30),
        ("在看剧", 25),
        ("在看书", 25),
        ("在整理明天要用的东西", 20),
    )),
    (21, 23, "夜里", (
        ("在看书", 30),
        ("在发呆", 25),
        ("在洗漱", 25),
        ("躺着不太想动", 20),
    )),
    (23, 28, "深夜", (              # 23:00 → 次日 04:00
        ("躺着看手机", 40),
        ("有点困了", 30),
        ("在关灯", 30),
    )),
)


# ══════════════════════════════════════════════════════════════════════
# 数据
# ══════════════════════════════════════════════════════════════════════

@dataclass
class LifeState:
    """一份会过日子的状态。字段改动要同步 load() 的兜底。"""

    version: int = STATE_VERSION
    #: 当前"天"的键（YYYY-MM-DD）。**只在醒来时推进**，不由时钟推。
    day: str = ""
    #: 是否睡着
    asleep: bool = False
    #: 0-100
    energy: float = ENERGY_FRESH
    #: 手头在做什么（必须来自 DOING_SLOTS）
    doing: str = ""
    #: doing 属于哪个时段（用来判断该不该换）
    doing_slot: str = ""
    #: 本次入睡的时刻（epoch 秒）
    asleep_since: float = 0.0
    #: 今天的目标入睡时刻（epoch 秒）
    bedtime: float = 0.0
    #: 今天的目标醒来时刻（epoch 秒）
    wake_at: float = 0.0
    #: 今晚已经为陪聊延长了多少分钟
    extended_minutes: float = 0.0
    #: 当晚最后一次延长时刻（用来判"多久没人说话了"）
    extended_at: float = 0.0
    #: 睡眠债（分钟）
    sleep_debt: float = 0.0
    #: 最近一次醒来（epoch 秒）。刚醒时语气上会露出来。
    just_woke_at: float = 0.0
    #: 今天有没有午睡过
    napped: bool = False
    #: 当前这一觉是不是午睡。**醒来时要分开处理** ——
    #: 午睡不是"新的一天"，不能推进 day、不能重排日程、不能衰减睡眠债。
    #: 第一版没分，结果 13:00 睡 19 分钟 → 醒 → 债被衰减 → 精力还低 →
    #: 13:25 又睡，一天在午睡里循环了八次。
    napping: bool = False
    #: 上一次 tick
    last_tick: float = 0.0
    #: 睡着期间收到的消息：{chat_id: {"count","first_at","last_at","preview"}}
    pending: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: 上次写盘的时刻与指纹（避免无变化写盘）
    saved_at: float = 0.0
    saved_fingerprint: str = ""


@dataclass
class LifeEvent:
    """tick 产生的一个事件。引擎据此记账 / 写日志，插件据此注入。"""

    kind: str            # new_day / asleep / woke / nap_start / dozing / spent / doing / resume
    text: str            # 人话，进日志
    at: float = 0.0


# ══════════════════════════════════════════════════════════════════════
# 路径
# ══════════════════════════════════════════════════════════════════════

def state_path(root: Optional[str] = None) -> str:
    from bigfish.vault import resolve_data_root

    return os.path.join(root or resolve_data_root(), STATE_NAME)


# ══════════════════════════════════════════════════════════════════════
# 读 / 写
# ══════════════════════════════════════════════════════════════════════

def _fingerprint(state: LifeState) -> str:
    """状态里真正会变、且值得写盘的那几个字段。"""
    raw = json.dumps(
        [state.day, state.asleep, round(state.energy, 2), state.doing,
         state.doing_slot, round(state.asleep_since), round(state.bedtime),
         round(state.wake_at), round(state.extended_minutes),
         round(state.extended_at),
         round(state.sleep_debt, 1), round(state.just_woke_at), state.napped,
         state.napping,
         # ⚠ 这里必须序列化**整个 pending**，不能只取键。
         #   只取键的话，同一个会话连发三条消息时指纹不变，
         #   save() 会以为"没变化"而跳过 —— count 永远停在 1。
         json.dumps(state.pending, ensure_ascii=False, sort_keys=True)],
        ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def load(root: Optional[str] = None) -> LifeState:
    """读状态。文件缺失 / 损坏 / 版本不符 → 一份新的一天，不抛。

    她的生活不该因为一个坏 JSON 就停下来；但也不能静默当成"一切正常" ——
    调用方可以从 `state.day == ""` 看出这是刚初始化。
    """
    path = state_path(root)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return LifeState()
    if not isinstance(raw, dict):
        return LifeState()
    state = LifeState()
    for key, value in raw.items():
        if not hasattr(state, key):
            continue
        try:
            setattr(state, key, value)
        except Exception:
            continue
    # 类型兜底：手改过的文件什么都能装进来
    try:
        state.energy = max(0.0, min(100.0, float(state.energy)))
    except Exception:
        state.energy = ENERGY_FRESH
    for name in ("sleep_debt", "extended_minutes", "bedtime", "wake_at",
                 "asleep_since", "last_tick", "just_woke_at", "extended_at"):
        try:
            setattr(state, name, float(getattr(state, name) or 0.0))
        except Exception:
            setattr(state, name, 0.0)
    if not isinstance(state.pending, dict):
        state.pending = {}
    if not isinstance(state.day, str):
        state.day = ""
    if not isinstance(state.doing, str):
        state.doing = ""
    if not isinstance(state.doing_slot, str):
        state.doing_slot = ""
    state.napped = bool(state.napped)
    state.napping = bool(state.napping)
    return state


def save(state: LifeState, *, force: bool = False,
         root: Optional[str] = None) -> bool:
    """写状态。无变化且未到间隔就跳过。失败返回 False，不抛。"""
    now = time.time()
    fp = _fingerprint(state)
    if (not force and fp == state.saved_fingerprint
            and now - state.saved_at < SAVE_MIN_INTERVAL):
        return False
    state.saved_fingerprint = fp
    state.saved_at = now
    try:
        from bigfish.vault import _atomic_write_json

        _atomic_write_json(state_path(root), asdict(state))
        return True
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════
# 时间工具
# ══════════════════════════════════════════════════════════════════════

def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(float(ts))


def day_key(ts: float) -> str:
    """这一"天"的键（YYYY-MM-DD）。

    ⚠ 只用于**生成种子**和日志。真正推进"哪一天"的是 `plan_day()`，
      由醒来触发。不要把它当成"现在属于哪一天"的权威判断。
    """
    return _local(ts).strftime("%Y-%m-%d")


def _minutes_of_day(ts: float) -> float:
    dt = _local(ts)
    return dt.hour * 60 + dt.minute + dt.second / 60.0


def _at_minutes(now: float, minutes: float) -> float:
    """把"一天中的第 N 分钟"还原成 epoch 秒（相对于 now 所在的那一天）。

    `minutes` 允许 > 1440（次日）。
    """
    base = _local(now).replace(hour=0, minute=0, second=0, microsecond=0)
    return (base + timedelta(minutes=float(minutes))).timestamp()


def _seed(*parts: Any) -> random.Random:
    """确定性随机。同一个种子永远同一个结果 —— 用来让 doing 一天内稳定。"""
    text = ":".join(str(p) for p in parts)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return random.Random(int(digest, 16))


def _jitter(span: int, *seed_parts: Any) -> float:
    """[-span, +span] 之间的确定性抖动（分钟）。"""
    if span <= 0:
        return 0.0
    return _seed(*seed_parts).uniform(-span, span)


# ══════════════════════════════════════════════════════════════════════
# 一天的计划
# ══════════════════════════════════════════════════════════════════════

def _debt_wake_late(debt: float) -> float:
    return min(DEBT_WAKE_CAP_MINUTES, max(0.0, debt) * DEBT_WAKE_PER_HOUR)


def plan_day(state: LifeState, now: float, *, waking: bool = False) -> None:
    """给她安排这一天的入睡与醒来时刻。

    调用时机只有两个：**醒来**、以及**冷启动**。
    （醒着跨过午夜不重排 —— 她还没睡，那一天没结束。）

    抖动 + 睡眠债都体现在这里：

        昨晚熬到 2 点 → 今天醒得晚（债 × 0.5，上限 100 分钟）
        债不清零，只衰减 ×0.55 —— 连续熬三天，不是第三天一觉就恢复

    `waking=True` 表示"这是在醒来那一刻重排"，此时老睡眠债刚刚生效过，
    要立刻衰减，否则会连续两天按同一笔债算。
    """
    key = day_key(now)
    debt = max(0.0, min(DEBT_CAP_MINUTES, float(state.sleep_debt or 0.0)))

    bed_minutes = (BEDTIME_BASE
                   + _jitter(BEDTIME_JITTER, key, "bed")
                   + debt * 0.15)          # 欠债的人更容易犯困，但只挪一点点

    sleep_minutes = (SLEEP_BASE_MINUTES
                     + _jitter(SLEEP_JITTER, key, "sleep")
                     + _debt_wake_late(debt))
    sleep_minutes = max(SLEEP_MIN_MINUTES, min(SLEEP_MAX_MINUTES, sleep_minutes))

    state.day = key
    state.bedtime = _at_minutes(now, bed_minutes)
    state.wake_at = state.bedtime + sleep_minutes * 60

    state.extended_minutes = 0.0
    state.extended_at = 0.0
    state.napped = False
    state.napping = False

    if waking:
        # 老债已经兑现成"今天醒得晚"，现在衰减；不清零是因为欠债是累积的。
        state.sleep_debt = round(debt * DEBT_DECAY, 1)


def asleep_window(state: LifeState, now: float) -> int:
    """now 落在她哪一个睡眠窗口里。0 = 醒着，+1 = 今晚这一觉，-1 = 昨晚那一觉。

    ⚠ 必须用 epoch 比较，不能用"一天中的分钟数"。
      入睡点会跨午夜（23:20 + 42 分钟抖动 = 次日 00:02），
      `_minutes_of_day(次日00:02)` 会折成 2 —— 于是 23:50 被算成"早就过了入睡点"。
      这个坑第一版踩过：所有深夜判断都错位一小时。
    """
    if state.bedtime <= now < state.wake_at:
        return 1
    if state.bedtime - 86400 <= now < state.wake_at - 86400:
        return -1
    return 0


def cold_start(state: LifeState, now: float) -> List[LifeEvent]:
    """没有历史状态时，判断现在该是醒着还是睡着。

    只在"第一次跑 / 状态文件丢了"时用。猜完立刻落盘，
    之后永远以 `state.asleep` 为准 —— 不再靠时钟推断。
    """
    events: List[LifeEvent] = []
    plan_day(state, now, waking=False)

    window = asleep_window(state, now)
    asleep = window != 0
    if window == -1:
        # 还在昨晚那一觉里：把整段窗口回退一天，否则她会"在床上躺 29 小时"
        state.bedtime -= 86400
        state.wake_at -= 86400

    state.asleep = bool(asleep)
    if asleep:
        state.asleep_since = state.bedtime
        state.energy = wake_energy(state, now)
        state.just_woke_at = 0.0
        events.append(LifeEvent(
            "resume_sleep", f"启动时她正在睡（{_local(state.bedtime).strftime('%H:%M')} 入睡）", now))
    else:
        state.asleep_since = 0.0
        state.energy = ENERGY_FRESH
        refresh_doing(state, now)

    state.last_tick = now
    return events


def wake_energy(state: LifeState, now: float) -> float:
    """睡醒那一刻的精力。睡得够不够、债还不还，全在这里。"""
    slept_min = max(0.0, (now - state.asleep_since) / 60.0) if state.asleep_since else 0.0
    slept_min = min(slept_min, 14 * 60)      # 睡过头不会无限加分
    base = ENERGY_WAKE_BASE
    diff_h = (slept_min - 7 * 60) / 60.0
    # 少睡 1 小时扣 3 分；多睡 1 小时加 1 分（边际递减）
    base += 3.0 * diff_h if diff_h < 0 else 1.0 * min(diff_h, 3)
    base -= float(state.sleep_debt or 0.0) * ENERGY_WAKE_DEBT_PENALTY
    return max(ENERGY_WAKE_FLOOR, min(100.0, base))


# ══════════════════════════════════════════════════════════════════════
# 手头在做什么
# ══════════════════════════════════════════════════════════════════════

def _slot_for(now: float) -> Tuple[str, Tuple[Tuple[str, int], ...]]:
    """按当前时刻找到所属时段与候选事。

    ⚠ DOING_SLOTS 里的小时是**小时**，这里要换成分钟再比。
      第一版漏了 ×60，于是 15:30 被当成 930 < 17 —— 全部落到兜底那句。
    """
    minute = _minutes_of_day(now)
    for start, end, label, items in DOING_SLOTS:
        lo, hi = float(start) * 60, float(end) * 60
        # ⚠ 判据是「结束时间超过 24 点」，不是「开始时间超过 24 点」。
        #   (23, 28) 的 start=23 没超 24，但 end=28 超了 —— 它才是跨夜那一档。
        #   第一版写成 `lo >= 24*60`，于是 00:00~03:59 谁都不认领，
        #   整晚落到兜底的「在忙自己的事」。
        if hi > 24 * 60:
            lo -= 24 * 60
            hi -= 24 * 60
            if minute >= lo or minute < hi:
                return label, items
            continue
        if lo <= minute < hi:
            return label, items
    # DOING_SLOTS 覆盖 24 小时，理论上到不了这里；兜底给一句中性的话
    return "别的时候", (("在忙自己的事", 1),)


def _pick_doing(day: str, label: str, items: Tuple[Tuple[str, int], ...]) -> str:
    """同一 `day + 时段` 永远同一个答案。

    这样「你在干嘛」问三次，三次一样。换了时段才换事。
    """
    if not items:
        return ""
    rng = _seed(day, label, "doing")
    total = sum(max(1, int(w)) for _t, w in items)
    hit = rng.uniform(0, total)
    acc = 0.0
    for text, weight in items:
        acc += max(1, int(weight))
        if hit <= acc:
            return text
    return items[-1][0]


def refresh_doing(state: LifeState, now: float) -> bool:
    """跨时段才换事。返回是否变了。

    种子用 `day_key(now)` 而不是 `state.day`：跨过午夜还醒着时，
    "深夜"这一档也该按新的一天重新取样，否则她会连着两晚做同一件事。
    """
    label, items = _slot_for(now)
    if state.doing_slot == label and state.doing:
        return False
    state.doing_slot = label
    state.doing = _pick_doing(day_key(now), label, items)
    return True


# ══════════════════════════════════════════════════════════════════════
# 推进
# ══════════════════════════════════════════════════════════════════════

def tick(state: LifeState, now: Optional[float] = None, *,
         talk_turns: int = 0) -> List[LifeEvent]:
    """推进她的生活。**只改 state，不读不写磁盘。**

    `talk_turns`：上次 tick 到现在她回了几轮。聊天也累，这里计入消耗。

    顺序是硬约束：
        1. 冷启动（没有 day）→ 猜一次，之后永远以状态为准
        2. 时间跳变过大 → 重新对齐
        3. **睡着 → 只判醒来**（醒来时才推进 day）
        4. 醒着 → 判精力、午睡、入睡

    ⚠ 第 3 步必须在"跨天"之前。第一版把跨天放在最前，于是早上 06:40
      醒来时先被 plan_day 重排了一次，wake_at 变成"今天 07:40"（还没到），
      醒来判定永远不成立 —— 她会一直睡下去。
    """
    now = float(now if now is not None else time.time())
    events: List[LifeEvent] = []

    # ── 1. 冷启动 ──────────────────────────────────────────────────
    if not state.day:
        return cold_start(state, now)

    # ── 2. 时间跳变（程序停了一整天以上）────────────────────────────
    last = state.last_tick or now
    gap_hours = max(0.0, (now - last) / 3600.0)
    if gap_hours > RESUME_GAP_HOURS:
        events.append(LifeEvent(
            "resume", f"很久没运行（{gap_hours:.0f} 小时），按当前时刻重新对齐", now))
        state.asleep = False
        state.asleep_since = 0.0
        plan_day(state, now, waking=True)
        state.energy = ENERGY_FRESH
        state.last_tick = now
        refresh_doing(state, now)
        window = asleep_window(state, now)
        if window:
            if window == -1:
                state.bedtime -= 86400
                state.wake_at -= 86400
            state.asleep = True
            state.asleep_since = state.bedtime if state.bedtime <= now else now
            events.append(LifeEvent("resume_sleep", "按当前时刻判为睡眠中", now))
        return events

    hours = gap_hours

    # ── 3. 睡着：只判醒来 ──────────────────────────────────────────
    if state.asleep:
        state.energy = min(100.0, state.energy + ENERGY_SLEEP_GAIN * hours)
        if now >= state.wake_at:
            slept_min = ((now - state.asleep_since) / 60.0) if state.asleep_since else 0.0
            slept_min = max(0.0, slept_min)
            # ⚠ 起床精力要用**旧债**算，所以必须在 plan_day 之前结算
            energy_after = wake_energy(state, now)
            debt_before = state.sleep_debt

            state.asleep = False
            state.asleep_since = 0.0

            if state.napping:
                # ── 午睡醒了 ──
                #
                # 这不是"新的一天"：不推进 day、不重排日程、不衰减债。
                # 只是把那 20 分钟睡回来了。第一版按主睡眠处理，
                # 于是每睡一次就把债还掉一大截，一天能醒八次。
                state.napping = False
                # 小睡按"睡了多少分钟"给，不用 wake_energy ——
                # 那是"睡了一整晚"的公式，套在 20 分钟上会算出"没睡够，-20 分"。
                state.energy = min(100.0, state.energy + slept_min * ENERGY_NAP_PER_MINUTE)
                refresh_doing(state, now)
                events.append(LifeEvent(
                    "nap_end", f"午睡醒了（{slept_min:.0f} 分钟，精力 {state.energy:.0f}）", now))
                state.last_tick = now
                return events

            plan_day(state, now, waking=True)
            state.energy = energy_after
            state.just_woke_at = now
            refresh_doing(state, now)

            tail = f"，还欠 {state.sleep_debt:.0f} 分钟" if state.sleep_debt >= 1 else ""
            if debt_before >= 1:
                tail = f"，昨晚欠下 {debt_before:.0f} 分钟，今天还了 {(debt_before - state.sleep_debt):.0f}"
            events.append(LifeEvent(
                "woke",
                f"醒了（睡了 {slept_min / 60:.1f} 小时，精力 {state.energy:.0f}{tail}）",
                now))
        state.last_tick = now
        return events

    # ── 4. 醒着 ────────────────────────────────────────────────────
    drain = ENERGY_AWAKE_DRAIN * hours
    drain += ENERGY_TALK_DRAIN * max(0, int(talk_turns))
    overtime = now - state.bedtime
    if overtime > 0:
        drain += ENERGY_OVERTIME_DRAIN * (overtime / 3600.0)
    before = state.energy
    state.energy = max(ENERGY_FLOOR, state.energy - drain)

    # 4.1 跨过阈值才报（不然每 5 秒刷一条）
    if before >= DOZING_THRESHOLD > state.energy:
        events.append(LifeEvent("dozing", f"开始困了（精力 {state.energy:.0f}）", now))
    if before >= SPENT_THRESHOLD > state.energy:
        events.append(LifeEvent("spent", f"快撑不住了（精力 {state.energy:.0f}）", now))

    # 4.2 午睡
    if (not state.napped
            and NAP_START_MIN <= _minutes_of_day(now) < NAP_END_MIN
            and state.energy < 70
            and _seed(state.day, "nap-roll").random() < NAP_PROBABILITY):
        minutes = _seed(state.day, "nap-len").uniform(NAP_MINUTES_MIN, NAP_MINUTES_MAX)
        state.napped = True
        state.napping = True
        state.asleep = True
        state.asleep_since = now
        # 午睡不改日程：醒来时按 napping 分支走，不重排计划。
        state.wake_at = now + minutes * 60
        events.append(LifeEvent("nap_start", f"午睡 {minutes:.0f} 分钟", now))
        state.last_tick = now
        return events

    # 4.3 到点睡觉
    deadline = state.bedtime + state.extended_minutes * 60
    if now >= deadline:
        late_min = max(0.0, (now - state.bedtime) / 60.0)
        state.sleep_debt = round(
            min(DEBT_CAP_MINUTES, state.sleep_debt + late_min), 1)
        state.asleep = True
        state.asleep_since = now
        # 醒来时刻按"今晚该睡多久"重算，而不是沿用今天的 wake_at
        sleep_minutes = SLEEP_BASE_MINUTES + _jitter(
            SLEEP_JITTER, day_key(now), "sleep") + _debt_wake_late(state.sleep_debt)
        sleep_minutes = max(SLEEP_MIN_MINUTES, min(SLEEP_MAX_MINUTES, sleep_minutes))
        state.wake_at = now + sleep_minutes * 60
        events.append(LifeEvent(
            "asleep", f"睡了（比计划晚 {late_min:.0f} 分钟，债 {state.sleep_debt:.0f}）", now))
        state.last_tick = now
        return events

    # 4.4 换手头的事
    if refresh_doing(state, now):
        events.append(LifeEvent("doing", f"手头：{state.doing}", now))

    state.last_tick = now
    return events


def _looks_like_sleep_time(state: LifeState, now: float) -> bool:
    """现在像不像她的睡觉时间（只用于"很久没运行"之后的对齐）。"""
    return asleep_window(state, now) != 0


# ══════════════════════════════════════════════════════════════════════
# 外部输入
# ══════════════════════════════════════════════════════════════════════

def note_pending(state: LifeState, chat_id: str, text: str, now: float) -> None:
    """她睡着时收到消息：记账，不叫醒。醒来才补一句。

    ⚠ 只记会话和一句预览，不记全文 —— 全文在微信库里，记两份会漂移。
    """
    if not chat_id:
        return
    item = state.pending.get(chat_id)
    if not isinstance(item, dict):
        item = {"count": 0, "first_at": now, "last_at": now, "preview": ""}
        state.pending[chat_id] = item
    item["count"] = int(item.get("count") or 0) + 1
    item["last_at"] = now
    item["preview"] = (text or "").strip()[:40]
    item["first_at"] = item.get("first_at") or now


def take_pending(state: LifeState, chat_id: str) -> Optional[Dict[str, Any]]:
    """取走一个会话的 pending（取完就清，避免反复说"昨晚你发过消息"）。"""
    item = state.pending.pop(chat_id, None)
    return item if isinstance(item, dict) else None


def peek_pending(state: LifeState, chat_id: str) -> Optional[Dict[str, Any]]:
    item = state.pending.get(chat_id)
    return item if isinstance(item, dict) else None


def expire_pending(state: LifeState, now: float, *, hours: float = 30.0) -> int:
    """太旧的 pending 清掉。

    昨晚的消息第二天说"我刚看到"是自然的；隔三天还说就变成可疑了。
    """
    drop = []
    for chat_id, item in list(state.pending.items()):
        if not isinstance(item, dict):
            drop.append(chat_id)
            continue
        try:
            last = float(item.get("last_at") or 0.0)
        except Exception:
            last = 0.0
        if not last or (now - last) > hours * 3600:
            drop.append(chat_id)
    for chat_id in drop:
        state.pending.pop(chat_id, None)
    return len(drop)


def try_extend(state: LifeState, now: float) -> bool:
    """对方还在聊，她多撑一会儿。返回是否真的延长了。

    只有**接近或已过入睡点**才可能延长；白天聊多久都不算熬夜。
    一晚总延长有硬上限 —— 她不是不会累的服务。
    """
    if state.asleep:
        return False
    if state.extended_minutes >= EXTEND_CAP_MINUTES:
        return False
    if now < state.bedtime - EXTEND_WINDOW_MINUTES * 60:
        return False
    # 刚延长过就别每条消息都加 —— 一句"在吗"给她续 30 分钟，五句就把
    # 一晚的额度烧完，然后她七点钟就"撑不住"了，反而不像人。
    if state.extended_at:
        gap = now - state.extended_at
        if gap < EXTEND_THROTTLE_MINUTES * 60:
            return False
        if gap > EXTEND_QUIET_MINUTES * 60:
            return False
    rng = _seed(state.day, "extend", int(state.extended_minutes))
    step = rng.uniform(EXTEND_STEP_MIN, EXTEND_STEP_MAX)
    step = min(step, EXTEND_CAP_MINUTES - state.extended_minutes)
    if step <= 0:
        return False
    state.extended_minutes = round(state.extended_minutes + step, 1)
    state.extended_at = now
    return True


# ══════════════════════════════════════════════════════════════════════
# 对外表达
# ══════════════════════════════════════════════════════════════════════

def is_asleep(state: LifeState, now: Optional[float] = None) -> bool:
    return bool(state.asleep)


def condition(state: LifeState, now: Optional[float] = None) -> str:
    """一句话说清她现在的状态。给日志和界面用。"""
    if state.asleep:
        since = _local(state.asleep_since).strftime("%H:%M") if state.asleep_since else "?"
        return f"睡着（{since} 入睡）"
    if state.energy < SPENT_THRESHOLD:
        return f"强撑（精力 {state.energy:.0f}）"
    if state.energy < DOZING_THRESHOLD:
        return f"有点困（精力 {state.energy:.0f}）"
    return f"醒着（精力 {state.energy:.0f}）"


def _clock_text(now: float) -> str:
    return _local(now).strftime("%H:%M")


def render(state: LifeState, now: Optional[float] = None, *,
           for_prompt: bool = True) -> str:
    """渲染成给模型看的一段（life 插件的 render 用）。

    只写**状态里确实有的**：现在几点、醒着还是睡着、手头在做什么。
    没写的一句都不编 —— 这是 D-36「事实必须有真来源」在生活层的落地。
    """
    now = float(now if now is not None else time.time())

    # ⚠ 没有状态文件（第一次跑、或者刚清过数据）就一个字都不说。
    #   在这里编一句「我醒着、状态还行」是最容易犯的错 ——
    #   它看起来无害，但等于告诉模型一个并不存在的处境，
    #   而 D-36 的规矩是「她讲出来的事必须能在文件里查到」。
    if not state.day:
        return ""

    lines: List[str] = []

    if state.asleep:
        since = _local(state.asleep_since).strftime("%H:%M") if state.asleep_since else "?"
        lines.append(f"现在是 {_clock_text(now)}，我已经睡了（{since} 睡的）。")
        if not for_prompt:
            lines.append(f"睡醒时间约 {_local(state.wake_at).strftime('%H:%M')}。")
        return "\n".join(lines)

    lines.append(f"现在是 {_clock_text(now)}，我醒着。")

    if state.doing:
        lines.append(f"手头在：{state.doing}。")

    if state.energy < SPENT_THRESHOLD:
        lines.append("今天差不多耗完了，脑子有点转不动，说话会短一些。")
    elif state.energy < DOZING_THRESHOLD:
        lines.append("有点困了，但还醒着。")
    else:
        lines.append("状态还行。")

    if state.just_woke_at and (now - state.just_woke_at) < 90 * 60:
        lines.append("刚醒没多久。")

    if state.sleep_debt >= 45:
        lines.append(f"这两天睡得不太够（欠 {state.sleep_debt:.0f} 分钟）。")

    if state.extended_minutes > 0:
        lines.append("本来该睡了，还撑着在聊。")

    return "\n".join(lines)


def briefing(state: LifeState, chat_id: str, now: Optional[float] = None) -> str:
    """她睡着期间这个会话有没有人找过她 —— 醒来第一句的素材。

    只描述事实（几条、什么时候），不替她决定怎么说。
    """
    now = float(now if now is not None else time.time())
    item = peek_pending(state, chat_id)
    if not item:
        return ""
    count = int(item.get("count") or 0)
    first = float(item.get("first_at") or now)
    when = _local(first).strftime("%H:%M")
    preview = str(item.get("preview") or "").strip()
    tail = f"，最后一条是「{preview}」" if preview else ""
    return f"我睡着的时候（{when} 起）对方发了 {count} 条消息{tail}。我刚看到。"
