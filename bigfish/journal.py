"""[bigfish] 她的日记：每天睡前写一篇，落在本地文件里。

═══════════════════════════════════════════════════════════════════════
和「睡前复盘」的分工
═══════════════════════════════════════════════════════════════════════

两件事都在睡前，但写的东西完全不同：

    复盘（narrative.py）  我改了什么看法  → 结构化 JSON → soul.selfNotes
    日记（这个文件）      今天我是怎么过的 → 自由散文   → journal/YYYY-MM-DD.md

为什么不合并在一次调用里：复盘要严格 JSON（`changed_view` 的字段一个
都不能错），日记要文笔。同一个输出里既要括号合法又要句子自然，
模型一定牺牲后者 —— 写出来会像"带 JSON 外壳的周报"。

所以两次调用，各写各的。

═══════════════════════════════════════════════════════════════════════
为什么是 Markdown 文件，不是 SQLite
═══════════════════════════════════════════════════════════════════════

用户要的是"在本地写一篇日志"——他会想直接打开看、想同步到网盘、
想打印、想拿记事本改两个字。

SQLite 做不到这些。存成一堆行，用户得先有个工具才能读到自己的日记，
那就不叫"她的日记本"了。

目录：`<数据根>/journal/YYYY-MM-DD.md`。
一天一篇，同一天重写就覆盖（日记是可以改的，不是账本）。

═══════════════════════════════════════════════════════════════════════
"不限字数"怎么落地
═══════════════════════════════════════════════════════════════════════

提示词里**不写字数要求**，也不要求"简洁""控制在 N 字以内"。
日记本来就该允许写得长、写得散、写跑题。

但代码里有一道软上限（`SOFT_LIMIT`）：超过就截断并记一行日志。
理由不是审美，是防跑飞 —— 模型偶尔会陷入复读，一篇日记写成八千字
同样的句子。截断比让用户打开一个 2MB 的 md 好。

═══════════════════════════════════════════════════════════════════════
进不进提示词
═══════════════════════════════════════════════════════════════════════

**不进。**

· 她"记得今天"靠的是 memory / mood / facts，不是重读日记；
· 上千字塞进 system 会挤掉记忆和对话，得不偿失；
· 真人也不会每天早上把昨天的日记读一遍再开口。

以后如果想让"她记得自己写过什么"，正确做法是把日记的**一句话摘要**
写进记忆库，而不是把全文注入。

═══════════════════════════════════════════════════════════════════════
什么时候写
═══════════════════════════════════════════════════════════════════════

入睡前 20 分钟，比复盘晚 10 分钟 —— 错开两次模型调用，避免同时排队。
和复盘一样，一天一次；写失败就明天再说，不重试。

素材不足时**仍然可以写**：日记允许平淡（"今天很安静"）。
只有连一句对话、一条事实都没有时才跳过 —— 那种情况写出来一定是编的。
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

#: 目录名（在数据根下）
DIR_NAME = "journal"

#: 软上限。超过就截断 —— 防模型复读，不是审美要求。
SOFT_LIMIT = 3000

#: 素材不足时至少要有这些，才允许写。
#:
#: ⚠ v0.10.2 起这个常量不再参与判定：用户要求"当天啥也没发生也能写"，
#:   所以门槛从"有没有内容"改成了"有没有一个可以依附的今天"。
#:   常量留着是为了让旧测试和外部脚本还能 import 到，不报错。
MIN_TALK = 0

SYSTEM = """你在写日记。用第一人称，写给以后的自己看。

## 先判断今天属于哪一种

**不限字数**，但也不等于每篇都写长。先分档：

- **允许空白**：什么也没发生，就写短。三五行，八十来字，交代今天怎么过去的就够。
- **有一两件小事**：两三百字。一件写细，别的带过。
- **有压在心头的事**：可以写到五六百甚至更多，但**七成篇幅给那一件事**，
  前面一整天压成两三句。

判据只有一个：今天有没有一件事，是以后还会想起来的。没有，就不是重日子。
**不许为了让篇幅像样而凑。** 空日子写得短，比写得满可信。

## 怎么写

1. 写今天**具体发生了什么**：谁说了什么、你当时怎么想、哪句话让你卡了一下。
   细节比结论重要。"他说加班到十点，我问吃没吃"比"今天聊了工作"好。
2. 空日子靠「少」，不靠「补」。能摆出来的只有材料里这几样：今天的时间线
   （几点做过什么）、日程里标了「（过去了）」的事（那是今天没做成的）、
   手头在做什么、天气、心情那一条。摆完就够——
   **不要为了让空白显得有内容，添一件材料里没有的动作。**
   「去厨房接了水」「把手机扣在桌上」这类读着像真的，但材料里没有就是假的。
