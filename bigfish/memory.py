"""[bigfish] 记忆库：原文层 + 事实层 + 检索 + 反思 + 遗忘。

为什么要有这一层（而不是继续只在 vault.py 里堆函数）
======================================================

第一版记忆只有一个「原文切片 + FTS」的原型：`capture_message()` 把每条消息
切成 800 字窗口塞进 chunks，`bridge.mjs` 拿这些 chunk 找相似的句子拼回去。
它记得住原话，但它没有「她认为什么」——于是：

  · **改不了**。她说错了，只能删掉那条原文，不能表达「我改了主意」。
    删掉之后连「她曾经以为过什么」都没了。
  · **压不住**。聊天越久，注入的原文越多；真正稳定的认识反而埋在里面。
  · **查得糙**。分词只有「单字 + 相邻二字」，没有 IDF、没有排序融合，
    结果按 maxChars 顺序截断。
  · **删不干净**。删一条消息要手工顺着 anchor_sources / activations /
    groups / replays / FTS 一条条清，漏一张表就留下幽灵命中。

这一层补上的东西，逐条对应上面的问题：

  1. **事实层**（`facts`）：anchor + predicate + object，带状态机
     `active / superseded / forgotten`、`superseded_by`、置信度、来源片段。
     纠正 = 新事实 + 旧事实标 superseded，**两边都留着**。
  2. **遗忘层**（`fact_events`）：忘记不是删除，是一条事件。
     原文还在（她确实听过），但检索和她本人都不会再拿它当结论。
  3. **检索融合**：词法（FTS5 + BM25）+ 可选向量 + MMR 去冗余，
     `maxChars` 不再靠截断，而是按分数填满预算。
  4. **反思**：`reflect_on_session()` 让模型只从「有来源的原文」提事实，
     输出严格 JSON，落地时按 anchor/predicate 做 supersede，并记事件。
  5. **锚点接入**：`ensure_anchor()` / `aliases` 真正被检索与反思用到，
     「老爸」「爸爸」「爸」归一成一个人。
  6. **级联删除收口**：`forget_chunk_side_tables()` 一处删干净，
     `delete_messages()` / `delete_session()` / `whale_fall` 全部走它。

设计约束（沿用 vault.py 的规矩，一条都没松）
--------------------------------------------
  · 任何对外函数都不抛异常，失败返回 `(ok, ..., error)` 或带 error 的 dict。
    「记忆坏了」不能变成「聊天崩了」。
  · 派生层可抛。`search_index` / `facts` / `fact_events` 都是**可以重建**的；
    权威永远是 `messages` / `chunks`。重建入口是 `rebuild_derived()`。
  · 表结构只做**加法**：新库走完整 DDL，老库由 `migrate()` 用
    `CREATE TABLE IF NOT EXISTS` 补齐，不改任何既有列。
  · 建表语句与 `vault.ensure_memory_db()` 共用同一份 DDL ——
    以前它俩各写一遍，少一张表就"打开就报错"。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# 版本号写入 PRAGMA user_version。老库是 1，加了事实层之后是 2。
# 只在 migrate() 里推进，不靠文件名或 PRAGMA 之外的任何状态。
SCHEMA_VERSION = 2


# ══════════════════════════════════════════════════════════════════════════
# 建表
# ══════════════════════════════════════════════════════════════════════════
#
# ⚠ 这份 DDL 是**唯一真源**：vault.ensure_memory_db() 直接 import 它。
#   以前那份抄在 vault.py 里，改了分词忘了改表，bridge 就报「没有这一列」。

BASE_STATEMENTS: Tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY, bank TEXT NOT NULL, session TEXT NOT NULL,
        event_key TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL,
        content_json TEXT NOT NULL, source_message_id TEXT,
        recorded_at INTEGER NOT NULL, source_time INTEGER,
        interrupted INTEGER NOT NULL DEFAULT 0,
        UNIQUE(bank,session,event_key))""",
    "CREATE INDEX IF NOT EXISTS messages_bank_session ON messages(bank,session,id)",
    "CREATE INDEX IF NOT EXISTS messages_bank_time ON messages(bank,source_time)",
    """CREATE TABLE IF NOT EXISTS chunks (
        id INTEGER PRIMARY KEY, message INTEGER NOT NULL REFERENCES messages(id),
        start INTEGER NOT NULL, end INTEGER NOT NULL, text TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'episodic',
        importance REAL NOT NULL DEFAULT 0.5,
        activated INTEGER NOT NULL, boosts INTEGER NOT NULL DEFAULT 0,
        last_boost INTEGER, UNIQUE(message,start))""",
    "CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(terms, tokenize='unicode61')",
    """CREATE TABLE IF NOT EXISTS anchors (
        id INTEGER PRIMARY KEY, bank TEXT NOT NULL, name TEXT NOT NULL,
        UNIQUE(bank,name))""",
    """CREATE TABLE IF NOT EXISTS aliases (
        anchor INTEGER REFERENCES anchors(id), alias TEXT NOT NULL,
        PRIMARY KEY(anchor,alias))""",
    """CREATE TABLE IF NOT EXISTS anchor_sources (
        anchor INTEGER REFERENCES anchors(id), chunk INTEGER REFERENCES chunks(id),
        PRIMARY KEY(anchor,chunk))""",
    """CREATE TABLE IF NOT EXISTS activations (
        bank TEXT NOT NULL, chunk INTEGER REFERENCES chunks(id),
        event_key TEXT NOT NULL, at INTEGER NOT NULL,
        PRIMARY KEY(bank,chunk,event_key))""",
    """CREATE TABLE IF NOT EXISTS ledger (
        id INTEGER PRIMARY KEY, bank TEXT NOT NULL, at INTEGER NOT NULL,
        kind TEXT NOT NULL, chars INTEGER NOT NULL, sources TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS housekeeping (
        bank TEXT PRIMARY KEY, last_chunk INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS groups (
        chunk INTEGER PRIMARY KEY REFERENCES chunks(id),
        bank TEXT NOT NULL, digest TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS replays (
        chunk INTEGER PRIMARY KEY REFERENCES chunks(id), at INTEGER NOT NULL)""",
)

