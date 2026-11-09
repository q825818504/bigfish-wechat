"""记忆层回归测试：事实、纠正、遗忘、检索、级联、迁移。

跑法：
    python tools/selftest_memory.py

覆盖的每一件事，都是"以前做不到"或"以前会静默错"的：
  · 纠正：原来只能删，删了就什么都不剩
  · 遗忘：原来"不记得"和"没发生过"分不清
  · 检索：原来中文整句进 MATCH，一条都命不中
  · 去冗余：原来同一段的三片会一起挤满预算
  · 级联：原来漏一张旁表就出幽灵命中
  · 迁移：原来老库打开就缺表
  · 分词不漂：DBD 里建索引与 bridge 里查索引必须同一套

只用临时目录，不碰真实数据。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import memory as M  # noqa: E402
from bigfish import vault as V  # noqa: E402

# 测试用 owner：私聊「老爸」
OWNER = "private:wechat:test-dad"

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \u2713 {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  \u2717 {name}  {detail}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


# ══════════════════════════════════════════════════════════════════════
# v5 起 remember() 默认校验来源：fact.source_chunks 必须指向真实存在的片段。
# 这是「任何记忆都要有来源」的执行点，所以测试里也必须真的造原文，
# 而不是传 verify_sources=False 绕过去 —— 绕过去就测不到那条校验了。
# ══════════════════════════════════════════════════════════════════════

_SEED_DB = ""


def seed_source(session: str, text: str = "测试原文") -> int:
    """在临时库里造一条消息 + 一个 chunk，返回 chunk id。

    每个用例的 db 不同，所以第一次调用时记下路径；调用方用 use_db() 切换。
    """
    conn = sqlite3.connect(_SEED_DB)
    try:
        cur = conn.execute(
            "INSERT INTO messages(bank,session,event_key,role,text,content_json,recorded_at) "
            "VALUES('dafeiyu',?,?, 'user',?, '[]', 1)",
            (session, f"e{os.urandom(6).hex()}", text))
        mid = int(cur.lastrowid)
        cur2 = conn.execute(
            "INSERT INTO chunks(message,start,end,text,activated) VALUES(?,?,?,?,1)",
            (mid, 0, len(text), text))
        conn.commit()
        return int(cur2.lastrowid)
    finally:
        conn.close()


def use_db(path: str) -> None:
    """声明后续 seed_source 往哪个库造数据。"""
    global _SEED_DB
    _SEED_DB = path


def remember(db: str, bank: str, fact, **kw):
    """造好来源再写 —— 等价于真实链路（reflect 从原文切片拿 chunk id）。"""
    if not fact.source_chunks:
        fact.source_chunks = [seed_source("s")]
    return M.remember(db, bank or "dafeiyu", fact, **kw)


# ══════════════════════════════════════════════════════════════════════
def t_terms() -> None:
    section("分词：与插件 store.js 同一套")
    t = M.terms("老爸今天加班")
    check("汉字串出二字组", "老爸" in t and "加班" in t, str(t))
    check("停用字被去掉", "的" not in t, str(t))
    check("英文整词", "ok" in M.terms("ok 了"), str(M.terms("ok 了")))
    check("去重保序", len(M.terms("好的好的")) == len(set(M.terms("好的好的"))))
    # vault 侧必须指向同一实现，不能有第二份
    check("vault.memory_terms 与 memory.terms 一致",
          V.memory_terms("老爸今天加班") == t)


def t_fts_query() -> None:
    section("检索表达式：整句不许直接进 MATCH")
    q = M.fts_query("老爸今天加班")
    check("切成 OR 组合", " OR " in q, q)
    check("含二元组", '"老爸"' in q and '"加班"' in q, q)
    check("空查询返回空", M.fts_query("") == "")


def t_schema(tmp: str) -> None:
    section("建库与迁移")
    db = os.path.join(tmp, "new.sqlite3")
    ok, err = M.ensure_schema(db)
    check("建新库", ok, err)
    conn = sqlite3.connect(db)
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    has = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    check("schema 版本 = 3", ver == M.SCHEMA_VERSION, str(ver))
    check("事实表存在", "facts" in has)
    check("事件表存在", "fact_events" in has)
    check("原文表也还在", {"messages", "chunks", "search_index"} <= has)

    # 老库（只有 user_version=1 的原文四表）迁移后要补齐
    old = os.path.join(tmp, "old.sqlite3")
    conn = sqlite3.connect(old)
    conn.executescript("""
      CREATE TABLE messages (id INTEGER PRIMARY KEY, bank TEXT NOT NULL,
        session TEXT NOT NULL, event_key TEXT NOT NULL, role TEXT NOT NULL,
        text TEXT NOT NULL, content_json TEXT NOT NULL, source_message_id TEXT,
        recorded_at INTEGER NOT NULL, source_time INTEGER,
        interrupted INTEGER NOT NULL DEFAULT 0, UNIQUE(bank,session,event_key));
      CREATE TABLE chunks (id INTEGER PRIMARY KEY, message INTEGER NOT NULL,
        start INTEGER NOT NULL, end INTEGER NOT NULL, text TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'episodic', importance REAL NOT NULL DEFAULT 0.5,
        activated INTEGER NOT NULL, boosts INTEGER NOT NULL DEFAULT 0,
        last_boost INTEGER, UNIQUE(message,start));
      CREATE VIRTUAL TABLE search_index USING fts5(terms, tokenize='unicode61');
      PRAGMA user_version=1;
    """)
    conn.execute("INSERT INTO messages(id,bank,session,event_key,role,text,content_json,recorded_at) "
                 "VALUES(1,'dafeiyu','s','e1','user','老库里的一句话','[]',1)")
    conn.execute("INSERT INTO chunks(id,message,start,end,text,activated) VALUES(1,1,0,8,'老库里的一句话',1)")
    conn.commit()
    conn.close()
    ok, err = M.migrate(old)
    check("老库迁移成功", ok, err)
    conn = sqlite3.connect(old)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    check("迁移补上了 facts", "facts" in tables, str(sorted(tables)))
    check("原文没丢", M.health(old, "dafeiyu").get("messages") == 1)
    # 迁移必须幂等：重复调用不能抛 "duplicate column name"
    ok2, err2 = M.migrate(old)
    check("重复迁移不报错（幂等）", ok2, err2)
    ok3, err3 = M.migrate(old)
    check("第三次迁移仍然幂等", ok3, err3)
    # 迁移后 facts 必须有 v3 的四列
    conn = sqlite3.connect(old)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(facts)")}
    conn.close()
    check("迁移补上了 owner/visibility", {"owner", "visibility", "platform", "pinned"} <= cols,
          str(sorted(cols)))
    # 迁移后应当能补索引
    r = M.rebuild_derived(old)
    check("老库能重建索引", r.get("ok") and r.get("chunks") == 1, str(r))


def t_facts(tmp: str) -> None:
    section("事实：新增 / 加固 / 纠正 / 遗忘")
    db = os.path.join(tmp, "facts.sqlite3")
    M.ensure_schema(db)
    use_db(db)

    ok, fid, err = remember(db, "dafeiyu", M.Fact(
        predicate="饮食偏好", object="不喝咖啡", anchor="老爸", confidence=0.7,
        owner=OWNER))
    check("新增一条", ok and fid, err)
    got = M.list_facts(db, "dafeiyu")["items"]
    check("列表里有一条 active", len(got) == 1 and got[0]["status"] == "active", str(got))

    ok, fid2, err = remember(db, "dafeiyu", M.Fact(
        predicate="饮食偏好", object="不喝咖啡", anchor="老爸", confidence=0.7,
        owner=OWNER))
    check("重复提及是幂等（同 id）", ok and fid2 == fid, f"{fid} vs {fid2}")
    got = M.list_facts(db, "dafeiyu")["items"]
    check("重复提及不新增行", len(got) == 1, str(len(got)))
    check("重复提及提高置信度", got[0]["confidence"] > 0.7, got[0]["confidence"])

    ok, fid3, err = remember(db, "dafeiyu", M.Fact(
        predicate="饮食偏好", object="改喝无糖豆浆", anchor="老爸", confidence=0.85,
        owner=OWNER))
    check("不同 object → 纠正，产生新行", ok and fid3 != fid, f"{fid} {fid3}")
    allf = M.list_facts(db, "dafeiyu")["items"]
    by_id = {f["id"]: f for f in allf}
    check("两条都在（旧的没被删）", len(allf) == 2, str(len(allf)))
    check("旧的是 superseded", by_id[fid]["status"] == "superseded", by_id[fid]["status"])
    check("旧指针指向新", by_id[fid]["supersededBy"] == fid3, str(by_id[fid]["supersededBy"]))
    check("新的是 active", by_id[fid3]["status"] == "active")
    check("active 只留一条", len(M.list_facts(db, "dafeiyu", status="active")["items"]) == 1)
    check("提示词只用 active",
          "无糖豆浆" in M.facts_for_prompt(db, "dafeiyu", owner=OWNER, tier=M.TIER_OWNER)
          and "不喝咖啡" not in M.facts_for_prompt(db, "dafeiyu", owner=OWNER, tier=M.TIER_OWNER))

    ok, err = M.forget_fact(db, "dafeiyu", fid3, "用户要求")
    check("遗忘成功", ok, err)
    by_id = {f["id"]: f for f in M.list_facts(db, "dafeiyu")["items"]}
    check("遗忘后是 forgotten", by_id[fid3]["status"] == "forgotten", by_id[fid3]["status"])
    check("遗忘后不再注入", M.facts_for_prompt(db, "dafeiyu", owner=OWNER, tier=M.TIER_OWNER) == "")
    check("遗忘仍然查得到（行还在）", fid3 in by_id)

    ev = M.fact_events(db, "dafeiyu")
    kinds = {e["kind"] for e in ev}
    check("事件留痕 add/correct/forget",
          {"add", "correct", "forget"} <= kinds, str(kinds))

    # 空谓词/空对象必须被拒
    ok, _f, err = remember(db, "dafeiyu", M.Fact(predicate="", object="x", owner=OWNER))
    check("空 predicate 被拒", not ok, err)
    ok, _f, err = remember(db, "dafeiyu", M.Fact(predicate="x", object="", owner=OWNER))
    check("空 object 被拒", not ok, err)


def t_search(tmp: str) -> None:
    section("检索：中文命得中、按分数排、去冗余")
    db = os.path.join(tmp, "search.sqlite3")
    M.ensure_schema(db)
    use_db(db)
    cases = [
        "老爸今天加班到十点，说不喝咖啡了",
        "我喜欢下雨天待在窗边发呆",
        "明天要去上海出差三天",
        "那爸爸记得早点睡别熬夜",
        "今天天气很热想喝冰的",
    ]
    for i, text in enumerate(cases):
        V.capture_message("wechat:dad", f"k{i}", "user", text, db_path=db)

    hits = M.search_chunks(db, "dafeiyu", "老爸加班", max_chars=2000)
    check("中文整句查得到（旧实现这里 0 命中）", bool(hits), str(hits))
    check("最相关排第一", hits and "加班" in hits[0]["text"], str(hits[:1]))
    check("分数是 0..1", all(0 <= h["score"] <= 1 for h in hits), str(hits))

    hits2 = M.search_chunks(db, "dafeiyu", "咖啡", max_chars=2000)
    check("单词也能命中", bool(hits2) and "咖啡" in hits2[0]["text"], str(hits2[:1]))

    text = M.recall_text(db, "dafeiyu", "爸爸熬夜")
    check("渲染成 - 对方：… 的格式", text.startswith("- "), repr(text[:40]))

    # 预算：max_chars 不该被某一整段挤爆
    small = M.search_chunks(db, "dafeiyu", "今天 老爸 咖啡 出差", max_chars=40)
    total = sum(len(h["text"]) for h in small)
    check("预算生效（不自爆）", total <= 40 or len(small) <= 1, f"{total}")


def t_chunk_hits(tmp: str) -> None:
    """命中加权：只读连接写不进 chunk_hits（0.11.1 修）。

    以前 `_bump_chunk_hits` 在检索用的 `mode=ro` 连接上 INSERT，
    抛 "attempt to write a readonly database" 又被 except 吞掉，
    于是这张表永远是空的、+0.15 的「以前帮上过忙」加权从没生效过。
    这个用例同时钉住两件事：真的写进去了，以及重复命中会累加。
    """
    section("命中加权：检索要真的记进 chunk_hits")
    db = os.path.join(tmp, "hits.sqlite3")
    M.ensure_schema(db)
    use_db(db)
    V.capture_message("wechat:dad", "h1", "user", "我特别爱吃烧烤，最近老熬夜",
                      db_path=db)

    conn = sqlite3.connect(db)
    n0 = conn.execute("SELECT COUNT(*) FROM chunk_hits").fetchone()[0]
    conn.close()
    check("初始没有命中记录", n0 == 0, str(n0))

    hits = M.search_chunks(db, "dafeiyu", "烧烤", max_chars=2000)
    check("检索有结果", bool(hits), str(hits))

    conn = sqlite3.connect(db)
    rows = [{"chunk": r[0], "hits": r[1]}
            for r in conn.execute("SELECT chunk,hits FROM chunk_hits")]
    conn.close()
    check("检索后写进 chunk_hits", len(rows) == 1, str(rows))
    check("命中计数 = 1", bool(rows) and rows[0]["hits"] == 1, str(rows))

    M.search_chunks(db, "dafeiyu", "烧烤", max_chars=2000)
    conn = sqlite3.connect(db)
    h = conn.execute("SELECT hits FROM chunk_hits").fetchone()
    conn.close()
    check("再检索一次累加到 2", bool(h) and h[0] == 2, str(h and h[0]))

    # 只记真的进了返回集的片段：查一个命中不到的词，不该新增行
    M.search_chunks(db, "dafeiyu", "量子纠缠", max_chars=2000)
    conn = sqlite3.connect(db)
    n2 = conn.execute("SELECT COUNT(*) FROM chunk_hits").fetchone()[0]
    conn.close()
    check("没命中就不记", n2 == 1, str(n2))


def t_cascade(tmp: str) -> None:
    section("级联：删消息不能留幽灵")
    db = os.path.join(tmp, "cascade.sqlite3")
    M.ensure_schema(db)
    use_db(db)
    for i in range(3):
        V.capture_message("wechat:dad", f"c{i}", "user", f"第{i}句话测试级联", db_path=db)

    h = M.health(db, "dafeiyu")
    check("删前健康", h["ok"] and h["problems"] == [], str(h))

    ok, n, err = V.delete_messages(db, [1])
    check("删一条成功", ok and n == 1, f"{ok} {n} {err}")
    h = M.health(db, "dafeiyu")
    check("删后索引无缺口", h["missingIndex"] == 0, str(h))
    check("删后无孤儿", h["orphanChunks"] == 0, str(h))

    conn = sqlite3.connect(db)
    left = conn.execute("SELECT COUNT(*) FROM search_index").fetchone()[0]
    meta = conn.execute("SELECT COUNT(*) FROM chunk_meta").fetchone()[0]
    conn.close()
    check("FTS 里没有已删片段", left == 2, str(left))
    check("chunk_meta 一起清", meta == 2, str(meta))

    # 只建过 messages/chunks 的残库也不能让删除失败
    bare = os.path.join(tmp, "bare.sqlite3")
    conn = sqlite3.connect(bare)
    conn.executescript("""
      CREATE TABLE messages (id INTEGER PRIMARY KEY, bank TEXT, session TEXT,
        event_key TEXT, role TEXT, text TEXT, content_json TEXT, recorded_at INTEGER);
      CREATE TABLE chunks (id INTEGER PRIMARY KEY, message INTEGER, start INTEGER,
        end INTEGER, text TEXT, activated INTEGER);
    """)
    conn.execute("INSERT INTO messages VALUES(1,'b','s','e','user','t','[]',1)")
    conn.execute("INSERT INTO chunks VALUES(1,1,0,1,'t',1)")
    conn.commit()
    conn.close()
    ok, n, err = V.delete_messages(bare, [1])
    check("旁表缺失也不影响删除", ok and n == 1, err)


def t_session_delete(tmp: str) -> None:
    section("删会话：chunk / FTS / meta 一起清")
    db = os.path.join(tmp, "sess.sqlite3")
    M.ensure_schema(db)
    use_db(db)
    V.capture_message("wechat:a", "a1", "user", "会话A的第一句", db_path=db)
    V.capture_message("wechat:a", "a2", "user", "会话A的第二句", db_path=db)
    V.capture_message("wechat:b", "b1", "user", "会话B的唯一一句", db_path=db)
    ok, n, err = V.delete_session(db, "wechat:a")
    check("删会话成功", ok and n == 2, f"{ok} {n} {err}")
    h = M.health(db, "dafeiyu")
    check("剩 1 条且索引一致", h["messages"] == 1 and h["missingIndex"] == 0, str(h))


def t_anchor(tmp: str) -> None:
    section("锚点：别名归一，检索能认出同一个人")
    db = os.path.join(tmp, "anchor.sqlite3")
    M.ensure_schema(db)
    use_db(db)
    conn = M.connect(db)
    aid = M.ensure_anchor(conn, "dafeiyu", "老爸", ["爸爸", "爸"])
    conn.commit()
    check("建锚点", aid is not None)
    check("别名命中：爸爸", M.resolve_anchor(conn, "dafeiyu", "爸爸今天不在家") == aid)
    check("别名命中：爸", M.resolve_anchor(conn, "dafeiyu", "爸说明天出差") == aid)
    check("无关文本不误判", M.resolve_anchor(conn, "dafeiyu", "今天下雨了") is None)
    lst = M.list_anchors(conn, "dafeiyu")
    conn.close()
    check("别名数正确", lst and lst[0]["aliases"] >= 3, str(lst))

    # capture 时自动挂锚点
    V.capture_message("wechat:dad", "x1", "user", "爸爸说晚上回来吃饭", db_path=db)
    conn = M.connect(db)
    linked = conn.execute("SELECT COUNT(*) FROM anchor_sources WHERE anchor=?", (aid,)).fetchone()[0]
    conn.close()
    check("入库时自动挂到锚点", linked >= 1, str(linked))


def t_reflect(tmp: str) -> None:
    section("反思：从原文沉淀事实，幂等且可重复")
    db = os.path.join(tmp, "reflect.sqlite3")
    M.ensure_schema(db)
    use_db(db)
    V.capture_message("wechat:dad", "r1", "user", "我不喝咖啡，改喝无糖豆浆了", db_path=db)
    V.capture_message("wechat:dad", "r2", "assistant", "好，那下次给你带豆浆", db_path=db)

    calls = {"n": 0}

    def fake(system: str, user: str) -> str:
        calls["n"] += 1
        check("提示词里带原文编号", "[1]" in user, user[:60])
        # 角色名必须传进去，否则模型会把「肥鱼」记成对方的名字
        check("提示词里带角色自己的名字", "角色自己叫什么" in user, user[:120])
        check("角色名出现在提示里", "大肥鱼" in user, user[:200])
        return json.dumps({"facts": [{
            "anchor": "对方", "predicate": "饮食偏好", "object": "不喝咖啡，改喝无糖豆浆",
            "confidence": 0.85, "chunks": [1], "evidence": "不喝咖啡"}],
            "skip_reason": ""}, ensure_ascii=False)

    r = M.reflect_on_session(db, "dafeiyu", "wechat:dad", fake,
                          owner=OWNER, tier=M.TIER_OWNER,
                          self_names=["大肥鱼"])
    check("反思成功", r.get("ok"), str(r))
    check("产出 1 条事实", r.get("added") == 1, str(r))
    check("调了一次模型", calls["n"] == 1, str(calls))

    # ⚠ 回归：多条事实必须**全部**入库。
    #   曾经的 bug：ensure_anchor 在反思的长连接上开了未提交写事务，
    #   下一条 remember（另开连接）拿到 "database is locked"，
    #   而错误被丢掉 —— 表现是"模型给了 N 条，库里只多 1 条"，全程无声。
    #   用三条带锚点的事实压住它。
    #
    #   v5 起 chunks 必须是真的：以前这里故意给 []，因为旧代码会用
    #   `or chunk_ids[:1]` 替模型补一个来源 —— 那正是"凭空捏造来源"。
    #   现在空 chunks 一律拒收（见 t_source_required），所以这里要给出
    #   本批原文里真实存在的 chunk 编号。
    V.capture_message("wechat:dad", "r-multi", "user", "又说了一句", db_path=db)
    real_chunk = sqlite3.connect(db).execute(
        "SELECT c.id FROM chunks c JOIN messages m ON m.id=c.message "
        "WHERE m.session='wechat:dad' ORDER BY c.id DESC LIMIT 1").fetchone()[0]

    def many(system, user):
        return json.dumps({"facts": [
            {"anchor": "我", "predicate": "多A", "object": "甲", "confidence": 0.8,
             "chunks": [real_chunk], "evidence": ""},
            {"anchor": "我", "predicate": "多B", "object": "乙", "confidence": 0.8,
             "chunks": [real_chunk], "evidence": ""},
            {"anchor": "老爸", "predicate": "多C", "object": "丙", "confidence": 0.8,
             "chunks": [real_chunk], "evidence": ""},
        ]}, ensure_ascii=False)

    rm = M.reflect_on_session(db, "dafeiyu", "wechat:dad", many,
                              owner=OWNER, tier=M.TIER_OWNER,
                              self_names=["大肥鱼"])
    check("多条事实：无失败项", rm.get("failed") == [], str(rm))
    check("多条事实：三条全进库", rm.get("added") == 3, str(rm))
    got3 = {f["predicate"] for f in M.list_facts(db, "dafeiyu")["items"]}
    check("三条都在库里", {"多A", "多B", "多C"} <= got3, str(sorted(got3)))

    r2 = M.reflect_on_session(db, "dafeiyu", "wechat:dad", fake,
                          owner=OWNER, tier=M.TIER_OWNER,
                          self_names=["大肥鱼"])
    check("没有新消息时不重复反思", r2.get("ok") and r2.get("added") == 0, str(r2))

    # 模型返回带围栏的 JSON 也要认
    facts, skip = M._parse_reflect('```json\n{"facts":[{"predicate":"p","object":"o"}]}\n```')
    check("容忍 Markdown 围栏", len(facts) == 1, str(skip))
    facts, _ = M._parse_reflect("不是 JSON")
    check("非 JSON 安全返回空", facts == [])

    # 模型抛异常 → 不推进水位
    def boom(system: str, user: str) -> str:
        raise RuntimeError("网络断了")

    V.capture_message("wechat:dad", "r3", "user", "还有一句新话", db_path=db)
    r3 = M.reflect_on_session(db, "dafeiyu", "wechat:dad", boom,
                          owner=OWNER, tier=M.TIER_OWNER,
                          self_names=["大肥鱼"])
    check("模型失败 → 报 ok=False", not r3.get("ok"), str(r3))
    r4 = M.reflect_on_session(db, "dafeiyu", "wechat:dad", fake,
                          owner=OWNER, tier=M.TIER_OWNER,
                          self_names=["大肥鱼"])
    check("失败后水位没推进，下次还能重试", r4.get("ok") and r4.get("messages", 0) >= 1, str(r4))


def t_health(tmp: str) -> None:
    section("体检：不把失败伪装成 0")
    check("没有库时明确说不存在", M.health("", "dafeiyu")["ok"] is False)
    bad = os.path.join(tmp, "corrupt.sqlite3")
    with open(bad, "wb") as f:
        f.write(b"not a database at all" * 100)
    h = M.health(bad, "dafeiyu")
    check("坏库返回 ok=False + 原因", h["ok"] is False and bool(h.get("message")), str(h))


def t_cli(tmp: str) -> None:
    section("CLI：bridge.mjs 靠这条拿检索结果")
    db = os.path.join(tmp, "cli.sqlite3")
    M.ensure_schema(db)
    use_db(db)
    V.capture_message("wechat:dad", "c1", "user", "她说周末去爬山", db_path=db)
    import io as _io
    from contextlib import redirect_stdout

    buf = _io.StringIO()
    with redirect_stdout(buf):
        rc = M._cli(["--recall", db, "--query", "爬山", "--maxChars", "800"])
    out = json.loads(buf.getvalue().strip())
    check("退出码 0", rc == 0, str(rc))
    check("输出是 JSON 且有 text", out.get("ok") and out.get("text"), str(out)[:120])


# ══════════════════════════════════════════════════════════════════════
def t_isolation(tmp: str) -> None:
    section("隔离：主人 vs 陌生人，私密永不外泄")
    db = os.path.join(tmp, "iso.sqlite3")
    M.ensure_schema(db)
    use_db(db)

    me = M.owner_of_private("owner-wxid")
    other = M.owner_of_private("stranger-wxid")

    # 主人两条：一条私密、一条可共享
    remember(db, "dafeiyu", M.Fact(
        predicate="身体状况", object="上个月做了个小手术", anchor="我",
        confidence=0.9, owner=me, visibility=M.VIS_PRIVATE, topic="身体"))
    remember(db, "dafeiyu", M.Fact(
        predicate="饮食偏好", object="爱吃辣", anchor="我",
        confidence=0.8, owner=me, visibility=M.VIS_SHARED, topic="食物"))
    # 陌生人自己一条
    remember(db, "dafeiyu", M.Fact(
        predicate="职业", object="做设计的", anchor="对方",
        confidence=0.8, owner=other, visibility=M.VIS_PRIVATE, topic="工作"))

    # 主人看自己：全部可见（v5 起主人能看所有人的内容 —— 数据在主人硬盘上）。
    # ⚠ 「看得见」≠「说得出口」：后者由 open_facts_for 与 guard 插件管。
    own_view = M.list_facts(db, "dafeiyu", owner=me, tier=M.TIER_OWNER)["items"]
    objs = {f["object"] for f in own_view}
    check("主人看得到自己的私密", "上个月做了个小手术" in objs, str(objs))
    check("主人看得到自己的共享", "爱吃辣" in objs, str(objs))
    check("主人也看得到别人的（v5）", "做设计的" in objs, str(objs))

    # 陌生人看：只能拿到 shared，拿不到主人的 private
    other_view = M.list_facts(db, "dafeiyu", owner=other, tier=M.TIER_GUEST)["items"]
    objs2 = {f["object"] for f in other_view}
    check("陌生人看得到共享（爱吃辣）", "爱吃辣" in objs2, str(objs2))
    check("陌生人拿不到主人私密", "上个月做了个小手术" not in objs2, str(objs2))
    check("陌生人看得到自己的", "做设计的" in objs2, str(objs2))

    # 注入函数必须走同一套规则
    prompt_other = M.facts_for_prompt(db, "dafeiyu", owner=other, tier=M.TIER_GUEST)
    check("注入：陌生人拿不到私密", "手术" not in prompt_other, prompt_other)
    check("注入：陌生人能拿到共享", "爱吃辣" in prompt_other, prompt_other)

    # 没 owner 就不注入（宁可空缺也不要瞎给）
    check("缺 owner 时不注入", M.facts_for_prompt(db, "dafeiyu", owner="", tier="") == "")

    # 主人默认值不能是 owner
    check("读不到等级时默认 guest",
          M.owner_tier(db, "private:wechat:nobody") == M.TIER_GUEST)

    # 非数字 owner：昵称不该能当 key（只校验 id 非空）
    try:
        M.owner_of_private("")
        check("空 owner 被拒", False)
    except ValueError:
        check("空 owner 被拒", True)

    # 升级 visibility 是显式动作，且会留痕。
    # v5 起还必须打 topic 才算"开放信息" —— 两个条件是与的关系。
    fid = [f["id"] for f in M.list_facts(db, "dafeiyu", query="手术")["items"]][0]
    ok, err = M.set_visibility(db, "dafeiyu", fid, M.VIS_SHARED, by="owner")
    check("可以显式改成共享", ok, err)
    no_topic = M.facts_for_prompt(db, "dafeiyu", owner=other, tier=M.TIER_GUEST)
    check("只改 shared、没打话题 → 仍然不外传", "手术" not in no_topic, no_topic)
    # ⚠ 「身体」是**黑名单**话题：即使标了 shared + 打了话题，也不外传。
    #   这正是"B 问主人住哪 / 生了什么病 → 她拒绝"的实现。
    ok, err = M.set_topic(db, "dafeiyu", fid, "身体", by="owner")
    check("可以打话题标签", ok, err)
    after = M.facts_for_prompt(db, "dafeiyu", owner=other, tier=M.TIER_GUEST)
    check("shared + 黑名单话题（身体）→ 仍不外传", "手术" not in after, after)

    # 换成白名单话题「食物」，同一 visibility 下就外传了
    fid3 = [f["id"] for f in M.list_facts(db, "dafeiyu", query="爱吃辣")["items"]][0]
    after2 = M.facts_for_prompt(db, "dafeiyu", owner=other, tier=M.TIER_GUEST)
    check("shared + 白名单话题（食物）→ 陌生人可见", "爱吃辣" in after2, after2)
    ev = [e["kind"] for e in M.fact_events(db, "dafeiyu")]
    check("改可见性留了事件", "visibility" in ev, str(ev))

    # ⚠ 关键边界：话题在白名单里，但 visibility 还是 private → 不外传。
    #   这是"你标了话题但没授权"，两个条件缺一不可。
    ok, err = M.set_visibility(db, "dafeiyu", fid, M.VIS_PRIVATE, by="owner")
    check("改回 private", ok, err)
    again = M.facts_for_prompt(db, "dafeiyu", owner=other, tier=M.TIER_GUEST)
    check("话题对但没授权 → 不外传", "手术" not in again, again)

    # 黑名单话题：即使标了 shared，也不外传
    remember(db, "dafeiyu", M.Fact(
        predicate="住址", object="住在朝阳区", anchor="我",
        confidence=0.9, owner=me, visibility=M.VIS_SHARED, topic="住址"))
    black = M.facts_for_prompt(db, "dafeiyu", owner=other, tier=M.TIER_GUEST)
    check("shared + 黑名单话题 → 不外传", "朝阳" not in black, black)

    # 撤回开放：一次把 visibility 和 topic 都清掉
    fid2 = [f["id"] for f in M.list_facts(db, "dafeiyu", query="朝阳")["items"]][0]
    ok, err = M.revoke_open(db, "dafeiyu", fid2)
    check("撤回开放", ok, err)
    row = {f["id"]: f for f in M.list_facts(db, "dafeiyu")["items"]}[fid2]
    check("撤回后变回 private", row["visibility"] == M.VIS_PRIVATE, row["visibility"])
    check("撤回后话题清空", row["topic"] == "", row["topic"])
    check("撤回不是忘掉", row["status"] == "active", row["status"])

    # topic_allowed：空话题一律不外传
    check("空话题不外传", M.topic_allowed("") is False)
    check("白名单话题可外传", M.topic_allowed("食物") is True)
    check("黑名单话题不外传", M.topic_allowed("住址") is False)
    check("灰区话题不外传（年龄）", M.topic_allowed("年龄") is False)
    check("乱写的话题不外传", M.topic_allowed("随便什么") is False)


def t_pinned(tmp: str) -> None:
    section("置顶：独立预算，不被普通检索挤掉")
    db = os.path.join(tmp, "pin.sqlite3")
    M.ensure_schema(db)
    use_db(db)
    me = M.owner_of_private("p")
    ok, fid, _ = remember(db, "dafeiyu", M.Fact(
        predicate="称呼", object="要叫她爸爸", anchor="我", confidence=0.9, owner=me))
    check("写入一条", ok and fid)
    # 再塞一堆普通事实
    for i in range(20):
        remember(db, "dafeiyu", M.Fact(
            predicate=f"杂项{i}", object=f"随便什么{i}", confidence=0.5, owner=me))
    ok, err = M.pin_fact(db, "dafeiyu", fid, pinned=True)
    check("置顶成功", ok, err)
    text = M.facts_for_prompt(db, "dafeiyu", owner=me, tier=M.TIER_OWNER,
                              limit=3, pinned_limit=1)
    check("置顶的排在前面且有星标", text.startswith("- ★"), text[:60])
    check("置顶内容出现", "要叫她爸爸" in text, text[:120])

    ok, err = M.pin_fact(db, "dafeiyu", fid, pinned=False)
    check("取消置顶", ok, err)
    text2 = M.facts_for_prompt(db, "dafeiyu", owner=me, tier=M.TIER_OWNER,
                               limit=3, pinned_limit=1)
    check("取消后没有星标", "★" not in text2, text2[:60])


def t_habits(tmp: str) -> None:
    section("习惯：说够三次且在三个不同日期才算")
    db = os.path.join(tmp, "habit.sqlite3")
    M.ensure_schema(db)
    use_db(db)
    me = M.owner_of_private("h")

    # 同一天说三次：不够格（这就是"今天说了三遍好困"不应该变习惯）
    day1 = 1_790_000_000_000
    for i in range(3):
        M.observe_habit(db, "dafeiyu", me, "好困啊想睡觉", at=day1 + i * 1000)
    txt = M.habits_for_prompt(db, "dafeiyu", owner=me, tier=M.TIER_OWNER)
    check("同一天说三次不算习惯", txt == "", repr(txt))

    # 再跨两天 → 够格
    M.observe_habit(db, "dafeiyu", me, "好困啊想睡觉", at=day1 + 86_400_000)
    M.observe_habit(db, "dafeiyu", me, "好困啊想睡觉", at=day1 + 2 * 86_400_000)
    txt2 = M.habits_for_prompt(db, "dafeiyu", owner=me, tier=M.TIER_OWNER)
    check("跨三天后够格", "好困" in txt2, repr(txt2))
    check("注入的是原话样本不是结论", "说过" in txt2 and "次" in txt2, txt2[:60])

    # 别的 owner 看不到
    other = M.owner_of_private("h2")
    check("别的 owner 看不到", M.habits_for_prompt(db, "dafeiyu", owner=other,
                                                   tier=M.TIER_GUEST) == "")

    # 衰减：放很久以后，评分应下降
    before = M.refresh_habits(db, "dafeiyu", me, now=day1 + 3 * 86_400_000)
    score_now = [r for r in [before]]
    check("重组成功", before.get("ok"), str(before))
    later = M.refresh_habits(db, "dafeiyu", me,
                             now=day1 + 3 * 86_400_000 + 300 * 86_400_000)
    check("放 300 天后仍在（未超上限）", later.get("total", 0) >= 1, str(later))
    import sqlite3 as _s3
    c = _s3.connect(db)
    sc = c.execute("SELECT score FROM habits").fetchone()[0]
    c.close()
    check("衰减后评分变低", sc < 3.0, f"score={sc}")


# ══════════════════════════════════════════════════════════════════════
def t_person(tmp: str) -> None:
    section("person：跨平台同一个人，只能由人认领")
    db = os.path.join(tmp, "person.sqlite3")
    M.ensure_schema(db)
    use_db(db)

    wx = M.owner_of_private("wxid_me")
    bl = M.owner_of_private("12345", platform="bilibili")
    st = M.owner_of_private("wxid_stranger")

    # 两个平台各写一条私密
    M.ensure_owner(db, "dafeiyu", wx, display_name="老爸", tier=M.TIER_OWNER)
    M.ensure_owner(db, "dafeiyu", bl, display_name="B站的老爸", tier=M.TIER_OWNER,
                   platform="bilibili")
    M.ensure_owner(db, "dafeiyu", st, display_name="路人", tier=M.TIER_GUEST)
    remember(db, "dafeiyu", M.Fact(predicate="微信里说的", object="最近失眠",
                                     owner=wx))
    remember(db, "dafeiyu", M.Fact(predicate="B站里说的", object="在追某部番",
                                     owner=bl, platform="bilibili"))
    remember(db, "dafeiyu", M.Fact(predicate="路人的事", object="他开了家店",
                                     owner=st))

    # 认领前：两个平台互相看不见
    t_wx = M.facts_for_prompt(db, "dafeiyu", owner=wx, tier=M.TIER_OWNER)
    t_bl = M.facts_for_prompt(db, "dafeiyu", owner=bl, tier=M.TIER_OWNER)
    check("认领前：微信看不到 B站的", "追某部番" not in t_wx, t_wx)
    check("认领前：B站看不到微信的", "失眠" not in t_bl, t_bl)

    # 建 person 并挂上去
    ok, pid, err = M.create_person(db, "老爸", "同一个人")
    check("建 person", ok and pid, err)
    ok, err = M.link_owner(db, wx, pid)
    check("把微信挂上去", ok, err)
    ok, err = M.link_owner(db, bl, pid)
    check("把 B站也挂上去", ok, err)

    # 认领后：互相能看到
    t_wx2 = M.facts_for_prompt(db, "dafeiyu", owner=wx, tier=M.TIER_OWNER)
    t_bl2 = M.facts_for_prompt(db, "dafeiyu", owner=bl, tier=M.TIER_OWNER)
    check("微信能看到 B站的记忆了", "追某部番" in t_wx2, t_wx2)
    check("B站能看到微信的记忆了", "失眠" in t_bl2, t_bl2)

    # 但别人的私密仍然不可见
    check("别人的私密仍不可见（微信）", "开了家店" not in t_wx2, t_wx2)
    check("别人的私密仍不可见（B站）", "开了家店" not in t_bl2, t_bl2)

    # 陌生人视角：认领不影响"别人能不能看我的"
    t_st = M.facts_for_prompt(db, "dafeiyu", owner=st, tier=M.TIER_GUEST)
    check("陌生人看不到认领者的私密", "失眠" not in t_st and "追某部番" not in t_st, t_st)

    # 合并事件留痕
    import sqlite3 as _s3
    c = _s3.connect(db)
    kinds = [r[0] for r in c.execute("SELECT kind FROM person_events")]
    c.close()
    check("合并留了事件", "create" in kinds and kinds.count("link") == 2, str(kinds))

    # 解除认领：又互相看不见
    ok, err = M.unlink_owner(db, bl)
    check("解除认领", ok, err)
    t_wx3 = M.facts_for_prompt(db, "dafeiyu", owner=wx, tier=M.TIER_OWNER)
    check("解除后又互相看不见", "追某部番" not in t_wx3, t_wx3)

    # 列表
    persons = M.list_persons(db)
    check("list_persons 有结果", len(persons) >= 1, str(persons)[:80])

    # 老事实不搬家：认领后，之前写的 facts.person 保持写入时的值
    c = _s3.connect(db)
    c.row_factory = _s3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT owner, person, person_at_write FROM facts ORDER BY id")]
    c.close()
    check("B站那条写入时还没 person", rows[1]["person_at_write"] == "",
          str(rows[1]))


# ══════════════════════════════════════════════════════════════════════
def t_source_required(tmp: str) -> None:
    section("v5 来源校验：没有真来源就不许入库")
    db = os.path.join(tmp, "src.sqlite3")
    M.ensure_schema(db)
    use_db(db)
    me = M.owner_of_private("src-user")

    # ① chunks 为空 → 拒收
    ok, fid, err = M.remember(db, "dafeiyu", M.Fact(
        predicate="饮食偏好", object="爱吃辣", owner=me))
    check("空来源拒收", not ok and "来源" in err, err)
    check("空来源没有写进库", M.list_facts(db, "dafeiyu")["total"] == 0)

    # ② chunks 指向不存在的 id → 拒收
    ok, fid, err = M.remember(db, "dafeiyu", M.Fact(
        predicate="饮食偏好", object="爱吃辣", owner=me, source_chunks=[99999]))
    check("假来源拒收", not ok and "99999" in err, err)

    # ③ 真来源 → 通过
    cid = seed_source("wechat:src", "我特别爱吃辣")
    ok, fid, err = M.remember(db, "dafeiyu", M.Fact(
        predicate="饮食偏好", object="爱吃辣", owner=me, source_chunks=[cid]))
    check("真来源通过", ok and fid, err)

    # ④ allowed_sessions 限制：别处的 chunk 不能拿来当来源
    other_cid = seed_source("wechat:other-guy", "别的会话里的话")
    ok, fid2, err = M.remember(db, "dafeiyu", M.Fact(
        predicate="饮食偏好", object="也爱吃辣", owner=me, source_chunks=[other_cid]),
        allowed_sessions=["wechat:src"])
    check("跨会话的来源被拒（防嫁接）", not ok, err)

    # ⑤ 反思链路里模型乱指来源 → 那条被丢掉，其余照常
    V.capture_message("wechat:src", "r1", "user", "我特别爱吃辣", db_path=db)

    def bad_chunks(system, user):
        return json.dumps({"facts": [
            {"predicate": "A", "object": "甲", "chunks": [99999], "confidence": 0.8},
            {"predicate": "B", "object": "乙", "chunks": [1], "confidence": 0.8},
        ], "skip_reason": ""}, ensure_ascii=False)

    r = M.reflect_on_session(db, "dafeiyu", "wechat:src", bad_chunks,
                             owner=me, tier=M.TIER_OWNER, self_names=["大肥鱼"])
    check("反思：坏来源那条被拒", any("来源编号无效" in x for x in r.get("failed") or []),
          str(r))
    check("反思：好来源那条入库", r.get("added") == 1, str(r))

    # ⑥ 模型的空 chunks 不再被自动补来源
    def empty_chunks(system, user):
        return json.dumps({"facts": [
            {"predicate": "C", "object": "丙", "chunks": [], "confidence": 0.8}],
            "skip_reason": ""}, ensure_ascii=False)

    V.capture_message("wechat:src", "r2", "user", "随便说点什么", db_path=db)
    r2 = M.reflect_on_session(db, "dafeiyu", "wechat:src", empty_chunks,
                              owner=me, tier=M.TIER_OWNER, self_names=["大肥鱼"])
    check("反思：空来源不自动补（旧行为是 or chunk_ids[:1]）",
          r2.get("added") == 0 and r2.get("failed"), str(r2))


def t_dual_write(tmp: str) -> None:
    section("v5 群聊双写：说话人的池 + 群的池")
    db = os.path.join(tmp, "dual.sqlite3")
    M.ensure_schema(db)
    use_db(db)
    a = M.owner_of_private("owner-a")
    gp = M.owner_of_group("group-c")

    # ① 私密内容：只进 A 池，群池不收
    V.capture_message("group-c", "g1", "user", "我明天去医院", db_path=db)
    cid = sqlite3.connect(db).execute(
        "SELECT c.id FROM chunks c JOIN messages m ON m.id=c.message "
        "WHERE m.session='group-c' ORDER BY c.id DESC LIMIT 1").fetchone()[0]

    res = M.remember_both(db, "dafeiyu", M.Fact(
        predicate="行程", object="明天去医院", owner=a,
        visibility=M.VIS_PRIVATE, topic="行程", origin="group:group-c",
        source_chunks=[cid]), group_owner=gp, allowed_sessions=["group-c"])
    check("人池写入成功", res.get("ok") and res.get("personal"), str(res))
    check("私密内容不进群池", res.get("group") is None, str(res))
    check("并且说明了原因", "私密" in str(res.get("skipped_group")), str(res))

    # ② 公开内容：两边都写
    res2 = M.remember_both(db, "dafeiyu", M.Fact(
        predicate="饮食偏好", object="爱吃烧烤", owner=a,
        visibility=M.VIS_SHARED, topic="食物", origin="group:group-c",
        source_chunks=[cid]), group_owner=gp, allowed_sessions=["group-c"])
    check("公开内容两边都写", res2.get("personal") and res2.get("group"), str(res2))

    # ③ 两份的 owner 不同、origin 相同
    #    ⚠ 这里必须传 tier，否则 list_facts 走"管理视角"（不过滤 owner），
    #      会把全库都返回 —— 测试里踩过一次，看着像双写写多了。
    a_facts = M.list_facts(db, "dafeiyu", owner=a, tier=M.TIER_OWNER, strict=True,
                           status="active")["items"]
    g_facts = M.list_facts(db, "dafeiyu", owner=gp, tier=M.TIER_OWNER, strict=True,
                           status="active")["items"]
    check("A 池有两条", len(a_facts) == 2, str(len(a_facts)))
    check("群池有一条", len(g_facts) == 1, str(len(g_facts)))
    check("群池那份 owner 是群",
          all(f["owner"] == gp for f in g_facts), str(g_facts))
    check("两份 origin 都是 group:group-c",
          g_facts[0]["origin"] == "group:group-c", g_facts[0]["origin"])
    check("群池那份 topic 保留", g_facts[0]["topic"] == "食物", g_facts[0]["topic"])

    # ④ 注入时标出"群里说的"
    txt = M.facts_for_prompt(db, "dafeiyu", owner=a, tier=M.TIER_OWNER)
    check("注入标出群里说的", "群里说的" in txt, txt)

    # ⑤ 删一条，连带删副本
    gid = g_facts[0]["id"]
    res3 = M.delete_fact_pair(db, "dafeiyu", gid)
    check("删群池那条", res3.get("ok"), str(res3))
    check("连带删了 A 池的副本（2 条）", len(res3.get("deleted") or []) == 2, str(res3))
    left_a = M.list_facts(db, "dafeiyu", owner=a, tier=M.TIER_OWNER, strict=True, status="active")["items"]
    check("A 池只剩私密那条", len(left_a) == 1, str(left_a))
    left_g = M.list_facts(db, "dafeiyu", owner=gp, tier=M.TIER_OWNER, strict=True, status="active")["items"]
    check("群池已空", len(left_g) == 0, str(left_g))

    # ⑥ 私聊写入没有副本，删除只删一条
    cid2 = seed_source("wechat:a", "私聊里说的")
    ok, fid = M.remember(db, "dafeiyu", M.Fact(
        predicate="爱好", object="打游戏", owner=a, origin="private",
        source_chunks=[cid2]))[:2]
    res4 = M.delete_fact_pair(db, "dafeiyu", fid)
    check("私聊删除只删自己一条", len(res4.get("deleted") or []) == 1, str(res4))


def t_open_info(tmp: str) -> None:
    section("v5 开放信息：B 能问主人爱吃什么，问不出主人住哪")
    db = os.path.join(tmp, "open.sqlite3")
    M.ensure_schema(db)
    use_db(db)
    a = M.owner_of_private("owner-a")
    b = M.owner_of_private("owner-b")

    cid = seed_source("wechat:a", "主人说过一些事")
    # 主人的四条：食物（白）、住址（黑）、身体（黑）、没打标签的 shared
    M.remember(db, "dafeiyu", M.Fact(
        predicate="饮食偏好", object="爱吃烧烤", owner=a, visibility=M.VIS_SHARED,
        topic="食物", source_chunks=[cid]))
    M.remember(db, "dafeiyu", M.Fact(
        predicate="住址", object="住在朝阳区", owner=a, visibility=M.VIS_SHARED,
        topic="住址", source_chunks=[cid]))
    M.remember(db, "dafeiyu", M.Fact(
        predicate="身体状况", object="上个月做了手术", owner=a,
        visibility=M.VIS_SHARED, topic="身体", source_chunks=[cid]))
    M.remember(db, "dafeiyu", M.Fact(
        predicate="爱好", object="周末钓鱼", owner=a, visibility=M.VIS_SHARED,
        topic="", source_chunks=[cid]))      # 没打标签

    opens = M.open_facts_for(db, "dafeiyu", a)
    objs = {o["object"] for o in opens}
    check("爱吃烧烤 进开放信息", "爱吃烧烤" in objs, str(objs))
    check("住址 不进", "住在朝阳区" not in objs, str(objs))
    check("手术 不进", "上个月做了手术" not in objs, str(objs))
    check("没打标签 不进", "周末钓鱼" not in objs, str(objs))

    # B 的注入里能看到主人的食物偏好，看不到其余
    txt = M.facts_for_prompt(db, "dafeiyu", owner=b, tier=M.TIER_GUEST)
    check("B 能看到主人的食物偏好", "爱吃烧烤" in txt, txt)
    check("B 看不到主人的住址", "朝阳" not in txt, txt)
    check("B 看不到主人的身体状况", "手术" not in txt, txt)
    check("B 看不到没打标签的 shared", "钓鱼" not in txt, txt)
    check("标出了「可以告诉别人」", "可以告诉别人" in txt, txt)

    # owner_said_about：专门回答"关于第三个人"的问题
    about = M.owner_said_about(db, "dafeiyu", b, a, "你主人喜欢吃什么呀")
    check("关于主人的问答：能拿到食物偏好", "爱吃烧烤" in about, about)
    check("关于主人的问答：拿不到住址", "朝阳" not in about, about)
    check("问自己时返回空", M.owner_said_about(db, "dafeiyu", b, b, "我吃啥") == "")

    # 主人自己注入：能看到自己的全部，但看不到 B 的私密
    cid_b = seed_source("wechat:b", "B 说的事")
    M.remember(db, "dafeiyu", M.Fact(
        predicate="秘密", object="B 的秘密", owner=b,
        visibility=M.VIS_PRIVATE, source_chunks=[cid_b]))
    own = M.facts_for_prompt(db, "dafeiyu", owner=a, tier=M.TIER_OWNER)
    check("主人注入：有自己的食物偏好", "爱吃烧烤" in own, own)
    check("主人注入：有自己的住址", "朝阳" in own, own)
    check("主人注入：没有别人的私密", "B 的秘密" not in own, own)


def t_impression(tmp: str) -> None:
    section("v5 角色印象页：一格一人 + 开放信息清单")
    db = os.path.join(tmp, "imp.sqlite3")
    M.ensure_schema(db)
    use_db(db)
    a = M.owner_of_private("owner-a")
    b = M.owner_of_private("owner-b")
    M.ensure_owner(db, "dafeiyu", a, display_name="主人")
    M.ensure_owner(db, "dafeiyu", b, display_name="小李")

    cid = seed_source("wechat:a", "一些事")
    M.remember(db, "dafeiyu", M.Fact(
        predicate="饮食偏好", object="爱吃烧烤", owner=a, visibility=M.VIS_SHARED,
        topic="食物", source_chunks=[cid]))
    M.remember(db, "dafeiyu", M.Fact(
        predicate="住址", object="住在朝阳区", owner=a, visibility=M.VIS_PRIVATE,
        source_chunks=[cid]))

    board = M.impression_board(db, "dafeiyu")
    people = {p["owner"]: p for p in board["people"]}
    check("印象页包含两个人", len(people) == 2, str(list(people)))
    check("主人格里有开放信息", any(
        o["object"] == "爱吃烧烤" for o in people[a]["open"]), str(people[a]["open"]))
    check("住址不在开放信息里", all(
        o["object"] != "住在朝阳区" for o in people[a]["open"]), str(people[a]["open"]))
    check("统计了事实总数", people[a]["facts"] == 2, str(people[a]["facts"]))
    check("统计了私密条数", people[a]["private"] == 1, str(people[a]["private"]))

    ok, err = M.set_impression(db, "dafeiyu", a, "这是个话不多但心细的人")
    check("能写印象评语", ok, err)
    board2 = M.impression_board(db, "dafeiyu")
    got = {p["owner"]: p for p in board2["people"]}
    check("印象评语读得回来", got[a]["note"] == "这是个话不多但心细的人",
          got[a]["note"])
    ok, err = M.set_impression(db, "dafeiyu", a, "改一下")
    check("能改印象评语", ok, err)
    board3 = {p["owner"]: p for p in M.impression_board(db, "dafeiyu")["people"]}
    check("改完读回来是新值", board3[a]["note"] == "改一下", board3[a]["note"])

    # 池总览：只给数量，不给内容
    ps = M.pool_summary(db, "dafeiyu", main_owner=a)
    check("池总览列出两个池", ps["total"] >= 2, str(ps["total"]))
    main = [p for p in ps["pools"] if p["isMain"]]
    check("标出了主人的池", len(main) == 1, str(ps["pools"]))
    check("池总览里有条数", main[0]["facts"] == 2, str(main[0]))
    check("池总览不返回任何事实内容",
          all("object" not in p and "predicate" not in p for p in ps["pools"]),
          str(ps["pools"]))


def t_retrieval_strategy(tmp: str) -> None:
    section("v6 取用策略：记得 ≠ 想得起来")
    db = os.path.join(tmp, "pick.sqlite3")
    M.ensure_schema(db)
    use_db(db)
    me = M.owner_of_private("pick-user")
    cid = seed_source("wechat:pick", "一些往事")

    now = M._now_ms()
    DAY = 86_400_000
    # 一条三年前的旧事，一条昨天的琐事
    M.remember(db, "dafeiyu", M.Fact(
        predicate="医疗", object="拔过智齿", owner=me, confidence=0.9,
        source_chunks=[cid]))
    M.remember(db, "dafeiyu", M.Fact(
        predicate="工作", object="昨天加班到十点", owner=me, confidence=0.9,
        source_chunks=[cid]))
    import sqlite3 as _sq

    c = _sq.connect(db)
    c.execute("UPDATE facts SET created_at=?, updated_at=? WHERE object='拔过智齿'",
              (now - 3 * 365 * DAY, now - 3 * 365 * DAY))
    # 让"加班"这条是昨天刚用过的（久远感只来自时间，不来自使用）
    c.execute("UPDATE facts SET last_used_at=? WHERE object='昨天加班到十点'",
              (now - 1 * DAY,))
    c.commit()
    c.close()

    # ① 带了相关问题：旧事该被相关性顶上来，即使它更老
    got = M.facts_for_prompt(db, "dafeiyu", owner=me, tier=M.TIER_OWNER,
                             limit=1, pinned_limit=0, question="我最近牙疼")
    check("问牙疼 → 捞到三年前拔智齿", "智齿" in got, got)
    check("问牙疼 → 不塞无关的加班", "加班" not in got, got)

    # ② 不带问题：按取用分数排，不按 id
    got2 = M.facts_for_prompt(db, "dafeiyu", owner=me, tier=M.TIER_OWNER,
                              limit=2, pinned_limit=0)
    check("无问题 → 两条都进", "智齿" in got2 and "加班" in got2, got2)

    # ③ 用过之后 use_count 涨了
    row = {f["object"]: f for f in
           M.list_facts(db, "dafeiyu", query="智齿")["items"]}["拔过智齿"]
    check("被注入过的事实记了使用次数", row["useCount"] >= 1, str(row["useCount"]))
    check("记了上次使用时间", row["lastUsedAt"] > 0, str(row["lastUsedAt"]))

    # ④ 三年前那次使用 → 分数明显低于昨天用过的
    old = dict(row, lastUsedAt=now - 3 * 365 * DAY)
    new = dict(row, lastUsedAt=now - 1 * DAY)
    check("久没用过的分数更低",
          M._fact_score(old, now) < M._fact_score(new, now),
          f"{M._fact_score(old, now):.3f} vs {M._fact_score(new, now):.3f}")
    # 但不会归零 —— 降权不是删除
    check("久没用过也不会归零", M._fact_score(old, now) > 0,
          str(M._fact_score(old, now)))

    # ⑤ 从没用过的按"全新鲜"算，不会一起沉底
    fresh = dict(row, lastUsedAt=0, useCount=0, confidence=0.9)
    check("从没用过 = 不折旧", abs(M._fact_score(fresh, now) - 0.9) < 1e-6,
          str(M._fact_score(fresh, now)))

    # ⑥ 相关性用同一套分词
    #
    # ⚠ 这里只能测**字面共词**。分词器不做同义词扩展 ——
    #   「牙疼」和「拔过智齿」一个字都不共，相关性就是 0，
    #   要它们关联起来得靠别的机制（同一条事实、或画像）。
    #   一开始我按"牙疼应该命中智齿"写了断言，那是错的：
    #   分词器没那么聪明，硬要它聪明就得接语义模型，那是另一个量级的成本。
    check("相关性：牙疼 命中 拔牙",
          M._relevance({"predicate": "医疗", "object": "去年拔过牙"}, "我最近牙疼") > 0)
    check("相关性：牙疼 不命中 智齿（无共词）",
          M._relevance({"predicate": "医疗", "object": "拔过智齿"}, "我最近牙疼") == 0)
    check("相关性：无关问题给 0",
          M._relevance({"predicate": "医疗", "object": "拔过牙"}, "今天天气不错") == 0)
    check("相关性：空问题给 0",
          M._relevance({"predicate": "医疗", "object": "拔过牙"}, "") == 0)

    # ⑦ 置顶必进，即使不相关
    fid = [f["id"] for f in M.list_facts(db, "dafeiyu", query="加班")["items"]][0]
    M.pin_fact(db, "dafeiyu", fid, pinned=True)
    got3 = M.facts_for_prompt(db, "dafeiyu", owner=me, tier=M.TIER_OWNER,
                              limit=1, pinned_limit=1, question="我最近牙疼")
    check("置顶的即使不相关也进", "加班" in got3, got3)

    # ⑧ mark_used 空输入安全
    check("mark_used 空输入返回 0", M.mark_used(db, "dafeiyu", []) == 0)


def t_profile(tmp: str) -> None:
    section("v6 画像：把几百条事实压成一段话")
    db = os.path.join(tmp, "prof.sqlite3")
    M.ensure_schema(db)
    use_db(db)
    me = M.owner_of_private("prof-user")
    M.ensure_owner(db, "dafeiyu", me, display_name="老王", tier=M.TIER_OWNER)
    cid = seed_source("wechat:prof", "一些事")

    # 事实太少：不该归纳
    M.remember(db, "dafeiyu", M.Fact(predicate="饮食", object="爱吃辣",
                                     owner=me, source_chunks=[cid]))
    check("只有 1 条事实时不归纳", M.should_refresh_profile(db, "dafeiyu", me) is False)

    for p, o in [("爱好", "养了只猫"), ("职业", "做设计"), ("作息", "经常熬夜")]:
        M.remember(db, "dafeiyu", M.Fact(predicate=p, object=o,
                                         owner=me, source_chunks=[cid]))
    check("事实够了就归纳", M.should_refresh_profile(db, "dafeiyu", me) is True)

    calls = {"n": 0}

    def fake(system: str, user: str) -> str:
        calls["n"] += 1
        # 提示词里必须带事实，且不许出现别人的
        check("画像提示词带事实", "爱吃辣" in user, user[:80])
        return "```\n老王是做设计的，爱吃辣，养了只猫，最近经常熬夜。\n```"

    r = M.refresh_profile(db, "dafeiyu", me, fake, name="老王")
    check("归纳成功", r.get("ok"), str(r))
    check("剥掉了 markdown 围栏", "```" not in (M.get_profile(db, "dafeiyu", me)["text"]),
          M.get_profile(db, "dafeiyu", me)["text"][:40])
    check("记下了用了多少条事实", r.get("fromFacts") == 4, str(r.get("fromFacts")))

    txt = M.profiles_for_prompt(db, "dafeiyu", owner=me)
    check("画像能注入", "做设计" in txt, txt)
    check("注入有长度上限", len(txt) <= M.PROFILE_MAX_CHARS, str(len(txt)))

    # 归纳完不该马上又提示要归纳
    check("刚归纳完不重复归纳", M.should_refresh_profile(db, "dafeiyu", me) is False)

    # 否决：注入立刻为空，但画像还在
    ok, err = M.set_profile_enabled(db, "dafeiyu", me, False)
    check("能停用", ok, err)
    check("停用后不注入", M.profiles_for_prompt(db, "dafeiyu", owner=me) == "")
    check("停用后画像仍留着", bool(M.get_profile(db, "dafeiyu", me)["text"]))

    # ⚠ 关键：重新归纳不能把"停用"改回"启用"
    M.refresh_profile(db, "dafeiyu", me, fake, name="老王")
    check("重新归纳不会偷偷恢复启用",
          M.get_profile(db, "dafeiyu", me)["enabled"] is False,
          str(M.get_profile(db, "dafeiyu", me)["enabled"]))
    check("停用状态下仍然不注入", M.profiles_for_prompt(db, "dafeiyu", owner=me) == "")

    ok, err = M.set_profile_enabled(db, "dafeiyu", me, True)
    check("能恢复", ok, err)
    check("恢复后又能注入", "做设计" in M.profiles_for_prompt(db, "dafeiyu", owner=me))

    # 别人的画像不能串
    other = M.owner_of_private("prof-other")
    check("别人的画像为空", M.profiles_for_prompt(db, "dafeiyu", owner=other) == "")

    # 印象页要同时给出 手写评语 与 画像
    M.set_impression(db, "dafeiyu", me, "我眼里的他")
    board = {p["owner"]: p for p in M.impression_board(db, "dafeiyu")["people"]}
    check("印象页带画像", "做设计" in (board[me]["profile"] or ""), board[me]["profile"][:40])
    check("印象页带手写评语", board[me]["note"] == "我眼里的他", board[me]["note"])
    check("两者分开给", board[me]["note"] != board[me]["profile"])


def main() -> int:
    print("=" * 66)
    print("记忆层自检")
    print("=" * 66)
    t_terms()
    t_fts_query()
    tmp = tempfile.mkdtemp(prefix="bf-memtest-")
    old_env = os.environ.get("BIGFISH_DATA_ROOT")
    os.environ["BIGFISH_DATA_ROOT"] = tmp
    try:
        t_schema(tmp)
        t_facts(tmp)
        t_search(tmp)
        t_chunk_hits(tmp)
        t_cascade(tmp)
        t_session_delete(tmp)
        t_anchor(tmp)
        t_reflect(tmp)
        t_isolation(tmp)
        t_pinned(tmp)
        t_habits(tmp)
        t_person(tmp)
        t_source_required(tmp)
        t_dual_write(tmp)
        t_open_info(tmp)
        t_impression(tmp)
        t_retrieval_strategy(tmp)
        t_profile(tmp)
        t_health(tmp)
        t_cli(tmp)
    finally:
        if old_env is None:
            os.environ.pop("BIGFISH_DATA_ROOT", None)
        else:
            os.environ["BIGFISH_DATA_ROOT"] = old_env
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 66)
    if FAIL:
        print(f"结果：{PASS}/{PASS + FAIL} 通过，{FAIL} 项失败")
        print("失败项：")
        for name in FAILED:
            print(f"  \u00b7 {name}")
        print("=" * 66)
        return 1
    print(f"结果：{PASS}/{PASS} 全部通过")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        if _s is not None:
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    raise SystemExit(main())
