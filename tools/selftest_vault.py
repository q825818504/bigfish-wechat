#!/usr/bin/env python
"""大肥鱼 · 灵魂/心情/记忆 可写层自检。

覆盖 vault.py 的每一条写路径，全部在**临时目录**里跑，绝不碰真实数据。

    python tools/selftest_vault.py

为什么要有这份：vault.py 不是在造新东西，而是在 Python 里复刻
dsh-bigfish/lib/soul/store.js 的语义。复刻最容易出的错是「看起来对，
但和原版差一点点」——夹取范围差一格、revision 没跟上、locked 保护漏了。
这些错在界面上都表现成"保存了但没生效"，用户根本查不出来。
所以每一条都必须对着插件的行为断言一次。
"""
from __future__ import annotations

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import traceback

# Windows 控制台默认 GBK，✓ / ✗ 会直接抛 UnicodeEncodeError。
# 先切 UTF-8；切不动就退成 ASCII 记号，绝不让编码问题盖住真正的测试结果。
for _s in (sys.stdout, sys.stderr):
    if _s is None:
        continue
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if (sys.stdout.encoding or "").lower() not in ("utf-8", "utf8"):
    OK_MARK, NG_MARK = "[OK]", "[XX]"
else:
    OK_MARK, NG_MARK = "✓", "✗"

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bigfish import vault  # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []


def _mk_tmp(prefix: str) -> str:
    """在仓库 .tmp/ 下建临时目录，不在仓库根留垃圾。

    ⚠ 用默认的 tempfile.mkdtemp 会在 %TEMP% 里建 —— 某些环境对系统临时目录
      有写限制（实测过 mkdtemp 直接 EPERM）。统一放 .tmp/ 下既避开了那个限制，
      路径也可预测、好清理。.tmp/ 已在 .gitignore 里。
    """
    base = os.path.join(_ROOT, ".tmp")
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=base)


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  {OK_MARK} {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  {NG_MARK} {name}" + (f"  —— {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 58 - len(title)))


# ══════════════════════════════════════════════════════════════════════
def test_scope_key() -> None:
    section("范围键（与 scope-key.js 对齐）")

    check("工作区键", vault.scope_key("workspace", "abc-123") == "ws-abc-123")
    check("空工作区 → 空串", vault.scope_key("workspace", "") == "")
    check("会话键取排序第一个",
          vault.scope_key("session", "", ["z", "a", "m"]) == "ss-a")
    check("空会话 → 空串", vault.scope_key("session", "", []) == "")
    check("未知模式 → 空串", vault.scope_key("nope", "x") == "")

    check("短键截 16 位",
          vault.short_key("ws-83ba9d80-ec53-4a24-9245-7ce877a74e70")
          == "ws-83ba9d80-ec53")
    check("短键只留字母数字下划线横线",
          vault.short_key("ws-a/b\\c*d e-f") == "ws-abcde-f",
          repr(vault.short_key("ws-a/b\\c*d e-f")))

    root = r"D:\x"
    check("灵魂路径",
          vault.scope_file_path(root, "ws-83ba9d80-ec53-aaaa", "soul")
          == os.path.join(root, "ws-83ba9d80-ec53-aaaa", "soul-ws-83ba9d80-ec53.json"),
          vault.scope_file_path(root, "ws-83ba9d80-ec53-aaaa", "soul"))
    check("记忆路径结尾",
          vault.scope_file_path(root, "ws-1", "memory").endswith("memory-ws-1.sqlite3"))
    check("心情路径结尾",
          vault.scope_file_path(root, "ws-1", "mood").endswith("mood-ws-1.json"))
    check("空键 → 空路径", vault.scope_file_path(root, "", "soul") == "")


