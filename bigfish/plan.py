"""[bigfish] 她的一天：醒来之后给今天排三件事。

═══════════════════════════════════════════════════════════════════════
为什么需要这一步
═══════════════════════════════════════════════════════════════════════

v0.9.7 之前，`life.doing` 是从 `DOING_SLOTS` 里按 `day + 时段`
确定性采样出来的：在看书、在收拾屋子、在等外卖。

它解决了一个问题（"问三次你在干嘛，三次答案一样"），但留下了更大的：

    **她的生活是采样，不是经历。**

今天和昨天没有区别，因为没有事情发生。所以：
· 她的"生活分享"永远只能分享池子里那几件事；
· 用户听不出"今天"和"上周"；
· 她不可能说"我下午要去趟超市，晚点回你"——
  因为她没有"下午"这个概念，只有"手头在做什么"。

日程补的就是这一层：醒来时排 2~3 件事，`doing` 优先从这里取。
这不是"任务系统"，没有完成率、没有提醒、没有打卡 ——
它只是让她的一天有形状。

═══════════════════════════════════════════════════════════════════════
为什么必须有一条接昨天
═══════════════════════════════════════════════════════════════════════

只让模型"随便排三件事"，它会排出三件互不相干的日常，每天都是新的。
那还是采样，只是换成了模型采样。

连续性来自约束：**至少一条必须和昨天有关**——
昨天没做完的、昨天说过的、昨天没回的那句。
所以 `build_request` 里昨天素材排在前面，且输出要求里写死
`from=yesterday` 至少一条。

═══════════════════════════════════════════════════════════════════════
为什么只写"不需要持续注意"的事
═══════════════════════════════════════════════════════════════════════

`doing` 的旧注释里已经写过：不写"在开会""在医院"——
那会引出"那你怎么有空回我"。

日程同理，而且更严重：日程是"打算"，一旦写了"10:00 开会"，
她 10 点还在回你消息，就是在打自己的脸。

所以提示词里明确排除：会议、面试、就医、长途出行、需要安静/专注的事。
允许的是：买菜、收拾、看书、做饭、散步、取快递、洗衣服、发呆。

═══════════════════════════════════════════════════════════════════════
失败时
═══════════════════════════════════════════════════════════════════════

生成失败 → 不写 plan → `doing` 自动退回 DOING_SLOTS。
这条路径以前就在跑，所以"模型挂了"不会让她没有生活，只是回到旧精度。
"""
from __future__ import annotations

import json
import re
from datetime import date as _date, timedelta as _timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: 一天排几件事。三条足够看出"有安排"，又不至于像待办清单。
PLAN_MIN = 2
PLAN_MAX = 3

#: 素材上限。日程不是长文任务，给太多反而会让她挑花眼。
MATERIAL_CHARS = 1600

SYSTEM = """你在帮一个角色排今天要做的事。

只输出一个 JSON 对象，不要 Markdown 代码块、不要解释。

格式：
{"plan":[{"when":"上午","what":"去趟超市","shareable":true,"from":"yesterday"}]}

规则（按重要性）：
1. 排 2~3 条。**when 只能是这六个词之一**：早上、上午、中午、下午、傍晚、晚上。
   （不要写几点几分 —— 具体时刻由程序按她的作息和空档算，你只管大致时段。）
2. **至少一条的 from 必须是 "yesterday"**，内容要真的接上昨天：
   昨天没做完的事、昨天提过的事、昨天没回的那句话。
   其余两条 from 用 "fact"（来自给的材料）或 "self"（她自己想做的）。
3. what 用 8~16 个字，写成一件具体的小事。
   **不要写**：会议、面试、就医、考试、长途出行、需要安静或专注的事。
   可以写：买菜、收拾屋子、洗衣服、看书、做饭、散步、取快递、发呆、晒太阳。
4. shareable：这件事适不适合主动跟对方提。私人的（洗澡、上厕所）写 false。
5. 不要编造材料里没有的重要事件（谁生病、谁来找她、去哪座城市）。
   没有材料就排普通日常。
6. 不要写"等他消息""想他"这类以对方为中心的事 —— 那是心情，不是日程。
7. 三条之间要有先后关系，不要排三件互相没关系的事：
   比如"买菜"和"做饭"可以同一天，"做饭"和"洗衣服"就没关系。
8. 如果有「## 你自己会想到的」，**可以**排一条接着它的事（from 用 "self"）。
   它不需要和他有关 —— 身上难受就去买点药，天晴了就想晒被子，
   昨天碰见的人说的话今天才回过味来。这是这一天里**只属于她**的那部分。"""


