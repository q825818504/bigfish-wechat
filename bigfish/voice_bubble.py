# -*- coding: utf-8 -*-
"""微信语音气泡发送（实验性）：按住微信录音键 + 把 WAV 播进虚拟声卡。

参考 whale_girl 的 `VoiceSendEngine` / `AutoVoiceWorker` / `RecordingShortcut`：

    本程序 → CABLE Input（播放）→ VB-CABLE → CABLE Output（微信麦克风）

关键顺序（少一步就会发出半截录音或发错人）：

1. 微信必须在前台，当前会话必须还是目标会话。
2. UIA 里必须找得到「按住 XXX 发语音」按钮，且快捷键与配置一致。
3. 先按下录音键，等 500ms 让微信进入录音态。
4. 把 WAV 播到 CABLE Input；播放期间持续校验窗口/会话没变。
5. 播完再松键；中途失败先给窗口发 ESC 取消录音，再松键。

⚠ 这是实验链路：个人微信版本、快捷键、UIA 控件名、虚拟声卡任何一个变了
都会失败。失败分两级：
  - 还没按录音键：`BubbleError(started=False)` → 可以安全改发文字。
  - 已经按了录音键：`BubbleError(started=True)` → **绝不补发文字**，
    因为对方可能已经收到一条半截语音。
"""
from __future__ import annotations

import ctypes
import re
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from . import voice_audio_out as _out

_user32 = ctypes.WinDLL("user32", use_last_error=True)
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
VK_ESCAPE = 0x1B
KEYEVENTF_KEYUP = 0x0002

_user32.GetForegroundWindow.restype = ctypes.c_void_p
_user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                 ctypes.c_void_p, ctypes.c_void_p]


class BubbleError(RuntimeError):
    def __init__(self, message: str, *, started: bool = False):
        super().__init__(message)
        self.started = bool(started)


@dataclass(frozen=True)
class RecordingShortcut:
    keys: List[int]
    label: str

    @classmethod
    def parse(cls, value: str) -> "RecordingShortcut":
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("请填写微信录音快捷键，例如 Ctrl+Alt")
        tokens = [t.strip() for t in raw.split("+")]
        if len(tokens) > 4 or any(not t for t in tokens):
            raise ValueError("快捷键格式无效")
        mods: List[int] = []
        labels: List[str] = []
        main: Optional[int] = None
        main_label = ""
        for token in tokens:
            key = token.upper()
            if key in ("CTRL", "CONTROL"):
                code, label = 0x11, "Ctrl"
            elif key == "ALT":
                code, label = 0x12, "Alt"
            elif key == "SHIFT":
                code, label = 0x10, "Shift"
            else:
                code, label = 0, ""
            if code:
                if code in mods:
                    raise ValueError("快捷键修饰键不能重复")
                mods.append(code)
                labels.append(label)
                continue
            if main is not None:
                raise ValueError("只能配置一个普通按键")
            if len(key) == 1 and (key.isalpha() or key.isdigit()):
                main, main_label = ord(key), key
            elif re.fullmatch(r"F(?:[1-9]|1[0-9]|2[0-4])", key):
                main = 0x70 + int(key[1:]) - 1
                main_label = key
            elif key in ("=", "OEMPLUS"):
                main, main_label = 0xBB, "="
            elif key == "TAB":
                main, main_label = 0x09, "Tab"
            else:
                raise ValueError("支持 Ctrl/Alt/Shift、Tab、字母、数字、F1-F24 和 =")
        keys = list(mods)
        if main is not None:
            keys.append(main)
            labels.append(main_label)
        if not keys:
            raise ValueError("快捷键不能为空")
        return cls(keys, "+".join(labels))

    def matches_button(self, text: str) -> bool:
        if "发语音" not in (text or ""):
            return False
        # 微信按钮文案可能是「按住 Ctrl+Alt 发语音」，也可能是
        # 「按住 Ctrl+Alt (发语音)」。只抓快捷键本身，别把「发语音」吃进去。
        m = re.search(r"按住\s*([A-Za-z0-9+=]+(?:\+[A-Za-z0-9+=]+)*)",
                      text or "")
        if not m:
            return False
        try:
            return self.parse(m.group(1)).keys == self.keys
        except ValueError:
            return False

    def press(self) -> None:
        for key in self.keys:
            _user32.keybd_event(key, 0, 0, 0)
            time.sleep(0.03)

    def release(self) -> None:
        for key in reversed(self.keys):
            try:
                _user32.keybd_event(key, 0, KEYEVENTF_KEYUP, 0)
            except Exception:
                pass
            time.sleep(0.02)


def _clean_chat(name: str) -> str:
    """输入框 Name 可能带「按住鼠标 语音输入文字」尾巴。"""
    return re.sub(r"(?:(?:长按|按住)鼠标\s*)?语音输入(?:文字)?$",
                  "", str(name or "").strip()).strip()


