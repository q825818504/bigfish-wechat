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
#:
#: 触发点只有 **13:00 那一刻**：`_next_boundary` 把"当天 13:00"当成一个边界，
#: 过去之后 `nap_open > t` 就不成立了。所以不需要"16:00 之后不再睡"这种
#: 上界判断 —— 22:00 才第一次 tick 时，13:00 早就过去了，压根不会触发。
#: NAP_END_MIN 留着只是**文档**（这段时间是她可能睡着的窗口），代码不读它。
NAP_START_MIN = 13 * 60              # 13:00 之后才可能
NAP_END_MIN = 16 * 60                # 只是说明，见上
NAP_PROBABILITY = 0.18               # 一天 18% 的概率午睡
NAP_MINUTES_MIN = 18
NAP_MINUTES_MAX = 42

#: 精力阈值
DOZING_THRESHOLD = 28.0              # 低于这个开始"困"
SPENT_THRESHOLD = 12.0               # 低于这个是"强撑"

#: 状态写盘的最小间隔（秒）—— 变了才写，别 5 秒一次
SAVE_MIN_INTERVAL = 30.0

# （这里以前有个 RESUME_GAP_HOURS = 26.0 —— "多久没动过就算很久"。
#  它配的是"超过 26 小时就当成一次冷启动"的特判，而那个特判会把一整段
#  时间当成"醒着"推进，于是关一晚上软件、第二天早上打开，她 09:xx 才睡。
#  现在 catch_up 按边界逐段补算，gap 多长都走同一条路径，这个阈值没有
#  存在的理由了。2026-11-10 删。）


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
#: 「什么都没在做」的姿势池。人生有近四成时间是这种状态，
#: 而她原来的 doing 永远非空 —— 那不是勤快，是永动机。
BLANK_DOINGS: Tuple[str, ...] = (
    "没在做什么",
    "瘫着，手机在手上但没看",
    "在发呆",
    "靠着窗，什么也没想",
    "在等水开",
    "刚坐下，还没想好要干什么",
)
#: 落在空白上的概率。0.3 是保守值：再高她会显得没生气，
#: 但一整天每段都在"做某事"一定假。
BLANK_PROBABILITY = 0.3

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
    #: 今天打算做的事（p1-4）。空 = 还没生成，`doing` 退回 DOING_SLOTS 取样。
    #: 每项 {at:"14:00", what:"去趟超市", done:False, shareable:True, from:"fact"}
    #:
    #: 为什么放在 life.json 而不是新文件：日程和 doing 是同一个问题的两级精度
    #: —— doing 是"此刻在做什么"，plan 是"今天打算做什么"。
    #: 分成两份状态，迟早会漂（她说在看书，日程里却在超市）。
    plan: List[Dict[str, Any]] = field(default_factory=list)
    #: plan 是哪一天生成的。跨天不自动清，但生成失败时旧的 plan 不能当今天用。
    plan_day: str = ""
    #: 睡前复盘（p1-5/p1-6）跑过哪一天。一天只跑一次 ——
    #: 它要调一次模型，重复跑只会让她每天多写几条一样的"我觉得"。
    narrative_day: str = ""
    #: 今天的日记写到哪一天了。一天一篇，重写同一天会覆盖。
    journal_day: str = ""
    #: 今天做过的事（按时段记，最多 12 条）。
    #:
    #: 为什么需要：日记要写"今天我是怎么过的"，只有一个当前 `doing`
    #: 根本不够 —— 实测过，素材只有"在沙发上看书"时，模型自己编出了
    #: 煮面、泡茶、楼下小孩、整理柜子。它不是在撒谎，是**没材料**。
    #: 把一天里 doing 的变化记下来，它就有真实的锚点可写。
    day_trace: List[Dict[str, Any]] = field(default_factory=list)
    #: 今天身上有点什么。空串 = 没什么特别的感觉。
    #:
    #: 为什么不是一个"健康系统"：人每天都有一点小毛病 —— 眼睛干、腰酸、
    #: 头沉。它们不构成病，但**它们决定了今天能干什么、说话是什么语气**。
    #: 以前这一层完全没有：`energy` 只是个数字，累到 6 分她照样回消息、
    #: 照样干活。那不是身体，是音量旋钮。
    body: str = ""
    #: 这个感觉从什么时候开始的（epoch 秒）
    body_since: float = 0.0
    #: 这个感觉什么时候自己好（epoch 秒）。0 = 一直带到睡着
    body_until: float = 0.0
    #: 这一觉做梦了没有。空串 = 没梦 / 不记得。
    #:
    #: 梦是"内部世界"最直接的证据：它没有外部输入，全是已有材料的重组。
    #: 而且**醒来只记得一点** —— 这不完美恰恰是它的可信之处。
    dream: str = ""
    #: 还记得这个梦到什么时候（epoch 秒）。过了就想不起来了。
    dream_until: float = 0.0
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
         json.dumps(state.pending, ensure_ascii=False, sort_keys=True),
         # 日程变化也要落盘：生成之后如果指纹不变，save() 会跳过，
         # 内存里有 plan、磁盘上没有，重启就丢了一天。
         json.dumps(state.plan, ensure_ascii=False, sort_keys=True),
         state.plan_day,
         state.narrative_day,
         state.journal_day,
         json.dumps(state.day_trace, ensure_ascii=False, sort_keys=True)],
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
                 "asleep_since", "last_tick", "just_woke_at", "extended_at",
                 "body_since", "body_until", "dream_until"):
        try:
            setattr(state, name, float(getattr(state, name) or 0.0))
        except Exception:
            setattr(state, name, 0.0)
    if not isinstance(state.pending, dict):
        state.pending = {}
    if not isinstance(state.plan, list):
        state.plan = []
    state.plan = [x for x in state.plan if isinstance(x, dict)]
    if not isinstance(state.plan_day, str):
        state.plan_day = ""
    if not isinstance(state.narrative_day, str):
        state.narrative_day = ""
    if not isinstance(state.journal_day, str):
        state.journal_day = ""
    if not isinstance(state.day_trace, list):
        state.day_trace = []
    state.day_trace = [x for x in state.day_trace if isinstance(x, dict)][-12:]
    if not isinstance(state.day, str):
        state.day = ""
    if not isinstance(state.doing, str):
        state.doing = ""
    if not isinstance(state.body, str):
        state.body = ""
    if not isinstance(state.dream, str):
        state.dream = ""
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
    # 新的一天，昨天的轨迹清掉 —— 日记只写今天。
    state.day_trace = []

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
# 身体今天有点什么
# ══════════════════════════════════════════════════════════════════════
#
# 为什么需要这一层：
#
#   在这之前，"身体"在代码里等价于 energy（0–100）+ asleep（布尔）。
#   energy 只影响表达预算（几条、几个字、能不能不接），**不影响她做什么**，
#   也不影响能不能回消息 —— 累到 6 分她照样回、照样干活。
#   那不是身体，是音量旋钮。
#
#   人每天身上都有一点什么：眼睛干、腰酸、头沉、手凉。
#   它们不构成病，但它们决定今天能干什么、说话是什么语气。
#
# 为什么必须有成因：
#
#   随机发一条"头疼"就是抽卡，用户一眼能看出"昨天睡得好好的怎么头疼"。
#   而"昨晚欠了两小时觉 → 头有点沉"是能对上的。所以这里全部由
#   **昨天的痕迹 + 睡眠债 + 季节**推出来，不用裸随机数。
#
# 为什么三成日子是空的：
#
#   天天都难受是病，不是生活。有几天就是神清气爽，什么都不疼。

