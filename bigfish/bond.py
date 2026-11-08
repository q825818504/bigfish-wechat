"""[bigfish] 关系：称呼是怎么变的、有什么没说出来。

═══════════════════════════════════════════════════════════════════════
为什么只有两条
═══════════════════════════════════════════════════════════════════════

最初列了五条：称呼演化、未说出口的话、我们之间的第一次、相处习惯、雷区。
最后砍到两条，每一条砍掉都有具体理由：

    · 我们之间的第一次
      模型看到"第一次"这个词就会记一条，但它分不清"第一次听说"
      和"我们第一次做某事"。假阳性高到会污染整张卡。
      而且日期一错，"纪念日"就变成了嘲讽。

    · 相处习惯
      `memory.habits` 已经在做（说够三次的原话样本）。
      再开一层的结果是两边各记一半，注入时互相矛盾。

    · 雷区
      存"不要提什么"等于把那个内容再存一份，反而扩大泄露面。
      现在靠 visibility + topic 白名单在源头拦，比事后记一张黑名单可靠。

留下的两条正好是关系的两端：

    称呼   关系最外显的痕迹 —— 什么时候从"你"变成"老爸"的
    没说出口的话  关系最有重量的部分 —— 也是模型最容易编的部分

═══════════════════════════════════════════════════════════════════════
铁律：每条都必须带原话
═══════════════════════════════════════════════════════════════════════

`evidence` 字段**不是可选的**，而且写入前要能在本轮素材里找到原文。

这和 `memory._verify_sources` 是同一条规矩（D-36）：
不许"自动补一个来源"。区别只在于事实层校验的是 message id，
这里校验的是原话片段。

为什么必须这么严：关系卡会进每一轮的 prompt。一条编造的
"他以前说过最怕被丢下"，会让她在之后几百轮里都按这个前提说话。
错一条，比没有这张卡糟得多。

═══════════════════════════════════════════════════════════════════════
存哪
═══════════════════════════════════════════════════════════════════════

memory.sqlite3 里的 `bonds` 表 —— 和 facts 同一个库，因为它们的
生命周期一样（都会被纠正、被否决、被追溯来源），
共用一套备份与迁移比另开一个文件省事得多。

为什么不存 JSON：这张表要按 owner 检索、要去重、要留 used_count，
手写 JSON 的并发与去重逻辑最后一定会和 sqlite 版本对不上。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

KIND_ADDRESS = "address"     # 称呼
KIND_UNSAID = "unsaid"       # 未说出口的话
#: p1-6：关系阶段。它不是"一条认识"，是"我们现在处在哪儿"。
#: 存进 bonds 表是为了复用同一套 status/updated_at/revision，
#: 但它不进「印象」的渲染顺序，单独一段。
KIND_STAGE = "stage"
KINDS = (KIND_ADDRESS, KIND_UNSAID, KIND_STAGE)

KIND_LABELS = {
    KIND_ADDRESS: "称呼",
    KIND_UNSAID: "没说出口的",
    KIND_STAGE: "现在的关系",
}

#: 关系事件的种类。和 bonds 分开存（见 bond_events 表）：
#: 事件不需要"原话子串"证据 —— "第一次一起看电影"本来就没有一句原话，
#: 硬套 bonds 的 evidence 约束，只会逼模型编一句来凑。
EVENT_WARM = "warm"          # 靠近：她主动说了平时不说的话、被暖到
EVENT_CONFLICT = "conflict"  # 摩擦：拌嘴、被推开、没说开的别扭
EVENT_PROMISE = "promise"    # 约定：谁答应了谁什么
EVENT_FIRST = "first"        # 第一次：第一次一起做某件事
EVENT_REPAIR = "repair"      # 修复：道歉、和好、把话说开
EVENT_KINDS = (EVENT_WARM, EVENT_CONFLICT, EVENT_PROMISE, EVENT_FIRST,
               EVENT_REPAIR)

EVENT_LABELS = {
    EVENT_WARM: "靠近",
    EVENT_CONFLICT: "摩擦",
    EVENT_PROMISE: "约定",
    EVENT_FIRST: "第一次",
    EVENT_REPAIR: "和好",
}

STATUS_ACTIVE = "active"
STATUS_RETIRED = "retired"
STATUS_REJECTED = "rejected"

#: evidence 最短长度。太短的（"嗯""好"）匹配到哪儿都成立，等于没证据。
MIN_EVIDENCE_CHARS = 4

#: 一条关系卡最多带多少字进 prompt
MAX_RENDER_CHARS = 900


# ══════════════════════════════════════════════════════════════════════
# 表
# ══════════════════════════════════════════════════════════════════════

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bonds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bank        TEXT NOT NULL,
    owner       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    text        TEXT NOT NULL,
    evidence    TEXT NOT NULL,
    source_owner TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active',
    confidence  INTEGER NOT NULL DEFAULT 60,
    used_count  INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL DEFAULT 0,
    updated_at  INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS bonds_uniq
    ON bonds(bank, owner, kind, text);
CREATE INDEX IF NOT EXISTS bonds_lookup
    ON bonds(bank, owner, status);

-- p1-6：关系事件。和 bonds 分开，因为它的证据要求不同 ——
-- "第一次一起看电影"没有一句原话可以子串匹配。
-- evidence 允许为空，但写入方（narrative）尽量从当天的原话里取一段。
CREATE TABLE IF NOT EXISTS bond_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bank        TEXT NOT NULL,
    owner       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    what        TEXT NOT NULL,
    evidence    TEXT NOT NULL DEFAULT '',
    at          INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'active'
);
CREATE UNIQUE INDEX IF NOT EXISTS bond_events_uniq
    ON bond_events(bank, owner, kind, what);
CREATE INDEX IF NOT EXISTS bond_events_lookup
    ON bond_events(bank, owner, at DESC);
"""


