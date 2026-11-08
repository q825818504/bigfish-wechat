#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""voice_director.py —— 把「她此刻的状态」翻译成「这一句怎么念」。

Breeze 的 `instructions` 不是标签，是一段**自然语言表演指令**：
情境、意图、情绪强度、节奏、身体状态。它和正文分开传，正文一个字都不改。

所以这一层做的事不是「加点情绪词」，而是回答四个问题：

    1. 现在是什么场合？（深夜 / 刚醒 / 群里 / 对方难受 / 刚拌嘴 / 被要语音）
    2. 她此刻是什么状态？（mood 分值 + 七根情绪 + 精力 + 余韵 + 被冷落）
    3. 这句话想达到什么？（安慰 / 嘴硬 / 撒娇 / 报平安 / 试探）
    4. 因此声音该怎么走？（语速、停顿、尾音、气声、音量、guidance）

输出三样东西：

    text          正文（只在合适的位置插 0~2 个中文音频标签，如 [叹气]）
    instructions  给模型的表演指令（中文，和正文同语言）
    voice_settings  speed / guidance_scale / volume

**不做什么**：不改写正文、不替模型决定说什么、不在每句都加标签。
标签是「声音事件」，不是标点；一段话里塞三个 [笑] 会像配音事故。

和现有状态对接
--------------
    mood.json      value 0..100 + dims{happy,calm,sweet,heartache,worry,sad,jealous}
    afterglow.json conflict / hurt / worry / warm（4h / 2h / 3h / 1h 内有效）
    life.json      asleep / energy / doing
    proactive      neglect_level 1..3
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

try:  # 包内用相对导入；单独跑 demo 时退回同目录
    from .voice_text import clean_for_tts
except ImportError:  # pragma: no cover
    from voice_text import clean_for_tts  # type: ignore

DIMS = ("happy", "calm", "sweet", "heartache", "worry", "sad", "jealous")


# ══════════════════════════════════════════════════════════════════════
# 一、场景：先定「这是什么场合」，再谈情绪
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Scene:
    key: str
    name: str
    instruction: str
    #: 相对基准的修正（基准 speed=1.0 / guidance=2.0）
    speed: float = 0.0
    guidance: float = 0.0
    volume: float = 0.0
    tags: Sequence[str] = ()
    #: 这个场景是不是「私下」的（群里不用）
    private: bool = True


