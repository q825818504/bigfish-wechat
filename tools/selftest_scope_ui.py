"""会话配置弹层的后端接口回归测试（C-1 的第二半）。

跑法：
    python tools/selftest_scope_ui.py

不需要微信、不需要窗口、不需要网络，**也不碰真实的 config.json** ——
读写函数在测试里被替换成内存字典，跑完内存就丢了。

## 测什么

`Backend.get_chat_scope` / `save_chat_scope` 是界面弹层的两个接口。
它们最容易出的错不是逻辑，而是**语义漂移**：

  · 界面上点「继承」，到底有没有真的把那一项从会话层删掉？
  · 删空了之后配置文件里会不会留一个空壳 `{}`？
  · 保存时会不会顺手把 `auto_reply.allow_self` 改掉（那是旧路径）？
  · 昵称键混进来会不会被静默接受，然后在微信改名后失效？

覆盖：
  1  读取：5 个字段 + 来源层 + overridden 标记
  2  保存 true/false：落到会话层，来源标成本会话
  3  保存 null：真的删除，恢复继承
  4  删空之后不留空壳
  5  写盘只写 scopes，旧 auto_reply 原样不动
  6  昵称键被拒绝（而不是静默接受）
  7  未注册字段被忽略，不写进配置
  8  一个会话的覆盖不影响另一个会话
  9  保存后回读的状态与独立计算一致
"""
from __future__ import annotations

import copy
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import desktop as D          # noqa: E402
from bigfish import scopes                # noqa: E402

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
#  打桩：把配置读写换成内存
# ══════════════════════════════════════════════════════════════════

STORE: dict = {}
SAVES: list = []


class FakeBackend:
    """只提供两个 slot 用得到的属性。

    `Backend` 是 QObject，但这两个方法只碰 `self._reader` / `self.account` /
    `self._scope_context()`，所以不必真的建窗口。
    """

    def __init__(self):
        self._reader = None
        self.account = "wxid_bot_fixture"
        self.account_id = self.account
        self.account_nick = ""

    _scope_context = D.Backend._scope_context
    _scope_rows = D.Backend._scope_rows
    get_chat_scope = D.Backend.get_chat_scope
    save_chat_scope = D.Backend.save_chat_scope


def install_fake_io():
    def load():
        return copy.deepcopy(STORE)

    def save(cfg):
        STORE.clear()
        STORE.update(copy.deepcopy(cfg))
        SAVES.append(copy.deepcopy(cfg))

    D.load_json_config = load
    D.save_json_config = save


PEER = "wxid_dad_fixture"
OTHER = "wxid_other_fixture"


def reset(initial=None):
    STORE.clear()
    STORE.update(copy.deepcopy(initial if initial is not None else {
        "auto_reply": {"allow_self": True, "max_reply_chars": 300},
        "scopes": {"global": {"group_reply": True, "max_reply_chars": 280}},
    }))
    SAVES.clear()


# ══════════════════════════════════════════════════════════════════
#  1 读取
# ══════════════════════════════════════════════════════════════════

