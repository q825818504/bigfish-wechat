"""[bigfish] 微信发送通道 —— UIA 路线。

和 iLink 通道的区别：

    iLink   纯 HTTP，不碰微信进程，但只能跟授权者对话、不能发朋友圈
    UIA     驱动微信自己的界面，功能全（发消息/朋友圈/加好友），
            但必须桌面解锁 + 微信窗口可见

本模块封装 UIA 路线，暴露一个和 iLink 版一致的 send 接口，
方便上层切换。

前置条件（不满足会明确报错，不会静默失败）：
  1. 管理员权限（热激活要写 gate 字节）
  2. 微信已登录且主窗口没收到托盘
  3. 桌面没锁

发送前的安全校验：
  打开会话后，读输入框的 Name 属性（等于当前聊天对象），
  和目标名不一致就中止 —— 防止发错人。
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from dataclasses import dataclass
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_VENDOR = os.path.join(_ROOT, "vendor", "wechatauto-replica")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)


class SendError(RuntimeError):
    """发送失败，附带可读原因。"""

    def __init__(self, msg: str, kind: str = ""):
        self.kind = kind
        super().__init__(msg)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def desktop_available() -> bool:
    """桌面是否解锁（锁屏时 UIA 操作会失败）。"""
    try:
        u = ctypes.windll.user32
        u.OpenInputDesktop.restype = ctypes.c_void_p
        h = u.OpenInputDesktop(0, False, 0x0001)
        if h:
            u.CloseDesktop(ctypes.c_void_p(h))
            return True
        return False
    except Exception:
        return False


@dataclass
class SendResult:
    ok: bool
    who: str = ""
    text: str = ""
    detail: str = ""


class UiaSender:
    """驱动微信界面发送消息。

    用法：
        s = UiaSender()
        s.wake()                      # 热激活（需要管理员）
        s.send("文件传输助手", "你好")
    """

    def __init__(self, auto_wake: bool = True):
        self._uia = None
        self._woken = False
        self.auto_wake = auto_wake

    # ------------------------------------------------------------------
    @property
    def ready(self) -> bool:
        return self._uia is not None

    def wake(self, force: bool = False) -> bool:
        """准备 UIA：检查环境 → 构造驱动 → 物化控件树。"""
        if self._woken and not force:
            return True

        if not is_admin():
            raise SendError(
                "UIA 发送需要管理员权限（热激活要往微信进程写 gate 字节）。\n"
                "请以管理员身份运行，或改用 --channel ilink。",
                kind="privilege")

        try:
            from wechatauto.uia_driver import WeChatUIA
        except Exception as exc:
            raise SendError(f"加载 UIA 驱动失败: {exc}", kind="import") from exc

        try:
            self._uia = WeChatUIA()
            ok = self._uia.ensure_window(wake=True)
        except Exception as exc:
            raise SendError(f"初始化 UIA 失败: {exc}", kind="init") from exc

        if not ok:
            raise SendError(
                "找不到微信主窗口。请确认微信已登录且没收到托盘。",
                kind="window")

        try:
            if not self._uia.is_materialized():
                self._uia.ensure_materialized(timeout=8)
        except Exception:
            pass

        self._woken = True
        return True

    # ------------------------------------------------------------------
    def current_chat(self) -> Optional[str]:
        if not self._uia:
            return None
        try:
            return self._uia.current_chat()
        except Exception:
            return None

    def open_chat(self, who: str) -> bool:
        self.wake()
        try:
            return bool(self._uia.open_chat(who))
        except Exception as exc:
            raise SendError(f"打开会话 {who!r} 失败: {exc}", kind="open") from exc

    # ------------------------------------------------------------------
    def _input_name(self) -> str:
        """读输入框的 Name（等于当前聊天对象），用于发送前校验。"""
        try:
            import uiautomation as auto
            from wechatauto.uia_driver import CHAT_INPUT_AID

            hwnd = (ctypes.windll.user32.FindWindowW("Qt51514QWindowIcon", None)
                    or ctypes.windll.user32.FindWindowW(None, "微信"))
            if not hwnd:
                return ""
            win = auto.ControlFromHandle(hwnd)
            if not win:
                return ""
            e = win.EditControl(AutomationId=CHAT_INPUT_AID)
            if e and e.Exists(0, 0):
                return getattr(e, "Name", "") or ""
        except Exception:
            pass
        return ""

    def send(self, who: str, text: str,
             verify_identity: bool = True) -> SendResult:
        """给 who 发一条文本。

        verify_identity=True 时，发送前核对输入框 Name 与 who 一致，
        不一致就中止 —— 防止发错人。
        """
        if not text or not text.strip():
            raise SendError("内容为空，拒绝发送", kind="empty")

        if not desktop_available():
            raise SendError(
                "桌面已锁定或不可见，UIA 无法操作。\n"
                "解锁桌面后重试，或改用 --channel ilink。",
                kind="desktop")

        self.wake()

        now = self.current_chat()
        if now != who:
            if not self.open_chat(who):
                raise SendError(f"打不开会话: {who!r}", kind="open")
            time.sleep(0.35)
            now = self.current_chat()
            if now != who:
                raise SendError(
                    f"会话名不匹配：期望 {who!r}，实际 {now!r}。"
                    f"为避免发错人，已中止。", kind="identity")

        if verify_identity:
            inp = self._input_name()
            if inp and inp != who and who != "文件传输助手":
                # 文件传输助手的输入框 Name 可能带占位符尾巴，放过
                if who not in inp:
                    raise SendError(
                        f"输入框归属校验失败：输入框 Name={inp!r}，"
                        f"目标={who!r}。已中止。", kind="identity")

        try:
            ok = bool(self._uia.send_text_to(text, who))
        except Exception as exc:
            raise SendError(f"发送失败: {exc}", kind="send") from exc

        return SendResult(ok=ok, who=who, text=text,
                          detail="" if ok else "send_text_to 返回 False")


class ILinkSender:
    """iLink 通道的适配器（可选，锁屏时用）。

    限制：只能回给扫码授权的那个用户，且需要 context_token。
    """

    def __init__(self, bot=None, last_message=None):
        self.bot = bot
        self.last_message = last_message

    @property
    def ready(self) -> bool:
        return bool(self.bot and self.bot.cred.valid)

    def send(self, who: str, text: str) -> SendResult:
        if not self.ready:
            raise SendError("iLink 未登录", kind="auth")
        if self.last_message is None:
            raise SendError(
                "iLink 需要先收到一条消息才能回复（context_token）",
                kind="context")
        try:
            res = self.bot.send_text(self.last_message, text)
            return SendResult(ok=True, who=who, text=text,
                              detail=str(res[:1]) if res else "")
        except Exception as exc:
            raise SendError(f"iLink 发送失败: {exc}", kind="send") from exc
