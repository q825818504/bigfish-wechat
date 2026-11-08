#!/usr/bin/env python
"""大肥鱼 · 监听列表热更新自检。

    python tools/selftest_watch_hot.py

═══════════════════════════════════════════════════════════════════════
为什么需要这一份
═══════════════════════════════════════════════════════════════════════
2026-11-08 用户现场：运行中新增一个会话监控，对方发了两条消息，
程序**没反应，日志里连一行 [收] 都没有**。

根因：`set_watching()` 只改内存 + 存盘。`watcher._chats` 在引擎启动
那一刻就定死了，之后加进来的人根本没人轮询 —— 而界面上的标签
`监听 3 个会话` 读的是 `self.watching`，所以它照样说 3 个。

这份自检守的是：watch / unwatch 两个方向都必须**立即**改到 `_chats`，
而且名字解析规则一致（否则「取消」会取消一个不存在的 uid）。
"""
from __future__ import annotations

import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "vendor", "wechatauto-replica"))

from bigfish.watcher import MessageWatcher  # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  ✗ {name}" + (f"  —— {detail}" if detail else ""))


class FakeDB:
    """只实现 watcher 用到的那几个方法。"""

    wxid = "me"

    def username_by_nickname(self, nick: str):
        return {"四条丶": "wxid_sitiao", "老爸": "wxid_51f85n6xp73o22"}.get(nick)

    def group_name_to_id(self, nick: str):
        return None

    def get_messages(self, uid: str, limit: int = 50):
        return []


def main() -> int:
    w = MessageWatcher(FakeDB())
    w.on_log = lambda s: None

    uid = w.watch("四条丶")
    check("watch 解析出 uid", uid == "wxid_sitiao", uid)
    check("watch 进了 _chats", "wxid_sitiao" in w._chats, str(w._chats))
    check("watch 建了水位", "wxid_sitiao" in w._wm)

    again = w.watch("四条丶")
    check("重复 watch 不重复入列",
          again == uid and w._chats.count("wxid_sitiao") == 1, str(w._chats))

    ok = w.unwatch("四条丶")
    check("unwatch 返回 True", ok is True)
    check("unwatch 移出 _chats", "wxid_sitiao" not in w._chats, str(w._chats))
    check("unwatch 清掉水位", "wxid_sitiao" not in w._wm)
    check("unwatch 清掉 prefill", "wxid_sitiao" not in w._prefill)

    check("unwatch 一个没监听的返回 False", w.unwatch("四条丶") is False)

    # 两个会话并存，删一个不影响另一个
    w.watch("老爸")
    w.watch("四条丶")
    w.unwatch("四条丶")
    check("删一个不影响另一个",
          w._chats == ["wxid_51f85n6xp73o22"], str(w._chats))

    print()
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILED:
        print("失败项：" + "、".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
