"""[bigfish] 周叙事：让她有"这周"，而不只是"今天"。

═══════════════════════════════════════════════════════════════════════
为什么需要这一层
═══════════════════════════════════════════════════════════════════════

她的时间已经独立于进程（`life.catch_up`），身体、心情、日程、熟人都有了。
但**跨天的经历没有落账**：

  · `day_trace` 只服务"今天"，`plan_day` 隔天会清空它；
  · `journal` 写得很好，但明写"不进提示词"；
  · 复盘写的是"看法变了什么"，不是"这周发生了什么"；
  · `soul.history` 有数据，但只有 `day_summary` 和 `evolved`。

于是你问"这几天干嘛了"，她只能从今天的 doing 和对话片段拼 —— 拼不出"三天"。

这一层补的就是那个对象：**一周**。

═══════════════════════════════════════════════════════════════════════
为什么不每天写一段
═══════════════════════════════════════════════════════════════════════

每天的流水账会让"回忆"变成"日志检索"。周叙事的意义是**压缩**：
七天的几十条 doing，压成五六句话，剩下的自然被忘掉 ——
人记得的本来就不是每天，是"那阵子"。

麦麦的 `context_compaction` 用的是同一套思路（五段式 + 允许舍弃低价值细节），
这里照它的形状，只是周期从"一次对话"换成"一周"。

═══════════════════════════════════════════════════════════════════════
最难的约束：允许"这周很平常"
═══════════════════════════════════════════════════════════════════════

和复盘同一条：模型最擅长把空的说成有的。素材里只有"在收拾屋子 / 在看书"，
就不许写出"这周我学会了一个人待着"。

所以 `parse` 里空数组合法，`week` 可以只有一句"这周没什么特别的"。

═══════════════════════════════════════════════════════════════════════
写到哪
═══════════════════════════════════════════════════════════════════════

    soul.json 的 history 追加一条 kind="week_summary"
    → `soul.week_line()` 渲染成「（11-04~11-11）这周：…」
    → desktop 的 compose 作为座位 861 注入（在 860 人格之后、862 以前之前）

不新开文件、不新开板块 —— 和"自我演化写进灵魂"是同一条规矩。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

#: 多少天写一次。7 = 一个自然周。
PERIOD_DAYS = 7
#: 至少要几天的轨迹才值得写（太少就变成"两天总结"）。
MIN_TRACE_DAYS = 3
#: 素材上限：太多会让模型写成流水账，太少又没有东西可压。
MAX_TRACE_LINES = 40
MAX_CHARS = 2400

SYSTEM = """你在帮一个角色写「这一周」的简短回顾。她有自己的生活、日程、心情，
还有一个固定和她说话的人。材料是这七天的真实轨迹。

只输出一个 JSON 对象，不要 Markdown、不要解释。格式：

{"week":"这周大概怎么样，不超过 60 字",
 "did":["这周做过的 2-4 件事"],
 "met":["碰见的人或他们的变化，0-2 条"],
 "mood":"这周整体心情，不超过 20 字",
 "unfinished":["没做完的事，0-2 条"],
 "together":["和他之间值得记住的，0-2 条"]}

规则：
1. **只写材料里有的**。没有就空数组 —— 空是合法结果，不是失败。
2. 不写流水账。七天的几十条轨迹要**压成几件记得住的事**，
   同一件事反复出现只写一次。
3. 不评价别人（"他其实是个好人"不要写），只写你看到的那一眼。
4. 不写"我很开心/我很难过"这种情绪标签，`mood` 只写一句处境
   （"这周有点闷"可以，"心情值 62"不行）。