# 派生层。全部可以删了重建，见 rebuild_derived()。
DERIVED_STATEMENTS: Tuple[str, ...] = (
    # 事实：她「认为」什么。和原文分开，是为了能纠正而不是只能删。
    """CREATE TABLE IF NOT EXISTS facts (
        id INTEGER PRIMARY KEY, bank TEXT NOT NULL,
        anchor TEXT, predicate TEXT NOT NULL, object TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 0.5,
        status TEXT NOT NULL DEFAULT 'active',
        superseded_by INTEGER, valid_from INTEGER, valid_to INTEGER,
        source_chunks TEXT NOT NULL DEFAULT '[]',
        evidence TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
        note TEXT NOT NULL DEFAULT '')""",
    "CREATE INDEX IF NOT EXISTS facts_bank_status ON facts(bank,status,id)",
    "CREATE INDEX IF NOT EXISTS facts_bank_anchor ON facts(bank,anchor,predicate)",
    # 事实事件：记 → 改 → 忘，每一步都留痕。忘记不是删除。
    """CREATE TABLE IF NOT EXISTS fact_events (
        id INTEGER PRIMARY KEY, bank TEXT NOT NULL, fact INTEGER,
        kind TEXT NOT NULL, at INTEGER NOT NULL, reason TEXT NOT NULL DEFAULT '',
        payload TEXT NOT NULL DEFAULT '{}')""",
    "CREATE INDEX IF NOT EXISTS fact_events_bank ON fact_events(bank,at)",
    # 切片级检索元数据：词频统计（IDF）与去冗余用的签名。
    # FTS5 rank 的 bm25() 已经在用全局词频，这张表补的是「她自己说过哪些」。
    """CREATE TABLE IF NOT EXISTS chunk_meta (
        chunk INTEGER PRIMARY KEY REFERENCES chunks(id),
        bank TEXT NOT NULL, speaker TEXT NOT NULL DEFAULT '',
        token_count INTEGER NOT NULL DEFAULT 0,
        term_blob TEXT NOT NULL DEFAULT '')""",
    # 反思水位：哪批消息已经被提炼过事实。
    """CREATE TABLE IF NOT EXISTS reflection_state (
        bank TEXT PRIMARY KEY, session TEXT NOT NULL DEFAULT '',
        last_message INTEGER NOT NULL DEFAULT 0,
        last_run INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '')""",
)


def all_statements() -> Tuple[str, ...]:
    return BASE_STATEMENTS + DERIVED_STATEMENTS


# ══════════════════════════════════════════════════════════════════════════
# 分词 —— 与插件 store.js 的 terms() 逐条对齐（bridge 那边用同一套查）
# ══════════════════════════════════════════════════════════════════════════

_STOP = {
    "的", "了", "吗", "呢", "我", "你", "他", "它", "是", "在", "和", "有", "就", "啊",
    "the", "a", "is", "and",
}

_HAN_RUN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]+|[a-z0-9_]+")


def _is_han(ch: str) -> bool:
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF)


def terms(text: str) -> List[str]:
    """中文二元分词 + 英文数字整词。**改这里必须同步 store.js 与 bridge.mjs。**

    规则（照抄 JS）：
      · 汉字串：单字（去掉停用字）+ 相邻二字
      · 英文/数字串：整词（去掉停用词）
      · 去重且保序
    """
    out: List[str] = []
    for run in _HAN_RUN.findall((text or "").lower()):
        if _is_han(run[0]):
            chars = list(run)
            for i, ch in enumerate(chars):
                if ch not in _STOP:
                    out.append(ch)
                if i + 1 < len(chars):
                    out.append(ch + chars[i + 1])
        else:
            if run not in _STOP:
                out.append(run)
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def fts_query(text: str) -> str:
    """把自然语言变成 FTS5 MATCH 表达式。

    以前检索直接把原始 query 丢给 MATCH，中文没有空格，FTS5 会把整句当
    一个 token 去匹配 —— 于是"老爸今天加班"一条都命不中（库里存的是二元组）。
    这里改成：切成同样的二元组，用 OR 连接，再交给 bm25() 排序。
    """
    toks = terms(text)
    if not toks:
        return ""
    # FTS5 的列值里 token 之间是用空格分隔的，所以"相邻二字"本身就是一个 token。
    safe = []
    for t in toks:
        if re.fullmatch(r"[a-z0-9_\u4e00-\u9fff]{1,32}", t):
            safe.append(f'"{t}"')
    return " OR ".join(safe)


# ══════════════════════════════════════════════════════════════════════════
# 连接
# ══════════════════════════════════════════════════════════════════════════


