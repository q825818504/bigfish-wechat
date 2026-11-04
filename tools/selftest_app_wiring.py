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
    def __init__(self, local_id: int, content: str) -> None:
        self.local_id = local_id
        self.content = content
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
    APP.call_model = lambda cfg, system, user: reply  # type: ignore[assignment]

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

    APP.call_model = lambda cfg, system, user: "一整段话，不分条。"  # type: ignore[assignment]
    app, sender = make_app(say=False)
    app.enqueue(incoming("在吗", local_id=1))
    import time as _t
    for batch in app.batcher.tick(now=_t.monotonic() + 10):
        app.handle_batch(batch)
    check("只发一条", len(sender.calls) == 1, f"{sender.calls}")
    check("内容原样", sender.calls[0][1] == "一整段话，不分条。", f"{sender.calls[0]}")


def test_listen_off() -> None:
    section("关掉攒批")

    APP.call_model = lambda cfg, system, user: "收到"  # type: ignore[assignment]
    app, sender = make_app(listen=False)
    app.enqueue(incoming("一", local_id=1))
    check("第一条立刻处理", len(sender.calls) == 1, f"{sender.calls}")
    app.enqueue(incoming("二", local_id=2))
    check("第二条也立刻处理", len(sender.calls) == 2, f"{sender.calls}")


def test_skip_rules() -> None:
    section("过滤")

    APP.call_model = lambda cfg, system, user: "收到"  # type: ignore[assignment]
    app, sender = make_app()
    app.enqueue(incoming("", local_id=1))
    check("空消息不入批", app.batcher.pending() == {}, f"{app.batcher.pending()}")

    app2, sender2 = make_app()
    app2.cfg.allow_self = False
    self_msg = incoming("我自己说的", local_id=2)
    self_msg.is_self = True
    app2.enqueue(self_msg)
    check("自己的消息不入批", app2.batcher.pending() == {}, f"{app2.batcher.pending()}")

    app3, sender3 = make_app()
    app3.cfg.quiet_hours = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11,
                            12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23]
    app3.enqueue(incoming("安静时段", local_id=3))
    check("安静时段不入批", app3.batcher.pending() == {}, f"{app3.batcher.pending()}")


def main() -> int:
    print("=" * 66)
    print("接入自检（enqueue → 攒批 → 分条 → 发送）")
    print("=" * 66)

    test_wiring()
    test_say_off()
    test_listen_off()
    test_skip_rules()

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
