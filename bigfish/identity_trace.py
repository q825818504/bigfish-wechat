# -*- coding: utf-8 -*-
"""[bigfish] 身份判定的证据链 —— 当时凭什么这么判。

## 为什么只记"拿不准"的那几条

一轮一条、一天几千条，如果全都记，99% 是 `sender == me → 对方`，
信息量为零，只会把真正要看的那几条冲掉。

出问题时要回答的是**另一个**问题：

> 这一条我当时凭什么判成"对方说的"？

所以只在结论不是确定值（`WHO_UNKNOWN`）时落一份。一天下来通常是几条到几十条，
打开就能看完。

## 和 prompt_trace 同一套纪律

  · **旁路**：写失败绝不影响聊天，对外函数不抛异常
  · **有界**：默认保留最近 200 份，按文件名滚动删除最旧的
  · **可关**：`config.json` 的 `identity_trace.enabled=false` 完全停写
  · **不碰记忆库**：这是可丢的诊断数据，不写 vault、不做 schema 迁移

目录：`<数据根>/identity-trace/<YYYYMMDD-HHMMSS-mmm>-<chat>.json`

## 为什么不按 mtime 排序

跟 prompt_trace 同一条教训：Windows 的 mtime 精度约 100ms，
同一批写入会撞在一起，按 mtime 排会把刚写的那份当最旧的删掉。
文件名里带毫秒 + 单调序号，按名字排就是按时间排。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from itertools import count
from typing import Any, Dict, Optional

DEFAULT_KEEP = 200
#: 正文最多记这么多字。够看出是什么消息，又不至于把聊天记录抄一份。
EXCERPT_CHARS = 80

#: 规则版本。判定逻辑改了要加一 —— 旧记录属于旧规则，不能混着解释。
RULE_VERSION = 1

_write_lock = threading.Lock()
_seq = count(1)


# ══════════════════════════════════════════════════════════════════
#  路径与配置
# ══════════════════════════════════════════════════════════════════

def _data_root() -> str:
    try:
        from bigfish.vault import resolve_data_root

        root = resolve_data_root()
        if root:
            return root
    except Exception:
        pass
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def trace_dir() -> str:
    return os.path.join(_data_root(), "identity-trace")


def _config() -> Dict[str, Any]:
    try:
        base = os.path.dirname(_data_root())
        for path in (os.path.join(base, "config.json"),
                     os.path.join(_data_root(), "config.json")):
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f).get("identity_trace") or {}
    except Exception:
        pass
    return {}


def enabled() -> bool:
    return bool(_config().get("enabled", True))


def keep_count() -> int:
    try:
        return max(10, int(_config().get("keep", DEFAULT_KEEP)))
    except Exception:
        return DEFAULT_KEEP


# ══════════════════════════════════════════════════════════════════
#  写
# ══════════════════════════════════════════════════════════════════

def _safe_name(value: str, limit: int = 20) -> str:
    text = re.sub(r"[^0-9A-Za-z_\-@]+", "_", str(value or "")).strip("_")
    return (text or "chat")[:limit]


def _excerpt(text: str) -> str:
    body = str(text or "").replace("\x01", "").strip()
    body = re.sub(r"\s+", " ", body)
    if len(body) > EXCERPT_CHARS:
        return body[:EXCERPT_CHARS] + "…"
    return body


def _roll(directory: str, keep: int) -> None:
    """只留最近 keep 份。文件名带毫秒和序号，按名字排就是按时间排。"""
    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith(".json"))
    except Exception:
        return
    for name in names[:-keep] if keep > 0 else []:
        try:
            os.remove(os.path.join(directory, name))
        except Exception:
            pass


def record(verdict, decision, *, chat_id: str = "", is_group: bool = False,
           sender: str = "", me: str = "", text: str = "",
           writer=None, force: bool = False) -> Optional[str]:
    """记一条判定证据。返回写出的路径，没写返回 None。

    **只在判定不干净时写**（`verdict.certain` 为假）。确定是自己的、
    确定是对方的，都不写 —— 那是噪音。

    `writer` 可注入，测试用；默认走文件。任何异常都被吃掉：
    旁路功能不能影响聊天。

    `force=True` 时连确定结论也写 —— 给"账本否决"用（见上）。
    """
    try:
        trail = getattr(verdict, "as_dict", None)
        if callable(trail):
            vdict = trail()
        else:
            vdict = {"who": getattr(verdict, "who", ""),
                     "rule": getattr(verdict, "rule", ""), "evidence": []}

        # 干净的判定不写。`force` 例外：账本否决（ledger_veto）本身是个
        # 干净的 SELF 结论，但它是"推翻了另一个结论"，必须留痕，
        # 否则万一吞了对方的真话，没人看得出是谁干的。
        certain = getattr(verdict, "certain", True)
        if certain and not force:
            return None
        # 注入了 writer 就不读配置：测试不该顺带读真 config.json
        if writer is None and not enabled():
            return None

        now = datetime.now()
        payload = {
            "at": now.isoformat(timespec="milliseconds"),
            "rule_version": RULE_VERSION,
            "chat": str(chat_id or ""),
            "is_group": bool(is_group),
            "sender": str(sender or ""),
            "me": str(me or ""),
            "verdict": vdict,
            "decision": {
                "proceed": bool(getattr(decision, "proceed", False)),
                "reason": getattr(decision, "reason", ""),
                "detail": getattr(decision, "detail", ""),
            },
            "excerpt": _excerpt(text),
        }

        if writer is not None:
            writer(payload)
            return ""

        directory = trace_dir()
        os.makedirs(directory, exist_ok=True)
        with _write_lock:
            stamp = now.strftime("%Y%m%d-%H%M%S-") + f"{now.microsecond // 1000:03d}"
            name = f"{stamp}-{next(_seq):05d}-{_safe_name(chat_id)}.json"
            path = os.path.join(directory, name)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        _roll(directory, keep_count())
        return path
    except Exception:
        return None


def recent(limit: int = 20) -> list:
    """最近的几份记录（新的在前）。界面用。失败返回空列表。"""
    directory = trace_dir()
    try:
        names = sorted((n for n in os.listdir(directory) if n.endswith(".json")),
                       reverse=True)[:max(1, limit)]
    except Exception:
        return []
    out = []
    for name in names:
        try:
            with open(os.path.join(directory, name), "r", encoding="utf-8") as f:
                item = json.load(f)
            item["_file"] = name
            out.append(item)
        except Exception:
            continue
    return out