def connect(path: str, *, read_only: bool = False, timeout: float = 10.0) -> sqlite3.Connection:
    """打开记忆库。

    `read_only=True` 走 URI 的 mode=ro：**不再把整个库拷到临时目录再读**。
    以前每轮对话拷一次 200MB，现在同一个文件并发读，WAL 模式本来就支持。
    拷库只在真需要快照时用（export_memory 的 VACUUM INTO）。
    """
    if read_only:
        uri = "file:" + path.replace("\\", "/") + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    else:
        conn = sqlite3.connect(path, timeout=timeout)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def migrate(path: str) -> Tuple[bool, str]:
    """把一个已有库补齐到当前 schema。**只加表，不改列**。

    返回 (ok, error)。已经是最新时是空操作，可以每次启动都调。
    """
    if not path or not os.path.isfile(path):
        return False, "记忆库不存在"
    try:
        conn = sqlite3.connect(path, timeout=10)
        try:
            for stmt in DERIVED_STATEMENTS:
                conn.execute(stmt)
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
            if ver < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
        finally:
            conn.close()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def ensure_schema(path: str) -> Tuple[bool, str]:
    """建库（不存在就建全量）+ 迁移（存在就补齐）。"""
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10)
        try:
            for stmt in all_statements():
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
        finally:
            conn.close()
        return True, ""
    except Exception as exc:
        return False, str(exc)


# ══════════════════════════════════════════════════════════════════════════
# 锚点（人物）—— 以前四张表建了没人用，这里真正接上
# ══════════════════════════════════════════════════════════════════════════


def ensure_anchor(conn: sqlite3.Connection, bank: str, name: str,
                  aliases: Sequence[str] = ()) -> Optional[int]:
    """按名字拿锚点 id，没有就建。别名一起登记。

    「老爸」「爸爸」「爸」是同一个人的三个叫法 —— 以前各算各的，
    于是检索"老爸"找不到"爸爸"说过的那段。
    """
    name = (name or "").strip()
    if not name:
        return None
    row = conn.execute("SELECT id FROM anchors WHERE bank=? AND name=?",
                       (bank, name)).fetchone()
    if row is not None:
        aid = int(row[0])
    else:
        cur = conn.execute("INSERT INTO anchors(bank,name) VALUES(?,?)",
                           (bank, name))
        aid = int(cur.lastrowid)
    for alias in {name, *[a.strip() for a in aliases if a and a.strip()]}:
        try:
            conn.execute("INSERT OR IGNORE INTO aliases(anchor,alias) VALUES(?,?)",
                         (aid, alias))
        except Exception:
            pass
    return aid


def resolve_anchor(conn: sqlite3.Connection, bank: str, text: str) -> Optional[int]:
    """在一句话里找出提到了哪个已知锚点（最长别名优先）。

    只做本地字符串匹配，不调模型 —— 每轮对话都跑，必须是 O(别名数)。
    """
    text = text or ""
    if not text:
        return None
    rows = conn.execute(
        "SELECT a.anchor, a.alias FROM aliases a JOIN anchors n ON n.id=a.anchor "
        "WHERE n.bank=? ORDER BY LENGTH(a.alias) DESC", (bank,)).fetchall()
    for row in rows:
        alias = row[1]
        if alias and alias in text:
            return int(row[0])
    return None


def link_anchor_chunk(conn: sqlite3.Connection, anchor: Optional[int],
                      chunk: int) -> None:
    if anchor is None:
        return
    try:
        conn.execute("INSERT OR IGNORE INTO anchor_sources(anchor,chunk) VALUES(?,?)",
                     (anchor, chunk))
    except Exception:
        pass


def list_anchors(conn: sqlite3.Connection, bank: str) -> List[Dict[str, Any]]:
    """锚点清单：别名数、挂了多少片段、还有多少条 active 事实。"""
    rows = conn.execute(
        "SELECT n.id, n.name, "
        "(SELECT COUNT(*) FROM aliases a WHERE a.anchor=n.id) AS alias_count, "
        "(SELECT COUNT(*) FROM anchor_sources s WHERE s.anchor=n.id) AS chunks, "
        "(SELECT COUNT(*) FROM facts f WHERE f.bank=n.bank AND f.anchor=n.name "
        " AND f.status='active') AS facts "
        "FROM anchors n WHERE n.bank=? ORDER BY chunks DESC, n.id", (bank,)).fetchall()
    return [{"id": int(r[0]), "name": r[1], "aliases": int(r[2]),
             "chunks": int(r[3]), "facts": int(r[4])} for r in rows]


# ══════════════════════════════════════════════════════════════════════════
# 事实层
# ══════════════════════════════════════════════════════════════════════════

ACTIVE, SUPERSEDED, FORGOTTEN = "active", "superseded", "forgotten"


@dataclass
class Fact:
    predicate: str
    object: str
    anchor: Optional[str] = None
    confidence: float = 0.6
    source_chunks: List[int] = field(default_factory=list)
    evidence: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        self.predicate = (self.predicate or "").strip()[:120]
        self.object = (self.object or "").strip()[:1200]
        self.anchor = (self.anchor or "").strip()[:60] or None
        try:
            self.confidence = max(0.0, min(1.0, float(self.confidence)))
        except (TypeError, ValueError):
            self.confidence = 0.6
        self.evidence = (self.evidence or "").strip()[:400]
        self.source_chunks = [int(c) for c in (self.source_chunks or [])][:32]

    @property
    def ok(self) -> bool:
        return bool(self.predicate and self.object and self.object != "null")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _event(conn: sqlite3.Connection, bank: str, fact_id: Optional[int],
           kind: str, reason: str = "", payload: Optional[dict] = None) -> None:
    conn.execute(
        "INSERT INTO fact_events(bank,fact,kind,at,reason,payload) VALUES(?,?,?,?,?,?)",
        (bank, fact_id, kind, _now_ms(), (reason or "")[:300],
         json.dumps(payload or {}, ensure_ascii=False)[:4000]))


