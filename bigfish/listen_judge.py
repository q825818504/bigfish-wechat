# -*- coding: utf-8 -*-
"""攒批判定：对方这句话说完了没有。

## 为什么需要它

固定窗口只能猜。窗口短了插话（「我稍微犹豫一点发，她就已经回复了」），
窗口长了又呆（对方明明说完了，她还在等）。真正决定"要不要再等"的，
是**这句话有没有说完**——而这个是模型最擅长、规则最难写准的东西。

所以这里做一件事：把对方刚发的几条消息摆给模型，问它
「按正常人的说话习惯，他很可能还有一句没发出来吗」。

## 边界

- **只回答继续等 / 可以回**，不参与说什么、回几条。
- 超时、解析失败、模型没配好 → 一律返回 `more=False`（按"说完了"处理）。
  宁可早回一句，也不要让一个判定把整批卡住——用户等的是回复。
- 上限由调用方（`MessageBatcher`）守：`judge_max_rounds` 与 `max_wait`。
  这里不做任何计时，纯函数 + 一次请求。
"""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

SYSTEM_PROMPT = """你在帮一个微信聊天程序判断：对方这一串消息**说完了没有**。

只输出一个 JSON 对象，不要任何其他文字、不要 Markdown：

{"more": true, "why": "不超过 20 字"}

判断规则：
- more=true —— 按正常人的说话习惯，他很可能还有一句没发出来。例如：
  · 最后一条以逗号、顿号、破折号、省略号结尾
  · 只有「在吗」「问你个事」「我跟你说」这类开场，没有正文
  · 正在列举（第一…第二…），清单没列完
  · 明显在等回应（「你说呢」「对不对」后面通常还有补充）
  · 太短且没有句末标点（「嗯」「那个」）
- more=false —— 这句话已经完整，可以回了。例如：
  · 句号、问号、感叹号收尾
  · 一段话说完了，信息完整
  · 对方只是在接一个话题、随口应了一声

拿不准就 more=false。早回一句的代价，远小于让对方干等。"""


def _loose_json(raw: str) -> Optional[Dict[str, Any]]:
    """从模型输出里抠 JSON（模型经常包 ```json 或前后加话）。"""
    s = (raw or "").strip()
    if not s:
        return None
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        if s.endswith("```"):
            s = s[:-3]
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        d = json.loads(s[start:end + 1])
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def render_lines(messages: Sequence[Any], peer: str = "对方") -> str:
    """把一批消息摆成模型能读的样子。

    `messages` 可以是字符串，也可以是 `{"text", "gap"}` 这样的字典
    （gap = 距上一条几秒）。间隔有用：隔了 8 秒才补一句的人，
    往往是真的在分条发。
    """
    out: List[str] = []
    for i, m in enumerate(messages):
        if isinstance(m, dict):
            text = str(m.get("text") or "")
            gap = m.get("gap")
        else:
            text, gap = str(m or ""), None
        text = text.replace("\n", " ").strip()
        if not text:
            continue
        head = f"{i + 1}. {peer}：{text}"
        if isinstance(gap, (int, float)) and gap > 0:
            head += f"（距上一条 {gap:.1f} 秒）"
        out.append(head)
    return "\n".join(out)


def judge_more(
    messages: Sequence[Any],
    *,
    peer: str = "对方",
    self_name: str = "",
    timeout: float = 6.0,
    cfg: Any = None,
) -> Tuple[bool, str]:
    """问模型：对方还有下一句吗。返回 (more, 原因)。

    任何失败都返回 (False, 原因) —— 判定本身不该成为回复的阻塞点。
    调用方应当把它放在后台线程里跑：一次请求 0.5~2 秒。
    """
    lines = render_lines(messages, peer=peer)
    if not lines.strip():
        return False, "没有正文，不需要再等"

    try:
        from bigfish.replier import ModelConfig, ModelError, call_model

        mc = cfg if cfg is not None else ModelConfig.load()
        if not mc.ready:
            return False, "模型没配好，按说完处理"
        # 判定只值几百 token，超时要短：等 6 秒才回一句，本身就很怪。
        try:
            mc = replace(mc, timeout=max(1.0, float(timeout)))
        except Exception:
            pass

        user = f"这段对话里，{peer}刚发来这些：\n\n{lines}\n\n他还有下一句吗？"
        if self_name:
            user += f"\n（回话的人叫{self_name}，不必考虑她怎么回。）"

        raw = call_model(mc, SYSTEM_PROMPT, user, attempts=1)
    except ModelError as exc:
        return False, f"判定失败：{exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"判定失败：{exc}"

    d = _loose_json(raw)
    if not d:
        return False, "判定输出看不懂，按说完处理"
    more = d.get("more")
    if isinstance(more, str):
        more = more.strip().lower() in ("true", "yes", "1", "是")
    why = str(d.get("why") or "")[:40]
    return bool(more), why
