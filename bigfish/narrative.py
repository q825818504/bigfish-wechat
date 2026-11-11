"""[bigfish] 睡前复盘：她今天怎么想，以及你们之间发生了什么。

═══════════════════════════════════════════════════════════════════════
为什么是"睡前"，不是"每天 0 点"
═══════════════════════════════════════════════════════════════════════

她的"一天"由睡觉推动（见 life.py）。0 点复盘会出现很怪的事：
23:40 她还在跟你聊，0 点突然"复盘今天"——今天还没过完。

所以触发点放在**入睡前 30 分钟**：这一天该发生的都发生了，
她还没睡，刚好有时间想一想。和真人写日记的时间一致。

═══════════════════════════════════════════════════════════════════════
一次调用，两件事
═══════════════════════════════════════════════════════════════════════

P1 里「自我演化」和「关系演化」都要求"每天一次"。分成两次调用，
就是每天多花一倍的钱，而且两边素材几乎一样。所以合并成一次：

    summary          今天大概怎么样（不超过 80 字）
    changed_view     我对你 / 对我自己的看法变了什么（0~2 条）
    relation_events  今天我们之间发生了什么（0~2 条）
    stage            关系阶段变了没（可选）

═══════════════════════════════════════════════════════════════════════
最难的约束：允许她"今天没什么可写"
═══════════════════════════════════════════════════════════════════════

模型最擅长的事是"把空的说成有的"。如果提示词里只写"输出 changed_view"，
它每天都会给出两条——于是她的自我认知一天变两次，
一周之后 `selfNotes` 里全是互相矛盾的"我现在觉得"。

所以这一层的判据不是"有没有输出"，而是**"有没有真实素材支持"**：

· `changed_view` 的 `why` 必须能在给的材料里找到由头（一句话即可）；
· 素材里没有情绪波动、没有冲突、没有新的约定 → 允许全空；
· `parse` 里空数组是合法结果，不是失败。

这和 D-36 是同一条：不许"自动补一个"。区别是这次补的是"她的成长"，
比补一条事实更容易骗过人。

═══════════════════════════════════════════════════════════════════════
写到哪
═══════════════════════════════════════════════════════════════════════

    changed_view  → soul.json 的 selfNotes（带日期前缀）+ history
    relation_events → memory.sqlite3 的 bond_events 表
    stage          → bonds 表 kind='stage'

用户明确要求"不要单独新开一个（板块），融入到灵魂那里"，
所以这里没有"自我"文件、没有"成长"页面。
她能变的只有灵魂里那段「我自己知道的事」。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

#: 睡前多久开始复盘（秒）
BEFORE_BED_SECONDS = 30 * 60

#: 素材上限。复盘不是长文任务。
MATERIAL_CHARS = 2200

#: 一次最多几条。多了会让"成长"变成每天一换的人设。
MAX_CHANGED = 2
MAX_EVENTS = 2

STAGE_WORDS = (
    "刚认识", "有点熟了", "熟了", "很熟", "有点依赖", "亲近",
    "闹过别扭", "和好了", "有点疏远", "冷淡",
)

SYSTEM = """你在帮一个角色做睡前的复盘。她今天和你聊过天，有自己的情绪和日程。

只输出一个 JSON 对象，不要 Markdown 代码块、不要解释。

格式：
{"summary":"...","changed_view":[],"relation_events":[],"stage":null}

字段说明：
- summary：今天大概怎么样，不超过 80 字。写事，不写感想。
- changed_view：**她对某人或对自己的看法变了什么**，0~2 条。每条：
    {"about":"他"或"我","from":"以前怎么看（可空）","to":"现在怎么看","why":"今天的哪件事让她这么想"}
  from/to 各不超过 40 字，why 不超过 40 字。
