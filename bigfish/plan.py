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
from typing import Any, Dict, List, Optional, Tuple

#: 一天排几件事。三条足够看出"有安排"，又不至于像待办清单。
PLAN_MIN = 2
PLAN_MAX = 3

#: 素材上限。日程不是长文任务，给太多反而会让她挑花眼。
MATERIAL_CHARS = 1600

SYSTEM = """你在帮一个角色排今天要做的事。

只输出一个 JSON 对象，不要 Markdown 代码块、不要解释。

格式：
{"plan":[{"at":"09:30","what":"去趟超市","shareable":true,"from":"yesterday"}]}

规则（按重要性）：
1. 排 2~3 条，时间在 08:00 到 22:00 之间，按时间升序。
2. **至少一条的 from 必须是 "yesterday"**，内容要真的接上昨天：
   昨天没做完的事、昨天提过的事、昨天没回的那句话。
   其余两条 from 用 "fact"（来自给的材料）或 "self"（她自己想做的）。
3. what 用 8~16 个字，写成一件具体的小事。
   **不要写**：会议、面试、就医、考试、长途出行、需要安静或专注的事。
   可以写：买菜、收拾屋子、洗衣服、看书、做饭、散步、取快递、发呆、晒太阳。
4. shareable：这件事适不适合主动跟对方提。私人的（洗澡、上厕所）写 false。
5. 不要编造材料里没有的重要事件（谁生病、谁来找她、去哪座城市）。
   没有材料就排普通日常。
6. 不要写"等他消息""想他"这类以对方为中心的事 —— 那是心情，不是日程。"""


def _clip(text: Any, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s[:limit]


def build_request(*, day: str, weekday: str, yesterday: List[str],
                  facts: List[str], recent: List[str],
                  weather: str = "") -> Tuple[str, str]:
    """拼一次日程请求。返回 (system, user)。

    `yesterday` 放最前：它对"连续性"的权重最高，模型对开头更敏感。
    素材总长超了就按 yesterday → recent → facts 的顺序保留，
    先砍事实 —— 事实是长期有效的，明天还有；昨天过了就没了。
    """
    parts: List[str] = [f"今天是 {day}，{weekday}。"]

    if weather:
        parts.append(f"天气：{_clip(weather, 120)}")

    y = [x for x in (yesterday or []) if str(x).strip()]
    if y:
        parts.append("## 昨天\n" + "\n".join(f"- {_clip(x, 80)}" for x in y[:6]))
    else:
        parts.append("## 昨天\n（没有记录）")

    r = [x for x in (recent or []) if str(x).strip()]
    if r:
        parts.append("## 最近的对话\n" + "\n".join(
            _clip(x, 60) for x in r[-10:]))

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


def parse_plan(raw: str) -> Tuple[List[Dict[str, Any]], str]:
    """把模型输出解析成日程项。返回 (items, error)。

    宽松解析 + 严格校验：模型经常多写一句话、少一个引号。
    宁可少一条，也不要一条时间错乱的日程 —— 那会让她在半夜说"我在买菜"。
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
        what = " ".join(str(row.get("what") or "").split())[:40]
        if not at or not what:
            continue
        if what in seen:
            continue
        seen.add(what)
        src = str(row.get("from") or "self").strip().lower()
        if src not in ("yesterday", "fact", "self"):
            src = "self"
        out.append({
            "at": at,
            "what": what,
            "done": False,
            "shareable": row.get("shareable") is not False,
            "from": src,
        })
        if len(out) >= PLAN_MAX:
            break

    if len(out) < PLAN_MIN:
        return out, f"只排了 {len(out)} 条（至少要 {PLAN_MIN}）"
    return out, ""


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
        if s and "（做完了）" not in s:
            # "昨天 14:00 把书看完" → "把书看完"
            seed = re.sub(r"\A.*?\d{1,2}[:：]\d{2}\s*", "", s).strip()
            seed = seed.replace("（做完了）", "").strip()
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


def yesterday_lines(state: Any, *, now: float) -> List[str]:
    """从 life 状态里取"昨天留下的东西"。

    两样：
      1. 昨天排了但没做完的事 —— 最自然的"接昨天"素材
      2. 昨天做完了的事 —— 可以作为"今天接着做"的由头（比如"书看完了"）

    只取 plan，不取聊天记录。聊天由调用方另外从 history 里取，
    因为 history 是"对话"，plan 是"生活"，混在一起会让她把
    "他说要体检"当成"我昨天去体检"。
    """
    out: List[str] = []
    try:
        entries = [x for x in (getattr(state, "plan", None) or [])
                   if isinstance(x, dict)]
    except Exception:
        return out
    for item in entries:
        what = str(item.get("what") or "").strip()
        if not what:
            continue
        at = str(item.get("at") or "").strip()
        mark = "（做完了）" if item.get("done") else ""
        out.append(f"{at} {what}{mark}".strip())
    return out
