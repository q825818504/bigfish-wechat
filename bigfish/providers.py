# -*- coding: utf-8 -*-
"""模型供应商预设 —— 「只填一个 Key 就能用」。

## 为什么需要这个

在这之前，模型配置页只有三个裸字段：接口地址 / 模型 ID / API Key。等于要求
用户自己知道：

  · DeepSeek 的地址是 https://api.deepseek.com/v1/chat/completions
  · 模型 ID 叫 deepseek-v4-flash
  · Claude 根本不走 OpenAI 格式，字段名全不一样

默认值又指向 `127.0.0.1:3425`（Magpie 本地网关）。所以除了本机装了 Magpie 的
那个人，**别人拿到这个程序是配不出模型的** —— 换个用户、换台电脑就废了。

现在：选一家，粘 Key，完事。地址和模型列表内置，但**都可以改** ——
预设写错了也不致命，用户改一次就好。

## 三跳

| channel | 意思 |
|---|---|
| 预设 id（`deepseek` / `anthropic` …） | 用内置地址，只填 Key |
| `custom` | 手填 base_url + Key，可选协议 |
| `magpie` | 本机或别人的 Magpie 网关，不需要 Key |

## 协议

市面上就三种值得支持：

| protocol | 请求 | 认证 | 正文位置 |
|---|---|---|---|
| `openai` | `POST {base}/chat/completions` | `Authorization: Bearer` | `choices[0].message.content` |
| `anthropic` | `POST {base}/messages` | `x-api-key` + `anthropic-version` | `content[].text`（挑 type=text） |
| `gemini` | `POST {base}/models/{id}:generateContent` | `?key=` 查询参数 | `candidates[0].content.parts[].text` |

其余家（Kimi、智谱、豆包、通义、硅基流动、OpenRouter、xAI、Groq、Mistral、
Ollama、LM Studio……）都是 OpenAI 兼容，归第一类。

## 模型 ID

内置的只是**建议值**，不是白名单。模型迭代比程序快，写死会过期。
界面上是可选可改的输入框。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

#: 支持的协议。顺序 = 界面上下拉框的顺序。
PROTOCOLS: Tuple[str, ...] = ("openai", "anthropic", "gemini")

PROTOCOL_LABEL: Dict[str, str] = {
    "openai": "OpenAI 兼容",
    "anthropic": "Anthropic Messages",
    "gemini": "Google Gemini",
}


@dataclass(frozen=True)
class Preset:
    """一家供应商。"""

    id: str
    name: str
    #: 请求前缀。拼 `/chat/completions`（openai）或 `/messages`（anthropic）用。
    base_url: str = ""
    protocol: str = "openai"
    #: 建议模型，第一个是默认。空 = 让用户自己填。
    models: Tuple[str, ...] = ()
    #: 需要 api key 吗（本机网关/本地模型不需要）
    needs_key: bool = True
    #: Key 从哪拿，一句话
    key_hint: str = ""
    #: 控制台/文档地址
    doc: str = ""
    #: 需要提醒的事
    note: str = ""

    @property
    def is_local(self) -> bool:
        return (not self.needs_key) or "127.0.0.1" in self.base_url \
            or "localhost" in self.base_url


#: 预设表。
#:
#: ⚠ 顺序有意义：界面上按这个顺序排。
#:   前面放国内可直连的（用户大概率用这些），后面放需要外网的。
PRESETS: Tuple[Preset, ...] = (
    Preset(
        id="magpie",
        name="Magpie 网关（本机）",
        base_url="http://127.0.0.1:3425/v1",
        models=("workbuddy-ai/deepseek-v4.1-flash",),
        needs_key=False,
        doc="https://github.com/yetone/magpie",
        note="装了 Magpie 就有，不用 Key。没装就换一个。",
    ),
    Preset(
        id="deepseek",
        name="DeepSeek 深度求索",
        base_url="https://api.deepseek.com/v1",
        models=("deepseek-v4-flash", "deepseek-v4-pro"),
        key_hint="platform.deepseek.com → API keys",
        doc="https://platform.deepseek.com",
        note="便宜，中文好，日常聊天首选。",
    ),
    Preset(
        id="moonshot",
        name="Kimi 月之暗面",
        base_url="https://api.moonshot.cn/v1",
        models=("kimi-k2.6", "kimi-k3", "kimi-k3.1-pro"),
        key_hint="platform.moonshot.cn → API Key 管理",
        doc="https://platform.moonshot.cn",
        note="长上下文。",
    ),
    Preset(
        id="zhipu",
        name="智谱 GLM",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        models=("glm-4.7-flash", "glm-5", "glm-5.2"),
        key_hint="bigmodel.cn → 个人中心 → API Keys",
        doc="https://open.bigmodel.cn",
        note="注意地址是 /api/paas/v4，不是 /v1。",
    ),
    Preset(
        id="qwen",
        name="通义千问（阿里云百炼）",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        models=("qwen3.8-max", "qwen-flash", "qwen3-coder-flash"),
        key_hint="bailian.console.aliyun.com → API-KEY",
        doc="https://bailian.console.aliyun.com",
        note="海外账号把 dashscope 换成 dashscope-intl。",
    ),
    Preset(
        id="volcengine",
        name="火山方舟（豆包）",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        models=("doubao-seed-2-1-pro-260628",
                "doubao-seed-2-0-mini-260428",
                "deepseek-v4-flash-ga-260731"),
        key_hint="console.volcengine.com/ark → API Key 管理",
        doc="https://console.volcengine.com/ark",
        note="模型 ID 带日期后缀，控制台里能复制准确的。",
    ),
    Preset(
        id="minimax",
        name="MiniMax",
        base_url="https://api.minimax.chat/v1",
        models=("MiniMax-M3.1-Flash", "MiniMax-M2.7", "MiniMax-M2.5"),
        key_hint="platform.minimaxi.com → 接口密钥",
        doc="https://platform.minimaxi.com",
        note="海外账号用 api.minimax.io/v1。",
    ),
    Preset(
        id="stepfun",
        name="阶跃星辰 StepFun",
        base_url="https://api.stepfun.com/v1",
        models=("step-2-16k",),
        key_hint="platform.stepfun.com → API Keys",
        doc="https://platform.stepfun.com",
    ),
    Preset(
        id="siliconflow",
        name="硅基流动 SiliconFlow",
        base_url="https://api.siliconflow.cn/v1",
        models=("zai-org/GLM-5.2", "deepseek-ai/DeepSeek-V3"),
        key_hint="cloud.siliconflow.cn → API 密钥",
        doc="https://cloud.siliconflow.cn",
        note="模型名带组织前缀，如 zai-org/GLM-5.2。",
    ),
    Preset(
        id="openrouter",
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        models=("deepseek/deepseek-v4-flash",
                "anthropic/claude-sonnet-5",
                "openai/gpt-5.5"),
        key_hint="openrouter.ai/keys",
        doc="https://openrouter.ai",
        note="一个 Key 通吃各家，模型名是 厂商/模型。",
    ),
    Preset(
        id="openai",
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        models=("gpt-5.5", "gpt-5-nano", "gpt-5.2"),
        key_hint="platform.openai.com → API keys",
        doc="https://platform.openai.com",
        note="国内需要代理。",
    ),
    Preset(
        id="anthropic",
        name="Anthropic Claude",
        base_url="https://api.anthropic.com/v1",
        protocol="anthropic",
        models=("claude-sonnet-5", "claude-haiku-4-5", "claude-opus-4-5"),
        key_hint="console.anthropic.com → API Keys",
        doc="https://console.anthropic.com",
        note="走的不是 OpenAI 格式，已按 Anthropic 协议适配。",
    ),
    Preset(
        id="gemini",
        name="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        protocol="gemini",
        models=("gemini-flash-latest", "gemini-2.5-flash"),
        key_hint="aistudio.google.com/apikey",
        doc="https://aistudio.google.com",
        note="Key 走查询参数，不是请求头。",
    ),
    Preset(
        id="xai",
        name="xAI Grok",
        base_url="https://api.x.ai/v1",
        models=("grok-4",),
        key_hint="console.x.ai → API Keys",
        doc="https://console.x.ai",
    ),
    Preset(
        id="groq",
        name="Groq",
        base_url="https://api.groq.com/openai/v1",
        models=("llama-3.3-70b-versatile",),
        key_hint="console.groq.com → API Keys",
        doc="https://console.groq.com",
        note="快。",
    ),
    Preset(
        id="mistral",
        name="Mistral",
        base_url="https://api.mistral.ai/v1",
        models=("mistral-large-latest",),
        key_hint="console.mistral.ai → API Keys",
        doc="https://console.mistral.ai",
    ),
    Preset(
        id="ollama",
        name="Ollama（本机）",
        base_url="http://127.0.0.1:11434/v1",
        models=("qwen3:8b", "llama3.2"),
        needs_key=False,
        doc="https://ollama.com",
        note="先 ollama serve，再把模型名填对（ollama list 能看到）。",
    ),
    Preset(
        id="lmstudio",
        name="LM Studio（本机）",
        base_url="http://127.0.0.1:1234/v1",
        models=(),
        needs_key=False,
        doc="https://lmstudio.ai",
        note="在 LM Studio 里开 Local Server，模型名照它的显示填。",
    ),
    Preset(
        id="custom",
        name="自定义（任意 OpenAI / Anthropic 兼容）",
        base_url="",
        models=(),
        needs_key=True,
        note="填接口地址和模型 ID。中转站、公司内网、自建服务都走这里。",
    ),
)

BY_ID: Dict[str, Preset] = {p.id: p for p in PRESETS}

#: 默认渠道。以前默认是 magpie，别人拿到程序配不出来 —— 现在默认 deepseek，
#: 它是国内最好拿 Key 的一家；没 Key 的话界面上会明确提示该选哪个。
DEFAULT_CHANNEL = "deepseek"


def get(channel: str) -> Preset:
    """按 id 取预设。不认识的 id 当自定义处理，不抛异常。"""
    return BY_ID.get((channel or "").strip()) or BY_ID["custom"]


def normalize_base(url: str) -> str:
    """把用户粘进来的地址收拾成「前缀」。

    用户经常直接粘完整接口地址：
        https://api.deepseek.com/v1/chat/completions
    或者带尾斜杠、带空格。这里统一剥掉，只留拼请求用的前缀。

    这一步是必要的：不加的话，"填了正确地址却连不上" 会长成最难查的那种 bug。
    """
    u = (url or "").strip()
    if not u:
        return ""
    u = u.rstrip("/")
    for tail in ("/chat/completions", "/completions", "/messages",
                 "/responses", "/generateContent"):
        if u.endswith(tail):
            u = u[: -len(tail)]
            break
    # Gemini 的 :generateContent 带模型名，形如 /models/xx:generateContent
    if ":generateContent" in u:
        u = u.split("/models/")[0]
    return u.rstrip("/")


def chat_url(base_url: str, protocol: str, model: str = "") -> str:
    """拼出真正要 POST 的地址。"""
    base = normalize_base(base_url)
    proto = (protocol or "openai").strip().lower()
    if proto == "anthropic":
        return f"{base}/messages"
    if proto == "gemini":
        return f"{base}/models/{model}:generateContent"
    return f"{base}/chat/completions"


def suggest_models(channel: str) -> List[str]:
    return list(get(channel).models)


def channels() -> List[Dict[str, object]]:
    """给界面的渠道清单（不带内部细节）。"""
    out: List[Dict[str, object]] = []
    for p in PRESETS:
        out.append({
            "id": p.id,
            "name": p.name,
            "base_url": p.base_url,
            "protocol": p.protocol,
            "protocol_label": PROTOCOL_LABEL.get(p.protocol, p.protocol),
            "models": list(p.models),
            "needs_key": p.needs_key,
            "key_hint": p.key_hint,
            "doc": p.doc,
            "note": p.note,
        })
    return out