3. 有重的事那天，写**停顿**，不写结论。你没想明白的地方就停在那儿。
   对方的话要短、要断——"是啊""不过""还是得干活"，碎句子比完整的安慰真。
4. 结尾**被什么打断**：汤开了、困了、他那边还没打字。写到没话说就停，
   不要收成漂亮的圆。十天里有一句像样的话就够了，每天都有那是散文集。
5. **同一句话不许出现第二次。** 心里搁着一件事可以反复回到它，但每次回来的
   说法要不一样。第二遍出现必须带新东西（新动作、新时间、新对象）——
   带不来就删掉。同义反复也算重复。
6. **避开最近几篇写过的东西。** 材料里给了最近几篇的开头，先读再动笔：
   同一件生活道具（汤、快递、看剧、整理衣服）三天内不要当第二次重心；
   天气不必每天都提；开头句式也不要重复。
7. **日期和事实要对得上。** 材料里有今天是星期几、天气是什么。别写出自己
   打自己的话——比如说明天要上班，又说还有一天假。

## 不许做的

- **日记里的每个动作、每件东西，都要能在今天的材料里找到出处。** 找不到的
  一个字都不写——哪怕那个细节很像真的，哪怕少了它日记会变短。
- **不许编「对方」的对话和行为。** 材料里没有的话，一个字都不要写
  "他说……""他今天……"。哪怕日记因此很短，也不能补一句假话。
- **材料里「今天碰见的人」可以写一句。** 卖鱼的、楼下老太太这类熟人，
  你只写你看到的那一眼（他在不在、说了句什么、摊子上什么样子），
  **不替他们编对话，也不替他们解释**（"他大概是不容易"这种不要写）。
  材料里没给你的人，当他不存在。
- 动作只能用材料里的（今天做过的事、手头在做的、今天的安排）。
  **没写进材料的日常，就当它没发生。**
- **材料少的时候，日记就该短。** 两三句话也是一篇日记。
- **不要总结**。不要"今天让我明白了……""总的来说……"。日记不是周报。
- 不要列点、不要标题、不要加粗。就是一段一段的话。
- 不要对读者说话。不要以"晚安"结尾。

