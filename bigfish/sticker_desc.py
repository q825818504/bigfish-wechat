"""[bigfish] 自动备注 —— 让模型看一眼这张表情，说出它是什么。

═══════════════════════════════════════════════════════════════════════
为什么需要它
═══════════════════════════════════════════════════════════════════════

表情库的每一张都该有描述，否则她不知道该在什么情景下发它 ——
文件名 `stk_0007.gif` 对她没有任何意义。

但让用户一张张手写备注，导入五十张就是五十次输入。没人会做。

所以导入时让模型看一眼，自动填两个字段：

    brief   概览，2~4 字，列表里显示的那一行（"生气"、"害羞"、"无语"）
    detail  一句话：画面是什么 + 什么情景适合用

═══════════════════════════════════════════════════════════════════════
模型怎么拿到图
═══════════════════════════════════════════════════════════════════════

走 OpenAI 的 vision 格式（`image_url` + base64 data URI）。
本地 Magpie 网关支持，2026-11-03 实测通过：

    发一张红色圆形 → 返回「红色圆形」

图先缩到 512 以内再编码 —— 模型不需要大图，而 base64 会让请求体
膨胀 33%，512 的 PNG 大约 100~300KB，够用且快。

动图取第一帧。判断"这张表情表达什么"不需要看全动效，
第一帧的构图和表情已经说明问题了。

═══════════════════════════════════════════════════════════════════════
失败怎么办
═══════════════════════════════════════════════════════════════════════

**绝不因为备注失败就拒绝导入。**

模型挂了、网关没开、超时、返回格式不对 —— 任何情况都退化成
「brief 留空，detail 留空」，用户之后可以自己补。
一张没有备注的表情仍然能发（只是插件挑的时候只能靠随机）。

所以这里所有函数都返回结果字典，不抛异常。
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

#: 送进模型的图最大边长。再大对判断没帮助，只是让请求变慢
VISION_MAX = 512

#: 单张备注的超时。模型看一眼很快，超过这个基本就是网关有问题
TIMEOUT = 45.0

SYSTEM_PROMPT = """你在帮一个聊天机器人整理她的表情包。

看这张图，说出三件事：

1. brief：这张表情表达的**核心情绪或动作**，2~4 个汉字。
   要具体，不要写"表情"、"图片"这种废话。
   好例子：生气、害羞、无语、得意、震惊、摆烂、委屈、加油、晚安
   坏例子：黄色笑脸、一个圆圈、卡通图案

2. detail：一句话说明画面内容 + 什么情景适合发它。
   画面要写具体的（"眉毛倒竖、脸涨红"而不是"看起来不开心"）。
   情景要写清楚对方说了什么话时可以用。
   不超过 60 字。

3. tags：2~4 个关键词，用于检索。每个 2~4 字。

