# -*- coding: utf-8 -*-
"""驱动「会话配置」弹层，验证交互真的对。

跑法：
    python tools/check_scope_ui.py

需要 Qt（和 shot_ui.py 一样），不需要微信、不写真实配置
（配置读写在这份脚本里被替换成内存字典）。

## 为什么是个"驱动界面"的检查，而不是静态解析

这个弹层出过的三个 bug，全都**看得见但不报错**：

  1. 值字段在「继承」状态下输入框 disabled，而唯一的按钮只能切回继承
     —— 用户永远改不成"自己设"，整条路是死的；
  2. 「继承中」的撤销按钮天生 disabled，而绑定写了 `if (b.disabled) return;`
     —— 监听器从来没绑上，改完值想撤销点不动；
  3. 来源标签取的是"当前生效层"，不是"清掉覆盖后会从哪继承"
     —— 点完继承仍显示「本会话」，用户以为没生效。

静态检查一条都发现不了。这里把 openScope → 改值 → 撤销 → 保存
整条链路跑一遍，把每个中间状态都断言掉。
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

os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

from PySide6.QtCore import QTimer, QUrl                          # noqa: E402
from PySide6.QtWebEngineWidgets import QWebEngineView           # noqa: E402
from PySide6.QtWidgets import QApplication                       # noqa: E402

from bigfish import desktop as D                                 # noqa: E402

CHAT = "wxid_scope_fixture"

#: 打桩的配置：会话层只覆盖 allow_self，max_reply_chars 继承全局
STORE = {
    "auto_reply": {"allow_self": True},
    "scopes": {
        "global": {"group_reply": True, "max_reply_chars": 280},
        "chat": {CHAT: {"allow_self": False}},
    },
}
D.load_json_config = lambda: copy.deepcopy(STORE)
D.save_json_config = lambda cfg: None


class _Backend:
    _reader = None
    account = "wxid_bot_fixture"
    _scope_context = D.Backend._scope_context
    _scope_rows = D.Backend._scope_rows
    get_chat_scope = D.Backend.get_chat_scope


PAYLOAD = _Backend().get_chat_scope(CHAT)

SETUP = """
window.__saved = null;
S.bridge = {
  get_chat_scope: () => Promise.resolve(%s),
  save_chat_scope: (id, p) => { window.__saved = JSON.parse(p);
                                return Promise.resolve(%s); },
};
S.chats = [{username: %s, name: '测试对象', kind: 'contact'}];
S.watching = new Set();
renderChats();
openScope(%s, '测试对象');
""" % (json.dumps(PAYLOAD, ensure_ascii=False),
       json.dumps(PAYLOAD, ensure_ascii=False),
       json.dumps(CHAT), json.dumps(CHAT))

PROBE = r"""
(function(){
  const out = {};
  const find = (key) => document.querySelector(
    `#scopeBody [data-scope="${key}"][data-val="inherit"]`)?.closest('.scope-row');
  const src = (key) => find(key)?.querySelector('.scope-src')?.textContent;
  const inp = (key) => find(key)?.querySelector('[data-scope-input]');
  const back = (key) => find(key)?.querySelector('[data-val="inherit"]');
  const onSeg = (key) => find(key)?.querySelector('.seg.is-on')?.textContent.trim();

  out.open = !document.querySelector('#scopeMask').hidden;
  out.rows = document.querySelectorAll('.scope-row').length;
  out.dirtyAtOpen = scopeDirty();

  // ① 继承中的值字段：输入框必须可编辑（不然改不成"自己设"）
  out.inheritInputDisabled = inp('max_reply_chars').disabled;
  out.inheritValue = inp('max_reply_chars').value;
  out.inheritLabel = src('max_reply_chars');
  out.inheritBackDisabled = back('max_reply_chars').disabled;

  // ② 直接改值 → 变成覆盖
  const box = inp('max_reply_chars');
  box.value = '120';
  box.dispatchEvent(new Event('input'));
  out.afterTypingDraft = SCOPE.draft.max_reply_chars;
  out.afterTypingLabel = src('max_reply_chars');
  out.afterTypingDirty = scopeDirty();
  out.afterTypingBackDisabled = back('max_reply_chars').disabled;

  // ③ 点「继承」→ 撤销覆盖（曾经点不动，因为监听器没绑上）
  back('max_reply_chars').click();
  out.afterRevertDraft = SCOPE.draft.max_reply_chars;
  out.afterRevertLabel = src('max_reply_chars');
  out.afterRevertValue = inp('max_reply_chars').value;
  out.afterRevertDirty = scopeDirty();
  out.afterRevertBackDisabled = back('max_reply_chars').disabled;

  // ④ 布尔三态：allow_self 目前是本会话 false
  out.boolOn = onSeg('allow_self');
  find('allow_self').querySelector('[data-val="inherit"]').click();
  out.boolAfterInherit = onSeg('allow_self');
  out.boolDraftAfterInherit = SCOPE.draft.allow_self;
  out.boolSrcAfterInherit = src('allow_self');

  // ⑤ 「全部恢复继承」
  document.querySelector('#scopeInheritAll').click();
  out.allInherit = JSON.stringify(SCOPE.draft);

  // ⑥ 再设一次，保存，payload 必须就是草稿
  const box2 = inp('max_reply_chars');
  box2.value = '120';
  box2.dispatchEvent(new Event('input'));
  find('allow_self').querySelector('[data-val="false"]').click();
  saveScope();
  return JSON.stringify(out);
})()
"""

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \u2713 {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  \u2717 {name}" + (f"  <- {detail}" if detail else ""))


def judge(d: dict, saved: str) -> None:
    print("\n[弹层交互]")
    check("弹层打开了", d.get("open") is True)
    check("五个字段都渲染出来", d.get("rows") == 5, str(d.get("rows")))
    check("刚打开时没有未保存修改", d.get("dirtyAtOpen") is False)

    check("① 继承中的输入框可编辑", d.get("inheritInputDisabled") is False)
    check("① 继承中显示继承来的值", d.get("inheritValue") == "280",
          str(d.get("inheritValue")))
    check("① 继承中标注来源全局", d.get("inheritLabel") == "继承自全局",
          str(d.get("inheritLabel")))
    check("① 继承中的撤销按钮禁用（本来就跟随）",
          d.get("inheritBackDisabled") is True)

    check("② 改值后草稿变成覆盖值", d.get("afterTypingDraft") == 120,
          str(d.get("afterTypingDraft")))
    check("② 改值后标注本会话", d.get("afterTypingLabel") == "本会话",
          str(d.get("afterTypingLabel")))
    check("② 改值后有未保存修改", d.get("afterTypingDirty") is True)
    check("② 改值后撤销按钮可用", d.get("afterTypingBackDisabled") is False)

    check("③ 点继承后草稿回到 null", d.get("afterRevertDraft") is None,
          str(d.get("afterRevertDraft")))
    check("③ 点继承后标注回到全局来源",
          d.get("afterRevertLabel") == "继承自全局",
          str(d.get("afterRevertLabel")))
    check("③ 点继承后显示回继承值", d.get("afterRevertValue") == "280",
          str(d.get("afterRevertValue")))
    check("③ 点继承后又没有未保存修改", d.get("afterRevertDirty") is False)

    check("④ 初始布尔值在本会话（关）", d.get("boolOn") == "关",
          str(d.get("boolOn")))
    check("④ 点继承后选中「继承」", d.get("boolAfterInherit") == "继承",
          str(d.get("boolAfterInherit")))
    check("④ 点继承后草稿为 null", d.get("boolDraftAfterInherit") is None)
    check("④ 点继承后来源是代码默认（不是本会话）",
          d.get("boolSrcAfterInherit") == "代码默认值",
          str(d.get("boolSrcAfterInherit")))

    check("⑤ 全部恢复继承后草稿全 null",
          all(v is None for v in json.loads(d.get("allInherit") or "{}").values()),
          str(d.get("allInherit")))

    payload = json.loads(saved) if saved and saved != "null" else {}
    print("\n[保存 payload]")
    check("⑥ 保存成功发出", bool(payload), str(saved))
    check("⑥ 改过的值在里面", payload.get("max_reply_chars") == 120,
          str(payload))
    check("⑥ 显式关的布尔在里面", payload.get("allow_self") is False,
          str(payload))
    check("⑥ 没动过的项是 null（表示跟随）",
          payload.get("quiet_hours") is None and payload.get("group_reply") is None,
          str(payload))


def main() -> int:
    app = QApplication(sys.argv[:1])
    view = QWebEngineView()
    view.resize(900, 700)
    view.show()
    view.load(QUrl.fromLocalFile(os.path.join(_ROOT, "ui", "index.html")))

    state = {"result": None, "saved": "null"}

    def after_load(ok: bool) -> None:
        if not ok:
            print("!! 页面加载失败")
            app.quit()
            return
        QTimer.singleShot(600, setup)

    def setup() -> None:
        view.page().runJavaScript(SETUP)
        QTimer.singleShot(700, probe)

    def probe() -> None:
        view.page().runJavaScript(PROBE, 0, got)

    def got(value) -> None:
        state["result"] = value
        QTimer.singleShot(300, grab_saved)

    def grab_saved() -> None:
        view.page().runJavaScript("JSON.stringify(window.__saved)", 0, done)

    def done(saved) -> None:
        state["saved"] = saved
        try:
            judge(json.loads(state["result"] or "{}"), state["saved"])
        except Exception as exc:                     # noqa: BLE001
            print("探针返回无法解析:", exc, state["result"])
            global FAIL
            FAIL += 1
            FAILED.append("探针返回")
        app.quit()

    view.loadFinished.connect(after_load)
    QTimer.singleShot(30000, app.quit)
    app.exec()

    total = PASS + FAIL
    print(f"\n{'=' * 46}")
    if FAIL:
        print(f"失败 {FAIL}/{total}")
        for n in FAILED:
            print(f"  - {n}")
        return 1
    print(f"全部通过 {PASS}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
