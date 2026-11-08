"""[bigfish] 每一轮的结构化记录。

日志是给人读的，这个文件是给**眼睛和脚本一起**读的：一行一轮，字段固定。
排查「她为什么慢」「这一轮到底发没发」时，不用再从一堆中文日志里数时间。

为什么不塞进 memory.sqlite3：
  它跟"她记得什么"无关，是运行时观测。单独一个 jsonl，
  坏了也不影响聊天；想删随时删。

字段（缺的会省略）：

    ts          写入时刻
    chat        会话 id
    chat_name   会话名
    outcome     sent / failed / unknown / timeout / silent / unsent
    wait_ms     消息进来到开始生成
    gen_ms      生成（模型 + 切条 + 挑图）
    queue_ms    生成完到开始投递（排队等发送线程）
    send_ms     投递（开会话 + 逐条发 + 表情）
    parts       发了几条
    sticker     before / after / none
"""
from __future__ import annotations

import json
import os
import time

#: 保留多少行。单人桌面一天几十轮，2000 行够看一个多月。
KEEP_LINES = 2000
FILE_NAME = "turns.jsonl"


def path(root: str = "") -> str:
    if root:
        return os.path.join(root, FILE_NAME)
    from bigfish.vault import resolve_data_root

    return os.path.join(resolve_data_root(), FILE_NAME)


def record(**fields) -> None:
    """追加一行。任何失败都静默 —— 观测不该影响聊天。

    `_root=` 只给自检用（写到临时目录），业务代码永远不传。
    """
    try:
        root = str(fields.pop("_root", "") or "")
        p = path(root)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        row = {"ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        row.update({k: v for k, v in fields.items() if v is not None})
        with open(p, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        _trim(p)
    except Exception:
        pass


def _trim(p: str) -> None:
    try:
        with open(p, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= KEEP_LINES:
            return
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.writelines(lines[-KEEP_LINES:])
        os.replace(tmp, p)
    except Exception:
        pass


def recent(limit: int = 20, root: str = "") -> list:
    """最近 N 条（留给以后的面板 / CLI）。`root` 只给自检用。"""
    try:
        with open(path(root), "r", encoding="utf-8") as f:
            lines = f.readlines()[-max(1, int(limit)):]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out
    except Exception:
        return []
