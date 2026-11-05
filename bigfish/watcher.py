"""[bigfish] 消息监听 —— 轮询数据库，发现新消息就回调。

为什么不监听界面：
    UIA 读消息列表要滚动、要匹配，慢且脆。
    数据库里已经有全部消息，直接查 sort_seq 水位即可，
    零界面操作、不抢焦点、不需要桌面解锁。

工作方式：
    每隔 interval 秒，对登记的每个会话查一次
        sort_seq > 上次水位
    有就回调，并推进水位。

水位持久化到磁盘，进程重启不会重复处理旧消息。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

# 走 paths，不自己拼 __file__ —— 打包后 __file__ 可能是相对路径，
# abspath 会按当前工作目录解析，水位文件就会写到"启动目录"旁边去。
from bigfish.paths import app_root, res_root  # noqa: E402

_ROOT = res_root()
_VENDOR = os.path.join(res_root(), "vendor", "wechatauto-replica")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

from bigfish.render import render as render_message  # noqa: E402
from bigfish import identity                        # noqa: E402

WATERMARK_FILE = os.path.join(app_root(), "state", "watermark.json")


@dataclass
class Incoming:
    """一条新消息。"""

    chat_id: str = ""
    chat_name: str = ""
    local_id: int = 0
    sort_seq: int = 0
    timestamp: int = 0
    msg_type: str = ""
    sender_id: object = None
    sender_name: str = ""
    text: str = ""
    is_self: bool = False
    #: 归属判定：self / peer / unknown（空 = 未判定，旧数据）
    identity: str = ""
    #: 哪条规则得出的（见 bigfish/identity.py）
    identity_rule: str = ""
    raw_content: str = ""

    def preview(self, width: int = 60) -> str:
        c = (self.text or "").replace("\n", " ")
        return c if len(c) <= width else c[:width] + "…"


def _load_watermarks() -> Dict[str, int]:
    if os.path.isfile(WATERMARK_FILE):
        try:
            with open(WATERMARK_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            return {k: int(v) for k, v in d.items() if str(v).isdigit()}
        except Exception:
            pass
    return {}


def _save_watermarks(wm: Dict[str, int]) -> None:
    try:
        os.makedirs(os.path.dirname(WATERMARK_FILE), exist_ok=True)
        tmp = WATERMARK_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(wm, f, ensure_ascii=False, indent=2)
        os.replace(tmp, WATERMARK_FILE)
    except Exception:
        pass


class MessageWatcher:
    """轮询数据库监听新消息。

    用法：
        w = MessageWatcher(db)
        w.watch("文件传输助手")
        w.on_message = lambda m: print(m.preview())
        w.start()
        ...
        w.stop()
    """

    def __init__(self, db, interval: float = 1.2,
                 on_message: Optional[Callable[[Incoming], None]] = None,
                 on_log: Optional[Callable[[str], None]] = None):
        self.db = db
        self.interval = interval
        self.on_message = on_message
        self.on_log = on_log or (lambda s: print(s, flush=True))

        self._chats: List[str] = []
        # 水位不跨进程持久化：进程重启就是一次新的对齐（见 watch() 的说明）。
        # 之前落盘导致残留值把新消息挡在外面，是这个模块最大的坑。
        self._wm: Dict[str, int] = {}
        self._prefill: Dict[str, set] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._me = ""
        try:
            self._me = db.wxid or ""
        except Exception:
            self._me = ""

    # ------------------------------------------------------------------
    def watch(self, chat: str) -> str:
        """登记要监听的会话（昵称/群名/wxid 都行），返回解析后的 username。

        水位初始化（踩过两个坑，这里的做法是最终版）：

          坑1：把水位设成「当前最新那条」，用户刚发完消息就启动时，
               那条的 sort_seq 恰好等于水位，被 `>` 跳过。
          坑2：水位从 0 开始但 prefill 没建（因为水位文件残留导致
               `if uid not in self._wm` 不成立），重启后行为不一致。

        最终做法：
          - **每次启动都重新对齐**：读最新一条的 sort_seq 作为水位，
            同时把当前已存在的消息记入 prefill。
          - 这样「启动之前」的消息一律不算新消息（避免重启后被旧消息刷屏），
            「启动之后」的消息一定大于水位，能被 `>` 捕获。
          - 水位**不跨进程持久化**：进程重启就是一次新的对齐，
            避免残留值造成的不一致。
        """
        uid = self.db.username_by_nickname(chat) or chat
        if not (uid.endswith("@chatroom") or uid.startswith("wxid_")
                or uid == "filehelper"):
            gid = self.db.group_name_to_id(chat)
            if gid:
                uid = gid

        if uid in self._chats:
            return uid

        self._chats.append(uid)

        # 每次都重新对齐：水位 = 当前最新，prefill = 当前已存在的
        existing = []
        try:
            existing = self.db.get_messages(uid, limit=50)
        except Exception:
            pass
        if existing:
            top = int(existing[0].get("sort_seq") or 0)
            self._wm[uid] = top
            self._prefill[uid] = {
                (m.get("shard", ""), int(m.get("local_id") or 0))
                for m in existing
            }
        else:
            self._wm[uid] = 0
            self._prefill[uid] = set()
        return uid

    def watch_all_recent(self, limit: int = 10) -> List[str]:
        """监听最近活跃的若干会话。"""
        out = []
        for s in self.db.get_sessions(limit=limit):
            uid = s["username"]
            if uid not in self._chats:
                self.watch(uid)
                out.append(uid)
        return out

    # ------------------------------------------------------------------
    def _current_seq(self, uid: str) -> int:
        try:
            msgs = self.db.get_messages(uid, limit=1)
            if msgs:
                return int(msgs[0].get("sort_seq") or 0)
        except Exception:
            pass
        return 0

    def _display(self, uid: str) -> str:
        try:
            n = self.db.get_nickname(uid) or ""
            if n and n != uid:
                return n
            g = self.db.group_id_to_name(uid)
            if g:
                return g
        except Exception:
            pass
        if uid == "filehelper":
            return "文件传输助手"
        return uid

    # ------------------------------------------------------------------
    def poll_once(self) -> List[Incoming]:
        """查一轮，返回新消息（按时间升序）。

        为什么不用 db.get_new_messages：
          它内部是 `ORDER BY sort_seq ASC LIMIT n` —— 取的是**最老的 n 条**。
          水位从 0 时，每轮只能拿到最老的一批，永远追不到最新消息，
          表现就是「发了消息但监听不到」。（实测踩过）

          这里改用 get_messages（降序取最新 n 条），再按水位自己比较：
          取最新 50 条，筛出 sort_seq > watermark 的，
          因为是降序取的最新一批，新消息一定在这个窗口里。
        """
        found: List[Incoming] = []
        for uid in list(self._chats):
            seq = self._wm.get(uid, 0)
            try:
                # 取最新的一批（降序）
                rows = self.db.get_messages(uid, limit=50)
            except Exception as exc:
                self.on_log(f"[watcher] 查询 {uid} 失败: {exc}")
                continue
            if not rows:
                continue

            name = self._display(uid)
            pre = self._prefill.get(uid, set())
            fresh = []
            for r in rows:
                sseq = int(r.get("sort_seq") or 0)
                if sseq <= seq:
                    break            # 降序，遇到不比水位新的就可以停
                shard = r.get("shard", "")
                lid = int(r.get("local_id") or 0)
                if (shard, lid) in pre:
                    continue         # 启动时已存在，不是新消息
                fresh.append(r)

            if not fresh:
                continue

            # fresh 是降序的，反转成升序（先来的先处理）
            for r in reversed(fresh):
                sseq = int(r.get("sort_seq") or 0)
                shard = r.get("shard", "")
                sid = r.get("sender_id")
                try:
                    sender = (self.db.sender_username(shard, sid)
                              if shard else "")
                except Exception:
                    sender = ""
                mtype = str(r.get("type") or "")
                raw = str(r.get("content") or "")
                # 归属判定收口到 bigfish/identity.py。
                #
                # 以前这里是 `is_self=(sender == self._me) if sender else False` ——
                # sender 解析不出就当成"对方说的"，而"对方说的"意味着要回复。
                # 现在改成三态：确定自己 / 确定对方 / 拿不准，交给调用方按场景处理。
                who = identity.decide(sender, self._me, text=raw, chat_id=uid,
                                      is_group=str(uid).endswith("@chatroom"))
                found.append(Incoming(
                    chat_id=uid,
                    chat_name=name,
                    local_id=int(r.get("local_id") or 0),
                    sort_seq=sseq,
                    timestamp=int(r.get("create_time") or 0),
                    msg_type=mtype,
                    sender_id=sid,
                    sender_name=sender or "",
                    text=render_message(raw, mtype),
                    is_self=who.is_self,
                    identity=who.who,
                    identity_rule=who.rule,
                    raw_content=raw,
                ))
                if sseq > self._wm.get(uid, 0):
                    self._wm[uid] = sseq

        if found:
            _save_watermarks(self._wm)
            found.sort(key=lambda m: m.sort_seq)
        return found

    # ------------------------------------------------------------------
    def _run(self) -> None:
        self.on_log(f"[watcher] 开始监听 {len(self._chats)} 个会话，"
                    f"间隔 {self.interval}s")
        while not self._stop.is_set():
            try:
                for m in self.poll_once():
                    try:
                        if self.on_message:
                            self.on_message(m)
                    except Exception as exc:
                        self.on_log(f"[watcher] 回调异常: {exc}")
            except Exception as exc:
                self.on_log(f"[watcher] 轮询异常: {exc}")
            self._stop.wait(self.interval)
        self.on_log("[watcher] 已停止")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
