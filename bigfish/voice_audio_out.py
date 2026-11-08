# -*- coding: utf-8 -*-
"""把 WAV 播到指定的 Windows 播放设备（不依赖 sounddevice/pyaudio）。

为什么要自己写
--------------
发微信语音气泡的路线是：

    本程序 → CABLE Input（播放）→ VB-CABLE → CABLE Output（录音）→ 微信麦克风

`winsound` 只能播到系统默认设备，没法选 CABLE Input；`sounddevice`
会多带一个 PortAudio 依赖。这里直接用 winmm 的 `waveOut*`，只用 ctypes，
打包不增加任何东西，且能精确选设备。

只支持 PCM WAV（Breeze 返回的就是 16bit PCM）。播放是阻塞的，但每 80ms
回调一次 `check()`，调用方可以检查"微信会话有没有被切走"。
"""
from __future__ import annotations

import ctypes
import time
import wave
from ctypes import wintypes
from pathlib import Path
from typing import Callable, List, Optional

_winmm = ctypes.WinDLL("winmm", use_last_error=True)

WAVE_MAPPER = -1
WHDR_DONE = 0x00000001


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class WAVEHDR(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_void_p),
        ("dwBufferLength", wintypes.DWORD),
        ("dwBytesRecorded", wintypes.DWORD),
        ("dwUser", ctypes.c_void_p),
        ("dwFlags", wintypes.DWORD),
        ("dwLoops", wintypes.DWORD),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_void_p),
    ]


class WAVEOUTCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT),
        ("szPname", wintypes.WCHAR * 32),
        ("dwFormats", wintypes.DWORD),
        ("wChannels", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
        ("dwSupport", wintypes.DWORD),
    ]


class WAVEINCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT),
        ("szPname", wintypes.WCHAR * 32),
        ("dwFormats", wintypes.DWORD),
        ("wChannels", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
    ]


_winmm.waveOutGetNumDevs.restype = wintypes.UINT
_winmm.waveOutGetDevCapsW.argtypes = [ctypes.c_void_p,
                                      ctypes.POINTER(WAVEOUTCAPSW),
                                      wintypes.UINT]
_winmm.waveOutGetDevCapsW.restype = wintypes.UINT
_winmm.waveOutOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p),
                               ctypes.c_void_p,
                               ctypes.POINTER(WAVEFORMATEX),
                               ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
_winmm.waveOutOpen.restype = wintypes.UINT
_winmm.waveOutPrepareHeader.argtypes = [ctypes.c_void_p,
                                        ctypes.POINTER(WAVEHDR),
                                        wintypes.UINT]
_winmm.waveOutWrite.argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR),
                                wintypes.UINT]
_winmm.waveOutUnprepareHeader.argtypes = [ctypes.c_void_p,
                                          ctypes.POINTER(WAVEHDR),
                                          wintypes.UINT]
_winmm.waveOutReset.argtypes = [ctypes.c_void_p]
_winmm.waveOutClose.argtypes = [ctypes.c_void_p]
_winmm.waveInGetNumDevs.restype = wintypes.UINT
_winmm.waveInGetDevCapsW.argtypes = [ctypes.c_void_p,
                                     ctypes.POINTER(WAVEINCAPSW),
                                     wintypes.UINT]
_winmm.waveInGetDevCapsW.restype = wintypes.UINT


def list_output_devices() -> List[dict]:
    """所有播放设备。返回 [{id, name}]，id 可直接给 waveOutOpen。"""
    out: List[dict] = []
    n = int(_winmm.waveOutGetNumDevs())
    for dev in range(n):
        caps = WAVEOUTCAPSW()
        rc = _winmm.waveOutGetDevCapsW(ctypes.c_void_p(dev), ctypes.byref(caps),
                                       ctypes.sizeof(caps))
        if rc != 0:
            continue
        out.append({"id": dev, "name": str(caps.szPname).strip()})
    return out


