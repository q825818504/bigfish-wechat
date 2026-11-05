"""配置作用域回归测试 —— C-1 的护栏。

跑法：
    python tools/selftest_scopes.py

不需要微信、不需要桌面、不需要网络、不写任何文件。
全部在内存 dict 上跑，配置快照用 `copy.deepcopy` 比对。

## 为什么有这份测试

自环事故的第一个根因是 `allow_self` 只能全局。
C-1 把它拆成三层继承，但**继承本身就是新的风险**：

  · 三层回退算错一层 → 某个会话静默用上了全局的 `allow_self=true`
  · `[]` 被 `or` 吞掉 → "我在这个会话关掉免打扰"变成"继承全局的免打扰"
  · `0` 被 `or` 吞掉 → 同上
  · 昵称键解析失败 → 那条配置完全不生效，且不报错（D-61 的形状）

这一份就是为了让这四件事回不来。

覆盖（对应 .planning/specs/C1-scopes.md 第八节）：
   1  三层回退：chat → account → global → default
   2  三态：false 覆盖 true；null == 缺失；缺失继承
   3  空列表：[] 是显式清空，不被 or 吞掉
   4  零值：0 是显式值，不被 or 吞掉
   5  昵称别名：能命中；解析不到时不命中且不抛异常
   6  id 优先：同一会话昵称键与 id 键并存时 id 赢
   7  旧路径兼容：只有 auto_reply.* 时也能读到
   8  新路径优先：新旧冲突时 scopes.global 赢
   9  只读：resolve / explain / diagnose 不修改传入的 config
  10  explain：来源层标注与实际回退结果一致
  ＋  类型错误不静默、写入拒绝昵称键、set/clear 三态、层名常量一致
"""
from __future__ import annotations

import copy
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import scopes  # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []


def _pick_marks() -> tuple[str, str]:
    try:
        "\u2713".encode(sys.stdout.encoding or "utf-8")
        return "\u2713", "\u2717"
    except (UnicodeEncodeError, LookupError):
        return "[OK]", "[X]"


