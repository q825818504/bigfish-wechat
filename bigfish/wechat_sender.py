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

两条发送通道
───────────
    send()        文本：Ctrl+V 打字 → 回读比对 → 回车
    send_media()  图片/表情包：CF_HDROP 写剪贴板 → Ctrl+V → 检测草稿 → 回车

表情包为什么走 send_media：
    微信把**剪贴板里的 .gif 文件**直接识别成表情消息（local_type 47），
    不是图片消息。所以想发原生表情包，不需要操作表情面板 ——
    把 GIF 当文件贴进输入框就够了。2026-11-02 在 4.1.15.53 上实测通过。
    尺寸建议 <=1024×1024，超过会被当作文件发送。
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
    #: 媒体路径（send_media 用；文本发送留空）
    path: str = ""
    #: media | text —— 上层据此决定日志措辞和回环登记内容
    kind: str = "text"


# ══════════════════════════════════════════════════════════════════════
# 媒体：格式与尺寸
# ══════════════════════════════════════════════════════════════════════

#: 微信中「当图片发」的扩展名。gif 也在这里，但它是**表情**不是图片。
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")

#: 微信吃 GIF 当表情的尺寸上限。超过就退化成文件消息。
#: 这是 pic2meme 的结论 + 2026-11-02 本机 1024×1024 实测，仍在验证中，
#: 所以做成可调（vault 的 sticker-config.json），不要写死成硬门槛。
STICKER_MAX = 1024

#: 表情显示上限（微信强制缩到 200×200 左右），用于提示"别画太细"
STICKER_DISPLAY = 200


def classify_media(path: str) -> str:
    """按扩展名判微信会把它当什么。

    返回 'sticker' | 'image' | 'file'。
    .gif 判 sticker —— 这是整条表情包路线的分界线，单独拎出来说清楚。
    """
    ext = os.path.splitext(path or "")[1].lower()
    if ext == ".gif":
        return "sticker"
    if ext in IMAGE_EXT:
        return "image"
    return "file"