def ensure_schema(db: str) -> None:
    """幂等建表。失败不抛 —— 关系卡坏了不该让她失语。"""
    if not db:
        return
    try:
        from bigfish.memory import connect

        conn = connect(db)
        try:
            conn.executescript(SCHEMA_SQL)
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
# 读写
# ══════════════════════════════════════════════════════════════════════

def _now_ms() -> int:
    return int(time.time() * 1000)


def list_bonds(db: str, bank: str, owner: str, *,
               kind: str = "", status: str = STATUS_ACTIVE,
               limit: int = 60) -> List[Dict[str, Any]]:
    if not db:
        return []
    try:
        from bigfish.memory import connect

        conn = connect(db, read_only=True)
    except Exception:
        try:
            from bigfish.memory import connect as _c

            conn = _c(db)
        except Exception:
            return []
    try:
        sql = "SELECT * FROM bonds WHERE bank=? AND owner=?"
        args: List[Any] = [bank, owner]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY kind, updated_at DESC LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def upsert_bond(db: str, bank: str, owner: str, *, kind: str, text: str,
                evidence: str, source_owner: str = "",
                confidence: int = 60) -> Tuple[bool, str]:
    """写一条。同 (bank, owner, kind, text) 已存在就只更新时间和置信度。

    返回 (是否写入, 原因)。**evidence 不过关一律拒绝** —— 这是这张表
    唯一的硬约束。
    """
    kind = str(kind or "").strip()
    text = " ".join(str(text or "").split())
    evidence = " ".join(str(evidence or "").split())

    if kind not in KINDS:
        return False, f"unknown kind: {kind}"
    if not text or len(text) > 200:
        return False, "text missing or too long"
    if len(evidence) < MIN_EVIDENCE_CHARS:
        return False, "evidence too short"
    if len(evidence) > 500:
        evidence = evidence[:500]
    if not db:
        return False, "no db"

    ensure_schema(db)
    now = _now_ms()
    conf = max(0, min(100, int(confidence)))
    try:
        from bigfish.memory import connect

        conn = connect(db)
    except Exception as exc:
        return False, f"db unavailable: {exc}"
    try:
        row = conn.execute(
            "SELECT id FROM bonds WHERE bank=? AND owner=? AND kind=? AND text=?",
            (bank, owner, kind, text)).fetchone()
        if row:
            conn.execute(
                "UPDATE bonds SET evidence=?, confidence=?, updated_at=?, "
                "status=? WHERE id=?",
                (evidence, conf, now, STATUS_ACTIVE, row["id"]))
            conn.commit()
            return True, "updated"
        conn.execute(
            "INSERT INTO bonds(bank,owner,kind,text,evidence,source_owner,"
            "status,confidence,used_count,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,0,?,?)",
            (bank, owner, kind, text, evidence, source_owner,
             STATUS_ACTIVE, conf, now, now))
        conn.commit()
        return True, "added"
    except Exception as exc:
        return False, str(exc)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def append_event(db: str, bank: str, owner: str, *, kind: str,
                 what: str, evidence: str = "",
                 at: Optional[float] = None) -> Tuple[bool, str]:
    """记一条关系事件（p1-6）。返回 (是否新增, 原因)。

    和 `upsert_bond` 的区别：
    · 不需要原话子串证据 —— 事件往往是"一起做了什么"，没有一句原话；
    · 不去重内容、只按 (kind, what) 去重：同一件事被复述两次不该记两条；
    · 不覆盖，只追加：关系是时间线，不是一张当前状态卡。

    为什么不让模型写"他说他爱我"这种总结：那是**判断**，不是事件。
    这里只收"发生了什么"，判断留给关系卡和印象。
    """
    kind = str(kind or "").strip()
    what = " ".join(str(what or "").split())
    evidence = " ".join(str(evidence or "").split())
    if kind not in EVENT_KINDS:
        return False, f"unknown kind: {kind}"
    if not what or len(what) > 120:
        return False, "what missing or too long"
    if not db:
        return False, "no db"
    ensure_schema(db)
    stamp = int((at if at is not None else time.time()) * 1000)
    try:
        from bigfish.memory import connect

        conn = connect(db)
    except Exception as exc:
        return False, f"db unavailable: {exc}"
    try:
        row = conn.execute(
            "SELECT id FROM bond_events WHERE bank=? AND owner=? AND kind=? AND what=?",
            (bank, owner, kind, what)).fetchone()
        if row:
            return False, "exists"
        conn.execute(
            "INSERT INTO bond_events(bank,owner,kind,what,evidence,at,status) "
            "VALUES(?,?,?,?,?,?,'active')",
            (bank, owner, kind, what, evidence[:500], stamp))
        conn.commit()
        return True, "added"
    except Exception as exc:
        return False, str(exc)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_events(db: str, bank: str, owner: str, *,
                limit: int = 20) -> List[Dict[str, Any]]:
    """最近的关系事件，新的在前。"""
    if not db:
        return []
    try:
        from bigfish.memory import connect

        conn = connect(db, read_only=True)
    except Exception:
        try:
            from bigfish.memory import connect as _c

            conn = _c(db)
        except Exception:
            return []
    try:
        rows = conn.execute(
            "SELECT * FROM bond_events WHERE bank=? AND owner=? AND status='active' "
            "ORDER BY at DESC LIMIT ?", (bank, owner, int(limit))).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def events_for_prompt(db: str, bank: str, owner: str, *,
                      limit: int = 6, max_chars: int = 500) -> str:
    """把关系事件拼成一小段。没有就返回空串。"""
    rows = list_events(db, bank, owner, limit=limit)
    if not rows:
        return ""
    lines: List[str] = []
    for row in rows:
        label = EVENT_LABELS.get(str(row.get("kind")), "事件")
        try:
            when = time.strftime("%m-%d", time.localtime(
                int(row.get("at") or 0) / 1000))
        except Exception:
            when = ""
        line = f"- {when} {row.get('what')}".replace("  ", " ")
        if label == EVENT_PROMISE:
            line += "（说好的）"
        lines.append(line)
    body = "### 你们之间发生过的\n\n" + "\n".join(lines)
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "…"
    return body


