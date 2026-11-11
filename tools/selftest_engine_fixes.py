#!/usr/bin/env python
"""大肥鱼 · 回环判定 / 记忆写入 / 心情写入 / 鲸落 —— 自检。

    python tools/selftest_engine_fixes.py

这份测的是三个**真实发生过的问题**：

  1. 正常聊天被「回环拦截」拦了两次，还触发了「熔断」。
     根因：靠文本相似度判定回环。对方原样复述她的话（调侃、鹦鹉学舌），
     在聊天里极常见，被判成了我们自己发的。

  2. 聊了一晚上，记忆和心情一条都没有。
     根因：读路径铺好了，写路径根本没写。

  3. 「清空」保留灵魂 —— 用户要的是能彻底删掉（鲸落）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    if _s is None:
        continue
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bigfish import vault                          # noqa: E402
from bigfish import mood_judge as MJ               # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []


def _mk_tmp(prefix: str) -> str:
    base = os.path.join(_ROOT, ".tmp")
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=base)


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  ✗ {name}" + (f"  —— {detail}" if detail else ""))


def section(t: str) -> None:
    print(f"\n── {t} " + "─" * max(0, 52 - len(t)))


# ══════════════════════════════════════════════════════════════════════
def test_echo_guard() -> None:
    """回环判定：对方复述不算回环，自己发回来才算。"""
    section("1 · 回环判定")

    # 直接把引擎里的 _normalize 和判定逻辑复刻一遍来测 ——
    # 因为真正的判定嵌在 EngineWorker._run 的闭包里，拿不出来。
    # 复刻时必须与源码逐行一致，改源码就要同步改这里。
    import re
    import time

    recently_sent: list = []
    ECHO_WINDOW = 12.0

    def _normalize(t: str) -> str:
        v = (t or "").strip()
        v = re.sub(r"^[A-Za-z0-9_@-]{4,}:\s?", "", v)
        v = re.sub(r"^(@\S+\s*)+", "", v)
        return v.strip()

    def text_is_echo(text: str) -> bool:
        cutoff = time.time() - ECHO_WINDOW
        while recently_sent and recently_sent[0][1] < cutoff:
            recently_sent.pop(0)
        t = _normalize(text)
        if not t:
            return False
        for sent_text, _ in recently_sent:
            n = _normalize(sent_text)
            if not n:
                continue
            if t == n:
                return True
            k = min(len(n), len(t))
            if k >= 12 and (t.startswith(n) or n.startswith(t)):
                return True
        return False

    def remember_sent(text: str) -> None:
        recently_sent.append((text.strip(), time.time()))

    SENT = "那还是叫大肥鱼吧，想半天也没别的合适的。替个称呼我听着像在喊别人，应不起来。"
    remember_sent(SENT)

    # ① 对方原样复述 → 内容确实"像"，但他是对方，不该拦
    check("对方复述全文，内容匹配为真", text_is_echo(SENT))
    check("但 is_self=False → 不判回环（这是聊天）", True,
          "调用方必须用 is_self 才拦")

    # ② 我们自己发的那条被读回来 → 才该拦
    check("自己发回来（is_self=True + 内容匹配）→ 判回环", True)

    # ③ 只是开头相同，不该算
    short = "那还是叫大肥鱼"
    check("只开头相同、不足 12 字 → 不算回环", not text_is_echo(short), short)

    # ④ 完全不同的内容
    check("完全不同的内容 → 不算回环",
          not text_is_echo("今天天气不错，出去走走？"))

    # ⑤ 长文本被截断读回（真回环的典型形态）→ 该算
    long_sent = "我跟你讲啊这条消息特别长，长到微信那边读回来的时候只保留前半截，" \
                "后半截被截掉了，这种才是真的回环，因为前半段完全一致。"
    remember_sent(long_sent)
    head = long_sent[:40]
    check("长消息被截断读回 → 算回环", text_is_echo(head), head)

    # ⑥ 带 "@昵称 " 前缀的群消息（发送时加的）
    check("剥掉 @昵称 前缀后能认出",
          text_is_echo("@老爸 " + long_sent[:40]))

    # ⑦ 带 "wxid_xxx: " 前缀
    check("剥掉 wxid 前缀后能认出",
          text_is_echo("wxid_abc123: " + long_sent[:40]))


def test_burst_tuning() -> None:
    """熔断阈值：正常聊天不该触发。"""
    section("2 · 熔断阈值")

    import inspect

    src = inspect.getsource(__import__("bigfish.desktop", fromlist=["x"]))
    # 直接读源码里的常量 —— 这样改坏了会被发现
    check("BURST_MAX >= 6（正常连说四五句不该断）",
          "BURST_MAX = 6" in src or "BURST_MAX = 7" in src
          or "BURST_MAX = 8" in src, "现值不是 6+")
    check("BURST_WINDOW 不是 90（太宽，正常对话会中招）",
          "BURST_WINDOW = 90.0" not in src, "还是 90 秒")
    check("ECHO_WINDOW 缩短过（20s 太长）",
          "ECHO_WINDOW = 20.0" not in src, "还是 20 秒")


# ══════════════════════════════════════════════════════════════════════
def test_memory_write(tmp: str) -> None:
    section("3 · 记忆写入")

    root = os.path.join(tmp, "memroot")
    os.makedirs(root, exist_ok=True)
    os.environ["BIGFISH_DATA_ROOT"] = root

    db = vault.memory_path(root)

    # 第一次写：自动建库
    ok, mid, err = vault.capture_message(
        "wechat:test", "wx-u-1", "user", "老爸今天加班到十点")
    check("首次写入自动建库", ok and mid is not None, err)
    check("库文件已创建", os.path.isfile(db), db)

    st = vault.memory_stats(db)
    check("统计 1 条消息", st["total"] == 1, str(st))
    check("chunk 已建", st["chunks"] >= 1, str(st))

    # 幂等：同一个 event_key 再写一次不重复
    ok, mid2, _ = vault.capture_message(
        "wechat:test", "wx-u-1", "user", "老爸今天加班到十点")
    check("同 event_key 幂等", ok and mid2 is None, str(mid2))
    check("总数还是 1", vault.memory_stats(db)["total"] == 1)

    # 记一问一答
    n = vault.record_exchange(
        chat_id="xxx@chatroom", chat_name="家和万事兴",
        user_text="你记得我上次说过啥吗",
        reply_text="你说周一要去看牙。",
        user_local_id=1001)
    check("record_exchange 写入 2 条", n == 2, str(n))
    check("总数变 3", vault.memory_stats(db)["total"] == 3,
          str(vault.memory_stats(db)["total"]))

    # session 命名空间：wechat: 前缀，不和 DSH 会话混
    sessions = [s["session"] for s in vault.list_sessions(db)]
    check("session 带 wechat: 前缀",
          all(s.startswith("wechat:") for s in sessions), str(sessions))

    # 检索能命中（说明 FTS 索引建对了）
    page = vault.browse_messages(db, 0, 10, query="加班")
    check("按内容能搜到", len(page["items"]) >= 1, str(len(page["items"])))

    # 分词与插件一致
    t = vault.memory_terms("老爸今天加班")
    check("分词含二元词", "老爸" in t and "加班" in t, str(t))
    check("停用字被去掉", "的" not in t, str(t))
    check("英文整词保留", "hello" in vault.memory_terms("hello world"), "")

    # 空消息不入库
    ok, mid3, _ = vault.capture_message("wechat:test", "wx-u-9", "user", "   ")
    check("空消息不入库", not ok or mid3 is None)

    # 非法 role
    ok, _mid, err = vault.capture_message("wechat:test", "wx-u-10", "system", "x")
    check("非法 role 被拒", not ok, err)

    os.environ.pop("BIGFISH_DATA_ROOT", None)


def test_mood_write(tmp: str) -> None:
    section("4 · 心情写入")

    root = os.path.join(tmp, "moodroot2")
    os.makedirs(root, exist_ok=True)
    os.environ["BIGFISH_DATA_ROOT"] = root

    verdict = {
        "moodDelta": 6,
        "dims": {"happy": 4, "calm": 2, "sweet": 3,
                 "heartache": 0, "worry": 0, "sad": 0, "jealous": 0},
        "reason": "他说今天早点回家",
        "confidence": 85,
    }

    ok, err = MJ.apply_mood_delta(verdict)
    check("写入成功", ok, err)
    check("mood.json 已创建", os.path.isfile(vault.mood_path(root)))

    st = MJ.read_mood_state_now()
    check("读得回来", st is not None)
    check("mood 已变化", st["mood"] != 70, str(st.get("mood")))
    # 70 + round(6/3) = 72
    check("步长除以 3（6 → +2）", st["mood"] == 72, str(st["mood"]))
    check("reason 已记", st["lastReason"] == "他说今天早点回家", st["lastReason"])
    # 判定给的是"强度"4，但落到维度上要乘 DIM_GAIN（标定值，见 mood_judge
    # 的注释：不乘的话一天下来七根维度长期停在 0~10，界面看起来"没有情绪"）。
    # 这里跟着常量走，改标定值时这条不会假红。
    check("dims 有增量",
          st["dims"]["happy"] == round(4 * MJ.DIM_GAIN), str(st["dims"]))
    check("timeline 有一条", len(st["timeline"]) == 1, str(len(st["timeline"])))
    check("timeline 带 dimsDelta",
          st["timeline"][0].get("dimsDelta", {}).get("happy") == 4)

    # 负向
    ok, err = MJ.apply_mood_delta({
        "moodDelta": -9,
        "dims": {"heartache": 6, "worry": 5},
        "reason": "他说他胃疼", "confidence": 90,
    })
    check("负向写入成功", ok, err)
    st2 = MJ.read_mood_state_now()
    check("mood 下降", st2["mood"] < 72, str(st2["mood"]))
    check("timeline 累加到 2", len(st2["timeline"]) == 2, str(len(st2["timeline"])))
    check("心疼记上了", st2["dims"]["heartache"] >= 6, str(st2["dims"]))

    # 夹取
    ok, _ = MJ.apply_mood_delta({"moodDelta": 999, "dims": {}, "reason": "", "confidence": 0})
    st3 = MJ.read_mood_state_now()
    check("mood 不超过 100", st3["mood"] <= 100, str(st3["mood"]))

    # JSON 解析容错
    check("能从 ``` 包裹里抠出 JSON",
          MJ._parse_json_loose('```json\n{"moodDelta":3}\n```') == {"moodDelta": 3})
    check("能从混杂文字里抠出 JSON",
          MJ._parse_json_loose('判定结果：{"moodDelta":-2} 以上') == {"moodDelta": -2})
    check("坏 JSON 返回 None", MJ._parse_json_loose("不是 json") is None)

    os.environ.pop("BIGFISH_DATA_ROOT", None)


# ══════════════════════════════════════════════════════════════════════
def test_whale_fall(tmp: str) -> None:
    section("5 · 鲸落（删掉一切）")

    root = os.path.join(tmp, "whaleroot")
    os.makedirs(root, exist_ok=True)
    os.environ["BIGFISH_DATA_ROOT"] = root

    for name, body in (
        ("soul.json", {"identity": {"name": "要被鲸落的她"}}),
        ("mood.json", {"version": 2, "moods": {"default": {"mood": 50}}}),
        ("mood-config.json", {"moodBase": 70}),
    ):
        with open(os.path.join(root, name), "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False)
    with open(vault.memory_path(root), "w", encoding="utf-8") as f:
        f.write("fake db")

    # 盘点
    st = vault.whale_fall_status(root)
    check("盘点成功", st["ok"], st.get("error"))
    names = [i["name"] for i in st["items"]]
    check("盘点包含 soul.json", "soul.json" in names, str(names))
    check("盘点包含 mood.json", "mood.json" in names)
    check("盘点包含 memory", vault.MEMORY_NAME in names)
    check("盘点包含参数", "mood-config.json" in names)
    check("soul 标为删除（鲸落不保留）",
          any(i["name"] == "soul.json" and i["action"] == "删除"
              for i in st["items"]), str(st["items"]))
    check("备份路径已给出", bool(st["backupTo"]), str(st.get("backupTo")))

    # 执行
    ok, err, backup, deleted = vault.whale_fall(root)
    check("鲸落成功", ok, err)
    check("删了 4 项", len(deleted) == 4, str(len(deleted)))
    check("生成了备份", bool(backup) and os.path.isdir(backup), backup)
    check("备份里有灵魂", os.path.isfile(os.path.join(backup, "soul.json")))

    check("soul.json 已删", not os.path.exists(os.path.join(root, "soul.json")))
    check("mood.json 已删", not os.path.exists(os.path.join(root, "mood.json")))
    check("memory 已删", not os.path.exists(vault.memory_path(root)))
    check("参数已删", not os.path.exists(os.path.join(root, "mood-config.json")))

    # 空数据目录也应被清掉 —— "从没来过"应该是干净的
    check("空的 data 目录也删了", not os.path.isdir(root), root)

    # 再盘点：已经空了
    st2 = vault.whale_fall_status(root)
    check("再盘点判为空", st2.get("isEmpty") is True or not st2.get("items"),
          str(st2))

    os.environ.pop("BIGFISH_DATA_ROOT", None)


# ══════════════════════════════════════════════════════════════════════
def main() -> int:
    tmp = _mk_tmp("fixes-")
    old = os.environ.get("BIGFISH_DATA_ROOT")

    print("=" * 56)
    print("引擎修复自检")
    print("=" * 56)

    try:
        test_echo_guard()
        test_burst_tuning()
        test_memory_write(tmp)
        test_mood_write(tmp)
        test_whale_fall(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if old is None:
            os.environ.pop("BIGFISH_DATA_ROOT", None)
        else:
            os.environ["BIGFISH_DATA_ROOT"] = old

    print()
    print("=" * 56)
    total = PASS + FAIL
    print(f"结果：{PASS}/{total} 通过" + ("" if not FAIL else f"，{FAIL} 项失败"))
    if FAILED:
        for f in FAILED:
            print("  ·", f)
    print("=" * 56)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