5. `unfinished` 只写材料里确实出现、且没有完成迹象的事。
6. `together` 只写你们之间真实发生过的（约定、别扭、和好），不编。"""


def _day_start(now: float) -> float:
    import datetime as _dt

    d = _dt.datetime.fromtimestamp(float(now))
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def due(life: Any, now: float, *, days: int = PERIOD_DAYS,
        min_trace_days: int = MIN_TRACE_DAYS) -> bool:
    """该不该写这一周。

    两种触发：
      1. 从没写过（`weekly_day` 空）——但要有 ≥3 天轨迹，否则不写；
      2. 距上次 ≥ days 天。
    """
    trace = [x for x in (getattr(life, "history_trace", None) or [])
             if isinstance(x, dict)]
    trace_days = {str(x.get("day") or "") for x in trace}
    trace_days.discard("")
    last = str(getattr(life, "weekly_day", "") or "")

    if not last:
        return len(trace_days) >= min_trace_days

    try:
        import datetime as _dt

        last_ts = _dt.datetime.strptime(last, "%Y-%m-%d").timestamp()
    except (ValueError, TypeError):
        return len(trace_days) >= min_trace_days
    return (float(now) - last_ts) >= days * 86400


def build_materials(life: Any, *, now: float, mood: Any = None,
                    events: Optional[List[Dict[str, Any]]] = None,
                    owner: str = "") -> Dict[str, Any]:
    """把七天的轨迹压成模型能读的几段。

    ⚠ 只读，不调模型、不联网。素材收集失败给空，不让上层崩。
    """
    trace = [x for x in (getattr(life, "history_trace", None) or [])
             if isinstance(x, dict)]

    # 按天分组，保持时间顺序；同一天内同一件事只留一次
    by_day: Dict[str, List[str]] = {}
    for item in trace:
        day = str(item.get("day") or "")
        doing = " ".join(str(item.get("doing") or "").split())
        if not day or not doing:
            continue
        bucket = by_day.setdefault(day, [])
        if doing not in bucket:
            bucket.append(doing)

    lines: List[str] = []
    for day in sorted(by_day):
        items = by_day[day][:6]
        lines.append(f"{day}：" + " / ".join(items))
    lines = lines[-7:]

    mood_line = ""
    try:
        value = getattr(mood, "value", None) if mood is not None else None
        if isinstance(value, (int, float)):
            mood_line = f"{float(value):.0f}/100"
    except Exception:
        mood_line = ""

    ev_lines: List[str] = []
    for ev in (events or [])[:8]:
        if not isinstance(ev, dict):
            continue
        kind = str(ev.get("kind") or "")
        what = " ".join(str(ev.get("what") or "").split())
        if what:
            ev_lines.append(f"{kind}：{what}")

    return {
        "days": lines,
        "mood": mood_line,
        "events": ev_lines,
        "trace_days": len(by_day),
    }


def has_material(materials: Dict[str, Any]) -> Tuple[bool, str]:
    days = materials.get("days") or []
    if len(days) < MIN_TRACE_DAYS:
        return False, f"只有 {len(days)} 天轨迹"
    return True, ""


def build_request(materials: Dict[str, Any]) -> Tuple[str, str]:
    parts: List[str] = []
    parts.append("## 这七天的轨迹\n" + "\n".join(materials.get("days") or []))
    if materials.get("mood"):
        parts.append("## 现在的心情\n" + str(materials["mood"]))
    if materials.get("events"):
        parts.append("## 和他之间\n" + "\n".join(materials["events"]))
    parts.append("请按上面的格式写「这一周」。")
    user = "\n\n".join(parts)
    if len(user) > MAX_CHARS:
        user = user[:MAX_CHARS]
    return SYSTEM, user


def parse(raw: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """解析模型输出。空数组合法，格式错返回 (None, err)。"""
    text = (raw or "").strip()
    if not text:
        return None, "空响应"
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None, "没有 JSON"
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        return None, f"JSON 解析失败：{exc}"
    if not isinstance(data, dict):
        return None, "不是对象"

    def _list(key: str, limit: int) -> List[str]:
        v = data.get(key)
        if not isinstance(v, list):
            return []
        out: List[str] = []
        for x in v:
            s = " ".join(str(x or "").split())[:60]
            if s:
                out.append(s)
            if len(out) >= limit:
                break
        return out

    week = " ".join(str(data.get("week") or "").split())[:60]
    mood = " ".join(str(data.get("mood") or "").split())[:20]
    if not week and not mood and not _list("did", 4):
        return None, "全空"
    return {
        "week": week,
        "did": _list("did", 4),
        "met": _list("met", 2),
        "mood": mood,
        "unfinished": _list("unfinished", 2),
        "together": _list("together", 2),
    }, ""


def apply_result(soul_path: Optional[str], payload: Dict[str, Any], *,
                 now: Optional[float] = None) -> Tuple[bool, str]:
    """写进 soul.history（kind=week_summary）。只追加，不改别的字段。

    `soul_path=None` 时自己解析（与 narrative.apply_result 同一约定）：
    调用方（world）不该关心"灵魂文件在哪"这件事。
    """
    try:
        from bigfish import vault as V

        soul_path = soul_path or V.soul_path()
        if not soul_path:
            return False, "没有灵魂文件路径"
        soul, _exists, err = V.read_soul_file(soul_path)
        if err:
            return False, err
        now = float(now if now is not None else time.time())
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        history = list(soul.get("history") or [])[:200]
        history.append({
            "at": int(now * 1000), "kind": "week_summary",
            "day": day, "week": payload,
        })
        soul["history"] = history[-200:]
        ok, _normalized, werr = V.write_soul_file(soul_path, soul)
        return (True, "") if ok else (False, werr)
    except Exception as exc:
        return False, str(exc)


def mark_done(life: Any, *, day: str) -> None:
    """记下"这周写过了"。写失败不影响叙事本身。"""
    try:
        life.weekly_day = str(day or "")
    except Exception:
        pass
