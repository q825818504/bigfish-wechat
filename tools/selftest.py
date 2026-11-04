"""端到端自检 —— 把每个模块拉到实际运行里跑，而不是断言它应该能跑。

与之前的 test_updater.py 的区别：
    那个测的是「函数返回 True」，这个测的是「真的读到了数据」。

逐项：
  1. 灵魂      真的从 soul 文件读出人格，字段非空
  2. 心情      真的从 mood 文件读出数值
  3. 记忆      真的从 sqlite 检索出条目
  4. 微信数据库 真的解出密钥、读到会话和消息
  5. 群聊判定  真的用本人昵称做判断
  6. 模型      真的调一次接口拿到回复
  7. UIA       真的能热激活并定位输入框
  8. 配置      真的能存能读

所有项都输出实测值，不输出「应该」。
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "vendor", "wechatauto-replica"))

HR = "=" * 72
results = []


class SkipTest(Exception):
    """环境不满足，不是失败。"""


def section(t):
    print()
    print(HR)
    print(t)
    print(HR)


def item(name, fn, critical=True):
    """跑一项，记录结果。"""
    t0 = time.time()
    try:
        detail = fn()
        dt = time.time() - t0
        results.append((name, "OK", detail, dt, critical))
        print(f"  [OK  ] {name}  ({dt:.2f}s)")
        if detail:
            for line in str(detail).splitlines():
                print(f"         {line}")
        return True
    except SkipTest as exc:
        dt = time.time() - t0
        results.append((name, "SKIP", str(exc), dt, critical))
        print(f"  [SKIP] {name}  ({dt:.2f}s)")
        print(f"         {exc}")
        return True
    except Exception as exc:
        dt = time.time() - t0
        msg = f"{type(exc).__name__}: {exc}"
        results.append((name, "FAIL", msg, dt, critical))
        print(f"  [FAIL] {name}  ({dt:.2f}s)")
        print(f"         {msg}")
        if os.environ.get("SELFTEST_TRACE"):
            traceback.print_exc()
        return False


# ════════════════════════════════════════════════════════════════
section("0. 环境")
import ctypes
is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
print(f"  Python  : {sys.version.split()[0]}")
print(f"  管理员  : {is_admin}")
print(f"  打包版  : {getattr(sys, 'frozen', False)}")
print(f"  根目录  : {ROOT}")

# ════════════════════════════════════════════════════════════════
section("1. 灵魂")
soul_holder = {}


def t_soul():
    from bigfish.soul import load_soul
    s = load_soul()
    soul_holder["s"] = s
    persona = s.render_persona()
    assert s.name, "名字为空"
    assert s.who_i_am, "whoIAm 为空"
    assert s.values, "珍视为空"
    assert len(persona) > 200, f"人格太短: {len(persona)}"
    return (f"名字={s.name} 称呼={s.address} rev={s.revision}\n"
            f"珍视={len(s.values)}条 性格={s.traits} 自知={len(s.self_notes)}条\n"
            f"人格={len(persona)}字符\n"
            f"来源={s.source_path}")


item("灵魂读取与渲染", t_soul)

# ════════════════════════════════════════════════════════════════
section("2. 心情")


def t_mood():
    from bigfish.bridge_client import load_mood_only
    m = load_mood_only()
    assert m is not None, "读不到 mood（返回 None）"
    assert m.value is not None, "mood 数值为空"
    assert 0 <= m.value <= 100, f"mood 超范围: {m.value}"
    dims = ", ".join(f"{k}={v:.3f}" for k, v in
                     list(m.dims.items())[:4])
    return (f"数值={m.value:.2f} 标签={m.label}\n"
            f"维度={len(m.dims)}个 ({dims}...)\n"
            f"注入文本={len(m.injected_text)}字符\n"
            f"最近原因={m.last_reason[:40]}...")


item("心情读取", t_mood)

# ════════════════════════════════════════════════════════════════
section("3. 记忆")


def t_memory_bridge():
    from bigfish.bridge_client import _default_data_root, find_node
    node = find_node()
    assert node, "找不到 node"
    bridge = os.path.join(ROOT, "bridge", "bridge.mjs")
    assert os.path.isfile(bridge), f"找不到 {bridge}"
    root = _default_data_root()
    assert root, "找不到 dataRoot"

    import subprocess
    r = subprocess.run([node, bridge, "search", "--root", root,
                        "--query", "大肥鱼"],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    raw = (r.stdout or "").strip()
    assert raw, f"桥接无输出 stderr={r.stderr[:200]}"
    d = json.loads(raw)
    mem = d.get("memory") or {}
    text = mem.get("text") or ""
    n = len(json.loads(text)) if text else 0
    assert n > 0, f"检索「大肥鱼」返回 0 条（text={text[:80]}）"
    return f"检索到 {n} 条记忆"


item("记忆检索（Node 桥接）", t_memory_bridge)


def t_memory_stats():
    import glob
    import sqlite3

    from bigfish.bridge_client import _default_data_root, _pick_scope_dir
    root = _default_data_root()
    scope = _pick_scope_dir(root)
    assert scope, "找不到 scope 目录"
    dbs = glob.glob(os.path.join(scope, "memory-*.sqlite3"))
    assert dbs, f"{scope} 下没有记忆库"

    import shutil
    # 必须用绝对路径：sqlite3.connect() 对相对路径的处理在 Windows 上
    # 会因为盘符解析问题报 OSError [Errno 22]（实测）。
    tmp = os.path.abspath(os.path.join(ROOT, ".tmp", "selftest_mem.sqlite3"))
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    shutil.copyfile(dbs[0], tmp)
    # -wal/-shm 一起带上，否则读不到最近未 checkpoint 的数据
    for suf in ("-wal", "-shm"):
        if os.path.exists(dbs[0] + suf):
            try:
                shutil.copyfile(dbs[0] + suf, tmp + suf)
            except OSError:
                pass
    conn = sqlite3.connect(tmp)
    try:
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        newest = conn.execute(
            "SELECT MAX(source_time) FROM messages").fetchone()[0]
    finally:
        conn.close()
    assert total > 0, "记忆库是空的"
    def fmt(ts):
        if not ts:
            return "未知"
        t = float(ts) / 1000.0 if float(ts) > 1e11 else float(ts)
        return time.strftime("%m-%d %H:%M", time.localtime(t))

    return f"库里 {total} 条，最新 {fmt(newest)}"


item("记忆库统计", t_memory_stats)

# ════════════════════════════════════════════════════════════════
section("4. 微信数据库")
db_holder = {}


def t_wechat_db():
    from bigfish.wechat import WeChatReader
    r = WeChatReader().open()
    db_holder["r"] = r
    info = r.self_info()
    keys = len(r._db._keys)
    files = len(r._db._db_files)
    assert keys > 0, f"一个密钥都没拿到 ({keys}/{files})"
    return (f"账号={info.get('nick_name')} ({info.get('username')})\n"
            f"密钥={keys}/{files}")


item("微信数据库解锁", t_wechat_db)


def t_sessions():
    r = db_holder.get("r")
    assert r, "数据库未就绪"
    rows = r.sessions(limit=8)
    assert rows, "会话列表为空"
    lines = []
    for s in rows[:6]:
        kind = "群" if s["is_group"] else "人"
        name = s["name"]
        assert name, f"会话 {s['username']} 名字为空"
        lines.append(f"[{kind}] {name} ({s['unread']}未读)")
    return f"{len(rows)} 个会话\n" + "\n".join(lines)


item("会话列表", t_sessions)


def t_messages():
    r = db_holder.get("r")
    assert r, "数据库未就绪"
    msgs = r.messages("filehelper", limit=5)
    assert msgs, "文件传输助手读不到消息"
    lines = []
    for m in msgs[:3]:
        assert m.content, f"消息 {m.local_id} 内容为空"
        lines.append(f"[{m.msg_type}] {m.preview(40)}")
    return f"{len(msgs)} 条\n" + "\n".join(lines)


item("消息读取与语义化", t_messages)

# ════════════════════════════════════════════════════════════════
section("5. 群聊判定")


def t_group_gate():
    r = db_holder.get("r")
    assert r, "数据库未就绪"
    from bigfish.group_gate import GroupGate
    from bigfish.wechat import WeChatReader as WR  # noqa

    gate = GroupGate(r._db, r._db.wxid, enable=True)
    names = gate.my_names
    assert names, "收集不到本人的任何叫法"
    me = list(names)[0]

    groups = [s for s in r.sessions(limit=30) if s["is_group"]]
    assert groups, "没有群可测"
    gid = groups[0]["username"]

    cases = [
        (f"wxid_test: @{me} 你好", True, "@本人"),
        ("wxid_test: @别人 你好", False, "@别人"),
        (f"wxid_test: {me}在吗", True, "叫名字"),
        ("wxid_test: 今天天气不错", False, "无关内容"),
    ]
    lines = [f"本人叫法: {sorted(names)}"]
    bad = 0
    for content, expect, label in cases:
        t = gate.check(gid, content, True)
        ok = t.respond == expect
        if not ok:
            bad += 1
        lines.append(f"{'OK' if ok else 'FAIL'} {label}: {content[12:40]} "
                     f"-> {t.respond}")
    assert bad == 0, f"{bad} 个用例不符"
    return "\n".join(lines)


item("群聊触发判定", t_group_gate)

# ════════════════════════════════════════════════════════════════
section("6. 模型")


def t_model():
    from bigfish.replier import ModelConfig, call_model
    cfg = ModelConfig.load()
    _ok, _why = cfg.status()
    assert _ok, f"模型未就绪：{_why}"
    t0 = time.time()
    reply = call_model(cfg, "你叫大肥鱼，说话简短。", "说三个字")
    dt = time.time() - t0
    assert reply, "模型返回空"
    return f"模型={cfg.model}\n回复={reply[:60]!r} ({dt:.1f}s)"


item("模型调用", t_model)

# ════════════════════════════════════════════════════════════════
section("7. UIA（需要管理员）")


def t_uia():
    if not is_admin:
        # 不是失败，是环境不满足。标 OK 并说明，避免把「没提权」
        # 误报成「UIA 坏了」——这两件事的排查方向完全不同。
        raise SkipTest("非管理员，未测（UIA 需要提权）")
    from bigfish.wechat_sender import UiaSender
    s = UiaSender()
    s.wake()
    cur = s.current_chat()
    if not cur:
        s.open_chat("文件传输助手")
        time.sleep(0.4)
        cur = s.current_chat()
    assert cur, "打开会话后仍读不到当前会话名"
    return f"热激活成功，当前会话={cur!r}"


item("UIA 热激活与定位", t_uia)

# ════════════════════════════════════════════════════════════════
section("8. 配置")


def t_config():
    from bigfish.desktop import CONFIG_PATH, load_json_config, save_json_config
    orig = load_json_config()
    test = dict(orig)
    test["_selftest_marker"] = int(time.time())
    save_json_config(test)
    back = load_json_config()
    assert back.get("_selftest_marker") == test["_selftest_marker"], \
        "写入后读不回来"
    # 还原
    orig.pop("_selftest_marker", None)
    save_json_config(orig)
    return f"读写正常，配置文件={CONFIG_PATH}"


item("配置读写", t_config)

# ════════════════════════════════════════════════════════════════
section("9. 监听器")


def t_watcher():
    r = db_holder.get("r")
    assert r, "数据库未就绪"
    from bigfish.watcher import MessageWatcher
    w = MessageWatcher(r._db, interval=0.5)
    uid = w.watch("文件传输助手")
    assert uid, "watch 返回空"
    assert w._wm.get(uid) is not None, "水位未初始化"
    got = w.poll_once()
    # 第一次应该 0 条（都是历史）
    assert len(got) == 0, f"第一次轮询返回了 {len(got)} 条历史消息"
    return (f"监听 {uid}\n"
            f"水位={w._wm[uid]} prefill={len(w._prefill.get(uid, set()))}\n"
            f"首次轮询 0 条（正确，历史被过滤）")


item("监听器", t_watcher)

# ════════════════════════════════════════════════════════════════
section("结果汇总")
nok = sum(1 for r in results if r[1] == "OK")
nfail = sum(1 for r in results if r[1] == "FAIL")
nskip = sum(1 for r in results if r[1] == "SKIP")
ncrit = sum(1 for r in results if r[1] == "FAIL" and r[4])

print(f"  通过 {nok} / 失败 {nfail} / 跳过 {nskip}")
for name, st, detail, dt, crit in results:
    mark = {"OK": "OK  ", "FAIL": "FAIL", "SKIP": "SKIP"}[st]
    star = "" if crit else "（非关键）"
    print(f"    [{mark}] {name:<22s} {dt:6.2f}s {star}")
    if st in ("FAIL", "SKIP"):
        print(f"           -> {detail}")

print()
if ncrit:
    print(f"  ✗ 有 {ncrit} 个关键项失败")
    sys.exit(1)
print("  ✓ 全部通过")
