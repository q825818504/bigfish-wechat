"""[bigfish] 拖放捕获 —— 把系统拖进来的文件路径交给界面。

═══════════════════════════════════════════════════════════════════════
试过哪些路，为什么都不行
═══════════════════════════════════════════════════════════════════════

① HTML5 的 `dataTransfer.files`
   QtWebEngine 里能拿到 File 对象，但**没有 path 属性** ——
   那是 Electron 的扩展，Chromium 标准里没有。没有路径就没法转 GIF。

② 页面里 `dragover` preventDefault + QWidget 的 eventFilter
   实测：鼠标拖到窗口上显示**禁止图标**，事件根本没产生。
   直接构造 QDropEvent 塞给控件时 eventFilter 是正常的（日志有、accepted
   为 True），所以代码没错 —— 是系统没把事件送过来。

③ OLE drag-drop + `ChangeWindowMessageFilterEx` 放行
   本程序要 UIA，所以必须以管理员身份运行（高完整性）。
   资源管理器是中完整性。UIPI 会拦住跨完整性的 OLE 拖放。
   放行 WM_DROPFILES / WM_COPYDATA / WM_COPYGLOBALDATA 三条消息
   （API 都返回 True），事件仍然没到。

═══════════════════════════════════════════════════════════════════════
现在走的路：Windows 原生 WM_DROPFILES
═══════════════════════════════════════════════════════════════════════

`DragAcceptFiles(hwnd, TRUE)` 告诉系统"这个窗口收文件"。
之后系统直接发 `WM_DROPFILES`，**不经过 OLE**，也就绕开了 OLE 那套
跨进程的复杂度。

配合 `QAbstractNativeEventFilter` 全局拦截窗口消息 —— 这比装
eventFilter 到某个子控件可靠：不管事件落在哪一层，消息都会经过这里。

拿到 hDrop 之后用 `DragQueryFileW` 逐条取路径，`DragFinish` 释放。

⚠ 注意 `DragFinish` 必须调，否则系统侧的资源不释放，
  第二次拖放可能失败。

═══════════════════════════════════════════════════════════════════════
前端约定
═══════════════════════════════════════════════════════════════════════

页面实现 `window.__onDropFiles(paths)` 接收字符串数组。
没实现就静默丢弃（比如用户在别的页面拖东西进来）。
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QObject

#: 系统拖进来文件时发的消息
WM_DROPFILES = 0x0233

#: DragQueryFileW 的"要文件数"特殊索引
_DRAG_QUERY_COUNT = 0xFFFFFFFF


class _MSG(ctypes.Structure):
    """Win32 MSG 结构。nativeEventFilter 拿到的是它的指针。"""

    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", wintypes.DWORD),
        ("pt_x", wintypes.LONG),
        ("pt_y", wintypes.LONG),
    ]


class DropCatcher(QObject, QAbstractNativeEventFilter):
    """接住系统拖进来的文件，转给页面。

    用法：
        catcher = DropCatcher(view, window)
        catcher.install()
    """

    def __init__(self, view, window=None):
        QObject.__init__(self, view)
        QAbstractNativeEventFilter.__init__(self)
        self.view = view
        self.window = window or view
        self._installed = False

    # ------------------------------------------------------------------
    def install(self) -> bool:
        """注册原生消息过滤器 + 让窗口接受文件拖放。幂等。"""
        if self._installed:
            return True
        ok = False

        # ① 全局拦窗口消息
        try:
            from PySide6.QtCore import QCoreApplication

            app = QCoreApplication.instance()
            if app is not None:
                app.installNativeEventFilter(self)
                ok = True
        except Exception as exc:
            self._log(f"installNativeEventFilter 失败：{exc}")

        # ② 让窗口接受文件
        n = 0
        for w in self._targets():
            try:
                hwnd = int(w.winId())
            except Exception:
                continue
            if not hwnd:
                continue
            # 先撤 OLE，否则系统不发 WM_DROPFILES
            revoked = self._revoke_ole(hwnd)
            if self._accept_files(hwnd):
                n += 1
                self._log(f"  hwnd={hwnd} 撤OLE={revoked} 已设接受文件")
            # OLE 那条也顺手放行 —— 万一某些系统上 WM_DROPFILES 不走
            self._allow_uipi(hwnd)

        self._installed = ok or n > 0
        self._log(f"install: 原生过滤器={ok}，接受文件的窗口={n}")
        return self._installed

    # ------------------------------------------------------------------
    def _targets(self):
        """要设成"接受文件"的窗口：主窗口 + view + view 的所有 QWidget 子控件。

        为什么全都要设：拖放事件可能落在任意一层，只设主窗口的话，
        鼠标悬在渲染区上时系统可能认为"这里不接受"。
        """
        out = [self.window, self.view]
        try:
            from PySide6.QtWidgets import QWidget

            out += self.view.findChildren(QWidget)
        except Exception:
            pass
        # 去重（按 hwnd）
        seen = set()
        uniq = []
        for w in out:
            try:
                h = int(w.winId())
            except Exception:
                continue
            if h and h not in seen:
                seen.add(h)
                uniq.append(w)
        return uniq

    # ------------------------------------------------------------------
    @staticmethod
    def _revoke_ole(hwnd: int) -> bool:
        """撤销这个窗口的 OLE drop target 注册。

        ═══════════════════════════════════════════════════════════════
        为什么必须撤
        ═══════════════════════════════════════════════════════════════

        一个窗口只要注册了 OLE drop target（`RegisterDragDrop`），
        系统就**只走 OLE 那条路，再也不发 WM_DROPFILES**。

        Qt 的 `setAcceptDrops(True)` 会注册 OLE；QWebEngineView 内部
        为了支持网页内拖放，肯定也注册了。所以 DragAcceptFiles 设了
        也没用 —— 日志里只有 install、没有 WM_DROPFILES，就是这个原因。

        而 OLE 这条路在管理员权限下被 UIPI 卡死（跨完整性 COM 调用被拒），
        表现就是拖动源显示禁止图标。

        两条路互相打架，只能选一条。这里选 WM_DROPFILES：
        撤销 OLE 注册，让系统回到老式路径。

        代价：网页内部的拖放（拖文字、拖图片进页面）会失效。
        我们的界面不需要那个 —— 要的只是"从资源管理器拖文件进来"。
        """
        try:
            r = ctypes.windll.ole32.RevokeDragDrop(
                ctypes.c_void_p(int(hwnd)))
            # S_OK=0；DRAGDROP_E_NOTREGISTERED=0x80040100（没注册过，正常）
            return r == 0
        except Exception:
            return False

    @staticmethod
    def _accept_files(hwnd: int) -> bool:
        """DragAcceptFiles(hwnd, TRUE)"""
        try:
            ctypes.windll.shell32.DragAcceptFiles(
                ctypes.c_void_p(int(hwnd)), True)
            return True
        except Exception:
            return False

    @staticmethod
    def _allow_uipi(hwnd: int) -> bool:
        """放行三条跨权限消息。管理员权限下 OLE 拖放要靠它。"""
        try:
            MSGFLT_ALLOW = 1
            u = ctypes.windll.user32
            ok = True
            for msg in (WM_DROPFILES, 0x004A, 0x0049):
                try:
                    r = u.ChangeWindowMessageFilterEx(
                        ctypes.c_void_p(int(hwnd)), int(msg),
                        MSGFLT_ALLOW, None)
                    ok = ok and bool(r)
                except Exception:
                    ok = False
            return ok
        except Exception:
            return False

    # ------------------------------------------------------------------
    def nativeEventFilter(self, event_type, message):
        """拦 WM_DROPFILES。

        返回 (False, 0) 表示"我没处理，继续传下去"——
        Qt 的约定，不是布尔"成功与否"。
        """
        try:
            et = event_type
            if isinstance(et, bytes):
                et = et.decode("ascii", "ignore")
            if et not in ("windows_generic_MSG", "windows_dispatcher_MSG"):
                return False, 0

            msg = ctypes.cast(int(message), ctypes.POINTER(_MSG)).contents
            if msg.message != WM_DROPFILES:
                return False, 0

            hdrop = int(msg.wParam)
            paths = self._read_drop(hdrop)
            self._log(f"WM_DROPFILES hdrop={hdrop} 文件数={len(paths)}")
            if paths:
                self._deliver(paths)
            return True, 0        # 已处理，别让 Qt 再转一遍
        except Exception as exc:
            self._log(f"nativeEventFilter 异常：{exc}")
            return False, 0

    # ------------------------------------------------------------------
    def _read_drop(self, hdrop: int):
        """从 HDROP 里把路径全读出来，然后 DragFinish。"""
        paths = []
        try:
            shell32 = ctypes.windll.shell32
            shell32.DragQueryFileW.argtypes = [
                ctypes.c_void_p, wintypes.UINT, wintypes.LPWSTR,
                wintypes.UINT]
            shell32.DragQueryFileW.restype = wintypes.UINT

            n = shell32.DragQueryFileW(
                ctypes.c_void_p(hdrop), _DRAG_QUERY_COUNT, None, 0)
            for i in range(int(n or 0)):
                # 先问长度，再分配
                need = shell32.DragQueryFileW(
                    ctypes.c_void_p(hdrop), i, None, 0)
                if not need:
                    continue
                buf = ctypes.create_unicode_buffer(int(need) + 1)
                got = shell32.DragQueryFileW(
                    ctypes.c_void_p(hdrop), i, buf, int(need) + 1)
                if got:
                    p = buf.value
                    if p and os.path.isfile(p):
                        paths.append(p)
        except Exception as exc:
            self._log(f"DragQueryFile 失败：{exc}")
        finally:
            # ⚠ 必须释放，否则第二次拖放可能失败
            try:
                ctypes.windll.shell32.DragFinish(ctypes.c_void_p(hdrop))
            except Exception:
                pass
        return paths

    # ------------------------------------------------------------------
    def _deliver(self, paths) -> None:
        """把路径数组交给页面。

        用 json.dumps 而不是手拼：路径里有反斜杠、引号、中文都可能。
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
            self._log(f"已交给页面：{len(paths)} 个")
        except Exception as exc:
            self._log(f"runJavaScript 失败：{exc}")

    # ------------------------------------------------------------------
    def _log(self, msg: str) -> None:
        """写到 <程序目录>/data/_drop.log。

        界面在管理员权限下跑，控制台看不到，出问题只能靠这个。
        """
        try:
            from bigfish.paths import app_root

            p = os.path.join(app_root(), "data", "_drop.log")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(time.strftime("%H:%M:%S") + " " + msg + "\n")
        except Exception:
            pass
