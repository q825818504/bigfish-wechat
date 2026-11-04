# -*- coding: utf-8 -*-
"""预设地址体检：每个预设发一个假 Key，看对方的返回码。

这不是单测（要联网）。用途是**确认预设表里的地址没写错** ——
地址错了用户第一次就会撞 404，而且他会以为是自己填错了。

判读规则：
  401 / 403 / 400 / 422  → 地址对，只是没给真 Key（预期）
  404                    → 地址大概率错了
  连不上 / SSL           → 域名不通（国内可能被墙，正常）

跑法：
    python tools/probe_providers.py
    python tools/probe_providers.py deepseek anthropic
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import providers as P  # noqa: E402
from bigfish.replier import ModelConfig, ModelError, call_model  # noqa: E402

#: 假 Key。格式做得像真的，这样对方会走到"认证失败"而不是"格式错误"。
FAKE_KEY = "sk-probe-00000000000000000000000000000000"

#: 这些家的假 Key 认证格式特殊，照样发
SKIP_LOCAL = {"magpie", "ollama", "lmstudio", "custom"}


def probe(preset: P.Preset, timeout: float = 12.0) -> str:
    models = preset.models
    model = models[0] if models else "test-model"
    cfg = ModelConfig(channel=preset.id, model=model,
                      api_key=FAKE_KEY if preset.needs_key else "")
    try:
        call_model(cfg, "测试", "收到")
        return "OK(??? 竟然通了)"
    except ModelError as exc:
        return str(exc)
    except Exception as exc:                     # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def verdict(msg: str) -> str:
    if msg.startswith("HTTP 401") or msg.startswith("HTTP 403"):
        return "地址对（认证失败为预期）"
    if msg.startswith("HTTP 400") or msg.startswith("HTTP 422"):
        return "地址对（参数/协议被拒为预期）"
    if msg.startswith("HTTP 404") or "HTTP 404" in msg[:20]:
        return "⚠ 地址可能错了（404）"
    if "连不上" in msg or "网络错误" in msg:
        return "连不上（域名不通，可能需代理）"
    return "看看下面这行"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    todo = [P.get(a) for a in args] if args else list(P.PRESETS)
    todo = [p for p in todo if p.id not in SKIP_LOCAL]

    print(f"体检 {len(todo)} 家（假 Key，只为看地址通不通）\n")
    rows = []
    for p in todo:
        msg = probe(p)
        v = verdict(msg)
        rows.append((p.id, p.base_url, v, msg))
        mark = "OK " if ("地址对" in v or "连不上" in v) else "!! "
        print(f"{mark}{p.id:<12} {v}")
        if "地址对" not in v:
            print(f"     地址 {p.base_url}")
            print(f"     返回 {msg[:200]}")
        print()

    bad = [r for r in rows if "地址对" not in r[2]]
    print("=" * 60)
    if bad:
        print(f"需要复核 {len(bad)} 家：")
        for pid, url, v, msg in bad:
            print(f"  {pid:<12} {url}  → {v}")
        return 1
    print(f"全部 {len(rows)} 家地址可用（假 Key 被正确拒绝）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