#: 感觉大概持续多久（小时）
BODY_HOURS = (2.0, 6.0)


def body_now(state: LifeState, now: float) -> str:
    """此刻身上还有没有那个感觉。过期了自己就好了。"""
    text = str(getattr(state, "body", "") or "").strip()
    if not text:
        return ""
    until = float(getattr(state, "body_until", 0) or 0)
    if until and now >= until:
        return ""
    return text


def roll_body(state: LifeState, now: float) -> str:
    """醒来时掷一次"今天身上有点什么"。返回选中的那句（空串 = 没有）。

    ⚠ 必须在 `plan_day()` **之前**调用：`plan_day` 会把 `day_trace` 清空，
      而这里要读的正是**昨天**的痕迹（看了多久屏幕、走了多少路）。
    """
    trace = " ".join(str(x.get("doing") or "")
                     for x in (state.day_trace or []) if isinstance(x, dict))
    try:
        debt = float(state.sleep_debt or 0.0)
    except Exception:
        debt = 0.0
    # ⚠ 每个用途单独一条随机流。不能 `rng.random()` 之后接着 `rng.uniform()`
    #   用同一个 Random —— 同一个种子下连续取值的序列是有偏的，实测
    #   "手一直是凉的"十二天里出现了九天（它排在候选表最后，而 hit 偏大）。
    _day = day_key(now)

    # 三成日子身上没什么感觉。人也有神清气爽的时候。
    if _seed(_day, "body", "skip").random() < 0.3:
        state.body = ""
        state.body_since = 0.0
        state.body_until = 0.0
        return ""

    cands: List[Tuple[str, int]] = []
    if debt >= 45:
        cands.append(("头有点沉，像没睡透", 3))
    if any(k in trace for k in ("看", "剧", "书", "写", "屏幕")):
        cands.append(("眼睛有点干", 3))
    if any(k in trace for k in ("收拾", "整理", "洗", "搬", "拖")):
        cands.append(("腰有点酸", 2))
    if any(k in trace for k in ("做饭", "煮", "炒", "热")):
        cands.append(("胃有点胀", 2))
    if any(k in trace for k in ("走", "散步", "下楼", "逛")):
        cands.append(("小腿有点酸", 1))
    try:
        if _local(now).month in (11, 12, 1, 2, 3):
            cands.append(("手一直是凉的", 2))
    except Exception:
        pass
    if not cands:
        # 昨天什么都没留下，今天仍然可能有点说不上来的钝
        cands = [("说不上来，就是有点钝", 1)]

    total = sum(w for _t, w in cands)
    hit = _seed(_day, "body", "pick").uniform(0, total)
    acc = 0.0
    chosen = cands[-1][0]
    for text, weight in cands:
        acc += weight
        if hit <= acc:
            chosen = text
            break

    hours = _seed(_day, "body", "hours").uniform(*BODY_HOURS)
    state.body = chosen
    state.body_since = now
    state.body_until = now + hours * 3600
    return chosen


# ══════════════════════════════════════════════════════════════════════
# 梦
# ══════════════════════════════════════════════════════════════════════
#
# 为什么要做这个：
#
#   梦是「内部世界」最直接的证据 —— 它没有任何外部输入，全是已有材料的
#   重组。一个只有输入-输出的系统不会有梦，一个有内部生活的人会有。
#
#   而且**醒来只记得一点**。这种不完美恰恰是它可信的地方：
#   一个能把梦完整复述出来的角色，是在念稿。
#
# 为什么不调模型：
#
#   睡前那一轮已经调过两次模型（复盘 + 日记），半夜再调一次只为做梦，
#   是拿最贵的资源做一个"没人要求它更聪明"的东西。梦的价值在于
#   它**出现了**，不在于它多精彩。所以这里是场景池 + 当天痕迹加权。

#: 醒来还记着梦的窗口（小时）。过了就模糊到说不出来。
DREAM_KEEP_HOURS = 2.0
#: 做完还记得的概率。三成多 —— 大部分梦是记不住的，这是常识。
DREAM_RECALL = 0.35

#: 梦的场景。都是"差一点"的东西：走不到头、够不着、白忙一场。
#: 不做成"美好回忆"或"可怕噩梦"—— 那种梦有戏剧性，日常的梦没有。
DREAM_SCENES = {
    "walk": (
        "在菜市场，怎么走都走不到头，摊位一排接一排",
        "在楼下等人，等到天黑了也没人来",
        "在找一样东西，一直找不到，醒了也想不起要找什么",
    ),
    "screen": (
        "手机一直在响，拿起来又没有通知",
        "屏幕上的字一直在动，一个字都看不清",
    ),
    "cook": (
        "那锅汤还在火上，回去一看火是关着的",
        "煮了东西，揭开锅盖是空的",
    ),
    "wash": (
        "衣服晾出去又下雨了，收回来还是湿的",
        "在水里洗一样东西，怎么冲都还有沫",
    ),
    "none": (
        "在一条不认识的街上走，两边的店都关着",
        "坐在一个很亮的地方，想不起来自己为什么在那",
    ),
}

#: 当天做过什么 → 做什么梦。没有痕迹就是"none"那一档。
_DREAM_BY_TRACE = (
    (("买菜", "超市", "菜市场", "散步", "下楼", "走"), "walk"),
    (("看", "剧", "书", "写", "画"), "screen"),
    (("做饭", "煮", "炒", "热", "吃"), "cook"),
    (("收拾", "洗", "擦", "拖", "整理"), "wash"),
)


def dream_now(state: LifeState, now: float) -> str:
    """此刻还记不记得那个梦。过了窗口就想不起来了。"""
    text = str(getattr(state, "dream", "") or "").strip()
    if not text:
        return ""
    until = float(getattr(state, "dream_until", 0) or 0)
    if until and now >= until:
        return ""
    return text


