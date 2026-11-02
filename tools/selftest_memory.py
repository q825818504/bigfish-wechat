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

    ok, fid, err = M.remember(db, "dafeiyu", M.Fact(
        predicate="饮食偏好", object="不喝咖啡", anchor="老爸", confidence=0.7,
        owner=OWNER))
    check("新增一条", ok and fid, err)
    got = M.list_facts(db, "dafeiyu")["items"]
    check("列表里有一条 active", len(got) == 1 and got[0]["status"] == "active", str(got))

    ok, fid2, err = M.remember(db, "dafeiyu", M.Fact(
        predicate="饮食偏好", object="不喝咖啡", anchor="老爸", confidence=0.7,
        owner=OWNER))
    check("重复提及是幂等（同 id）", ok and fid2 == fid, f"{fid} vs {fid2}")
    got = M.list_facts(db, "dafeiyu")["items"]
    check("重复提及不新增行", len(got) == 1, str(len(got)))
    check("重复提及提高置信度", got[0]["confidence"] > 0.7, got[0]["confidence"])

    ok, fid3, err = M.remember(db, "dafeiyu", M.Fact(
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
    ok, _f, err = M.remember(db, "dafeiyu", M.Fact(predicate="", object="x", owner=OWNER))
    check("空 predicate 被拒", not ok, err)
    ok, _f, err = M.remember(db, "dafeiyu", M.Fact(predicate="x", object="", owner=OWNER))
    check("空 object 被拒", not ok, err)


def t_search(tmp: str) -> None:
    section("检索：中文命得中、按分数排、去冗余")
    db = os.path.join(tmp, "search.sqlite3")
    M.ensure_schema(db)
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


def t_cascade(tmp: str) -> None:
    section("级联：删消息不能留幽灵")
    db = os.path.join(tmp, "cascade.sqlite3")
    M.ensure_schema(db)
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

    me = M.owner_of_private("owner-wxid")
    other = M.owner_of_private("stranger-wxid")

    # 主人两条：一条私密、一条可共享
    M.remember(db, "dafeiyu", M.Fact(
        predicate="身体状况", object="上个月做了个小手术", anchor="我",
        confidence=0.9, owner=me, visibility=M.VIS_PRIVATE))
    M.remember(db, "dafeiyu", M.Fact(
        predicate="饮食偏好", object="爱吃辣", anchor="我",
        confidence=0.8, owner=me, visibility=M.VIS_SHARED))
    # 陌生人自己一条
    M.remember(db, "dafeiyu", M.Fact(
        predicate="职业", object="做设计的", anchor="对方",
        confidence=0.8, owner=other, visibility=M.VIS_PRIVATE))

    # 主人看自己：能看私密与共享；**但看不到别人的 private**
    # （主人权限管的是"我的东西给不给别人看"，不是"我能不能翻别人的"）
    own_view = M.list_facts(db, "dafeiyu", owner=me, tier=M.TIER_OWNER)["items"]
    objs = {f["object"] for f in own_view}
    check("主人看得到自己的私密", "上个月做了个小手术" in objs, str(objs))
    check("主人看得到自己的共享", "爱吃辣" in objs, str(objs))
    check("主人也看不到别人的私密", "做设计的" not in objs, str(objs))

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

    # 升级 visibility 是显式动作，且会留痕
    fid = [f["id"] for f in M.list_facts(db, "dafeiyu", query="手术")["items"]][0]
    ok, err = M.set_visibility(db, "dafeiyu", fid, M.VIS_SHARED, by="owner")
    check("可以显式改成共享", ok, err)
    after = M.facts_for_prompt(db, "dafeiyu", owner=other, tier=M.TIER_GUEST)
    check("改共享后陌生人可见", "手术" in after, after)
    ev = [e["kind"] for e in M.fact_events(db, "dafeiyu")]
    check("改可见性留了事件", "visibility" in ev, str(ev))


def t_pinned(tmp: str) -> None:
    section("置顶：独立预算，不被普通检索挤掉")
    db = os.path.join(tmp, "pin.sqlite3")
    M.ensure_schema(db)
    me = M.owner_of_private("p")
    ok, fid, _ = M.remember(db, "dafeiyu", M.Fact(
        predicate="称呼", object="要叫她爸爸", anchor="我", confidence=0.9, owner=me))
    check("写入一条", ok and fid)
    # 再塞一堆普通事实
    for i in range(20):
        M.remember(db, "dafeiyu", M.Fact(
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

    wx = M.owner_of_private("wxid_me")
    bl = M.owner_of_private("12345", platform="bilibili")
    st = M.owner_of_private("wxid_stranger")

    # 两个平台各写一条私密
    M.ensure_owner(db, "dafeiyu", wx, display_name="老爸", tier=M.TIER_OWNER)
    M.ensure_owner(db, "dafeiyu", bl, display_name="B站的老爸", tier=M.TIER_OWNER,
                   platform="bilibili")
    M.ensure_owner(db, "dafeiyu", st, display_name="路人", tier=M.TIER_GUEST)
    M.remember(db, "dafeiyu", M.Fact(predicate="微信里说的", object="最近失眠",
                                     owner=wx))
    M.remember(db, "dafeiyu", M.Fact(predicate="B站里说的", object="在追某部番",
                                     owner=bl, platform="bilibili"))
    M.remember(db, "dafeiyu", M.Fact(predicate="路人的事", object="他开了家店",
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
        t_cascade(tmp)
        t_session_delete(tmp)
        t_anchor(tmp)
        t_reflect(tmp)
        t_isolation(tmp)
        t_pinned(tmp)
        t_habits(tmp)
        t_person(tmp)
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
