"""模型接入回归测试。

跑法：
    python tools/selftest_providers.py

不联网、不需要 Key。请求体和响应解析都是纯函数，用假数据验。

覆盖：
  · 预设表完整性（id 唯一、协议合法、本地渠道不需要 Key）
  · normalize_base：用户粘完整地址 / 带尾斜杠 / 带模型名
  · chat_url：三种协议拼出的地址
  · ModelConfig：新结构加载、覆盖预设、缺字段兜底
  · 旧配置迁移（老顶层 endpoint/model/api_key 还能用）
  · status()：四种"没配好"的情况各给对提示
  · needs_key：本机 vs 远端
  · 三种协议的请求体字段（Anthropic 的 system 是顶层、Gemini 的 key 走查询串）
  · 三种协议的响应解析（含推理字段、安全拦截）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import providers as P  # noqa: E402
from bigfish import replier as R  # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []


def _pick_marks() -> tuple[str, str]:
    try:
        "\u2713".encode(sys.stdout.encoding or "utf-8")
        return "\u2713", "\u2717"
    except (UnicodeEncodeError, LookupError):
        return "[OK]", "[X]"


MARK_OK, MARK_FAIL = _pick_marks()


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  {MARK_OK} {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  {MARK_FAIL} {name}  {detail}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


def cfg_with(**kw) -> R.ModelConfig:
    base = dict(channel="deepseek", model="deepseek-v4-flash",
                api_key="sk-test")
    base.update(kw)
    return R.ModelConfig(**base)


# ══════════════════════════════════════════════════════════════════
def test_presets() -> None:
    section("预设表")
    ids = [p.id for p in P.PRESETS]
    check("id 不重复", len(ids) == len(set(ids)), f"{len(ids)} vs {len(set(ids))}")
    check("至少 12 家", len(P.PRESETS) >= 12, f"{len(P.PRESETS)}")

    bad_proto = [p.id for p in P.PRESETS if p.protocol not in P.PROTOCOLS]
    check("协议都合法", not bad_proto, f"{bad_proto}")

    no_name = [p.id for p in P.PRESETS if not p.name]
    check("都有名字", not no_name, f"{no_name}")

    # 非自定义、非本地渠道必须有地址
    missing = [p.id for p in P.PRESETS
               if p.id != "custom" and not p.needs_key and not p.base_url]
    check("本地渠道也有地址", not missing, f"{missing}")

    missing2 = [p.id for p in P.PRESETS
                if p.id != "custom" and p.needs_key and not p.base_url]
    check("远端渠道都有地址", not missing2, f"{missing2}")

    # 关键三家必须在
    for want in ("deepseek", "anthropic", "gemini", "custom", "magpie"):
        check(f"有 {want}", want in P.BY_ID)

    check("custom 的 base_url 为空", P.get("custom").base_url == "")
    check("不认识的 id → custom", P.get("不存在").id == "custom")
    check("空 id → custom", P.get("").id == "custom")

    # 本地/免 Key
    check("magpie 不需要 Key", not P.get("magpie").needs_key)
    check("ollama 不需要 Key", not P.get("ollama").needs_key)
    check("deepseek 需要 Key", P.get("deepseek").needs_key)


def test_normalize() -> None:
    section("normalize_base")
    cases = [
        ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1"),
        ("https://api.deepseek.com/v1/", "https://api.deepseek.com/v1"),
        ("https://api.deepseek.com/v1/chat/completions",
         "https://api.deepseek.com/v1"),
        ("https://api.anthropic.com/v1/messages",
         "https://api.anthropic.com/v1"),
        ("https://open.bigmodel.cn/api/paas/v4",
         "https://open.bigmodel.cn/api/paas/v4"),
        ("  https://api.x.ai/v1/  ", "https://api.x.ai/v1"),
        ("https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
         "https://generativelanguage.googleapis.com/v1beta"),
        ("", ""),
    ]
    for raw, want in cases:
        got = P.normalize_base(raw)
        check(f"剥 {raw[:42]!r}", got == want, f"got={got!r} want={want!r}")


def test_urls() -> None:
    section("chat_url")
    check("openai",
          P.chat_url("https://api.deepseek.com/v1", "openai")
          == "https://api.deepseek.com/v1/chat/completions")
    check("anthropic",
          P.chat_url("https://api.anthropic.com/v1", "anthropic")
          == "https://api.anthropic.com/v1/messages")
    check("gemini",
          P.chat_url("https://generativelanguage.googleapis.com/v1beta",
                     "gemini", "gemini-2.5-flash")
          == "https://generativelanguage.googleapis.com/v1beta"
             "/models/gemini-2.5-flash:generateContent")
    check("粘了完整地址也能拼对",
          P.chat_url("https://api.deepseek.com/v1/chat/completions", "openai")
          == "https://api.deepseek.com/v1/chat/completions")


def test_config() -> None:
    section("配置加载")
    tmp = tempfile.mkdtemp()
    pth = os.path.join(tmp, "config.json")

    # 新结构
    json.dump({"llm": {"channel": "moonshot", "model": "kimi-k3",
                       "api_key": "sk-a", "temperature": 0.7,
                       "max_tokens": 900}},
              open(pth, "w", encoding="utf-8"))
    c = R.ModelConfig.load(pth)
    check("channel 读到", c.channel == "moonshot")
    check("模型读到", c.model == "kimi-k3")
    check("温度读到", abs(c.temperature - 0.7) < 1e-6)
    check("max_tokens 读到", c.max_tokens == 900)
    check("base_url 用预设",
          c.eff_base_url == "https://api.moonshot.cn/v1", c.eff_base_url)

    # 覆盖预设地址
    json.dump({"llm": {"channel": "deepseek", "model": "x",
                       "api_key": "k",
                       "base_url": "https://my-relay.example.com/v1/"}},
              open(pth, "w", encoding="utf-8"))
    c = R.ModelConfig.load(pth)
    check("自定义地址覆盖预设",
          c.eff_base_url == "https://my-relay.example.com/v1", c.eff_base_url)

    # 空 model → 用预设第一个
    json.dump({"llm": {"channel": "openai", "api_key": "k"}},
              open(pth, "w", encoding="utf-8"))
    c = R.ModelConfig.load(pth)
    check("空模型用预设默认", c.eff_model == "gpt-5.5", c.eff_model)

    # 协议覆盖
    json.dump({"llm": {"channel": "custom", "model": "m", "api_key": "k",
                       "base_url": "https://x.test/v1",
                       "protocol": "anthropic"}},
              open(pth, "w", encoding="utf-8"))
    c = R.ModelConfig.load(pth)
    check("协议可覆盖", c.eff_protocol == "anthropic")
    check("custom 走 anthropic 拼地址",
          c.url == "https://x.test/v1/messages", c.url)

    # 非法协议 → 回落预设
    json.dump({"llm": {"channel": "deepseek", "model": "m",
                       "protocol": "乱写"}},
              open(pth, "w", encoding="utf-8"))
    c = R.ModelConfig.load(pth)
    check("非法协议回落 openai", c.eff_protocol == "openai")


def test_legacy() -> None:
    section("旧配置迁移")
    tmp = tempfile.mkdtemp()
    pth = os.path.join(tmp, "config.json")

    # 老的本机 Magpie
    json.dump({"endpoint": "http://127.0.0.1:3425/v1/chat/completions",
               "model": "workbuddy-ai/deepseek-v4.1-flash"},
              open(pth, "w", encoding="utf-8"))
    c = R.ModelConfig.load(pth)
    check("老 Magpie 地址 → magpie 渠道", c.channel == "magpie", c.channel)
    check("老 Magpie 模型保留",
          c.eff_model == "workbuddy-ai/deepseek-v4.1-flash")
    check("老 Magpie 不需要 Key", not c.needs_key)

    # 老的远端 OpenAI 兼容
    json.dump({"endpoint": "https://api.deepseek.com/v1/chat/completions",
               "model": "deepseek-v4-flash", "api_key": "sk-old"},
              open(pth, "w", encoding="utf-8"))
    c = R.ModelConfig.load(pth)
    check("老远端 → custom", c.channel == "custom", c.channel)
    check("老地址剥成前缀",
          c.eff_base_url == "https://api.deepseek.com/v1", c.eff_base_url)
    check("老 Key 保留", c.api_key == "sk-old")
    check("老远端需要 Key", c.needs_key)

    # 老的 Anthropic
    json.dump({"endpoint": "https://api.anthropic.com/v1/messages",
               "model": "claude-sonnet-5", "api_key": "sk-ant"},
              open(pth, "w", encoding="utf-8"))
    c = R.ModelConfig.load(pth)
    check("老 Anthropic 地址 → anthropic 协议",
          c.eff_protocol == "anthropic", c.eff_protocol)

    # 空配置：不该崩，但也不能 ready
    json.dump({}, open(pth, "w", encoding="utf-8"))
    c = R.ModelConfig.load(pth)
    check("空配置不崩", c is not None)
    ok, why = c.status()
    check("空配置 status 说清缺什么", (not ok) and "Key" in why, why)


def test_status() -> None:
    section("status：四种没配好")

    c = cfg_with(channel="custom", base_url="", model="m")
    ok, why = c.status()
    check("没地址", not ok and "地址" in why, why)

    c = cfg_with(channel="deepseek", model="", api_key="k")
    # deepseek 预设自带模型，所以这里其实会用预设默认 → 应该 ok
    ok, why = c.status()
    check("空模型自动用预设 → 可用", ok, why)

    c = cfg_with(channel="openai", model="gpt-5.5", api_key="")
    ok, why = c.status()
    check("缺 Key", not ok and "Key" in why, why)
    check("缺 Key 时给出取 Key 的地方", "platform.openai.com" in why, why)

    c = cfg_with(channel="magpie", model="m", api_key="")
    ok, why = c.status()
    check("本地网关不需要 Key → 可用", ok, why)

    c = cfg_with()
    ok, why = c.status()
    check("配好就 ok，且说明是哪家", ok and "DeepSeek" in why, why)


def test_bodies() -> None:
    section("三种协议的请求体")
    c = cfg_with()
    b = R._body_openai(c, "SYS", "USER")
    check("openai: system 在 messages 里",
          b["messages"][0]["role"] == "system")
    check("openai: 有 max_tokens", "max_tokens" in b)
    check("openai: 不流式", b["stream"] is False)

    ca = cfg_with(channel="anthropic", model="claude-sonnet-5")
    ba = R._body_anthropic(ca, "SYS", "USER")
    check("anthropic: system 是顶层字段", ba.get("system") == "SYS")
    check("anthropic: messages 里只有 user",
          len(ba["messages"]) == 1 and ba["messages"][0]["role"] == "user")
    check("anthropic: 有 max_tokens", "max_tokens" in ba)

    cg = cfg_with(channel="gemini", model="gemini-2.5-flash")
    bg = R._body_gemini(cg, "SYS", "USER")
    check("gemini: systemInstruction",
          bg["systemInstruction"]["parts"][0]["text"] == "SYS")
    check("gemini: contents",
          bg["contents"][0]["parts"][0]["text"] == "USER")
    check("gemini: maxOutputTokens",
          bg["generationConfig"]["maxOutputTokens"] == cg.max_tokens)


def test_headers() -> None:
    section("认证方式")
    c = cfg_with()
    h = R._headers(c)
    check("openai: Bearer", h.get("Authorization") == "Bearer sk-test")

    ca = cfg_with(channel="anthropic", model="claude-sonnet-5")
    ha = R._headers(ca)
    check("anthropic: x-api-key", ha.get("x-api-key") == "sk-test")
    check("anthropic: 带 version", "anthropic-version" in ha)
    check("anthropic: 不用 Bearer", "Authorization" not in ha)

    cg = cfg_with(channel="gemini", model="gemini-2.5-flash")
    hg = R._headers(cg)
    check("gemini: 不放请求头",
          "Authorization" not in hg and "x-api-key" not in hg)
    u = R._request_url(cg)
    check("gemini: key 在查询串", "?key=sk-test" in u, u)
    check("gemini: 地址含模型名", "models/gemini-2.5-flash:generateContent" in u, u)


def test_parsers() -> None:
    section("响应解析")
    txt, rea = R._parse_openai(
        {"choices": [{"message": {"content": "  你好  ",
                                  "reasoning_content": "想一下"}}]})
    check("openai: 取 content 并去空白", txt == "你好", txt)
    check("openai: 取 reasoning", rea == "想一下")

    txt, rea = R._parse_anthropic(
        {"content": [{"type": "thinking", "thinking": "思考中"},
                     {"type": "text", "text": "收到"},
                     {"type": "text", "text": "了"}]})
    check("anthropic: 拼接多个 text 块", txt == "收到了", txt)
    check("anthropic: 取 thinking", rea == "思考中")

    txt, rea = R._parse_gemini(
        {"candidates": [{"content": {"parts": [
            {"text": "内心戏", "thought": True},
            {"text": "正文"},
        ]}}]})
    check("gemini: thought 块算推理", rea == "内心戏", rea)
    check("gemini: 正文不含 thought", txt == "正文", txt)

    try:
        R._parse_gemini({"promptFeedback": {"blockReason": "SAFETY"}})
        check("gemini: 没有 candidates 要报错", False)
    except R.ModelError as exc:
        check("gemini: 没有 candidates 报安全拦截", "安全" in str(exc), str(exc))


def test_models_url() -> None:
    section("模型列表地址")
    c = cfg_with()
    check("openai: /models",
          R._models_url(c) == "https://api.deepseek.com/v1/models",
          R._models_url(c))

    ca = cfg_with(channel="anthropic", model="claude-sonnet-5")
    check("anthropic: /models",
          R._models_url(ca) == "https://api.anthropic.com/v1/models",
          R._models_url(ca))

    cg = cfg_with(channel="gemini", model="gemini-2.5-flash")
    u = R._models_url(cg)
    check("gemini: key 在查询串", u.endswith("/models?key=sk-test"), u)


def test_parse_models() -> None:
    section("模型列表解析")
    openai_like = {"object": "list", "data": [
        {"id": "deepseek-v4-flash"}, {"id": "deepseek-v4-pro"}]}
    got = R._parse_models(openai_like, "openai")
    check("openai: 取 data[].id", got == ["deepseek-v4-flash", "deepseek-v4-pro"], got)

    check("anthropic 同形状",
          R._parse_models(openai_like, "anthropic") == got)

    # 中转站常见变体
    check("变体: 裸数组字符串",
          R._parse_models({"data": ["a", "b"]}, "openai") == ["a", "b"])
    check("变体: models 键",
          R._parse_models({"models": [{"name": "x"}]}, "openai") == ["x"])

    gem = {"models": [
        {"name": "models/gemini-2.5-flash",
         "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/text-embedding-004",
         "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/gemini-3-pro",
         "supportedGenerationMethods": ["generateContent", "countTokens"]},
    ]}
    got = R._parse_models(gem, "gemini")
    check("gemini: 去掉 models/ 前缀", "gemini-2.5-flash" in got, got)
    check("gemini: 过滤掉 embedding", "text-embedding-004" not in got, got)
    check("gemini: 只留 generateContent", got == ["gemini-2.5-flash", "gemini-3-pro"], got)


def test_list_models_offline() -> None:
    section("list_models：不联网的分支")
    c = cfg_with(channel="custom", base_url="", api_key="k")
    ok, models, msg = R.list_models(c)
    check("没地址 → 拒绝且说清", (not ok) and "地址" in msg, msg)

    # 免 key 也要让请求发出去（OpenRouter 的 /models 是公开的）
    c2 = cfg_with(channel="openrouter", api_key="")
    check("没 key 不在本地就拦住",
          "先填 API Key" not in R.list_models.__doc__)
    import inspect
    src = inspect.getsource(R.list_models)
    check("list_models 里没有 key 前置检查",
          "needs_key and not cfg.api_key" not in src)


def test_http_hint() -> None:
    section("错误翻译成中文")
    c = cfg_with()
    for code, want in ((401, "Key"), (404, "地址"), (429, "频繁"),
                       (402, "余额")):
        msg = R._http_hint(code, '{"error":"x"}', c)
        check(f"{code} 提到「{want}」", want in msg, msg)
    check("404 会带上当前地址",
          "api.deepseek.com" in R._http_hint(404, "", c))


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    print("模型接入回归测试（bigfish/providers.py + replier.py）")
    test_presets()
    test_normalize()
    test_urls()
    test_config()
    test_legacy()
    test_status()
    test_bodies()
    test_headers()
    test_parsers()
    test_models_url()
    test_parse_models()
    test_list_models_offline()
    test_http_hint()

    print(f"\n{'=' * 46}")
    total = PASS + FAIL
    if FAIL:
        print(f"失败 {FAIL}/{total}")
        for n in FAILED:
            print(f"  - {n}")
        return 1
    print(f"全部通过 {PASS}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
