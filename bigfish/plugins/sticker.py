"""[插件] 表情包 —— 让她也会说话说到一半甩一张图。

═══════════════════════════════════════════════════════════════════════
这个插件管什么
═══════════════════════════════════════════════════════════════════════

只管**行为**：这一轮要不要发、发哪张。

不管**素材**：库里有什么、每张什么描述、怎么转 GIF —— 那是
bigfish/stickers.py 和「表情包」页的事。

分开的理由：用户想"关掉表情"时该在插件页找到开关，而不是
先去表情包页翻半天。两个页面控制两件不同的事。

═══════════════════════════════════════════════════════════════════════
怎么挑
═══════════════════════════════════════════════════════════════════════

mode=ai（默认）：把库里的**代码 + 概览**给模型，问「该不该配一张，
配哪张」。模型回答代码。

    为什么给代码而不是文件名：代码一辈子不变，文件名会改。
    为什么给概览而不是详细：详细太长，几十张塞进 prompt 会爆；
        概览（"生气"/"害羞"）正好是判断所需的最小信息。

mode=random：不看内容随机挑。便宜，但经常不搭。

为什么用模型而不是关键词匹配：
    表情包的关键是**语义**。"你说得对"和"确实"是同一个意思，
    关键词表永远写不全。而模型一秒钟就能判断。

═══════════════════════════════════════════════════════════════════════
边界
═══════════════════════════════════════════════════════════════════════

  · 不发库里没有的。不会去网上找图。
  · 一条回复最多跟一张。
  · 拿不准就不发 —— 发错比不发更尴尬。
  · 关掉插件 = 完全不参与，连日志都不会有。
"""
from __future__ import annotations

import json
import os
import random
import time
from typing import Any, Dict, List

from . import register

#: 微信把 >1024 的图当文件发；这之上就不再是表情了。
_MAX_DIM = 1024

#: 一次给模型看多少张候选。太多会让它挑花眼，也浪费 token。
_MAX_CANDIDATES = 24


def _lib() -> List[Dict[str, Any]]:
    from bigfish import stickers

    return stickers.list_stickers(with_detail=False)


def _usable(entry: Dict[str, Any]) -> bool:
    """快速过滤：文件在、不太大、尺寸不过界。

    尺寸读不出来时**放行** —— 没有探测能力不等于文件不能用。
    """
    p = entry.get("path") or ""
    if not os.path.isfile(p):
        return False
    try:
        if os.path.getsize(p) > 8 * 1024 * 1024:
            return False
    except OSError:
        return False
    try:
        from PIL import Image

        with Image.open(p) as im:
            w, h = im.size
        if max(w, h) > _MAX_DIM:
            return False
    except Exception:
        pass
    return True


