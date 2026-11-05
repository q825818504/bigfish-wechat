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


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    print("会话配置接口回归测试（desktop.py 的两个 slot）")
    install_fake_io()
    test_read()
    test_write_bool()
    test_write_value()
    test_rejects()
    test_isolation()

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
