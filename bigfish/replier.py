"""[bigfish] 回复生成 —— 调模型 API。

上下文组装顺序（这个顺序是有讲究的）：

    system = 人格（灵魂）+ 这会儿的状态（情绪）
    user   = 记住的事（记忆检索）+ 本轮怎么答（协议）+ 对话记录

人格和情绪放 system：它们描述「我是谁、我现在什么状态」，
在整个会话里稳定。
记忆和协议放 user：它们是「这一轮相关的信息」，每轮都变。

不把协议写进人格，是为了让灵魂保持单纯——它只该定义「我是谁」，
不该被输出格式污染。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import List, Optional

from bigfish.bridge_client import Context, load_context
from bigfish.soul import Soul

# 默认配置。
# 优先复用 DSH 已有的 Magpie 本地网关（OpenAI 兼容接口，不需要 api_key），
# 这样用户在微信里用的大肥鱼，和 DSH 里是同一套模型，不用另配一份。
# 想换官方 API 或别的自定义接口，写在 config.json 里覆盖即可。
DEFAULT_ENDPOINT = "http://127.0.0.1:3425/v1/chat/completions"
DEFAULT_MODEL = "workbuddy-ai/deepseek-v4.1-flash"


@dataclass
class ModelConfig:
    endpoint: str = DEFAULT_ENDPOINT
    api_key: str = ""
    model: str = DEFAULT_MODEL
    temperature: float = 1.0
    max_tokens: int = 1600
    timeout: float = 90.0

    @classmethod
    def load(cls, path: str = "") -> "ModelConfig":
        """加载顺序：config.json > 环境变量 > 默认值。

        config.json 放第一，是因为它才是用户看得见、能改的那份；
        环境变量留给临时覆盖（CI、调试）。
        """
        cfg = cls()

        # 1) config.json
        p = path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config.json")
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f)
                cfg.endpoint = d.get("endpoint") or cfg.endpoint
                cfg.model = d.get("model") or cfg.model
                cfg.api_key = d.get("api_key") or cfg.api_key
                cfg.temperature = float(d.get("temperature", cfg.temperature))
                cfg.max_tokens = int(d.get("max_tokens", cfg.max_tokens))
                cfg.timeout = float(d.get("timeout", cfg.timeout))
            except Exception:
                pass

        # 2) 环境变量覆盖
        cfg.api_key = (os.environ.get("BIGFISH_API_KEY")
                       or os.environ.get("DEEPSEEK_API_KEY")
                       or cfg.api_key)
        cfg.endpoint = os.environ.get("BIGFISH_ENDPOINT") or cfg.endpoint
        cfg.model = os.environ.get("BIGFISH_MODEL") or cfg.model

        return cfg

    @property
    def needs_key(self) -> bool:
        """本地网关不需要 key；远程端点才需要。"""
        return not ("127.0.0.1" in self.endpoint
                    or "localhost" in self.endpoint)

    @property
    def ready(self) -> bool:
        return bool(self.api_key) or not self.needs_key


class ModelError(RuntimeError):
    pass


def call_model(cfg: ModelConfig, system: str, user: str) -> str:
    """调一次 chat/completions，返回纯文本回复。"""
    if not cfg.ready:
        raise ModelError("没有配置 api_key（config.json 的 api_key "
                         "或环境变量 BIGFISH_API_KEY）")

    body = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        "stream": False,
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    req = urllib.request.Request(cfg.endpoint, data=data, method="POST",
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
        raise ModelError(f"HTTP {exc.code}: {detail[:300]}") from exc
    except urllib.error.URLError as exc:
        raise ModelError(
            f"网络错误: {exc.reason}（本地网关的话，确认 Magpie 在跑）"
        ) from exc

    try:
        payload = json.loads(raw)
        msg = payload["choices"][0]["message"]
    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        raise ModelError(f"响应解析失败: {raw[:300]}") from exc

    content = msg.get("content")
    if content:
        return str(content).strip()
    # 推理模型可能把 token 全用在 reasoning 上，正文为空
    reasoning = msg.get("reasoning_content")
    if reasoning:
        raise ModelError(
            f"模型只输出了推理、正文为空（max_tokens={cfg.max_tokens} 太小？）"
            f" reasoning 前 100 字: {str(reasoning)[:100]}")
    return ""
