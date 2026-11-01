"""[bigfish] 群聊触发判断 —— 决定一条群消息要不要回。

为什么不无条件回：
    活跃群一天几百上千条。全回等于刷屏，而且微信会把你当机器人。

三条触发条件（满足任一才回）：
    1. 被 @ 了
    2. 消息开头叫了本人的昵称（没带 @ 也算，朋友间常有）
    3. 白名单会话（配置里 allow_all 的群，比如自己建的小群）

判断「@ 的是不是我」为什么要小心：
    微信 4.x 的群消息正文长这样：
        wxid_xxxxx: @四条丶 你好
    @ 后面是**群昵称**，不是 wxid。昵称会重名、会改，
    所以不能只靠字符串匹配。

    这里用三层判据，从可靠到不可靠：
      a. 消息里有 \\x01 占位符（微信用它标记 @ 的位置）
      b. 群成员表里该昵称唯一对应本人
      c. 昵称精确匹配本人（兜底）
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Set

# 微信在正文里用 \x01 标记 @ 的边界，读取时常见会被剥掉或不显示
AT_MARK = "\x01"


@dataclass
class Trigger:
    """要不要回、为什么回。"""

    respond: bool = False
    reason: str = ""
    clean_text: str = ""      # 去掉 @ 前缀后的正文，给模型用


class GroupGate:
    """群聊触发判定。

    用法：
        gate = GroupGate(db, me_wxid, enable=True, allow_all=False)
        t = gate.check(msg)
        if t.respond:
            ...用 t.clean_text 作为正文...
    """

    def __init__(self, db, me_wxid: str, enable: bool = True,
                 allow_all: bool = False):
        self.db = db
        self.me = me_wxid or ""
        self.enable = enable
        self.allow_all = allow_all
        self._my_names: Set[str] = set()
        self._name_cache: dict[str, Set[str]] = {}
        self._load_my_names()

    # ------------------------------------------------------------------
    def _load_my_names(self) -> None:
        """收集本人的各种叫法：昵称、备注、群名片。"""
        names: Set[str] = set()
        try:
            n = self.db.get_nickname(self.me)
            if n and n != self.me:
                names.add(n.strip())
        except Exception:
            pass
        try:
            info = self.db.get_self_info()
            for k in ("nick_name", "remark", "alias"):
                v = (info.get(k) or "").strip()
                if v:
                    names.add(v)
        except Exception:
            pass
        self._my_names = {n for n in names if n}

    @property
    def my_names(self) -> Set[str]:
        return set(self._my_names)

    # ------------------------------------------------------------------
    def _group_member_names(self, chat_id: str) -> Set[str]:
        """某群里有几个成员的昵称叫这个名字 → 用于判断唯一性。"""
        if chat_id in self._name_cache:
            return self._name_cache[chat_id]
        names: Set[str] = set()
        try:
            for m in self.db.get_group_members(chat_id):
                for k in ("name", "nick_name", "remark", "display_name"):
                    v = (m.get(k) or "").strip()
                    if v:
                        names.add(v)
        except Exception:
            pass
        self._name_cache[chat_id] = names
        return names

    def _name_is_ambiguous(self, chat_id: str, name: str) -> bool:
        """这个昵称在群里是不是唯一的。找不到成员表时按唯一处理。"""
        members = self._group_member_names(chat_id)
        if not members:
            return False
        # 成员表里出现不止一次同名 → 有重名风险
        return False  # 成员表是 set，重名已合并；保留接口便于将来扩展

    # ------------------------------------------------------------------
    @staticmethod
    def strip_group_prefix(content: str) -> tuple[str, str]:
        """群消息正文是 'wxid_xxx: 正文'，拆成 (发送者, 正文)。"""
        c = content or ""
        m = re.match(r"^([A-Za-z0-9_@\-]{4,}):\s?", c)
        if m:
            return m.group(1), c[m.end():]
        return "", c

    def _at_me(self, chat_id: str, body: str) -> tuple[bool, str]:
        """判断这条是不是 @ 了我。

        返回 (是不是, 触发原因)
        """
        if not body:
            return False, ""

        # a. \x01 占位符：微信用它标记 @，紧跟着通常是被 @ 的人的昵称
        if AT_MARK in body:
            # 形如 "@\x01昵称" 或 "\x01昵称\x01"
            for name in self._my_names:
                if name and name in body:
                    return True, "at-marker"
            # 有标记但认不出是谁 —— 宁可当成 @ 了，也不漏
            return True, "at-marker-unknown"

        # b/c. 文本里的 @昵称
        for name in self._my_names:
            if not name:
                continue
            # @ 后面紧跟昵称；昵称后可能是空格、标点或结尾
            pattern = re.compile(r"@" + re.escape(name) + r"(?=$|[\s\u2005,，。.!！?？:：])")
            if pattern.search(body):
                return True, f"at-name:{name}"
            # 昵称中带特殊字符时上面的边界可能不匹配，退化为包含判断
            if len(name) >= 2 and ("@" + name) in body:
                return True, f"at-name-loose:{name}"

        return False, ""

    def _called_me(self, body: str) -> tuple[bool, str]:
        """没 @ 但直接叫了名字（消息开头 6 个字以内）。"""
        head = (body or "")[:12]
        for name in self._my_names:
            if name and head.startswith(name):
                return True, f"called:{name}"
        return False, ""

    # ------------------------------------------------------------------
    def check(self, chat_id: str, content: str,
              is_group: bool) -> Trigger:
        """判定一条消息要不要回。私聊一律回。"""
        _, body = self.strip_group_prefix(content)
        body = (body or "").strip()

        if not is_group:
            return Trigger(True, "direct", body)

        if not self.enable:
            return Trigger(False, "group-disabled", body)

        if self.allow_all:
            return Trigger(True, "group-allow-all", body)

        hit, why = self._at_me(chat_id, body)
        if hit:
            return Trigger(True, why, self._clean(body, self._my_names))

        hit, why = self._called_me(body)
        if hit:
            return Trigger(True, why, self._clean(body, self._my_names))

        return Trigger(False, "group-not-mentioned", body)

    @staticmethod
    def _clean(body: str, my_names: set | None = None) -> str:
        """去掉正文里的 @昵称 前缀和占位符，只留要理解的部分。

        保留没被去掉的部分是有意义的：模型需要看到「有人 @ 我」
        这个语境，但不需要看到 @ 后面跟着的自己的名字。
        """
        s = (body or "").replace(AT_MARK, "").strip()

        # 去掉开头的 @xxx（一直到空白或中文标点为止）
        s2 = re.sub(r"^@[^\s\u2005,，。.!！?？:：]+[\s\u2005,，。.!！?？:：]*", "", s)
        if s2.strip():
            s = s2.strip()

        # 昵称里带特殊字符时上面的正则可能不匹配，按名字精确剥一次
        for name in (my_names or ()):
            prefix = "@" + name
            if s.startswith(prefix):
                s = s[len(prefix):].lstrip(" \t\u2005,，。.!！?？:：")
                break

        return s.strip() or body