def roll_dream(state: LifeState, now: float) -> str:
    """醒来时掷一次"这一觉做梦了没有、还记得多少"。

    ⚠ 和 roll_body 一样，必须在 `plan_day()` 之前调用 ——
      要读的是**昨天**的痕迹（梦是昨天的事重组出来的）。
    """
    _day = day_key(now)
    # 先清掉上一次的：新的一天，旧的梦早就忘了。
    state.dream = ""
    state.dream_until = 0.0
    if _seed(_day, "dream", "recall").random() >= DREAM_RECALL:
        return ""

    trace = " ".join(str(x.get("doing") or "")
                     for x in (state.day_trace or []) if isinstance(x, dict))
    kind = "none"
    for marks, label in _DREAM_BY_TRACE:
        if any(m in trace for m in marks):
            kind = label
            break
    pool = DREAM_SCENES.get(kind) or DREAM_SCENES["none"]
    scene = _seed(_day, "dream", "pick").choice(list(pool))

    state.dream = scene
    state.dream_until = now + DREAM_KEEP_HOURS * 3600
    return scene


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


#: 醒来之后那段**有顺序的**流程（相对醒来的分钟偏移）。
#:
#: 以前那段时间只是"被占掉"（plan 里不从这儿排事），所以她的早上从
#: "在煮点东西"直接跳到"去趟超市"，中间没有"收拾一下准备出门"。
#: 用户要的"早上起床，然后去做什么"里的"起床"就是这一段。
#:
#: 为什么放 life 不放 plan：这是"她此刻在做什么"的精度问题，不是日程问题。
#: 而且 plan 已经依赖 life（取 _seed / state_path），反过来导会成环。
WAKE_STEPS = (
    (0, 15, "刚起来，还在洗漱"),
    (15, 35, "在弄点吃的"),
    (35, 55, "收拾一下，准备出门"),
)
#: 流程总时长。plan.layout 的 WAKE_BLOCK_MINUTES 必须 ≥ 这个数，
#: 否则会排出"她还在洗漱，日程说她在超市"。
WAKE_ROUTINE_MINUTES = 55


def wake_step_text(state: LifeState, now: float) -> str:
    """醒来后第几分钟、该在干什么。不是那段就返回空串。"""
    try:
        woke = float(getattr(state, "just_woke_at", 0) or 0)
    except (TypeError, ValueError):
        return ""
    if not woke or now < woke:
        return ""
    mins = (now - woke) / 60.0
    if mins > WAKE_ROUTINE_MINUTES:
        return ""
    for lo, hi, text in WAKE_STEPS:
        if lo <= mins < hi:
            return text
    return ""


def _pick_doing(day: str, label: str,
                items: Tuple[Tuple[str, int], ...],
                used: Tuple[str, ...] = ()) -> str:
    """同一 `day + 时段` 永远同一个答案。**但今天做过的今天不再重复。**

    这样「你在干嘛」问三次，三次一样；换了时段才换事。
    `used` 是今天已经出现过的 doing —— 一天里十来个时段，池子就那几件事，
    不去重就会「上一秒整理过衣服、下一秒又去整理衣服」。
    """
    if not items:
        return ""
    used_set = {str(x).strip() for x in (used or ()) if str(x or "").strip()}
    pool = [x for x in items if x[0] not in used_set]
    if not pool:
        # 这个时段能做的事今天全做过了：允许重样，比空着好
        pool = list(items)
    rng = _seed(day, label, "doing")
    total = sum(max(1, int(w)) for _t, w in pool)
    hit = rng.uniform(0, total)
    acc = 0.0
    for text, weight in pool:
        acc += max(1, int(weight))
        if hit <= acc:
            return text
    return pool[-1][0]


def _pick_blank(day: str, label: str) -> str:
    """一个「什么都没在做」的姿势。

    为什么要有这个：原来的 doing 永远非空，所以她 24 小时都在做事 ——
    那不是勤快，是永动机。人的一天有近四成时间是空白：瘫着、等水开、
    刷手机刷到忘了在看什么。一个从不无聊的人，一眼就是程序。
    """
    rng = _seed(day, label, "blank-pick")
    return rng.choice(BLANK_DOINGS)


def _plan_minutes(item: Dict[str, Any]) -> int:
    """日程项的 HH:MM → 一天中的分钟。解析不出来返回 -1。"""
    try:
        hh, mm = str(item.get("at") or "").strip().split(":")
        return int(hh) * 60 + int(mm)
    except (TypeError, ValueError):
        return -1


def _plan_end_minutes(item: Dict[str, Any]) -> int:
    """日程项的结束时刻（一天中的分钟）。没有 end 就用 at + 时长。"""
    try:
        hh, mm = str(item.get("end") or "").strip().split(":")
        return int(hh) * 60 + int(mm)
    except (TypeError, ValueError):
        pass
    at = _plan_minutes(item)
    if at < 0:
        return -1
    try:
        dur = int(item.get("dur") or 30)
    except Exception:
        dur = 30
    return at + max(5, dur)


def expire_plan(state: LifeState, now: float) -> int:
    """区间已经过完的事，标成「过去了」。返回标了几条。

    以前只有 `doing` 跨时段时才被动标记（`refresh_doing` 里那句
    `by="elapsed"`），于是她永远不会"做完"一件事 —— 19:00 那条到
    21:58 还挂在"此刻在做"上。区间落地之后，结束时刻就是结束时刻：
    它自己会过去，不需要谁路过。
    """
    changed = 0
    # 只结算**今天**的 plan。
    #
    # 陈旧 plan（她十几天没醒、或昨夜跨过午夜还没重排）不该被"结算"：
    # 那会把一批她根本没经历过的日程按**历史时刻**标成"过去了"，
    # 而且会落盘 —— 她醒来看到的是"今天的安排全过去了"。
    # ⚠ 判据是**日历日**，不是 state.day。
    #   她凌晨还没醒时 state.day 仍停在前一天，用它做判据等于
    #   "昨天 == 昨天"，昨天的安排照样会被结算成"过去了"，还会落盘。
    if (state.plan and state.plan_day
            and state.plan_day != day_key(now)):
        return 0
    minute = _minutes_of_day(now)
    for item in state.plan or []:
        if not isinstance(item, dict) or item.get("done"):
            continue
        end = _plan_end_minutes(item)
        if end < 0:
            continue
        if minute > end:
            item["done"] = True
            item["done_by"] = "elapsed"
            # 和 mark_plan_done 对齐：那条写 done_at，这条以前不写。
            try:
                item["done_at"] = float(now)
            except Exception:
                pass
            changed += 1
    return changed