开头直接从今天的第一件事写起，或者从"今天没什么事"写起都行。"""

_WEEKDAY = ("星期一", "星期二", "星期三", "星期四", "星期五",
            "星期六", "星期日")


def _weekday_cn(day: str) -> str:
    """把 2026-11-09 换成「星期一」。

    以前只把日期给模型，星期让它自己推——它推不出来，于是写出过
    周日晚上「明天又要上班」和「还有一天假」并存这种自相矛盾的句子。
    素材里直接给出星期，是让日记的事实能对得上最省事的办法。
    """
    try:
        return _WEEKDAY[datetime.strptime(str(day)[:10], "%Y-%m-%d").weekday()]
    except Exception:
        return ""


def dir_path(root: Optional[str] = None) -> str:
    from bigfish.vault import resolve_data_root

    return os.path.join(root or resolve_data_root(), DIR_NAME)


def path_for(day: str, root: Optional[str] = None) -> str:
    """一天的日记路径。day 形如 2026-11-08。"""
    safe = re.sub(r"[^0-9-]", "", str(day or ""))[:10] or "unknown"
    return os.path.join(dir_path(root), f"{safe}.md")


def list_days(root: Optional[str] = None, limit: int = 60) -> List[str]:
    """已有日记的日期，新的在前。"""
    d = dir_path(root)
    try:
        names = [n for n in os.listdir(d) if n.endswith(".md")]
    except Exception:
        return []
    days = sorted((n[:-3] for n in names), reverse=True)
    return days[:limit]


def read(day: str, root: Optional[str] = None) -> str:
    try:
        with open(path_for(day, root), "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def latest(root: Optional[str] = None) -> Tuple[str, str]:
    """最近一篇：(day, text)。没有就 ("", "")。"""
    for day in list_days(root, limit=1):
        text = read(day, root)
        if text:
            return day, text
    return "", ""


def write(day: str, body: str, *, meta: Optional[Dict[str, Any]] = None,
          root: Optional[str] = None) -> Tuple[bool, str, int]:
    """原子写一篇日记。返回 (ok, path, 字数)。

    格式刻意极简：一行标题 + 正文。用户会拿记事本打开它，
    任何 frontmatter / YAML 都会变成噪音。
    """
    text = clean(body)
    if not text:
        return False, "", 0
    path = path_for(day, root)
    lines = [f"# {day}", ""]
    text = text.strip()
    if text.startswith("#"):
        # 模型自己写了标题就去掉，避免两个 #
        text = re.sub(r"\A#+[^\n]*\n+", "", text).strip()
    lines.append(text)
    if meta:
        # 元信息放最后，一行注释，不挡阅读
        bits = " ".join(f"{k}={v}" for k, v in meta.items()
                        if v not in (None, "", 0))
        if bits:
            lines.append("")
            lines.append(f"<!-- {bits} -->")
    lines.append("")
    payload = "\n".join(lines)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
        os.replace(tmp, path)
        return True, path, len(text)
    except Exception:
        try:
            if os.path.exists(path + ".tmp"):
                os.remove(path + ".tmp")
        except Exception:
            pass
        return False, path, 0


_FENCE = re.compile(r"\A```[A-Za-z0-9_+.-]*[ \t]*\r?\n?(.*?)\r?\n?```[ \t]*\Z", re.S)

#: 模型常见的开场白。不剥掉的话，日记第一句会是"好的，这是今天的日记"。
_OPENERS = (
    "好的，这是今天的日记：", "好的，这是今天的日记。",
    "这是今天的日记：", "以下是今天的日记：", "日记：",
    "好的，我写一下：", "好的。",
)


def clean(raw: str) -> str:
    """把模型输出收拾成一篇能直接读的日记。

    只做三件事：去围栏、去开场白、超长截断。
    **不改写、不精简** —— 日记是她的东西，不该被程序润色。
    """
    text = (raw or "").strip()
    m = _FENCE.match(text)
    if m:
        text = m.group(1).strip()
    for opener in _OPENERS:
        if text.startswith(opener):
            text = text[len(opener):].lstrip()
            break
    text = text.strip()
    if len(text) > SOFT_LIMIT:
        cut = text[:SOFT_LIMIT].rstrip()
        # 尽量断在句末，不要切在半句
        for sep in ("。", "！", "？", "\n", "…"):
            idx = cut.rfind(sep)
            if idx >= SOFT_LIMIT * 0.7:
                cut = cut[:idx + 1]
                break
        text = cut + "\n\n（写到一半，停了。）"
    return text


def build_request(*, day: str, materials: Dict[str, Any]) -> Tuple[str, str]:
    """拼一次日记请求。素材按"对日记有用"排序。

    顺序和复盘不同：日记要的是**具体的话**，所以对话放最前，
    心情摘要放后面（它是二手材料，只能当氛围）。
    """
    wd = _weekday_cn(day)
    parts: List[str] = [f"今天是 {day}（{wd}）。" if wd else f"今天是 {day}。"]

    # 最近几篇的开头一句。跨篇重复比单篇内重复更容易穿帮：连着几天都从
    # 「今天」或「外面阴着」起头、或者天天写汤，读三篇就能看出来是生成的。
    # 只给开头——给全文既挤掉今天的素材，又给模型抄的机会。
    recent_opens: List[str] = []
    for prev in [d for d in list_days(limit=4) if d != day][:3]:
        for line in read(prev).splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("<!--"):
                continue
            recent_opens.append(f"- {prev}：{line[:40]}")
            break
    if recent_opens:
        parts.append("## 最近几篇是怎么开头的（别重复这些写法和道具）\n"
                     + "\n".join(recent_opens))

    talk = [str(x).strip() for x in (materials.get("talk") or [])
            if str(x).strip()]
    if talk:
        parts.append("## 今天说过的话\n" + "\n".join(talk[-24:]))
    else:
        # 空白要**明说**，不能留一个空段让模型自己猜。
        # 留空的时候它会以为"材料没给全"，然后自己补一段对话 ——
        # 这正是"瞎编"的入口。
        parts.append("## 今天没有人跟你说话\n"
                     "今天一条消息都没有。日记里不要出现「他说」「他问我」"
                     "这类句子 —— 那些话没有发生过。")

    plan = [str(x).strip() for x in (materials.get("plan") or [])
            if str(x).strip()]
    if plan:
        parts.append("## 今天本来打算做的\n" + "\n".join(
            "- " + x[:80] for x in plan[:6]))
    else:
        parts.append("## 今天没有安排\n日程是空的，没排什么事。")

    doing = str(materials.get("doing") or "").strip()
    if doing:
        # 只写"手头在做什么"，不写"全天都在做这个"。
        # 日记里写"我当时在收拾屋子"要有据，否则第二天一对就穿帮。
        parts.append(f"## 你现在手头在做的\n{doing}")

    trace = [str(x).strip() for x in (materials.get("trace") or [])
             if str(x).strip()]
    if trace:
        # 今天做过的事（按时段）。**这是日记唯一可以展开的动作来源** ——
        # 素材只有"此刻在做什么"时，模型会自己编一天（煮面、泡茶、楼下小孩）。
        parts.append("## 今天做过的事\n" + "\n".join(
            "- " + x[:60] for x in trace[:10]))

    mood = [str(x).strip() for x in (materials.get("mood") or [])
            if str(x).strip()]
    if mood:
        parts.append("## 今天的心情起落\n" + "\n".join(
            "- " + x[:80] for x in mood[:8]))

    facts = [str(x).strip() for x in (materials.get("facts") or [])
             if str(x).strip()]
    if facts:
        parts.append("## 今天说到的事\n" + "\n".join(
            "- " + x[:80] for x in facts[:8]))

    # 今天碰见的熟人。这一段的用处不是"素材更丰富"，而是让她的一天里
    # 第一次出现**不是他**的人 —— 卖鱼的、楼下喂猫的老太太。
    # 没有这一段，她的日记永远只有两个主语：我、他。
    met = [str(x).strip() for x in (materials.get("met") or [])
           if str(x).strip()]
    if met:
        parts.append("## 今天碰见的人\n" + "\n".join(
            "- " + x[:60] for x in met[:3]))
        cast = str(materials.get("cast") or "").strip()
        if cast:
            parts.append(cast)

    relation = materials.get("relation") or {}
    if relation:
        lines = []
        if relation.get("stage"):
            lines.append(f"- 现在的关系：{relation['stage']}")
        for ev in (relation.get("events") or [])[:5]:
            lines.append(f"- 发生过：{str(ev)[:60]}")
        if lines:
            parts.append("## 你们之间\n" + "\n".join(lines))

    proactive = [str(x).strip() for x in (materials.get("proactive") or [])
                 if str(x).strip()]
    if proactive:
        parts.append("## 你主动说的话和结果\n" + "\n".join(
            "- " + x[:80] for x in proactive[:6]))

    if materials.get("weather"):
        parts.append(f"## 天气\n{materials['weather']}")

    if materials.get("narrative_summary"):
        parts.append(f"## 你今天想过的一句话\n{materials['narrative_summary']}")

    user = "\n\n".join(parts)
    if len(user) > 2600:
        user = user[:2600]
    return SYSTEM, user


def has_material(materials: Dict[str, Any]) -> Tuple[bool, str]:
    """几乎总是能写 —— **空白本身是可以写的题材**。

    用户要的是"允许当天啥也没发生也能写日志"。
    所以门槛不是"今天有没有内容"，而是"有没有一个可以依附的今天"：

        有日期 / 有对话 / 有事实 / 有事件 / 有手头的事 / 有日程
        → 写

        连日期都没有（冷启动，什么都读不到）
        → 不写。那时候写出来只能是编的。

    注意这和复盘的门槛**故意不同**（D-104 要求 ≥3 条不重复对话）：
    复盘写的是"我的看法变了"，没有真实素材就一定是编；
    日记写的是"今天我是怎么过的"，平淡本身是真实的一种。
    """
    talk = [t for t in (materials.get("talk") or []) if str(t).strip()]
    facts = [t for t in (materials.get("facts") or []) if str(t).strip()]
    events = (materials.get("relation") or {}).get("events") or []
    doing = str(materials.get("doing") or "").strip()
    plan = [p for p in (materials.get("plan") or []) if str(p).strip()]
    met = [p for p in (materials.get("met") or []) if str(p).strip()]
    day = str(materials.get("day") or "").strip()
    # ⚠ met 必须进这个判断。它是「今天碰见的人」—— 只碰见人、别的都空的日子
    #   （没聊天、没事实、没日程）恰恰是**最该写**的那种：她一个人下楼，
    #   碰见了卖鱼的，回来什么也没干。漏了它，那天的日记永远是"不写"。
    if day or talk or facts or events or doing or plan or met:
        return True, ""
    return False, "连今天是哪天都不知道"


def summary(day: str, body: str) -> str:
    """面板/日志用的一行。"""
    if not body:
        return f"{day}：还没有"
    chars = len(re.sub(r"\s+", "", body))
    return f"{day}：{chars} 字"
