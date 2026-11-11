"""[bigfish] 她生活里的别人。

═══════════════════════════════════════════════════════════════════════
为什么要这一层
═══════════════════════════════════════════════════════════════════════

在那之前，她的世界里**制度上只允许存在一个人**：

    narrative.py「不要写对第三方的评价（同事、家人），你只知道你们俩之间的事」
    plan.py「不要编造材料里没有的重要事件（谁生病、**谁来找她**、去哪座城市）」
    journal.py「绝对不许编对话和对方的行为」

于是她的日程是买菜、看书、散步、发呆 —— 全是独处；她的日记里
只有"他"；她能碰见的所有事都得先在对话里发生过。

真人的生活里有一半是**不认识她的人**：卖鱼的、楼下的老太太、
值夜班的保安。这些人不服务任何目的，不推动任何剧情，只是**在场**。

═══════════════════════════════════════════════════════════════════════
三条克制
═══════════════════════════════════════════════════════════════════════

1. **遭遇没有剧情。**
   "卖鱼的今天不在，摊子上盖着蓝布" —— 这样就好。
   不要写成"卖鱼的说他女儿考上大学了"。那是有用的信息，会被拿去
   跟你聊；一旦它有用，它就变成了道具，不再是她的生活。
   生活的大部分是没有信息量的。

2. **人是固定的，状态是变的。**
   只有这五个人，永远不会有第六个（模型不许新增）。但"今天在不在"
   "多塞了一条鱼"是变的 —— 变的不是人，是日子。

3. **不进提示词的不写。**
   这一层产出的东西只有一个去处：挂到当天的日程条目上（`meet` 字段），
   跟着 `plan_for_prompt` 和日记素材走。没有独立的注入面，
   所以不会挤掉记忆和对话。

═══════════════════════════════════════════════════════════════════════
存哪
═══════════════════════════════════════════════════════════════════════

`<数据根>/neighbors.json`。只记 `count` 和 `last`（见过几次、上次什么时候），
用来实现"好久没见"和"这个人跟以前不一样了"。

不记对话内容 —— 那是记忆库的活。这里只管"谁在这个世界里出现过"。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

STATE_NAME = "neighbors.json"
STATE_VERSION = 1

#: 她生活里的固定几个人。**只能有这些，模型不许新增。**
#:
#: 挑这五个的理由：覆盖她的活动范围（买菜、下楼、取快递、吃饭、回家），
#: 而且每一个都是"只打个照面"的关系 —— 没有一个是朋友。
#: 朋友会占掉她的注意力，也会变成倾诉对象；熟人不会，熟人只是点头。
CAST = (
    {
        "id": "fish",
        "name": "卖鱼的",
        "spot": "菜市场",
        "quirk": "话极少，称完总往袋子里多塞一条小的",
        "states": (
            "今天在，还是不爱说话",
            "摊子空着，蓝布盖着",
            "多塞了一条小鲫鱼，也没说要钱",
            "在跟人吵价钱，嗓门比平时大",
        ),
        "marks": ("买菜", "菜市场", "买点菜", "超市", "买个菜"),
    },
    {
        "id": "catlady",
        "name": "楼下喂猫的老太太",
        "spot": "单元门口",
        "quirk": "认得每一只猫，从来不记得人的名字",
        "states": (
            "还坐在花坛边上，那只三花没来",
            "今天带了新的猫粮，袋子是拆过的",
            "不在，猫在等",
            "在跟人抱怨谁把车停到花坛里了",
        ),
        "marks": ("散步", "下楼", "走一圈", "遛"),
    },
    {
        "id": "guard",
        "name": "小区保安",
        "spot": "门口",
        "quirk": "值夜班的时候一直看手机",
        "states": (
            "换了个人，年轻的，一直在看手机",
            "还是那个，点了下头",
            "在登记外来车辆，没抬头",
        ),
        "marks": ("散步", "下楼", "走一圈", "回来"),
    },
    {
        "id": "noodle",
        "name": "面馆老板",
        "spot": "楼下",
        "quirk": "记得她不吃香菜",
        "states": (
            "在门口择菜，问了一句今天怎么这么晚",
            "店里没客人，电视开着没人看",
            "卷帘门拉下来一半，在收拾",
        ),
        "marks": ("吃", "外卖", "面", "下楼"),
    },
    {
        "id": "courier",
        "name": "送快递的小哥",
        "spot": "快递柜",
        "quirk": "总是跑着来，说话的时候还在喘",
        "states": (
            "把件放进柜子就走了，没等人",
            "在打电话，说今天件太多",
            "换了个人，穿着别的颜色的马甲",
        ),
        "marks": ("取快递", "快递"),
    },
)

#: 碰见的概率。一天最多两次。
MEET_PROBABILITY = 0.5
MEET_MAX_PER_DAY = 2

#: 多久没见，这个人就算"不在了"。
#:
#: 只涨不跌的不是关系，是账本。一个永远在场的邻居不是人，是背景板 ——
#: 她的世界得有"后来就没见着了"这种事，而且不需要解释为什么。
GONE_AFTER_DAYS = 14

#: 不在了之后，他被提起的方式。不带戏剧性 —— 生活里的"不见了"
#: 一般没有下文，就是没了。
GONE_STATES = (
    "换了个人，说是他回老家了",
    "位子空着，东西都收走了",
    "一直没见着，问也没人知道",
)

_BY_ID = {x["id"]: x for x in CAST}


def state_path(root: Optional[str] = None) -> str:
    from bigfish.life import state_path as _life_path

    return os.path.join(os.path.dirname(_life_path(root)), STATE_NAME)


def _seed(*parts: Any):
    from bigfish.life import _seed as _life_seed

    return _life_seed(*parts)


def load(root: Optional[str] = None) -> Dict[str, Any]:
    """读状态。文件缺失/坏了 → 一份空的，不抛。"""
    path = state_path(root)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {"version": STATE_VERSION, "seen": {}, "day": "", "met": 0}
    if not isinstance(raw, dict):
        return {"version": STATE_VERSION, "seen": {}, "day": "", "met": 0}
    seen = raw.get("seen")
    if not isinstance(seen, dict):
        seen = {}
    return {"version": STATE_VERSION, "seen": seen,
            "day": str(raw.get("day") or ""),
            "met": int(raw.get("met") or 0) if str(raw.get("met") or "").isdigit()
            else 0}


def save(state: Dict[str, Any], root: Optional[str] = None) -> bool:
    try:
        from bigfish.vault import _atomic_write_json

        _atomic_write_json(state_path(root), state)
        return True
    except Exception:
        return False


def note_seen(state: Dict[str, Any], npc_id: str, *, at: float,
              text: str) -> None:
    """记一次"见着了"。

    ⚠ **同一天只记一次**。以前无条件 `count += 1`，于是同一天 `attach`
      被调用两次（重启后重排日程就会）会：重复计数、重复写盘，
      而且 `_state_for` 的档位是 `count // 3` —— 计数一涨，同一天的
      "卖鱼的"从"摊子空着"变成"多塞了一条小鲫鱼"，日程、日记、复盘
      三处说法互相打架。
    """
    row = state.setdefault("seen", {}).setdefault(npc_id, {})
    if not isinstance(row, dict):
        # 手改坏的数据：直接覆盖成干净的，别让它永久失效
        row = {}
        state["seen"][npc_id] = row
    try:
        last = float(row.get("last") or 0)
    except (TypeError, ValueError):
        last = 0.0
    if last and same_day(last, at):
        return
    try:
        row["count"] = int(row.get("count") or 0) + 1
    except (TypeError, ValueError):
        row["count"] = 1
    row["last"] = float(at)
    row["last_text"] = str(text or "")[:60]


def same_day(a: float, b: float) -> bool:
    """两个 epoch 秒是不是同一天（本地时区）。"""
    try:
        import datetime as _dt

        return (_dt.datetime.fromtimestamp(float(a)).date()
                == _dt.datetime.fromtimestamp(float(b)).date())
    except Exception:
        return False


def _state_for(npc: Dict[str, Any], day: str, seen: Dict[str, Any],
               now: float = 0.0) -> str:
    """这个配角"今天怎么样"。同一个 day 永远同一个答案。

    超过 GONE_AFTER_DAYS 没见过的，改用「不在了」那一组写法 ——
    她的世界里得有关系消失这件事，而且不需要给理由。
    """
    row = seen.get(npc["id"]) or {}
    last = 0.0
    try:
        last = float(row.get("last") or 0)
    except Exception:
        last = 0.0
    if now and last and (now - last) > GONE_AFTER_DAYS * 86400:
        state_list = list(GONE_STATES)
    else:
        state_list = list(npc.get("states") or ())
    if not state_list:
        return ""
    n = 0
    try:
        n = int(row.get("count") or 0)
    except Exception:
        n = 0
    rng = _seed(day, npc["id"], "state", n // 3)   # 每见三次换一档
    return rng.choice(state_list)


def _match(npc: Dict[str, Any], what: str) -> bool:
    text = str(what or "")
    return any(m in text for m in (npc.get("marks") or ()))


def attach(items: List[Dict[str, Any]], *, day: str, root: Optional[str] = None,
           now: Optional[float] = None) -> List[Dict[str, Any]]:
    """给当天的日程条目挂「碰见了谁」。

    返回新的 list（不改入参）。命中的条目会多一个 `meet` 字段：
        {"who": "卖鱼的", "spot": "菜市场", "text": "多塞了一条小鲫鱼"}

    为什么挂在日程上而不是单独注入：日程已经是"她今天有什么"的唯一来源，
    遭遇跟着它走，就不会出现"日程里在买菜、却在聊菜市场碰见谁"的断裂。
    """
    if not items:
        return items
    at = float(now if now is not None else time.time())
    try:
        state = load(root)
    except Exception:
        state = {"seen": {}, "day": "", "met": 0}
    seen = state.get("seen") or {}
    if not isinstance(seen, dict):
        seen = {}
        state["seen"] = seen

    # 「一天最多两次」必须跨调用。以前 `met` 是局部变量，只管一次 attach ——
    # 同一天重排两次日程就能碰见四次，而注释写的是"一天最多两次"。
    if str(state.get("day") or "") != day:
        state["day"] = day
        state["met"] = 0
    met = state.get("met") or 0
    try:
        met = int(met)
    except (TypeError, ValueError):
        met = 0

    tries = []
    for item in items:
        if not isinstance(item, dict):
            continue
        what = str(item.get("what") or "")
        for npc in CAST:
            if _match(npc, what):
                tries.append((item, npc))
                break

    out: List[Dict[str, Any]] = []
    used = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        partner = next((n for (i, n) in tries if i is item), None)
        if partner is not None and met < MEET_MAX_PER_DAY:
            if partner["id"] not in used:
                rng = _seed(day, partner["id"], "meet")
                if rng.random() < MEET_PROBABILITY:
                    text = _state_for(partner, day, seen, at)
                    row["meet"] = {
                        "who": partner["name"],
                        "spot": partner["spot"],
                        "text": text,
                    }
                    used.add(partner["id"])
                    met += 1
                    state["met"] = met
                    try:
                        note_seen(state, partner["id"], at=at, text=text)
                    except Exception:
                        pass
        out.append(row)
    if met:
        try:
            save(state, root)
        except Exception:
            pass
    return out


def meet_lines(items: List[Dict[str, Any]]) -> List[str]:
    """把当天的"碰见"抽成几行人话，给日记/复盘素材用。"""
    out: List[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        meet = item.get("meet")
        if not isinstance(meet, dict):
            continue
        who = str(meet.get("who") or "").strip()
        text = str(meet.get("text") or "").strip()
        if not who:
            continue
        out.append(f"{who}：{text}" if text else who)
    return out


def cast_block(limit: int = 5) -> str:
    """给提示词的一段：她生活里有这几个人（不是朋友，是熟人）。"""
    rows = []
    for npc in CAST[:limit]:
        rows.append(f"- {npc['name']}（{npc['spot']}）：{npc['quirk']}")
    if not rows:
        return ""
    return "## 你生活里这几个人\n" + "\n".join(rows) + (
        "\n（都是点头之交，不是朋友。可以提到他们，不许新增别人。）")


def render(now: Optional[float] = None, root: Optional[str] = None) -> str:
    """一行摘要，给日志和调试用。"""
    try:
        state = load(root)
        seen = state.get("seen") or {}
    except Exception:
        return ""
    bits = []
    for npc in CAST:
        row = seen.get(npc["id"]) or {}
        # 手改坏的数据（值是字符串）不该让这个函数抛 —— 它只给日志用，
        # 抛出去会把调用方的诊断整段打断。
        if not isinstance(row, dict):
            continue
        try:
            n = int(row.get("count") or 0)
        except (TypeError, ValueError):
            continue
        if n:
            bits.append(f"{npc['name']}×{n}")
    return " / ".join(bits)
