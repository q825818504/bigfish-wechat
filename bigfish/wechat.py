"""[bigfish] 微信只读数据源。

基于 vendor/wechatauto-replica 的数据库解密层，**只读不写**：
本模块不提供任何发送能力，不驱动微信界面，不修改微信文件。

取密钥需要管理员权限（微信 4.1.10.27 实测：普通权限
OpenProcess(PROCESS_VM_READ) 返回 err=5），由 db.py 里的
_bigfish_keybridge 自动处理，只在首次取密钥时弹一次 UAC。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional

# 让 import wechatauto 找到 vendor 里的副本
_HERE = os.path.dirname(os.path.abspath(__file__))
_VENDOR = os.path.join(os.path.dirname(_HERE), "vendor", "wechatauto-replica")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

from bigfish.render import render as render_message  # noqa: E402


@dataclass
class ChatMessage:
    """归一化后的一条消息。"""

    chat_id: str          # wxid_* 或 *@chatroom
    local_id: int
    timestamp: int
    msg_type: str         # 文本 / 图片 / 语音 / ...
    sender_id: object     # 原始 sender_id（数字，需按分片解析）
    sender_name: str      # 解析后的昵称，解析不到为空串
    content: str
    shard: str = ""
    is_self: bool = False

    def preview(self, width: int = 60) -> str:
        c = (self.content or "").replace("\n", " ")
        if len(c) > width:
            c = c[:width] + "…"
        return c


class WeChatReader:
    """微信数据库只读封装。

    用法：
        r = WeChatReader()
        r.open()                       # 首次会弹 UAC 取密钥
        for s in r.sessions(limit=10):
            print(s)
        for m in r.messages(s["username"], limit=20):
            print(m.preview())
    """

    def __init__(self, account: Optional[str] = None):
        self._db = None
        self._account = account

    # ------------------------------------------------------------------
    def open(self) -> "WeChatReader":
        """打开数据库（触发密钥提取）。"""
        from wechatauto import WeChatDB

        self._db = WeChatDB(account=self._account) if self._account else WeChatDB()
        return self

    @property
    def ready(self) -> bool:
        return self._db is not None and bool(getattr(self._db, "_keys", None))

    @property
    def account(self) -> str:
        return self._db.account if self._db else ""

    @property
    def wxid(self) -> str:
        return self._db.wxid if self._db else ""

    def self_info(self) -> Dict:
        self._require()
        return self._db.get_self_info()

    # ------------------------------------------------------------------
    def sessions(self, limit: int = 20) -> List[Dict]:
        """会话列表，附带解析好的显示名。"""
        self._require()
        out = []
        for s in self._db.get_sessions(limit=limit):
            uid = s["username"]
            out.append({
                "username": uid,
                "name": self._display_name(uid),
                "unread": s.get("unread", 0),
                "summary": (s.get("summary") or "")[:40],
                "last_time": s.get("last_time"),
                "is_group": uid.endswith("@chatroom"),
            })
        return out

    def _display_name(self, uid: str) -> str:
        """解析会话显示名。

        contact.db 里查不到的联系人（从未加好友 / 换了设备 / 已删除）会返回
        空串，这时退到群名片与会话摘要里的线索，实在没有就给一个
        「未知联系人(wxid 后 6 位)」，而不是留下一个空名字让下游没法说话。

        实测坑：有些联系人的 nick_name 字段里存的是纯控制字符
        （如 '\\x7f\\x7f\\x7f\\x7f'），strip() 不会去掉它们，
        必须显式清洗，否则会给下游一个「看起来非空、实际不可见」的名字。
        """
        # ---- 1. contact.db 的昵称 / 备注 ----
        try:
            name = self._sanitize(self._db.get_nickname(uid) or "")
        except Exception:
            name = ""
        if name and name != uid:
            return name

        # ---- 2. 微信号/别名（alias），比昵称更稳定 ----
        alias = self._sanitize(self._contact_field(uid, "alias") or "")
        if alias:
            return alias

        # ---- 3. 群名 ----
        try:
            g = self._db.group_id_to_name(uid)
            g = self._sanitize(g or "")
            if g:
                return g
        except Exception:
            pass

        # ---- 4. 固定名称 ----
        if uid == "filehelper":
            return "文件传输助手"

        # ---- 5. 兜底占位 ----
        if uid.endswith("@chatroom"):
            return f"群聊({uid[:10]}…)"
        tail = uid[-6:] if len(uid) > 6 else uid
        return f"未知联系人({tail})"

    @staticmethod
    def _sanitize(s: str) -> str:
        """剔除控制字符（保留普通空格与换行折叠后的内容）。"""
        import unicodedata
        if not s:
            return ""
        out = []
        for ch in s:
            cat = unicodedata.category(ch)
            # Cc=控制字符, Cf=格式字符, Cs=代理, Co=私用, Cn=未分配
            if cat in ("Cc", "Cf", "Cs", "Co", "Cn"):
                continue
            out.append(ch)
        return "".join(out).strip()

    def _contact_field(self, uid: str, field: str) -> str:
        """读 contact.db 里某联系人的单个字段（字段名白名单校验）。"""
        if field not in ("alias", "nick_name", "remark"):
            raise ValueError(f"不允许的字段: {field}")
        for rel, path, _ in self._db._db_files:
            if os.path.basename(path) != "contact.db":
                continue
            conn = self._db._open(rel)
            try:
                row = conn.execute(
                    f"SELECT {field} FROM contact WHERE username=? LIMIT 1",
                    (uid,),
                ).fetchone()
            finally:
                conn.close()
            return str(row[0]) if row and row[0] else ""
        return ""

    def contacts(self, keyword: str) -> List[Dict]:
        """按昵称/备注/微信号搜索联系人。"""
        self._require()
        return self._db.search_contact(keyword)

    def resolve(self, keyword: str) -> Optional[str]:
        """把昵称/群名换成读库用的 username。"""
        self._require()
        uid = self._db.username_by_nickname(keyword)
        if uid:
            return uid
        uid = self._db.group_name_to_id(keyword)
        if uid:
            return uid
        hits = self._db.search_contact(keyword)
        return hits[0]["username"] if hits else None

    # ------------------------------------------------------------------
    def messages(self, chat: str, limit: int = 20,
                 readable: bool = True) -> List[ChatMessage]:
        """读某个会话的最近消息（按时间倒序，最新的在前）。

        readable=True（默认）时把富媒体 XML 压成可读文本，
        否则保留数据库原始内容（调试用）。
        """
        self._require()
        uid = self.resolve(chat) or chat
        out: List[ChatMessage] = []
        me = self.wxid
        for m in self._db.get_messages(uid, limit=limit):
            shard = m.get("shard", "")
            sid = m.get("sender_id")
            try:
                sender = self._db.sender_username(shard, sid) if shard else ""
            except Exception:
                sender = ""
            mtype = str(m.get("type") or "")
            raw = str(m.get("content") or "")
            content = render_message(raw, mtype) if readable else raw
            out.append(ChatMessage(
                chat_id=uid,
                local_id=int(m.get("local_id") or 0),
                timestamp=int(m.get("create_time") or 0),
                msg_type=mtype,
                sender_id=sid,
                sender_name=sender or "",
                content=content,
                shard=shard,
                is_self=(sender == me) if sender else False,
            ))
        return out

    def pending(self, chat: str, since_seq: int = 0,
                limit: int = 200) -> List[Dict]:
        """增量拉取（sort_seq 水位之后的新消息），用于轮询。"""
        self._require()
        uid = self.resolve(chat) or chat
        return self._db.get_new_messages(uid, since_seq=since_seq, limit=limit)

    def watermark(self, chat: str) -> int:
        """取当前会话的 sort_seq 水位。"""
        self._require()
        uid = self.resolve(chat) or chat
        msgs = self._db.get_messages(uid, limit=1)
        if not msgs:
            return 0
        return int(msgs[0].get("sort_seq") or 0)

    # ------------------------------------------------------------------
    def groups(self) -> List[Dict]:
        self._require()
        return self._db.get_groups()

    def group_members(self, chatroom_wxid: str) -> List[Dict]:
        self._require()
        return self._db.get_group_members(chatroom_wxid)

    # ------------------------------------------------------------------
    def _require(self) -> None:
        if self._db is None:
            raise RuntimeError("请先调用 open()")
        if not self.ready:
            raise RuntimeError("数据库尚未解锁（缺密钥）")
