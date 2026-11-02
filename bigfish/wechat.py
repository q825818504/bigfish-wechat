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


def _classify(uid: str) -> str:
    """给会话分类。只看 username 的格式，不查库 —— 快且稳。

        @chatroom        群聊
        gh_              公众号 / 服务号
        @openim          企微 / 外部
        brandsession     品牌会话
        filehelper 等    系统
        其余             联系人
    """
    u = uid or ""
    if u.endswith("@chatroom"):
        return "group"
    if u.startswith("gh_"):
        return "official"
    if u.startswith("brandsessionholder") or u in (
            "filehelper", "fmessage", "medianote", "newsapp",
            "notifymessage", "qqmail", "floatbottle", "weixin"):
        return "system"
    if u.endswith("@openim"):
        return "official"
    # holder 类：微信内部的占位账号（"未知联系人(holder)"），
    # 不是真人，混在联系人里会让人以为名单是脏的。
    if "holder" in u:
        return "system"
    return "contact"


def _clean_name(value: str) -> str:
    """洗掉昵称里的控制字符。

    微信昵称里混控制字符是常态：\\x7f 做占位、\\\\u200b 零宽空格、
    还有一堆不可见的分隔符。不做清洗的话，界面上会出现
    「看起来非空、实际一片空白」的名字，比空字符串更难排查。
    """
    if not value:
        return ""
    out = []
    for ch in str(value):
        code = ord(ch)
        # C0/C1 控制符、DEL、零宽与格式字符、BOM 一律丢弃
        if code < 0x20 or code == 0x7f or 0x80 <= code <= 0x9f:
            continue
        if code in (0x200b, 0x200c, 0x200d, 0x200e, 0x200f, 0xfeff):
            continue
        out.append(ch)
    return "".join(out).strip()


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

    def close(self) -> None:
        """释放当前数据库连接。

        ⚠ 切换账号时必须先 close 再 open 新的 —— 否则旧的 WeChatDB
          还握着上一账号的密钥与文件句柄，很可能出现
          "明明选了 B，读出来的还是 A 的内容"。
        """
        db = self._db
        self._db = None
        if db is None:
            return
        # WeChatDB 没暴露公开的 close，逐项尽力释放；失败不影响重新打开
        for attr in ("_conns", "conns", "_cache"):
            try:
                v = getattr(db, attr, None)
                if isinstance(v, dict):
                    for c in v.values():
                        try:
                            c.close()
                        except Exception:
                            pass
                    v.clear()
            except Exception:
                pass

    @property
    def account(self) -> str:
        return self._db.account if self._db else ""

    @property
    def wxid(self) -> str:
        return self._db.wxid if self._db else ""

    def self_info(self) -> Dict:
        """自己的账号信息。昵称要**兜底**，不能直接用数据库里的。

        ⚠ 微信在 contact 表里对"自己"那条记录不存真名，实测值是
          '\x7f\x7f\x7f\x7f'（四个 DEL 控制字符）。直接用它，
          界面上就是一块空白 —— 用户不知道现在连着哪个号。

        兜底顺序（按"像人话"的程度递减）：
          1. contact.nick_name     真名（大多数人的自己记录是空的）
          2. contact.remark        备注名
          3. contact.alias         **微信号**（如 daimeng2001，可读性最好）
          4. wxid                  最后手段，至少能区分账号

        实测：自己那条 nick_name='\x7f\x7f\x7f\x7f'、remark=''、alias='daimeng2001'。
        所以 alias 这一级**必须有** —— 没有它，界面上只能显示 wxid。
        """
        self._require()
        info = self._db.get_self_info() or {}
        nick = _clean_name(info.get("nick_name") or "")
        source = "nick" if nick else ""
        if not nick:
            nick = _clean_name(info.get("remark") or "")
            source = "remark" if nick else ""
        if not nick:
            nick = _clean_name(self._self_alias())
            source = "alias" if nick else ""
        if not nick:
            nick = info.get("username") or self.wxid or ""
            source = "wxid"
        return {**info, "nick_name": nick, "nick_source": source,
                "raw_nick": info.get("nick_name") or "",
                "alias": self._self_alias()}

    def _self_alias(self) -> str:
        """自己那条 contact 记录的 alias（微信号）。取不到返回空串。"""
        try:
            wxid = self.wxid
            for rel, path, _ in self._db._db_files:
                if not path.endswith("contact.db"):
                    continue
                conn = self._db._open(rel)
                try:
                    row = conn.execute(
                        "SELECT alias FROM contact WHERE username=? LIMIT 1",
                        (wxid,)).fetchone()
                    return (row[0] if row else "") or ""
                finally:
                    conn.close()
        except Exception:
            return ""
        return ""

    # ------------------------------------------------------------------
    def sessions(self, limit: int = 200) -> List[Dict]:
        """会话列表，附带解析好的显示名与分类。

        limit 默认从 20 提到 200：以前界面只显示最近 20 个，
        用户会觉得"我的会话不全"。

        ⚠ 但 200 条里实测公众号占 71% —— 真正的人只有 42 个。
          所以这里**按分类分桶取**：联系人和群聊给足，公众号只留最近 20 个。
          用户要的是"能回复的会话"，不是"完整的会话历史"。
        """
        self._require()
        quota = {"contact": 300, "group": 300, "official": 20, "system": 5}
        used = {"contact": 0, "group": 0, "official": 0, "system": 0}
        out = []
        for s in self._db.get_sessions(limit=max(limit, 400)):
            uid = s["username"] or ""
            kind = _classify(uid)
            if used[kind] >= quota[kind]:
                continue
            used[kind] += 1
            out.append({
                "username": uid,
                "name": self._display_name(uid),
                "unread": s.get("unread", 0),
                "summary": (s.get("summary") or "")[:40],
                "last_time": s.get("last_time"),
                "is_group": kind == "group",
                "kind": kind,
            })
            if len(out) >= limit:
                break
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
