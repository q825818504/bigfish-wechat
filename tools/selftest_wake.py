"""2026-11-09 那批真机 bug 的回归：醒来补回 / 会话名解析 / 断句。

跑法：
    python tools/selftest_wake.py

三个都是用户实跑一天撞出来的，而且都属于"日志看起来正常、行为是错的"：

1. **睡醒不补回**：日志写「下次说话时会带出来」—— 对方不发新消息，
   她永远不会回那句。现在醒来 20 秒后主动走一遍正常回复链。
2. **主动消息发不出去**：`WeChatReader.sessions()` 返回 dict，代码用
   `getattr(sess, "username")` 读，永远空 → wxid 原样进 UIA →
   「会话名不匹配：期望 'wxid_…'，实际 '老爸'」→ 中止。
3. **话说一半**：跨轮配额把预算压到 18 字，`_cut_at_sentence` 硬切，
   「还得对着那领导笑」被切成「还得对着那领导」—— 缺谓语的病句。

⚠ 第 2、3 条要 import desktop / world，PySide6 不在时跳过（不算失败）。
"""
from __future__ import annotations

import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PASS = 0
FAIL = 0
SKIP = 0
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


def skip(name: str, why: str) -> None:
    global SKIP
    SKIP += 1
    print(f"  \u2013 {name}（跳过：{why}）")


def section(title: str) -> None:
    print(f"\n== {title} ==")


def main() -> int:
    print("=" * 66)
    print("醒来补回 / 会话名解析 / 断句")
    print("=" * 66)

    from bigfish import life as L

    section("睡醒补回：pending 要带上会话名")
    st = L.LifeState()
    L.note_pending(st, "wxid_a", "早，大肥鱼", 100.0, chat_name="老爸")
    item = L.peek_pending(st, "wxid_a") or {}
    check("记下会话名", item.get("chat_name") == "老爸", str(item))
    check("记下预览", "大肥鱼" in str(item.get("preview")), str(item))
    L.note_pending(st, "wxid_a", "还没睡醒？", 200.0)
    item = L.peek_pending(st, "wxid_a") or {}
    check("第二条不覆盖会话名", item.get("chat_name") == "老爸", str(item))
    check("条数累加", int(item.get("count") or 0) == 2, str(item))

    try:
        from bigfish import desktop as D
        from bigfish import world as W
        from bigfish.watcher import Incoming  # noqa: F401
    except Exception as exc:          # noqa: BLE001
        skip("补回队列", f"{exc}")
        skip("会话名解析", f"{exc}")
        skip("断句", f"{exc}")
        return _report()

    section("补回队列：到点后真的进 handle")
    got: list = []

    class _FakeWorker:
        def __init__(self) -> None:
            self._stop = False
            self.logs: list = []

        def _log(self, text: str, kind: str = "") -> None:
            self.logs.append(text)

    w = _FakeWorker()
    w._catchup = [(0.0, "wxid_a", "老爸", "早，大肥鱼")]
    D.EngineWorker._drain_catchup(w, 100.0, got.append)
    check("到点送进 handle", len(got) == 1, f"{len(got)} 条")
    check("带对会话", got and got[0].chat_id == "wxid_a", str(got[:1]))
    check("带对会话名", got and got[0].chat_name == "老爸", str(got[:1]))
    check("带对方原话", got and "大肥鱼" in got[0].text, str(got[:1]))
    check("标成补回（不重复写库/上屏）",
          bool(got) and getattr(got[0], "replayed", False) is True,
          str(getattr(got[0], "replayed", None)))
    check("发完清空队列", w._catchup == [], str(w._catchup))

    section("补回队列：没到点不动、没正文不补")
    w2 = _FakeWorker()
    early: list = []
    w2._catchup = [(500.0, "wxid_b", "小红", "在吗")]
    D.EngineWorker._drain_catchup(w2, 100.0, early.append)
    check("没到点不动", early == [], f"{early}")
    check("没到点仍留在队列", len(w2._catchup) == 1, str(w2._catchup))

    w3 = _FakeWorker()
    empty: list = []
    w3._catchup = [(0.0, "wxid_c", "小明", "")]
    D.EngineWorker._drain_catchup(w3, 100.0, empty.append)
    check("没正文不补（避免空消息）", empty == [], f"{empty}")

    section("会话名解析：wxid → 昵称")
    class _Reader:
        def sessions(self):
            return [
                {"username": "wxid_a", "name": "老爸"},
                {"username": "wxid_b", "name": "小红"},
            ]

    class _World:
        def _reader(self):
            return _Reader()

    check("dict 结构的会话能解析",
          W.World._display_name(_World(), "wxid_a") == "老爸",
          W.World._display_name(_World(), "wxid_a"))
    check("查不到就原样返回",
          W.World._display_name(_World(), "wxid_zzz") == "wxid_zzz",
          W.World._display_name(_World(), "wxid_zzz"))

    section("断句：宁可超预算，也不要半句")
    line = "交接这段最难熬，还得对着那领导笑"
    cut = D._cut_at_sentence(line, 18)
    check("超预算时保留整句", "领导笑" in cut, cut)
    huge = "很长" * 40
    check("离谱超长仍会切", "…" in D._cut_at_sentence(huge, 10),
          D._cut_at_sentence(huge, 10)[:20])
    check("预算内原样返回", D._cut_at_sentence("在呢", 40) == "在呢")

    return _report()


def _report() -> int:
    print()
    if FAIL:
        print(f"结果：{PASS}/{PASS + FAIL} 通过，{FAIL} 项失败"
              + (f"，{SKIP} 项跳过" if SKIP else ""))
        for n in FAILED:
            print(f"  · {n}")
        return 1
    print(f"结果：{PASS}/{PASS} 全部通过"
          + (f"（{SKIP} 项跳过）" if SKIP else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
