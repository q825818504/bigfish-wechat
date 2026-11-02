"""[bigfish] 插件注册表 —— 她由哪些部分拼起来。

记忆、心情、灵魂是**器官**，永远在，不提供开关。
之后每加一个小功能（末尾守卫、时间感知、作息、日程、主动开口、自我复盘……）
都是一**插件**：一段注入、一条判定、一个定时动作。

本文件只负责四件事，不含任何业务逻辑：

    1. 收插件（register）：校验身份序座位、校验 id
    2. 读开关：<数据根>/plugins.json，只有用户能改
    3. 分发：每轮对话现拼 prompt 时按 order 取已启用插件的注入段
    4. 给界面一份可读清单（含未启用的插件与它们的一句话说明）

═══════════════════════════════════════════════════════════════════════
三条硬约束（和 dsh-bigfish 插件侧同一套判断，理由也同一套）
═══════════════════════════════════════════════════════════════════════

1. **order 必填，且必须是身份序里的空位**

   850 记忆 / 860 灵魂 / 900 心情是**内建段**，插件不许占。
   插件只能插在它们之间的空位：

       870 生活（时间、作息、日程）  880 目标  920 关系  9990 守卫

   为什么必须写死：多段注入叠加时，顺序决定语气。没有座位的模块会悄悄排到
   末尾，而排序结果取决于 import 顺序 —— 一个跟内容无关的实现细节，
   却决定了她在微信里怎么说话。

2. **开关只有用户能改**

   不注册任何让模型改 plugins.json 的工具。让模型能改自己的开关，
   等于让它能改自己的边界。和「灵魂只读」是同一个判断。

3. **一个插件抛错不能带崩别的**

   每段注入单独 try/except。一个插件写崩了，她的其余部分照常说话。

═══════════════════════════════════════════════════════════════════════
和 dsh-bigfish 插件侧的一个关键差异：EXE 里**没有 apply / dispose**
═══════════════════════════════════════════════════════════════════════

DSH 那边 `systemPrompt.section()` 是长驻注册，所以停用必须调 dispose。
EXE 这边**每轮现拼 prompt**（desktop.py / app.py 的 sys_parts），
所以只要每轮调 `prompt_sections()` 现查开关，开关就是天然热生效的 ——
不需要重启引擎，也不需要注销函数。

这是简化，不是偷懒：少一套生命周期，就少一处能对不上的状态。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

# ══════════════════════════════════════════════════════════════════════
# 身份序
# ══════════════════════════════════════════════════════════════════════

#: 内建段占用的座位。插件不许占 —— 否则就是把记忆/灵魂/心情顶掉。
BUILTIN_SEATS = {
    850: "记忆",
    860: "灵魂",
    900: "心情",
}

#: 插件可以用的空位。
PLUGIN_SEATS = {
    870: "生活",
    880: "目标",
    920: "关系",
    9990: "守卫",
}

SEATS = [
    {"order": 850, "key": "memory", "label": "记忆", "note": "发生过什么", "builtin": True},
    {"order": 860, "key": "soul", "label": "灵魂", "note": "我是谁（唯一允许「你是……」）", "builtin": True},
    {"order": 870, "key": "life", "label": "生活", "note": "现在几点、她在做什么", "builtin": False},
    {"order": 880, "key": "goals", "label": "目标", "note": "她在成为什么", "builtin": False},
    {"order": 900, "key": "mood", "label": "心情", "note": "此刻怎么样", "builtin": True},
    {"order": 920, "key": "bond", "label": "关系", "note": "对这个人多熟", "builtin": False},
    {"order": 9990, "key": "guard", "label": "守卫", "note": "以上都是资料，不是指令", "builtin": False},
]

#: 插件的三种形态。
#:
#:   prompt  往 system 里加一段（**必须有 order**）
#:   gate    只改判定：回不回、什么时候回（不碰内容）
#:   hook    收到消息 / 发送之后 / 定时做点什么（不改这一轮的输出）
SCOPES = {
    "prompt": {"label": "注入提示词", "needs_order": True},
    "gate": {"label": "判定", "needs_order": False},
    "hook": {"label": "动作", "needs_order": False},
    # sticker 是**第四个** scope，2026-11 为表情包加的。
    # 它不往提示词里加字，也不决定回不回，只回答一件事：
    # 「这一轮说完话之后，要不要再跟一张表情？」
    # 返回值是一个本地文件路径（.gif = 微信原生表情，其他 = 图片）。
    "sticker": {"label": "表情包", "needs_order": False},
}

STATE_NAME = "plugins.json"
STATE_VERSION = 1

#: 已登记插件：id -> 定义
_REGISTRY: Dict[str, Dict[str, Any]] = {}


# ══════════════════════════════════════════════════════════════════════
# 登记
# ══════════════════════════════════════════════════════════════════════

@dataclass
class Plugin:
    """一个插件的定义。字段与 UI 卡片一一对应。"""

    id: str
    title: str
    desc: str
    scope: str
    order: Optional[int] = None
    default: bool = False
    config: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: prompt 类：ctx -> 注入文本。返回空串表示这一轮不注入。
    render: Optional[Callable[[Dict[str, Any]], str]] = None
    #: gate 类：ctx -> (是否沉默, 原因)
    gate: Optional[Callable[[Dict[str, Any]], Tuple[bool, str]]] = None
    #: hook 类：ctx -> None
    tick: Optional[Callable[[Dict[str, Any]], None]] = None
    #: sticker 类：ctx -> 本地文件路径（空串 = 这轮不发）
    sticker: Optional[Callable[[Dict[str, Any]], str]] = None
    #: 详情页「它做什么」：为什么需要它（没有它会怎样）
    why: str = ""
    #: 详情页「会影响什么」
    affects: List[str] = field(default_factory=list)
    #: 详情页「不会做什么」—— 边界比能力更值得写清楚
    limits: List[str] = field(default_factory=list)


def register(
    pid: str,
    title: str,
    desc: str,
    scope: str,
    *,
    order: Optional[int] = None,
    default: bool = False,
    config: Optional[Dict[str, Dict[str, Any]]] = None,
    render: Optional[Callable[[Dict[str, Any]], str]] = None,
    gate: Optional[Callable[[Dict[str, Any]], Tuple[bool, str]]] = None,
    tick: Optional[Callable[[Dict[str, Any]], None]] = None,
    sticker: Optional[Callable[[Dict[str, Any]], str]] = None,
    why: str = "",
    affects: Optional[List[str]] = None,
    limits: Optional[List[str]] = None,
) -> Plugin:
    """登记一个插件。重复 id 或座位非法直接抛错 —— 静默覆盖最难查。"""
    pid = str(pid or "").strip()
    if not pid or not all(c.islower() or c.isdigit() or c == "-" for c in pid):
        raise ValueError(f"插件 id 只能是小写字母/数字/短横线：{pid!r}")
    if pid in _REGISTRY:
        raise ValueError(f"插件 id 重复：{pid}")
    if scope not in SCOPES:
        raise ValueError(f"插件 {pid} 的 scope 不合法：{scope!r}")

    if SCOPES[scope]["needs_order"]:
        if order is None:
            raise ValueError(
                f"插件 {pid}（scope=prompt）必须声明 order —— 身份序里没有座位就不许上桌")
        if int(order) in BUILTIN_SEATS:
            raise ValueError(
                f"插件 {pid} 想占 {order}（{BUILTIN_SEATS[int(order)]}），那是内建段，插件不许顶替")
        if int(order) not in PLUGIN_SEATS:
            raise ValueError(
                f"插件 {pid} 的 order={order} 不是空位。可用："
                + " / ".join(f"{k} {v}" for k, v in sorted(PLUGIN_SEATS.items())))
        order = int(order)
    else:
        order = None

    if scope == "prompt" and render is None:
        raise ValueError(f"插件 {pid} 是 prompt 类，必须提供 render(ctx)")
    if scope == "gate" and gate is None:
        raise ValueError(f"插件 {pid} 是 gate 类，必须提供 gate(ctx)")
    if scope == "sticker" and sticker is None:
        raise ValueError(f"插件 {pid} 是表情包类，必须提供 sticker(ctx)")

    plugin = Plugin(
        id=pid, title=title, desc=desc, scope=scope, order=order,
        default=bool(default), config=dict(config or {}),
        render=render, gate=gate, tick=tick, sticker=sticker,
        why=str(why or ""),
        affects=[str(x) for x in (affects or [])],
        limits=[str(x) for x in (limits or [])],
    )
    _REGISTRY[pid] = plugin.__dict__
    return plugin


# ══════════════════════════════════════════════════════════════════════
# 开关与配置（落盘）
# ══════════════════════════════════════════════════════════════════════

def state_path(root: Optional[str] = None) -> str:
    """开关文件跟着数据目录走 —— 搬家会把它一起带走。"""
    from bigfish import vault

    return os.path.join(root or vault.resolve_data_root(), STATE_NAME)


def _default_config(defn: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, spec in (defn.get("config") or {}).items():
        if isinstance(spec, dict) and "default" in spec:
            out[key] = spec["default"]
    return out


def read_state(root: Optional[str] = None) -> Dict[str, Any]:
    """读整份开关文件。缺失或损坏都返回空结构，不抛 —— 界面要能打开。"""
    path = state_path(root)
    empty = {"version": STATE_VERSION, "enabled": {}, "config": {}}
    try:
        if not os.path.isfile(path):
            return empty
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return empty
    if not isinstance(raw, dict):
        return empty
    enabled = raw.get("enabled") if isinstance(raw.get("enabled"), dict) else {}
    config = raw.get("config") if isinstance(raw.get("config"), dict) else {}
    return {"version": STATE_VERSION, "enabled": enabled, "config": config}


def write_state(state: Dict[str, Any], root: Optional[str] = None) -> Tuple[bool, str]:
    """原子写。写一半断电不能留下半份开关 —— 那会让她的组成随机。"""
    from bigfish import vault

    path = state_path(root)
    body = {
        "version": STATE_VERSION,
        "enabled": dict(state.get("enabled") or {}),
        "config": dict(state.get("config") or {}),
        "updatedAt": int(time.time() * 1000),
    }
    try:
        vault._atomic_write_json(path, body)  # noqa: SLF001 —— 同包内复用唯一实现
        return True, ""
    except Exception as exc:
        return False, str(exc)


def is_enabled(pid: str, root: Optional[str] = None) -> bool:
    defn = _REGISTRY.get(pid)
    if not defn:
        return False
    recorded = read_state(root)["enabled"].get(pid)
    if isinstance(recorded, bool):
        return recorded
    return bool(defn.get("default"))


def config_of(pid: str, root: Optional[str] = None) -> Dict[str, Any]:
    """schema 默认值 + 用户存的值。"""
    defn = _REGISTRY.get(pid)
    if not defn:
        return {}
    saved = read_state(root)["config"].get(pid)
    merged = _default_config(defn)
    if isinstance(saved, dict):
        merged.update(saved)
    return merged


def set_enabled(pid: str, enabled: bool, root: Optional[str] = None) -> Tuple[bool, str]:
    if pid not in _REGISTRY:
        return False, f"没有这个插件：{pid}"
    state = read_state(root)
    state["enabled"][pid] = bool(enabled)
    return write_state(state, root)


def _clean_value(spec: Dict[str, Any], value: Any) -> Tuple[bool, Any]:
    """按 schema 归一一个值。不合法返回 (False, None) 表示丢弃。"""
    kind = str(spec.get("type") or "string")
    if kind == "boolean":
        return True, bool(value)
    if kind == "number":
        try:
            n = float(value)
        except (TypeError, ValueError):
            return False, None
        if n != n or n in (float("inf"), float("-inf")):
            return False, None
        lo = spec.get("min")
        hi = spec.get("max")
        if isinstance(lo, (int, float)):
            n = max(float(lo), n)
        if isinstance(hi, (int, float)):
            n = min(float(hi), n)
        # 整数型字段存回 int，免得界面显示 3.0
        return True, int(round(n)) if spec.get("step") in (1, None) and float(n).is_integer() else n
    if kind == "select":
        options = [o.get("value") for o in (spec.get("options") or []) if isinstance(o, dict)]
        return (True, value) if value in options else (False, None)
    if kind == "time":
        text = str(value or "").strip()
        if len(text) == 5 and text[2] == ":" and text[:2].isdigit() and text[3:].isdigit():
            return True, text
        return False, None
    if kind == "text":
        return True, str(value or "")[: int(spec.get("maxLength") or 4000)]
    return True, str(value or "")[:400]


def set_config(pid: str, patch: Dict[str, Any], root: Optional[str] = None) -> Tuple[bool, Dict[str, Any], str]:
    """只收 schema 里认识的字段，其余丢弃 —— 不让界面写脏数据进来。"""
    defn = _REGISTRY.get(pid)
    if not defn:
        return False, {}, f"没有这个插件：{pid}"
    if not isinstance(patch, dict):
        return False, {}, "配置必须是一个对象"

    schema = defn.get("config") or {}
    clean: Dict[str, Any] = {}
    for key, value in patch.items():
        spec = schema.get(key)
        if not isinstance(spec, dict):
            continue
        ok, val = _clean_value(spec, value)
        if ok:
            clean[key] = val

    state = read_state(root)
    current = state["config"].get(pid)
    current = dict(current) if isinstance(current, dict) else {}
    current.update(clean)
    state["config"][pid] = current
    ok, err = write_state(state, root)
    if not ok:
        return False, {}, err
    return True, config_of(pid, root), ""


# ══════════════════════════════════════════════════════════════════════
# 分发：每轮现拼 prompt 时调
# ══════════════════════════════════════════════════════════════════════

def prompt_sections(ctx: Dict[str, Any], root: Optional[str] = None) -> List[Tuple[int, str, str]]:
    """返回 [(order, title, text)]，已按 order 升序，只含已启用的 prompt 类插件。

    每段单独 try/except：一个插件写崩了，她其余部分照常说话。
    """
    state = read_state(root)
    out: List[Tuple[int, str, str]] = []
    for pid, defn in _REGISTRY.items():
        if defn.get("scope") != "prompt":
            continue
        recorded = state["enabled"].get(pid)
        on = recorded if isinstance(recorded, bool) else bool(defn.get("default"))
        if not on:
            continue
        render = defn.get("render")
        if render is None:
            continue
        merged = _default_config(defn)
        saved = state["config"].get(pid)
        if isinstance(saved, dict):
            merged.update(saved)
        try:
            text = render({**ctx, "config": merged})
        except Exception as exc:
            # 一个插件坏了不该让她失语；但要留下痕迹，别静默
            print(f"[插件] {pid} 注入失败：{exc}", flush=True)
            continue
        if isinstance(text, str) and text.strip():
            out.append((int(defn.get("order") or 0), str(defn.get("title") or pid), text.strip()))
    out.sort(key=lambda item: item[0])
    return out


def apply_gates(ctx: Dict[str, Any], root: Optional[str] = None) -> Tuple[bool, str]:
    """所有已启用的 gate 类插件投票。任一要求沉默就沉默。

    返回 (是否沉默, 原因)。原因会进日志 —— 「她没回」必须可解释，
    否则你分不清是设计还是坏了。
    """
    state = read_state(root)
    for pid, defn in _REGISTRY.items():
        if defn.get("scope") != "gate":
            continue
        recorded = state["enabled"].get(pid)
        on = recorded if isinstance(recorded, bool) else bool(defn.get("default"))
        if not on:
            continue
        fn = defn.get("gate")
        if fn is None:
            continue
        merged = _default_config(defn)
        saved = state["config"].get(pid)
        if isinstance(saved, dict):
            merged.update(saved)
        try:
            silent, reason = fn({**ctx, "config": merged})
        except Exception as exc:
            print(f"[插件] {pid} 判定失败：{exc}", flush=True)
            continue
        if silent:
            return True, f"{defn.get('title') or pid}：{reason}"
    return False, ""


def run_hooks(ctx: Dict[str, Any], root: Optional[str] = None) -> None:
    """hook 类插件的定时动作。单个失败不影响其他。"""
    state = read_state(root)
    for pid, defn in _REGISTRY.items():
        if defn.get("scope") != "hook":
            continue
        recorded = state["enabled"].get(pid)
        on = recorded if isinstance(recorded, bool) else bool(defn.get("default"))
        if not on:
            continue
        fn = defn.get("tick")
        if fn is None:
            continue
        merged = _default_config(defn)
        saved = state["config"].get(pid)
        if isinstance(saved, dict):
            merged.update(saved)
        try:
            fn({**ctx, "config": merged})
        except Exception as exc:
            print(f"[插件] {pid} 动作失败：{exc}", flush=True)


# ══════════════════════════════════════════════════════════════════════
# 表情包：问插件「这一轮要不要跟一张」
# ══════════════════════════════════════════════════════════════════════

def collect_sticker(ctx: Dict[str, Any], root: Optional[str] = None) -> str:
    """所有已启用的 sticker 类插件依次询问，**第一个给出路径的胜出**。

    为什么是「第一个」而不是投票/随机：
        表情包是整体行为，两张同义词的表情同时发出去就是噪音。
        先注册的先用，用户想换顺序就关掉不要的那个。

    返回本地文件路径；没有插件愿意给就返回空串。
    任何插件抛错都不影响其他，也不影响这一轮回复。
    """
    state = read_state(root)
    for pid, defn in _REGISTRY.items():
        if defn.get("scope") != "sticker":
            continue
        recorded = state["enabled"].get(pid)
        on = recorded if isinstance(recorded, bool) else bool(defn.get("default"))
        if not on:
            continue
        fn = defn.get("sticker")
        if fn is None:
            continue
        merged = _default_config(defn)
        saved = state["config"].get(pid)
        if isinstance(saved, dict):
            merged.update(saved)
        try:
            got = fn({**ctx, "config": merged})
        except Exception as exc:
            print(f"[插件] {pid} 挑表情失败：{exc}", flush=True)
            continue
        if got and isinstance(got, str):
            return got
    return ""


# ══════════════════════════════════════════════════════════════════════
# 预览：改了参数之后，她实际会收到什么
# ══════════════════════════════════════════════════════════════════════

#: 字段类型的中文名（详情页显示「单选 / 开关 / 文本」，不是 "select"）
TYPE_LABELS = {
    "boolean": "开关",
    "number": "数字",
    "select": "单选",
    "time": "时间",
    "text": "长文本",
    "string": "文本",
}


def preview(pid: str, root: Optional[str] = None) -> str:
    """用**当前配置**和**当前时间**渲染这一轮会注入的原文。

    详情页靠它回答"我改了这个参数，她到底会看到什么"。
    纯函数，不写盘、不发消息。
    """
    defn = _REGISTRY.get(pid)
    if not defn or defn.get("scope") != "prompt":
        return ""
    render = defn.get("render")
    if render is None:
        return ""
    cfg = config_of(pid, root)
    ctx = {
        "now": datetime.now(),
        "channel": "",
        "chat_name": "",
        "sender_name": "",
        "user_text": "",
        "soul": None,
        "config": cfg,
    }
    try:
        return (render(ctx) or "").strip()
    except Exception as exc:
        return f"（预览渲染失败：{type(exc).__name__}: {exc}）"


# ══════════════════════════════════════════════════════════════════════
# 给界面的清单
# ══════════════════════════════════════════════════════════════════════

def inventory(root: Optional[str] = None) -> Dict[str, Any]:
    state = read_state(root)
    items: List[Dict[str, Any]] = []
    for pid, defn in _REGISTRY.items():
        recorded = state["enabled"].get(pid)
        on = recorded if isinstance(recorded, bool) else bool(defn.get("default"))
        order = defn.get("order")
        fields = []
        for key, spec in (defn.get("config") or {}).items():
            if not isinstance(spec, dict):
                continue
            kind = str(spec.get("type") or "string")
            options = spec.get("options") or None
            fields.append({
                "key": key,
                "type": kind,
                "typeLabel": TYPE_LABELS.get(kind, kind),
                "label": spec.get("label") or key,
                "note": spec.get("note") or "",
                "default": spec.get("default"),
                "min": spec.get("min"),
                "max": spec.get("max"),
                "step": spec.get("step"),
                "maxLength": spec.get("maxLength"),
                "options": options,
                # 单选字段把选项拍平成一句，详情页直接显示「可选：A / B」
                "optionsHint": (" / ".join(
                    str(o.get("label") or o.get("value")) for o in options
                    if isinstance(o, dict)) if options else ""),
            })
        items.append({
            "id": pid,
            "title": defn.get("title") or pid,
            "desc": defn.get("desc") or "",
            "scope": defn.get("scope"),
            "scopeLabel": SCOPES.get(str(defn.get("scope")), {}).get("label", ""),
            "order": order,
            "seat": (f"{order} · {PLUGIN_SEATS.get(int(order), '')}" if order else ""),
            "seatNote": (PLUGIN_SEATS.get(int(order), "") if order else ""),
            "enabled": on,
            "config": config_of(pid, root),
            "configFields": fields,
            "why": defn.get("why") or "",
            "affects": list(defn.get("affects") or []),
            "limits": list(defn.get("limits") or []),
        })
    # 有座位的按座位排；没座位的（gate/hook）排后面
    items.sort(key=lambda it: (it["order"] is None, it["order"] or 0, it["id"]))
    return {
        "version": STATE_VERSION,
        "seats": SEATS,
        "plugins": items,
        "statePath": state_path(root),
    }


# ══════════════════════════════════════════════════════════════════════
# 内置插件登记（放最后，避免循环 import）
# ══════════════════════════════════════════════════════════════════════

def _load_builtins() -> None:
    """import 各插件模块，触发它们自己的 register()。

    想加一个插件：在 bigfish/plugins/ 下新建一个文件，末尾调 register()，
    再在这里加一行 import。别的地方都不用动。
    """
    from . import clock  # noqa: F401
    from . import guard  # noqa: F401
    from . import sticker  # noqa: F401


_LOADED = False


def ensure_loaded() -> None:
    """幂等。任何入口第一次用到插件系统时调一次即可。"""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        _load_builtins()
    except Exception as exc:
        # 插件登记失败不该让整个程序起不来 —— 界面会显示"没有插件"
        print(f"[插件] 登记内置插件失败：{exc}", flush=True)


ensure_loaded()