def test_read() -> None:
    section("1 读取：字段 + 来源层")
    reset()
    be = FakeBackend()
    d = json.loads(be.get_chat_scope(PEER))

    check("ok", d.get("ok") is True, str(d))
    fields = {f["key"]: f for f in d.get("fields", [])}
    check("五个字段都在",
          set(fields) == set(scopes.FIELDS), str(sorted(fields)))
    check("每个字段都有 label / kind / layer_label",
          all(f.get("label") and f.get("kind") and f.get("layer_label")
              for f in fields.values()))
    check("没覆盖过 → overridden 全为 False",
          all(not f["overridden"] for f in fields.values()))
    check("max_reply_chars 落在全局层",
          fields["max_reply_chars"]["layer"] == scopes.LAYER_GLOBAL,
          fields["max_reply_chars"]["layer"])
    check("group_allow_all 落在默认层",
          fields["group_allow_all"]["layer"] == scopes.LAYER_DEFAULT,
          fields["group_allow_all"]["layer"])

    check("缺会话 id 时明确报错",
          json.loads(be.get_chat_scope("")).get("ok") is False)
    check("读取不写盘", not SAVES, str(len(SAVES)))

    # 占位符必须和字段对得上。之前前端按"是不是数字"瞎猜，
    # 结果「无条件回复的群」的输入框里写着「例如 23:00-07:00」。
    check("两个列表字段的占位符不一样",
          fields["quiet_hours"]["placeholder"]
          != fields["group_allow_all"]["placeholder"],
          fields["quiet_hours"]["placeholder"])
    check("免打扰的占位符是时间段的例子",
          "23:00-07:00" in fields["quiet_hours"]["placeholder"],
          fields["quiet_hours"]["placeholder"])
    check("群名单的占位符不是时间段",
          "23:00" not in fields["group_allow_all"]["placeholder"],
          fields["group_allow_all"]["placeholder"])
    check("占位符全部来自注册表，不是前端硬编码",
          all(f.get("placeholder") for f in fields.values()
              if f["kind"] != "bool"),
          str({k: v.get("placeholder") for k, v in fields.items()}))


# ══════════════════════════════════════════════════════════════════
#  2 / 3 / 4 保存
# ══════════════════════════════════════════════════════════════════

def test_write_bool() -> None:
    section("2 保存 false：落到会话层")
    reset()
    be = FakeBackend()
    d = json.loads(be.save_chat_scope(PEER, json.dumps({"allow_self": False})))

    check("ok", d.get("ok") is True, str(d))
    fields = {f["key"]: f for f in d.get("fields", [])}
    check("allow_self 现在标成本会话",
          fields["allow_self"]["overridden"] is True
          and fields["allow_self"]["layer"] == scopes.LAYER_CHAT,
          str(fields["allow_self"]))
    check("值确实是 false", fields["allow_self"]["value"] is False)
    check("写进了 scopes.chat",
          STORE["scopes"]["chat"][PEER] == {"allow_self": False},
          str(STORE.get("scopes", {}).get("chat")))

    section("3 保存 null：恢复继承")
    d2 = json.loads(be.save_chat_scope(PEER, json.dumps({"allow_self": None})))
    fields2 = {f["key"]: f for f in d2.get("fields", [])}
    check("cleared 里报告了 allow_self",
          "allow_self" in (d2.get("cleared") or []), str(d2.get("cleared")))
    check("回到继承（不再是本会话）",
          fields2["allow_self"]["overridden"] is False,
          str(fields2["allow_self"]))

    section("4 删空之后不留空壳")
    check("空的会话块被一起删掉",
          PEER not in (STORE.get("scopes", {}).get("chat") or {}),
          str(STORE.get("scopes", {}).get("chat")))


def test_write_value() -> None:
    section("5 值字段与写盘路径")
    reset()
    be = FakeBackend()
    be.save_chat_scope(PEER, json.dumps({
        "max_reply_chars": 80,
        "quiet_hours": ["23:00-07:00"],
        "group_allow_all": [],
    }))

    check("int 落会话层",
          STORE["scopes"]["chat"][PEER]["max_reply_chars"] == 80,
          str(STORE["scopes"]["chat"][PEER]))
    check("list 落会话层",
          STORE["scopes"]["chat"][PEER]["quiet_hours"] == ["23:00-07:00"])
    check("空列表被保留（表示显式清空，不是没写）",
          STORE["scopes"]["chat"][PEER]["group_allow_all"] == [])

    check("旧的 auto_reply 原样不动（可回滚）",
          STORE["auto_reply"] == {"allow_self": True, "max_reply_chars": 300},
          str(STORE["auto_reply"]))
    check("只写了一次盘", len(SAVES) == 1, str(len(SAVES)))

    r = scopes.resolve(STORE, chat_id=PEER)
    check("独立计算的结果与接口一致",
          r["max_reply_chars"] == (80, scopes.LAYER_CHAT), str(r["max_reply_chars"]))
    check("显式清空的空列表生效",
          r["group_allow_all"] == ((), scopes.LAYER_CHAT), str(r["group_allow_all"]))


