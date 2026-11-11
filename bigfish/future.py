"""[bigfish] 期待账本：让"下周三"有地方放。

═══════════════════════════════════════════════════════════════════════
为什么需要这一层
═══════════════════════════════════════════════════════════════════════

她会说"下周三体检"，但**第二天不会记得这件事在周三**：

  · `bond_events` 里有 `promise` 这个 kind，但只存"发生了什么"，
    没有"什么时候到期"；
  · `proactive.candidate_recall` 抓的是"他提过、1.5–10 天没结果"，
    它问的是"后来怎么样了"，不是"明天就是了"；
  · 于是"以后"在她的系统里没有载体 —— 只有今天。

这一层只做三件事：把日期短语解析成时间戳、查到期的约定、兑现后标记。

═══════════════════════════════════════════════════════════════════════
为什么不自己编日期
═══════════════════════════════════════════════════════════════════════

`parse_due` 只认**明确写在原话里**的日期短语（明天 / 下周三 / 11月20日）。
解析不出来就返回 0（不是有期限的事）——宁可少记一条期待，
不要让她凭空"记得"一个谁都没说过的日子（D-36：不许编）。
"""
from __future__ import annotations

import datetime as _dt
import re
import time
from typing import Any, Dict, List, Optional

#: 提前多久开始提醒（秒）。一天：周二晚上提醒"明天体检"。
AHEAD_SECONDS = 24 * 3600
#: 过期多久还算"可以问一句"（秒）。半天：当天下午还能问"去了吗"。
GRACE_SECONDS = 12 * 3600

_WEEK_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5,
            "日": 6, "天": 6, "1": 0, "2": 1, "3": 2, "4": 3, "5": 4,
            "6": 5, "7": 6}

#: 相对日。按长度降序匹配（"大后天" 必须先于 "后天"）。
_REL_DAYS = (
    ("大后天", 3), ("后天", 2), ("明天", 1), ("今天", 0), ("今晚", 0),
)


def _day_start(ts: float) -> _dt.datetime:
    d = _dt.datetime.fromtimestamp(float(ts))
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def parse_due(text: str, now: Optional[float] = None) -> float:
    """从一句原话里解析出"什么时候"。解析不出返回 0.0。

    只认这些形状（都是人在聊天里真会说的）：
        明天 / 后天 / 大后天 / 今天
        下周三 / 下星期日 / 周三 / 星期三
        11月20日 / 11-20 / 2026-11-20

    不认："过几天""月底""有空的时候" —— 没有确定日期就不是期待，
    是愿望。愿望由 `candidate_recall` 管，不走这一层。
    """
    s = str(text or "").strip()
    if not s:
        return 0.0
    now = float(now if now is not None else time.time())
    base = _day_start(now)

    # 1) 绝对日期：2026-11-20 / 11月20日 / 11-20
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", s)
    if m:
        try:
            d = _dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d.timestamp()
        except ValueError:
            return 0.0
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", s)
    if m:
        try:
            d = base.replace(month=int(m.group(1)), day=int(m.group(2)))
            # 已经过去就顺延到明年（"1月3日"在 11 月说，指的是明年）
            if d.timestamp() < base.timestamp() - 86400:
                d = d.replace(year=d.year + 1)
            return d.timestamp()
        except ValueError:
            return 0.0
    # ⚠ 不能用 `\b`：中文和数字之间没有词边界，"约在11-20" 会整条匹配失败。
    #   实测（2026-11-11）：`\b(\d{1,2})-(\d{1,2})\b` 对 "约在11-20" 和
    #   "12-03号" 都返回 0 —— 而这正是人在聊天里最常见的写法。
    #   改成"前后不是数字"：既避开中文边界问题，也不会把 2026-11-20 切出 "26-11"。
    m = re.search(r"(?<!\d)(\d{1,2})-(\d{1,2})(?!\d)", s)
    if m:
        try:
            d = base.replace(month=int(m.group(1)), day=int(m.group(2)))
            if d.timestamp() < base.timestamp() - 86400:
                d = d.replace(year=d.year + 1)
            return d.timestamp()
        except ValueError:
            return 0.0

    # 2) 相对日
    for word, delta in _REL_DAYS:
        if word in s:
            return (base + _dt.timedelta(days=delta)).timestamp()

    # 3) 周几：下周三 / 周三 / 星期三
    m = re.search(r"(下下|下|这|本)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天1-7])", s)
    if m:
        prefix, wd = m.group(1) or "", m.group(2)
        target = _WEEK_CN.get(wd)
        if target is None:
            return 0.0
        today = base.weekday()
        ahead = (target - today) % 7
        if prefix in ("下", "下下"):
            ahead += 7 if ahead > 0 else 7
            if prefix == "下下":
                ahead += 7
        elif prefix in ("这", "本"):
            pass
        else:
            # 只说"周三"：指最近的那个未来（今天就是周三则指今天）
            pass
        return (base + _dt.timedelta(days=ahead)).timestamp()

    return 0.0


def upcoming(db: str, bank: str, owner: str, *,
             now: Optional[float] = None,
             ahead: float = AHEAD_SECONDS,
             grace: float = GRACE_SECONDS) -> List[Dict[str, Any]]:
    """到期/临近的约定。按到期时间升序（最近的在最前）。"""
    if not db:
        return []
    now = float(now if now is not None else time.time())
    lo = (now - grace) * 1000
    hi = (now + ahead) * 1000
    try:
        from bigfish.memory import connect

        conn = connect(db, read_only=True)
    except Exception:
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM bond_events "
            "WHERE bank=? AND owner=? AND status='active' AND due_at>0 "
            "AND due_at BETWEEN ? AND ? ORDER BY due_at ASC LIMIT 8",
            (bank, owner, int(lo), int(hi))).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def resolve(db: str, event_id: int, *, status: str = "resolved") -> bool:
    """兑现/放弃一条约定。**不删行** —— 时间线要留着。"""
    if not db or not event_id:
        return False
    try:
        from bigfish.memory import connect

        conn = connect(db)
    except Exception:
        return False
    try:
        conn.execute(
            "UPDATE bond_events SET status=? WHERE id=?", (status, int(event_id)))
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def due_line(event: Dict[str, Any], *, now: Optional[float] = None) -> str:
    """把一条到期约定渲染成一句人话（给主动开口用）。"""
    now = float(now if now is not None else time.time())
    what = " ".join(str(event.get("what") or "").split())[:40]
    try:
        due = float(event.get("due_at") or 0) / 1000.0
    except (TypeError, ValueError):
        due = 0.0
    if not what:
        return ""
    if not due:
        return what
    days = (due - _day_start(now).timestamp()) / 86400.0
    if days < 0.5:
        when = "今天"
    elif days < 1.5:
        when = "明天"
    elif days < 2.5:
        when = "后天"
    else:
        when = _dt.datetime.fromtimestamp(due).strftime("%m-%d")
    return f"{when}：{what}"