def set_stage(db: str, bank: str, owner: str, stage: str,
              why: str = "") -> Tuple[bool, str]:
    """换关系阶段。同一天重复写同一个阶段是幂等的。

    阶段不是"好感度"，是一句描述：刚认识 / 熟了 / 有点依赖 /
    闹过别扭 / 和好了。它只影响称呼和分寸，不影响任何权限。
    """
    stage = " ".join(str(stage or "").split())
    why = " ".join(str(why or "").split())
    if not stage or len(stage) > 40:
        return False, "stage missing or too long"
    if not db:
        return False, "no db"
    ensure_schema(db)
    now = _now_ms()
    try:
        from bigfish.memory import connect

        conn = connect(db)
    except Exception as exc:
        return False, f"db unavailable: {exc}"
    try:
        conn.execute(
            "UPDATE bonds SET status='retired', updated_at=? "
            "WHERE bank=? AND owner=? AND kind=? AND status='active'",
            (now, bank, owner, KIND_STAGE))
        conn.execute(
            "INSERT OR REPLACE INTO bonds(bank,owner,kind,text,evidence,"
            "source_owner,status,confidence,used_count,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'', 'active',70,0,?,?)",
            (bank, owner, KIND_STAGE, stage,
             why or "阶段变化", now, now))
        conn.commit()
        return True, "saved"
    except Exception as exc:
        return False, str(exc)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_stage(db: str, bank: str, owner: str) -> str:
    """当前关系阶段。没有返回空串。"""
    rows = list_bonds(db, bank, owner, kind=KIND_STAGE, limit=1)
    return str(rows[0].get("text") or "") if rows else ""


