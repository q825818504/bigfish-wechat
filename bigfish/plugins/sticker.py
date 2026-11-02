"""[插件] 表情包 —— 让她也会说话说到一半甩一张图。

═══════════════════════════════════════════════════════════════════════
这个插件解决的问题
═══════════════════════════════════════════════════════════════════════

以前的她只会打字。真实的人在微信里不是这样说话 —— 情绪到了、话说到一半、
懒得打字的时候，发的是表情。

现在能发了，但**发得对**比**发得出**难：
  · 每句都发 → 噪音，用户会关掉
  · 发的不搭 → 比不发更尴尬

所以这个插件做的只有一件事：**判断这一轮该不该跟一张，跟哪一张。**

═══════════════════════════════════════════════════════════════════════
表情库从哪来
═══════════════════════════════════════════════════════════════════════

    <数据目录>/stickers/          用户放进去的，或者程序生成的
    <数据目录>/stickers/index.json 每张的一句话说明

没有表情库 → 这个插件永远不发。这是刻意的：
新装好的程序里她手里是空的，不该凭空变出图来。

═══════════════════════════════════════════════════════════════════════
怎么挑
═══════════════════════════════════════════════════════════════════════

mode=ai（默认）：把表情库的说明书（描述列表）+ 这一轮她要说的话
交给模型，问「该不该配一张，配哪张」。模型回答编号。

mode=random：不看内容随机挑。便宜，但经常不搭 —— 给不在乎的用户。

为什么用模型而不是关键词匹配：
    表情包的关键是**语义**，不是关键词。"你说得对"和"确实"是同一个意思，
    关键词表永远写不全。而模型一秒钟就能判断。

═══════════════════════════════════════════════════════════════════════
边界（写清楚比写清楚能力更重要）
═══════════════════════════════════════════════════════════════════════

  · 不发她手里没有的表情。不会去网上找图。
  · 不发超过 1024×1024 的 —— 微信会把它当文件发，对方收到一个附件。
  · 一条回复最多跟一张。两张以上是刷屏。
"""
from __future__ import annotations

import json
import os
import random
import time
from typing import Any, Dict, List

from . import register

#: 微信把 >1024 的 GIF 当文件发；这之上就不再是表情了。
_MAX_DIM = 1024


def _lib(root: str) -> List[Dict[str, Any]]:
    from bigfish import vault

    return vault.list_stickers(root)


def _usable(entry: Dict[str, Any]) -> bool:
    """快速过滤：只发微信会当表情/图片的，且尺寸不过界。"""
    p = entry.get("path") or ""
    if not os.path.isfile(p):
        return False
    try:
        if os.path.getsize(p) > 8 * 1024 * 1024:
            return False
    except OSError:
        return False
    # 尺寸只在能读的时候才检查 —— 读不出来不等于不能用
    try:
        from PIL import Image

        with Image.open(p) as im:
            w, h = im.size
        if max(w, h) > _MAX_DIM:
            return False
    except Exception:
        pass
    return True


def _ask_model(candidates: List[Dict[str, Any]], ctx: Dict[str, Any],
               cfg: Dict[str, Any]) -> str:
    """让模型从候选里挑一张，或决定不挑。返回文件路径（空 = 不挑）。

    ⚠ 这里刻意**不重试、不降级到随机**：模型抽风的时候宁可不发，
      也不要发一张明显不搭的。发错比不发杀伤大。
    """
    from bigfish.replier import ModelConfig, call_model

    lines = []
    for i, e in enumerate(candidates):
        d = e.get("desc") or ""
        kind = "动图表情" if e.get("kind") == "sticker" else "图片"
        lines.append(f"{i}. [{kind}] {d or os.path.splitext(e['name'])[0]}")

    system = (
        "你在帮一个正在微信上聊天的人决定：说完这句话之后，要不要再发一张表情包。\n"
        "表情包不是必需品。大多数人发十句话才发一张表情。\n"
        "只有当这句话带有明显情绪（高兴、无语、得意、委屈、摆烂、鼓励），"
        "而且下面正好有一张真的贴合的时候，才发。\n"
        "拿不准就不发 —— 发一张不搭的表情比不发更尴尬。\n\n"
        "只输出一行 JSON，不要解释，不要代码块：\n"
        '{"pick": 序号或 -1}\n'
        "-1 表示这一轮不发。"
    )
    user = (
        f"她刚要说的话：{ctx.get('reply_text') or ''}\n"
        f"对方说的话：{ctx.get('user_text') or ''}\n\n"
        f"可用的表情（共 {len(candidates)} 张）：\n" + "\n".join(lines)
    )

    mc = ModelConfig.load()
    raw = call_model(mc, system, user)
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[-1] if "\n" in raw else raw
    try:
        obj = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        pick = int(obj.get("pick", -1))
    except Exception:
        return ""
    if pick < 0 or pick >= len(candidates):
        return ""
    return candidates[pick]["path"]