def _ask_model(candidates: List[Dict[str, Any]], ctx: Dict[str, Any]) -> str:
    """让模型从候选里挑一张，或决定不挑。返回代码（空 = 不挑）。

    ⚠ 这里刻意**不重试、不降级到随机**：模型抽风的时候宁可不发，
      也不要发一张明显不搭的。发错比不发杀伤大。
    """
    from bigfish.replier import ModelConfig, call_model

    lines = []
    for e in candidates:
        code = e.get("code") or ""
        brief = e.get("brief") or ""
        # 没有 brief 的用代码兜底 —— 至少模型知道有这么一张
        label = brief or "（没写说明）"
        frames = "动图" if int(e.get("frames") or 1) > 1 else "静图"
        lines.append(f"- {code} [{frames}] {label}")

    system = (
        "你在帮一个正在微信上聊天的人决定：说完这句话之后，要不要再发一张表情包。\n"
        "表情包不是必需品。大多数人发十句话才发一张表情。\n"
        "只有当这句话带有明显情绪（高兴、无语、得意、委屈、摆烂、鼓励），"
        "而且下面正好有一张真的贴合的时候，才发。\n"
        "拿不准就不发 —— 发一张不搭的表情比不发更尴尬。\n\n"
        "只输出一行 JSON，不要解释，不要代码块：\n"
        '{"pick": "stk_0007"}\n'
        '不发表情时 pick 用空字符串。'
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
        pick = str(obj.get("pick") or "").strip()
    except Exception:
        return ""
    if not pick:
        return ""
    # 只认候选里的代码 —— 模型可能编一个不存在的
    for e in candidates:
        if e.get("code") == pick:
            return e["path"]
    return ""


def stamp(ctx: Dict[str, Any]) -> str:
    """sticker 入口。返回要发的文件路径，空串表示这一轮不发。"""
    from bigfish import stickers

    cfg = ctx.get("config") or {}

    lib = [e for e in _lib() if _usable(e)]
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
        last = stickers.last_sent(chat_id)
        if last and time.time() - int(last.get("at") or 0) < cool:
            return ""

    mode = str(cfg.get("mode") or "ai")
    if mode == "random":
        return random.choice(lib)["path"]

    # 候选：优先有 brief 的（用户/模型写过说明，说明这张被在意），
    # 其余随机凑数。
    with_desc = [e for e in lib if e.get("brief")]
    without = [e for e in lib if not e.get("brief")]
    random.shuffle(without)
    candidates = (with_desc + without)[:_MAX_CANDIDATES]

    try:
        return _ask_model(candidates, ctx)
    except Exception as exc:
        print(f"[插件] sticker 让模型挑失败：{exc}", flush=True)
        return ""



# ══════════════════════════════════════════════════════════════════════
# 学习：主人纠正用法时，改那张表情的备注
# ══════════════════════════════════════════════════════════════════════
#
# 场景（用户的原话）：
#   「在对话过程中，主人告诉他这个表情包不是这么用的，她会积极学习，
#     并修改表情包库对应表情包的备注信息」
#
# 为什么值得做：
#   自动备注是模型看图猜的，猜得再准也会偏 —— 同一张图在不同人眼里
#   意思可能不一样（"无语"还是"鄙视"）。而主人知道她自己那张图的意思。
#   他一句纠正，比模型猜十次都准。
#
# 两道门，缺一不可：
#   ① 关键词粗筛 —— 省掉 99% 的模型调用
#   ② 模型判断 —— 关键词会误伤（"这个表情真好看"不是纠正）
#
# 只改**最近刚发的那张**。翻旧账（"你上个月那个表情用错了"）不处理 ——
# 分不清指的是哪张，乱改比不改糟。

#: 粗筛关键词。命中任一才去问模型。
#: 刻意写得宽一点：多问一次模型不贵（1 秒），漏学一次很贵（她会一直用错）。
_CORRECTION_HINTS = (
    "不是这么用", "不是这个意思", "用错", "别用", "不要用", "少用",
    "这个表情", "那张表情", "表情不对", "意思不对",
    "应该是", "其实是", "表示的是", "意思是",
)

#: 只在发出表情后这么久之内才算"在说这张"
_LEARN_WINDOW = 300.0


def _learn_prompt(item: Dict[str, Any], text: str):
    """构造「这是不是在纠正」的提示词。返回 (system, user)。"""
    system = "\n".join([
        "你在帮一个聊天机器人整理她的表情包。",
        "她刚发了一张表情，对方接着说了一句话。",
        "你要判断：对方是不是在**纠正这张表情的用法或含义**。",
        "",
        "算纠正的例子：",
        "  「这个不是生气，是无语」",
        "  「别用这个，看着凶」",
        "  「这个应该在我夸你的时候用」",
        "不算纠正的例子：",
        "  「这个表情好可爱」（只是评价）",
        "  「哈哈哈哈」（无关）",
        "  「你发的是什么」（没在说含义）",
        "",
        "如果是纠正，给出新的 brief（2~4 字）和 detail（一句话，"
        "说明什么情景用）。要采纳对方说的意思，但保留原来准确的画面描述。",
        "",
        "只输出一行 JSON，不要解释：",
        '{"correct": true, "brief": "...", "detail": "..."}',
        '不是纠正就输出 {"correct": false}',
    ])
    user = "\n".join([
        "她刚发的表情——",
        f"  概览：{item.get('brief') or '（没写）'}",
        f"  详细：{item.get('detail') or '（没写）'}",
        "",
        f"对方接着说：{text}",
    ])
    return system, user


def learn(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """看看主人是不是在纠正上一张表情的用法。是就更新备注。

    返回 {"learned": bool, "code": str, "brief": str, "detail": str, "why": str}
    **任何失败都返回 learned=False，不抛** —— 学不会不该影响聊天。
    """
    from bigfish import stickers

    out: Dict[str, Any] = {"learned": False, "code": "", "brief": "",
                           "detail": "", "why": ""}

    text = str(ctx.get("user_text") or "").strip()
    chat_id = str(ctx.get("chat_id") or "")
    if not text or not chat_id:
        return out

    # ① 最近刚发过表情吗
    last = stickers.last_sent(chat_id)
    if not last:
        out["why"] = "最近没发过表情"
        return out
    if time.time() - float(last.get("at") or 0) > _LEARN_WINDOW:
        out["why"] = "表情发出去太久了"
        return out

    code = str(last.get("code") or "")
    item = stickers.get_sticker(code)
    if not item:
        out["why"] = "那张表情已经不在库里了"
        return out

    # ② 关键词粗筛
    if not any(k in text for k in _CORRECTION_HINTS):
        out["why"] = "没听出在说表情"
        return out

    # ③ 问模型：这是纠正吗？新备注该写什么？
    try:
        from bigfish.replier import ModelConfig, call_model
    except Exception as exc:
        out["why"] = f"模型不可用：{exc}"
        return out

    system, user = _learn_prompt(item, text)

    try:
        mc = ModelConfig.load()
        raw = call_model(mc, system, user)
    except Exception as exc:
        out["why"] = f"调模型失败：{exc}"
        return out

    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[-1] if "\n" in raw else raw
    try:
        obj = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except Exception:
        out["why"] = "模型没给出可解析的结果"
        return out

    if not obj.get("correct"):
        out["why"] = "模型判断不是在纠正"
        return out

    brief = str(obj.get("brief") or "").strip()
    detail = str(obj.get("detail") or "").strip()
    if not brief and not detail:
        out["why"] = "模型说对但没给内容"
        return out

    ok, err = stickers.update_sticker(
        code, brief=brief or None, detail=detail or None, auto=False)
    if not ok:
        out["why"] = f"写回失败：{err}"
        return out

    out.update({"learned": True, "code": code,
                "brief": brief, "detail": detail})
    return out


def should_learn(ctx: Dict[str, Any]) -> bool:
    """便宜的预判：值不值得调 learn()。

    引擎每轮都会问一次，不能让它每次都去读磁盘 + 可能调模型。
    这里只做最轻的检查：关键词命中 + 有最近发送记录且在窗口内。
    """
    text = str(ctx.get("user_text") or "").strip()
    chat_id = str(ctx.get("chat_id") or "")
    if not text or not chat_id:
        return False
    if not any(k in text for k in _CORRECTION_HINTS):
        return False
    try:
        from bigfish import stickers

        last = stickers.last_sent(chat_id)
        if not last:
            return False
        return (time.time() - float(last.get("at") or 0)) <= _LEARN_WINDOW
    except Exception:
        return False



register(
    "sticker",
    "表情包",
    "说完话之后，合适的话再跟一张表情",
    "sticker",
    default=False,
    sticker=stamp,
    config={
        "rate": {
            "type": "number",
            "label": "多频繁",
            "note": "每一轮回复有多大概率考虑配一张。0.15 大约十句一张",
            "default": 0.15,
            "min": 0,
            "max": 1,
            "step": 0.05,
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
    },
    why=(
        "她只会打字的时候，聊天是干的。真实的人情绪到了会甩一张图，"
        "而不是把情绪写成一句话。没有这个插件，她永远体会不到"
        "「懒得说，发个图」。"),
    affects=[
        "回复的文字之后可能多一条表情消息",
        "会额外调一次模型来判断该不该发（约 1 秒）",
        "只在表情库里有东西时才可能发生",
    ],
    limits=[
        "不会去网上找图，只发你放进表情库的",
        "一条回复最多跟一张",
        "拿不准就不发 —— 发错比不发更尴尬",
        "关掉之后完全不会被调用",
    ],
)