# ══════════════════════════════════════════════════════════════════════
def test_soul_normalize() -> None:
    section("灵魂归一化（与 normalizeSoul 对齐）")

    s = vault.normalize_soul(None)
    check("None → 默认灵魂", s["identity"]["name"] == "大肥鱼")
    check("默认称呼是「爸爸」", s["identity"]["address"] == "爸爸")
    check("默认 revision = 1", s["revision"] == 1)
    check("默认 4 条价值", len(s["values"]) == 4)
    check("默认全部锁定", all(v["locked"] for v in s["values"]))
    check("默认 4 个性格键", set(s["traits"]) == set(vault.TRAIT_DOC))
    check("默认 warmth=65", s["traits"]["warmth"] == 65)

    # 夹取：越界值必须被夹进 0..100
    s = vault.normalize_soul({"traits": {"warmth": 999, "assertiveness": -5}})
    check("warmth 999 → 100", s["traits"]["warmth"] == 100, str(s["traits"]["warmth"]))
    check("assertiveness -5 → 0", s["traits"]["assertiveness"] == 0)

    # 未知 trait 键必须丢弃（否则会注入无意义字段）
    s = vault.normalize_soul({"traits": {"warmth": 50, "bogus": 77}})
    check("未知 trait 键被丢弃", "bogus" not in s["traits"])

    # 非数字回退默认
    s = vault.normalize_soul({"traits": {"warmth": "abc"}})
    check("非数字回退默认 65", s["traits"]["warmth"] == 65)

    # 浮点四舍五入
    s = vault.normalize_soul({"traits": {"warmth": 64.6}})
    check("64.6 → 65", s["traits"]["warmth"] == 65, str(s["traits"]["warmth"]))

    # 价值：空数组是真实意图，不能偷偷补回默认
    s = vault.normalize_soul({"values": []})
    check("values=[] 保持空（不补默认）", s["values"] == [], str(s["values"]))

    # 价值：id 撞了要重分配而不是丢弃
    s = vault.normalize_soul({"values": [
        {"id": "v1", "text": "甲"}, {"id": "v1", "text": "乙"}]})
    check("重复 id 被重分配而非丢弃", len(s["values"]) == 2, str(s["values"]))
    check("重分配后 id 唯一",
          len({v["id"] for v in s["values"]}) == 2, str(s["values"]))

    # 空文本条目被丢弃
    s = vault.normalize_soul({"values": [{"text": "  "}, {"text": "好的"}]})
    check("空文本被丢弃", len(s["values"]) == 1 and s["values"][0]["text"] == "好的")

    # locked 默认 True
    s = vault.normalize_soul({"values": [{"text": "x"}]})
    check("条目默认 locked=True", s["values"][0]["locked"] is True)
    s = vault.normalize_soul({"values": [{"text": "x", "locked": False}]})
    check("显式 locked=false 保留", s["values"][0]["locked"] is False)

    # 文本超长截断
    s = vault.normalize_soul({"identity": {"name": "x" * 100}})
    check("名字截到 40 字", len(s["identity"]["name"]) == 40)

    # 完全脏输入不抛
    try:
        vault.normalize_soul({"traits": [], "values": "str", "identity": 5,
                              "selfNotes": {"a": 1}, "history": "x"})
        check("脏结构不抛异常", True)
    except Exception as exc:
        check("脏结构不抛异常", False, str(exc))


def test_trait_sentence() -> None:
    section("性格文案（与 traitSentence 分档一致）")
    # 分档：<=40 low / >=60 high / 其余 mid
    check("warmth 40 → 低档",
          vault.trait_sentence("warmth", 40) == "温暖：克制，不主动靠近。",
          vault.trait_sentence("warmth", 40))
    check("warmth 41 → 中档",
          vault.trait_sentence("warmth", 41) == "温暖：关心，但不黏人。",
          vault.trait_sentence("warmth", 41))
    check("warmth 59 → 中档",
          vault.trait_sentence("warmth", 59) == "温暖：关心，但不黏人。")
    check("warmth 60 → 高档",
          vault.trait_sentence("warmth", 60) == "温暖：明显关心，会主动问候。",
          vault.trait_sentence("warmth", 60))
    check("未知键返回空串", vault.trait_sentence("bogus", 50) == "")


