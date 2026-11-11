"""[bigfish] 对话的短期记忆 —— 重启之后她还记得刚才聊到哪。

═══════════════════════════════════════════════════════════════════════
为什么需要它
═══════════════════════════════════════════════════════════════════════

`desktop.py` 里的 `convs` 是一个内存 dict：`{chat_id: [(who, text), ...]}`。
进程一重启，**最近几轮对话全部消失**。

后果很具体：你刚说完"明天我生日"，程序崩了重启，她开口问"明天什么日子来着"。
长期记忆库里有原文（`memory.sqlite3`），但那要靠检索命中 ——
"最近聊了什么"和"记得哪些事"是两件事。

所以这里只干一件事：**把最近 N 轮对话落成一行一行，重启后原样读回来。**

═══════════════════════════════════════════════════════════════════════
为什么是 jsonl 而不是塞进 memory.sqlite3
═══════════════════════════════════════════════════════════════════════

1. 两者的写法完全不同。记忆库是"一问一答之后写两条 + 分词进 FTS"，
   节奏是"一轮写一次"；这里是"每说一句追加一行"，要能容忍半截写入。
2. 记忆库挂了要降级（D-68），但不该连"最近聊了什么"一起丢。
   分开存，一边坏了另一边还在。
3. 出问题时可以直接用记事本打开看。记忆库不能。

文件位置：`<数据根>/history/<会话哈希>.jsonl`。
用哈希而不是原始 chat_id：chat_id 里可能带特殊字符（群号 @chatroom、
wxid 里的下划线），直接当文件名在 Windows 上会踩一堆坑。

═══════════════════════════════════════════════════════════════════════
边界
═══════════════════════════════════════════════════════════════════════

· 只存最近 `KEEP_LINES` 行，超了就重写一份（不是无限追加）。
· 任何失败都不抛：读不出来就是空历史，写不进去就是这次不记 ——
  「她这条回得对不对」不该被一个日志文件绑住。
· 不加密、不压缩。这份文件里没有密码，只有聊天原文，
  和微信自己的数据库同等级别；数据根已经在用户自己的目录下。
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import List, Optional, Tuple

#: 每个会话保留多少行。20 轮对话 ≈ 40 行，够"接着刚才的话说"，
#: 又不至于让 prompt 里塞满三小时前的内容。
KEEP_LINES = 120

#: 读回来时给 prompt 用多少条（一轮 = 对方 + 她）
DEFAULT_LOAD = 20

DIR_NAME = "history"


def _dir(root: Optional[str] = None) -> str:
    from bigfish.vault import resolve_data_root

    return os.path.join(root or resolve_data_root(), DIR_NAME)


def path_for(chat_id: str, root: Optional[str] = None) -> str:
    """会话 → 文件路径。用哈希做文件名，避免 chat_id 里的特殊字符。"""
    key = hashlib.sha1(str(chat_id or "").encode("utf-8")).hexdigest()[:16]
    return os.path.join(_dir(root), f"{key}.jsonl")


def _tail_lines(path: str, n: int) -> List[str]:
    """只读文件最后 n 行。

    为什么不 readlines()：这个文件会一直长（一天几十上百行），
    一年后是几 MB。而调用方要的永远是"最近几条"——
    每次把整个文件读进内存再切片，是纯粹的浪费。
    （2026-11-10：`_tics_block` 每轮都读一次，把它暴露出来了。）

    从尾部按块往回读，找到够 n 行就停。切到多字节字符中间时
    那一行会解码成乱码 —— json 解析失败会被跳过，只丢一行，可接受。
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = min(size, 32 * 1024)
            while True:
                f.seek(max(0, size - block))
                data = f.read()
                text = data.decode("utf-8", "replace")
                lines = text.splitlines()
                if len(lines) > n or block >= size:
                    return lines[-max(1, int(n)):]
                block = min(size, block * 2)
    except Exception:
        return []


def load(chat_id: str, *, limit: int = DEFAULT_LOAD,
         root: Optional[str] = None) -> List[Tuple[str, str]]:
    """读回最近 limit 条 (who, text)。任何失败都返回空列表。"""
    path = path_for(chat_id, root)
    if not os.path.isfile(path):
        return []
    lines = _tail_lines(path, int(limit) + 2)   # 多读两行兜住空行/坏行
    out: List[Tuple[str, str]] = []
    for raw in lines[-max(1, int(limit)):]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if not isinstance(item, dict):
            continue
        who = str(item.get("who") or "")
        text = str(item.get("text") or "")
        if text:
            out.append((who, text))
    return out


def append(chat_id: str, who: str, text: str, *,
           root: Optional[str] = None) -> bool:
    """追加一行。超过 KEEP_LINES 时顺手裁一次。失败返回 False。"""
    if not chat_id or not str(text or "").strip():
        return False
    path = path_for(chat_id, root)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = json.dumps({"who": str(who or ""), "text": str(text)},
                          ensure_ascii=False)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
    except Exception:
        return False
    _maybe_trim(path)
    return True


def _maybe_trim(path: str) -> None:
    """行数超了才裁。裁的时候保留最后 KEEP_LINES 行。

    为什么按行数而不是按大小：一份 200 行的对话可能只有 8KB，
    一份 12 行的可能 40KB（有人贴了一大段代码）。按行数更贴近
    "最近几轮"这个语义。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= KEEP_LINES:
            return
        keep = lines[-KEEP_LINES:]
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.writelines(keep)
        os.replace(tmp, path)
    except Exception:
        pass


def clear(chat_id: str, root: Optional[str] = None) -> bool:
    """抹掉一个会话的短期历史。清空/鲸落时用。"""
    path = path_for(chat_id, root)
    try:
        if os.path.isfile(path):
            os.remove(path)
        return True
    except Exception:
        return False


def clear_all(root: Optional[str] = None) -> int:
    """抹掉整个 history 目录。返回删掉的文件数。"""
    d = _dir(root)
    if not os.path.isdir(d):
        return 0
    n = 0
    try:
        for name in os.listdir(d):
            if not name.endswith(".jsonl"):
                continue
            try:
                os.remove(os.path.join(d, name))
                n += 1
            except Exception:
                pass
    except Exception:
        pass
    return n


def stats(root: Optional[str] = None) -> dict:
    """给「数据目录」页看一眼：几个会话、多少行、多大。"""
    d = _dir(root)
    if not os.path.isdir(d):
        return {"dir": d, "files": 0, "lines": 0, "bytes": 0}
    files = lines = size = 0
    try:
        for name in os.listdir(d):
            if not name.endswith(".jsonl"):
                continue
            p = os.path.join(d, name)
            files += 1
            try:
                size += os.path.getsize(p)
                with open(p, "r", encoding="utf-8") as f:
                    lines += sum(1 for _ in f)
            except Exception:
                continue
    except Exception:
        pass
    return {"dir": d, "files": files, "lines": lines, "bytes": size}
