"""[bigfish] 回复生成 —— 调模型 API。

上下文组装顺序（这个顺序是有讲究的）：

    system = 人格（灵魂）+ 这会儿的状态（情绪）
    user   = 记住的事（记忆检索）+ 本轮怎么答（协议）+ 对话记录

人格和情绪放 system：它们描述「我是谁、我现在什么状态」，
在整个会话里稳定。
记忆和协议放 user：它们是「这一轮相关的信息」，每轮都变。

不把协议写进人格，是为了让灵魂保持单纯——它只该定义「我是谁」，
不该被输出格式污染。

## 三种协议

以前只有 OpenAI 兼容一条路，默认还指向本机 Magpie 网关 —— 换个用户
或换台电脑就配不出来。现在按 `bigfish/providers.py` 的预设走，
支持 openai / anthropic / gemini 三种请求格式。

配置优先读 `config.json` 的 `llm` 段：

    "llm": {
      "channel": "deepseek",        // 预设 id，或 custom / magpie
      "base_url": "",               // 空 = 用预设的
      "model": "deepseek-v4-flash",
      "api_key": "sk-...",
      "protocol": "openai",         // 空 = 用预设的
      "temperature": 1.0,
      "max_tokens": 2600,
      "timeout": 90
    }

老的顶层 `endpoint` / `model` / `api_key` 仍然认（自动映射成 custom），
所以升级不会把现有配置弄丢。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from typing import Any, Dict, Optional, Tuple

from bigfish.bridge_client import Context, load_context
from bigfish import providers
from bigfish.soul import Soul

# 老的默认值。只在没有任何配置时兜底 —— 现在默认走 deepseek 预设，
# 因为「本机 Magpie 网关」这个默认值对别人等于不可用。
DEFAULT_ENDPOINT = "http://127.0.0.1:3425/v1/chat/completions"
DEFAULT_MODEL = "workbuddy-ai/deepseek-v4.1-flash"


@dataclass
class ModelConfig:
    #: 预设 id（deepseek / anthropic / custom / magpie …）
    channel: str = providers.DEFAULT_CHANNEL
    #: 请求前缀。空 = 用预设的
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    #: openai / anthropic / gemini。空 = 用预设的
    protocol: str = ""
    temperature: float = 1.0
    # 2600 而不是 1600：当前默认模型是**推理模型**（deepseek-v4.x 系列），
    # 它先在 reasoning_content 里思考、再写正文，两者共用同一份 token 预算。
    # 1600 时实测：短回复够用（13~16 字，4 秒），但提示词一长
    # （记忆 + 事实 + 对话历史）就会把预算吃光，正文返回 None，
    # call_model 抛「模型只输出了推理、正文为空」——看起来像"她不回复了"。
    # 2600 给推理留了足够余量，普通聊天成本几乎不变（实际用多少算多少）。
    max_tokens: int = 2600
    timeout: float = 90.0

    # ------------------------------------------------------------------
    # 生效值（预设 + 覆盖）
    # ------------------------------------------------------------------
    @property
    def preset(self) -> providers.Preset:
        return providers.get(self.channel)

    @property
    def eff_base_url(self) -> str:
        return providers.normalize_base(self.base_url) or self.preset.base_url

    @property
    def eff_protocol(self) -> str:
        p = (self.protocol or "").strip().lower()
        if p in providers.PROTOCOLS:
            return p
        return self.preset.protocol or "openai"

    @property
    def eff_model(self) -> str:
        if self.model:
            return self.model
        ms = self.preset.models
        return ms[0] if ms else ""

    @property
    def url(self) -> str:
        return providers.chat_url(self.eff_base_url, self.eff_protocol,
                                  self.eff_model)

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: str = "") -> "ModelConfig":
        """加载顺序：config.json 的 llm 段 > 老顶层字段 > 环境变量 > 默认值。

        config.json 放第一，是因为它才是用户看得见、能改的那份；
        环境变量留给临时覆盖（CI、调试）。
        """
        cfg = cls()

        # 1) config.json
        p = path or os.path.join(
            __import__("bigfish.paths", fromlist=["app_root"]).app_root(),
            "config.json")
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f)
                llm = d.get("llm")
                if isinstance(llm, dict):
                    cfg.channel = str(llm.get("channel") or cfg.channel)
                    cfg.base_url = str(llm.get("base_url") or "")
                    cfg.model = str(llm.get("model") or "")
                    cfg.api_key = str(llm.get("api_key") or "")
                    cfg.protocol = str(llm.get("protocol") or "")
                    cfg.temperature = float(
                        llm.get("temperature", cfg.temperature))
                    cfg.max_tokens = int(llm.get("max_tokens", cfg.max_tokens))
                    cfg.timeout = float(llm.get("timeout", cfg.timeout))
                else:
                    cfg._load_legacy(d)
            except Exception:
                pass

        # 2) 环境变量覆盖
        cfg.api_key = (os.environ.get("BIGFISH_API_KEY")
                       or cfg.api_key)
        if os.environ.get("BIGFISH_ENDPOINT"):
            # 老变量：整个接口地址。转成 base_url + custom
            cfg.channel = "custom"
            cfg.base_url = providers.normalize_base(
                os.environ["BIGFISH_ENDPOINT"])
        if os.environ.get("BIGFISH_MODEL"):
            cfg.model = os.environ["BIGFISH_MODEL"]
        if os.environ.get("BIGFISH_CHANNEL"):
            cfg.channel = os.environ["BIGFISH_CHANNEL"]

        return cfg

    def _load_legacy(self, d: Dict[str, Any]) -> None:
        """老的顶层字段 → 新结构。

        老配置长这样：

            { "endpoint": "https://api.deepseek.com/v1/chat/completions",
              "model": "deepseek-v4-flash", "api_key": "sk-..." }

        读进来以后当成 custom 渠道，地址剥成前缀。这样升级后立刻能用，
        用户不用手动迁移。
        """
        ep = str(d.get("endpoint") or "").strip()
        md = str(d.get("model") or "").strip()
        key = str(d.get("api_key") or "").strip()
        if not (ep or md or key):
            return

        cfg_default = ModelConfig()
        self.temperature = float(d.get("temperature", cfg_default.temperature))
        self.max_tokens = int(d.get("max_tokens", cfg_default.max_tokens))
        self.timeout = float(d.get("timeout", cfg_default.timeout))

        # 老默认值（本机 Magpie）原样保留，让它继续能用
        if ep and "127.0.0.1:3425" in ep:
            self.channel = "magpie"
            self.model = md or DEFAULT_MODEL
            self.api_key = key
            return

        self.channel = "custom"
        self.base_url = providers.normalize_base(ep) if ep else ""
        self.model = md
        self.api_key = key
        # 老的 endpoint 可能本来就是 Anthropic 的
        if "/messages" in ep:
            self.protocol = "anthropic"

    # ------------------------------------------------------------------
    @property
    def needs_key(self) -> bool:
        """这个渠道需要 key 吗。

        不再靠"地址里有没有 127.0.0.1"猜 —— 那是猜不准的
        （内网中转站也不需要 key，而远端 Ollama 需要）。
        现在按预设声明 + 地址本身判断。
        """
        if self.preset.is_local:
            return False
        base = self.eff_base_url
        return not ("127.0.0.1" in base or "localhost" in base)

    @property
    def ready(self) -> bool:
        if not self.eff_base_url or not self.eff_model:
            return False
        return bool(self.api_key) or not self.needs_key

    def status(self) -> Tuple[bool, str]:
        """能不能用 + 一句话说明。给界面和自检用。"""
        if not self.eff_base_url:
            return False, "没有接口地址"
        if not self.eff_model:
            return False, "没有填模型 ID"
        if self.needs_key and not self.api_key:
            hint = self.preset.key_hint or "该渠道需要 API Key"
            return False, f"缺 API Key（{hint}）"
        return True, f"{self.preset.name} / {self.eff_model}"


class ModelError(RuntimeError):
    """一次模型调用失败。

    `retryable` 决定 `call_model` 要不要重试 —— 只有「换个时间可能成功」的
    错误才值得重试：网络抖动、限流、网关 5xx、响应解析失败，以及推理模型
    把预算吃在 reasoning 上导致正文为空。

    key 错 / 模型名错 / 余额不足这类 4xx 重试多少次都一样，直接抛。
    """

    def __init__(self, message: str, *, status_code: int = 0,
                 retryable: bool = False, kind: str = "generic") -> None:
        super().__init__(message)
        self.status_code = int(status_code or 0)
        self.retryable = bool(retryable)
        self.kind = kind


#: 值得重试的 HTTP 状态：限流 / 网关错误 / 服务端临时不可用
RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
#: 默认尝试次数（含第一次）
DEFAULT_ATTEMPTS = 3
#: 退避基数：1s → 2s → 4s（封顶）
RETRY_BACKOFF = (1.0, 2.0, 4.0)

#: 最近一次失败（给界面显示"她为什么没回"）。空 = 最近一次成功。
_LAST_ERROR: Dict[str, Any] = {}


def last_error() -> Dict[str, Any]:
    """最近一次模型调用失败。空 dict 表示最近一次是成功的。"""
    return dict(_LAST_ERROR)


def _note_error(exc: "ModelError") -> None:
    _LAST_ERROR.clear()
    _LAST_ERROR.update({
        "at": int(time.time()),
        "time": time.strftime("%m-%d %H:%M", time.localtime()),
        "kind": exc.kind,
        "status": exc.status_code,
        "message": str(exc)[:200],
    })


def _clear_error() -> None:
    _LAST_ERROR.clear()


# ══════════════════════════════════════════════════════════════════
#  三种协议的请求体
# ══════════════════════════════════════════════════════════════════
def _body_openai(cfg: ModelConfig, system: str, user: str) -> Dict[str, Any]:
    return {
        "model": cfg.eff_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        "stream": False,
    }


def _body_anthropic(cfg: ModelConfig, system: str, user: str) -> Dict[str, Any]:
    # ⚠ system 是**顶层字段**，不是 messages 里的一条。
    #   塞进 messages 会被拒（400: unexpected role "system"）。
    return {
        "model": cfg.eff_model,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
    }


def _body_gemini(cfg: ModelConfig, system: str, user: str) -> Dict[str, Any]:
    return {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": cfg.temperature,
            "maxOutputTokens": cfg.max_tokens,
        },
    }


def _headers(cfg: ModelConfig) -> Dict[str, str]:
    h = {"Content-Type": "application/json"}
    proto = cfg.eff_protocol
    if proto == "anthropic":
        # Anthropic 用 x-api-key，不是 Bearer
        if cfg.api_key:
            h["x-api-key"] = cfg.api_key
        h["anthropic-version"] = "2023-06-01"
    elif proto == "gemini":
        # Gemini 的 key 走查询参数，不放请求头
        pass
    else:
        if cfg.api_key:
            h["Authorization"] = f"Bearer {cfg.api_key}"
    return h


def _request_url(cfg: ModelConfig) -> str:
    u = cfg.url
    if cfg.eff_protocol == "gemini" and cfg.api_key:
        sep = "&" if "?" in u else "?"
        u = f"{u}{sep}key={urllib.parse.quote(cfg.api_key)}"
    return u


# ══════════════════════════════════════════════════════════════════
#  三种协议的响应解析
# ══════════════════════════════════════════════════════════════════
def _parse_openai(payload: Dict[str, Any]) -> Tuple[str, str]:
    msg = payload["choices"][0]["message"]
    return (str(msg.get("content") or "").strip(),
            str(msg.get("reasoning_content") or ""))


def _parse_anthropic(payload: Dict[str, Any]) -> Tuple[str, str]:
    blocks = payload.get("content") or []
    text, thinking = [], []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            text.append(str(b.get("text") or ""))
        elif b.get("type") in ("thinking", "redacted_thinking"):
            thinking.append(str(b.get("thinking") or ""))
    return "".join(text).strip(), "".join(thinking)


def _parse_gemini(payload: Dict[str, Any]) -> Tuple[str, str]:
    cands = payload.get("candidates") or []
    if not cands:
        # 被安全策略拦掉时没有 candidates，会带 promptFeedback
        fb = payload.get("promptFeedback") or {}
        raise ModelError(f"没有 candidates（可能被安全策略拦了）：{fb}")
    parts = ((cands[0].get("content") or {}).get("parts") or [])
    text, thinking = [], []
    for p in parts:
        if not isinstance(p, dict):
            continue
        if p.get("thought"):
            thinking.append(str(p.get("text") or ""))
        else:
            text.append(str(p.get("text") or ""))
    return "".join(text).strip(), "".join(thinking)


_PARSERS = {
    "openai": _parse_openai,
    "anthropic": _parse_anthropic,
    "gemini": _parse_gemini,
}


def _call_once(cfg: ModelConfig, system: str, user: str) -> str:
    """发一次请求。失败抛带 retryable 标记的 ModelError。"""
    proto = cfg.eff_protocol
    if proto == "anthropic":
        body = _body_anthropic(cfg, system, user)
    elif proto == "gemini":
        body = _body_gemini(cfg, system, user)
    else:
        body = _body_openai(cfg, system, user)

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(_request_url(cfg), data=data, method="POST",
                                 headers=_headers(cfg))
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
        raise ModelError(
            _http_hint(exc.code, detail, cfg),
            status_code=exc.code,
            retryable=exc.code in RETRY_STATUS,
            kind="http",
        ) from exc
    except urllib.error.URLError as exc:
        raise ModelError(_net_hint(exc, cfg), retryable=True, kind="network") from exc
    except TimeoutError as exc:
        raise ModelError(f"超时 {cfg.timeout:.0f}s：{cfg.eff_base_url}",
                         retryable=True, kind="network") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        # 网关偶尔返回 HTML 502 页面 —— 这是"这次不行"，不是"永远不行"
        raise ModelError(f"响应不是 JSON：{raw[:300]}", retryable=True,
                         kind="parse") from exc

    try:
        content, reasoning = _PARSERS[proto](payload)
    except ModelError:
        raise
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ModelError(f"响应结构不认识（{proto}）：{raw[:300]}",
                         retryable=True, kind="parse") from exc

    if content:
        return content
    if reasoning:
        # 推理模型把 token 预算吃在 thinking 上。重试时 call_model 会加大预算。
        raise ModelError(
            f"模型只输出了推理、正文为空（max_tokens={cfg.max_tokens} 太小？）"
            f" reasoning 前 100 字: {reasoning[:100]}",
            retryable=True, kind="empty_content")
    return ""


def _trace(cfg: ModelConfig, system: str, user: str, response: str,
           elapsed_ms: float, meta: Optional[Dict[str, Any]],
           error: Optional[ModelError] = None) -> None:
    """把这一轮请求写进 data/prompts（旁路，失败绝不影响聊天）。"""
    if not meta:
        return
    try:
        from bigfish import prompt_trace

        prompt_trace.record(cfg=cfg, system=system, user=user,
                            response=response, elapsed_ms=elapsed_ms,
                            meta=meta, error=error)
    except Exception:
        pass


def call_model(cfg: ModelConfig, system: str, user: str, *,
               attempts: int = 0,
               trace_meta: Optional[Dict[str, Any]] = None) -> str:
    """调模型，返回纯文本回复；可重试的错误自动重试。

    attempts=0 表示用默认 3 次（含第一次）。trace_meta 非空时写一份请求快照，
    见 `bigfish/prompt_trace.py`。
    """
    ok, why = cfg.status()
    if not ok:
        raise ModelError(f"模型没配好：{why}", kind="config")

    n = max(1, int(attempts or DEFAULT_ATTEMPTS))
    started = time.time()
    last: Optional[ModelError] = None
    cur = cfg

    for i in range(n):
        try:
            text = _call_once(cur, system, user)
        except ModelError as exc:
            last = exc
            if not exc.retryable or i == n - 1:
                break
            # 推理吃光预算：加 max_tokens 再试，而不是原样重试
            if exc.kind == "empty_content":
                bumped = int(cur.max_tokens * 1.5)
                if bumped > cur.max_tokens:
                    cur = replace(cur, max_tokens=bumped)
            time.sleep(RETRY_BACKOFF[min(i, len(RETRY_BACKOFF) - 1)])
            continue
        else:
            _clear_error()
            _trace(cfg, system, user, text, (time.time() - started) * 1000,
                   trace_meta)
            return text

    elapsed_ms = (time.time() - started) * 1000
    _trace(cfg, system, user, "", elapsed_ms, trace_meta, error=last)
    if last:
        _note_error(last)
        raise last
    raise ModelError("模型调用失败", kind="unknown")


def _net_hint(exc: Exception, cfg: ModelConfig) -> str:
    reason = getattr(exc, "reason", exc)
    base = cfg.eff_base_url
    low = str(reason).lower()
    tip = ""
    if "127.0.0.1" in base or "localhost" in base:
        if cfg.channel == "magpie":
            tip = "（Magpie 网关没开？或者换一个渠道）"
        elif cfg.channel in ("ollama", "lmstudio"):
            tip = f"（{cfg.preset.name} 没启动？）"
        else:
            tip = "（本地服务没起来）"
    elif "timed out" in low or "timeout" in low:
        tip = "（超时：网络不通，可能需要代理）"
    elif ("ssl" in low or "eof" in low or "reset" in low
          or "refused" in low or "getaddrinfo" in low
          or "name or service" in low):
        # 这些在 Windows 上长得很难看（SSL: UNEXPECTED_EOF_WHILE_READING），
        # 但对用户来说都是同一件事：这条路走不通。
        tip = "（连不上：域名不通、被拦截，或者地址写错了）"
    return f"网络错误: {reason}{tip}｜地址 {base}"


def _http_hint(code: int, detail: str, cfg: ModelConfig) -> str:
    """把各家的错误翻成人话。

    每家返回的报文格式都不一样，但状态码能说清大半问题。用户看到的
    "HTTP 401: {...一大坨 json...}" 基本上帮不上忙。
    """
    base = cfg.eff_base_url
    tips = {
        400: "请求格式不对。检查模型 ID 是不是这家认识的，"
             "以及协议（OpenAI / Anthropic / Gemini）选对没有。",
        401: "Key 不对或没带上。检查是否复制完整、有没有多余空格。",
        402: "余额不足。",
        403: "Key 没有这个模型的权限，或者所在地区被限制。",
        404: f"地址或模型不存在。当前地址 {base}，模型 {cfg.eff_model}。"
             f"有的家地址不带 /v1（智谱是 /api/paas/v4）。",
        422: "参数不被接受。多半是协议选错了 —— Claude 不走 OpenAI 格式。",
        429: "请求太频繁或额度用完，等一会儿再试。",
        500: "对方服务出错，稍后重试。",
        502: "网关错误，稍后重试。",
        503: "对方服务过载。",
    }
    tip = tips.get(code, "")
    short = (detail or "").strip().replace("\n", " ")[:240]
    return f"HTTP {code}：{tip}｜{short}" if tip else f"HTTP {code}: {short}"


# ══════════════════════════════════════════════════════════════════
def _models_url(cfg: ModelConfig) -> str:
    """「这家有哪些模型」的查询地址。

    三种协议都能查，形状略有不同：
      openai/anthropic  GET {base}/models          → {"data":[{"id":...}]}
      gemini            GET {base}/models?key=…    → {"models":[{"name":"models/…"}]}
    """
    base = cfg.eff_base_url
    u = f"{base}/models"
    if cfg.eff_protocol == "gemini" and cfg.api_key:
        u = f"{u}?key={urllib.parse.quote(cfg.api_key)}"
    return u


def _parse_models(payload: dict, protocol: str) -> list:
    """从 /models 的响应里抠出模型 id 列表。

    三种形状：
      openai / anthropic  {"data":[{"id":"..."}]}
      openrouter          {"data":[{"id":"..."}]}（同上，453 个）
      gemini              {"models":[{"name":"models/gemini-2.5-flash",
                                       "supportedGenerationMethods":[...]}]}
    也有中转站直接返回 {"models":["a","b"]} 或裸数组，都兜一下。
    """
    ids: list = []
    if protocol == "gemini":
        for m in (payload.get("models") or []):
            if not isinstance(m, dict):
                continue
            name = str(m.get("name") or "")
            # 只留能对话的：embedding / aqa 之类在这里没用
            methods = m.get("supportedGenerationMethods") or []
            if methods and "generateContent" not in methods:
                continue
            if name.startswith("models/"):
                name = name[len("models/"):]
            if name:
                ids.append(name)
        return ids

    data = payload.get("data")
    if data is None:
        data = payload.get("models") or []
    if isinstance(data, dict):              # 少见但见过：{"data":{"models":[...]}}
        data = data.get("models") or list(data.values())
    for m in data:
        if isinstance(m, str):
            ids.append(m)
        elif isinstance(m, dict):
            mid = m.get("id") or m.get("name") or m.get("model")
            if mid:
                ids.append(str(mid))
    return ids


def list_models(cfg: ModelConfig,
                timeout: float = 15.0) -> Tuple[bool, list, str]:
    """拉这家「你这把 Key 能用哪些模型」。

    为什么必须真拉：预设里的模型 ID 只是**我抄的时候**的样本，会过期；
    而「你这把 Key 能用哪些」只有对方知道（免费额度、区域限制、企业版
    都可能不一样）。magpie 也是这么做的 —— 模型列表来自厂商，
    不是来自它自己的常量表。

    返回 (成功, 模型 id 列表, 一句话)。不抛异常 —— 界面要用它显示原因。
    """
    if not cfg.eff_base_url:
        return False, [], "先填接口地址"
    # ⚠ 这里**不强制要求 Key**。有的家 /models 是公开的（OpenRouter 就是），
    #   没 Key 也让人先看一眼列表；要认证的会返回 401，如实报出来就行。
    #   挡在前面的坏处是：用户想「先看看这家有什么模型」都不行。

    req = urllib.request.Request(_models_url(cfg),
                                 headers=_headers(cfg), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if exc.fp else ""
        return False, [], _http_hint(exc.code, body, cfg)
    except urllib.error.URLError as exc:
        return False, [], _net_hint(exc, cfg)
    except Exception as exc:                        # noqa: BLE001
        return False, [], f"{type(exc).__name__}: {exc}"

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return False, [], f"返回的不是 JSON：{raw[:160]}"

    ids: list = []
    try:
        ids = _parse_models(payload, cfg.eff_protocol)
    except Exception as exc:                        # noqa: BLE001
        return False, [], f"响应结构不认识：{exc}｜{raw[:200]}"

    ids = sorted(set(i for i in ids if i))
    if not ids:
        return False, [], "拉到了空列表（这家可能没实现 /models，手填吧）"
    return True, ids, f"{len(ids)} 个模型"


# ══════════════════════════════════════════════════════════════════
def test_connection(cfg: ModelConfig, timeout: float = 25.0) -> Tuple[bool, str]:
    """界面上的「测试连接」。

    真发一次最小请求，而不是只检查字段 —— 字段填对了但 Key 无效、
    地址不通、模型名不存在，这四种都在这一步才会暴露。

    返回 (成功, 一句话)。不抛异常。
    """
    ok, why = cfg.status()
    if not ok:
        return False, why

    probe = ModelConfig(**{**cfg.__dict__, "max_tokens": 24,
                           "timeout": timeout})
    t0 = time.time()
    try:
        out = call_model(probe, "你是测试助手。", "只回复两个字：收到")
    except ModelError as exc:
        return False, str(exc)
    except Exception as exc:                    # 兜底，界面不能因为奇怪异常崩
        return False, f"{type(exc).__name__}: {exc}"
    dt = time.time() - t0
    head = (out or "").strip().replace("\n", " ")[:20]
    if not head:
        return False, f"连上了但回复是空的（{dt:.1f}s）"
    return True, f"{cfg.preset.name} · {cfg.eff_model} · {dt:.1f}s · 「{head}」"