# ══════════════════════════════════════════════════════════════════
#  6 / 7 / 8 边界
# ══════════════════════════════════════════════════════════════════

def test_rejects() -> None:
    section("6 昵称键必须被拒绝")
    reset()
    be = FakeBackend()
    d = json.loads(be.save_chat_scope("老爸", json.dumps({"allow_self": True})))
    check("ok=false", d.get("ok") is False, str(d))
    check("错误里说清楚为什么",
          "昵称" in (d.get("error") or "") or "id" in (d.get("error") or ""),
          d.get("error"))
    check("配置没被写坏", not SAVES, str(len(SAVES)))

    section("7 未注册字段被忽略")
    reset()
    be2 = FakeBackend()
    d2 = json.loads(be2.save_chat_scope(PEER, json.dumps({
        "allow_self": True, "no_such_field": 123})))
    check("ok", d2.get("ok") is True, str(d2))
    check("未知字段没进配置",
          "no_such_field" not in (STORE.get("scopes", {})
                                  .get("chat", {}).get(PEER, {})),
          str(STORE.get("scopes", {}).get("chat")))
    check("已知字段正常写入",
          STORE["scopes"]["chat"][PEER]["allow_self"] is True)


def test_isolation() -> None:
    section("8 一个会话的覆盖不影响另一个")
    reset()
    be = FakeBackend()
    be.save_chat_scope(PEER, json.dumps({"allow_self": True}))
    other = json.loads(be.get_chat_scope(OTHER))
    of = {f["key"]: f for f in other.get("fields", [])}
    check("另一个会话仍是继承",
          of["allow_self"]["overridden"] is False, str(of["allow_self"]))
    check("另一个会话拿不到 true",
          scopes.get(STORE, "allow_self", chat_id=OTHER) is False)

    section("9 filehelper 不受普通会话的覆盖影响")
    be.save_chat_scope("filehelper", json.dumps({"allow_self": True}))
    check("filehelper 显式开",
          scopes.get(STORE, "allow_self", chat_id="filehelper") is True)
    check("普通会话仍然关",
          scopes.get(STORE, "allow_self", chat_id=PEER) is True)  # 上面自己开过
    reset()
    be2 = FakeBackend()
    be2.save_chat_scope("filehelper", json.dumps({"allow_self": True}))
    check("重置后：filehelper 开、私聊关",
          scopes.get(STORE, "allow_self", chat_id="filehelper") is True
          and scopes.get(STORE, "allow_self", chat_id=PEER) is False)


def test_watching_bootstrap() -> None:
    """监听列表必须能从配置里自己读出来。

    ⚠ 真出过的 bug：界面上的勾选来自「选账号」的返回值，而 start_engine
      读的是 Backend.watching —— 后者原本只在**手动保存**时才被赋值。
      于是界面勾着、后端是空的，点启动就报"没有选择要监听的会话"，
      非要把会话取消再勾一次才能启动。
    """
    section("监听列表（启动时自读）")

    class Bare:
        pass

    def load_with(cfg):
        D.load_json_config = lambda: copy.deepcopy(cfg)
        return D.Backend._watching_from_config(Bare())

    got = load_with({"auto_reply": {"chats": ["filehelper"]}})
    check("auto_reply.chats 能读到", got == ["filehelper"], str(got))

    got = load_with({
        "auto_reply": {"account": "wxid_a", "chats": ["old"]},
        "per_account": {"wxid_a": {"chats": ["filehelper"]}},
    })
    check("这个号专属的优先于全局", got == ["filehelper"], str(got))

    got = load_with({
        "auto_reply": {"account": "wxid_b", "chats": ["old"]},
        "per_account": {"wxid_a": {"chats": ["filehelper"]}},
    })
    check("别的号的配置不串过来", got == ["old"], str(got))

    got = load_with({"auto_reply": {"chats": [123, "filehelper"]}})
    check("非字符串也转成字符串", got == ["123", "filehelper"], str(got))

    got = load_with({})
    check("什么都没配 → 空", got == [], str(got))

    def boom():
        raise RuntimeError("配置坏了")
    D.load_json_config = boom
    check("配置读不出来 → 空，不抛",
          D.Backend._watching_from_config(Bare()) == [])

    install_fake_io()          # 恢复