# ══════════════════════════════════════════════════════════════════════
# 一天的骨架：什么时段一定被占着
# ══════════════════════════════════════════════════════════════════════
#
# 骨架不由模型决定。人不会 14:00 才吃第一口饭，也不会 23:00 才开始洗漱 ——
# 这些是生理，不是安排。模型只决定"空档里做什么"，空档由这里算出来。

#: 醒来之后这一段被占掉（起床、洗漱、吃点东西）。相对醒来的分钟数，
#: 因为她的起床时刻是浮动的（07:40 ± 35 + 晚醒最多 100 分钟）。
#:
#: ⚠ 必须 ≥ `life.WAKE_ROUTINE_MINUTES`。life 那边把这段时间写成了
#:   有顺序的三步（洗漱 → 弄点吃的 → 收拾出门），而且**优先于日程**；
#:   这里如果更小，就会排出"她还在洗漱、日程说她在超市"。
WAKE_BLOCK_MINUTES = 55

#: 绝对时刻的固定块。都是"人不会挪"的事。
FIXED_BLOCKS = (
    (11, 40, 12, 30),   # 午饭
    (17, 40, 18, 40),   # 晚饭
    (21, 40, 22, 10),   # 洗漱
)

#: 时段 → 一天中的分钟窗口。模型只给这六个词，具体落点在这里算。
WHEN_WINDOWS = {
    "早上": (7 * 60, 9 * 60 + 30),
    "上午": (9 * 60, 11 * 60 + 40),
    "中午": (11 * 60 + 40, 13 * 60),
    "下午": (13 * 60, 17 * 60),
    "傍晚": (17 * 60, 19 * 60),
    "晚上": (19 * 60, 22 * 60),
}
WHEN_ORDER = {"早上": 0, "上午": 1, "中午": 2, "下午": 3, "傍晚": 4, "晚上": 5}

#: 两件事之间留的走动/收尾时间（分钟）。
#:
#: 没有这个，她就会"11:10 刚从超市回来、11:10 已经在做饭"。
#: 人是要走路的。
TRAVEL_MINUTES = 10

#: 一件事大概要多久。按关键词最长匹配，越具体的越优先。
#: 排不下就不排 —— 她的一天不是待办清单，塞不进去就说明今天没空做。
DURATION_RULES = (
    (("买菜", "超市", "菜市场", "逛"), 50),
    (("取快递", "快递"), 25),
    (("做饭", "煮", "炒", "热一热", "热一下", "下面"), 40),
    (("收拾", "整理", "归置", "洗衣服", "晾", "扫地", "拖地", "擦"), 35),
    (("看书", "翻", "写", "读书"), 45),
    (("散步", "下楼", "走一圈", "遛"), 30),
    (("洗澡", "洗头", "洗漱"), 30),
    (("晒太阳", "发呆", "坐着", "躺着"), 30),
)
DEFAULT_DURATION = 30

#: 这件事会"用掉"什么 / "留下"什么。目前只用来补前置 ——
#: 排了"做饭"却没有"买菜"，就在上午补一条"下楼买点菜"。
#:
#: 为什么只做这一条链：真正的因果图会很复杂，而这一条是唯一
#: 任何家庭每天都发生的因果（没有菜做不了饭）。够用，且不会乱。
COOK_MARKS = ("做饭", "做顿", "下厨", "弄点吃", "煮", "炒", "热一热", "热一下", "烧")
SHOP_MARKS = ("买菜", "买点菜", "超市", "菜市场", "买菜")
SHOP_FILLER = "下楼买点菜"