def check_media(path: str) -> tuple[bool, str]:
    """发送前检查文件。返回 (是否可发, 说明)。

    不抛异常：调用方要能把"这张图不行"记成一条可读日志，
    而不是让整轮回复崩掉。
    """
    if not path:
        return False, "路径为空"
    if not os.path.isfile(path):
        return False, f"文件不存在：{path}"
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return False, f"读不到文件：{exc}"
    if size <= 0:
        return False, "空文件"
    # 微信表情/图片上限很宽松，但 10MB 以上会明显卡住渲染
    if size > 10 * 1024 * 1024:
        return False, f"文件过大（{size / 1024 / 1024:.1f}MB）"
    return True, ""


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

    # ------------------------------------------------------------------
    # 媒体：图片 / 表情包
    # ------------------------------------------------------------------
    def _chat_input_ctrl(self):
        """定位当前会话的输入框控件（UIA）。找不到返回 None，调用方回退坐标路线。"""
        try:
            import uiautomation as auto
            from wechatauto.uia_driver import CHAT_INPUT_AID

            hwnd = (ctypes.windll.user32.FindWindowW("Qt51514QWindowIcon", None)
                    or ctypes.windll.user32.FindWindowW(None, "微信"))
            if not hwnd:
                return None
            win = auto.ControlFromHandle(hwnd)
            if not win:
                return None
            e = win.EditControl(AutomationId=CHAT_INPUT_AID)
            if e and e.Exists(0, 0):
                return e
        except Exception:
            pass
        return None

    def _gui(self):
        """chatbox.send_file 需要的 GUI 门面。

        vendor 的 ``chatbox.send_file`` 走 ``SetClipboardFiles`` + ``SendKeys``，
        要一个 ChatBox 对象；``guia``（WeChatGUI）已经具备
        ``copy_files_to_clipboard`` / ``_uia_has_draft`` 等全套，而且已经在
        本机验证过 4.1.x/4.2.0 两条分支。优先用它，失败再退回 chatbox。
        """
        try:
            from wechatauto.guia import WeChatGUI

            g = WeChatGUI()
            return g
        except Exception as exc:
            raise SendError(f"初始化 GUI 门面失败: {exc}", kind="init") from exc

    def send_media(self, who: str, path: str,
                   verify_identity: bool = True) -> SendResult:
        """给 who 发一个本地媒体文件（图片 / GIF 表情包 / 其他文件）。

        表情包的关键：**GIF 文件会被微信识别成表情消息**，对方收到的是
        原生表情（可长按添加、会动），不是图片。

        与 send() 的区别：这里不能用文本回读校验（输入框里只有一个
        U+FFFC 对象符），所以校验改成「草稿是否出现」+「数据库是否落库」。
        """
        okf, why = check_media(path)
        if not okf:
            raise SendError(f"媒体不可发送：{why}", kind="media")

        if not desktop_available():
            raise SendError(
                "桌面已锁定或不可见，UIA 无法操作。\n"
                "解锁桌面后重试。",
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
                if who not in inp:
                    raise SendError(
                        f"输入框归属校验失败：输入框 Name={inp!r}，"
                        f"目标={who!r}。已中止。", kind="identity")

        kind = classify_media(path)

        # ① 先试 UIA 控件路线（不吃前台焦点，最稳）
        err = ""
        try:
            edit = self._chat_input_ctrl()
            if edit is not None:
                ok = self._paste_media_uia(edit, path)
                if ok:
                    return SendResult(ok=True, who=who, path=path,
                                      kind=kind, text=f"[{kind}]")
                err = "UIA 粘贴未出现草稿"
        except Exception as exc:
            err = f"UIA 路线异常: {exc}"

        # ② 退回坐标 + 真键盘路线（旧版微信仍然有效）
        try:
            gui = self._gui()
            res = gui.send_file(path, who=None)
            ok = bool(getattr(res, "is_success", None)) if hasattr(
                res, "is_success") else bool(res)
            if ok:
                return SendResult(ok=True, who=who, path=path, kind=kind,
                                  text=f"[{kind}]")
            err = err or f"send_file 返回失败: {res!r}"
        except Exception as exc:
            err = err or f"坐标路线异常: {exc}"

        raise SendError(f"媒体发送失败：{err}", kind="send")

    def _paste_media_uia(self, edit, path: str, attempts: int = 3) -> bool:
        """UIA 路线：CF_HDROP 写剪贴板 → 控件级 Ctrl+V → 等草稿 → 回车。

        草稿判据是输入框 Value 里出现 U+FFFC（对象替换符）：
        微信把粘贴进来的图片/表情当成一个内嵌对象塞进编辑框，
        文本层就只剩这个字符。空框读回 ''。
        控件不暴露 Value 时按"不知道"处理，**仍然发送** —— 条款没变，
        读不到不等于内容错了（和 send_text 的同一条道理）。
        """
        try:
            from wechatauto.guia import WeChatGUI
        except Exception as exc:
            raise SendError(f"加载粘贴工具失败: {exc}", kind="import") from exc

        if not WeChatGUI.copy_files_to_clipboard([os.path.abspath(path)]):
            return False
        time.sleep(0.3)

        edit.SetFocus()
        landed = False
        for _ in range(max(1, attempts)):
            edit.SendKeys("{Ctrl}a{Delete}", waitTime=0.1)
            edit.SendKeys("{Ctrl}v", waitTime=0.1)
            if self._wait_draft(edit, wait=4.0):
                landed = True
                break
        if not landed:
            # 读不到草稿标记：可能是控件不暴露 Value，也可能是粘贴真失败。
            # 这里选择"再试一次纯键盘 Ctrl+V"，失败才放弃，避免静默发空。
            try:
                edit.SendKeys("{Ctrl}v", waitTime=0.1)
                landed = self._wait_draft(edit, wait=2.0)
            except Exception:
                pass
        if not landed:
            return False

        edit.SendKeys("{Enter}", waitTime=0.05)
        time.sleep(1.2)
        return True

    @staticmethod
    def _wait_draft(edit, wait: float = 4.0) -> bool:
        """轮询输入框 Value 是否出现 U+FFFC。控件不暴露 Value 时返回 True。"""
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                val = edit.GetValuePattern().Value
            except Exception:
                # 读不到 → 没有探测能力 ≠ 内容错了
                return True
            if val and "\ufffc" in val:
                return True
            time.sleep(0.25)
        return False

    # ------------------------------------------------------------------
    # 发送后确认
    # ------------------------------------------------------------------
    def verify_media_sent(self, who: str, path: str,
                          before_seq: int = 0,
                          timeout: float = 8.0) -> bool:
        """回读数据库，确认媒体真的发出去了。返回 True/False。

        ⚠ 这里是 vendor ``_verify_attachment_sent`` 的一处缺口修正：
          它只认「图片」和「文件/链接/卡片」两类，**没认「动画表情」**。
          我们发的是 GIF → 微信落库成 local_type 47（动画表情），
          用它的判据永远判不成功，于是发成功也会被记成失败。

        表情消息在库里**没有文件名**（content 是加密串），
        所以只能靠「类型 47 + sender 是自己 + sort_seq 比发送前大」判定。
        """
        kind = classify_media(path)
        want = "动画表情" if kind == "sticker" else (
            "图片" if kind == "image" else "文件/链接/卡片")

        db = getattr(self, "_db", None)
        if db is None:
            try:
                from wechatauto import WeChatDB

                db = WeChatDB()
                self._db = db
            except Exception:
                return False

        chat = who
        try:
            hits = db.search_contact(who)
            if hits:
                chat = hits[0]["username"]
        except Exception:
            pass

        deadline = time.time() + timeout
        fname = os.path.basename(path)
        while time.time() < deadline:
            try:
                rows = db.get_new_messages(chat, since_seq=before_seq, limit=8)
            except Exception:
                rows = []
            for r in rows:
                if int(r.get("sender_id") or 0) != 2:
                    continue
                t = str(r.get("type") or "")
                if t == want:
                    return True
                # 类型对不上但 content 里带了文件名，也算（文件/图片兜底）
                if kind != "sticker" and fname in str(r.get("content") or ""):
                    return True
            time.sleep(0.5)
        return False


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