def current_plan_item(state: LifeState, now: float) -> str:
    """此刻应该在做的那件"计划里的事"。没有就空串。

    取法：开始时间 ≤ 现在的最后一条。全是未来的话用第一条
    （刚醒来时第一条通常就是"洗漱/吃早饭"，用第一条比空着更像人）。

    ⚠ 先校验 `plan_day`：这份 plan 是不是**今天**排的。
      跨天之后旧 plan 还留在内存里（生成失败时特意保留，好过没有安排），
      但它不能当今天用 —— 否则醒来第一句"我在收拾屋子"说的是昨天的事。
      生成日程要调一次模型（几秒到几十秒），那段窗口以前就是这么错的。
    """
    if (state.plan and state.plan_day
            and state.plan_day != day_key(now)):
        return ""
    entries = []
    for item in state.plan or []:
        if not isinstance(item, dict):
            continue
        m = _plan_minutes(item)
        what = str(item.get("what") or "").strip()
        if m < 0 or not what:
            continue
        entries.append((m, what, item))
    if not entries:
        return ""
    entries.sort(key=lambda x: x[0])
    minute = _minutes_of_day(now)
    # 1. 落在某个区间里 → 就是它。
    #    这是「正在做」唯一正确的判据：start ≤ now < end。
    #    旧逻辑是"取已经开始过的最后一条"，于是 19:00-19:30 的一条会一直
    #    显示到 21:58（2026-11-10 的真实数据就是这样）。
    for m, what, item in entries:
        # 已做完的事不算"正在做" —— 少了这个判断，界面上点一次完成，
        # 下一次 5 秒 tick 又会把这条写进 state.doing，她就说"我在去超市"
        # 而这件事早就被标成做完了。
        if item.get("done"):
            continue
        end = _plan_end_minutes(item)
        if end > m and m <= minute < end:
            return what
    # 2. 现在早于第一条：**只有离得够近**才提前用它。
    #
    # 原来这里无条件 `return entries[0][1]`，理由是"刚醒来第一条通常是
    # 洗漱/吃早饭，用第一条比空着更像人"。但代价是：一条 19:00 的
    # 「把冬天的厚衣服翻出来整理」，17:00 就被当成"手头在做的事"显示，
    # 19:00 又显示一次 —— 用户看到的是一天里做了两遍同一件事
    # （2026-11-09 实锤）。30 分钟以内算"提前准备"，超过就交给静态池。
    # （早先注释写的是 90 分钟，代码是 30；代码是对的，注释改回来了。）
    first = entries[0]
    if first[2].get("done"):
        return ""
    first_m = first[0]
    if first_m > minute and first_m - minute <= 30:
        return first[1]
    return ""


def mark_plan_done(state: LifeState, what: str, *, now: float,
                   by: str = "manual") -> bool:
    """标记某条日程结束了。按原文匹配，不做模糊 —— 宁可标不上，不要标错。

    `by` 决定这条是**怎么**结束的，渲染时要分开写：
      · "manual"  用户/调用方明确说做完了 → 写「（做完了）」
      · "elapsed" 那个时段过去了，她换去做下一件 → 写「（过去了）」

    为什么必须分开：日程是"打算做什么"，不是"做了什么"。按时段过去就
    一律标"做完了"，等于替她编一件她没做完的事 —— 和 D-36（事实要有
    来源）直接冲突。但她确实在那个时段"在"做这件事（day_trace 有记录），
    所以也不能当它没发生。"过去了"是唯一诚实的说法。
    """
    for item in state.plan or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("what") or "").strip() == str(what or "").strip():
            if item.get("done"):
                return False
            item["done"] = True
            item["done_at"] = float(now)
            item["done_by"] = str(by or "manual")[:12]
            return True
    return False


def _clamp_int(value: Any, lo: int, hi: int) -> int:
    """宽松转 int 并夹到区间里。转不了就返回下界。

    为什么不直接 int()：模型会给 "1h"、"45分钟"、"约半小时"。
    在 `set_plan` 里抛出去，调用方会把异常当成"日程生成失败"，
    **整天日程一起丢** —— 一条坏字段不该有这种杀伤力。
    """
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return lo


def _norm_end(value: Any) -> str:
    """日程结束时刻归一成 HH:MM。

    两种写法都认：`"14:30"` 和 `1430`（模型有时给"一天中的分钟"）。
    以前直接 `str(v)[:5]`，`1430` 存成 "1430" → split(":") 失败 →
    静默退回 at+30，区间整天错半小时。
    """
    s = str(value or "").strip()
    if not s:
        return ""
    if ":" in s:
        return s[:5]
    try:
        m = int(s)
    except (TypeError, ValueError):
        return ""
    if not (0 <= m < 24 * 60):
        return ""
    return f"{m // 60:02d}:{m % 60:02d}"


def _clamp_meet(value: Any) -> Dict[str, Any]:
    """日程上「碰见谁」的字段夹一遍。

    以前只判 `isinstance(dict)`，内部完全不夹 —— 实测 `who=['张三']`
    加 500 字 `text` 会原样落库并直接进提示词。
    """
    if not isinstance(value, dict):
        return {}
    who = str(value.get("who") or "").strip()[:20]
    text = str(value.get("text") or "").strip()[:60]
    spot = str(value.get("spot") or "").strip()[:20]
    if not who:
        return {}
    return {"who": who, "text": text, "spot": spot}