def test_soul_io(tmp: str) -> None:
    section("灵魂读写（原子写 + revision）")

    path = os.path.join(tmp, "ws-test", "soul-ws-test.json")

    soul, exists, err = vault.read_soul_file(path)
    check("文件不存在不算错误", exists is False and err == "")
    check("读不到时给默认灵魂", soul["identity"]["name"] == "大肥鱼")

    ok, out, err = vault.write_soul_file(path, {"identity": {"name": "小蓝"},
                                                "values": [], "traits": {}})
    check("写入成功", ok, err)
    check("落盘 revision = 2（基于默认 1 +1）", out["revision"] == 2, str(out["revision"]))
    check("没有临时文件残留", not os.path.exists(path + ".tmp"))

    soul, exists, err = vault.read_soul_file(path)
    check("读回 exists=True", exists is True)
    check("读回名字正确", soul["identity"]["name"] == "小蓝")
    check("读回 values 保持空", soul["values"] == [])

    # 再写一次：revision 基于磁盘现值继续 +1
    ok, out2, _ = vault.write_soul_file(path, {"identity": {"name": "小蓝"}, "values": []})
    check("第二次写 revision = 3", out2["revision"] == 3, str(out2["revision"]))

    # 文件损坏 → 回退默认但不抛
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ 这不是 json")
    soul, exists, err = vault.read_soul_file(path)
    check("损坏文件回退默认", soul["identity"]["name"] == "大肥鱼")
    check("损坏文件 exists=False", exists is False)
    check("损坏文件带出 error", bool(err))

    # 原子性：写之前就存在 .tmp 也不能污染结果
    ok, out3, _ = vault.write_soul_file(path, {"identity": {"name": "恢复"}})
    check("从损坏状态能正常写回", ok and out3["identity"]["name"] == "恢复")


# ══════════════════════════════════════════════════════════════════════
def test_scope_io(tmp: str) -> None:
    section("scope.json 读写")

    root = os.path.join(tmp, "root")
    os.makedirs(root, exist_ok=True)

    check("没有 scope.json 时返回 None", vault.read_scope(root) is None)

    ok, err = vault.write_scope(root, "workspace", "")
    check("工作区模式缺 id → 拒绝", not ok, err)

    ok, err = vault.write_scope(root, "session", "", [])
    check("会话模式缺 id → 拒绝", not ok)

    ok, err = vault.write_scope(root, "nope", "x")
    check("非法模式 → 拒绝", not ok)

    ok, err = vault.write_scope(root, "workspace", "uuid-1")
    check("工作区模式写入成功", ok, err)

    sc = vault.read_scope(root)
    check("读回 mode", sc and sc["mode"] == "workspace")
    check("读回 workspaceId", sc and sc["workspaceId"] == "uuid-1")
    check("updatedAt 是毫秒", sc and sc["updatedAt"] > 1_000_000_000_000,
          str(sc and sc["updatedAt"]))

    ok, _ = vault.write_scope(root, "session", "", ["s2", "s1", "s1"])
    sc = vault.read_scope(root)
    check("会话 id 去重排序", sc["sessionIds"] == ["s1", "s2"], str(sc["sessionIds"]))

    # 损坏的 scope.json → None（不能崩）
    with open(os.path.join(root, "scope.json"), "w", encoding="utf-8") as f:
        f.write("not json")
    check("损坏 scope.json → None", vault.read_scope(root) is None)


def test_infer_scope(tmp: str) -> None:
    section("范围推导兜底（与 inferScopeKey 对齐）")

    root = os.path.join(tmp, "root2")
    os.makedirs(root, exist_ok=True)
    check("空目录 → 空串", vault.infer_scope_key(root) == "")

    os.makedirs(os.path.join(root, "ws-a"))
    check("只有一个范围 → 直接用它", vault.infer_scope_key(root) == "ws-a")

    os.makedirs(os.path.join(root, "ws-b"))
    os.makedirs(os.path.join(root, "ss-c"))
    # ws-a / ws-b / ss-c 都是空的，应该按 mtime 取最新
    with open(os.path.join(root, "ws-b", "soul-ws-b.json"), "w") as f:
        f.write("{}")
    check("有灵魂的优先", vault.infer_scope_key(root) == "ws-b")

    with open(os.path.join(root, "ss-c", "mood-ss-c.json"), "w") as f:
        f.write("{}")
    check("灵魂优先于心情", vault.infer_scope_key(root) == "ws-b")