def _foreground() -> int:
    try:
        return int(_user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def _cancel_recording(hwnd: int) -> None:
    if not hwnd:
        return
    try:
        _user32.PostMessageW(ctypes.c_void_p(hwnd), WM_KEYDOWN,
                             ctypes.c_void_p(VK_ESCAPE), None)
        _user32.PostMessageW(ctypes.c_void_p(hwnd), WM_KEYUP,
                             ctypes.c_void_p(VK_ESCAPE), None)
    except Exception:
        pass


def _find_voice_button(win):
    """在 UIA 树里找「按住 X 发语音」按钮。"""
    try:
        btn = win.ButtonControl(SubName="发语音", Depth=20)
        if btn and btn.Exists(0.5, 0.1):
            return btn
    except Exception:
        pass
    # 某些微信版本 Name 是「按住 Ctrl+Tab 说话」；用正则从按钮里找。
    try:
        for ctrl in win.GetChildren():
            try:
                name = ctrl.Name or ""
            except Exception:
                continue
            if "发语音" in name and "按住" in name:
                return ctrl
    except Exception:
        pass
    return None


def _bring_to_front(sender, hwnd: int, who: str) -> None:
    """把微信窗口真正放到前台。

    `UiaSender.wake()` 只保证窗口**可见**；窗口在后台但可见时它直接返回，
    于是语音这边检查前台就会失败，而文字走 open_chat/输入框点击时会
    自己把窗口顶上来。语音不能比文字低一等，所以这里显式做同样的事。
    """
    if _foreground() == hwnd:
        return
    try:
        from .wechat_sender import force_show_wechat
    except Exception:
        force_show_wechat = None

    for wait in (0.6, 1.0):
        if force_show_wechat is not None:
            try:
                force_show_wechat(wait=wait)
            except Exception:
                pass
        if _foreground() == hwnd:
            return

    # 某些前台锁下 SetForegroundWindow 会被系统忽略；open_chat 会点会话，
    # 输入框聚焦后 Windows 通常会把窗口带到前面。
    try:
        sender.open_chat(who)
        time.sleep(0.3)
    except Exception:
        pass
    if _foreground() != hwnd:
        raise BubbleError("微信不在前台；已尝试置前但仍被系统挡住，"
                          "请解锁桌面/关闭全屏程序后重试")


def send_bubble(*, sender, who: str, wav: str, device: str,
                hotkey: str = "Ctrl+Alt",
                log: Optional[Callable[[str], None]] = None,
                check_interval: float = 0.08) -> float:
    """发一条语音气泡。返回音频时长；失败抛 BubbleError(started=...)。"""
    say = log or (lambda _m: None)
    try:
        shortcut = RecordingShortcut.parse(hotkey)
    except ValueError as exc:
        raise BubbleError(f"快捷键配置错误：{exc}") from exc

    if sender is None:
        raise BubbleError("微信发送器未就绪")
    try:
        sender.wake()
    except Exception as exc:
        raise BubbleError(f"微信 UIA 未就绪：{exc}") from exc
    uia = getattr(sender, "_uia", None)
    win = getattr(uia, "_win", None)
    hwnd = int(getattr(win, "NativeWindowHandle", 0)
               or getattr(uia, "hwnd", 0) or 0)
    if not uia or not win or not hwnd:
        raise BubbleError("找不到微信聊天窗口（UIA 树未物化）")

    chat = ""
    try:
        chat = _clean_chat(uia.current_chat() or "")
    except Exception:
        pass
    if who and chat != _clean_chat(who):
        # 语音只发当前会话；目标没打开就先打开，打不开就中止。
        try:
            sender.open_chat(who)
            chat = _clean_chat(uia.current_chat() or "")
        except Exception as exc:
            raise BubbleError(f"打不开目标会话「{who}」：{exc}") from exc
        if chat != _clean_chat(who):
            raise BubbleError(f"当前会话是「{chat}」，不是「{who}」")

    _bring_to_front(sender, hwnd, who)

    btn = _find_voice_button(win)
    if btn is None:
        raise BubbleError("找不到「发语音」按钮；请确认微信版本支持电脑端语音，"
                          "并已把发语音快捷键设在 UIA 能读到的位置")
    try:
        button_name = btn.Name or ""
    except Exception:
        button_name = ""
    if not shortcut.matches_button(button_name):
        raise BubbleError(
            f"微信的发语音快捷键与配置不一致（按钮：{button_name!r}，"
            f"配置：{shortcut.label}）。两边改成同一个组合再试")

    if _foreground() != hwnd:
        raise BubbleError("微信不在前台；已尝试置前但仍失败")

    try:
        seconds = _out.wav_duration(wav)
    except Exception as exc:
        raise BubbleError(f"读不了音频：{exc}") from exc
    if seconds < 1.0:
        raise BubbleError(f"音频只有 {seconds:.2f}s，微信可能不认；改发文字")
    if seconds > 50.0:
        raise BubbleError(f"音频 {seconds:.1f}s，超过 50 秒上限；改发文字")
    if _out.find_device(device) is None:
        raise BubbleError(f"找不到播放设备：{device or '（空）'}；"
                          "语音气泡需要 CABLE Input")

    started = False
    hold = False
    ok = False
    try:
        # 等用户松开自己可能还按着的键（最多 5 秒）
        deadline = time.monotonic() + 5.0
        while any(ctypes.windll.user32.GetAsyncKeyState(k) & 0x8000
                  for k in shortcut.keys):
            if time.monotonic() > deadline:
                raise BubbleError("请先松开录音快捷键")
            time.sleep(0.03)

        if _foreground() != hwnd:
            raise BubbleError("微信窗口在录音前切走了")
        hold = True
        shortcut.press()
        started = True
        time.sleep(0.5)

        def check() -> None:
            if _foreground() != hwnd:
                raise BubbleError("录音期间微信失去前台，已取消", started=True)
            try:
                now_chat = _clean_chat(uia.current_chat() or "")
            except Exception:
                now_chat = ""
            if who and now_chat and now_chat != _clean_chat(who):
                raise BubbleError("录音期间会话被切走，已取消", started=True)

        seconds = _out.play_wav(wav, device, check=check)
        time.sleep(0.35)
        check()
        ok = True
        say(f"[声] 语音气泡：{seconds:.2f}s → {device}")
        return seconds
    except BubbleError:
        raise
    except Exception as exc:
        raise BubbleError(f"语音气泡发送失败：{exc}", started=started) from exc
    finally:
        if hold:
            if started and not ok:
                # 没走完就取消：先 ESC，再松键，避免半截录音被发出去。
                _cancel_recording(hwnd)
            shortcut.release()