def set_status(db: str, bond_id: int, status: str) -> bool:
    if status not in (STATUS_ACTIVE, STATUS_RETIRED, STATUS_REJECTED):
        return False
    try:
        from bigfish.memory import connect

        conn = connect(db)
        try:
            cur = conn.execute("UPDATE bonds SET status=?, updated_at=? WHERE id=?",
                               (status, _now_ms(), int(bond_id)))
            conn.commit()
            # 影响 0 行说明这条不存在 —— 别让界面显示「已下线」却什么都没变
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception:
        return False


def update_text(db: str, bond_id: int, text: str) -> Tuple[bool, str]:
    """改一条的措辞。**evidence 不动** —— 那是原话，改了就不是证据了。

    为什么允许改：模型写出来的概括可能不够准，或者语气不对。
    用户比模型清楚"这句话该怎么说"。
    """
    body = " ".join(str(text or "").split())
    if not body:
        return False, "内容不能为空"
    if len(body) > 200:
        return False, "太长了（200 字以内）"
    try:
        from bigfish.memory import connect

        conn = connect(db)
        try:
            cur = conn.execute(
                "UPDATE bonds SET text=?, updated_at=? WHERE id=?",
                (body, _now_ms(), int(bond_id)))
            conn.commit()
            return (cur.rowcount > 0), ("" if cur.rowcount > 0 else "找不到这一条")
        finally:
            conn.close()
    except Exception as exc:
        return False, str(exc)


def delete_bond(db: str, bond_id: int) -> bool:
    try:
        from bigfish.memory import connect

        conn = connect(db)
        try:
            cur = conn.execute("DELETE FROM bonds WHERE id=?", (int(bond_id),))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception:
        return False


def render_for_prompt(db: str, bank: str, owner: str,
                      *, max_chars: int = MAX_RENDER_CHARS) -> str:
    """拼成给模型看的一段。没有内容返回空串（调用方据此整段不注入）。"""
    rows = list_bonds(db, bank, owner, limit=40)
    if not rows:
        return ""

    address = [r for r in rows if r.get("kind") == KIND_ADDRESS]
    unsaid = [r for r in rows if r.get("kind") == KIND_UNSAID]
    stage = [r for r in rows if r.get("kind") == KIND_STAGE]

    # 用三级标题：这一段现在是「## 你印象里的他」下面的子段。
    # 还写二级的话，模型会把它当成与「印象」并列的另一个来源，
    # 于是「我叫他老爸」和「他是个嘴硬心软的人」在它眼里就不是一回事了。
    parts: List[str] = []
    # p1-6：先给"我们现在处在哪儿"，再给细节。
    # 顺序反了的话，模型会拿一条三年前的称呼去解释今天的关系。
    if stage:
        parts.append("### 我们现在\n\n- " + str(stage[0]["text"]))
    if address:
        parts.append("### 我们之间怎么叫\n\n"
                     + "\n".join(f"- {r['text']}" for r in address[:6]))
    events = events_for_prompt(db, bank, owner, limit=5)
    if events:
        parts.append(events)
    if unsaid:
        parts.append("### 当时没说出口的\n\n"
                     + "\n".join(f"- {r['text']}" for r in unsaid[:6]))

    body = "\n\n".join(parts)
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "…"
    return body