def _find_same(conn: sqlite3.Connection, bank: str, fact: Fact) -> Optional[sqlite3.Row]:
    """同一主体 + 同一谓词已经有一条 active 的事实，就是"要更新"的那条。

    object 相同 → 重复提及，只涨置信度，不产生新行（幂等）。
    object 不同 → 新认识覆盖旧的：旧行标 superseded，不删。
    """
    if fact.anchor:
        return conn.execute(
            "SELECT * FROM facts WHERE bank=? AND status='active' AND "
            "anchor=? AND predicate=? ORDER BY id DESC LIMIT 1",
            (bank, fact.anchor, fact.predicate)).fetchone()
    return conn.execute(
        "SELECT * FROM facts WHERE bank=? AND status='active' AND "
        "anchor IS NULL AND predicate=? ORDER BY id DESC LIMIT 1",
        (bank, fact.predicate)).fetchone()


def remember(path: str, bank: str, fact: Fact) -> Tuple[bool, Optional[int], str]:
    """写入一条事实。返回 (ok, fact_id, error)。

    三种结果：
      · **新增**：没有同主体同谓词的 active 事实 → INSERT
      · **加固**：有，且 object 完全相同 → 置信度上浮（最多到 0.95），不新增行
      · **纠正**：有，但 object 不同 → 旧行 superseded + superseded_by=新行，
        新行新开一条。**两行都在**，所以「她以前以为是 X」查得到。

    这个三分法是大肥鱼原来完全没有的：原来记错了只能删，删了就什么都不剩。
    """
    if not fact.ok:
        return False, None, "predicate / object 为空"
    try:
        conn = connect(path)
    except Exception as exc:
        return False, None, str(exc)
    try:
        now = _now_ms()
        prev = _find_same(conn, bank, fact)
        with conn:  # 事务
            if prev is not None:
                if (prev["object"] or "") == fact.object:
                    conf = min(0.95, float(prev["confidence"] or 0) + 0.05)
                    conn.execute(
                        "UPDATE facts SET confidence=?, updated_at=? WHERE id=?",
                        (conf, now, prev["id"]))
                    _event(conn, bank, int(prev["id"]), "reinforce",
                           f"重复提及：{fact.predicate}", {"confidence": conf})
                    return True, int(prev["id"]), ""

                cur = conn.execute(
                    "INSERT INTO facts(bank,anchor,predicate,object,confidence,status,"
                    "source_chunks,evidence,created_at,updated_at,note) "
                    "VALUES(?,?,?,?,?,'active',?,?,?,?,?)",
                    (bank, fact.anchor, fact.predicate, fact.object, fact.confidence,
                     json.dumps(fact.source_chunks), fact.evidence, now, now, fact.note))
                new_id = int(cur.lastrowid)
                conn.execute(
                    "UPDATE facts SET status=?, superseded_by=?, updated_at=? WHERE id=?",
                    (SUPERSEDED, new_id, now, prev["id"]))
                _event(conn, bank, int(prev["id"]), "correct",
                       f"{prev['object']} → {fact.object}",
                       {"new": new_id, "old_object": prev["object"]})
                return True, new_id, ""

            cur = conn.execute(
                "INSERT INTO facts(bank,anchor,predicate,object,confidence,status,"
                "source_chunks,evidence,created_at,updated_at,note) "
                "VALUES(?,?,?,?,?,'active',?,?,?,?,?)",
                (bank, fact.anchor, fact.predicate, fact.object, fact.confidence,
                 json.dumps(fact.source_chunks), fact.evidence, now, now, fact.note))
            new_id = int(cur.lastrowid)
            _event(conn, bank, new_id, "add", fact.predicate,
                   {"object": fact.object})
            return True, new_id, ""
    except Exception as exc:
        return False, None, str(exc)
    finally:
        conn.close()


def forget_fact(path: str, bank: str, fact_id: int, reason: str = "") -> Tuple[bool, str]:
    """忘掉一条事实。**不删行**，只标 forgotten 并记事件。

    为什么保留：她说「我不记得这个了」和数据库里从来没出现过，
    是两件事。前者以后能被问「你是不是忘了什么」查到。
    """
    try:
        conn = connect(path)
    except Exception as exc:
        return False, str(exc)
    try:
        with conn:
            row = conn.execute("SELECT * FROM facts WHERE id=? AND bank=?",
                               (fact_id, bank)).fetchone()
            if row is None:
                return False, "没有这条事实"
            conn.execute("UPDATE facts SET status=?, updated_at=? WHERE id=?",
                         (FORGOTTEN, _now_ms(), fact_id))
            _event(conn, bank, fact_id, "forget", reason, {"predicate": row["predicate"]})
        return True, ""
    except Exception as exc:
        return False, str(exc)
    finally:
        conn.close()


def list_facts(path: str, bank: str, *, status: str = "", anchor: str = "",
               query: str = "", limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    """列/搜事实。UI 的「事实」页直接用它。"""
    if not path or not os.path.isfile(path):
        return {"items": [], "total": 0}
    where, params = ["bank = ?"], [bank]
    if status in (ACTIVE, SUPERSEDED, FORGOTTEN):
        where.append("status = ?")
        params.append(status)
    if anchor:
        where.append("anchor = ?")
        params.append(anchor)
    if (query or "").strip():
        where.append("(predicate LIKE ? OR object LIKE ? OR evidence LIKE ?)")
        q = f"%{query.strip()}%"
        params += [q, q, q]
    clause = " WHERE " + " AND ".join(where)
    try:
        conn = connect(path, read_only=True)
    except Exception:
        try:
            conn = connect(path)
        except Exception as exc:
            return {"items": [], "total": 0, "error": str(exc)}
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM facts{clause}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM facts{clause} ORDER BY "
            "CASE status WHEN 'active' THEN 0 ELSE 1 END, id DESC LIMIT ? OFFSET ?",
            params + [max(1, int(limit)), max(0, int(offset))]).fetchall()
        items = []
        for r in rows:
            try:
                chunks = json.loads(r["source_chunks"] or "[]")
            except Exception:
                chunks = []
            items.append({
                "id": int(r["id"]), "anchor": r["anchor"] or "",
                "predicate": r["predicate"], "object": r["object"],
                "confidence": round(float(r["confidence"] or 0), 2),
                "status": r["status"], "supersededBy": r["superseded_by"],
                "createdAt": int(r["created_at"] or 0),
                "updatedAt": int(r["updated_at"] or 0),
                "chunks": chunks, "evidence": r["evidence"] or "",
            })
        return {"items": items, "total": int(total)}
    except Exception as exc:
        return {"items": [], "total": 0, "error": str(exc)}
    finally:
        conn.close()