# ══════════════════════════════════════════════════════════════════════
def test_mood_config(tmp: str, home: str) -> None:
    section("心情配置读写（未知字段必须原样保留）")

    os.environ["DSH_HOME"] = home
    cfg_path = vault.mood_config_path()
    check("配置路径跟随 DSH_HOME", cfg_path.startswith(home), cfg_path)

    # 没有配置 → 默认值
    c = vault.read_mood_config()
    check("默认 moodBase=70", c["moodBase"] == 70)
    check("默认 injectMode=event", c["injectMode"] == "event")
    check("默认 decayHalfHours=12", c["decayHalfHours"] == 12)

    # 写一份带未知字段的配置
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"moodBase": 55, "dataRoot": r"D:\保留我", "weird": {"a": 1}}, f)

    ok, out, err = vault.write_mood_config({"moodBase": 62})
    check("写入成功", ok, err)
    check("moodBase 已改", out["moodBase"] == 62)
    check("dataRoot 被保留（不能抹掉）", out.get("dataRoot") == r"D:\保留我",
          str(out.get("dataRoot")))
    check("未知字段被保留", out.get("weird") == {"a": 1})

    # 夹取
    ok, out, _ = vault.write_mood_config({"moodBase": 999, "sigMood": -3,
                                          "decayHalfHours": 9999})
    check("moodBase 999 → 100", out["moodBase"] == 100)
    check("sigMood -3 → 1", out["sigMood"] == 1)
    check("decayHalfHours 9999 → 168", out["decayHalfHours"] == 168)

    # 非法 injectMode 不生效
    ok, out, _ = vault.write_mood_config({"injectMode": "bogus"})
    check("非法 injectMode 被忽略", out["injectMode"] == "event", out["injectMode"])

    ok, out, _ = vault.write_mood_config({"injectMode": "off"})
    check("合法 injectMode 生效", out["injectMode"] == "off")

    # 布尔
    ok, out, _ = vault.write_mood_config({"autoJudge": False})
    check("autoJudge False 生效", out["autoJudge"] is False)


