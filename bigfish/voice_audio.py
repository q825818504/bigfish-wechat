# -*- coding: utf-8 -*-
"""声音产物：读 WAV / 量指标 / 判合格 / 统一成 24k 单声道 PCM16。

为什么单独一个文件
------------------
「合成返回 200」和「真的有一段能听的声音」是两件事。没有这一层，
日志会一直说成功：文件可能是 0 字节、44 字节空 WAV、3 秒静音、
或者 80 个字被念成 2 秒。

只用标准库：Breeze 直接返回 WAV，不需要 numpy/soundfile。
（要接 mp3 的引擎时，在 provider 层先转成 WAV 再进来。）
"""
from __future__ import annotations

import math
import wave
from array import array
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TARGET_SR = 24000
TARGET_PEAK = 0.89


def read_wav(path: str | Path) -> Tuple[array, int, int]:
    """返回 (PCM16 样本, 采样率, 声道数)。只接受 16bit。"""
    with wave.open(str(path), "rb") as w:
        channels = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        if width != 2:
            raise ValueError(f"只支持 16bit PCM，当前 {width * 8}bit")
        raw = w.readframes(w.getnframes())
    samples = array("h")
    samples.frombytes(raw)
    return samples, rate, channels


def _mono(samples: array, channels: int) -> array:
    if channels <= 1:
        return samples
    out = array("h")
    for i in range(0, len(samples), channels):
        chunk = samples[i:i + channels]
        out.append(int(sum(chunk) / len(chunk)))
    return out


def _resample(samples: array, sr_in: int, sr_out: int) -> array:
    """线性重采样。只用于把非 24k 的引擎产物拉齐；Breeze 本身就是 24k。"""
    if sr_in == sr_out or not samples:
        return samples
    n_out = max(1, int(round(len(samples) * sr_out / sr_in)))
    out = array("h", [0]) * n_out
    step = sr_in / sr_out
    for i in range(n_out):
        pos = i * step
        j = int(pos)
        frac = pos - j
        a = samples[j] if j < len(samples) else samples[-1]
        b = samples[j + 1] if j + 1 < len(samples) else a
        out[i] = int(a + (b - a) * frac)
    return out


def normalized(samples: array, *, target_peak: float = TARGET_PEAK) -> array:
    """削峰 + 把过小的声音抬到能听清。不做 LUFS。"""
    if not samples:
        return samples
    peak = max(abs(min(samples)), abs(max(samples)))
    if peak == 0:
        return samples
    gain = 1.0
    if peak > target_peak * 32767:
        gain = target_peak * 32767 / peak
    else:
        rms = math.sqrt(sum(s * s for s in samples) / len(samples))
        if 0 < rms < 0.05 * 32767:
            gain = min(0.1 * 32767 / rms, 4.0)
    if gain == 1.0:
        return samples
    return array("h", [max(-32768, min(32767, int(s * gain))) for s in samples])


def write_wav(path: str | Path, samples: array, sr: int = TARGET_SR) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(samples.tobytes())


def canonicalize(src: str | Path, dst: str | Path,
                 *, sr: int = TARGET_SR) -> None:
    """任何 WAV → 24k / 单声道 / PCM16。失败就抛。"""
    samples, sr0, channels = read_wav(src)
    if not samples:
        raise ValueError(f"音频为空：{src}")
    samples = _mono(samples, channels)
    if sr0 != sr:
        samples = _resample(samples, sr0, sr)
    samples = normalized(samples)
    write_wav(dst, samples, sr)


def _speech_units(text: str) -> float:
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    latin = sum(1 for w in text.replace("\n", " ").split(" ") if w.strip())
    return cjk + latin * 2


def inspect(path: str | Path, *, frame_ms: int = 50) -> Dict[str, Any]:
    p = Path(path)
    try:
        samples, sr, channels = read_wav(path)
        samples = _mono(samples, channels)
    except Exception as exc:  # noqa: BLE001 —— 解不开也是结论
        return {"path": str(path), "bytes": p.stat().st_size if p.exists() else 0,
                "sample_rate": 0, "duration": 0.0, "rms_dbfs": -120.0,
                "peak": 0.0, "clipped_ratio": 0.0, "silence_ratio": 1.0,
                "readable": False, "error": f"{type(exc).__name__}: {exc}"}
    n = len(samples)
    dur = n / sr if sr else 0.0
    if n:
        rms = math.sqrt(sum(s * s for s in samples) / n)
        peak = max(abs(min(samples)), abs(max(samples)))
        clipped = sum(1 for s in samples if abs(s) > 32767 * 0.999) / n
    else:
        rms = peak = clipped = 0.0
    win = max(1, int(sr * frame_ms / 1000))
    frames = n // win
    silent = 0
    for i in range(frames):
        seg = samples[i * win:(i + 1) * win]
        r = math.sqrt(sum(s * s for s in seg) / len(seg)) if seg else 0
        if r < 0.01 * 32767:
            silent += 1
    return {
        "path": str(path),
        "bytes": p.stat().st_size if p.exists() else 0,
        "sample_rate": sr,
        "duration": round(dur, 3),
        "rms_dbfs": round(20 * math.log10(max(rms / 32767, 1e-9)), 1),
        "peak": round(peak / 32767, 4),
        "clipped_ratio": round(clipped, 5),
        "silence_ratio": round(silent / frames, 3) if frames else 0.0,
        "readable": True,
    }


def judge(stat: Dict[str, Any], text: str) -> List[str]:
    """返回问题清单。空 = 这条音频可以发。"""
    problems: List[str] = []
    if not stat.get("readable", True):
        problems.append("无法解码")
    dur = float(stat.get("duration") or 0)
    sr = int(stat.get("sample_rate") or 0)
    rms = float(stat.get("rms_dbfs") or -120)
    peak = float(stat.get("peak") or 0)
    sil = float(stat.get("silence_ratio") or 0)
    clip = float(stat.get("clipped_ratio") or 0)
    if stat.get("bytes", 0) < 60:
        problems.append("文件过小")
    if sr < 8000:
        problems.append(f"采样率过低({sr})")
    if dur < 0.2:
        problems.append(f"时长过短({dur:.2f}s)")
    if rms < -50:
        problems.append(f"近乎静音({rms}dBFS)")
    if rms > -1:
        problems.append(f"电平过高({rms}dBFS)")
    if peak >= 1.0 or clip > 0.01:
        problems.append(f"削波(peak={peak}, clip={clip:.1%})")
    if sil > 0.95:
        problems.append(f"几乎全是静音({sil:.0%})")
    units = _speech_units(text)
    if units >= 4 and dur > 0:
        cps = units / dur
        if cps > 14:
            problems.append(f"语速异常快({cps:.1f}字/秒，疑似截断)")
        elif cps < 1.0:
            problems.append(f"语速异常慢({cps:.1f}字/秒，疑似重复)")
    return problems