SCENES: Dict[str, Scene] = {
    s.key: s for s in (
        Scene(
            "late_night", "深夜",
            "像深夜贴着手机轻声说话：她有点困，但还不想挂。语速慢半拍，"
            "句尾收住，气声多一点；别撒娇过头，也别拖长音。",
            speed=-0.04, guidance=0.4, volume=-0.05, tags=("[呼气]",),
        ),
        Scene(
            "morning_wake", "刚醒",
            "刚醒不久，嗓子还没完全打开：语速慢半拍，带一点鼻音和慵懒，"
            "句尾不要上扬；像还没坐起来。",
            speed=-0.05, guidance=0.3, tags=("[吸气]",),
        ),
        Scene(
            "tired", "累了",
            "她今天有点累：声音放轻，句子短，停顿多一点；不是抱怨，"
            "只是没力气扬声。",
            speed=-0.04, guidance=0.3, tags=("[叹气]",),
        ),
        Scene(
            "low", "低落",
            "她情绪不高：语调偏低，语速慢，尾音收住；别把难过演出来，"
            "像在克制。",
            speed=-0.05, guidance=0.6, tags=("[叹气]",),
        ),
        Scene(
            "hurt", "委屈",
            "她心里还有点委屈：声音软但发紧，说到一半停一下，像把话咽回去；"
            "不要哭腔，也不要立刻装作没事。",
            speed=-0.03, guidance=0.7, tags=("[停顿]",),
        ),
        Scene(
            "conflict", "还没消气",
            "刚拌过嘴，还没消气：语气收着，句子短，停顿明显；关心的话"
            "别说得太甜，像嘴硬心软。",
            speed=-0.01, guidance=0.6, tags=("[停顿]",),
        ),
        Scene(
            "worry", "担心",
            "她有点悬着心：语速略快一拍，句尾收住，像在确认对方没事；"
            "别追问，也别用哄小孩的语气。",
            speed=0.02, guidance=0.4, tags=("[吸气]",),
        ),
        Scene(
            "warm", "被暖到",
            "刚才那句话让她心里暖了一下：声音放软，尾音轻，带一点"
            "藏不住的笑；不用说得太满。",
            speed=0.01, guidance=0.2, tags=("[轻笑]",),
        ),
        Scene(
            "happy", "开心",
            "她心情不错：语速略快，句尾轻轻上扬，笑意在声音里；不要喊，"
            "也不要每句都笑。",
            speed=0.03, guidance=0.0, tags=("[笑]",),
        ),
        Scene(
            "sweet", "亲近",
            "她在跟亲近的人说话：声音软、贴近，语速自然，尾音轻轻收；"
            "别黏得过火。",
            speed=0.0, guidance=0.3, tags=("[轻笑]",),
        ),
        Scene(
            "neglected", "被冷落",
            "她这两天没怎么被搭理：声音收着，句子短，不主动撒娇；"
            "可对方一开口，她还是应，尾音别太冷。",
            speed=-0.02, guidance=0.4, tags=("[停顿]",),
        ),
        Scene(
            "comfort", "安慰",
            "对方正难受：她放低声音，慢一点，停顿留出来，像在斟酌；"
            "先接住情绪，不讲道理。",
            speed=-0.04, guidance=0.6, tags=("[叹气]",),
        ),
        Scene(
            "apology", "歉疚",
            "她知道自己刚才说得不好：声音低一点，语速慢，句子短；"
            "不急着解释，也不抢着被原谅。",
            speed=-0.04, guidance=0.5, tags=("[停顿]",),
        ),
        Scene(
            "good_news", "替你高兴",
            "对方带来好消息：她真的高兴，语速快半拍，尾音上扬；"
            "别用播音腔，像在电话这头笑出来。",
            speed=0.04, guidance=0.1, tags=("[笑]",),
        ),
        Scene(
            "missing", "被想念",
            "对方说想她：她心里软了一下，声音低而近，尾音轻；"
            "不用句句回应，也别立刻撒娇。",
            speed=0.0, guidance=0.3, tags=("[轻笑]",),
        ),
        Scene(
            "group_public", "群里",
            "在群里说话：清楚、自然、稍微亮一点；别露出私下的黏糊，"
            "也别像在念通知。",
            speed=0.02, guidance=0.0, tags=(),
        ),
        Scene(
            "guest", "不太熟的人",
            "对不太熟的人说话：客气、清楚，语速正常；不要用私下那种"
            "软尾音，也别太热络。",
            speed=0.01, guidance=0.0, tags=(),
        ),
        Scene(
            "proactive", "主动开口",
            "她主动找对方，有点试探：声音轻，句子短，像不确定对方在不在；"
            "别一上来就黏。",
            speed=-0.01, guidance=0.3, tags=("[吸气]",),
        ),
        Scene(
            "requested", "对方要语音",
            "对方想听她的声音：直接对着他说，贴近、自然；不要朗读腔，"
            "也不要刻意压低。",
            speed=0.0, guidance=0.2, tags=(),
        ),
        Scene(
            "neutral", "平常",
            "平常说话：自然、松弛，像熟人聊天；不要朗读腔，句尾别拖。",
            speed=0.0, guidance=0.0, tags=(),
        ),
    )
}


#: 音色基线。同一个「低落」，明亮少女音不该靠压低嗓子演。
PROFILES: Dict[str, Dict[str, Any]] = {
    "bright_young": {
        "name": "明亮少女音（薄荷）",
        "speed_min": 0.94,
        "speed_max": 1.04,
        #: 用户定死 1.00：情绪只走 instructions / 停顿，不用 time-stretch 改语速
        "speed_fixed": 1.0,
        "instructions": {
            "late_night": "像深夜贴着手机轻声说话：把平时的亮收一点，停顿多一点，"
                          "句尾收住；别撒娇过头，也别拖长音。",
            "low": "她情绪不高：把平时的亮度收住，停顿多一点，尾音收住；"
                   "不要刻意压低嗓子装低沉，也别把难过演出来。",
            "hurt": "她心里还有点委屈：声音软下来，亮度收住，说到一半停一下，"
                    "像把话咽回去；不要哭腔，也不要立刻装作没事。",
            "conflict": "刚拌过嘴，还没消气：把俏皮的尾音收掉，语气放平，句子短，"
                        "停顿明显；关心的话别说得太甜，像嘴硬心软。",
            "sweet": "她在跟亲近的人说话：声音软、贴近，保留一点平时的俏皮，"
                     "但别黏得过火。",
        },
    },
}