MARK_OK, MARK_FAIL = _pick_marks()


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  {MARK_OK} {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  {MARK_FAIL} {name}" + (f"  <- {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n[{title}]")


# ══════════════════════════════════════════════════════════════════
#  夹具
# ══════════════════════════════════════════════════════════════════

BOT = "wxid_51f85n6xp73o22_0cb8"
PEER = "wxid_abc123"
GROUP = "456789@chatroom"


def cfg(**over) -> dict:
    """一份三层都写满的配置。"""
    base = {
        "scopes": {
            "global": {
                "allow_self": True,
                "group_reply": True,
                "max_reply_chars": 300,
                "quiet_hours": ["23:00-07:00"],
            },
            "account": {
                BOT: {"max_reply_chars": 200},
            },
            "chat": {
                PEER: {"allow_self": False},
            },
        },
    }
    base.update(over)
    return base


def resolved(c, **kw):
    return dict(scopes.resolve(c, **kw))


# ══════════════════════════════════════════════════════════════════
#  1 三层回退
# ══════════════════════════════════════════════════════════════════

def test_fallback() -> None:
    section("1 三层回退")
    c = cfg()
    r = resolved(c, bot_id=BOT, chat_id=PEER)

    check("会话层有值 → 用会话层",
          r["allow_self"] == (False, scopes.LAYER_CHAT), str(r.get("allow_self")))
    check("会话层没有 → 落账号层",
          r["max_reply_chars"] == (200, scopes.LAYER_ACCOUNT), str(r.get("max_reply_chars")))
    check("账号层没有 → 落全局层",
          r["group_reply"] == (True, scopes.LAYER_GLOBAL), str(r.get("group_reply")))
    check("三层都没有 → 代码默认值",
          r["group_allow_all"] == ((), scopes.LAYER_DEFAULT), str(r.get("group_allow_all")))

    # 换一个会话：会话层没有它的块，只该到账号层为止
    r2 = resolved(c, bot_id=BOT, chat_id="wxid_other")
    check("别的会话不串用本会话的覆盖",
          r2["allow_self"] == (True, scopes.LAYER_GLOBAL), str(r2.get("allow_self")))

    # 不给账号：账号层整体跳过
    r3 = resolved(c, chat_id=PEER)
    check("没给 bot_id 时跳过账号层",
          r3["max_reply_chars"] == (300, scopes.LAYER_GLOBAL), str(r3.get("max_reply_chars")))

    # 空配置：全部默认
    r4 = resolved({}, bot_id=BOT, chat_id=PEER)
    check("空配置全是默认值",
          all(layer is scopes.LAYER_DEFAULT for _v, layer in r4.values()),
          str(r4))
    check("空配置 allow_self 默认关",
          r4["allow_self"][0] is False, str(r4["allow_self"]))


# ══════════════════════════════════════════════════════════════════
#  2 三态
# ══════════════════════════════════════════════════════════════════

def test_three_state() -> None:
    section("2 三态：false / null / 缺失")
    c = cfg()
    r = resolved(c, bot_id=BOT, chat_id=PEER)

    check("global=true 被 chat=false 覆盖（事故开关能关掉了）",
          r["allow_self"] == (False, scopes.LAYER_CHAT), str(r["allow_self"]))

    # null == 缺失 == 继承
    c2 = cfg()
    c2["scopes"]["chat"][PEER] = {"allow_self": None, "max_reply_chars": None}
    r2 = resolved(c2, bot_id=BOT, chat_id=PEER)
    check("null 等同缺失（allow_self 落回 global）",
          r2["allow_self"] == (True, scopes.LAYER_GLOBAL), str(r2["allow_self"]))
    check("null 等同缺失（max_reply_chars 落回 account）",
          r2["max_reply_chars"] == (200, scopes.LAYER_ACCOUNT), str(r2["max_reply_chars"]))

    # false 不能被当成"没写"
    c3 = cfg()
    c3["scopes"]["chat"][PEER] = {"group_reply": False}
    r3 = resolved(c3, bot_id=BOT, chat_id=PEER)
    check("false 是显式关闭，不是缺失",
          r3["group_reply"] == (False, scopes.LAYER_CHAT), str(r3["group_reply"]))

    # "显式写 true" 和 "没写" 都能得到 True，但层不同
    c4 = cfg()
    c4["scopes"]["chat"][PEER] = {"group_reply": True}
    r4 = resolved(c4, bot_id=BOT, chat_id=PEER)
    check("显式 true 与继承 true 值相同、层不同",
          r4["group_reply"] == (True, scopes.LAYER_CHAT), str(r4["group_reply"]))


# ══════════════════════════════════════════════════════════════════
#  3 / 4 空列表与零值
# ══════════════════════════════════════════════════════════════════

def test_falsy_not_swallowed() -> None:
    section("3 空列表：[] 是显式清空")
    c = cfg()
    c["scopes"]["chat"][PEER] = {"quiet_hours": []}
    r = resolved(c, bot_id=BOT, chat_id=PEER)
    check("[] 覆盖 global 的非空列表（不被 or 吞掉）",
          r["quiet_hours"] == ((), scopes.LAYER_CHAT), str(r["quiet_hours"]))
    check("此时 allow_self 仍按自己的层走",
          r["allow_self"][1] is scopes.LAYER_GLOBAL, str(r["allow_self"]))

    c2 = cfg()
    c2["scopes"]["chat"][PEER] = {"group_allow_all": []}
    r2 = resolved(c2, bot_id=BOT, chat_id=PEER)
    check("group_allow_all 的 [] 也是显式清空",
          r2["group_allow_all"] == ((), scopes.LAYER_CHAT), str(r2["group_allow_all"]))

    section("4 零值：0 是显式值")
    c3 = cfg()
    c3["scopes"]["chat"][PEER] = {"max_reply_chars": 0}
    r3 = resolved(c3, bot_id=BOT, chat_id=PEER)
    check("0 不被当成缺失",
          r3["max_reply_chars"] == (0, scopes.LAYER_CHAT), str(r3["max_reply_chars"]))

    # 和"真的没写"对照
    c4 = cfg()
    c4["scopes"]["chat"][PEER] = {}
    r4 = resolved(c4, bot_id=BOT, chat_id=PEER)
    check("空块（没写）才该继承",
          r4["max_reply_chars"] == (200, scopes.LAYER_ACCOUNT), str(r4["max_reply_chars"]))


# ══════════════════════════════════════════════════════════════════
#  5 / 6 昵称别名与 id 优先
# ══════════════════════════════════════════════════════════════════

def test_alias() -> None:
    section("5 昵称别名")
    alias = {"老爸": PEER, "文件传输助手": "filehelper"}
    resolver = lambda name: alias.get(name)  # noqa: E731

    c = {"scopes": {"global": {"allow_self": True},
                    "chat": {"老爸": {"allow_self": False}}}}

    r = resolved(c, chat_id=PEER, resolver=resolver)
    check("昵称键能命中真实会话",
          r["allow_self"] == (False, scopes.LAYER_CHAT), str(r["allow_self"]))

    r2 = resolved(c, chat_id=PEER)          # 不给 resolver
    check("没有 resolver 时昵称键不命中（而不是误命中）",
          r2["allow_self"] == (True, scopes.LAYER_GLOBAL), str(r2["allow_self"]))

    bad = lambda name: (_ for _ in ()).throw(RuntimeError("微信没开"))  # noqa: E731
    try:
        r3 = resolved(c, chat_id=PEER, resolver=bad)
        ok = r3["allow_self"] == (True, scopes.LAYER_GLOBAL)
        check("resolver 抛异常时不命中、不冒泡", ok, str(r3["allow_self"]))
    except Exception as exc:  # noqa: BLE001
        check("resolver 抛异常时不命中、不冒泡", False, f"抛了 {type(exc).__name__}")

    problems = scopes.diagnose(c, chat_id=PEER)          # 无 resolver
    codes = [code for code, _d in problems]
    check("解析不出的昵称键会被 diagnose 报出来",
          "scope_chat_key_unresolved" in codes, str(codes))

    section("6 id 优先")
    c2 = {"scopes": {"global": {"allow_self": True},
                     "chat": {"老爸": {"allow_self": False},
                              PEER: {"allow_self": True}}}}
    r4 = resolved(c2, chat_id=PEER, resolver=resolver)
    check("昵称键与 id 键并存时 id 赢",
          r4["allow_self"] == (True, scopes.LAYER_CHAT), str(r4["allow_self"]))

    c3 = {"scopes": {"global": {"allow_self": True},
                     "chat": {"老爸": {"allow_self": False}}}}
    r5 = resolved(c3, chat_id=PEER, resolver=resolver)
    check("只有昵称键时它照样生效",
          r5["allow_self"] == (False, scopes.LAYER_CHAT), str(r5["allow_self"]))


# ══════════════════════════════════════════════════════════════════
#  7 / 8 旧路径
# ══════════════════════════════════════════════════════════════════

def test_legacy_paths() -> None:
    section("7 旧路径兼容")
    old = {"auto_reply": {"allow_self": True, "max_reply_chars": 120,
                          "group_reply": False, "quiet_hours": [1, 2],
                          "group_allow_all": ["某群"]}}
    r = resolved(old, chat_id=PEER)
    check("旧路径的 max_reply_chars",
          r["max_reply_chars"] == (120, scopes.LAYER_GLOBAL), str(r["max_reply_chars"]))
    check("旧路径的 group_reply=false 不会被当成缺失",
          r["group_reply"] == (False, scopes.LAYER_GLOBAL), str(r["group_reply"]))
    check("旧路径的 quiet_hours 元素被转成字符串",
          r["quiet_hours"] == (("1", "2"), scopes.LAYER_GLOBAL), str(r["quiet_hours"]))
    check("旧路径的 group_allow_all",
          r["group_allow_all"] == (("某群",), scopes.LAYER_GLOBAL), str(r["group_allow_all"]))

    # ⚠ allow_self 是例外：旧的全局 true 只落到自测会话，不进 global
    check("旧全局 allow_self 不落到普通私聊（事故开关不再泄漏）",
          r["allow_self"] == (False, scopes.LAYER_DEFAULT), str(r["allow_self"]))
    rf = resolved(old, chat_id="filehelper")
    check("旧全局 allow_self 只落到 filehelper",
          rf["allow_self"] == (True, scopes.LAYER_LEGACY), str(rf["allow_self"]))
    check("旧全局 allow_self 不落到群里",
          resolved(old, chat_id=GROUP)["allow_self"] == (False, scopes.LAYER_DEFAULT))

    problems = scopes.diagnose(old, chat_id=PEER)
    codes = [code for code, _d in problems]
    check("仍在用旧路径时会提示",
          "scope_legacy_path" in codes, str(codes))

    section("8 新路径优先")
    both = {"auto_reply": {"allow_self": True, "max_reply_chars": 120},
            "scopes": {"global": {"group_reply": False}}}
    r2 = resolved(both, chat_id=PEER)
    check("新路径写的字段生效",
          r2["group_reply"] == (False, scopes.LAYER_GLOBAL), str(r2["group_reply"]))
    check("新路径没写的字段仍回落旧路径",
          r2["max_reply_chars"] == (120, scopes.LAYER_GLOBAL), str(r2["max_reply_chars"]))

    c3 = {"auto_reply": {"allow_self": True},
          "scopes": {"global": {"allow_self": False}}}
    check("显式 global=false 压过旧的全局 true",
          resolved(c3, chat_id="filehelper")["allow_self"]
          == (False, scopes.LAYER_GLOBAL),
          str(resolved(c3, chat_id="filehelper")["allow_self"]))

    c4 = {"auto_reply": {"allow_self": True},
          "scopes": {"chat": {"filehelper": {"allow_self": False}}}}
    check("显式会话 false 压过旧的全局 true",
          resolved(c4, chat_id="filehelper")["allow_self"]
          == (False, scopes.LAYER_CHAT),
          str(resolved(c4, chat_id="filehelper")["allow_self"]))


# ══════════════════════════════════════════════════════════════════
#  9 只读
# ══════════════════════════════════════════════════════════════════

def test_no_mutation() -> None:
    section("9 读取不改配置")
    c = cfg()
    c["scopes"]["chat"]["老爸"] = {"allow_self": False}      # 故意留一个未解析键
    before = copy.deepcopy(c)

    scopes.resolve(c, bot_id=BOT, chat_id=PEER, resolver=lambda n: PEER)
    scopes.get(c, "allow_self", bot_id=BOT, chat_id=PEER, resolver=lambda n: PEER)
    scopes.layer_of(c, "allow_self", bot_id=BOT, chat_id=PEER)
    scopes.diagnose(c, bot_id=BOT, chat_id=PEER)
    scopes.explain(c, bot_id=BOT, chat_id=PEER, resolver=lambda n: PEER)

    check("resolve/get/explain/diagnose 都不修改传入的 config",
          c == before, "配置被改动了")

    # 三态：缺失的键不该被 setdefault 写出来
    check("缺失的键没有被 setdefault 补进去",
          set(c["scopes"]["chat"][PEER]) == {"allow_self"},
          str(c["scopes"]["chat"][PEER]))


# ══════════════════════════════════════════════════════════════════
#  10 explain
# ══════════════════════════════════════════════════════════════════

def test_explain() -> None:
    section("10 explain 来源标注")
    c = cfg()
    text = scopes.explain(c, bot_id=BOT, chat_id=PEER)
    r = resolved(c, bot_id=BOT, chat_id=PEER)

    ok = True
    for key, (_value, layer) in r.items():
        label = scopes.LAYER_LABELS[layer]
        # 每一行都要出现字段名和它真实的来源层
        line = next((ln for ln in text.splitlines() if ln.strip().startswith(key)), "")
        if key not in line or label not in line:
            ok = False
            break
    check("每个字段的来源层与 resolve 一致", ok, text)

    check("explain 带出作用域标题",
          "账号=" in text and "会话=" in text, text.splitlines()[0])
    check("LAYER_LABELS 覆盖全部层",
          set(scopes.LAYER_LABELS) == set(scopes.LAYER_ORDER), str(scopes.LAYER_LABELS))


# ══════════════════════════════════════════════════════════════════
#  边界：类型错误 / 写入 / 常量
# ══════════════════════════════════════════════════════════════════

def test_ui_roundtrip() -> None:
    section("事故复现 2：界面上勾「自测通道」会不会泄漏到私聊")
    # 模拟 desktop.save_config 的写端：五个字段落到 scopes，
    # allow_self 落到 filehelper 而不是 global。
    c = {"auto_reply": {"chats": ["文件传输助手"], "allow_self": True}}

    scopes.set_global_value(c, "max_reply_chars", 240)
    scopes.set_global_value(c, "quiet_hours", ["23:00-07:00"])
    scopes.set_global_value(c, "group_reply", True)
    scopes.set_global_value(c, "group_allow_all", [])
    scopes.set_chat_value(c, "filehelper", "allow_self", True)

    check("勾选后自测通道仍开",
          scopes.get(c, "allow_self", chat_id="filehelper") is True)
    check("勾选后私聊拿不到（不泄漏）",
          scopes.get(c, "allow_self", chat_id=PEER) is False)
    check("勾选后群里拿不到",
          scopes.get(c, "allow_self", chat_id=GROUP) is False)
    check("其余字段落在 global 层",
          scopes.layer_of(c, "max_reply_chars") == scopes.LAYER_GLOBAL,
          scopes.layer_of(c, "max_reply_chars"))

    # 取消勾选：必须写显式 false，否则旧的全局 true 会把它重新点亮
    scopes.set_chat_value(c, "filehelper", "allow_self", False)
    check("取消勾选后自测通道关闭",
          scopes.get(c, "allow_self", chat_id="filehelper") is False)

    # 对比：如果用 clear（写 None）就会被旧值重新点亮 —— 这条钉住这个坑
    scopes.clear_chat_value(c, "filehelper", "allow_self")
    check("clear 会被旧的全局 true 重新点亮（所以写端必须写显式 false）",
          scopes.get(c, "allow_self", chat_id="filehelper") is True)


def test_memo_resolver() -> None:
    section("边界：昵称解析缓存")
    now = [0.0]
    calls = []

    def slow(name):
        calls.append(name)
        return PEER if name == "老爸" else None

    r = scopes.memo_resolver(slow, ttl=30.0, clock=lambda: now[0])
    check("首次调用真的查了", r("老爸") == PEER and len(calls) == 1)
    check("TTL 内命中缓存，不再查库",
          r("老爸") == PEER and len(calls) == 1, str(calls))

    now[0] = 31.0
    r("老爸")
    check("超过 TTL 会重查", len(calls) == 2, str(calls))

    # 解析失败也要缓存：微信没开时不能每条消息都去撞打不开的库
    before = len(calls)
    r("不存在的人")
    r("不存在的人")
    check("解析失败（None）也缓存",
          len(calls) == before + 1, str(calls))

    # 解析器抛异常 → 当 None，不冒泡
    def boom(name):
        raise RuntimeError("库打不开")

    r2 = scopes.memo_resolver(boom, clock=lambda: now[0])
    check("解析器抛异常时返回 None 而不是冒泡", r2("老爸") is None)

    check("None 解析器原样返回", scopes.memo_resolver(None) is None)


def test_type_errors() -> None:
    section("边界：类型不对不静默")
    c = {"scopes": {"global": {"allow_self": "yes", "max_reply_chars": -5}}}
    r = resolved(c, chat_id=PEER)
    check("字符串 'yes' 不当布尔用，落回默认",
          r["allow_self"] == (False, scopes.LAYER_DEFAULT), str(r["allow_self"]))
    check("负数不被当上限，落回默认",
          r["max_reply_chars"] == (300, scopes.LAYER_DEFAULT), str(r["max_reply_chars"]))

    codes = [code for code, _d in scopes.diagnose(c, chat_id=PEER)]
    check("类型问题被 diagnose 报出来",
          "scope_value_type" in codes, str(codes))

    c2 = {"scopes": {"global": {"max_reply_chars": 120.0}}}
    check("整数值的 float 能收",
          resolved(c2)["max_reply_chars"] == (120, scopes.LAYER_GLOBAL))

    c3 = {"scopes": {"global": {"max_reply_chars": "80"}}}
    check("数字字符串能收",
          resolved(c3)["max_reply_chars"] == (80, scopes.LAYER_GLOBAL))

    c4 = {"scopes": {"global": {"quiet_hours": "23:00, 07:00"}}}
    check("逗号串能收成列表",
          resolved(c4)["quiet_hours"] == (("23:00", "07:00"), scopes.LAYER_GLOBAL),
          str(resolved(c4)["quiet_hours"]))

    c5 = {"scopes": {"global": {"allow_self": 1}}}
    check("0/1 当布尔收",
          resolved(c5)["allow_self"] == (True, scopes.LAYER_GLOBAL))


def test_writes() -> None:
    section("边界：写入只写 id、只改内存")
    c = {"auto_reply": {"allow_self": True}}

    scopes.set_chat_value(c, PEER, "allow_self", False)
    check("写会话层后立刻生效",
          scopes.get(c, "allow_self", chat_id=PEER) is False)
    check("写到 scopes.chat[peer]",
          c["scopes"]["chat"][PEER] == {"allow_self": False}, str(c.get("scopes")))

    try:
        scopes.set_chat_value(c, "老爸", "allow_self", False)
        check("拒绝用昵称当会话键", False, "没有抛异常")
    except ValueError:
        check("拒绝用昵称当会话键", True)

    try:
        scopes.set_chat_value(c, PEER, "nonexistent_field", 1)
        check("拒绝未注册字段", False, "没有抛异常")
    except KeyError:
        check("拒绝未注册字段", True)

    check("clear 返回 True", scopes.clear_chat_value(c, PEER, "allow_self") is True)
    check("clear 后恢复继承（普通会话回到默认 false）",
          scopes.get(c, "allow_self", chat_id=PEER) is False)
    check("空块被一起删掉，不留空壳",
          PEER not in (c.get("scopes", {}).get("chat") or {}), str(c.get("scopes")))
    check("clear 不存在的键返回 False",
          scopes.clear_chat_value(c, PEER, "allow_self") is False)

    # 自测会话 clear 后回到旧的全局开关（这才是"恢复继承"能看见的场景）
    c1b = {"auto_reply": {"allow_self": True}}
    scopes.set_chat_value(c1b, "filehelper", "allow_self", False)
    check("filehelper 显式关掉",
          scopes.get(c1b, "allow_self", chat_id="filehelper") is False)
    scopes.clear_chat_value(c1b, "filehelper", "allow_self")
    check("filehelper clear 后回到旧全局开关",
          scopes.get(c1b, "allow_self", chat_id="filehelper") is True)

    scopes.set_chat_value(c, PEER, "allow_self", None)
    check("写 None 等同 clear（三态只有一个写法）",
          PEER not in (c.get("scopes", {}).get("chat") or {}), str(c.get("scopes")))

    scopes.set_global_value(c, "max_reply_chars", 500)
    check("全局写入走 scopes.global",
          scopes.get(c, "max_reply_chars") == 500)
    check("全局写入不删旧字段（可回滚）",
          "allow_self" in c["auto_reply"], str(c["auto_reply"]))

    c2 = {}
    scopes.set_account_value(c2, BOT, "max_reply_chars", 150)
    check("账号写入生效",
          scopes.get(c2, "max_reply_chars", bot_id=BOT) == 150)
    scopes.set_account_value(c2, BOT, "max_reply_chars", None)
    check("账号层 clear 生效",
          BOT not in (c2.get("scopes", {}).get("account") or {}), str(c2.get("scopes")))


def test_key_shapes() -> None:
    section("边界：键形态识别")
    check("wxid 认成 id", scopes.looks_like_id("wxid_abc123") is True)
    check("群 id 认成 id", scopes.looks_like_id("456789@chatroom") is True)
    check("filehelper 认成 id", scopes.looks_like_id("filehelper") is True)
    check("纯数字认成 id", scopes.looks_like_id("12345678") is True)
    check("中文昵称不是 id", scopes.looks_like_id("老爸") is False)
    check("带空格昵称不是 id", scopes.looks_like_id(" 老爸 ") is False)
    check("空串不是 id", scopes.looks_like_id("") is False)
    check("None 不是 id", scopes.looks_like_id(None) is False)

    check("id 形态直接返回自己",
          scopes.key_to_id("wxid_abc123") == "wxid_abc123")
    check("没有 resolver 时昵称返回 None",
          scopes.key_to_id("老爸") is None)
    check("resolver 返回空串当没解析出",
          scopes.key_to_id("老爸", lambda n: "") is None)


def test_get_registry() -> None:
    section("边界：字段注册表")
    c = cfg()
    check("get 未注册字段返回 default",
          scopes.get(c, "no_such", chat_id=PEER, default="X") == "X")
    check("layer_of 未注册字段返回空",
          scopes.layer_of(c, "no_such", chat_id=PEER) == "")
    check("layer_of 与 resolve 一致",
          scopes.layer_of(c, "allow_self", chat_id=PEER) == scopes.LAYER_CHAT)
    check("五个字段都在表里",
          set(scopes.FIELDS) == {"allow_self", "group_reply", "group_allow_all",
                                 "max_reply_chars", "quiet_hours"},
          str(sorted(scopes.FIELDS)))
    check("每个字段都有 kind/default/label",
          all({"kind", "default", "label"} <= set(spec)
              for spec in scopes.FIELDS.values()))
    check("allow_self 走 legacy_self 而不是 legacy（事故开关不映射到 global）",
          scopes.FIELDS["allow_self"].get("legacy_self") is True
          and "legacy" not in scopes.FIELDS["allow_self"],
          str(scopes.FIELDS["allow_self"]))
    check("其余四个字段仍走 legacy 旧路径",
          all(scopes.FIELDS[k].get("legacy") for k in
              ("group_reply", "group_allow_all", "max_reply_chars", "quiet_hours")))


# ══════════════════════════════════════════════════════════════════
#  事故复现：allow_self 全局开着，只有一个会话该关
# ══════════════════════════════════════════════════════════════════

def test_incident_replay() -> None:
    section("事故复现：线上那份配置直接跑，开关还漏不漏")
    # 线上真实配置（2026-11-04 事故时的样子）：全局 allow_self=true，
    # 监听名单里同时有「文件传输助手」和「老爸」。
    live = {"auto_reply": {"chats": ["文件传输助手", "老爸"], "allow_self": True}}
    resolver = lambda n: {"老爸": PEER, "文件传输助手": "filehelper"}.get(n)  # noqa: E731

    check("老爸的私聊拿不到 true（事故开关不再泄漏）",
          scopes.get(live, "allow_self", chat_id=PEER, resolver=resolver) is False)
    check("群里也拿不到 true",
          scopes.get(live, "allow_self", chat_id=GROUP) is False)
    check("文件传输助手仍然是 true（自测通道不受影响）",
          scopes.get(live, "allow_self", chat_id="filehelper") is True)
    check("一个字的配置都不用改，就挡住了",
          "scopes" not in live, str(live))

    # 真的需要在某个会话自测时，必须显式写，而且要写 id
    scopes.set_chat_value(live, GROUP, "allow_self", True)
    check("显式给某个会话开，才真的开",
          scopes.get(live, "allow_self", chat_id=GROUP) is True)
    check("开了这一个不影响另一个",
          scopes.get(live, "allow_self", chat_id=PEER) is False)
    check("也不影响全局（旧字段原样保留）",
          live["auto_reply"]["allow_self"] is True)

    # 反过来：显式关掉自测通道
    scopes.set_chat_value(live, "filehelper", "allow_self", False)
    check("显式关掉 filehelper 也生效",
          scopes.get(live, "allow_self", chat_id="filehelper") is False)


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    print("配置作用域回归测试（bigfish/scopes.py）")
    test_fallback()
    test_three_state()
    test_falsy_not_swallowed()
    test_alias()
    test_legacy_paths()
    test_no_mutation()
    test_explain()
    test_type_errors()
    test_writes()
    test_key_shapes()
    test_get_registry()
    test_incident_replay()
    test_ui_roundtrip()
    test_memo_resolver()

    print(f"\n{'=' * 46}")
    total = PASS + FAIL
    if FAIL:
        print(f"失败 {FAIL}/{total}")
        for n in FAILED:
            print(f"  - {n}")
        return 1
    print(f"全部通过 {PASS}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