- relation_events：**今天你们之间发生了什么**，0~2 条。每条：
    {"kind":"warm|conflict|promise|first|repair|unsaid","what":"发生了什么（不超过 30 字）","evidence":"今天的原话片段（可为空）"}
  `unsaid` 只用一种情况：你今天**想说但咽回去**的话。
  判据是材料里看得到你停顿、改口，或者明明有话却只回了个很短的句子。
  没有这种痕迹就别写 —— 编出来的"欲言又止"比直说还假。
  这条只有你自己知道，不会给对方看；它挂在心里，下次同样的话题
  再出现时，它会让你更犹豫。
- stage：关系阶段变了才填，否则 null。只能填下面这几个词之一（单选）：
    {"name":"刚认识|有点熟了|熟了|很熟|有点依赖|亲近|闹过别扭|和好了|有点疏远|冷淡","why":"为什么"}
  ⚠ 以前这里只给了 6 个词，而 `STAGE_WORDS` 有 10 个 ——「很熟」「亲近」
    「有点熟了」「冷淡」属于**能收不能生成**。模型想写"冷淡"时只能退回
    "有点疏远"，那正是她开始机械的地方。

最重要的规则：
1. **没有真实素材就留空**。changed_view 和 relation_events 都可以是 []。
   今天很平常，就是很平常 —— 不要为了填满而编。
2. changed_view 不是"今天发生了什么"，是"我的看法变了"。
   今天他加班 → 这是事件；"我以前觉得他不在乎我，现在发现他只是累" → 这是看法。
3. 不要写"我很开心""我很难过"这种情绪描述，那不是看法。
4. 你生活里有几个固定出现的熟人（材料「今天碰见的人」里会给，比如卖鱼的、
   楼下喂猫的老太太）。可以写他们今天怎么样、你跟他们照面时的反应 ——
   但**不许替他们下判断**（"他其实是个好人"不要写），
   也**不许新增材料里没有的人**。他们只是你过日子时碰到的人，不是朋友。