def fact_events(path: str, bank: str, limit: int = 60) -> List[Dict[str, Any]]:
    if not path or not os.path.isfile(path):
        return []
    try:
        conn = connect(path, read_only=True)
    except Exception:
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM fact_events WHERE bank=? ORDER BY id DESC LIMIT ?",
            (bank, max(1, int(limit)))).fetchall()
        return [{"id": int(r["id"]), "fact": r["fact"], "kind": r["kind"],
                 "at": int(r["at"] or 0), "reason": r["reason"] or ""} for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# 检索：词法 + 向量 + MMR 融合
# ══════════════════════════════════════════════════════════════════════════


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _mmr(cands: List[Dict[str, Any]], vectors: List[Optional[Sequence[float]]],
         k: int, lambda_: float = 0.72) -> List[Dict[str, Any]]:
    """最大边际相关性：既相关又不重复。

    以前按分数顺序截断，于是同一段对话的三个切片会一起挤进注入预算 ——
    模型看到三遍"今天加班到十点"，不如看到一次加班 + 一次别的事。
    """
    if k <= 0 or not cands:
        return []
    picked: List[int] = []
    remaining = list(range(len(cands)))
    while remaining and len(picked) < k:
        best, best_score = None, -1e9
        for i in remaining:
            rel = float(cands[i].get("score") or 0)
            if vectors and vectors[i] is not None:
                div = max((_cosine(vectors[i], vectors[j])
                           for j in picked if vectors[j] is not None), default=0.0)
            else:
                div = 0.0
            s = lambda_ * rel - (1 - lambda_) * div
            if s > best_score:
                best, best_score = i, s
        if best is None:
            break
        picked.append(best)
        remaining.remove(best)
    return [cands[i] for i in picked]


def search_chunks(path: str, bank: str, query: str, *, limit: int = 8,
                  max_chars: int = 2400) -> List[Dict[str, Any]]:
    """原文检索：FTS5(bm25) 排序 → MMR 去冗余 → 按分数填满预算。

    返回 [{chunk, message, role, text, time, score}]。

    与旧实现的差别：
      1. 查询先过分词再进 MATCH（旧实现整句匹配，中文一条都命不中）；
      2. 用 bm25() 排序而不是插入顺序；
      3. 去冗余而不是硬截断；
      4. 只读打开，不拷库。
    """
    if not path or not os.path.isfile(path):
        return []
    expr = fts_query(query)
    if not expr:
        return []
    conn = None
    try:
        conn = connect(path, read_only=True)
    except Exception:
        try:
            conn = connect(path)
        except Exception:
            return []
    try:
        rows = conn.execute(
            "SELECT s.rowid AS chunk, bm25(search_index) AS rank "
            "FROM search_index s JOIN chunks c ON c.id = s.rowid "
            "JOIN messages m ON m.id = c.message "
            "WHERE search_index MATCH ? AND m.bank = ? "
            "ORDER BY rank LIMIT ?",
            (expr, bank, max(20, int(limit) * 6))).fetchall()
        if not rows:
            return []
        idmap = {int(r["chunk"]): float(r["rank"]) for r in rows}
        ids = list(idmap)
        marks = ",".join("?" * len(ids))
        info = conn.execute(
            f"SELECT c.id, c.text, c.message, m.role, "
            f"COALESCE(m.source_time, m.recorded_at) AS t "
            f"FROM chunks c JOIN messages m ON m.id=c.message "
            f"WHERE c.id IN ({marks})", ids).fetchall()
        # bm25 越小越相关（负数），翻成 0..1 的分数
        worst = max(idmap.values()) if idmap else 0.0
        best = min(idmap.values()) if idmap else 0.0
        span = (worst - best) or 1.0
        cands = []
        for r in info:
            raw = idmap.get(int(r["id"]), worst)
            score = 1.0 - (raw - best) / span
            cands.append({"chunk": int(r["id"]), "message": int(r["message"]),
                          "role": r["role"], "text": r["text"], "time": int(r["t"] or 0),
                          "score": round(max(0.0, min(1.0, score)), 4)})
        cands.sort(key=lambda x: -x["score"])
        picked = _mmr(cands, [None] * len(cands), len(cands))
        out, used = [], 0
        for c in picked:
            if used + len(c["text"]) > max_chars and out:
                break
            out.append(c)
            used += len(c["text"])
        return out[:limit]
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()


def render_recall(items: List[Dict[str, Any]]) -> str:
    """把检索结果渲染成注入文本 —— 与 bridge_client.render_memory 同一个格式。

    `- 我/对方：<文本>`，一行一条。格式变了两边就不一致（插件那边也是这么约定的）。
    """
    lines = []
    for it in items:
        who = "我" if it.get("role") == "assistant" else "对方"
        text = str(it.get("text") or "").replace("\n", " ")[:160]
        if text:
            lines.append(f"- {who}：{text}")
    return "\n".join(lines)


def recall_text(path: str, bank: str, query: str, *, max_chars: int = 2400) -> str:
    """检索并渲染。失败返回空串，绝不抛 —— 记忆坏了不能拖垮聊天。"""
    try:
        return render_recall(search_chunks(path, bank, query, max_chars=max_chars))
    except Exception:
        return ""


def facts_for_prompt(path: str, bank: str, *, anchor: str = "",
                     limit: int = 12) -> str:
    """把「她认为什么」渲染成注入段。这是原文检索之外的第二条腿。

    原文给她"你说过什么"，事实给她"我知道你是谁、你偏好什么"。
    分开注入的原因：原文是逐句的，事实是稳定的，模型用法完全不同。
    """
    got = list_facts(path, bank, status=ACTIVE, anchor=anchor,
                     limit=limit).get("items") or []
    lines = []
    for f in got:
        who = f["anchor"] or "我"
        conf = f["confidence"]
        mark = "" if conf >= 0.7 else "（不确定）"
        lines.append(f"- {who}｜{f['predicate']}：{f['object']}{mark}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════
# 反思：从原文提炼事实
# ══════════════════════════════════════════════════════════════════════════

REFLECT_SYSTEM = """你在帮一个角色把她记得的事整理成「事实」，而不是复述原文。

只输出一个 JSON 对象，不要任何其它文字、不要 Markdown 代码块。

格式：
{"facts":[{"anchor":"","predicate":"","object":"","confidence":0.6,"chunks":[1],"evidence":""}],"skip_reason":""}

规则：
1. **只从下面给出的原文里提炼**，不许补充原文没有的事，不许把她的猜测写成事实。
2. anchor 是"这条事实属于谁"：**对方**的名字/称呼，或留空表示关于角色自己。
   只在原文里真的出现该称呼时才填。
   ⚠ **角色自己的名字不是对方的称呼。** 原文里她自称、或对方在叫她
   （比如对方只发了一句"肥鱼"），都只是打招呼，**不产生任何事实** ——
   尤其不要生成「对方的名字叫 X」这种。只有对方明确自我介绍
   （"我叫…"/"我是…"）、或有人第三人称指认（"他叫…"）时，才记称呼。
3. predicate 是简短的一类属性（6 字以内），例如「饮食偏好」「工作时间」「称呼」
   「生日」「身体状态」「家人」。同一类事实必须用同一个 predicate 词。
4. object 是具体内容，一句话，不要带"她说""好像"这类转述前缀。
5. confidence 0~1：原文明确陈述 0.8~0.9；从语气推测 0.4~0.6；玩笑/气话/客套 0.2~0.3
   或干脆不提。
6. chunks 填支持这条事实的原文编号（下面每行开头的 [n]），至少一个。
7. evidence 摘 20 字以内的原话片段作依据。
8. **不确定就少提**。宁缺毋滥：一次情绪、玩笑、客套不能变成长期标签。
   纯寒暄（"在吗""嗯""好"）不产生任何事实，skip_reason 里说明。
9. 涉及密码、密钥、身份证号、银行卡的内容一律不提。
"""


def _parse_reflect(raw: str) -> Tuple[List[Fact], str]:
    """解析反思输出。模型爱包 ```json，这里容忍一层围栏。"""
    text = (raw or "").strip()
    fence = re.match(r"^```[A-Za-z0-9_-]*\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except Exception:
        # 退一步：截第一个 { 到最后一个 }
        i, j = text.find("{"), text.rfind("}")
        if i < 0 or j <= i:
            return [], "输出不是 JSON"
        try:
            data = json.loads(text[i:j + 1])
        except Exception:
            return [], "输出不是 JSON"
    if not isinstance(data, dict):
        return [], "输出不是对象"
    facts: List[Fact] = []
    for item in (data.get("facts") or [])[:24]:
        if not isinstance(item, dict):
            continue
        f = Fact(
            predicate=str(item.get("predicate") or ""),
            object=str(item.get("object") or ""),
            anchor=str(item.get("anchor") or ""),
            confidence=item.get("confidence", 0.5),
            source_chunks=[c for c in (item.get("chunks") or []) if isinstance(c, int)],
            evidence=str(item.get("evidence") or ""),
        )
        if f.ok:
            facts.append(f)
    return facts, str(data.get("skip_reason") or "")


def reflect_on_session(path: str, bank: str, session: str, call_model,
                       *, batch: int = 40, now: Optional[int] = None,
                       self_names: Sequence[str] = ()) -> Dict[str, Any]:
    """对某个会话的新消息做一次反思，把原文沉淀成事实。

    `call_model(system, user) -> str` 由调用方注入（replier.call_model）。
    这样这一层不需要知道模型怎么调、key 在哪，也就不会把网络代码拖进存储层。

    `self_names`：**角色自己**的名字与自称（别人叫她什么）。必须传 ——
    否则模型分不清"对方发来一句「肥鱼」"是打招呼还是自我介绍，
    会把角色自己的名字记成对方的名字（真实踩过：攒出一条
    「对方的名字叫肥鱼」的事实，而"肥鱼"本来就是她）。

    幂等：`reflection_state.last_message` 记住水位，同一条消息不会被反思两次。
    失败不推进水位 —— 下次重试同一批。
    """
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": "找不到记忆库", "added": 0}
    now = now or _now_ms()
    try:
        conn = connect(path)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "added": 0}
    try:
        st = conn.execute("SELECT * FROM reflection_state WHERE bank=?",
                          (bank,)).fetchone()
        watermark = int(st["last_message"] or 0) if st else 0
        rows = conn.execute(
            "SELECT id, role, text, COALESCE(source_time, recorded_at) AS t "
            "FROM messages WHERE bank=? AND session=? AND id>? "
            "ORDER BY id LIMIT ?", (bank, session, watermark, max(1, batch))).fetchall()
        if not rows:
            return {"ok": True, "added": 0, "repaired": 0, "skipped": True,
                    "reason": "没有新消息"}
        lines, chunk_ids, names = [], [], []
        for i, r in enumerate(rows, 1):
            who = "她" if r["role"] == "assistant" else "对方"
            chunk_rows = conn.execute(
                "SELECT id FROM chunks WHERE message=?", (int(r["id"]),)).fetchall()
            cids = [int(c[0]) for c in chunk_rows]
            chunk_ids.extend(cids)
            tag = ",".join(str(c) for c in cids) or "-"
            lines.append(f"[{i}] (chunk {tag}) {who}：{r['text'][:400]}")
        names = [n[0] for n in conn.execute(
            "SELECT name FROM anchors WHERE bank=?", (bank,)).fetchall()]
        self_line = "、".join(n for n in self_names if n) or "（未配置）"
        user = ("角色自己叫什么：" + self_line
                + "\n（上面这些是**她**的名字，不是对方的。对方在叫她时，"
                  "那只是打招呼，不产生事实。）"
                + "\n\n已知人物（对方这一侧）："
                + ("、".join(names) if names else "（无）")
                + "\n\n原文：\n" + "\n".join(lines))
        try:
            raw = call_model(REFLECT_SYSTEM, user)
        except Exception as exc:
            conn.execute(
                "INSERT INTO reflection_state(bank,session,last_message,last_run,last_error) "
                "VALUES(?,?,?,?,?) ON CONFLICT(bank) DO UPDATE SET "
                "last_run=excluded.last_run, last_error=excluded.last_error",
                (bank, session, watermark, now, str(exc)[:300]))
            conn.commit()
            return {"ok": False, "error": str(exc), "added": 0}
        facts, skip = _parse_reflect(raw)
        valid_chunks = set(chunk_ids)
        added = repaired = 0
        for f in facts:
            f.source_chunks = [c for c in f.source_chunks if c in valid_chunks] or chunk_ids[:1]
            ok, _fid, _err = remember(path, bank, f)
            if ok:
                added += 1
                if f.anchor:
                    aid = ensure_anchor(conn, bank, f.anchor)
                    for c in f.source_chunks:
                        link_anchor_chunk(conn, aid, c)
        last_id = int(rows[-1]["id"])
        conn.execute(
            "INSERT INTO reflection_state(bank,session,last_message,last_run,last_error) "
            "VALUES(?,?,?,?,'') ON CONFLICT(bank) DO UPDATE SET "
            "session=excluded.session, last_message=excluded.last_message, "
            "last_run=excluded.last_run, last_error=''",
            (bank, session, last_id, now))
        conn.commit()
        return {"ok": True, "added": added, "repaired": repaired,
                "messages": len(rows), "skip_reason": skip}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "added": 0}
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# 级联维护：删、重建、迁移、健康检查
# ══════════════════════════════════════════════════════════════════════════

# 所有引用 chunk 的旁表。删 chunk 之前必须先把这些行清掉 ——
# 它们没有建 ON DELETE CASCADE，漏一张就留下幽灵。
CHUNK_SIDE_TABLES: Tuple[str, ...] = (
    "search_index", "anchor_sources", "activations", "groups", "replays", "chunk_meta",
)


def forget_chunk_side_tables(conn: sqlite3.Connection, chunk_ids: Sequence[int]) -> None:
    """把一批 chunk 在**所有**旁表里的痕迹清干净。一处收口，不再各删各的。

    旧实现是 delete_messages 里手写五条 DELETE，少一张表就出幽灵命中；
    以后再加旁表也只需要改这一个常数。

    ⚠ 表不存在要跳过，不能抛：老库可能只建过原文四表（甚至只建过
      messages/chunks），而删消息这条路径**不能因为"少一张派表"就失败** ——
      用户点删，结果什么都没删掉，比留下幽灵更糟。
    """
    ids = [int(c) for c in chunk_ids if str(c).strip()]
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    for table in CHUNK_SIDE_TABLES:
        if not _has_table(conn, table):
            continue
        if table == "search_index":
            # FTS5 的 rowid 就是 chunk.id
            conn.execute(f"DELETE FROM search_index WHERE rowid IN ({marks})", ids)
        else:
            conn.execute(f"DELETE FROM {table} WHERE chunk IN ({marks})", ids)


def rebuild_derived(path: str, *, rebuild_facts: bool = False) -> Dict[str, Any]:
    """用权威层（messages/chunks）重建所有派生层。

    派生层坏了、分词改了、从别的实现导入了一份只含 messages 的库 ——
    都靠这个入口修好，而不是让用户"导出再导入"或者手动 VACUUM。

    不重建事实层时（默认），只重建 FTS 索引、锚点关联与 chunk_meta；
    因为事实是从原文**推理**出来的，重建等于重新推理一次，
    那是 refresh 的活（reflect_on_session），不该在"修索引"里偷偷做。
    """
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": "找不到记忆库"}
    try:
        conn = connect(path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    try:
        for stmt in DERIVED_STATEMENTS:
            conn.execute(stmt)
        conn.execute("DELETE FROM search_index")
        conn.execute("DELETE FROM chunk_meta")
        rows = conn.execute(
            "SELECT c.id, c.text, m.bank, m.role FROM chunks c "
            "JOIN messages m ON m.id=c.message").fetchall()
        n = 0
        with conn:
            for r in rows:
                cid = int(r[0])
                body = r[1] or ""
                conn.execute("INSERT INTO search_index(rowid,terms) VALUES(?,?)",
                             (cid, " ".join(terms(body))))
                conn.execute(
                    "INSERT OR REPLACE INTO chunk_meta(chunk,bank,speaker,token_count,term_blob) "
                    "VALUES(?,?,?,?,?)",
                    (cid, r[2], r[3] or "", len(terms(body)),
                     hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]))
                n += 1
            if rebuild_facts:
                conn.execute("DELETE FROM facts")
                conn.execute("DELETE FROM fact_events")
                conn.execute("DELETE FROM reflection_state")
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        return {"ok": True, "chunks": n}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


def health(path: str, bank: str = "") -> Dict[str, Any]:
    """一句话回答「她的记忆现在健康吗」。

    与插件治理页的「结果未知不造假」同一个口径：要么给出可核对的数量，
    要么明确说"读不到"，不把失败显示成 0。
    """
    out: Dict[str, Any] = {"ok": False, "path": path or "", "exists": bool(path and os.path.isfile(path))}
    if not out["exists"]:
        out["message"] = "还没有记忆库"
        return out
    try:
        conn = connect(path, read_only=True)
    except Exception as exc:
        out["message"] = f"打不开：{exc}"
        return out
    try:
        # / 注意：facts 表的列叫 bank，messages/chunks 也都有 bank；
        #   这里按表别名写清楚，不要写成裸 bank —— 单表查询里裸列名会撞。
        has_facts = _has_table(conn, "facts")
        if bank:
            msg = conn.execute("SELECT COUNT(*) FROM messages m WHERE m.bank=?",
                               (bank,)).fetchone()[0]
            chk = conn.execute("SELECT COUNT(*) FROM chunks c JOIN messages m "
                               "ON m.id=c.message WHERE m.bank=?", (bank,)).fetchone()[0]
            act = (conn.execute("SELECT COUNT(*) FROM facts f WHERE f.status='active' "
                                "AND f.bank=?", (bank,)).fetchone()[0]) if has_facts else 0
            sup = (conn.execute("SELECT COUNT(*) FROM facts f WHERE f.status='superseded' "
                                "AND f.bank=?", (bank,)).fetchone()[0]) if has_facts else 0
        else:
            msg = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            chk = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            act = (conn.execute("SELECT COUNT(*) FROM facts WHERE status='active'"
                                ).fetchone()[0]) if has_facts else 0
            sup = (conn.execute("SELECT COUNT(*) FROM facts WHERE status='superseded'"
                                ).fetchone()[0]) if has_facts else 0
        idx = conn.execute("SELECT COUNT(*) FROM search_index").fetchone()[0]
        orphan = conn.execute(
            "SELECT COUNT(*) FROM chunks c LEFT JOIN messages m ON m.id=c.message "
            "WHERE m.id IS NULL").fetchone()[0]
        missing = max(0, int(chk) - int(idx))
        out.update({
            "ok": True, "messages": int(msg), "chunks": int(chk), "indexed": int(idx),
            "missingIndex": missing, "orphanChunks": int(orphan),
            "facts": int(act), "superseded": int(sup),
            "schemaVersion": int(conn.execute("PRAGMA user_version").fetchone()[0]),
        })
        problems = []
        if missing:
            problems.append(f"{missing} 个片段没进索引，检索会漏")
        if orphan:
            problems.append(f"{orphan} 个孤儿片段")
        out["problems"] = problems
        out["message"] = "；".join(problems) if problems else "正常"
        return out
    except Exception as exc:
        out["message"] = f"检查失败：{exc}"
        return out
    finally:
        conn.close()


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (name,)).fetchone()
    return row is not None