def set_plan(state: LifeState, items: List[Dict[str, Any]], *,
             day: str) -> None:
    """换掉今天的日程。字段在这里统一夹一遍，不信模型给的形状。"""
    clean: List[Dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        what = " ".join(str(item.get("what") or "").split())[:40]
        if not what:
            continue
        at = str(item.get("at") or "").strip()
        _m = _plan_minutes({"at": at})
        # 范围也要校验：只判"能不能解析"的话，"99:99" 会算成 6039 落库，
        # 然后永不命中、永不 elapsed —— 一条永久占位的僵尸。
        if not (0 <= _m < 24 * 60):
            continue
        clean.append({
            "at": at,
            # 区间必须保住 —— 以前这里只存 at，于是 plan.layout() 排好的
            # 起止在落库那一刻被抹平，_plan_end_minutes 只能退回 at+30，
            # 一条一小时的"去趟超市"半小时就算过去了。
            "end": _norm_end(item.get("end")),
            "dur": _clamp_int(item.get("dur"), 0, 240),
            "when": str(item.get("when") or "").strip()[:6],
            "what": what,
            "done": bool(item.get("done")),
            "done_by": str(item.get("done_by") or "")[:12],
            "shareable": item.get("shareable") is not False,
            "from": str(item.get("from") or "self")[:12],
            # 碰见的人要跟日程一起存 —— 它是这一天的一部分。
            # 丢了它，日记和复盘就永远不知道她今天见过谁。
            "meet": _clamp_meet(item.get("meet")),
        })
    clean.sort(key=_plan_minutes)
    state.plan = clean[:6]
    state.plan_day = str(day or "")


def plan_for_prompt(state: LifeState, *, now: float,
                    limit: int = 6) -> str:
    """给提示词的一段：今天打算做什么。

    和 `doing` 分开写：doing 回答"你在干嘛"，这里回答"你今天还打算做什么"。
    合在一起的话，模型会把"打算"当成"正在"，说出一句"我在去超市的路上"
    而其实还没到点。
    """
    entries = [x for x in (state.plan or []) if isinstance(x, dict)]
    if not entries:
        return ""
    # 和 current_plan_item 同一条守卫：陈旧 plan 不能当"今天打算做的"渲染。
    # 少了这一条，跨午夜那段窗口会精神分裂 —— doing 说"没在做计划里的事"，
    # 提示词却把昨天的三条写成「## 你今天打算做的」。
    # 判据同上：用日历日。凌晨 00:15 她还没醒时 state.day 是昨天，
    # 用它会渲染出「## 你今天打算做的」+ 三条「过去了」。
    if state.plan_day and state.plan_day != day_key(now):
        return ""
    entries = entries[:limit]
    lines = []
    for item in entries:
        mark = ("（过去了）" if item.get("done_by") == "elapsed"
                else "（做完了）" if item.get("done") else "")
        # 碰见谁跟在后面，用括号 —— 它是这件事的副产物，不是另一件事。
        # 写成两个列表项会被模型当成"两件要做的事"。
        meet = item.get("meet")
        met = ""
        if isinstance(meet, dict):
            who = str(meet.get("who") or "").strip()
            text = str(meet.get("text") or "").strip()
            # who 空但 text 有值时以前整段丢掉 —— 那是"碰见谁"这件事被吞，
            # 不是渲染出错。至少把看到的那一眼说出来。
            if who and text:
                met = f"（碰见{who}，{text}）"
            elif who:
                met = f"（碰见{who}）"
            elif text:
                met = f"（{text}）"
        lines.append(f"- {item.get('at')} {item.get('what')}{mark}{met}")
    if not lines:
        return ""
    return ("## 你今天打算做的\n\n" + "\n".join(lines)
            + "\n\n（这是你自己排的。已经过去又没做的就过去了，"
              "不用解释、不用补做；还没到的别提前说成正在做。）")


def goodnight_due(state: LifeState, now: float, last_touch: float, *,
                  before: float = 20 * 60,
                  quiet: float = 40 * 60) -> bool:
    """该不该道别（"我先睡啦"）。

    纯函数，注入 now / last_touch 便于离线测。三道闸：

      1. 醒着，且落在入睡前 `before` 秒内；
      2. 最近 `quiet` 秒内有过互动（对方说话或她回话）；
      3. 还没说过 —— 由调用方记 `goodnight_day`，这里只看状态。

    为什么要有第 2 条：对空气说"我先睡啦"不是温柔，是诡异。
    她只在你还在的时候道别；你早就不说话了，她默默去睡。
    """
    if state is None:
        return False
    try:
        if bool(getattr(state, "asleep", False)):
            return False
        bedtime = float(getattr(state, "bedtime", 0) or 0)
        if bedtime <= 0:
            return False
        if not (bedtime - float(before) <= now < bedtime):
            return False
        if not last_touch:
            return False
        return (now - float(last_touch)) <= float(quiet)
    except (TypeError, ValueError):
        return False


#: 日记：入睡前多久开始写（秒）。
JOURNAL_BEFORE = 20 * 60
#: 日记：睡后多久还允许补写（秒）。
#:
#: 她可能提前困倒（精力耗尽直接睡），也可能写的时候程序没开。
#: 补写一篇"今天"的日记仍然合理 —— 回忆今天，不是撒谎。
JOURNAL_GRACE = 2 * 3600


def journal_due(state: LifeState, now: float, *,
                before: int = JOURNAL_BEFORE,
                grace: int = JOURNAL_GRACE) -> bool:
    """该不该写今天的日记。

    窗口：`[bedtime - before, bedtime + grace)`。

    为什么跟着 `bedtime` 而不是固定 23:00：她的入睡点每天在抖
    （23:20 ± 42 分钟），熬夜还会往后推。写死的钟点会和她真正的
    作息错位 —— 她 00:02 才睡，日志 23:00 就写完了，等于提前交卷。

    为什么睡后还留 grace：
    · 她可能提前困倒，`asleep` 已经 True；
    · 也可能那个点程序没开。
    这两种情况补写都合理。

    为什么不能无限补：第二天醒来 `plan_day` 会清空 `day_trace`，
    再补昨天的日记只剩空壳，或者靠编。

    ⚠ 这里**故意不看 `asleep`**：睡后 grace 内仍然可写。
      真正决定"今天过完没有"的是 `journal_day`，由调用方判。
    """
    if state is None:
        return False
    try:
        bedtime = float(getattr(state, "bedtime", 0) or 0)
    except (TypeError, ValueError):
        return False
    if bedtime <= 0:
        return False
    return bedtime - float(before) <= now < bedtime + float(grace)


def refresh_doing(state: LifeState, now: float) -> bool:
    """跨时段才换事。返回是否变了。

    种子用 `day_key(now)` 而不是 `state.day`：跨过午夜还醒着时，
    "深夜"这一档也该按新的一天重新取样，否则她会连着两晚做同一件事。

    p1-4：今天有日程时，此刻在做什么**以日程为准**。
    静态池降级成"日程没生成时的兜底" —— 不这样，她的一天永远是
    那几件模板事，第 20 次说"我在收拾屋子"，用户会听出来。
    """
    label, items = _slot_for(now)
    _plan_now = current_plan_item(state, now)
    if state.doing_slot == label and state.doing:
        # 时段没变，但**日程可能已经翻页了**：
        #   · 原来在做某条日程，那条区间过完了 → 必须换（否则 09:50 结束的
        #     "去趟超市"会一直挂到 11:00 下一个时段边界）
        #   · 原来没在做日程，现在进了某条 → 必须换
        # 「跨时段才换」是静态池的精度；日程是按分钟的区间，比它细。
        if _plan_now == state.doing:
            return False
        _was_plan = state.doing in {
            str(x.get("what") or "") for x in (state.plan or [])
            if isinstance(x, dict)}
        if not _plan_now and not _was_plan:
            return False
    # 区间过完的事在这里划掉。以前靠"跨时段时把上一件标记完成"，
    # 那是拿时段当粒度去猜结束时刻 —— 09:20 就把 09:50 的事标成做完了。
    try:
        expire_plan(state, now)
    except Exception:
        pass
    # 跨时段了：上一条"计划里的事"到此结束。
    #
    # 只在她**确实进入过那一条**时才记（`current_plan_item` 是唯一来源）。
    # 静态池里的事（在看书 / 收拾屋子）不属于日程，不该出现在计划列表里，
    # 也就不会被标成"过去了"。
    #
    # 以前这里什么都不做，于是 `done` 永远是 False：
    #   · 渲染里的「（做完了）」三个字永远不会出现
    #   · 「至少一条接昨天」只能接一条"看不出做没做完"的条目，
    #     于是反复提同一件早就翻篇的事
    state.doing_slot = label
    _day = day_key(now)
    # 醒来后的前 55 分钟是**有顺序的流程**，优先于日程和静态池 ——
    # 她 08:58 才醒，09:00 那条"去趟超市"不可能正在做。
    _step = wake_step_text(state, now)
    if _step:
        state.doing = _step
        try:
            trace = [x for x in (state.day_trace or []) if isinstance(x, dict)]
            if not trace or str(trace[-1].get("doing")) != state.doing:
                trace.append({"at": float(now), "doing": state.doing})
            state.day_trace = trace[-12:]
        except Exception:
            pass
        return True
    _planned = current_plan_item(state, now)
    if _planned:
        # 日程里有本条 → 以日程为准（她的一天得有形状，不能全靠池子抽）
        state.doing = _planned
    else:
        # 身上不舒服的时候更容易什么都不做。
        # 这是身体第一次真的影响她**做什么**，而不只是说话的多少 ——
        # 以前 energy 掉到 6 分她也照样干活，那不像人。
        _blank_p = BLANK_PROBABILITY
        try:
            if body_now(state, now):
                _blank_p += 0.15
        except Exception:
            pass
        if _seed(_day, label, "blank").random() < _blank_p:
            state.doing = _pick_blank(_day, label)
        else:
            _used = tuple(str(x.get("doing") or "")
                          for x in (state.day_trace or [])
                          if isinstance(x, dict))
            state.doing = _pick_doing(_day, label, items, _used)
    # 记进今天的轨迹：日记要"今天我是怎么过的"，只有一个当前 doing
    # 不够。同一条事不重复记（跨时段可能抽到同一句）。
    try:
        trace = [x for x in (state.day_trace or []) if isinstance(x, dict)]
        if not trace or str(trace[-1].get("doing")) != state.doing:
            trace.append({"at": float(now), "doing": state.doing})
        state.day_trace = trace[-12:]
    except Exception:
        pass
    return True


# ══════════════════════════════════════════════════════════════════════
# 推进
# ══════════════════════════════════════════════════════════════════════

def tick(state: LifeState, now: Optional[float] = None, *,
         talk_turns: int = 0) -> List[LifeEvent]:
    """推进她的生活。**只改 state，不读不写磁盘。**

    `talk_turns`：上次 tick 到现在她回了几轮。聊天也累，这里计入消耗。

    现在的实现只有两步：

        1. 冷启动（没有 day）→ 猜一次，之后永远以状态为准
        2. `catch_up()` 把 [last_tick, now] 之间该发生的事按边界补算出来

    为什么不再有「时间跳变过大就重新对齐」：
        旧实现在 gap > 26 小时时走一条特判，直接把状态按当前时刻重排。
        那 26 小时以内呢？她会被当成"熬了一整夜"。用户 2026-11-10 遇到的
        正是这个：21:00 关软件、次日 19:32 打开，just_woke_at 落在 19:32，
        "她今天晚上才醒"。catch_up 之后 gap 多长都是同一条路径。

    ⚠ 醒来必须先于跨天（旧注释，仍然成立）：
        否则早上醒来时会先被 plan_day 重排一次，wake_at 变成"今天 07:40"
        （还没到），醒来判定永远不成立 —— 她会一直睡下去。
        catch_up 里 `_next_boundary` 用**旧的** wake_at 当边界，
        所以醒来的判定永远发生在重排之前。
    """
    now = float(now if now is not None else time.time())

    # ── 1. 冷启动 ──────────────────────────────────────────────────
    if not state.day:
        return cold_start(state, now)

    # ── 2. 补算区间 ────────────────────────────────────────────────
    events = catch_up(state, now)

    # 区间过完的事，在这一刻划掉 —— 她会"做完"，不需要谁路过才结算。
    try:
        expire_plan(state, now)
    except Exception:
        pass

    # 聊天消耗是"事件"不是"时间"，补算管不到，单独结算。
    if talk_turns > 0 and not state.asleep:
        state.energy = max(
            ENERGY_FLOOR,
            state.energy - ENERGY_TALK_DRAIN * max(0, int(talk_turns)))

    state.last_tick = now
    return events


# ══════════════════════════════════════════════════════════════════════
# 区间补算：按边界切段
# ══════════════════════════════════════════════════════════════════════
#
# 为什么不是「每次 tick 用 now - last_tick 当一个 delta，判一次事件」：
#
#   那样一段 8 小时的 interval 只判一次。她本该在 23:47 睡、次日 07:40 醒，
#   代码却只问一句"现在到点没"。实测（2026-11-10，用户真实数据）：
#   21:00 关软件、次日 19:32 才打开 —— 状态里 just_woke_at 落在 19:32，
#   一整天从晚上七点半才开始，日程里 09:30 和 14:00 那两条**从未发生过**。
#   更糟的是 wake_energy() 把这段冻结算成"睡了 14 小时"，于是她带着
#   sleep_debt=132 分钟"睡过头醒来"。
#
#   本质：入睡和醒来是**时间的函数**，不是**观测的副作用**。进程开不开，
#   她都该在 23:47 睡。所以区间必须被补算，不能被跳过。
#
#   这也顺带解决了另外两个同源的毛病：
#     · 睡前复盘只有 30 分钟死窗口（narrative.py:345），错过就永久丢失
#       —— 实测 soul.selfNotes 至今 0 条自动条目，而给了 2 小时宽限的
#       日记却写出来了。不是复盘写得差，是它没有补算的机会。
#     · 日程的 done 只能靠 by="elapsed" 被动标记，她永远"做不完"一件事。
#
# 切段判据（每个都是"必须处理的状态转移"，不是采样点）：
#   睡着  → wake_at
#   醒着  → 入睡点（bedtime + 今晚延长）、午睡窗口起点、DOING_SLOTS 时段边缘
# 段内状态不变，按公式一次算完；段末做转移。

#: 一次最多补多少段。每段至少跨一个时段边界（≥2 小时），
#: 512 段能覆盖 42 天；上限的真正作用是防住"边界算错导致 while 不前进"。
#:
#: 为什么不再小一点：128 段只够 10 天，超过之后会走"剩下的按一段算完"的
#: 兜底 —— 那会让 30 天没开软件时，她"醒着"连续过完剩下的 20 天，
#: energy 扣到地板 6，而真实情况是她睡了很多觉。
MAX_CATCH_STEPS = 512

#: 时段边缘（一天中的分钟）。从 DOING_SLOTS 推，不另写一份，免得漂。
_SLOT_EDGES: Tuple[int, ...] = tuple(sorted({
    int(start) * 60 for start, _end, _label, _items in DOING_SLOTS
}))


def _next_slot_edge(t: float) -> float:
    """t 之后最近的时段边缘。"""
    minute = _minutes_of_day(t)
    for edge in _SLOT_EDGES:
        if edge > minute:
            return _at_minutes(t, edge)
    # 已经过了当天最后一个边缘 → 明天第一个
    return _at_minutes(t, _SLOT_EDGES[0] + 24 * 60)


def _next_boundary(state: LifeState, t: float, now: float) -> float:
    """t 之后最近一个必须处理的时刻。永远 > t。"""
    cands: List[float] = [now]
    if state.asleep:
        if state.wake_at > t:
            cands.append(state.wake_at)
    else:
        deadline = state.bedtime + state.extended_minutes * 60
        if deadline > t:
            cands.append(deadline)
        if not state.napped and not state.napping:
            nap_open = _at_minutes(t, NAP_START_MIN)
            if nap_open > t:
                cands.append(nap_open)
    edge = _next_slot_edge(t)
    if edge > t:
        cands.append(edge)
    return min(cands)


def _advance_flat(state: LifeState, t0: float, t1: float) -> List[LifeEvent]:
    """[t0, t1) 这段里状态不变，按公式一次算完。返回跨过的阈值事件。"""
    events: List[LifeEvent] = []
    hours = (t1 - t0) / 3600.0
    if hours <= 0:
        return events
    if state.asleep:
        state.energy = min(100.0, state.energy + ENERGY_SLEEP_GAIN * hours)
        return events
    drain = ENERGY_AWAKE_DRAIN * hours
    # 超过入睡点还醒着的那一段额外扣。
    #
    # ⚠ 基准是**允许她熬到的时刻**（含陪聊延长），不是 bedtime ——
    #   否则"我愿意陪你多聊 96 分钟"会被当成熬夜收两遍钱：
    #   当晚多扣一次 overtime，同时那 96 分钟又被计进睡眠债，次日再扣一次。
    #   和"愿意多撑一会儿"的设定方向正好相反。
    _allowed = state.bedtime + state.extended_minutes * 60
    late_start = max(t0, _allowed)
    if t1 > late_start:
        drain += ENERGY_OVERTIME_DRAIN * ((t1 - late_start) / 3600.0)
    before = state.energy
    state.energy = max(ENERGY_FLOOR, state.energy - drain)
    # 阈值只在跨过那一刻报一次（不然每段都刷一条）
    if before >= DOZING_THRESHOLD > state.energy:
        events.append(LifeEvent("dozing", f"开始困了（精力 {state.energy:.0f}）", t1))
    if before >= SPENT_THRESHOLD > state.energy:
        events.append(LifeEvent("spent", f"快撑不住了（精力 {state.energy:.0f}）", t1))
    return events


def _wake(state: LifeState, t: float, *, roll: bool = True) -> List[LifeEvent]:
    """在 t 醒来。午睡和整夜睡分开处理 —— 午睡不是新的一天。"""
    events: List[LifeEvent] = []
    slept_min = ((t - state.asleep_since) / 60.0) if state.asleep_since else 0.0
    slept_min = max(0.0, slept_min)
    # ⚠ 起床精力要用**旧债**算，所以必须在 plan_day 之前结算
    energy_after = wake_energy(state, t)
    debt_before = state.sleep_debt
    state.asleep = False
    state.asleep_since = 0.0

    if state.napping:
        state.napping = False
        # 小睡按"睡了多少分钟"给，不用 wake_energy ——
        # 那是"睡了一整晚"的公式，套在 20 分钟上会算出"没睡够，-20 分"。
        state.energy = min(100.0, state.energy + slept_min * ENERGY_NAP_PER_MINUTE)
        refresh_doing(state, t)
        events.append(LifeEvent(
            "nap_end", f"午睡醒了（{slept_min:.0f} 分钟，精力 {state.energy:.0f}）", t))
        return events

    # day 用**醒来时刻**推进，不用 now —— 补算时 now 可能在几小时之后。
    # 身体必须在 plan_day 之前掷：plan_day 清 day_trace，而 roll_body
    # 读的正是昨天留下的痕迹（看了多久屏幕、走了多少路）。
    if roll:
        try:
            roll_dream(state, t)
        except Exception:
            pass
        try:
            roll_body(state, t)
        except Exception:
            pass
    plan_day(state, t, waking=True)
    state.energy = energy_after
    state.just_woke_at = t
    refresh_doing(state, t)
    tail = f"，还欠 {state.sleep_debt:.0f} 分钟" if state.sleep_debt >= 1 else ""
    if debt_before >= 1:
        tail = (f"，昨晚欠下 {debt_before:.0f} 分钟，"
                f"今天还了 {(debt_before - state.sleep_debt):.0f}")
    events.append(LifeEvent(
        "woke", f"醒了（睡了 {slept_min / 60:.1f} 小时，精力 {state.energy:.0f}{tail}）", t))
    return events


def _fall_asleep(state: LifeState, t: float) -> List[LifeEvent]:
    """在 t 睡下。醒来时刻按"今晚该睡多久"重算，不沿用旧的 wake_at。"""
    # 晚睡时长按 **bedtime** 算，不按"允许时刻"。
    #
    # 这里和 `_advance_flat` 的 overtime 故意不同：
    #   · overtime 是"熬夜额外消耗"，只能在真正超时才收 —— 用允许时刻当基准，
    #     否则陪聊延长的每一分钟都会被当晚罚一次。
    #   · 睡眠债是"少睡了多少"，延长多少就真的少睡多少 —— 必须按 bedtime 算。
    # 两者混用就会变成三重惩罚（当晚 overtime + 次日精力 + 晚醒）。
    late_min = max(0.0, (t - state.bedtime) / 60.0)
    state.sleep_debt = round(min(DEBT_CAP_MINUTES, state.sleep_debt + late_min), 1)
    state.asleep = True
    state.asleep_since = t
    sleep_minutes = (SLEEP_BASE_MINUTES + _jitter(SLEEP_JITTER, day_key(t), "sleep")
                     + _debt_wake_late(state.sleep_debt))
    sleep_minutes = max(SLEEP_MIN_MINUTES, min(SLEEP_MAX_MINUTES, sleep_minutes))
    state.wake_at = t + sleep_minutes * 60
    return [LifeEvent(
        "asleep", f"睡了（比计划晚 {late_min:.0f} 分钟，债 {state.sleep_debt:.0f}）", t)]


def _on_boundary(state: LifeState, t: float, *,
                 roll: bool = True) -> List[LifeEvent]:
    """到了边界：做该做的那一次状态转移。"""
    events: List[LifeEvent] = []
    eps = 0.5   # 边界是算出来的浮点，给半秒容差
    if state.asleep:
        if t + eps >= state.wake_at:
            events.extend(_wake(state, t, roll=roll))
        return events

    deadline = state.bedtime + state.extended_minutes * 60
    if t + eps >= deadline:
        events.extend(_fall_asleep(state, t))
        return events

    # 午睡：只在**窗口起点**掷一次。
    # 为什么不在窗口里每个边界都判：边界变了判据也变了，
    # 一天会从 13:00/15:00 掷两三次，概率从 18% 涨到 40% 以上。
    if (not state.napped and not state.napping
            and abs(_minutes_of_day(t) - NAP_START_MIN) < 1.0
            and state.energy < 70
            and _seed(state.day, "nap-roll").random() < NAP_PROBABILITY):
        minutes = _seed(state.day, "nap-len").uniform(NAP_MINUTES_MIN, NAP_MINUTES_MAX)
        state.napped = True
        state.napping = True
        state.asleep = True
        state.asleep_since = t
        # 午睡不改日程：醒来时按 napping 分支走，不重排计划。
        state.wake_at = t + minutes * 60
        events.append(LifeEvent("nap_start", f"午睡 {minutes:.0f} 分钟", t))
        return events

    # 时段变了就换手头的事（refresh_doing 自己比对 doing_slot，没变返回 False）
    if refresh_doing(state, t):
        events.append(LifeEvent("doing", f"手头：{state.doing}", t))
    return events


def catch_up(state: LifeState, now: float, *,
             roll: bool = True) -> List[LifeEvent]:
    """把 [last_tick, now] 之间该发生的事补算出来。

    这是"她的生活不依赖进程在跑"的唯一实现点：无论中间隔了 5 秒还是 5 天，
    走的都是同一条路径，只是段数不同。
    """
    events: List[LifeEvent] = []
    t = float(state.last_tick or 0.0)
    if t <= 0 or t > now:
        # 从没 tick 过 / 系统时钟回拨：不补算，直接落到 now
        state.last_tick = now
        return events

    steps = 0
    # 起点已经在入睡点之后：说明上一觉整个被跳过了。
    #
    # 典型场景 —— 用户在 23:50 关软件（asleep 还是 False，bedtime 是 23:47），
    # 今早 6:00 才打开。如果直接判"现在到点了，睡吧"，她就变成早上六点才睡，
    # 当天晚上才醒 —— 2026-11-10 用户遇到的正是这个。
    # 正确做法是把那一觉补在**它该在的位置**：23:47 睡、今早 07:40 醒，
    # 然后从醒来继续推进。
    if not state.asleep:
        _deadline = state.bedtime + state.extended_minutes * 60
        if _deadline > 0 and t > _deadline:
            # 补那一觉。**不用旧的 wake_at 当哨兵** —— 它可能是 0（旧版数据），
            # 拿它当哨兵等于恰好把"没有这一觉"的情况关掉，而那正是要修的场景。
            # wake_at=0 还会让 _wake 去算 1970 年，Windows 上 .timestamp() 直接抛。
            events.extend(_fall_asleep(state, _deadline))
            _target_wake = float(state.wake_at or 0)
            if _target_wake > _deadline and now >= _target_wake:
                events.extend(_wake(state, _target_wake, roll=roll))
                t = _target_wake
            else:
                # 她此刻还在睡：直接落到 now，**不要回拨到 _deadline**。
                # 回拨会让"已经按清醒扣过费的窗口"再加一遍睡眠增益（实测 +23）。
                events.extend(_advance_flat(state, _deadline, now))
                t = now

    while t < now and steps < MAX_CATCH_STEPS:
        steps += 1
        # ① 先处理「t 这一刻本来就该发生的事」。
        #
        # `_next_boundary` 只返回 **> t** 的候选，所以"入睡点已经过了"
        # （deadline <= t）不会被当成边界 —— 她要等下一个时段边界才判睡，
        # 实测 21:25 的入睡点拖到 23:00，而且那 95 分钟按 overtime 又收了一遍。
        # 醒来同理（wake_at <= t 时会晚醒最多 2 小时）。
        # 在这里补一刀，让它先结算。
        events.extend(_on_boundary(state, t, roll=roll))
        edge = _next_boundary(state, t, now)
        if edge <= t:          # 理论上不会发生；真发生了就直接跳到 now
            edge = now
        events.extend(_advance_flat(state, t, edge))
        t = edge
        events.extend(_on_boundary(state, t, roll=roll))

    if t < now:
        # 段数用完了（gap 太长）：不再逐步补算，直接按"很久没运行"对齐。
        #
        # 以前这里只跑 _advance_flat —— 只算精力、不做任何边界转移。
        # 后果是四个字段互相打架：day 停在十几天前、energy 扣到地板 6，
        # 而 asleep / wake_at / just_woke_at 还是老的；而且 last_tick 已经
        # 推进，这套错乱状态**会落盘**，之后要好几次 tick 才爬回来。
        events.append(LifeEvent("resume", "太久没运行，按当前时刻重新对齐", now))
        plan_day(state, now, waking=False)
        state.energy = ENERGY_FRESH
        state.asleep = False
        state.asleep_since = 0.0
        state.just_woke_at = 0.0
        try:
            if asleep_window(state, now):
                state.asleep = True
                state.asleep_since = now
        except Exception:
            pass
        try:
            refresh_doing(state, now)
        except Exception:
            pass
    state.last_tick = now
    return events


# ══════════════════════════════════════════════════════════════════════
# 外部输入
# ══════════════════════════════════════════════════════════════════════

def note_pending(state: LifeState, chat_id: str, text: str, now: float,
                 chat_name: str = "") -> None:
    """她睡着时收到消息：记账，不叫醒。醒来主动补一句。

    ⚠ 只记会话和一句预览，不记全文 —— 全文在微信库里，记两份会漂移。

    `chat_name` 是给对方看的会话名（「老爸」）。醒来补回时要拿它去发 ——
    UIA 认显示名，不认 wxid，缺了它就得现场再查一次会话列表。
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
    #: 醒来补回时要拿它当"对方说的那句话"去查记忆、拼上下文，
    #: 40 字的预览常常刚好处在半句上。留 200 字，够用又不至于把整段聊天存两份。
    item["text"] = (text or "").strip()[:200]
    item["first_at"] = item.get("first_at") or now
    if chat_name:
        item["chat_name"] = str(chat_name)[:60]


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

    # 身上有点什么。**带时长** —— "眼睛有点干"和"眼睛干了三个小时"
    # 是两种状态，后者才会让人想躺一会儿。
    _body = body_now(state, now)
    if _body:
        try:
            _mins = max(0.0, (now - float(state.body_since or now)) / 60.0)
        except Exception:
            _mins = 0.0
        if _mins >= 90:
            lines.append(f"{_body}，已经 {_mins / 60:.0f} 个小时了。")
        else:
            lines.append(f"{_body}。")

    # 梦。只在还记着的时候说，而且**说得不完整** —— 人回忆梦总是
    # "好像梦见了……""记不太清了"。说完整了就不是梦，是念稿。
    _dream = dream_now(state, now)
    if _dream:
        lines.append(f"昨晚好像梦见了：{_dream}。记不太清，就剩这一截。")

    # ⚠ 一定要给**具体的醒来时刻**，不能只说「刚醒没多久」。
    #   2026-11-09 实测：她 09:33 醒，中午被问「几点醒的」，
    #   答「八点多吧」—— 提示词里根本没有这个数，她只能编。
    #   「我几点醒的」属于"她自己的生活"，必须由状态直接给，不能让模型猜。
    if state.just_woke_at:
        try:
            _wake_hm = _local(state.just_woke_at).strftime("%H:%M")
            _mins = max(0.0, (now - float(state.just_woke_at)) / 60.0)
            if _mins < 90:
                lines.append(f"今天 {_wake_hm} 醒的，刚醒没多久。")
            else:
                lines.append(f"今天 {_wake_hm} 醒的。")
        except Exception:
            if (now - state.just_woke_at) < 90 * 60:
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
