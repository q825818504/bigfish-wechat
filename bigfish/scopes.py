# -*- coding: utf-8 -*-
"""配置作用域 —— 全局 / 账号 / 会话 三层继承。

## 为什么需要它

2026-11-04 自环事故的第一个根因：`allow_self` 是**全局开关**。

它不是"读错了路径"。是配置层**根本没有"会话级"这个概念** ——
所以 `app.py` 只能把白名单写死在代码里（`SELF_CHAT_IDS = {"filehelper"}`），
于是任何一个人的私聊需要它，就等于所有人的私聊都开着它。

结论：只要配置表达不了"这个会话不一样"，任何安全开关就只能全局，
而全局的安全开关，迟早会被某个会话的用途撑开。

## 三层

    scopes.global                    全局默认
    scopes.account[<bot_wxid>]       按 Bot 账号（多开时用）
    scopes.chat[<peer_id>]           按会话（对方 wxid，或群 id）

回退链：**chat → account → global → 代码默认值**，取第一个"有值"的层。

## 三态（这是本模块存在的一半理由）

布尔字段必须区分三件事：

    缺失 / null  →  继承上一层
    true         →  显式开启
    false        →  显式关闭

`false` 必须能和"没写"分开。否则"我在这个会话里关掉了它"和
"我从来没配过它"在数据上是同一个东西 —— 那就又回到 D-61 那个形状：
**两处都能跑通、都不报错，只有用户会发现"我改的东西没生效"。**

数字与列表没有 `false`，但仍然要区分"缺失"和"显式清空"：

    quiet_hours 缺失  →  继承
    quiet_hours []    →  显式清空（本会话没有免打扰）

⚠ 因此**一律不能用 `d.get(k) or fallback`**。它会把 `[]`、`0`、`False`
一起吞掉，正好把"显式清空"变成"继承" —— 这正是我们要消灭的那类 bug。

## 键：id 为主，昵称只读

历史配置里 `chats` 混着昵称（`"文件传输助手"`）和 wxid（绑定主人时写入）。
会话层的键以 id 为主；昵称只做**只读别名**：

  · 读取时通过 `resolver(昵称) -> id` 把配置里的昵称键解析成 id 再比对；
  · 解析不到（微信没开、库里没这个人）→ 视为**未命中**，不抛异常，
    由 `diagnose()` 报出来、UI 标提示；
  · 写入永远写 id（D-35：保存必须显式，系统不替用户改文件）。

## 与旧配置的关系

旧的 `auto_reply.<field>` 仍然能读（映射到 global 层），但**新路径优先**；
写入只写 `scopes`。旧字段永远不删，保证可回滚。

## 用法

    from bigfish import scopes

    # 取单个值
    on = scopes.get(cfg, "allow_self", chat_id="wxid_abc")

    # 取全部值 + 来源层（UI 要给每个字段标"继承自全局 / 本会话"）
    row = scopes.resolve(cfg, bot_id=bot, chat_id=chat)
    row["allow_self"]          # -> (False, "chat")

    # 排障：哪些键没解析出来、哪些值类型不对
    for code, detail in scopes.diagnose(cfg, chat_id=chat):
        print(code, detail)

    # 写（只改内存 dict，落盘由调用方决定）
    scopes.set_chat_value(cfg, "wxid_abc", "allow_self", False)
    scopes.clear_chat_value(cfg, "wxid_abc", "allow_self")   # 恢复继承

⚠ 本模块**不 import 任何 bigfish 子模块**，也不碰文件与微信。
  它被 app / loopguard / desktop / ui 共同依赖，自己再依赖别人就会循环。
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


# ══════════════════════════════════════════════════════════════════
#  层
# ══════════════════════════════════════════════════════════════════

LAYER_CHAT = "chat"
LAYER_ACCOUNT = "account"
LAYER_GLOBAL = "global"
LAYER_LEGACY = "legacy"
LAYER_DEFAULT = "default"

#: 回退顺序，也是 `explain()` 的显示顺序
LAYER_ORDER = (LAYER_CHAT, LAYER_ACCOUNT, LAYER_GLOBAL, LAYER_LEGACY, LAYER_DEFAULT)

#: 给人看的层名。UI 直接拿这个显示"继承自全局"。
LAYER_LABELS = {
    LAYER_CHAT: "本会话",
    LAYER_ACCOUNT: "继承自账号",
    LAYER_GLOBAL: "继承自全局",
    LAYER_LEGACY: "旧配置的全局开关（仅自测会话）",
    LAYER_DEFAULT: "代码默认值",
}

#: 旧的全局 `allow_self` 只落到这些会话。见 FIELDS["allow_self"] 的说明。
LEGACY_SELF_CHATS = ("filehelper",)


# ══════════════════════════════════════════════════════════════════
#  字段注册表
#
#  加字段只改这张表，不改读取逻辑。
#  `legacy` 是本轮之前它住在 config.json 的哪个位置（读时兼容用）。
# ══════════════════════════════════════════════════════════════════

FIELDS: Dict[str, dict] = {
    "allow_self": {
        "kind": "bool",
        "default": False,
        "label": "允许回应自己发的消息",
        # ⚠ 没有 `legacy`：旧的 `auto_reply.allow_self` 不映射到 global。
        #
        # 那是一个全局开关，而它**本来的意图**只针对文件传输助手（自测通道，
        # 见 loopguard 的 L1 白名单）。如果把它当 global 读，
        # 线上那份 `allow_self: true` 会让**每一个会话**重新拿到 true ——
        # 等于把 11-04 的事故开关重新打开。
        #
        # 所以它按 `legacy_self` 处理：全局 true 只落到 filehelper 这一个会话。
        "legacy_self": True,
        "note": "自测通道。生产会话必须关。",
    },
    "group_reply": {
        "kind": "bool",
        "default": True,
        "label": "群聊参与",
        "legacy": ("auto_reply", "group_reply"),
    },
    "group_allow_all": {
        "kind": "list",
        "default": (),
        "label": "无条件回复的群",
        "legacy": ("auto_reply", "group_allow_all"),
        "placeholder": "群名或 123456789@chatroom，逗号分隔",
    },
    "max_reply_chars": {
        "kind": "int",
        "default": 300,
        "label": "单轮回复字数上限",
        "legacy": ("auto_reply", "max_reply_chars"),
        "placeholder": "例如 300；0 表示不截断",
    },
    "quiet_hours": {
        "kind": "list",
        "default": (),
        "label": "免打扰时段",
        "legacy": ("auto_reply", "quiet_hours"),
        "placeholder": "例如 23:00-07:00 或 23，逗号分隔",
    },
}


# ══════════════════════════════════════════════════════════════════
#  键形态
# ══════════════════════════════════════════════════════════════════

#: 认得出是平台 id 的形态。认不出的一律当昵称，走 resolver。
_ID_RE = re.compile(r"^(?:wxid_[A-Za-z0-9_\-]+|[A-Za-z0-9_\-]+@chatroom)$")


def looks_like_id(key: Any) -> bool:
    """这个键是不是平台 id（而不是昵称）。

    认得出 → True；认不出 → False（调用方会尝试用 resolver 解析）。

    `filehelper` 单独放行：它是微信内置的文件传输助手，不是 wxid 也不是群 id，
    但它是一个**稳定的**平台标识，不随昵称变。
    """
    k = str(key or "").strip()
    if not k:
        return False
    if k == "filehelper":
        return True
    if _ID_RE.match(k):
        return True
    # 纯数字：QQ 号 / 某些平台数字群号
    return k.isdigit()


def key_to_id(key: Any, resolver: Optional[Callable[[str], Optional[str]]] = None) -> Optional[str]:
    """把一个配置键归一成 id。解析不到返回 None。

    解析失败**不抛异常** —— 微信没开的时候整页配置都得能读，
    只是那个键匹配不上而已。
    """
    k = str(key or "").strip()
    if not k:
        return None
    if looks_like_id(k):
        return k
    if resolver is None:
        return None
    try:
        got = resolver(k)
    except Exception:
        return None
    if not got:
        return None
    return str(got).strip() or None


def memo_resolver(resolver: Optional[Callable[[str], Optional[str]]],
                  ttl: float = 30.0, clock: Optional[Callable[[], float]] = None
                  ) -> Optional[Callable[[str], Optional[str]]]:
    """给昵称解析器套一层短期缓存。

    为什么需要：一条消息到达时要问 4~5 个字段，每个字段都会遍历一次
    `scopes.chat`；只要表里有非 id 的键，就会走一次
    `WeChatReader.resolve` —— 那是**查微信数据库**。不缓存的话，
    每条消息要查好几遍 SQLite。

    TTL 默认 30 秒：改名、新增联系人不该等太久；也不用每条消息都重查。
    解析失败（None）**也会缓存** —— 微信没开的时候，不缓存就会变成
    "每条消息都卡在打不开的库上重试一次"，那才是最坏的情况。

    `clock` 可注入，测试用。
    """
    if resolver is None or not callable(resolver):
        return resolver

    tick = clock or time.monotonic
    cache: Dict[str, Tuple[float, Optional[str]]] = {}

    def wrapped(name: str) -> Optional[str]:
        key = str(name or "")
        now = tick()
        hit = cache.get(key)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
        try:
            value = resolver(key)
        except Exception:
            value = None
        if len(cache) > 256:
            cache.clear()
        cache[key] = (now, value)
        return value

    return wrapped


# ══════════════════════════════════════════════════════════════════
#  取值与类型
# ══════════════════════════════════════════════════════════════════

def _coerce_bool(raw: Any) -> Tuple[Optional[bool], str]:
    if isinstance(raw, bool):
        return raw, ""
    # 手改 JSON 时常见的 0/1；接受，但只接受这两个数字
    if isinstance(raw, int) and raw in (0, 1):
        return bool(raw), ""
    return None, "type"


def _coerce_int(raw: Any) -> Tuple[Optional[int], str]:
    if isinstance(raw, bool):          # bool 是 int 的子类，必须先挡掉
        return None, "type"
    if isinstance(raw, int):
        return (raw, "") if raw >= 0 else (None, "range")
    if isinstance(raw, float) and raw.is_integer() and raw >= 0:
        return int(raw), ""
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip()), ""
    return None, "type"


def _coerce_list(raw: Any) -> Tuple[Optional[tuple], str]:
    if isinstance(raw, str):
        # 手改时把列表写成一个逗号串 —— 常见，容忍
        items = [x.strip() for x in raw.split(",") if x.strip()]
        return tuple(items), ""
    if isinstance(raw, (list, tuple)):
        return tuple(str(x).strip() for x in raw if str(x).strip() != ""), ""
    return None, "type"


_COERCE = {
    "bool": _coerce_bool,
    "int": _coerce_int,
    "list": _coerce_list,
}


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


# ══════════════════════════════════════════════════════════════════
#  三层读取
# ══════════════════════════════════════════════════════════════════

def _scopes(config: Any) -> dict:
    return _dict(_dict(config).get("scopes"))


def _chat_block(config: Any, chat_id: Optional[str],
                resolver: Optional[Callable[[str], Optional[str]]]) -> Tuple[dict, List[Tuple[str, str]]]:
    """找到属于 `chat_id` 的会话层配置块。

    返回 (block, problems)。problems 里记"哪个键没解析出来"。

    id 优先：同一个会话既有昵称键又有 id 键时，id 键赢 ——
    昵称键只是历史遗留，不能覆盖用户后来显式选的 id。
    """
    table = _dict(_scopes(config).get("chat"))
    problems: List[Tuple[str, str]] = []
    if not table:
        return {}, problems

    target = str(chat_id or "").strip()
    direct: Optional[dict] = None
    aliased: Optional[dict] = None

    for key, block in table.items():
        # `_comment` / `_note` 这类下划线开头的键是这个项目贯穿始终的注释写法，
        # 不是配置。把它们当"非法的会话块"报出来，会让每个写注释的人都看到
        # 一条假警告 —— 那种"狼来了"的提示比没有提示更糟。
        if str(key).startswith("_"):
            continue
        if not isinstance(block, dict):
            problems.append(("scope_chat_block_invalid", f"scopes.chat[{key}] 不是对象"))
            continue
        mapped = key_to_id(key, resolver)
        if mapped is None:
            problems.append((
                "scope_chat_key_unresolved",
                f"scopes.chat 的键 {key!r} 既不是平台 id，也没解析出 id"
                f"（微信没开时会这样）",
            ))
            continue
        if not target or mapped != target:
            continue
        if looks_like_id(key):
            direct = block
        elif aliased is None:
            aliased = block

    return (direct if direct is not None else (aliased or {})), problems


def _account_block(config: Any, bot_id: Optional[str]) -> dict:
    table = _dict(_scopes(config).get("account"))
    if not table:
        return {}
    target = str(bot_id or "").strip()
    if not target:
        return {}
    return _dict(table.get(target))


def _global_block(config: Any) -> Tuple[dict, List[Tuple[str, str]]]:
    """global 层 = 新路径 `scopes.global` ∪ 旧路径 `auto_reply.*`（新优先）。"""
    cfg = _dict(config)
    new = _dict(_scopes(cfg).get("global"))
    problems: List[Tuple[str, str]] = []

    merged: dict = {}
    for key, spec in FIELDS.items():
        legacy_path = spec.get("legacy")
        if key in new:
            merged[key] = new[key]
        elif legacy_path:
            section, name = legacy_path
            old_section = _dict(cfg.get(section))
            if name in old_section:
                merged[key] = old_section[name]

    # `scopes.global` 里出现本次未登记的键：留着，方便以后加字段先写配置
    for key, value in new.items():
        merged.setdefault(key, value)

    return merged, problems


# ══════════════════════════════════════════════════════════════════
#  回退
# ══════════════════════════════════════════════════════════════════

def _pick(key: str, spec: dict, layers: Iterable[Tuple[str, dict]],
          problems: List[Tuple[str, str]]) -> Tuple[Any, str]:
    """按层序取第一个"有值"的。三态在这里落地。"""
    for layer_name, block in layers:
        if not block or key not in block:
            continue
        raw = block[key]
        if raw is None:
            # 显式 null == 没写 == 继承。这是三态的一半。
            continue
        coerce = _COERCE.get(spec["kind"])
        if coerce is None:
            return raw, layer_name
        value, why = coerce(raw)
        if value is None:
            problems.append((
                f"scope_value_{why}",
                f"{layer_name} 层的 {key} 值 {raw!r} 不能当 {spec['kind']} 用，已忽略",
            ))
            continue
        return value, layer_name
    return spec["default"], LAYER_DEFAULT


def _legacy_self_flag(config: Any) -> bool:
    """旧的全局 `auto_reply.allow_self` 是否为 true。

    这是 11-04 事故的开关。它不再被当成 global 值读 ——
    只在**所有层都没显式写过** `allow_self` 时，对 `LEGACY_SELF_CHATS`
    里的会话生效（见 FIELDS 里 `allow_self` 的注释）。
    """
    cfg = _dict(config)
    old = _dict(cfg.get("auto_reply"))
    raw = old.get("allow_self")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int) and raw in (0, 1):
        return bool(raw)
    return False


def resolve(config: Any, *, bot_id: Optional[str] = None,
            chat_id: Optional[str] = None,
            resolver: Optional[Callable[[str], Optional[str]]] = None
            ) -> Dict[str, Tuple[Any, str]]:
    """算出这个作用域下每个字段的最终值和来源层。

    返回 `{字段名: (值, 层名)}`。层名取 `LAYER_CHAT` / `LAYER_ACCOUNT` /
    `LAYER_GLOBAL` / `LAYER_LEGACY` / `LAYER_DEFAULT` 之一 ——
    UI 靠它标"继承自全局"。

    只读：不修改 `config`，不碰文件。
    """
    chat_block, _ = _chat_block(config, chat_id, resolver)
    account_block = _account_block(config, bot_id)
    global_block, _ = _global_block(config)

    layers = (
        (LAYER_CHAT, chat_block),
        (LAYER_ACCOUNT, account_block),
        (LAYER_GLOBAL, global_block),
    )
    sink: List[Tuple[str, str]] = []
    out: Dict[str, Tuple[Any, str]] = {}
    for key, spec in FIELDS.items():
        value, layer = _pick(key, spec, layers, sink)
        # 旧全局开关的专用回落：只对自测会话生效，且必须"谁都没写过"。
        if (layer is LAYER_DEFAULT and spec.get("legacy_self")
                and str(chat_id or "") in LEGACY_SELF_CHATS
                and _legacy_self_flag(config)):
            value, layer = True, LAYER_LEGACY
        out[key] = (value, layer)
    return out


def get(config: Any, key: str, *, bot_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        resolver: Optional[Callable[[str], Optional[str]]] = None,
        default: Any = None) -> Any:
    """取单个字段的最终值。

    `key` 不在注册表里时返回 `default` —— 不做隐式透传，
    否则拼错字段名会静默读到一个永远为 `None` 的值。
    """
    if key not in FIELDS:
        return default
    row = resolve(config, bot_id=bot_id, chat_id=chat_id, resolver=resolver)
    value, _layer = row[key]
    return value


def layer_of(config: Any, key: str, *, bot_id: Optional[str] = None,
             chat_id: Optional[str] = None,
             resolver: Optional[Callable[[str], Optional[str]]] = None) -> str:
    """这个字段最终由哪一层决定。"""
    if key not in FIELDS:
        return ""
    row = resolve(config, bot_id=bot_id, chat_id=chat_id, resolver=resolver)
    return row[key][1]


# ══════════════════════════════════════════════════════════════════
#  诊断
# ══════════════════════════════════════════════════════════════════

def diagnose(config: Any, *, bot_id: Optional[str] = None,
             chat_id: Optional[str] = None,
             resolver: Optional[Callable[[str], Optional[str]]] = None
             ) -> List[Tuple[str, str]]:
    """列出这层配置里"看着对、其实没生效"的地方。

    返回 `[(原因码, 人话)]`，空列表表示干净。

    存在的意义：`scopes.chat` 里一个解析不出来的昵称键，效果是
    **那条配置完全不生效，而且没有任何报错** —— 这正是 D-61 的形状。
    与其让用户自己发现，不如主动报出来。
    """
    problems: List[Tuple[str, str]] = []

    _chat, chat_problems = _chat_block(config, chat_id, resolver)
    problems.extend(chat_problems)

    account_block = _account_block(config, bot_id)
    table = _dict(_scopes(config).get("account"))
    if table and str(bot_id or "").strip() and not account_block:
        problems.append((
            "scope_account_not_found",
            f"scopes.account 里没有当前账号 {bot_id!r}，账号层不生效",
        ))

    global_block, _ = _global_block(config)
    sink: List[Tuple[str, str]] = []
    layers = ((LAYER_CHAT, _chat), (LAYER_ACCOUNT, account_block), (LAYER_GLOBAL, global_block))
    for key in FIELDS:
        _pick(key, FIELDS[key], layers, sink)
    problems.extend(sink)

    # 旧路径还在用：提醒一句，不自动改
    cfg = _dict(config)
    legacy_hits = []
    if "global" not in _scopes(cfg):
        for key, spec in FIELDS.items():
            path = spec.get("legacy")
            if not path:
                continue
            if path[1] in _dict(cfg.get(path[0])):
                legacy_hits.append(f"{path[0]}.{path[1]}")
    if _legacy_self_flag(cfg):
        legacy_hits.append("auto_reply.allow_self")
    if legacy_hits:
        problems.append((
            "scope_legacy_path",
            "仍在使用旧路径（读得到，但新保存会写到 scopes）：" + "、".join(sorted(legacy_hits)),
        ))

    return problems


def explain(config: Any, *, bot_id: Optional[str] = None,
            chat_id: Optional[str] = None,
            resolver: Optional[Callable[[str], Optional[str]]] = None) -> str:
    """人读的逐字段来源表。UI 的弹层、排障命令都用这一份。"""
    row = resolve(config, bot_id=bot_id, chat_id=chat_id, resolver=resolver)
    lines = [f"作用域：账号={bot_id or '—'}  会话={chat_id or '—'}"]
    for key, spec in FIELDS.items():
        value, layer = row[key]
        label = LAYER_LABELS.get(layer, layer)
        lines.append(f"  {key:<18} = {value!r:<24} ← {label}（{spec['label']}）")
    problems = diagnose(config, bot_id=bot_id, chat_id=chat_id, resolver=resolver)
    if problems:
        lines.append("")
        lines.append("注意：")
        for code, detail in problems:
            lines.append(f"  ! [{code}] {detail}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
#  写入（只改内存 dict，落盘由调用方决定 —— D-35：保存必须显式）
# ══════════════════════════════════════════════════════════════════

def _ensure_dict(parent: dict, key: str) -> dict:
    value = parent.get(key)
    if not isinstance(value, dict):
        value = {}
        parent[key] = value
    return value


def set_chat_value(config: dict, chat_id: str, key: str, value: Any) -> None:
    """把 `key` 写到会话层。`chat_id` 必须是平台 id。

    传 `None` 等同 `clear_chat_value` —— 三态里的"继承"只有一个写法，
    免得 UI 和调用方各写一套。
    """
    if key not in FIELDS:
        raise KeyError(f"未注册的字段：{key}")
    cid = str(chat_id or "").strip()
    if not cid:
        raise ValueError("chat_id 不能为空")
    if not looks_like_id(cid):
        raise ValueError(
            f"{cid!r} 看起来是昵称，不是平台 id —— 会话层的键必须是 id，"
            f"否则微信改名后这条配置就失效了"
        )
    if value is None:
        clear_chat_value(config, cid, key)
        return
    table = _ensure_dict(_ensure_dict(config, "scopes"), "chat")
    _ensure_dict(table, cid)[key] = value


def clear_chat_value(config: dict, chat_id: str, key: str) -> bool:
    """删掉会话层的这一个键 —— 效果是"恢复继承"。

    返回是否真的删了。整个会话块空了就一起删掉，别在配置里留空壳。
    """
    cid = str(chat_id or "").strip()
    table = _dict(_dict(_dict(config).get("scopes")).get("chat"))
    block = table.get(cid)
    if not isinstance(block, dict) or key not in block:
        return False
    block.pop(key, None)
    if not block:
        table.pop(cid, None)
    return True


def set_account_value(config: dict, bot_id: str, key: str, value: Any) -> None:
    if key not in FIELDS:
        raise KeyError(f"未注册的字段：{key}")
    bid = str(bot_id or "").strip()
    if not bid:
        raise ValueError("bot_id 不能为空")
    if value is None:
        table = _dict(_dict(_dict(config).get("scopes")).get("account"))
        block = table.get(bid)
        if isinstance(block, dict):
            block.pop(key, None)
            if not block:
                table.pop(bid, None)
        return
    table = _ensure_dict(_ensure_dict(config, "scopes"), "account")
    _ensure_dict(table, bid)[key] = value


def set_global_value(config: dict, key: str, value: Any) -> None:
    """写全局层。

    注意：**不会顺手删掉旧的 `auto_reply.<key>`**。
    旧字段留着可回滚，读取时新路径优先，所以不影响结果。
    """
    if key not in FIELDS:
        raise KeyError(f"未注册的字段：{key}")
    _ensure_dict(_ensure_dict(config, "scopes"), "global")[key] = value
