# -*- coding: utf-8 -*-
"""声音层自检（不花钱）。

它用本地假 Breeze 服务验证：
  · 文本清洗保护 [叹气] 这类标签
  · 导演层：深夜/低落/拌嘴/群聊，speed 固定 1.00
  · 请求形状：路径 voice、query output_format、xi-api-key、数字参数
  · 体检：静音/截断会被拒绝
  · 缓存：第二次不重复请求；坏产物不进缓存
  · 错误：401 翻成「Key 不对」

跑法：
    python tools/selftest_voice.py
"""
from __future__ import annotations

import array
import json
import math
import os
import sys
import tempfile
import threading
import urllib.parse
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bigfish import voice as V          # noqa: E402
from bigfish import voice_audio as A    # noqa: E402
from bigfish import voice_director as D  # noqa: E402
from bigfish import voice_text as T     # noqa: E402

FAILS: list[str] = []
STATE = {"body": {}, "headers": {}, "query": {}, "calls": 0}


def wav_bytes(seconds: float = 1.6, sr: int = 24000,
              silence: bool = False) -> bytes:
    n = int(seconds * sr)
    if silence:
        data = array.array("h", [0]) * n
    else:
        data = array.array("h", [
            int(0.25 * 32767 * math.sin(2 * math.pi * 220 * i / sr) *
                min(1.0, min(i / (0.05 * sr), (n - i) / (0.1 * sr))))
            for i in range(n)
        ])
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data.tobytes())
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def _send(self, code: int, body: bytes, ctype: str = "audio/wav") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/last":
            return self._send(200, json.dumps(STATE, ensure_ascii=False).encode(),
                              "application/json")
        return self._send(404, b"{}", "application/json")

    def do_POST(self) -> None:
        STATE["calls"] += 1
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        STATE["body"] = json.loads(raw.decode("utf-8")) if raw else {}
        STATE["headers"] = {k.lower(): v for k, v in self.headers.items()}
        parsed = urllib.parse.urlparse(self.path)
        STATE["query"] = {k: v[0] for k, v in
                          urllib.parse.parse_qs(parsed.query).items()}
        voice = parsed.path.rsplit("/", 1)[-1]
        if voice == "bad":
            return self._send(200, wav_bytes(2.0, silence=True))
        if voice == "short":
            return self._send(200, wav_bytes(0.1))
        if voice == "err401":
            return self._send(401, b'{"ok":false,"code":"AUTH_REQUIRED",'
                                   b'"detail":"Authentication required.",'
                                   b'"error":"Authentication required."}',
                              "application/json")
        return self._send(200, wav_bytes())


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"{'✓' if cond else '✗'} {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILS.append(label)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    tmp = Path(tempfile.mkdtemp(prefix="voice-selftest-"))

    # 1) 文本清洗：音频标签必须活着
    cleaned = T.clean_for_tts("还不睡吗？[呼气]我这边灯已经关了。")
    check("[呼气]" in cleaned, "清洗 · 保护音频标签", cleaned)
    check("@" not in T.clean_for_tts("@老爸 好呀"), "清洗 · 去掉 @")

    # 2) 导演：场景 + speed 固定 1.00
    late = D.from_state(text="还不睡吗？我这边灯已经关了。", mood=77,
                        dims={"sweet": 15}, hour=23,
                        profile="bright_young")
    check(late.scene == "late_night", "导演 · 深夜", late.scene)
    check(late.speed == 1.0, "导演 · 语速 1.00", str(late.speed))
    check("[呼气]" in late.text, "导演 · [呼气]", late.text)
    low = D.from_state(text="嗯，我听着。", mood=30, dims={"sad": 60},
                       profile="bright_young")
    check(low.scene == "low" and "[叹气]" in low.text, "导演 · 低落")
    check("压低嗓子" in low.instructions, "导演 · 薄荷不吃低沉", low.instructions[:30])
    conflict = D.from_state(text="知道了，我先不说了。", afterglow=("conflict",),
                            mood=42, profile="bright_young")
    check(conflict.scene == "conflict" and "俏皮" in conflict.instructions,
          "导演 · 拌嘴")
    group = D.from_state(text="我也觉得这个可以。", is_group=True,
                         is_owner=False, mood=70, profile="bright_young")
    check(group.scene == "group_public", "导演 · 群里", group.scene)

    # 2.5) 要不要出声
    from bigfish import voice_policy as P
    pol = P.VoicePolicy(mode="auto", probability=1.0, cooldown=180,
                        max_chars=220, scenes=dict(P.DEFAULT_SCENES))
    check(not P.decide(text="", policy=pol).want, "策略 · 空文本")
    check(not P.decide(text="嗯", policy=P.VoicePolicy(mode="off")).want,
          "策略 · 总开关")
    check(not P.decide(text="字" * 260, policy=pol).want, "策略 · 太长")
    check(P.decide(text="嗯", policy=pol, requested=True).want,
          "策略 · 对方要语音")
    check(not P.decide(text="嗯", policy=pol, last_voice_at=100,
                       now=101).want, "策略 · 冷却")
    tagged = P.decide(text="[[voice]] 嗯，我在", policy=pol)
    check(tagged.want and "[[voice]]" not in tagged.text,
          "策略 · 标记并清掉", tagged.text)
    check(not P.decide(text="[[text]] 嗯，我在", policy=pol).want,
          "策略 · [[text]] 压住概率")
    check(not P.decide(text="嗯", policy=pol, is_group=True).want,
          "策略 · 群里默认不发声")
    check(not P.decide(text="看这个 https://a.b/c", policy=pol).want,
          "策略 · 链接转文字")
    check(not P.decide(text="嗯", policy=pol, scene="conflict").want,
          "策略 · 拌嘴默认不出声")
    prev = P.decide(text="嗯", policy=P.VoicePolicy(mode="preview",
                                                    probability=1.0))
    check(prev.want and prev.preview, "策略 · 预演只判定")
    check(not P.decide(text="嗯", policy=P.VoicePolicy(mode="request")).want,
          "策略 · request 没要求不出")
    check(P.decide(text="嗯", policy=pol).delivery == "voice_only",
          "策略 · 命中后只发语音")
    check(P.decide(text="字" * 150, policy=pol).want,
          "策略 · 150字仍可语音（长文本）")
    check(not P.decide(text="字" * 260, policy=pol).want,
          "策略 · 超过220字改文字")

    # 2.6) 气泡基础件：设备枚举 + 快捷键解析（不播放、不按键盘）
    from bigfish import voice_audio_out as AO
    from bigfish.voice_bubble import RecordingShortcut
    devices = AO.list_output_devices()
    check(isinstance(devices, list), "气泡 · 能枚举播放设备", f"{len(devices)} 个")
    sc = RecordingShortcut.parse("Ctrl+Alt")
    check(sc.keys == [0x11, 0x12] and sc.label == "Ctrl+Alt",
          "气泡 · 快捷键解析")
    check(sc.matches_button("按住 Ctrl+Alt 发语音"), "气泡 · 按钮文案匹配")
    check(not sc.matches_button("按住 Ctrl+Tab 发语音"),
          "气泡 · 快捷键不一致要拒绝")
    from bigfish import voice_driver as VD
    dst = VD.package_status()
    check(bool(dst.get("ok") or dst.get("installed")),
          "驱动 · 内置 VB-CABLE 包可校验",
          f"{dst.get('files', 0)} 文件 / installed={dst.get('installed')}")

    # 3) 核心：真实请求形状（打到假服务）
    settings = V.VoiceSettings(
        enabled=True, base_url=base, api_key="brz_test", voice="voc_test",
        model="breeze-tts-2-multilingual", speed=1.0, profile="bright_young",
        cache_mb=10,
    )
    engine = V.VoiceEngine(settings, data_root=str(tmp))
    res = engine.synthesize_turn(
        ["还不睡吗？我这边灯已经关了。"], mood=77, dims={"sweet": 15},
        hour=23, doing="在沙发上看书")
    check(res.ok, "合成 · 成功", res.summary)
    check(res.duration > 0.2, "合成 · 时长", f"{res.duration:.2f}s")
    check(STATE["query"].get("output_format") == "wav_24000",
          "请求 · output_format", str(STATE["query"]))
    check(STATE["headers"].get("xi-api-key") == "brz_test",
          "请求 · xi-api-key")
    body = STATE["body"]
    check(body.get("model_id") == "breeze-tts-2-multilingual",
          "请求 · 模型", str(body.get("model_id")))
    check(body.get("language_code") == "zh", "请求 · language_code")
    check("[呼气]" in str(body.get("text")), "请求 · 标签", str(body.get("text")))
    check("深夜" in str(body.get("instructions")), "请求 · 情境指令",
          str(body.get("instructions"))[:30])
    vs = body.get("voice_settings") or {}
    check(vs.get("speed") == 1.0, "请求 · speed 1.00", str(vs))
    check(isinstance(vs.get("guidance_scale"), (int, float)),
          "请求 · guidance 是数字", str(vs))

    # 4) 缓存：第二次不应再发请求
    calls = STATE["calls"]
    res2 = engine.synthesize_turn(
        ["还不睡吗？我这边灯已经关了。"], mood=77, dims={"sweet": 15},
        hour=23, doing="在沙发上看书")
    check(res2.ok and res2.cached, "缓存 · 命中", res2.summary)
    check(STATE["calls"] == calls, "缓存 · 没有重复请求",
          f"calls={STATE['calls']}")

    # 5) 坏产物：静音/截断必须被拒绝，且不进缓存
    for bad, label in (("bad", "静音"), ("short", "截断")):
        s = V.VoiceSettings(enabled=True, base_url=base, api_key="k",
                            voice=bad, model="breeze-tts-2-multilingual",
                            speed=1.0, profile="bright_young",
                            cache_mb=10)
        e = V.VoiceEngine(s, data_root=str(tmp / bad))
        r = e.synthesize_turn(["今天有点累，不过看到你的消息好了一点。"],
                              mood=60, profile="bright_young")
        check(not r.ok, f"体检 · {label}拒绝", r.error[:50])
        check(not list((tmp / bad / "voice" / "cache").glob("*.wav")),
              f"缓存 · {label}不进缓存")

    # 6) 401 翻译
    s = V.VoiceSettings(enabled=True, base_url=base, api_key="bad",
                        voice="err401", model="breeze-tts-2-multilingual",
                        speed=1.0)
    e = V.VoiceEngine(s, data_root=str(tmp / "err"))
    r = e.synthesize_turn(["测试"])
    check(not r.ok and "Key 不对" in r.error, "错误 · 401", r.error[:60])

    server.shutdown()
    print()
    if FAILS:
        print(f"{len(FAILS)} 项没通过：")
        for f in FAILS:
            print("  -", f)
        return 1
    print("声音层自检全过（未调用真实 API，不消耗积分）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