def test_wants_sticker() -> None:
    """用户在要表情包吗。

    ⚠ 线上出过：用户说「发个表情给爷看看」，她回了「鼓腮算表情不」。
      原因是表情包只看 15% 概率，**完全不读用户说了什么**。
    """
    section("要不要表情包（意图）")

    want = D.wants_sticker
    check("「发个表情给爷看看」→ 要", want("发个表情给爷看看"))
    check("「来个表情包」→ 要", want("来个表情包"))
    check("「斗图吗」→ 要", want("斗图吗"))
    check("「发张图过来」→ 要", want("发张图过来"))
    check("「今天天气不错」→ 不要", not want("今天天气不错"))
    check("空串 → 不要", not want(""))
    check("None 不炸", want(None) is False)
    # 已知代价：用户只是在夸表情时也会算"要"，多发一张而已，
    # 比"明确要求了却没反应"便宜。
    check("「你这表情挺好玩」→ 也算要（已知误判，可接受）",
          want("你这表情挺好玩"))


def test_restore_watching() -> None:
    """切账号时的监听列表恢复。三件事必须同时成立。"""
    section("切账号恢复监听列表")

    # 1) 从没存过 → 用全局那份当种子，并落成这个号专属的
    c = {"auto_reply": {"chats": ["filehelper"]}}
    got = D.restore_watching_for(c, "wxid_a")
    check("首次进入：沿用全局那份", got == ["filehelper"], str(got))
    check("顺手落成这个号专属的",
          c["per_account"]["wxid_a"]["chats"] == ["filehelper"], str(c))

    # 2) 这个号存过 → 用它，且覆盖全局
    c = {"auto_reply": {"chats": ["old"]},
         "per_account": {"wxid_a": {"chats": ["filehelper"]}}}
    got = D.restore_watching_for(c, "wxid_a")
    check("有专属配置就用专属的", got == ["filehelper"], str(got))
    check("全局那份被同步", c["auto_reply"]["chats"] == ["filehelper"], str(c))

    # 3) 这个号明确存了空 → 必须清空全局，不能把上一个号的捡回来
    c = {"auto_reply": {"chats": ["上一个号的会话"]},
         "per_account": {"wxid_b": {"chats": []}}}
    got = D.restore_watching_for(c, "wxid_b")
    check("专属配置为空 → 就是空", got == [], str(got))
    check("全局也必须跟着清空（否则启动兜底会捡回别人的会话）",
          c["auto_reply"]["chats"] == [], str(c))

    # 4) 切到第三个号（没存过），全局现在是空 → 不能凭空给东西
    got = D.restore_watching_for(c, "wxid_c")
    check("新号没存过、全局是空 → 空", got == [], str(got))


