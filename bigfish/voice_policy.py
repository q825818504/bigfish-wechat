# -*- coding: utf-8 -*-
"""什么时候该出声：一轮的「发语音还是发文字」判定。

为什么不塞进 `voice.py`
-----------------------
合成是「怎么出」，这里是「出不出」。两件事分开，才能单独关：
用户可以把语音关掉而保留缓存/配置；也可以只改概率而不碰合成。

参考了两类成熟做法
------------------
- AstrBot 的 TTS 情绪路由：自动语音 / 文字+语音 / 分段 / 概率四类策略，
  按会话黑白名单、长度、冷却、概率门控；另有手动 `tts_say` 和 LLM 工具。
- MaiBot 的 MiMo 语音插件：`llm_trigger`（LLM 判断代码/网址/表格更适合
  文字）、`random`（按概率）、`voice_only`（全部转语音）；出站时原位替换，
  避免同一回复既发文字又发语音。

大肥鱼这边多一层：她有心情、生活状态、余韵。所以判定顺序是
「硬门槛 → 对方明确要求 → 模型标记 → 场景 → 策略掷骰」。

判定顺序（先否决，再掷骰）：
    1. 模式 off / 空文本
    2. 群聊、只对主人、长度、条数、代码/链接/表格/清单
    3. 对方明确要语音        → 出（绕过冷却/场景，但每天上限仍算）
    4. 模型写了 [[text]]     → 不出
    5. 模型写了 [[voice]]    → 出（绕过概率/场景，仍受冷却）
    6. 冷却 / 每日上限 / 连续语音上限
    7. 场景不允许            → 不出
    8. 模式 request          → 不出（没要求就不出）
    9. 模式 always           → 出
   10. 模式 probability/auto → 稳定掷骰
   11. 模式 preview          → 只返回判定，不合成、不发送

标记 `[[voice]]` / `[[text]]` 会从正文里删掉，绝不发给对方。
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

VOICE_TAG = re.compile(r"\[\[\s*voice\s*\]\]", re.I)
TEXT_TAG = re.compile(r"\[\[\s*(?:text|no[_-]?voice)\s*\]\]", re.I)
#: 表情标记。和语音标记走同一个清理点 —— 发给对方之前必须清干净。
#: `[[text]]` 是两件事共用的一句「别用非文字」：不出声、也不配图。
STICKER_TAG = re.compile(r"\[\[\s*(?:sticker|no[_-]?sticker)\s*\]\]", re.I)

#: 对方明确要语音。宁可窄一点：误判成"要语音"会让她突然发一条音频文件。
REQUEST_RE = re.compile(
    r"(发|来|说|讲|念|回).{0,6}(语音|声音|语音条)"
    r"|语音(说|回|发|条)"
    r"|想听.{0,6}(声音|说话|念)"
    r"|用声音(说|回)"
    r"|听你(说|念)"
)

URL_RE = re.compile(r"https?://|www\.", re.I)
CODE_RE = re.compile(r"```|`[^`\n]+`|^\s*(def|class|import|from|function|const|let|var)\s",
                     re.M)
TABLE_RE = re.compile(r"^\s*\|.*\|\s*$", re.M)
LIST_RE = re.compile(r"^\s*(?:[-*•]|\d+[.、)])\s+", re.M)

#: 场景开关的默认值。冲突和群聊默认不出声：这两处文字更稳。
DEFAULT_SCENES = {
    "sweet": True, "warm": True, "missing": True, "comfort": True,
    "low": True, "hurt": True, "tired": True, "worry": True,
    "neglected": True, "happy": True, "good_news": True,
    "late_night": True, "morning_wake": True, "neutral": True,
    "apology": True, "proactive": True, "guest": True,
    "conflict": False, "group_public": False,
}

VALID_MODES = {"off", "request", "auto", "probability", "always", "preview"}
VALID_DELIVERY = {"voice_only"}
VALID_SEND_AS = {"file", "bubble"}


def is_requested(text: str) -> bool:
    return bool(REQUEST_RE.search(text or ""))


def strip_tags(text: str) -> str:
    """把 [[voice]] / [[text]] / [[sticker]] 从正文里拿掉，发给对方之前一定要过。

    这些标记是模型对系统说的话，不是对对方说的话。漏一个，
    对方就会看到一句「[[sticker]]」——比不发图更糟。
    """
    return STICKER_TAG.sub("", TEXT_TAG.sub("", VOICE_TAG.sub("", text or ""))).strip()


@dataclass
class VoicePolicy:
    #: off / request / auto / probability / always / preview
    mode: str = "preview"
    #: 语音命中后只发语音，绝不再发同内容文字（text_then_voice 是旧配置）
    delivery: str = "voice_only"
    #: file：音频文件（现在可用）；bubble：微信语音气泡（需虚拟声卡）
    send_as: str = "file"
    probability: float = 0.2
    cooldown: int = 600              # 秒
    daily_limit: int = 12            # 0 = 不限
    consecutive_limit: int = 2       # 0 = 不限
    #: 220 字约 45–50 秒；真正的硬上限是合成后的 50 秒
    max_chars: int = 220
    #: 0 = 不看分条数：语音念整段，不跟文字分条绑定
    max_parts: int = 0
    group_enabled: bool = False
    owner_only: bool = False
    filter_url: bool = True
    filter_code: bool = True
    filter_table: bool = True
    filter_list: bool = True
    tag_override: bool = True
    fallback_text: bool = True
    scenes: Dict[str, bool] = field(
        default_factory=lambda: dict(DEFAULT_SCENES))

    @classmethod
    def from_config(cls, cfg: Optional[Mapping[str, Any]]) -> "VoicePolicy":
        cfg = dict(cfg or {})
        obj = cls()
        for key in ("mode", "delivery", "send_as"):
            if cfg.get(key):
                setattr(obj, key, str(cfg[key]))
        if obj.mode not in VALID_MODES:
            obj.mode = "preview"
        # 旧配置的 text_then_voice 一律迁到 voice_only：同一内容不允许
        # 文字和语音各发一遍。
        if obj.delivery not in VALID_DELIVERY:
            obj.delivery = "voice_only"
        if obj.send_as not in VALID_SEND_AS:
            obj.send_as = "file"
        for key in ("probability",):
            if cfg.get(key) is not None:
                setattr(obj, key, max(0.0, min(1.0, float(cfg[key]))))
        for key in ("cooldown", "daily_limit", "consecutive_limit",
                    "max_chars", "max_parts"):
            if cfg.get(key) is not None:
                setattr(obj, key, max(0, int(cfg[key])))
        for key in ("group_enabled", "owner_only", "filter_url",
                    "filter_code", "filter_table", "filter_list",
                    "tag_override", "fallback_text"):
            if key in cfg:
                setattr(obj, key, bool(cfg[key]))
        raw_scenes = cfg.get("scenes")
        if isinstance(raw_scenes, dict):
            for key, val in raw_scenes.items():
                if key in obj.scenes:
                    obj.scenes[key] = bool(val)
        return obj

    def as_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode, "delivery": self.delivery,
            "send_as": self.send_as, "probability": self.probability,
            "cooldown": self.cooldown, "daily_limit": self.daily_limit,
            "consecutive_limit": self.consecutive_limit,
            "max_chars": self.max_chars, "max_parts": self.max_parts,
            "group_enabled": self.group_enabled,
            "owner_only": self.owner_only,
            "filter_url": self.filter_url, "filter_code": self.filter_code,
            "filter_table": self.filter_table, "filter_list": self.filter_list,
            "tag_override": self.tag_override,
            "fallback_text": self.fallback_text,
            "scenes": dict(self.scenes),
        }


@dataclass
class VoiceDecision:
    want: bool
    reason: str = ""
    mode: str = "preview"
    text: str = ""
    delivery: str = "voice_only"
    send_as: str = "file"
    preview: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"want": self.want, "reason": self.reason,
                "mode": self.mode, "delivery": self.delivery,
                "send_as": self.send_as, "preview": self.preview}


def _roll(chat_id: str, text: str, now: float) -> float:
    """稳定掷骰：同一会话、同一句、同一分钟内结果一致，方便复现。"""
    bucket = int(now // 60)
    digest = hashlib.sha1(
        f"{chat_id}\n{text}\n{bucket}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") / 0xFFFFFFFF


def _content_blocked(body: str, policy: VoicePolicy) -> str:
    if policy.filter_url and URL_RE.search(body):
        return "含链接"
    if policy.filter_code and CODE_RE.search(body):
        return "含代码"
    if policy.filter_table and len(TABLE_RE.findall(body)) >= 2:
        return "含表格"
    if policy.filter_list and len(LIST_RE.findall(body)) >= 2:
        return "含清单"
    return ""


def decide(*, text: str, chat_id: str = "", parts: Optional[List[str]] = None,
           policy: VoicePolicy, requested: bool = False,
           is_group: bool = False, is_owner: bool = True,
           scene: str = "neutral", last_voice_at: float = 0.0,
           today_count: int = 0, consecutive: int = 0,
           now: Optional[float] = None) -> VoiceDecision:
    """返回「这一轮要不要出声」。不调用模型、不花钱。"""
    now = time.time() if now is None else now
    body = (text or "").strip()
    if not body:
        return VoiceDecision(False, "空文本", policy.mode)

    # 标记要从正文里拿掉：它是给发送层的指令，不该被念出来
    has_voice_tag = bool(VOICE_TAG.search(body))
    has_text_tag = bool(TEXT_TAG.search(body))
    body = TEXT_TAG.sub("", VOICE_TAG.sub("", body)).strip()
    parts = list(parts or [body])

    base = VoiceDecision(False, "", policy.mode, body,
                         policy.delivery, policy.send_as,
                         policy.mode == "preview")

    if policy.mode == "off":
        base.reason = "语音模式关闭"
        return base
    if is_group and not policy.group_enabled:
        base.reason = "群里不发声"
        return base
    if policy.owner_only and not is_owner:
        base.reason = "只对主人发声"
        return base
    if len(body) > policy.max_chars:
        base.reason = f"太长（>{policy.max_chars}字）"
        return base
    if policy.max_parts and len(parts) > policy.max_parts:
        base.reason = f"条数太多（{len(parts)}>{policy.max_parts}）"
        return base
    blocked = _content_blocked(body, policy)
    if blocked:
        base.reason = f"{blocked}，更适合文字"
        return base

    # 对方明确要求优先级最高：模型写 [[text]] 也盖不过"我想听你说"。
    if requested:
        if policy.daily_limit and today_count >= policy.daily_limit:
            base.reason = f"今天语音已到上限（{policy.daily_limit}）"
            return base
        base.want = True
        base.reason = "对方要语音"
        base.preview = policy.mode == "preview"
        return base

    if policy.tag_override and has_text_tag:
        base.reason = "模型标记 [[text]]"
        return base
    if policy.tag_override and has_voice_tag:
        if last_voice_at and now - last_voice_at < policy.cooldown:
            base.reason = "模型要语音，但还在冷却"
            return base
        if policy.daily_limit and today_count >= policy.daily_limit:
            base.reason = f"今天语音已到上限（{policy.daily_limit}）"
            return base
        base.want = True
        base.reason = "模型标记 [[voice]]"
        base.preview = policy.mode == "preview"
        return base

    if last_voice_at and now - last_voice_at < policy.cooldown:
        left = int(policy.cooldown - (now - last_voice_at))
        base.reason = f"冷却中（{left}s）"
        return base
    if policy.daily_limit and today_count >= policy.daily_limit:
        base.reason = f"今天语音已到上限（{policy.daily_limit}）"
        return base
    if policy.consecutive_limit and consecutive >= policy.consecutive_limit:
        base.reason = f"连续 {consecutive} 条语音，换文字"
        return base

    # always 不挑场景：用户明确说"每条都发"，冲突时也照发。
    if policy.mode == "always":
        base.want = True
        base.reason = "总是语音"
        return base

    if not policy.scenes.get(scene or "neutral", True):
        base.reason = f"场景「{scene}」不出声"
        return base
    if policy.mode == "request":
        base.reason = "没要求语音"
        return base
    if policy.mode in ("probability", "auto", "preview"):
        hit = _roll(chat_id, body, now) < policy.probability
        base.want = hit
        base.reason = (f"{'预演' if policy.mode == 'preview' else '概率'}"
                       f" {policy.probability:.0%}"
                       + (" 命中" if hit else " 未中"))
        return base
    base.reason = f"mode={policy.mode} 不触发"
    return base
