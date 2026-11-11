# -*- coding: utf-8 -*-
"""声音核心：配置、缓存、合成、体检、失败语义。

它回答的问题
------------
1. 这一轮要不要出声？（由调用方/插件决定，这里只负责「怎么出」）
2. 用哪个音色、什么情境、什么参数？（`voice_director` 决定）
3. 声音真的出来了吗？（`voice_audio` 量指标，不合格不算成功）
4. 同一句话第二次还要不要花钱？（缓存，只存体检通过的）
5. 失败了怎么办？（返回结构化错误；绝不返回静音、绝不自动重试超时）

为什么先只接 Breeze
-------------------
它的返回形状最简单（二进制 WAV + `xi-api-key`），而且**情绪/情境是模型原生
支持的**。本地 GPT-SoVITS / CosyVoice 以后按同一个 `synthesize()` 接口加，
不在这一层写厂商分支。

和发送层的关系
--------------
这里只产出文件，不碰微信、不碰 UIA。发送在 `_deliver` 里另行处理。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import paths
from . import voice_audio as A
from . import voice_director as D

#: Breeze 文本上限（默认 1000；申请后最高 2000）。大肥鱼正文上限 300，够用。
MAX_CHARS = 1000
#: 单次请求超时。Breeze 实测 2~4 秒，90 秒是上限不是常态。
DEFAULT_TIMEOUT = 90.0
#: 缓存上限。语音是「同一句重复说」的重灾区，缓存能省很多积分。
DEFAULT_CACHE_MB = 200


class VoiceError(RuntimeError):
    """合成失败。带 status / hint / body，日志里能直接看出是哪一类。"""

    def __init__(self, message: str, *, status: int = 0, hint: str = "",
                 body: str = ""):
        super().__init__(message)
        self.status = status
        self.hint = hint
        self.body = body

    def human(self) -> str:
        return f"{self}｜{self.hint}" if self.hint else str(self)


_HINTS = {
    400: "请求格式不对：检查音色 ID、模型 ID、language_code。",
    401: "Key 不对或没带上。Breeze 用 xi-api-key: brz_...。",
    403: "Key 没有这个音色/模型的权限。",
    404: "音色不存在，或不属于这个账号。先 breeze voices 查一遍。",
    422: "参数不合规：常见是 Key 格式、language_code、voice_settings 越界。",
    429: "频率/并发超了。等 Retry-After 再试，别自动连发。",
    503: "共享容量忙。按 Retry-After 等，别自动重试扣两次积分。",
}


@dataclass
class VoiceSettings:
    #: 兼容旧配置；真正的开关是插件页的「声音」勾选。
    enabled: bool = True
    engine: str = "breezeblue"
    base_url: str = "https://api.breeze.blue"
    path: str = "/v1/text-to-speech/{voice}"
    api_key: str = ""
    model: str = "breeze-tts-2-multilingual"
    voice: str = ""
    language: str = "zh"
    speed: float = 1.0
    guidance: float = 2.0
    volume: float = 1.0
    output_format: str = "wav_24000"
    profile: str = "bright_young"
    #: 语音气泡：虚拟声卡播放端（CABLE Input）和微信录音快捷键
    device: str = ""
    hotkey: str = "Ctrl+Alt"
    timeout: float = DEFAULT_TIMEOUT
    cache_mb: int = DEFAULT_CACHE_MB
    #: 一轮正文超过这个长度就不合成（分条太多，念起来像播报）
    max_turn_chars: int = 400
    #: 什么时候出声的规则；结构见 voice_policy.VoicePolicy。
    policy: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: Optional[Mapping[str, Any]] = None,
                    path: Optional[str] = None) -> "VoiceSettings":
        if cfg is None:
            p = Path(path or paths.app_path("config.json"))
            try:
                data = json.loads(p.read_text("utf-8-sig"))
            except Exception:
                data = {}
            cfg = (data.get("voice") if isinstance(data, dict) else None) or {}
            if not cfg:
                # config.json 还没写过 voice 段：拿示例配置当默认值。
                # 地址/模型/音色可以先填好，Key 仍然要用户自己粘。
                try:
                    ex = json.loads(Path(paths.res_path("config.example.json"))
                                    .read_text("utf-8-sig"))
                    cfg = (ex.get("voice") if isinstance(ex, dict) else None) or {}
                except Exception:
                    cfg = {}
        cfg = dict(cfg or {})
        obj = cls()
        simple = ("engine", "base_url", "path", "api_key", "model", "voice",
                  "language", "output_format", "profile", "device", "hotkey")
        for key in simple:
            if cfg.get(key) not in (None, ""):
                setattr(obj, key, str(cfg[key]))
        for key in ("speed", "guidance", "volume", "timeout"):
            if cfg.get(key) not in (None, ""):
                setattr(obj, key, float(cfg[key]))
        if cfg.get("cache_mb") not in (None, ""):
            obj.cache_mb = int(cfg["cache_mb"])
        if cfg.get("max_turn_chars") not in (None, ""):
            obj.max_turn_chars = int(cfg["max_turn_chars"])
        if isinstance(cfg.get("policy"), dict):
            obj.policy = dict(cfg["policy"])
        if "enabled" in cfg:
            obj.enabled = bool(cfg["enabled"])
        if not obj.api_key:
            obj.api_key = os.environ.get("BREEZE_API_KEY", "").strip()
        return obj


@dataclass
class VoiceResult:
    ok: bool
    path: str = ""
    ms: int = 0
    cached: bool = False
    provider: str = ""
    duration: float = 0.0
    stat: Dict[str, Any] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)
    error: str = ""
    plan: Dict[str, Any] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        if not self.ok:
            return self.error
        return (f"{self.provider} {self.duration:.2f}s "
                f"{'缓存' if self.cached else str(self.ms) + 'ms'}")


class VoiceEngine:
    """一轮声音的完整流程：导演 → 合成 → 体检 → 缓存。"""

    def __init__(self, settings: VoiceSettings, *, data_root: Optional[str] = None,
                 log: Optional[Any] = None):
        self.settings = settings
        if not data_root:
            try:
                from . import vault
                data_root = vault.resolve_data_root()
            except Exception:
                data_root = paths.app_path("data")
        self.root = Path(data_root)
        self.cache_dir = self.root / "voice" / "cache"
        self.outbox = self.root / "voice" / "outbox"
        # 不在这里 mkdir：数据根可能是用户的真实目录（D:\大肥鱼），
        # 打开设置页不该顺手建一堆空文件夹。真正要写的时候再建。
        self._log = log or (lambda *_a, **_k: None)

    # ── 可用性 ─────────────────────────────────────────────────────
    def available(self) -> tuple[bool, str]:
        s = self.settings
        # 总开关是插件页的「声音」勾选，不是这里的 enabled 字段。
        # 这一层只回答「参数够不够合成」，免得两处开关打架。
        if not s.api_key:
            return False, "没有 api_key"
        if not s.voice:
            return False, "没有音色 voice"
        if not s.base_url:
            return False, "没有 base_url"
        return True, ""

    def health(self, timeout: float = 15.0) -> Dict[str, Any]:
        """启动预检：Key 能不能用、模型在不在。**不消耗生成额度**。"""
        ok, why = self.available()
        out: Dict[str, Any] = {"ok": False, "reason": why,
                               "model": self.settings.model,
                               "voice": self.settings.voice}
        if not ok:
            return out
        url = f"{self.settings.base_url.rstrip('/')}/v1/models"
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                models = json.loads(resp.read().decode("utf-8"))
            ids = [m.get("model_id") for m in models if isinstance(m, dict)]
            out.update(ok=True, models=ids,
                       model_ok=self.settings.model in ids)
            if not out["model_ok"]:
                out["reason"] = f"账号没有模型 {self.settings.model}"
            return out
        except urllib.error.HTTPError as exc:
            out["reason"] = f"HTTP {exc.code}: {self._short(exc.read())}"
            out["hint"] = _HINTS.get(exc.code, "")
            return out
        except Exception as exc:  # noqa: BLE001
            out["reason"] = f"{type(exc).__name__}: {exc}"
            return out

    # ── 对外：合成 ─────────────────────────────────────────────────
    def synthesize_text(self, text: str, **state: Any) -> VoiceResult:
        return self.synthesize_turn([text], **state)

    def synthesize_turn(self, parts: Sequence[str], *,
                        use_cache: bool = True, **state: Any) -> VoiceResult:
        """一轮（1~N 条）→ 一个音频文件。

        `state` 直接透传给 `voice_director`：mood / dims / life / hour /
        is_group / afterglow / neglect_level / user_text / requested / ...
        """
        ok, why = self.available()
        if not ok:
            return VoiceResult(False, error=why)

        plan = self._plan(parts, **state)
        if not plan.text:
            return VoiceResult(False, error="清洗后没有可念的文本")
        if len(plan.text) > MAX_CHARS:
            return VoiceResult(False, error=f"文本超过 {MAX_CHARS} 字，先切段",
                               plan=plan.as_dict())
        if len(plan.text) > self.settings.max_turn_chars:
            return VoiceResult(False, error="这一轮太长，不出声",
                               plan=plan.as_dict())

        key = self._cache_key(plan)
        cached = self.cache_dir / f"{key}.wav"
        meta = self.cache_dir / f"{key}.json"
        if use_cache and cached.exists():
            stat = A.inspect(cached)
            if not A.judge(stat, plan.text):
                return VoiceResult(True, str(cached), cached=True,
                                   provider=self.settings.engine,
                                   duration=stat["duration"], stat=stat,
                                   plan=plan.as_dict())
            self._log(f"[声] 缓存不合格，删掉重做：{cached.name}")
            cached.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)

        target = self.outbox / (
            f"voice-{time.strftime('%Y%m%d-%H%M%S')}-{key[:8]}.wav")
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()
        try:
            self._call(plan, target)
        except VoiceError as exc:
            return VoiceResult(False, error=exc.human(), plan=plan.as_dict())
        except Exception as exc:  # noqa: BLE001 —— 解码/磁盘/权限都算失败
            return VoiceResult(False, error=f"{type(exc).__name__}: {exc}",
                               plan=plan.as_dict())
        ms = int((time.perf_counter() - t0) * 1000)

        stat = A.inspect(target)
        problems = A.judge(stat, plan.text)
        if problems:
            target.unlink(missing_ok=True)
            return VoiceResult(False, error="产物体检不过：" + "；".join(problems),
                               stat=stat, problems=problems, plan=plan.as_dict())

        if use_cache:
            self._cache_put(target, cached, meta, plan, stat)
        self._prune_cache()
        return VoiceResult(True, str(target), ms=ms, provider=self.settings.engine,
                           duration=stat["duration"], stat=stat,
                           plan=plan.as_dict())

    # ── 导演 ───────────────────────────────────────────────────────
    def _plan(self, parts: Sequence[str], **state: Any) -> D.VoicePlan:
        ctx = D.VoiceContext(
            text="", user_text=str(state.get("user_text") or ""),
            mood=float(state.get("mood") or 65),
            dims=dict(state.get("dims") or {}),
            energy=float(state.get("energy") or 70),
            asleep=bool(state.get("asleep")),
            doing=str(state.get("doing") or ""),
            hour=int(state.get("hour") if state.get("hour") is not None
                     else time.localtime().tm_hour),
            is_group=bool(state.get("is_group")),
            is_owner=bool(state.get("is_owner", True)),
            afterglow=tuple(state.get("afterglow") or ()),
            neglect_level=int(state.get("neglect_level") or 0),
            requested=bool(state.get("requested")),
            wake=bool(state.get("wake")),
            proactive=bool(state.get("proactive")),
            scene=str(state.get("scene") or ""),
            profile=str(state.get("profile") or self.settings.profile),
        )
        return D.direct_turn(parts, ctx)

    # ── Breeze 请求 ────────────────────────────────────────────────
    def _headers(self) -> Dict[str, str]:
        return {"xi-api-key": self.settings.api_key,
                "Content-Type": "application/json"}

    @staticmethod
    def _short(body: bytes, limit: int = 300) -> str:
        return body.decode("utf-8", errors="replace").replace("\n", " ")[:limit]

    def _url(self, plan: D.VoicePlan) -> str:
        s = self.settings
        path = s.path.replace("{voice}", plan.voice or s.voice)
        base = s.base_url.strip().rstrip("/")
        # 用户可能把完整接口地址粘进来；剥掉到 /v1 为止，避免拼两次。
        if "/text-to-speech/" in base:
            base = base.split("/text-to-speech/")[0]
        if base.endswith("/v1") and path.startswith("/v1/"):
            path = path[3:]
        query = urllib.parse.urlencode({"output_format": s.output_format,
                                        "delivery": "sync"})
        return f"{base}{path}?{query}"

    def _body(self, plan: D.VoicePlan) -> bytes:
        s = self.settings
        vs: Dict[str, Any] = {"guidance_scale": round(float(plan.guidance), 2),
                              "speed": round(float(s.speed), 2),
                              "volume": round(float(s.volume), 2)}
        payload: Dict[str, Any] = {
            "text": plan.text,
            "model_id": s.model,
            "language_code": s.language,
            "voice_settings": vs,
        }
        if plan.instructions:
            payload["instructions"] = plan.instructions
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _call(self, plan: D.VoicePlan, target: Path) -> None:
        if self.settings.engine != "breezeblue":
            raise VoiceError(f"还没接这个引擎：{self.settings.engine}",
                             hint="目前只实现 breezeblue")
        url = self._url(plan)
        req = urllib.request.Request(url, data=self._body(plan), method="POST",
                                     headers=self._headers())
        tmp = target.with_suffix(".part")
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.settings.timeout) as resp:
                body = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                if not body:
                    raise VoiceError("响应体为空", status=getattr(resp, "status", 0))
                tmp.write_bytes(body)
                if body[:4] != b"RIFF":
                    raise VoiceError(f"返回的不是 WAV（Content-Type={ctype}）",
                                     body=self._short(body))
                A.canonicalize(tmp, target)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            raise VoiceError(f"HTTP {exc.code}: {self._short(raw)}",
                             status=exc.code, hint=_HINTS.get(exc.code, ""),
                             body=self._short(raw, 2000)) from exc
        except urllib.error.URLError as exc:
            raise VoiceError(f"连不上：{exc.reason}｜{url}",
                             hint="域名/代理/防火墙；把 api.breeze.blue 加进 NO_PROXY 试试") from exc
        except TimeoutError as exc:
            # 超时可能已经在对方扣了积分；**不自动重试**，交给上层决定
            raise VoiceError(f"超时（{self.settings.timeout}s）｜{url}",
                             hint="可能已扣积分，不自动重试") from exc
        finally:
            tmp.unlink(missing_ok=True)
            self._log(f"[声] {url.split('?')[0]} "
                      f"{int((time.perf_counter() - t0) * 1000)}ms")

    # ── 缓存 ───────────────────────────────────────────────────────
    def _cache_key(self, plan: D.VoicePlan) -> str:
        s = self.settings
        blob = json.dumps({
            "engine": s.engine, "model": s.model, "voice": plan.voice or s.voice,
            "language": s.language, "speed": s.speed, "guidance": plan.guidance,
            "volume": s.volume, "text": plan.text,
            "instructions": plan.instructions,
        }, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:20]

    def _cache_put(self, src: Path, dst: Path, meta: Path,
                   plan: D.VoicePlan, stat: Dict[str, Any]) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            data = src.read_bytes()
            tmp = dst.with_suffix(".part")
            tmp.write_bytes(data)
            os.replace(tmp, dst)
            meta.write_text(json.dumps({"plan": plan.as_dict(), "stat": stat},
                                       ensure_ascii=False, indent=2), "utf-8")
        except Exception as exc:  # noqa: BLE001
            self._log(f"[声] 缓存失败（不影响这一轮）：{exc}")

    def _prune_cache(self) -> None:
        limit = max(0, self.settings.cache_mb) * 1024 * 1024
        if not limit or not self.cache_dir.exists():
            return
        files = sorted(self.cache_dir.glob("*.wav"),
                       key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        while files and total > limit:
            old = files.pop(0)
            try:
                total -= old.stat().st_size
                old.unlink()
                old.with_suffix(".json").unlink(missing_ok=True)
            except OSError:
                break