5. evidence 只从给的材料里抄，不要改写、不要编。"""


def _day_start(now: float) -> float:
    import datetime as _dt

    d = _dt.datetime.fromtimestamp(float(now))
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def mood_rows_for_day(timeline: Any, *, now: float,
                      limit: int = 8) -> List[str]:
    """只取**今天**的心情记录。

    ⚠ 这是一个真 bug 的修复：`timeline[-6:]` 会把三天前的记录
      当成"今天发生了什么"。实测过一次 —— 模型看到 11-05 23:45 的
      「表情包爹味」争执，写出一句「和爸聊到深夜」，而当时是上午 11 点。

      timeline 的 `at` 是毫秒。取不到时间的条目一律跳过：
      宁可比实际少一条，也不要让旧事伪装成今天。
    """
    start = _day_start(now)
    out: List[str] = []
    for item in timeline or []:
        if not isinstance(item, dict):
            continue
        try:
            at = float(item.get("at") or 0) / 1000.0
        except (TypeError, ValueError):
            continue
        if at < start:
            continue
        text = (item.get("reason") or item.get("text")
                or item.get("label") or "")
        text = " ".join(str(text).split())
        if text:
            out.append(text)
    return out[-limit:]


def has_material(materials: Dict[str, Any]) -> Tuple[bool, str]:
    """今天的素材够不够写一次复盘。

    为什么需要这道闸：模型最擅长"把空的说成有的"。实测过一次 ——
    对话 0 条、事实 0 条，只靠 6 条旧心情，它照样写出一段
    「和爸聊到深夜」的总结，`changed_view` 也填得满满当当。

    判据（满足任一）：
      · 今天有不重复的对话 ≥ 3 条（排除"吃了吗"刷屏这种测试内容）
      · 有事实
      · 有关系事件

    心情记录**单独不算**：它是模型上一轮写的二手摘要，
    拿它再生成二手，会漂两次。
    """
    talk = [str(t).strip() for t in (materials.get("talk") or [])
            if str(t).strip()]
    # "我: 吃了吗" 重复十条只算一条 —— 测试消息不该被当成"有内容的一天"
    distinct = {t.split(":", 1)[-1].strip() for t in talk}
    facts = [str(t).strip() for t in (materials.get("facts") or [])
             if str(t).strip()]
    events = (materials.get("relation") or {}).get("events") or []
    if len(distinct) >= 3:
        return True, ""
    if len(facts) >= 1 or len(events) >= 1:
        return True, ""
    return False, (f"不重复对话 {len(distinct)} 条 / 事实 {len(facts)} 条 / "
                   f"事件 {len(events)} 条，都不够")


def build_request(*, today: str, summary_materials: Dict[str, Any]) -> Tuple[str, str]:
    """拼复盘请求。素材按"权重从高到低"排，超长先砍低权重的。

    顺序：今天的事 → 心情变化 → 关系卡现状 → 主动消息的回执。
    "心情变化"比"今天聊了什么"权重高：看法变化几乎都由情绪波动触发。
    """
    parts: List[str] = [f"今天是 {today}。"]

    plan = summary_materials.get("plan") or []
    if plan:
        parts.append("## 今天本来打算做的\n" + "\n".join(
            "- " + str(x)[:80] for x in plan[:6]))

    mood = summary_materials.get("mood") or []
    if mood:
        parts.append("## 今天的心情\n" + "\n".join(
            "- " + str(x)[:80] for x in mood[:6]))

    facts = summary_materials.get("facts") or []
    if facts:
        parts.append("## 今天说到的事\n" + "\n".join(
            "- " + str(x)[:80] for x in facts[:8]))

    talk = summary_materials.get("talk") or []
    if talk:
        parts.append("## 今天的对话片段\n" + "\n".join(
            str(x)[:60] for x in talk[:10]))

    relation = summary_materials.get("relation") or {}
    if relation:
        lines = []
        if relation.get("stage"):
            lines.append(f"- 现在的阶段：{relation['stage']}")
        for ev in (relation.get("events") or [])[:5]:
            lines.append(f"- 之前发生过：{str(ev)[:60]}")
        if lines:
            parts.append("## 你们现在\n" + "\n".join(lines))

    proactive = summary_materials.get("proactive") or []
    if proactive:
        parts.append("## 今天她主动说的话和结果\n" + "\n".join(
            "- " + str(x)[:80] for x in proactive[:6]))

    user = "\n\n".join(parts)
    if len(user) > MATERIAL_CHARS:
        user = user[:MATERIAL_CHARS]
    return SYSTEM, user


_FENCE = re.compile(r"\A```[A-Za-z0-9_+.-]*[ \t]*\r?\n?(.*?)\r?\n?```[ \t]*\Z", re.S)


def _first_object(text: str) -> str:
    s = (text or "").strip()
    m = _FENCE.match(s)
    if m:
        s = m.group(1).strip()
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


def _clip(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def parse(raw: str) -> Tuple[Dict[str, Any], str]:
    """解析复盘输出。空数组是合法结果，不是失败。"""
    obj = _first_object(raw or "")
    if not obj:
        return {}, "没有 JSON 对象"
    try:
        data = json.loads(obj)
    except Exception as exc:
        return {}, f"JSON 解析失败：{exc}"
    if not isinstance(data, dict):
        return {}, "顶层不是对象"

    out: Dict[str, Any] = {
        "summary": _clip(data.get("summary"), 120),
        "changed_view": [],
        "relation_events": [],
        "stage": None,
    }

    rows = data.get("changed_view")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            to = _clip(row.get("to"), 60)
            if not to:
                continue
            about = _clip(row.get("about"), 8) or "我"
            if about not in ("他", "我", "我们", "你"):
                about = "我"
            out["changed_view"].append({
                "about": "他" if about == "你" else about,
                "from": _clip(row.get("from"), 40),
                "to": to,
                "why": _clip(row.get("why"), 40),
            })
            if len(out["changed_view"]) >= MAX_CHANGED:
                break

    rows = data.get("relation_events")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            kind = _clip(row.get("kind"), 12).lower()
            if kind not in ("warm", "conflict", "promise", "first",
                            "repair", "unsaid"):
                continue
            what = _clip(row.get("what"), 40)
            if not what:
                continue
            out["relation_events"].append({
                "kind": kind,
                "what": what,
                "evidence": _clip(row.get("evidence"), 80),
            })
            if len(out["relation_events"]) >= MAX_EVENTS:
                break

    stage = data.get("stage")
    if isinstance(stage, dict):
        name = _clip(stage.get("name"), 12)
        if name in STAGE_WORDS:
            out["stage"] = {"name": name, "why": _clip(stage.get("why"), 60)}

    return out, ""


def due(life_state: Any, now: Optional[float] = None) -> bool:
    """现在该不该做睡前复盘。

    窗口是 `bedtime - 30 分钟` 到 `bedtime`，且今天还没跑过。
    睡着之后不做 —— 那时候她已经"不在"了，复盘会变成第二天的。
    """
    if life_state is None:
        return False
    t = float(now if now is not None else time.time())
    try:
        if bool(getattr(life_state, "asleep", False)):
            return False
        day = str(getattr(life_state, "day", "") or "")
        if not day:
            return False
        if str(getattr(life_state, "narrative_day", "") or "") == day:
            return False
        bedtime = float(getattr(life_state, "bedtime", 0) or 0)
        if bedtime <= 0:
            return False
        return bedtime - BEFORE_BED_SECONDS <= t < bedtime
    except Exception:
        return False


def apply_result(result: Dict[str, Any], *, soul_path: Optional[str],
                 db: str = "", bank: str = "", owner: str = "",
                 now: Optional[float] = None) -> Dict[str, Any]:
    """把复盘结果写进灵魂和关系卡。返回一份回执给日志用。

    任何一块失败都不影响另一块 —— 灵魂写不进去，关系事件照样记。
    """
    from bigfish import bond as B
    from bigfish import vault as V

    t = float(now if now is not None else time.time())
    receipt: Dict[str, Any] = {
        "notes": 0, "events": 0, "stage": "", "errors": [],
    }

    changed = result.get("changed_view") or []
    if changed:
        ok, n, err = V.evolve_soul(changed, result.get("summary") or "",
                                   path=soul_path)
        if ok:
            receipt["notes"] = n
        elif err:
            receipt["errors"].append(f"soul: {err}")

    if db:
        for ev in result.get("relation_events") or []:
            ok, why = B.append_event(
                db, bank, owner, kind=ev.get("kind"),
                what=ev.get("what"), evidence=ev.get("evidence") or "",
                at=t)
            if ok:
                receipt["events"] += 1
            elif why not in ("exists",):
                receipt["errors"].append(f"event: {why}")

        stage = result.get("stage")
        if isinstance(stage, dict) and stage.get("name"):
            ok, why = B.set_stage(db, bank, owner, stage["name"],
                                  stage.get("why") or "")
            if ok:
                receipt["stage"] = stage["name"]
            else:
                receipt["errors"].append(f"stage: {why}")

    return receipt


def mark_done(life_state: Any, *, day: str) -> None:
    """记下今天跑过了。失败也不抛 —— 大不了多跑一次。"""
    try:
        life_state.narrative_day = str(day or "")
    except Exception:
        pass


def summarize_receipt(receipt: Dict[str, Any]) -> str:
    """日志用的一行。"""
    bits = []
    if receipt.get("notes"):
        bits.append(f"自述 +{receipt['notes']}")
    if receipt.get("events"):
        bits.append(f"事件 +{receipt['events']}")
    if receipt.get("stage"):
        bits.append(f"阶段→{receipt['stage']}")
    if not bits:
        bits.append("今天没什么可写的")
    if receipt.get("errors"):
        bits.append("；".join(receipt["errors"][:2]))
    return "，".join(bits)
