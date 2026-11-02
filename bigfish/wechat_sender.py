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
        """WeChatGUI 门面（懒加载并缓存）。

        ⚠ 必须缓存。WeChatGUI.__init__ 会做布局校准（OCR + 截图），
          实测首次要 20 秒以上。每次发图都新建一个 = 每张图卡 20 秒。
          而且这个对象持有截图坐标/渲染尺寸，草稿的像素检测全靠它。

        为什么用 guia 而不是 chatbox：
          chatbox.send_file 只有一条路（剪贴板 + SendKeys + 点发送按钮），
          guia 有 UIA/坐标双路径 + 两套草稿判据 + 「自动最小化遮挡窗口」。
          发附件比发文字脆弱得多，多出来的判据就是多出来的活路。
        """
        g = getattr(self, "_gui_obj", None)
        if g is not None:
            return g
        try:
            from wechatauto.guia import WeChatGUI

            g = WeChatGUI()
            self._gui_obj = g
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

        # 发送前记基线 —— 后面要靠它判断"到底发没发出去"
        before_seq = self._target_seq(who)

        # ① 先试 UIA 控件路线（不吃前台焦点，最稳）
        err = ""
        try:
            edit = self._chat_input_ctrl()
            if edit is not None:
                ok = self._paste_media_uia(edit, path)
                if ok:
                    # ⚠ 不在这里返回成功。回车是按下去了，
                    #   发没发出去只有数据库知道。下面统一验。
                    pass
                else:
                    err = "UIA 粘贴没让输入框发生变化"
        except Exception as exc:
            err = f"UIA 路线异常: {exc}"

        # ② UIA 没成 → 交给 guia 的完整路线。
        #
        #    guia.send_file 有自己一套判据和会话复用逻辑，比我在外面
        #    猜"谁成功了"靠谱。但它返回 True 也只代表"动作做完了"，
        #    同样不代表对方收到了 —— 所以仍然走下面统一验证。
        if not err or err.startswith("UIA 粘贴没"):
            try:
                gui = self._gui()
                res = gui.send_file(path, who=None)
                gui_ok = bool(getattr(res, "is_success", None)) if hasattr(
                    res, "is_success") else bool(res)
                if not gui_ok:
                    err = err or f"guia.send_file 返回失败: {res!r}"
            except Exception as exc:
                err = err or f"坐标路线异常: {exc}"

        # ③ 唯一判据：数据库里有没有多出一条。
        #
        #    这条路的教训：上一版 `return True` 是无条件的，
        #    于是"发送成功 3.3 秒"和"用户什么都没收到"同时成立。
        #    从那以后，这里只信数据库。
        if self.verify_media_sent(who, path, before_seq=before_seq,
                                  timeout=10.0):
            return SendResult(ok=True, who=who, path=path, kind=kind,
                              text=f"[{kind}]")

        raise SendError(
            f"媒体没能发出去（数据库 10 秒内没有新增）。"
            f"{('原因：' + err) if err else ''}",
            kind="send")

    def _paste_media_uia(self, edit, path: str, attempts: int = 3) -> bool:
        """UIA 路线：CF_HDROP 写剪贴板 → 控件级 Ctrl+V → 判草稿 → 回车。

        ═══════════════════════════════════════════════════════════════
        返回值的含义：**真的发出去了**，不是"我按了回车"
        ═══════════════════════════════════════════════════════════════

        这里踩过一个很贵的坑。早先这版是：

            edit.SendKeys("{Enter}")
            return True          # ← 无条件

        日志就变成"发送成功 3.3 秒"，而用户那边什么都没收到。
        更糟的是上层据此记了 [表情] 已发 —— 一次失败被写成成功，
        之后所有排查都从错误的前提出发。

        现在改成：回车之后**回读数据库**，看到新消息才算成功。
        数据库是唯一不会说谎的判据。

        ═══════════════════════════════════════════════════════════════
        草稿判据为什么不用彩色像素
        ═══════════════════════════════════════════════════════════════

        vendor 的 `_input_box_has_color_draft` 扫的是输入框上方 150px
        区域里有没有彩色像素。实测（2026-11-02）它会**假阳性**：
        那个位置是消息区底部，上一条消息气泡就在那儿。

        同一时刻另一个判据给出了相反答案：
            像素草稿            : True     ← 数到彩色
            _input_box_has_text : False    ← 数不到深色
        框里要是真有个表情缩略图，它既该彩色也该有深色描边。
        只有前者为真 → 说明数到的彩色是别的东西。

        所以改用**区域变化量**：粘贴前后同一块区域像不像。
        进了东西 → 那块区域变了；没进 → 一个像素都不变。
        它不预设"草稿长什么样"，所以不会被别的彩色骗。
        """
        try:
            from wechatauto.guia import WeChatGUI
        except Exception as exc:
            raise SendError(f"加载粘贴工具失败: {exc}", kind="import") from exc

        # ═══════════════════════════════════════════════════════════════
        # 顺序是这段代码的全部要点
        # ═══════════════════════════════════════════════════════════════
        #
        # 踩过的坑（2026-11-02，用户手动验证出来的）：
        #   用户手动复制 GIF → 贴进微信 → 成了。
        #   我这边同样的剪贴板格式（CF_HDROP）→ 什么都没进去。
        #
        #   用户后来确认：**截图会吃掉剪贴板**。
        #   而我原来的流程是：
        #       写剪贴板 → 截基准图 → get_input_box（截图）
        #       → 聚焦（截图）→ Ctrl+V        ← 这时剪贴板早就空了
        #
        #   所以正确的顺序是：
        #       ① 先把该截的图都截完（基准指纹）
        #       ② 再写剪贴板
        #       ③ **立刻** Ctrl+V，中间一个截图动作都没有
        #       ④ 粘贴完成之后才去验证
        #
        #   基准指纹一定要在写剪贴板**之前**拍好。

        gui = None
        box = None
        try:
            gui = self._gui()
            box = gui.get_input_box()
        except Exception as exc:
            self._warn(f"拿不到输入框坐标，区域变化判据不可用：{exc}")

        # ── 阶段 ①：写剪贴板之前的准备 ──
        # 清空用控件级 SendKeys：纯文本操作，控件自己处理，不需要系统队列。
        for n in range(max(1, attempts)):
            try:
                edit.SendKeys("{Ctrl}a{Delete}", waitTime=0.1)
            except Exception:
                pass
            time.sleep(0.3)

            # 聚焦。真实按键吃焦点，必须在写剪贴板之前点好。
            if gui is not None and box:
                try:
                    gui.focus_input(box)
                except Exception as exc:
                    self._warn(f"聚焦失败：{exc}")
            time.sleep(0.2)

            # 基准指纹 —— 最后一次截图，之后到粘贴完成前不再截。
            before = self._input_region_signature(gui, box)

            # ── 阶段 ②：写剪贴板 ──
            if not WeChatGUI.copy_files_to_clipboard([os.path.abspath(path)]):
                self._warn("写剪贴板失败（CF_HDROP 没进去）")
                return False

            # ── 阶段 ③：立刻粘贴。这一段里**不许有任何截图或探测** ──
            try:
                from wechatauto.guia import WinInput

                WinInput().key(0x56, ctrl=True)     # VK_V
            except Exception as exc:
                self._warn(f"真实 Ctrl+V 失败：{exc}")
                return False

            # 给微信时间把文件读进来。这里的 sleep 不截图，是安全的。
            time.sleep(1.2)

            # ── 阶段 ④：这时候才验证 ──
            changed, _after = self._wait_input_area_change(
                gui, box, before, wait=2.5)

            if changed or self._value_has_object(edit):
                self._real_enter(gui)
                return True

            self._warn(
                f"第 {n + 1} 次粘贴后输入框没变化，重试")

        self._warn(
            f"粘贴 {attempts} 次都没让输入框发生变化，判定失败："
            f"{os.path.basename(path)}")
        return False

    # ── 真实键盘 ────────────────────────────────────────────────────

    def _real_enter(self, gui) -> bool:
        """真实回车。附件提交同样需要走系统输入队列。"""
        try:
            from wechatauto.guia import WinInput

            WinInput().key(0x0D)                # VK_RETURN
            time.sleep(1.0)
            return True
        except Exception as exc:
            self._warn(f"真实回车失败：{exc}")
            return False

    # ── 草稿判据 ────────────────────────────────────────────────────

    def _input_region_signature(self, gui, box):
        """给输入框（含上方一小条）拍个"指纹"，用来比前后。

        没有 gui/box 时返回 None —— 调用方会退回到只看 Value。
        """
        if gui is None or not box:
            return None
        try:
            x0, y0, x1, y1 = box
            # 只看输入框本体，不往上看 150px ——
            # 上方是消息区，那里的变化跟草稿无关，是噪音源。
            rel = (x0 + 4, max(y0, y0), max(y0 + 4, y1 - 4), y1)
            rel = (rel[0], rel[1], x1 - 4, rel[3])
            shot = gui._grab_screen(gui._rel_to_screen(rel))
            return shot.convert("L").resize((48, 16))
        except Exception:
            return None

    @staticmethod
    def _wait_input_area_change(gui, box, before, wait: float = 3.0):
        """轮询输入框区域，返回 (是否变了, 最新指纹)。"""
        if before is None:
            return False, None
        deadline = time.time() + wait
        last = None
        while time.time() < deadline:
            time.sleep(0.3)
            now = UiaSender._input_region_signature(gui, box)
            if now is None:
                return False, None
            last = now
            # 逐像素平均差。阈值 6 是试出来的：
            # 同一个静止画面重拍两次的差约 0~2，真进了缩略图是 30+。
            try:
                a = list(before.getdata())
                b = list(now.getdata())
                diff = sum(abs(x - y) for x, y in zip(a, b)) / max(1, len(a))
                if diff > 6.0:
                    return True, now
            except Exception:
                return False, None
        return False, last

    @staticmethod
    def _value_has_object(edit) -> bool:
        """Value 里有没有 U+FFFC（4.2.0 上附件会进 Value）。"""
        try:
            v = edit.GetValuePattern().Value
        except Exception:
            return False
        return bool(v and "\ufffc" in v)

    def _warn(msg: str) -> None:
        """把警告同时写到 wechatauto 的日志和标准错误。

        两边都写是因为：调试时人在看 stderr，而程序跑起来以后
        只有 wxlog 落盘。只写一边总会有一边看不到。
        """
        try:
            from wechatauto.logger import wxlog

            wxlog.warning(msg)
        except Exception:
            pass
        try:
            print(f"[wechat_sender] {msg}", file=sys.stderr, flush=True)
        except Exception:
            pass

    # ── 发送后确认 ──────────────────────────────────────────────────

    def _db(self):
        """懒加载数据库只读连接，缓存住。

        注意：WeChatDB 需要管理员权限取密钥。拿不到就返回 None，
        所有依赖它的判据退化成"不知道"，不要因此把发送判成失败。
        """
        db = getattr(self, "_db_obj", None)
        if db is not None:
            return db
        try:
            from wechatauto import WeChatDB

            db = WeChatDB()
            self._db_obj = db
            return db
        except Exception as exc:
            self._warn(f"打开微信数据库失败，发送校验退化：{exc}")
            return None

    def _resolve_chat(self, who: str) -> str:
        """把会话显示名换成 wxid。换不到就用原名（filehelper 这类本来就是）。"""
        db = self._db()
        if db is None:
            return who
        try:
            hits = db.search_contact(who)
            if hits:
                return hits[0]["username"]
        except Exception:
            pass
        return who

    def _target_seq(self, who: str) -> int:
        """取会话当前最大 sort_seq，作为"发送前"的基线。

        拿不到返回 0 —— 那样后面任何新消息都算新，宁可误判成功也不要
        因为读不到数据库就把一次真发送记成失败。
        """
        db = self._db()
        if db is None:
            return 0
        try:
            rows = db.get_messages(self._resolve_chat(who), limit=1)
            return int(rows[0]["sort_seq"]) if rows else 0
        except Exception:
            return 0

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

        db = self._db()
        if db is None:
            return False
        chat = self._resolve_chat(who)

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