def list_input_devices() -> List[dict]:
    """所有录音设备。CABLE Output 是虚拟声卡的录音端。"""
    out: List[dict] = []
    n = int(_winmm.waveInGetNumDevs())
    for dev in range(n):
        caps = WAVEINCAPSW()
        rc = _winmm.waveInGetDevCapsW(ctypes.c_void_p(dev), ctypes.byref(caps),
                                      ctypes.sizeof(caps))
        if rc != 0:
            continue
        out.append({"id": dev, "name": str(caps.szPname).strip()})
    return out


def cable_installed() -> bool:
    return any("cable" in d["name"].lower()
               for d in list_output_devices() + list_input_devices())


def find_device(name_or_id: str) -> Optional[dict]:
    """按名字（包含匹配）或 id 找设备。CABLE Input 常用名字匹配。"""
    raw = str(name_or_id or "").strip()
    devices = list_output_devices()
    if not raw:
        return devices[0] if devices else None
    for d in devices:
        if str(d["id"]) == raw:
            return d
    low = raw.lower()
    for d in devices:
        if low in d["name"].lower():
            return d
    return None


def wav_duration(path: str) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate() or 1)


def _read_pcm(path: str) -> tuple[bytes, WAVEFORMATEX, float]:
    with wave.open(str(path), "rb") as w:
        channels = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        frames = w.getnframes()
        raw = w.readframes(frames)
    if width != 2:
        raise ValueError(f"只支持 16bit PCM，当前 {width * 8}bit")
    if not raw:
        raise ValueError("音频为空")
    fmt = WAVEFORMATEX(
        wFormatTag=1, nChannels=channels, nSamplesPerSec=rate,
        nAvgBytesPerSec=rate * channels * 2,
        nBlockAlign=channels * 2, wBitsPerSample=16, cbSize=0)
    return raw, fmt, frames / float(rate or 1)


def play_wav(path: str, device: str = "", *,
             check: Optional[Callable[[], None]] = None,
             timeout_pad: float = 10.0) -> float:
    """把 WAV 播到 device（名字或 id），阻塞到播完。返回实际时长。"""
    dev = find_device(device)
    if dev is None:
        raise ValueError(f"找不到播放设备：{device or '（空）'}")
    raw, fmt, seconds = _read_pcm(path)
    if seconds <= 0:
        raise ValueError("音频时长为 0")

    handle = ctypes.c_void_p()
    rc = _winmm.waveOutOpen(ctypes.byref(handle), ctypes.c_void_p(dev["id"]),
                            ctypes.byref(fmt), None, None, 0)
    if rc != 0:
        raise OSError(f"waveOutOpen 失败：{rc}（设备 {dev['name']}）")

    buf = ctypes.create_string_buffer(raw, len(raw))
    hdr = WAVEHDR(lpData=ctypes.cast(buf, ctypes.c_void_p),
                  dwBufferLength=len(raw))
    prepared = False
    try:
        rc = _winmm.waveOutPrepareHeader(handle, ctypes.byref(hdr),
                                         ctypes.sizeof(hdr))
        if rc != 0:
            raise OSError(f"waveOutPrepareHeader 失败：{rc}")
        prepared = True
        rc = _winmm.waveOutWrite(handle, ctypes.byref(hdr),
                                 ctypes.sizeof(hdr))
        if rc != 0:
            raise OSError(f"waveOutWrite 失败：{rc}")
        deadline = time.monotonic() + seconds + timeout_pad
        while not (hdr.dwFlags & WHDR_DONE):
            if check is not None:
                check()
            if time.monotonic() > deadline:
                raise TimeoutError("音频播放超时")
            time.sleep(0.08)
        return seconds
    finally:
        try:
            _winmm.waveOutReset(handle)
        except Exception:
            pass
        if prepared:
            try:
                _winmm.waveOutUnprepareHeader(handle, ctypes.byref(hdr),
                                              ctypes.sizeof(hdr))
            except Exception:
                pass
        try:
            _winmm.waveOutClose(handle)
        except Exception:
            pass


def selfcheck() -> None:
    devices = list_output_devices()
    print(f"播放设备 {len(devices)} 个")
    for d in devices:
        print(f"  [{d['id']}] {d['name']}")
    if not any("cable" in d["name"].lower() for d in devices):
        print("没看到 CABLE；要发语音气泡需要先装 VB-CABLE")