# ══════════════════════════════════════════════════════════════════════
# 二、上下文
# ══════════════════════════════════════════════════════════════════════


@dataclass
class VoiceContext:
    """一轮里导演需要知道的全部。字段名对齐 bigfish 现有状态。"""

    text: str
    #: 对方这一轮说了什么（用来判断「对方难受 / 道歉 / 好消息 / 想她」）
    user_text: str = ""
    #: mood.json 的 value / dims
    mood: float = 65.0
    dims: Dict[str, Any] = field(default_factory=dict)
    #: life.json
    energy: float = 70.0
    asleep: bool = False
    doing: str = ""
    #: 时段
    hour: int = 21
    #: 会话
    is_group: bool = False
    is_owner: bool = True
    #: afterglow 当前有效的 kind（可能多条，取最重）
    afterglow: Sequence[str] = ()
    #: proactive.neglect_level
    neglect_level: int = 0
    #: 触发原因
    requested: bool = False
    wake: bool = False
    proactive: bool = False
    #: 显式指定场景（调试/测试用）
    scene: str = ""
    #: 音色基线（bright_young / 空）
    profile: str = ""

    def dim(self, key: str) -> float:
        try:
            return float(self.dims.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0


@dataclass
class VoicePlan:
    text: str
    instructions: str
    scene: str = "neutral"
    scene_name: str = "平常"
    language: str = "zh"
    model: str = "breeze-tts-2"
    voice: str = ""
    speed: float = 1.0
    guidance: float = 2.0
    volume: float = 1.0
    tags: List[str] = field(default_factory=list)
    reason: str = ""

    def apply_to(self, cfg: Any) -> Any:
        """写进 `voice_providers.VoiceConfig`，返回同一个对象。"""
        cfg.instructions = self.instructions
        cfg.language_code = self.language
        cfg.model = self.model
        cfg.speed = self.speed
        cfg.guidance_scale = self.guidance
        cfg.volume = self.volume
        if self.voice:
            cfg.voice = self.voice
        return cfg

    def as_dict(self) -> Dict[str, Any]:
        return {
            "scene": self.scene, "scene_name": self.scene_name,
            "text": self.text, "instructions": self.instructions,
            "language": self.language, "model": self.model,
            "voice": self.voice, "speed": self.speed,
            "guidance": self.guidance, "volume": self.volume,
            "tags": self.tags, "reason": self.reason,
        }


# ══════════════════════════════════════════════════════════════════════
# 三、判断
# ══════════════════════════════════════════════════════════════════════

_USER_SCENES = (
    ("comfort", re.compile(r"难受|难过|想哭|崩溃|不开心|委屈|累死|烦死|压力|失眠")),
    ("apology", re.compile(r"对不起|抱歉|是我不好|别生气|我错了|原谅")),
    ("good_news", re.compile(r"升职|加薪|通过了|考过|拿到|赢了|成功|好消息|录取|offer", re.I)),
    ("missing", re.compile(r"想你|想我|在吗|干嘛呢|睡了吗|陪我")),
)


def _user_scene(text: str) -> str:
    for key, pattern in _USER_SCENES:
        if pattern.search(text or ""):
            return key
    return ""


def _text_scene(text: str) -> str:
    """正文自己暗示了场合。她说「还不睡吗」时，不该按下午的平常语气念。"""
    t = text or ""
    if re.search(r"晚安|睡吧|睡觉|还不睡|困了|闭眼|做梦", t):
        return "late_night"
    if re.search(r"早安|早上好|刚醒|起床|醒了", t):
        return "morning_wake"
    return ""


def _pick_scene(ctx: VoiceContext) -> str:
    if ctx.scene and ctx.scene in SCENES:
        return ctx.scene
    if ctx.requested:
        return "requested"
    # 余韵优先于「对方这句话」：刚拌过嘴时，对方说句好听的也不该立刻变甜
    for kind in ("conflict", "hurt", "worry", "warm"):
        if kind in ctx.afterglow:
            return kind
    found = _user_scene(ctx.user_text)
    if found:
        return found
    hinted = _text_scene(ctx.text)
    if hinted:
        return hinted
    if ctx.asleep or ctx.hour >= 23 or ctx.hour < 6:
        return "late_night"
    if ctx.wake or 6 <= ctx.hour < 9:
        return "morning_wake"
    if ctx.is_group:
        return "group_public"
    if not ctx.is_owner:
        return "guest"
    if ctx.mood <= 35 or ctx.dim("sad") >= 55:
        return "low"
    if ctx.dim("happy") >= 60 and ctx.mood >= 72:
        return "happy"
    if ctx.dim("sweet") >= 55:
        return "sweet"
    if ctx.neglect_level >= 2:
        return "neglected"
    if ctx.energy < 35:
        return "tired"
    if ctx.proactive:
        return "proactive"
    return "neutral"


# ══════════════════════════════════════════════════════════════════════
# 四、标签：插在哪、插几个
# ══════════════════════════════════════════════════════════════════════

#: 句子边界（插在标点之后，不在句首堆）
_BOUNDARY = "。！？…；;"


def _insert_tag(text: str, tag: str) -> str:
    """在第一个句末标点后插标签。太短就放句首，没标点就放末尾。"""
    if not tag or tag in text:
        return text
    # 吸气/哈欠这类是「开口前」的动作，放句首才自然
    if tag in ("[吸气]", "[打哈欠]", "[清嗓]", "[咳嗽]", "[吸鼻子]"):
        return tag + text
    if len(text) <= 6:
        return tag + text
    for i, ch in enumerate(text):
        if ch in _BOUNDARY and i >= 2:
            return text[:i + 1] + tag + text[i + 1:]
    for i, ch in enumerate(text):
        if ch in "，,、" and i >= 4:
            return text[:i + 1] + tag + text[i + 1:]
    return text + tag


def _choose_tags(ctx: VoiceContext, scene: Scene, text: str) -> List[str]:
    """0~2 个。标签是声音事件，不是情绪装饰。"""
    existing = re.findall(r"\[[^\[\]]{1,6}\]|\([^()]{1,20}\)", text)
    if existing:                      # 正文自己带了，别叠加
        return []
    tags: List[str] = list(scene.tags)[:1]
    # 情绪再加一个：只在正文够长（>=14 字）时加，短句一个就够
    if len(text) >= 14:
        if ctx.dim("sweet") >= 60 and "[轻笑]" not in tags:
            tags.append("[轻笑]")
        elif ctx.dim("happy") >= 65 and "[笑]" not in tags:
            tags.append("[笑]")
        elif ctx.dim("jealous") >= 45 and "[哼鼻子]" not in tags:
            tags.append("[哼鼻子]")
        elif ctx.dim("sad") >= 60 and "[叹气]" not in tags:
            tags.append("[叹气]")
    # 去重 + 去掉正文里已经出现的字面
    out: List[str] = []
    for t in tags:
        core = t.strip("[]()")
        if t in out or core in text:
            continue
        out.append(t)
    return out[:2]


# ══════════════════════════════════════════════════════════════════════
# 五、组装
# ══════════════════════════════════════════════════════════════════════


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def direct(ctx: VoiceContext) -> VoicePlan:
    scene_key = _pick_scene(ctx)
    scene = SCENES[scene_key]
    text = clean_for_tts(ctx.text)
    if not text:
        text = "嗯。"

    speed = 1.0 + scene.speed
    guidance = 2.0 + scene.guidance
    volume = 1.0 + scene.volume

    # ── 七根情绪对声音的修正（保守，不抢场景的主导）──
    sad, heartache = ctx.dim("sad"), ctx.dim("heartache")
    worry, sweet = ctx.dim("worry"), ctx.dim("sweet")
    happy, jealous = ctx.dim("happy"), ctx.dim("jealous")
    if sad >= 40:
        speed -= 0.02
        guidance += 0.2
    if heartache >= 40:
        guidance += 0.2
        volume -= 0.02
    if worry >= 40:
        speed += 0.01
    if sweet >= 50 and scene.private and not ctx.is_group:
        guidance += 0.15
    if happy >= 60:
        speed += 0.01
    if jealous >= 40:
        guidance += 0.15

    # 被冷落：收着，不主动撒娇
    if ctx.neglect_level >= 2 and scene.private:
        guidance += 0.2
        volume -= 0.03

    speed = round(_clamp(speed, 0.9, 1.08), 2)
    guidance = round(_clamp(guidance, 1.2, 3.4), 1)
    volume = round(_clamp(volume, 0.85, 1.0), 2)

    profile = PROFILES.get(ctx.profile)
    if profile:
        speed = round(_clamp(speed, float(profile["speed_min"]),
                             float(profile["speed_max"])), 2)
        if profile.get("speed_fixed"):
            speed = float(profile["speed_fixed"])

    tags = _choose_tags(ctx, scene, text)
    for tag in tags:
        text = _insert_tag(text, tag)

    instruction = scene.instruction
    if profile:
        instruction = profile.get("instructions", {}).get(scene_key, instruction)
    parts = [instruction]
    # 手头的事：只在「平常/主动」里说，且不能喧宾夺主
    if (ctx.doing and scene_key in ("neutral", "proactive")
            and not ctx.is_group and ctx.is_owner):
        parts.append(f"她刚才在{ctx.doing.strip('，。 ')}，不是一直守着手机。")
    if ctx.neglect_level >= 2 and scene_key not in ("conflict", "hurt",
                                                    "neglected"):
        parts.append("她这两天没怎么被搭理，声音里有一点收着，不主动撒娇。")
    instructions = "".join(parts)
    if len(instructions) > 900:              # 文档建议 1000 字以内
        instructions = instructions[:900]

    reason = (
        f"场景={scene.name}；mood={ctx.mood:.0f}，"
        f"happy={happy:.0f}/sweet={sweet:.0f}/sad={sad:.0f}/"
        f"worry={worry:.0f}/heartache={heartache:.0f}/jealous={jealous:.0f}，"
        f"energy={ctx.energy:.0f}，"
        f"余韵={','.join(ctx.afterglow) or '无'}"
    )
    return VoicePlan(text=text, instructions=instructions, scene=scene_key,
                     scene_name=scene.name, speed=speed, guidance=guidance,
                     volume=volume, tags=tags, reason=reason)


def direct_turn(parts: Sequence[str], ctx: VoiceContext,
                *, pause_tag: bool = True) -> VoicePlan:
    """一轮多条 → 一条语音。

    Breeze 一次请求就能念完整轮（上限 1000 字），比「每条一次请求」
    便宜、音色也更连贯。代价是丢掉了说法层的发送间隔 —— 用 `[停顿]`
    标签补回来：分条之间有停顿，本来就该在声音里。
    """
    cleaned = [clean_for_tts(p).strip() for p in parts if p and str(p).strip()]
    if not cleaned:
        return direct(ctx)
    if len(cleaned) == 1:
        ctx.text = cleaned[0]
        return direct(ctx)
    joiner = "。[停顿]" if pause_tag else "。"
    text = joiner.join(p.rstrip("。！？…；;") for p in cleaned) + "。"
    ctx.text = text
    return direct(ctx)


def from_state(*, text: str, mood: Any = 65,
               dims: Optional[Mapping[str, Any]] = None,
               life: Optional[Mapping[str, Any]] = None,
               hour: int = 21, is_group: bool = False,
               is_owner: bool = True, afterglow: Sequence[str] = (),
               neglect_level: int = 0, user_text: str = "",
               requested: bool = False, wake: bool = False,
               proactive: bool = False, scene: str = "",
               profile: str = "") -> VoicePlan:
    """从 bigfish 的 mood.json / life.json 形状直接建上下文。

    `mood` 既可以是数值，也可以是 mood.json 里那一整段
    `{"mood": 72, "dims": {...}}` —— 两种调用都常见。
    """
    if isinstance(mood, Mapping):
        mood_value = float(mood.get("mood") or mood.get("value") or 65)
        dims = dims if dims is not None else (mood.get("dims") or {})
    else:
        mood_value = float(mood)
    life = life or {}
    return direct(VoiceContext(
        text=text,
        user_text=user_text,
        mood=mood_value,
        dims=dict(dims or {}),
        energy=float(life.get("energy") or 70),
        asleep=bool(life.get("asleep")),
        doing=str(life.get("doing") or ""),
        hour=int(hour),
        is_group=is_group,
        is_owner=is_owner,
        afterglow=tuple(afterglow or ()),
        neglect_level=int(neglect_level or 0),
        requested=requested,
        wake=wake,
        proactive=proactive,
        scene=scene,
        profile=profile,
    ))


# ══════════════════════════════════════════════════════════════════════
# 六、自检 / 预览
# ══════════════════════════════════════════════════════════════════════

DEMO = [
    ("深夜舍不得挂", dict(hour=23, mood=72, dims={"sweet": 55, "calm": 40},
                        life={"energy": 30, "doing": "在沙发上看书"})),
    ("刚醒", dict(hour=7, mood=60, dims={"calm": 30},
                life={"energy": 45, "asleep": False, "doing": "刚坐起来"},
                wake=True)),
    ("对方难受", dict(hour=20, mood=58, dims={"heartache": 55, "worry": 40},
                  user_text="今天真的好难受，什么都不想干",
                  life={"energy": 50})),
    ("拌嘴后", dict(hour=21, mood=42, dims={"sad": 35, "jealous": 30},
                  afterglow=("conflict",), life={"energy": 45})),
    ("被夸开心", dict(hour=18, mood=82, dims={"happy": 70, "sweet": 45},
                   user_text="今天升职了！", life={"energy": 65})),
    ("群里", dict(hour=15, mood=70, dims={"happy": 40}, is_group=True,
                is_owner=False, life={"energy": 60})),
    ("被冷落", dict(hour=22, mood=48, dims={"sad": 40}, neglect_level=2,
                 life={"energy": 40})),
    ("对方要语音", dict(hour=21, mood=70, dims={"sweet": 40}, requested=True,
                    life={"energy": 60})),
]


def _demo_text(scene: str) -> str:
    return {
        "late_night": "还不睡吗？我这边灯已经关了。",
        "morning_wake": "早……我还没完全醒。",
        "comfort": "先别急着想办法，我在这儿呢。",
        "conflict": "知道了，我先不说了。",
        "good_news": "真的？那也太好了吧。",
        "group_public": "我也觉得这个可以。",
        "low": "嗯，我听着。",
        "requested": "好，那我说给你听。",
    }.get(scene, "嗯，我在。")


def preview() -> None:
    for label, kw in DEMO:
        probe = from_state(text="嗯。", **kw)
        plan = from_state(text=_demo_text(probe.scene), **kw)
        print(f"\n【{label}】{plan.scene_name}  "
              f"speed={plan.speed} guidance={plan.guidance} "
              f"volume={plan.volume} tags={plan.tags}")
        print("  念：", plan.text)
        print("  令：", plan.instructions)


def selfcheck() -> List[str]:
    bad: List[str] = []
    for label, kw in DEMO:
        probe = from_state(text="嗯。", **kw)
        plan = from_state(text=_demo_text(probe.scene), **kw)
        if not plan.text or not plan.instructions:
            bad.append(f"{label}: 空文本或空指令")
        if len(plan.instructions) > 1000:
            bad.append(f"{label}: 指令超过 1000 字")
        if not (0.5 <= plan.speed <= 2.0):
            bad.append(f"{label}: speed 越界 {plan.speed}")
        if not (1.0 <= plan.guidance <= 10.0):
            bad.append(f"{label}: guidance 越界 {plan.guidance}")
        if len(plan.tags) > 2:
            bad.append(f"{label}: 标签超过 2 个")
    if "叹气" not in direct(VoiceContext(text="今天有点累。", mood=30,
                                        dims={"sad": 60})).text:
        bad.append("低情绪场景没插入 [叹气]")
    long_text = "我知道啦，这就去。" * 6
    p = direct(VoiceContext(text=long_text, mood=80, dims={"happy": 70}))
    if len(re.findall(r"\[[^\]]+\]", p.text)) > 2:
        bad.append(f"长句标签过多：{p.tags}")
    return bad


if __name__ == "__main__":
    preview()
    issues = selfcheck()
    print("\n自检：", "全过" if not issues else "\n".join(issues))
