"""接入自检：enqueue → 攒批 → handle_batch → 分条 → 发送。

跑法：
    python tools/selftest_app_wiring.py

不碰微信、不碰桌面、不调真模型：
  · reader/sender 是假对象
  · call_model 被替换成固定回复
  · build_prompt 被替换成固定 system/user

验证的是「接线对不对」，不是「模型说得好不好」。
"""
from __future__ import annotations

import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import app as APP  # noqa: E402
from bigfish.watcher import Incoming  # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []

#: 证据链在测试里收集到内存，不写真实数据目录。
#: 顺带让"否决必须留痕"这条能被断言（spec D-1 第 18 条）。
TRACES: list[dict] = []


def _capture_trace(verdict, decision, **kw):
    TRACES.append({"verdict": getattr(verdict, "who", ""),
                   "rule": getattr(verdict, "rule", ""),
                   "reason": getattr(decision, "reason", ""),
                   "chat": kw.get("chat_id", "")})
    return None


def _install_trace_capture() -> None:
    from bigfish import identity_trace

    identity_trace.record = _capture_trace


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
        print(f"  {MARK_FAIL} {name}  {detail}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


# ----------------------------------------------------------------------
class FakeRow:
    """库里的一行。sender_name 是"自己的 wxid"，和真 reader 一致。"""

    def __init__(self, local_id: int, content: str) -> None:
        self.local_id = local_id
        self.content = content
        self.sender_name = "me"
        self.is_self = True


class FakeReader:
    def __init__(self) -> None:
        self.rows: list[FakeRow] = []
        self._id = 0
        self._db = types.SimpleNamespace(wxid="me", account="test")

    def messages(self, chat: str, limit: int = 20):
        return self.rows[-limit:]

    def push(self, text: str) -> None:
        self._id += 1
        self.rows.append(FakeRow(self._id, text))


class FakeSender:
    def __init__(self, reader: FakeReader) -> None:
        self.reader = reader
        self.calls: list[tuple[str, str]] = []

    def send(self, who: str, text: str):
        self.calls.append((who, text))
        self.reader.push(text)          # 模拟「发出去就落库」
        return types.SimpleNamespace(ok=True, detail="")


class FakeSoul:
    name = "大肥鱼"
    address = "爸"


def make_app(*, listen: bool = True, say: bool = True) -> tuple[APP.BigFishApp, FakeSender]:
    reader = FakeReader()
    sender = FakeSender(reader)
    cfg = APP.AppConfig()
    cfg.listen_enabled = listen
    cfg.say_enabled = say
    app = APP.BigFishApp(reader, sender, FakeSoul(), object(), cfg)
    # 不读灵魂/记忆/插件：直接给固定 prompt
    app.build_prompt = lambda conv, msg, body: ("SYS", "USER")  # type: ignore[assignment]
    return app, sender


def incoming(text: str, *, chat: str = "小麟", chat_id: str = "wxid_x",
             sender: str = "小麟", local_id: int = 0) -> Incoming:
    return Incoming(
        chat_id=chat_id, chat_name=chat, local_id=local_id, sort_seq=local_id,
        timestamp=0, msg_type="文本", sender_id=None, sender_name=sender,
        text=text, is_self=False, raw_content="",
    )


def test_wiring() -> None:
    section("接线")

    # 固定模型回复：3 段带标记
    reply = "知道了\n{{split}}\n那明天就去长坂坡玩吧\n{{split}}\n咱们先看樱花，再看枣树"
    APP.call_model = lambda cfg, system, user, **kw: reply  # type: ignore[assignment]

    app, sender = make_app()
    app.enqueue(incoming("大肥鱼", local_id=1))
    app.enqueue(incoming("你在不在", local_id=2))
    app.enqueue(incoming("吃饭了没", local_id=3))

    check("三条都在攒批里", app.batcher.pending().get("wxid_x") == 3, f"{app.batcher.pending()}")
    check("还没发送", sender.calls == [], f"{sender.calls}")

    # 跳到 10 秒后，让批次到期
    import time as _t
    for batch in app.batcher.tick(now=_t.monotonic() + 10):
        app.handle_batch(batch)

    check("批次处理后发送 3 条", len(sender.calls) == 3, f"{sender.calls}")
    check("第一条是「知道了」", sender.calls[0][1] == "知道了", f"{sender.calls[0]}")
    check("三条内容正确",
          [t for _w, t in sender.calls] == ["知道了", "那明天就去长坂坡玩吧", "咱们先看樱花，再看枣树"],
          f"{[t for _w, t in sender.calls]}")
    check("发送目标用会话名", all(w == "小麟" for w, _t2 in sender.calls), f"{sender.calls}")

    # 历史：3 条对方 + 3 条自己（最后一条走异步确认线程，等它落一下）
    _t.sleep(0.8)
    turns = app.conv("wxid_x").turns
    check("历史里对方 3 条", sum(1 for w, _ in turns if w == "小麟") == 3, f"{turns}")
    check("历史里自己 3 条", sum(1 for w, _ in turns if w == "爸") == 3, f"{turns}")


def test_say_off() -> None:
    section("关掉分条")

    APP.call_model = lambda cfg, system, user, **kw: "一整段话，不分条。"  # type: ignore[assignment]
    app, sender = make_app(say=False)
    app.enqueue(incoming("在吗", local_id=1))
    import time as _t
    for batch in app.batcher.tick(now=_t.monotonic() + 10):
        app.handle_batch(batch)
    check("只发一条", len(sender.calls) == 1, f"{sender.calls}")
    check("内容原样", sender.calls[0][1] == "一整段话，不分条。", f"{sender.calls[0]}")


def test_listen_off() -> None:
    section("关掉攒批")

    APP.call_model = lambda cfg, system, user, **kw: "收到"  # type: ignore[assignment]
    app, sender = make_app(listen=False)
    app.enqueue(incoming("一", local_id=1))
    check("第一条立刻处理", len(sender.calls) == 1, f"{sender.calls}")
    app.enqueue(incoming("二", local_id=2))
    check("第二条也立刻处理", len(sender.calls) == 2, f"{sender.calls}")


def test_skip_rules() -> None:
    section("过滤")

    APP.call_model = lambda cfg, system, user, **kw: "收到"  # type: ignore[assignment]

    # ⚠ 这里断言的是 `_accept` 本身，不是 `batcher.pending()`。
    #   以前写成"自己的消息不入批"，但 `listen.feed()` 自己就有一道
    #   `if msg.is_self: return None` —— 那条断言其实一直在测 listen 层，
    #   `_accept` 里的归属判断从来没被覆盖到（改坏了也不会红）。
    app, sender = make_app()
    check("空消息被拦下", app._accept(incoming("", local_id=1)) is None)

    app2, sender2 = make_app()
    self_msg = incoming("我自己说的", local_id=2, sender="me")
    self_msg.is_self = True
    check("自己发的被拦下（allow_self 默认关）",
          app2._accept(self_msg) is None)

    app3, sender3 = make_app()
    app3.cfg.raw = {"scopes": {"global": {
        "quiet_hours": [str(h) for h in range(24)]}}}
    check("安静时段被拦下",
          app3._accept(incoming("安静时段", local_id=3)) is None)

    app4, sender4 = make_app()
    check("对方的正常消息能过",
          app4._accept(incoming("你在不在", local_id=4)) is not None)

    # A-1：sender 解析不出的私聊，以前会被当成"对方说的"直接回，
    # 那正是自环的形状。现在拿不准就不回。
    app5, sender5 = make_app()
    unknown = incoming("拿不准是谁发的", local_id=5, sender="")
    check("sender 为空的私聊被拦下（拿不准就不回）",
          app5._accept(unknown) is None)


def test_ledger_veto() -> None:
    section("账本否决（D-1）")

    APP.call_model = lambda cfg, system, user, **kw: "收到"  # type: ignore[assignment]

    app, sender = make_app()
    # 模拟"她 1 秒前发过这句"
    app.ledger.note_sent("wxid_x", "我今天有点累")
    TRACES.clear()
    m = incoming("我今天有点累", local_id=9)
    check("判据说对方，但账本说刚发过 → 不回", app._accept(m) is None)

    check("否决写了证据链（否则吞了真消息没人查得到）",
          len(TRACES) == 1, str(TRACES))
    item = TRACES[0] if TRACES else {}
    check("留痕里写明是账本干的",
          item.get("reason") == "ledger_veto", str(item))
    check("留痕里的结论是自己",
          item.get("verdict") == "self" and item.get("rule") == "ledger_veto",
          str(item))

    app2, sender2 = make_app()
    check("账本没记过的照常放行",
          app2._accept(incoming("你今天怎么样", local_id=10)) is not None)

    # 发一条，账本要自己记上（send_parts 里 note_sent）
    app3, sender3 = make_app()
    app3.hit = 0
    app3.enqueue(incoming("在吗", local_id=11))
    for batch in app3.batcher.tick(now=app3.batcher._clock() + 99):
        app3.handle_batch(batch)
    check("发出去的话进了账本", len(app3.ledger) >= 1, str(len(app3.ledger)))
    texts = [e["text"] for e in app3.ledger.snapshot()]
    check("账本里是刚发的那句", any("收到" in t for t in texts), str(texts))


def main() -> int:
    _install_trace_capture()
    print("=" * 66)
    print("接入自检（enqueue → 攒批 → 分条 → 发送）")
    print("=" * 66)

    test_wiring()
    test_say_off()
    test_listen_off()
    test_skip_rules()
    test_ledger_veto()

    print("\n" + "=" * 66)
    if FAIL:
        print(f"结果：{PASS}/{PASS + FAIL} 通过，{FAIL} 项失败")
        for name in FAILED:
            print(f"  \u00b7 {name}")
        return 1
    print(f"结果：{PASS}/{PASS + FAIL} 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