def test_sticker_echo() -> None:
    """表情回环：时间戳一对一取走。

    ⚠ 线上真出过：她发了一张表情，日志写"发送失败"（数据库 12 秒才落库，
      窗口只等 10 秒），于是没记账 → 表情读回来被当成对方发来的 →
      她又回了一句。表情在库里没有文本，只能靠时间认。
    """
    section("表情回环（时间戳）")

    take = D.take_sticker_echo
    sent: dict = {}
    now = 1000.0

    check("没发过表情 → 不拦", take(sent, "c1", now) is False)
    check("别的会话发过 → 本会话不拦",
          take({"c2": [now]}, "c1", now) is False)

    sent = {"c1": [now]}
    check("刚发过一张 → 认出是回环", take(sent, "c1", now + 2) is True)
    check("戳被取走了（一对一，不会一直拦）", take(sent, "c1", now + 3) is False)

    sent = {"c1": [now, now + 1]}
    take(sent, "c1", now + 5)
    check("发两张、只回一张 → 还剩一张的戳", sent["c1"] == [now + 1], str(sent))

    sent = {"c1": [now]}
    check("超过窗口就不认（45 秒）",
          take(sent, "c1", now + 60) is False)

    sent = {"c1": [now - 100, now]}
    check("过期戳先被清掉，不会挡住新的",
          take(sent, "c1", now + 1) is True and sent["c1"] == [], str(sent))

    check("占位符覆盖两种写法",
          "[表情]" in D.STICKER_MARKS and "[动画表情]" in D.STICKER_MARKS)


# ══════════════════════════════════════════════════════════════════
class SettingsBackend(FakeBackend):
    """设置页那一对 slot（get_config / save_config）。

    它们要碰 `self.watching`，所以比会话弹层多一个属性。
    """

    def __init__(self):
        super().__init__()
        self.watching: list[str] = []

    get_config = D.Backend.get_config
    save_config = D.Backend.save_config


def test_settings_allow_self_roundtrip() -> None:
    """设置页的「处理自己发的消息」：存完必须读得回来。

    ⚠ 这条是真出过的 bug：写会话层、读全局层，于是界面点开→保存→
      显示「已保存」，勾选框却弹回关闭。磁盘上明明是 true。
      用户以为没生效，其实生效了 —— 只是界面看错了地方。
    """
    section("设置页 allow_self 存取一致")

    b = SettingsBackend()
    STORE.clear()
    STORE.update({"auto_reply": {"interval": 1.2}})

    check("初始是关的", json.loads(b.get_config())["allow_self"] is False)

    r = json.loads(b.save_config(json.dumps({"allow_self": True})))
    check("保存返回成功", r.get("ok") is True, str(r))
    check("存完读回来是开的（曾经读回 False）",
          json.loads(b.get_config())["allow_self"] is True)

    chat = (STORE.get("scopes") or {}).get("chat") or {}
    check("写在会话层 filehelper，不写 global",
          chat.get("filehelper", {}).get("allow_self") is True, str(chat))
    glob = (STORE.get("scopes") or {}).get("global") or {}
    check("global 里没有 allow_self（否则所有会话都能回自己）",
          "allow_self" not in glob, str(glob))

    r = json.loads(b.save_config(json.dumps({"allow_self": False})))
    check("关掉也返回成功", r.get("ok") is True, str(r))
    check("关掉之后读回来是关的",
          json.loads(b.get_config())["allow_self"] is False)
    chat = (STORE.get("scopes") or {}).get("chat") or {}
    check("关掉写的是显式 false，不是删掉（删掉会被旧值点亮）",
          chat.get("filehelper", {}).get("allow_self") is False, str(chat))

    # 旧全局 true 不能把已经关掉的开关重新点亮
    STORE["auto_reply"]["allow_self"] = True
    check("旧配置里的全局 true 不会覆盖显式 false",
          json.loads(b.get_config())["allow_self"] is False)


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    print("会话配置接口回归测试（desktop.py 的两个 slot）")
    install_fake_io()
    test_read()
    test_write_bool()
    test_write_value()
    test_rejects()
    test_isolation()
    test_settings_allow_self_roundtrip()
    test_watching_bootstrap()
    test_restore_watching()
    test_wants_sticker()
    test_sticker_echo()

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