# ══════════════════════════════════════════════════════════════════════
# 从模型输出解析
# ══════════════════════════════════════════════════════════════════════

_FENCE = re.compile(r"\A```[A-Za-z0-9_+.-]*[ \t]*\r?\n?(.*?)\r?\n?```[ \t]*\Z", re.S)


def _loads(text: str) -> Any:
    raw = (text or "").strip()
    m = _FENCE.match(raw)
    if m:
        raw = m.group(1).strip()
    return json.loads(raw)


def _in_transcript(evidence: str, transcript: str) -> bool:
    """证据必须真的出现在素材里（去空白后子串匹配）。"""
    if not evidence or not transcript:
        return False
    norm = lambda s: "".join(str(s or "").split())
    return norm(evidence) in norm(transcript)


def parse_bonds(raw: str, transcript: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """解析模型输出。返回 (通过的条目, 被拒原因)。

    拒绝的三种：结构不对 / evidence 太短 / evidence 在素材里找不到。
    第三种是关键 —— 它把"编一条关系"的成本拉高到"必须引用原话"。
    """
    passed: List[Dict[str, Any]] = []
    rejected: List[str] = []
    try:
        data = _loads(raw)
    except Exception:
        return [], ["not json"]

    items = data.get("bonds") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return [], ["no bonds array"]

    for item in items[:12]:
        if not isinstance(item, dict):
            rejected.append("item not object")
            continue
        kind = str(item.get("kind") or "").strip()
        text = " ".join(str(item.get("text") or "").split())
        evidence = " ".join(str(item.get("evidence") or "").split())
        if kind not in KINDS:
            rejected.append(f"bad kind: {kind}")
            continue
        if not text or len(text) > 200:
            rejected.append("bad text")
            continue
        if len(evidence) < MIN_EVIDENCE_CHARS:
            rejected.append(f"evidence too short: {text[:12]}")
            continue
        if not _in_transcript(evidence, transcript):
            rejected.append(f"evidence not in transcript: {text[:12]}")
            continue
        try:
            conf = int(item.get("confidence") or 60)
        except Exception:
            conf = 60
        passed.append({"kind": kind, "text": text, "evidence": evidence,
                       "confidence": max(0, min(100, conf))})
    return passed, rejected


# ══════════════════════════════════════════════════════════════════════
# 提示词
# ══════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """你在整理「她和他之间」的两类东西。只输出 JSON，不要任何解释。

输出格式：
{"bonds":[{"kind":"address","text":"...","evidence":"...","confidence":70}]}

两类（kind 只能取这两个值之一）：

1. address —— **称呼是怎么变的**
   谁先开始用什么称呼、什么时候开始改口、改口的原因。
   写成一句有时间的陈述，例如：
   「他从 10 月开始叫我「鱼」，之前一直叫全名」
   「我最早叫她「你」，现在叫她「老爸」，是从他自称老爸那天开始的」

2. unsaid —— **当时没说出口的话**
   她本来想说但没说的、或者他问了她回避了的。
   必须是**事后能确认确实发生过**的，不能是推测。
   例如：「他问我要不要一起回去，我没答，换了个话题」

硬性要求：
- text ≤ 60 字，一条只说一件事，用第三人称写清楚是谁对谁
- **evidence 必须逐字复制原文里出现过的一句**（至少 4 个字，可以是一小段），
  不许改写、不许概括。找不到原话就不要输出这一条
- 没有可写的就输出 {"bonds":[]}
- 不要输出「我们之间的第一次」「相处习惯」「禁忌话题」—— 这三类不在范围内

判断标准：这条能不能在原文里指到一句话？指不到就是在编，别写。
"""

USER_TEMPLATE = """下面是最近的对话（原文，不要改写）：

{transcript}

请从里面提炼 address / unsaid 两类，每条都要带 evidence 原话。没有就返回空数组。"""


def build_request(transcript: str) -> Tuple[str, str]:
    return SYSTEM_PROMPT, USER_TEMPLATE.format(transcript=transcript[:6000])