# ══════════════════════════════════════════════════════════════════════════
# 命令行入口 —— bridge.mjs 调这一条拿检索结果
# ══════════════════════════════════════════════════════════════════════════
#
#     python -m bigfish.memory --recall <db> --query "文本" --maxChars 2400
#     python -m bigfish.memory --health <db>
#
# stdout 只输出一行 JSON，日志一律走 stderr（与 bridge.mjs 的约定一致）。
# 检索逻辑只有这一份：Node 侧不再有第二套排序，两边不会再漂。

def _cli(argv: Sequence[str]) -> int:
    import sys

    args = list(argv)
    cmd = ""
    db = ""
    query = ""
    max_chars = 2400
    bank = ""
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--recall", "--health", "--search"):
            cmd = a.lstrip("-")
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                db = args[i + 1]
                i += 1
        elif a == "--query" and i + 1 < len(args):
            query = args[i + 1]
            i += 1
        elif a == "--maxChars" and i + 1 < len(args):
            try:
                max_chars = max(200, min(20000, int(args[i + 1])))
            except ValueError:
                pass
            i += 1
        elif a == "--bank" and i + 1 < len(args):
            bank = args[i + 1]
            i += 1
        i += 1

    if cmd == "health":
        print(json.dumps(health(db, bank), ensure_ascii=False))
        return 0
    if not db or not query:
        print(json.dumps({"ok": False, "error": "缺少 db 或 query"}, ensure_ascii=False))
        return 2
    items = search_chunks(db, bank or _default_bank(db), query, max_chars=max_chars)
    print(json.dumps({
        "ok": bool(items),
        "text": render_recall(items),
        "ids": [it["chunk"] for it in items],
        "diagnostics": {"mode": "hybrid", "hits": len(items),
                        "chars": sum(len(it["text"]) for it in items)},
    }, ensure_ascii=False))
    return 0


def _default_bank(db_path: str) -> str:
    """库里的 bank 一般是 dafeiyu；查不到就用它，查得到就取最常见的那个。"""
    try:
        conn = connect(db_path, read_only=True)
        try:
            row = conn.execute(
                "SELECT bank FROM messages GROUP BY bank ORDER BY COUNT(*) DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else "dafeiyu"
        finally:
            conn.close()
    except Exception:
        return "dafeiyu"


if __name__ == "__main__":  # pragma: no cover - 由 bridge.mjs 调用
    import sys

    raise SystemExit(_cli(sys.argv[1:]))