只输出一行 JSON，不要解释，不要代码块：
{"brief": "...", "detail": "...", "tags": ["...", "..."]}"""


# ══════════════════════════════════════════════════════════════════════
# 图片 → base64
# ══════════════════════════════════════════════════════════════════════

def image_to_data_uri(path: str, max_dim: int = VISION_MAX) -> str:
    """把图片缩到 max_dim 以内，编码成 data URI。失败返回空串。

    动图只取第一帧 —— 判断"这张表达什么"看构图就够了，
    不需要把每一帧都送进去。
    """
    try:
        from PIL import Image
    except Exception:
        return ""

    try:
        with Image.open(path) as im:
            im.seek(0)
            frame = im.convert("RGBA")
            w, h = frame.size
            if max(w, h) > max_dim:
                scale = max_dim / float(max(w, h))
                frame = frame.resize(
                    (max(1, int(w * scale)), max(1, int(h * scale))),
                    Image.LANCZOS)
            # 白底合成：PNG 透明区域在模型眼里应该是一片白，
            # 而不是黑（直接 convert("RGB") 会把透明变黑）
            bg = Image.new("RGB", frame.size, (255, 255, 255))
            bg.paste(frame, mask=frame.getchannel("A"))
            buf = io.BytesIO()
            bg.save(buf, "PNG", optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/png;base64,{b64}"
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════════════════
# 解析模型输出
# ══════════════════════════════════════════════════════════════════════

def _parse(raw: str) -> Dict[str, Any]:
    """从模型输出里抠出 JSON。抠不出来返回空结构。

    模型经常在 JSON 外面包一层 ```json ``` 或者先说一句话，
    所以先找第一个 { 到最后一个 }。
    """
    out: Dict[str, Any] = {"brief": "", "detail": "", "tags": []}
    s = (raw or "").strip()
    if not s:
        return out
    if "{" in s and "}" in s:
        s = s[s.find("{"): s.rfind("}") + 1]
    try:
        obj = json.loads(s)
    except Exception:
        # 兜底：正则抠字段（模型偶尔会漏引号）
        m = re.search(r'"brief"\s*:\s*"([^"]*)"', raw or "")
        if m:
            out["brief"] = m.group(1)
        m = re.search(r'"detail"\s*:\s*"([^"]*)"', raw or "")
        if m:
            out["detail"] = m.group(1)
        return out

    if not isinstance(obj, dict):
        return out
    out["brief"] = str(obj.get("brief") or "").strip()
    out["detail"] = str(obj.get("detail") or "").strip()
    tags = obj.get("tags")
    if isinstance(tags, list):
        out["tags"] = [str(t).strip() for t in tags if str(t).strip()][:6]
    return out


# ══════════════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════════════

def describe(path: str, endpoint: str = "", model: str = "",
             api_key: str = "") -> Dict[str, Any]:
    """看一张表情，返回 {brief, detail, tags, ok, error}。

    **任何失败都不抛异常** —— 备注是锦上添花，不该挡住导入。
    """
    out: Dict[str, Any] = {"brief": "", "detail": "", "tags": [],
                           "ok": False, "error": ""}

    if not os.path.isfile(path):
        out["error"] = "文件不存在"
        return out

    uri = image_to_data_uri(path)
    if not uri:
        out["error"] = "图片读取失败"
        return out

    # 配置：没传就自己读（保持这个模块能独立调用）
    try:
        from bigfish.replier import ModelConfig

        mc = ModelConfig.load()
        endpoint = endpoint or mc.endpoint
        model = model or mc.model
        api_key = api_key or mc.api_key
    except Exception as exc:
        out["error"] = f"读配置失败：{exc}"
        return out

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": "这张表情是什么？按要求的 JSON 输出。"},
                {"type": "image_url", "image_url": {"url": uri}},
            ]},
        ],
        "temperature": 0.3,
        "max_tokens": 400,
        "stream": False,
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        req = urllib.request.Request(endpoint, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        out["error"] = f"HTTP {exc.code}"
        return out
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    try:
        msg = resp["choices"][0]["message"]
        raw = (msg.get("content") or "").strip()
    except Exception as exc:
        out["error"] = f"响应结构不对：{exc}"
        return out

    if not raw:
        out["error"] = "模型返回空"
        return out

    parsed = _parse(raw)
    out.update(parsed)
    out["ok"] = bool(parsed["brief"] or parsed["detail"])
    if not out["ok"]:
        out["error"] = f"没解析出内容：{raw[:80]}"
    return out


def describe_many(paths, on_progress=None, stop_flag=None,
                  **kw) -> list:
    """批量备注。逐张调，带进度回调。

    为什么串行不并发：本地 Magpie 网关是单进程，并发只会互相排队，
    还会让每张的耗时变得不可预测。串行至少进度是线性的。

    on_progress(i, total, path, result) 每张调一次。
    stop_flag 是个可调用对象，返回 True 就提前停（用户点了取消）。
    """
    results = []
    total = len(paths)
    for i, p in enumerate(paths, 1):
        if stop_flag and stop_flag():
            break
        res = describe(p, **kw)
        res["path"] = p
        results.append(res)
        if on_progress:
            try:
                on_progress(i, total, p, res)
            except Exception:
                pass
        # 网关不需要喘息，但连续请求太快时给一点点间隔，
        # 避免连接池复用出问题
        time.sleep(0.05)
    return results