def _clip(text: Any, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s[:limit]


def self_material(*, body: str = "", met: Sequence[str] = (),
                  weather: str = "", trace: Sequence[str] = ()) -> List[str]:
    """她自己会想到的事 —— 材料全部来自**她的处境**，不来自对话。

    这是"只跟她自己有关的动机"的入口。

    为什么需要：日程的素材以前只有 yesterday / fact / self，而 fact 全部
    来自对话 —— 于是她做的每件事最终都能追溯到你。一个人如果所有动机
    都要经过另一个人，那她不是独立的，是被驱动的。

    这里给的是：身上那点不舒服、最近碰见的人、天气、她反复在做的事。
    这些都跟"他说了什么"无关，但她会因此想做点什么。
    """
    out: List[str] = []
    b = str(body or "").strip()
    if b:
        out.append(f"身上{('' if b.startswith('身上') else '：')}{b}"
                   "（不用硬撑，该歇就歇）")
    for line in [str(x).strip() for x in (met or ()) if str(x).strip()][:2]:
        out.append(f"最近碰见：{line}")
    w = str(weather or "")
    if w and any(k in w for k in ("晴", "雨", "雪", "阴", "降温", "冷")):
        out.append(f"天气：{w}")
    # 她这一两天反复在做的那件事 —— 同一条 doing 出现两次以上，
    # 说明她手上有件没弄完的活，不是随手做的。
    seen: Dict[str, int] = {}
    for t in [str(x).strip() for x in (trace or ()) if str(x).strip()]:
        seen[t] = seen.get(t, 0) + 1
    heavy = [k for k, n in seen.items() if n >= 2]
    if heavy:
        out.append(f"这段时间一直在：{heavy[0]}")
    return out


def build_request(*, day: str, weekday: str, yesterday: List[str],
                  facts: List[str], recent: List[str],
                  weather: str = "",
                  self_lines: Sequence[str] = ()) -> Tuple[str, str]:
    """拼一次日程请求。返回 (system, user)。

    `yesterday` 放最前：它对"连续性"的权重最高，模型对开头更敏感。
    素材总长超了就按 yesterday → recent → facts 的顺序保留，
    先砍事实 —— 事实是长期有效的，明天还有；昨天过了就没了。
    """
    parts: List[str] = [f"今天是 {day}，{weekday}。"]

    if weather:
        parts.append(f"天气：{_clip(weather, 120)}")

    y = [x for x in (yesterday or []) if str(x).strip()]
    # 「昨天」必须写明是哪一天。素材来自生活状态，它可能是空的
    # （`yesterday_lines` 会挡掉"不是昨天"的那份），而写死「昨天」两个字时，
    # 模型会把任何素材都当成昨天 —— 包括其实是三天前的那份。
    y_label = _prev_day(day) or "昨天"
    if y:
        parts.append(f"## 昨天（{y_label}）\n"
                     + "\n".join(f"- {_clip(x, 80)}" for x in y[:6]))
    else:
        parts.append(f"## 昨天（{y_label}）\n（没有记录）")

    r = [x for x in (recent or []) if str(x).strip()]
    if r:
        parts.append("## 最近的对话\n" + "\n".join(
            _clip(x, 60) for x in r[-10:]))

    # 她自己会想到的：身上、碰见的人、天气、一直在做的事。
    # 放在对话前面 —— 这一天的底色是她的处境，不是他昨天说了什么。
    sl = [str(x).strip() for x in (self_lines or ()) if str(x).strip()]
    if sl:
        parts.append("## 你自己会想到的\n" + "\n".join(
            f"- {_clip(x, 60)}" for x in sl[:4]))

    f = [x for x in (facts or []) if str(x).strip()]
    if f:
        parts.append("## 你记得的事\n" + "\n".join(
            f"- {_clip(x, 60)}" for x in f[:8]))

    user = "\n\n".join(parts)
    if len(user) > MATERIAL_CHARS:
        # 从后往前砍：先砍事实，再砍对话，昨天保留
        user = user[:MATERIAL_CHARS]
    return SYSTEM, user


_FENCE = re.compile(r"\A```[A-Za-z0-9_+.-]*[ \t]*\r?\n?(.*?)\r?\n?```[ \t]*\Z", re.S)


def _strip_fence(text: str) -> str:
    m = _FENCE.match(text.strip())
    return (m.group(1) if m else text).strip()


def _first_object(text: str) -> str:
    """从一段可能带前言的话里取出第一个 JSON 对象。"""
    s = _strip_fence(text)
    i = s.find("{")
    if i < 0:
        return ""
    depth = 0
    in_str = False
    esc = False
    for j in range(i, len(s)):
        ch = s[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[i:j + 1]
    return ""


def _norm_time(value: Any) -> str:
    """把各种写法收成 HH:MM。收不了返回空串。"""
    s = str(value or "").strip()
    # 分钟允许一位（"9:5"）。模型偶尔会这么写，收成 09:05 比丢掉好 ——
    # 丢掉之后她那天就少一件事，而这不是用户能看见的错误。
    m = re.match(r"\A(\d{1,2})[:：.](\d{1,2})\Z", s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
    else:
        m = re.match(r"\A(\d{1,2})\Z", s)
        if not m:
            return ""
        hh, mm = int(m.group(1)), 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return ""
    return f"{hh:02d}:{mm:02d}"


_WHEN_ALIAS = {
    "早上": "早上", "清晨": "早上", "一早": "早上", "早起": "早上",
    "上午": "上午",
    "中午": "中午", "午饭": "中午", "午间": "中午",
    "下午": "下午", "午后": "下午",
    "傍晚": "傍晚", "黄昏": "傍晚", "晚饭": "傍晚", "天黑": "傍晚",
    "晚上": "晚上", "夜里": "晚上", "今晚": "晚上", "睡前": "晚上",
}


def _norm_when(value: Any) -> str:
    """把各种写法收成六个时段之一。收不了返回空串。"""
    s = str(value or "").strip()
    if not s:
        return ""
    if s in _WHEN_ALIAS:
        return _WHEN_ALIAS[s]
    for key, label in _WHEN_ALIAS.items():
        if key in s:
            return label
    return ""


def parse_plan(raw: str) -> Tuple[List[Dict[str, Any]], str]:
    """把模型输出解析成日程项。返回 (items, error)。

    宽松解析 + 严格校验：模型经常多写一句话、少一个引号。
    宁可少一条，也不要一条时间错乱的日程 —— 那会让她在半夜说"我在买菜"。

    **接受两种写法**：新的 `when`（早上/上午/…）和旧的 `at`（09:30）。
    旧写法仍然认，但只在没有 when 时才用 —— 时刻是模型最不擅长的东西，
    让它猜等于默认她会"09:30 在超市、09:40 在做饭"。
    """
    obj = _first_object(raw or "")
    if not obj:
        return [], "没有 JSON 对象"
    try:
        data = json.loads(obj)
    except Exception as exc:
        return [], f"JSON 解析失败：{exc}"
    if not isinstance(data, dict):
        return [], "顶层不是对象"
    rows = data.get("plan")
    if not isinstance(rows, list):
        return [], "plan 不是数组"

    out: List[Dict[str, Any]] = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        at = _norm_time(row.get("at"))
        when = _norm_when(row.get("when"))
        what = " ".join(str(row.get("what") or "").split())[:40]
        if not what:
            continue
        if not when and not at:
            continue
        if what in seen:
            continue
        seen.add(what)
        src = str(row.get("from") or "self").strip().lower()
        if src not in ("yesterday", "fact", "self"):
            src = "self"
        out.append({
            "at": at,                     # 可能为空：layout 会按 when 算出来
            "when": when,
            "what": what,
            "done": False,
            "shareable": row.get("shareable") is not False,
            "from": src,
        })
        if len(out) >= PLAN_MAX:
            break

    # 让顺序有意义：先按时段，再按模型给的先后（同时段内保持原序）
    out.sort(key=lambda x: WHEN_ORDER.get(x.get("when") or "", 9))

    if len(out) < PLAN_MIN:
        # ⚠ 这只是**偏少**，不是失败。返回的 err 给日志看，调用方
        #   （`world._maybe_plan`）只判 `if not items` —— 以前文案写
        #   「至少要 2 条」，读代码的人会以为少于 2 条会被丢掉，实际不会。
        #   宁可 1 条也不要这天没安排：1 条还能被 `ensure_yesterday` 补，
        #   空列表连补的锚都没有。
        return out, f"只排了 {len(out)} 条（期望 {PLAN_MIN}，照样用）"
    return out, ""


# ══════════════════════════════════════════════════════════════════════
# 把「时段 + 做什么」展开成「一段一段的时间」
# ══════════════════════════════════════════════════════════════════════
#
# 为什么排程不交给模型：
#
#   模型排时刻是瞎猜 —— 它会给出 09:30 去超市、09:40 在做饭，两条重叠；
#   也不会想"来回要一个多小时"。而它一旦排错，她就得在 09:35 同时出现在
#   两个地方，或者 11:10 刚从超市回来、11:10 已经在炒菜。
#
#   所以分工是：**模型只判断"大致什么时段做什么"（它擅长的粗判断），
#   具体落点、时长、间隙全部由代码算**（它不擅长的算术）。
#   这样时间永远自洽，用户问"你现在在干嘛"三次也不会前后矛盾。

def _minute_of(hhmm: str) -> int:
    try:
        hh, mm = str(hhmm).split(":")
        return int(hh) * 60 + int(mm)
    except Exception:
        return -1


def _hhmm(minutes: float) -> str:
    m = int(round(minutes)) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def duration_of(what: str) -> int:
    """这件事大概要多久（分钟）。"""
    text = str(what or "")
    for marks, minutes in DURATION_RULES:
        if any(m in text for m in marks):
            return minutes
    return DEFAULT_DURATION


def _free_slots(start_ts: float, end_ts: float) -> List[List[float]]:
    """[醒来, 该睡] 之间，挖掉固定块和起床段之后剩下的空档。

    返回 [[起, 止], ...]（epoch 秒），按时间升序。
    """
    if end_ts <= start_ts:
        return []
    lo = start_ts + WAKE_BLOCK_MINUTES * 60
    slots: List[List[float]] = []
    for h1, m1, h2, m2 in FIXED_BLOCKS:
        b0 = _anchor(start_ts, h1 * 60 + m1)
        b1 = _anchor(start_ts, h2 * 60 + m2)
        if b1 <= lo:
            continue
        if b0 >= end_ts:
            break
        if b0 > lo:
            slots.append([lo, min(b0, end_ts)])
        lo = max(lo, b1)
    if lo < end_ts:
        slots.append([lo, end_ts])
    return [s for s in slots if s[1] - s[0] >= 20 * 60]


def _anchor(ts: float, minutes: int) -> float:
    """把"一天中的第 N 分钟"落到 ts 所在的那一天。"""
    import datetime as _dt

    base = _dt.datetime.fromtimestamp(ts).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return (base + _dt.timedelta(minutes=float(minutes))).timestamp()


def _place(slots: List[List[float]], lo_limit: float, hi_limit: float,
           need: float) -> Optional[float]:
    """在空档里找一个装得下 need 秒的位置，找到就把它消耗掉。"""
    for i, (a, b) in enumerate(slots):
        lo = max(a, lo_limit)
        hi = min(b, hi_limit)
        if hi - lo < need:
            continue
        start = lo
        rest = start + need + TRAVEL_MINUTES * 60
        if rest < b:
            slots[i] = [rest, b]
        else:
            slots.pop(i)
        return start
    return None


def with_prerequisites(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """补前置：排了「做饭」却没有「买菜」，就在上午补一条。

    没有这一步，她的日程就是三件互不相干的事 —— 买菜和做饭没关系，
    做饭和洗碗没关系。加上它，"因为"才开始存在：**因为**昨天买了菜，
    **所以**今天能做饭。

    只在真的缺的时候补，且只补一条。补的是"下楼买点菜"这种最小动作，
    不是超市大采购 —— 那会反过来把一天塞满。
    """
    if not items:
        return items
    text = " ".join(str(x.get("what") or "") for x in items)
    has_cook = any(m in text for m in COOK_MARKS)
    has_shop = any(m in text for m in SHOP_MARKS)
    if not (has_cook and not has_shop):
        return items
    if any(str(x.get("what") or "") == SHOP_FILLER for x in items):
        return items
    items.insert(0, {
        "at": "",
        "when": "上午",
        "what": SHOP_FILLER,
        "done": False,
        "shareable": True,
        "from": "self",
    })
    return items


def layout(items: List[Dict[str, Any]], *, wake_at: float,
           bedtime: float) -> List[Dict[str, Any]]:
    """把时段展开成带起止的区间。返回新列表（不改入参）。

    `wake_at`：她今天几点醒的。空档从这里开始算，不从 08:00 开始 ——
    她 09:47 才醒，就不该有一条排在 09:00。
    """
    if not items:
        return items
    start_ts = float(wake_at or 0) or (float(bedtime or 0) - 9 * 3600)
    end_ts = float(bedtime or 0)
    if end_ts <= start_ts:
        return items

    slots = _free_slots(start_ts, end_ts)
    out: List[Dict[str, Any]] = []

    for item in items:
        try:
            row = dict(item)
        except Exception:
            continue
        when = str(row.get("when") or "")
        what = str(row.get("what") or "")

        # 模型给过时刻就用它（旧格式），否则按时段算
        fixed = _minute_of(str(row.get("at") or ""))
        dur = duration_of(what) * 60

        if 0 <= fixed:
            at = _anchor(start_ts, fixed)
            placed = _place(slots, at, at + dur, dur)
            if placed is None:
                placed = _place(slots, 0, end_ts, dur)   # 被占了就往后找
        else:
            win = WHEN_WINDOWS.get(when)
            if win:
                lo = _anchor(start_ts, win[0])
                hi = _anchor(start_ts, win[1])
                # 窗口已经过去（她醒得晚）→ 从"现在能排"的地方开始
                lo = max(lo, start_ts + WAKE_BLOCK_MINUTES * 60)
                placed = _place(slots, lo, hi, dur)
                if placed is None:
                    # 偏好窗口塞不下（比如「中午」被午饭块占掉大半）：
                    # **往后放**，不要塞回更早的时段 —— 那会让她 10:00 吃午饭。
                    placed = _place(slots, max(lo, hi), end_ts, dur)
            else:
                placed = None
            if placed is None:
                placed = _place(slots, start_ts, end_ts, dur)

        if placed is None:
            # 排不下就不排。她的一天不是待办清单，塞不进去就是今天没空。
            continue

        # 做这件事要花多少体力（T-4）。每小时 20 点，最少 8、最多 35。
        #
        # 为什么上限是 35 而不是 25：25 会让 45 分钟以上的事全部封顶，
        # 于是"下楼买点菜"和"收拾一上午屋子"一样贵 —— 那就失去区分度了。
        # 35 大约等于"精力 100 时能连做三件正事"。
        #
        # 为什么放在 layout 而不是让模型给：模型不知道她的精力曲线，
        # 也给不准数字；它只该判断"做什么"，成本是代码算的（和落点一样）。
        row["cost"] = int(max(8, min(35, round(dur / 60.0 * 20))))

        row["start"] = _hhmm(_minutes_local(placed))
        row["end"] = _hhmm(_minutes_local(placed + dur))
        row["at"] = row["start"]        # 兼容：UI 和旧代码只看 at
        row["dur"] = int(dur // 60)
        out.append(row)

    out.sort(key=lambda x: str(x.get("start") or ""))
    return out


def _minutes_local(ts: float) -> float:
    import datetime as _dt

    d = _dt.datetime.fromtimestamp(ts)
    return d.hour * 60 + d.minute + d.second / 60.0


def ensure_yesterday(items: List[Dict[str, Any]],
                     yesterday: List[str]) -> List[Dict[str, Any]]:
    """保证至少有一条接昨天。

    模型不听话时补一条：拿昨天第一条没做完的/最后一条原话，
    塞到今天最晚的时间点。补的这条 `from=yesterday`，内容必须来自素材，
    所以它仍然满足 D-36（不许凭空造）。
    """
    if not items:
        return items
    if any(str(x.get("from")) == "yesterday" for x in items):
        return items
    seed = ""
    for line in yesterday or []:
        s = str(line or "").strip()
        if s and "（做完了）" not in s and "（过去了）" not in s:
            # "昨天 14:00 把书看完" → "把书看完"
            seed = re.sub(r"\A.*?\d{1,2}[:：]\d{2}\s*", "", s).strip()
            seed = seed.replace("（做完了）", "").replace("（过去了）", "").strip()
            if seed:
                break
    if not seed:
        return items
    # 塞在最后一个时段之后，避免打乱已有顺序
    last = items[-1].get("at") or "20:00"
    try:
        hh, mm = str(last).split(":")
        hh = min(21, int(hh) + 1)
        at = f"{hh:02d}:{mm}"
    except Exception:
        at = "20:00"
    items.append({
        "at": at,
        "what": seed[:40],
        "done": False,
        "shareable": True,
        "from": "yesterday",
    })
    items.sort(key=lambda x: str(x.get("at") or ""))
    return items


def summarize(items: List[Dict[str, Any]]) -> str:
    """日志/面板用的一行。"""
    if not items:
        return "今天还没排"
    return " / ".join(
        f"{x.get('at')} {x.get('what')}" for x in items if isinstance(x, dict))


def _prev_day(day: str) -> str:
    """`2026-11-11` → `2026-11-10`。算不出来返回空串。"""
    try:
        return (_date.fromisoformat(str(day or "").strip())
                - _timedelta(days=1)).isoformat()
    except Exception:
        return ""


def yesterday_lines(state: Any, *, now: float) -> List[str]:
    """从 life 状态里取"昨天留下的东西"。

    两样：
      1. 昨天排了但没做完的事 —— 最自然的"接昨天"素材
      2. 昨天做完了的事 —— 可以作为"今天接着做"的由头（比如"书看完了"）

    只取 plan，不取聊天记录。聊天由调用方另外从 history 里取，
    因为 history 是"对话"，plan 是"生活"，混在一起会让她把
    "他说要体检"当成"我昨天去体检"。

    ⚠ **只认真的昨天**：`plan_day` 必须正好是 `day` 的前一天。

      关三天再打开时，`catch_up` 会把 `day` 推进三次，而 `plan` 还是
      三天前那份（`plan_day` 停在三天前）。以前这里完全不看 `plan_day`，
      于是"接昨天"接的是三天前的安排；而且那些条目早就被 `expire_plan`
      标成"（过去了）"，`ensure_yesterday` 一个 seed 都取不到，
      静默 `return items` —— 「至少一条 from=yesterday」这条规则整条失效，
      只剩模型自觉。

      这条错得不像 bug，像她"记性不好"：日程看着正常，只是一直不接昨天。
      而它是 D-36（事实必须有真来源）在生活层唯一的破口。
    """
    out: List[str] = []
    try:
        entries = [x for x in (getattr(state, "plan", None) or [])
                   if isinstance(x, dict)]
    except Exception:
        return out
    plan_day = str(getattr(state, "plan_day", "") or "")
    today = str(getattr(state, "day", "") or "")
    # 两者都非空才校验；老数据缺 plan_day 时保持旧行为（下一次 set_plan 就有）。
    if plan_day and today and plan_day != _prev_day(today):
        return []
    for item in entries:
        what = str(item.get("what") or "").strip()
        if not what:
            continue
        at = str(item.get("at") or "").strip()
        mark = ("（过去了）" if item.get("done_by") == "elapsed"
                else "（做完了）" if item.get("done") else "")
        out.append(f"{at} {what}{mark}".strip())
    return out
