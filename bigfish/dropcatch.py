"""[bigfish] 拖放捕获 —— 把系统拖进来的文件路径交给界面。

═══════════════════════════════════════════════════════════════════════
为什么要用 Python 拦，而不是靠 HTML5 的 drag & drop
═══════════════════════════════════════════════════════════════════════

两条路都不通，各有各的原因：

① HTML5 的 `dataTransfer.files`
   QtWebEngine 里能拿到 File 对象，但**拿不到路径** ——
   `file.path` 是 Electron 的扩展属性，Chromium 标准里没有。
   而我们要的恰恰是路径（后面要靠它转 GIF、复制文件）。

② 靠 JS 的 `dragover` preventDefault 放行
   实测（2026-11-03）：鼠标拖到窗口上显示的是**禁止图标**，
   说明事件根本没进到页面 —— `QWebEngineView` 是 QWidget 子类，
   `acceptDrops` 默认是 False，系统层面的拖放被它自己拒了。

所以只能在 Python 侧拦 `QEvent.Drop`，从 `QMimeData` 拿
`urls()`，再通过 `runJavaScript` 把路径数组交给前端。

═══════════════════════════════════════════════════════════════════════
怎么装
═══════════════════════════════════════════════════════════════════════

`QWebEngineView` 内部还有一层渲染控件，事件可能落在子控件上，
所以要递归装到整棵子树 —— 只装 view 本身有时候收不到。

前端约定：实现 `window.__onDropFiles(paths)`。
没实现就静默丢弃（比如用户在别的页面拖东西进来）。
"""
from __future__ import annotations

import json
import os
import sys

from PySide6.QtCore import QEvent, QObject


class DropCatcher(QObject):
    """装到 QWebEngineView 上，把拖进来的文件路径转给页面。"""

    def __init__(self, view, parent=None):
        super().__init__(parent or view)
        self.view = view
        self._installed = []

    # ------------------------------------------------------------------
    def install(self) -> int:
        """递归装到 view 及其所有子控件。返回装了几个。"""
        n = 0
        targets = [self.view]
        try:
            targets += self.view.findChildren(QObject)
        except Exception:
            pass
        for w in targets:
            try:
                if not hasattr(w, "installEventFilter"):
                    continue
                if w in self._installed:
                    continue        # 幂等：loadFinished 后会再调一次
                # QWidget 必须 setAcceptDrops(True)，否则系统根本不给它 Drop 事件
                if hasattr(w, "setAcceptDrops"):
                    w.setAcceptDrops(True)
                w.installEventFilter(self)
                self._installed.append(w)
                n += 1
            except Exception:
                continue
        return n

    # ------------------------------------------------------------------
    def eventFilter(self, obj, event):
        t = event.type()
        try:
            if t in (QEvent.DragEnter, QEvent.DragMove):
                md = event.mimeData()
                if md is not None and md.hasUrls():
                    event.acceptProposedAction()
                    return True
            elif t == QEvent.Drop:
                md = event.mimeData()
                if md is not None and md.hasUrls():
                    paths = []
                    for u in md.urls():
                        try:
                            if u.isLocalFile():
                                p = u.toLocalFile()
                                if p and os.path.isfile(p):
                                    paths.append(p)
                        except Exception:
                            continue
                    if paths:
                        self._deliver(paths)
                    event.acceptProposedAction()
                    return True
        except Exception as exc:
            # 拖放出问题不该让整个界面崩，记一笔就算了
            try:
                print(f"[drop] {exc}", file=sys.stderr, flush=True)
            except Exception:
                pass
        return False

    # ------------------------------------------------------------------
    def _deliver(self, paths) -> None:
        """把路径数组交给页面。

        用 json.dumps 而不是手拼字符串：路径里有反斜杠、引号、
        中文都可能，手拼必炸。
        """
        try:
            arr = json.dumps([str(p) for p in paths], ensure_ascii=False)
        except Exception:
            return
        js = (
            "(function(){"
            f"  var a = {arr};"
            "   if (typeof window.__onDropFiles === 'function') {"
            "     window.__onDropFiles(a);"
            "   }"
            "})();"
        )
        try:
            self.view.page().runJavaScript(js)
        except Exception as exc:
            try:
                print(f"[drop] runJavaScript 失败：{exc}",
                      file=sys.stderr, flush=True)
            except Exception:
                pass