def stamp(ctx: Dict[str, Any]) -> str:
    """sticker 入口。返回要发的文件路径，空串表示这一轮不发。"""
    from bigfish import vault

    root = vault.resolve_data_root()
    cfg = ctx.get("config") or {}

    lib = [e for e in _lib(root) if _usable(e)]
    if not lib:
        return ""

    # 冷却：同一个会话刚发过就别再发。
    # ⚠ 引擎那边也有一层冷却。这里再查一次不是冗余 ——
    #   插件可以被单独调用（预览、测试），不能默认外面兜住了。
    chat_id = str(ctx.get("chat_id") or "")
    try:
        cool = int(cfg.get("cooldown", 180))
    except (TypeError, ValueError):
        cool = 180
    if cool > 0 and chat_id:
        try:
            raw = vault.sticker_usage("")
            rec = raw.get("_last") if isinstance(raw, dict) else None
            if isinstance(rec, dict) and rec.get("chat") == chat_id:
                if time.time() - int(rec.get("at") or 0) < cool:
                    return ""
        except Exception:
            pass

    mode = str(cfg.get("mode") or "ai")
    if mode == "random":
        return random.choice(lib)["path"]

    # 候选别太多：几十张塞进 prompt 反而让模型挑花眼，也浪费 token。
    # 优先带描述的（说明用户在意这张），其余随机凑数。
    with_desc = [e for e in lib if e.get("desc")]
    without = [e for e in lib if not e.get("desc")]
    random.shuffle(without)
    candidates = (with_desc + without)[:24]

    try:
        return _ask_model(candidates, ctx, cfg)
    except Exception as exc:
        print(f"[插件] sticker 让模型挑失败：{exc}", flush=True)
        return ""


register(
    "sticker",
    "表情包",
    "说完话之后，合适的话再跟一张表情",
    "sticker",
    default=False,
    sticker=stamp,
    config={
        "mode": {
            "type": "select",
            "label": "怎么挑",
            "note": "让模型判断该不该发、发哪张；随机则完全不看内容",
            "default": "ai",
            "options": [
                {"value": "ai", "label": "看这句话的意思挑（慢一点，但搭）"},
                {"value": "random", "label": "随便挑一张（快，常不搭）"},
            ],
        },
        "cooldown": {
            "type": "number",
            "label": "同一个会话隔多久才能再发",
            "note": "秒。0 = 不限。密度太高会被当成刷屏",
            "default": 180,
            "min": 0,
            "max": 3600,
            "step": 30,
        },
    },
    why=(
        "她只会打字的时候，聊天是干的。真实的人情绪到了会甩一张图，"
        "而不是把情绪写成一句话。没有这个插件，她永远体会不到「懒得说，发个图」。" ),
    affects=[
        "回复的文字之后可能多一条表情消息",
        "会额外调一次模型来判断该不该发（约 1 秒）",
        "只在表情库里有东西、且总开关打开时才可能发生",
    ],
    limits=[
        "不会去网上找图，只发你放进表情库的",
        "一条回复最多跟一张",
        "拿不准就不发 —— 发错比不发更尴尬",
        "总开关关着时，这个插件完全不会被调用",
    ],
)