def test_mood_reset(tmp: str, home: str) -> None:
    section("心情重置")

    os.environ["DSH_HOME"] = home
    root = os.path.join(tmp, "moodroot")
    key = "ws-moodtest"
    d = os.path.join(root, key)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "mood-ws-moodtest.json")

    state = {
        "version": 2,
        "moods": {key: {
            "mood": 23,
            "dims": {"happy": 0.8, "calm": 0.1, "sweet": 0.3,
                     "heartache": 0.5, "worry": 0.4, "sad": 0.6, "jealous": 0.2},
            "lastReason": "他说了句重话",
            "lastConfidence": 88,
            "timeline": [{"at": 1, "mood": 23}],
            "updatedAt": 111,
        }},
        "lastActive": key,
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(state, f)

    vault.write_data_root(root)
    ok, err = vault.reset_mood()
    check("重置成功", ok, err)

    with open(p, "r", encoding="utf-8") as f:
        after = json.load(f)
    slot = after["moods"][key]
    cfg = vault.read_mood_config()
    check("mood 回到基线", slot["mood"] == cfg["moodBase"], str(slot["mood"]))
    check("dims 全部归零", all(v == 0 for v in slot["dims"].values()), str(slot["dims"]))
    check("lastReason 清空", slot["lastReason"] == "")
    check("时间线保留（是历史证据）", slot["timeline"] == [{"at": 1, "mood": 23}],
          str(slot["timeline"]))

    vault.write_data_root("")


# ══════════════════════════════════════════════════════════════════════
def test_memory(tmp: str) -> None:
    section("记忆库读写删（chunk 级联必须一起清）")

    db = os.path.join(tmp, "mem", "memory-ws-t.sqlite3")
    os.makedirs(os.path.dirname(db), exist_ok=True)

    # 建一个与 MemoryStore 结构一致的库
    conn = sqlite3.connect(db)
    conn.executescript("""
      CREATE TABLE messages (
        id INTEGER PRIMARY KEY, bank TEXT NOT NULL, session TEXT NOT NULL,
        event_key TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL,
        content_json TEXT NOT NULL, source_message_id TEXT,
        recorded_at INTEGER NOT NULL, source_time INTEGER,
        interrupted INTEGER NOT NULL DEFAULT 0,
        UNIQUE(bank,session,event_key));
      CREATE TABLE chunks (
        id INTEGER PRIMARY KEY, message INTEGER NOT NULL REFERENCES messages(id),
        start INTEGER NOT NULL, end INTEGER NOT NULL, text TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'episodic',
        importance REAL NOT NULL DEFAULT 0.5, activated INTEGER NOT NULL,
        boosts INTEGER NOT NULL DEFAULT 0, last_boost INTEGER, UNIQUE(message,start));
      CREATE VIRTUAL TABLE search_index USING fts5(terms, tokenize='unicode61');
      CREATE TABLE anchors (id INTEGER PRIMARY KEY, bank TEXT NOT NULL,
        name TEXT NOT NULL, UNIQUE(bank,name));
      CREATE TABLE anchor_sources (anchor INTEGER, chunk INTEGER, PRIMARY KEY(anchor,chunk));
      CREATE TABLE activations (bank TEXT, chunk INTEGER, event_key TEXT,
        at INTEGER, PRIMARY KEY(bank,chunk,event_key));
      CREATE TABLE ledger (id INTEGER PRIMARY KEY, bank TEXT, at INTEGER,
        kind TEXT, chars INTEGER, sources TEXT);
      CREATE TABLE housekeeping (bank TEXT PRIMARY KEY, last_chunk INTEGER);
      CREATE TABLE groups (chunk INTEGER PRIMARY KEY, bank TEXT, digest TEXT);
      CREATE TABLE replays (chunk INTEGER PRIMARY KEY, at INTEGER);
    """)
    for i in range(1, 6):
        sess = "session-a@1" if i <= 3 else "session-b@2"
        conn.execute(
            "INSERT INTO messages(id,bank,session,event_key,role,text,content_json,"
            "recorded_at,source_time) VALUES(?,?,?,?,?,?,?,?,?)",
            (i, "dafeiyu", sess, f"e{i}", "user" if i % 2 else "assistant",
             f"这是第 {i} 条记忆内容", "[]", 1700000000000 + i, 1700000000000 + i))
        cid = i * 10
        conn.execute(
            "INSERT INTO chunks(id,message,start,end,text,activated) VALUES(?,?,?,?,?,?)",
            (cid, i, 0, 5, f"这是第 {i} 条记忆内容", 1))
        conn.execute("INSERT INTO search_index(rowid,terms) VALUES(?,?)",
                     (cid, f"第{i} 记忆 内容"))
    conn.execute("INSERT INTO anchors(id,bank,name) VALUES(1,'dafeiyu','大肥鱼')")
    conn.execute("INSERT INTO anchor_sources(anchor,chunk) VALUES(1,10)")
    conn.commit()
    conn.close()

    st = vault.memory_stats(db)
    check("统计 total=5", st["total"] == 5, str(st))
    check("统计 sessions=2", st["sessions"] == 2, str(st))
    check("统计 chunks=5", st["chunks"] == 5, str(st))
    check("统计 anchors=1", st["anchors"] == 1)

    sessions = vault.list_sessions(db)
    check("会话清单 2 个", len(sessions) == 2, str(len(sessions)))
    check("会话 A 有 3 条", any(s["count"] == 3 for s in sessions))

    page = vault.browse_messages(db, 0, 2)
    check("分页 limit 生效", len(page["items"]) == 2, str(len(page["items"])))
    check("分页 total 是总数", page["total"] == 5)
    check("倒序：第一条是 id=5", page["items"][0]["id"] == 5, str(page["items"][0]["id"]))

    page = vault.browse_messages(db, 0, 10, role="assistant")
    check("角色过滤生效", all(i["role"] == "assistant" for i in page["items"]))

    page = vault.browse_messages(db, 0, 10, query="第 3 条")
    check("内容过滤命中", len(page["items"]) == 1, str(len(page["items"])))

    page = vault.browse_messages(db, 0, 10, session="session-b@2")
    check("会话过滤命中 2 条", len(page["items"]) == 2, str(len(page["items"])))

    # ── 删单条：chunks / FTS / anchor_sources 必须一起清 ──
    ok, n, err = vault.delete_messages(db, [1])
    check("删 1 条成功", ok and n == 1, f"{ok} {n} {err}")

    conn = sqlite3.connect(db)
    try:
        left_msg = conn.execute("SELECT COUNT(*) FROM messages WHERE id=1").fetchone()[0]
        left_chunk = conn.execute("SELECT COUNT(*) FROM chunks WHERE message=1").fetchone()[0]
        left_fts = conn.execute(
            "SELECT COUNT(*) FROM search_index WHERE rowid=10").fetchone()[0]
        left_as = conn.execute(
            "SELECT COUNT(*) FROM anchor_sources WHERE chunk=10").fetchone()[0]
    finally:
        conn.close()
    check("消息已删", left_msg == 0)
    check("对应 chunk 已删", left_chunk == 0, str(left_chunk))
    check("FTS 索引已清（否则检索命中幽灵）", left_fts == 0, str(left_fts))
    check("anchor_sources 已清", left_as == 0, str(left_as))

    st = vault.memory_stats(db)
    check("删后 total=4", st["total"] == 4, str(st["total"]))
    check("删后 chunks=4", st["chunks"] == 4, str(st["chunks"]))

    # ── 删整会话 ──
    ok, n, err = vault.delete_session(db, "session-a@1")
    check("删会话成功", ok, err)
    check("会话 A 删掉 2 条（id 2、3）", n == 2, str(n))
    st = vault.memory_stats(db)
    check("删后 total=2", st["total"] == 2, str(st["total"]))
    check("删后 sessions=1", st["sessions"] == 1, str(st["sessions"]))

    # ── 导出 / 导入 ──
    backup = os.path.join(tmp, "backup.sqlite3")
    ok, err = vault.export_memory(db, backup)
    check("导出成功", ok, err)
    check("导出文件存在", os.path.isfile(backup))
    bst = vault.memory_stats(backup)
    check("导出内容一致", bst["total"] == 2, str(bst))

    ok, err = vault.vacuum(db)
    check("VACUUM 成功", ok, err)

    # 导入会先备份现有库
    ok, err = vault.import_memory(backup, db)
    check("导入成功", ok, err)
    import glob as _glob
    baks = _glob.glob(db + ".bak-*")
    check("导入前自动留了 .bak", len(baks) == 1, str(baks))


# ══════════════════════════════════════════════════════════════════════
def test_move(tmp: str, home: str) -> None:
    section("数据根搬家")

    os.environ["DSH_HOME"] = home
    src = os.path.join(tmp, "move-src")
    dst = os.path.join(tmp, "move-dst")
    os.makedirs(os.path.join(src, "ws-x"), exist_ok=True)
    with open(os.path.join(src, "ws-x", "soul-ws-x.json"), "w", encoding="utf-8") as f:
        json.dump({"identity": {"name": "搬家测试"}}, f)
    with open(os.path.join(src, "scope.json"), "w", encoding="utf-8") as f:
        json.dump({"mode": "workspace", "workspaceId": "x"}, f)

    vault.write_data_root(src)

    plan = vault.inspect_move(src)
    check("目标=当前 → 报错", not plan.ok and "相同" in plan.error, plan.error)

    plan = vault.inspect_move(dst)
    check("盘点成功", plan.ok, plan.error)
    check("盘点看到 ws-x", any(i["name"] == "ws-x" for i in plan.items), str(plan.items))
    check("盘点看到 scope.json",
          any(i["name"] == "scope.json" for i in plan.items))
    check("盘点统计了字节", plan.total_bytes > 0, str(plan.total_bytes))
    check("盘点没有冲突", plan.conflicts == [], str(plan.conflicts))

    # 目标里已有同名 → 应报冲突
    os.makedirs(dst, exist_ok=True)
    with open(os.path.join(dst, "scope.json"), "w") as f:
        f.write("{}")
    plan = vault.inspect_move(dst)
    check("检测到冲突", plan.conflicts == ["scope.json"], str(plan.conflicts))

    ok, err = vault.apply_move(dst)
    check("搬家成功", ok, err)
    check("root.json 指向新目录", vault.read_data_root() == dst, vault.read_data_root())
    check("新目录里有灵魂",
          os.path.isfile(os.path.join(dst, "ws-x", "soul-ws-x.json")))
    check("源目录被改名保留（没删）",
          any(".old-" in n for n in os.listdir(tmp)), str(os.listdir(tmp)))

    vault.write_data_root("")
    check("恢复默认 → 读到空串", vault.read_data_root() == "")


# ══════════════════════════════════════════════════════════════════════
def test_list_scopes(tmp: str, home: str) -> None:
    section("范围列表")

    os.environ["DSH_HOME"] = home
    root = os.path.join(tmp, "listroot")
    os.makedirs(os.path.join(root, "ws-one"), exist_ok=True)
    os.makedirs(os.path.join(root, "ws-two"), exist_ok=True)
    with open(os.path.join(root, "ws-one", "soul-ws-one.json"), "w",
              encoding="utf-8") as f:
        json.dump({"identity": {"name": "一号"}}, f)
    with open(os.path.join(root, "ws-two", "mood-ws-two.json"), "w") as f:
        f.write("{}")

    rows = vault.list_scopes(root)
    check("列出 2 个范围", len(rows) == 2, str(len(rows)))
    one = next(r for r in rows if r["key"] == "ws-one")
    two = next(r for r in rows if r["key"] == "ws-two")
    check("ws-one 有灵魂", one["has_soul"] is True)
    check("ws-one 无记忆", one["has_memory"] is False)
    check("ws-one 名字读到了", one["name"] == "一号", one["name"])
    check("ws-two 有心情", two["has_mood"] is True)
    check("ws-two 无灵魂", two["has_soul"] is False)


# ══════════════════════════════════════════════════════════════════════
def main() -> int:
    global FAIL

    tmp = _mk_tmp("vault-")
    home = os.path.join(tmp, "dshhome")
    os.makedirs(home, exist_ok=True)
    old_home = os.environ.get("DSH_HOME")
    os.environ["DSH_HOME"] = home

    print("=" * 66)
    print("大肥鱼 · 可写层自检  vault.py")
    print(f"临时目录: {tmp}")
    print("=" * 66)

    tests = [
        ("范围键", lambda: test_scope_key()),
        ("灵魂归一化", lambda: test_soul_normalize()),
        ("性格文案", lambda: test_trait_sentence()),
        ("灵魂读写", lambda: test_soul_io(tmp)),
        ("scope.json", lambda: test_scope_io(tmp)),
        ("范围推导", lambda: test_infer_scope(tmp)),
        ("心情配置", lambda: test_mood_config(tmp, home)),
        ("心情重置", lambda: test_mood_reset(tmp, home)),
        ("记忆库", lambda: test_memory(tmp)),
        ("搬家", lambda: test_move(tmp, home)),
        ("范围列表", lambda: test_list_scopes(tmp, home)),
    ]

    for name, fn in tests:
        try:
            fn()
        except Exception:
            FAIL += 1
            FAILED.append(f"{name}（异常）")
            print(f"  ✗ {name} 抛异常：")
            traceback.print_exc()

    print()
    print("=" * 66)
    total = PASS + FAIL
    print(f"结果：{PASS}/{total} 通过" + ("" if not FAIL else f"，{FAIL} 项失败"))
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print("  ·", f)
    print("=" * 66)

    # 清场
    if old_home is None:
        os.environ.pop("DSH_HOME", None)
    else:
        os.environ["DSH_HOME"] = old_home
    shutil.rmtree(tmp, ignore_errors=True)

    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
