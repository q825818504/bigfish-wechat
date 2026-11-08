"""[bigfish] 记忆库：原文层 + 事实层 + 检索 + 反思 + 遗忘。

为什么要有这一层（而不是继续只在 vault.py 里堆函数）
======================================================

第一版记忆只有一个「原文切片 + FTS」的原型：`capture_message()` 把每条消息
切成 800 字窗口塞进 chunks，`bridge.mjs` 拿这些 chunk 找相似的句子拼回去。
它记得住原话，但它没有「她认为什么」——于是：

  · **改不了**。她说错了，只能删掉那条原文，不能表达「我改了主意」。
    删掉之后连「她曾经以为过什么」都没了。
  · **压不住**。聊天越久，注入的原文越多；真正稳定的认识反而埋在里面。
  · **查得糙**。分词只有「单字 + 相邻二字」，没有 IDF、没有排序融合，
    结果按 maxChars 顺序截断。
  · **删不干净**。删一条消息要手工顺着 anchor_sources / activations /
    groups / replays / FTS 一条条清，漏一张表就留下幽灵命中。

这一层补上的东西，逐条对应上面的问题：

  1. **事实层**（`facts`）：anchor + predicate + object，带状态机
     `active / superseded / forgotten`、`superseded_by`、置信度、来源片段。
     纠正 = 新事实 + 旧事实标 superseded，**两边都留着**。
  2. **遗忘层**（`fact_events`）：忘记不是删除，是一条事件。
     原文还在（她确实听过），但检索和她本人都不会再拿它当结论。
  3. **检索融合**：词法（FTS5 + BM25）+ 可选向量 + MMR 去冗余，
     `maxChars` 不再靠截断，而是按分数填满预算。
  4. **反思**：`reflect_on_session()` 让模型只从「有来源的原文」提事实，
     输出严格 JSON，落地时按 anchor/predicate 做 supersede，并记事件。
  5. **锚点接入**：`ensure_anchor()` / `aliases` 真正被检索与反思用到，
     「老爸」「爸爸」「爸」归一成一个人。
  6. **级联删除收口**：`forget_chunk_side_tables()` 一处删干净，
     `delete_messages()` / `delete_session()` / `whale_fall` 全部走它。

设计约束（沿用 vault.py 的规矩，一条都没松）
--------------------------------------------
  · 任何对外函数都不抛异常，失败返回 `(ok, ..., error)` 或带 error 的 dict。
    「记忆坏了」不能变成「聊天崩了」。
  · 派生层可抛。`search_index` / `facts` / `fact_events` 都是**可以重建**的；
    权威永远是 `messages` / `chunks`。重建入口是 `rebuild_derived()`。
  · 表结构只做**加法**：新库走完整 DDL，老库由 `migrate()` 用
    `CREATE TABLE IF NOT EXISTS` 补齐，不改任何既有列。
  · 建表语句与 `vault.ensure_memory_db()` 共用同一份 DDL ——
    以前它俩各写一遍，少一张表就"打开就报错"。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# 版本号写入 PRAGMA user_version。
#   1  原文四表（MemoryStore 兼容）
#   2  事实层 + 反思 + 锚点元数据
#   3  多 owner 隔离 + 行为习惯 + 置顶 + visibility
#   4  person：跨平台同一个人
#   5  origin（来源场景）+ topic（话题标签）+ 群聊双写 + 事实来源校验
#   6  取用策略：last_used_at / use_count（事实）+ hit_count（原文）+ 画像层
# 只在 migrate() 里推进，不靠文件名或 PRAGMA 之外的任何状态。
SCHEMA_VERSION = 6

# 老库（v1/v2/v3）没有这些列，migrate() 用 ALTER TABLE 补。
# SQLite 的 ADD COLUMN 是常数时间，不影响已有行。
FACT_COLUMNS_V3: Tuple[Tuple[str, str], ...] = (
    # 这条事实属于哪个 owner（private:<platform>:<chat> 或 group:<platform>:<id>）
    ("owner", "TEXT NOT NULL DEFAULT ''"),
    # 共享级别：private / shared / public —— 见下面的 VIS_* 常数
    ("visibility", "TEXT NOT NULL DEFAULT 'private'"),
    # 平台来源，为以后接抖音 / B站预留；空 = 老数据（微信）
    ("platform", "TEXT NOT NULL DEFAULT 'wechat'"),
    # 置顶标记。置顶的事实进独立预算，不跟普通检索抢名额
    ("pinned", "INTEGER NOT NULL DEFAULT 0"),
    # v4：这条事实属于哪个"人"（跨平台）。空 = 还没认领，按 owner 单独算。
    ("person", "TEXT NOT NULL DEFAULT ''"),
    # v4：写入时就记下当时的 person —— 之后 owner 改认领了，
    # 老事实不会跟着跑。记忆归属要稳定，不能因为改绑定就整体搬家。
    ("person_at_write", "TEXT NOT NULL DEFAULT ''"),
)

OWNER_COLUMNS_V4: Tuple[Tuple[str, str], ...] = (
    ("person", "TEXT NOT NULL DEFAULT ''"),
)

# v5：来源场景 + 话题标签。
#
#   origin   这条事实是在哪儿说出来的
#              private          私聊里说的
#              group:<group_id> 群里说的（带群 id）
#              unknown          老数据，不知道出处
#
#   topic    这条事实讲的是哪一类事。**只用于"能不能告诉外人"的判定**，
#            不参与检索排序、不参与反思、不改置信度。
#
# 为什么 origin 要单独存，而不是从 owner 推：
#   owner 是"归谁"（A 的池 / 群 C 的池），origin 是"在哪儿说的"。
#   群聊双写之后，同一条事实在 A 池和 C 池各有一份，两份的 owner 不同，
#   但 origin 都是 group:C —— 只有把两者分开存，才说得清"这是在群里说的"。
FACT_COLUMNS_V5: Tuple[Tuple[str, str], ...] = (
    ("origin", "TEXT NOT NULL DEFAULT ''"),
    ("topic", "TEXT NOT NULL DEFAULT ''"),
)

# v6：取用策略 —— 「记得」和「想得起来」是两件事。
#
# 背景（用户的原意）：硬盘多大记忆就多大，不压缩。这条一直成立。
# 但存得住不等于用得上：提示词每轮只有那么大，几千条事实里能进去的就十几条。
# 以前按 `id DESC` 取（= 最新的优先），于是**越老的事实越沉底，永远进不来**。
# 三年后你说"我牙疼"，她想不起三年前你拔过智齿 —— 不是没存，是排序把它埋了。
#
#   last_used_at  这条事实上一次**被注入到提示词**的时间。注意不是"写入时间"、
#                 也不是"被搜索到"，是**真的用上了**。用它算衰减：久未使用的降权。
#   use_count     被用过多少次。高频使用的浮上来 —— 反复被验证有用。
#
# 为什么要记"用过"而不是"搜到"：搜到但没进提示词的，等于没起作用。
# 记它会让分数虚高，而那些其实从没帮上忙的条目会一直霸着前排。
FACT_COLUMNS_V6: Tuple[Tuple[str, str], ...] = (
    ("last_used_at", "INTEGER NOT NULL DEFAULT 0"),
    ("use_count", "INTEGER NOT NULL DEFAULT 0"),
)

# 事实取用的衰减半衰期（天）。
#
# MaiBot 用的是 24 小时 —— 那是给闲聊机器人调的，一句话隔一天就该忘。
# 我们要的是「三年后还认得你」，所以放到 90 天：
#     三个月没用到 → 权重减半
#     一年没用到   → 剩 1/16
#     但它**不会消失**，只是排到后面去 —— 一旦被新的检索命中，立刻回升。
FACT_HALF_LIFE_DAYS = 90.0

# 话题白名单：**只有这些话题的 shared 事实才允许说给非主人听**。
#
# 这张表是「B 问主人爱吃什么 → 她可以答」和「B 问主人住哪 → 她拒绝」的分界线。
# 定语用词统一放在常数里，反思提示词、UI 下拉、注入判定三处共用 ——
# 三处各写一份就会出现"模型说的话题 UI 里选不出来"这种对不上的问题。
TOPIC_WHITELIST: Tuple[str, ...] = ("食物", "爱好", "作息", "性格", "口头禅", "称呼")

# 黑名单：明确不许外传的话题。白名单之外的**一律**按黑名单处理，
# 这张表存在的意义是：UI 上有一个明确的"这条为什么不能说"，
# 以及给注入判定一个可读的拒绝理由。
TOPIC_BLACKLIST: Tuple[str, ...] = (
    "住址", "工作", "行程", "身体", "金钱", "家人", "联系人", "隐私",
)

# 灰区：我按「宁缺毋滥」全部划到不放行（用户已确认）。
# 单独列出来是为了以后想放开时，能一眼看到"当初是哪几条被我关掉的"，
# 而不是从黑名单里猜。
TOPIC_GRAY_REFUSED: Tuple[str, ...] = ("年龄", "职业", "城市", "情感", "近况")

TOPICS: Tuple[str, ...] = TOPIC_WHITELIST + TOPIC_BLACKLIST + TOPIC_GRAY_REFUSED


# ══════════════════════════════════════════════════════════════════════════
# 建表
# ══════════════════════════════════════════════════════════════════════════
#
# ⚠ 这份 DDL 是**唯一真源**：vault.ensure_memory_db() 直接 import 它。
#   以前那份抄在 vault.py 里，改了分词忘了改表，bridge 就报「没有这一列」。

BASE_STATEMENTS: Tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY, bank TEXT NOT NULL, session TEXT NOT NULL,
        event_key TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL,
        content_json TEXT NOT NULL, source_message_id TEXT,
        recorded_at INTEGER NOT NULL, source_time INTEGER,
        interrupted INTEGER NOT NULL DEFAULT 0,
        UNIQUE(bank,session,event_key))""",
    "CREATE INDEX IF NOT EXISTS messages_bank_session ON messages(bank,session,id)",
    "CREATE INDEX IF NOT EXISTS messages_bank_time ON messages(bank,source_time)",
    """CREATE TABLE IF NOT EXISTS chunks (
        id INTEGER PRIMARY KEY, message INTEGER NOT NULL REFERENCES messages(id),
        start INTEGER NOT NULL, end INTEGER NOT NULL, text TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'episodic',
        importance REAL NOT NULL DEFAULT 0.5,
        activated INTEGER NOT NULL, boosts INTEGER NOT NULL DEFAULT 0,
        last_boost INTEGER, UNIQUE(message,start))""",
    "CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(terms, tokenize='unicode61')",
    """CREATE TABLE IF NOT EXISTS anchors (
        id INTEGER PRIMARY KEY, bank TEXT NOT NULL, name TEXT NOT NULL,
        UNIQUE(bank,name))""",
    """CREATE TABLE IF NOT EXISTS aliases (
        anchor INTEGER REFERENCES anchors(id), alias TEXT NOT NULL,
        PRIMARY KEY(anchor,alias))""",
    """CREATE TABLE IF NOT EXISTS anchor_sources (
        anchor INTEGER REFERENCES anchors(id), chunk INTEGER REFERENCES chunks(id),
        PRIMARY KEY(anchor,chunk))""",
    """CREATE TABLE IF NOT EXISTS activations (
        bank TEXT NOT NULL, chunk INTEGER REFERENCES chunks(id),
        event_key TEXT NOT NULL, at INTEGER NOT NULL,
        PRIMARY KEY(bank,chunk,event_key))""",
    """CREATE TABLE IF NOT EXISTS ledger (
        id INTEGER PRIMARY KEY, bank TEXT NOT NULL, at INTEGER NOT NULL,
        kind TEXT NOT NULL, chars INTEGER NOT NULL, sources TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS housekeeping (
        bank TEXT PRIMARY KEY, last_chunk INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS groups (
        chunk INTEGER PRIMARY KEY REFERENCES chunks(id),
        bank TEXT NOT NULL, digest TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS replays (
        chunk INTEGER PRIMARY KEY REFERENCES chunks(id), at INTEGER NOT NULL)""",
)

# 派生层。全部可以删了重建，见 rebuild_derived()。
DERIVED_STATEMENTS: Tuple[str, ...] = (
    # 事实：她「认为」什么。和原文分开，是为了能纠正而不是只能删。
    """CREATE TABLE IF NOT EXISTS facts (
        id INTEGER PRIMARY KEY, bank TEXT NOT NULL,
        anchor TEXT, predicate TEXT NOT NULL, object TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 0.5,
        status TEXT NOT NULL DEFAULT 'active',
        superseded_by INTEGER, valid_from INTEGER, valid_to INTEGER,
        source_chunks TEXT NOT NULL DEFAULT '[]',
        evidence TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        owner TEXT NOT NULL DEFAULT '',
        visibility TEXT NOT NULL DEFAULT 'private',
        platform TEXT NOT NULL DEFAULT 'wechat',
        pinned INTEGER NOT NULL DEFAULT 0,
        person TEXT NOT NULL DEFAULT '',
        person_at_write TEXT NOT NULL DEFAULT '',
        origin TEXT NOT NULL DEFAULT '',
        topic TEXT NOT NULL DEFAULT '',
        last_used_at INTEGER NOT NULL DEFAULT 0,
        use_count INTEGER NOT NULL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS facts_bank_status ON facts(bank,status,id)",
    "CREATE INDEX IF NOT EXISTS facts_bank_anchor ON facts(bank,anchor,predicate)",
    "CREATE INDEX IF NOT EXISTS facts_owner ON facts(bank,owner,status,id)",
    "CREATE INDEX IF NOT EXISTS facts_vis ON facts(bank,visibility,status)",
    "CREATE INDEX IF NOT EXISTS facts_origin ON facts(bank,origin,status)",
    "CREATE INDEX IF NOT EXISTS facts_topic ON facts(bank,topic,visibility)",
    # v6：按取用排序要走这个索引
    "CREATE INDEX IF NOT EXISTS facts_used ON facts(bank,owner,status,last_used_at)",
    # 事实事件：记 → 改 → 忘，每一步都留痕。忘记不是删除。
    """CREATE TABLE IF NOT EXISTS fact_events (
        id INTEGER PRIMARY KEY, bank TEXT NOT NULL, fact INTEGER,
        kind TEXT NOT NULL, at INTEGER NOT NULL, reason TEXT NOT NULL DEFAULT '',
        payload TEXT NOT NULL DEFAULT '{}')""",
    "CREATE INDEX IF NOT EXISTS fact_events_bank ON fact_events(bank,at)",
    # 切片级检索元数据：词频统计（IDF）与去冗余用的签名。
    # FTS5 rank 的 bm25() 已经在用全局词频，这张表补的是「她自己说过哪些」。
    """CREATE TABLE IF NOT EXISTS chunk_meta (
        chunk INTEGER PRIMARY KEY REFERENCES chunks(id),
        bank TEXT NOT NULL, speaker TEXT NOT NULL DEFAULT '',
        token_count INTEGER NOT NULL DEFAULT 0,
        term_blob TEXT NOT NULL DEFAULT '')""",
    # v6：原文片段被检索**用上**过几次。
    #
    # 为什么单独一张表而不是给 chunks 加列：
    #   chunks 是权威层（vault 也写它），列越少越稳；而且 hit_count 是
    #   **可重建的派生数据**（重建索引时归零也无所谓），混进权威层会被误当成事实。
    #
    # 为什么需要它：FTS5 的 bm25 只看词频，不看"这条以前有没有帮上过忙"。
    #   同一段对话被问三次都命中的片段，比偶然命中一次的更该浮上来。
    """CREATE TABLE IF NOT EXISTS chunk_hits (
        chunk INTEGER PRIMARY KEY,
        hits INTEGER NOT NULL DEFAULT 0,
        last_hit INTEGER NOT NULL DEFAULT 0)""",
    # 反思水位：哪批消息已经被提炼过事实。
    """CREATE TABLE IF NOT EXISTS reflection_state (
        bank TEXT PRIMARY KEY, session TEXT NOT NULL DEFAULT '',
        last_message INTEGER NOT NULL DEFAULT 0,
        last_run INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '')""",
    # 反思水位（按会话）。
    #
    # ⚠ 上面那张表的主键只有 bank。一个库里只要有两个会话（私聊 + 群、
    #   或者两个人），先反思的那个会把水位推到它自己那批的末尾，
    #   另一个会话的消息 id 全都 <= watermark —— 于是**永不提炼**。
    #   症状是"只有最近说话的那个人有事实记录"，非常难查：
    #   日志里一切正常，只是另一个人永远是陌生人。
    #
    # 新表按 (bank, session) 分开记；老表保留不动，作兼容与汇总用。
    """CREATE TABLE IF NOT EXISTS reflection_sessions (
        bank TEXT NOT NULL, session TEXT NOT NULL,
        last_message INTEGER NOT NULL DEFAULT 0,
        last_run INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(bank, session))""",

    # ── v3：多用户隔离 ────────────────────────────────────────────────
    #
    # 以前全库只有一个 bank（"dafeiyu"），所有聊天混在一起。现在她要去
    # 微信以外的地方（群、抖音、B站），必须按「谁在跟我说话」分区。
    #
    # owner 的算法（与插件同一个思路，但更简单）：
    #     private:<platform>:<chat_id>     私聊，只有这个人能看到
    #     group:<platform>:<group_id>      群聊，群里的人共同可见
    #
    # ⚠ 绝不用昵称做 key —— 昵称会改、会重名。只用平台给的稳定 id。
    """CREATE TABLE IF NOT EXISTS owners (
        owner TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        platform TEXT NOT NULL DEFAULT 'wechat',
        external_id TEXT NOT NULL DEFAULT '',
        display_name TEXT NOT NULL DEFAULT '',
        tier TEXT NOT NULL DEFAULT 'guest',
        first_seen INTEGER NOT NULL DEFAULT 0,
        last_seen INTEGER NOT NULL DEFAULT 0,
        person TEXT NOT NULL DEFAULT '')""",
    "CREATE INDEX IF NOT EXISTS owners_person ON owners(person)",

    # ── v4：person（跨平台同一个人）─────────────────────────────────
    #
    # 问题：同一个人在微信是 wxid_xxx，在 B站是 UID 12345。
    # 两套 id 天然对不上 —— 它们在各自的平台里就是两个账号。
    #
    # 三种方案，只有第三种能用：
    #   ① 猜：昵称相同就合并 → 昵称会撞、会改，等于随机把人搞混
    #   ② 让模型判断：它一定会说"是的，看起来像同一个人" → 不可控
    #   ③ **人确认**：主人在界面上手动把一个身份挂到某个 person 上
    #
    # 所以 person 是一张"人工认领"的表。没有自动合并，一条都没有。
    """CREATE TABLE IF NOT EXISTS persons (
        person TEXT PRIMARY KEY,
        display_name TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL DEFAULT 0)""",

    # 合并事件：把 A 并进 B 必须留痕，否则以后没人说得清
    # "为什么 B站那个人能看到我微信里的记忆"。
    """CREATE TABLE IF NOT EXISTS person_events (
        id INTEGER PRIMARY KEY, at INTEGER NOT NULL,
        kind TEXT NOT NULL, person TEXT NOT NULL DEFAULT '',
        owner TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '')""",

    # ── v3：行为习惯 ──────────────────────────────────────────────────
    #
    # 「说够三次才算」。插件那边的规则值得抄：必须在**不同日期**出现够次数，
    # 单条消息永远不能变成稳定偏好；陈旧证据随时间衰减。
    """CREATE TABLE IF NOT EXISTS habits (
        id INTEGER PRIMARY KEY, bank TEXT NOT NULL, owner TEXT NOT NULL,
        category TEXT NOT NULL, signature TEXT NOT NULL,
        samples TEXT NOT NULL DEFAULT '[]',
        count INTEGER NOT NULL DEFAULT 0,
        days INTEGER NOT NULL DEFAULT 0,
        first_at INTEGER NOT NULL DEFAULT 0,
        last_at INTEGER NOT NULL DEFAULT 0,
        score REAL NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
        UNIQUE(bank, owner, category, signature))""",
    "CREATE INDEX IF NOT EXISTS habits_owner ON habits(bank,owner,score)",

    # ── v3：置顶 ──────────────────────────────────────────────────────
    #
    # 置顶 = "这条不该被挤掉"。独立预算，不跟普通检索抢名额。
    """CREATE TABLE IF NOT EXISTS pins (
        id INTEGER PRIMARY KEY, bank TEXT NOT NULL, owner TEXT NOT NULL,
        fact INTEGER, text TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL,
        UNIQUE(bank, owner, fact))""",

    # ── v5：角色印象 ──────────────────────────────────────────────────
    #
    # 「我对这个人的印象」是一段**主人手写**的话，不是模型产出的。
    # 所以它单独一张表，不塞进 owners（owners 是账号注册表，
    # 混进自由文本以后导数据会很难看），也不塞进 facts（facts 有来源校验，
    # 手写印象没有原文可指，塞进去会被来源校验直接拒掉）。
    #
    # 印象页里"开放信息"那一栏**不存这张表**，是实时从 facts 查出来的 ——
    # 存一份就等于给"她准备说什么"开了第二个真源，两边一定会漂。
    """CREATE TABLE IF NOT EXISTS impressions (
        bank TEXT NOT NULL, owner TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(bank, owner))""",

    # ── v6：画像（她自己归纳的「这个人是什么样」）─────────────────────
    #
    # 为什么需要它：
    #   事实层是**逐条**的。"爱吃辣""养了只猫""做设计"分开存，
    #   几千条之后每轮只能进去十几条，剩下的等于不存在。
    #   画像是**一段话**，900 字左右，把几百条事实压成"能整段带进提示词"的量。
    #
    # 它和 impressions 的区别（两张表，别混）：
    #   impressions.note  你手写的评语。模型不许动。
    #   profiles.text     她自己归纳的。你可以否决（enabled=0）。
    #
    # 为什么要 enabled 而不是直接删：
    #   归纳是定期跑的，删了下次又生成。否决要留痕，让她知道"这段你不认"，
    #   下次归纳时把被否的那部分当错误信号。删掉等于什么都不知道。
    #
    # facts_at / used_at：归纳时用掉了多少条事实、哪些。用来判断
    #   "要不要重新归纳"（事实涨了 50 条就该重跑），以及出错时能倒查。
    """CREATE TABLE IF NOT EXISTS profiles (
        bank TEXT NOT NULL, owner TEXT NOT NULL,
        text TEXT NOT NULL DEFAULT '',
        facts_at INTEGER NOT NULL DEFAULT 0,
        fact_ids TEXT NOT NULL DEFAULT '[]',
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(bank, owner))""",
)

# 共享级别。**这是防泄露的唯一闸门**，任何注入都必须先过它。
#
#   private  只属于某个 owner，绝不跨 owner 出现（默认）
#   shared   可以给"非主人"看的基础信息（爱吃什么、爱好、作息）
#   public   世界观级别，谁都能看（角色自己的设定）
#
# ⚠ 反过来不成立：private 的**永远**不能因为"对方问了"就升级为 shared。
#   升级必须由主人显式操作，不能由模型决定 —— 模型一定会被诱导。
VIS_PRIVATE = "private"
VIS_SHARED = "shared"
VIS_PUBLIC = "public"
VISIBILITIES = (VIS_PRIVATE, VIS_SHARED, VIS_PUBLIC)

# 主人 / 熟人 / 陌生人。非主人只能拿到 shared 与 public。
TIER_OWNER = "owner"
TIER_KNOWN = "known"
TIER_GUEST = "guest"
TIERS = (TIER_OWNER, TIER_KNOWN, TIER_GUEST)


def all_statements() -> Tuple[str, ...]:
    return BASE_STATEMENTS + DERIVED_STATEMENTS


# ══════════════════════════════════════════════════════════════════════════
# 分词 —— 与插件 store.js 的 terms() 逐条对齐（bridge 那边用同一套查）
# ══════════════════════════════════════════════════════════════════════════

_STOP = {
    "的", "了", "吗", "呢", "我", "你", "他", "它", "是", "在", "和", "有", "就", "啊",
    "the", "a", "is", "and",
}

_HAN_RUN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]+|[a-z0-9_]+")


def _is_han(ch: str) -> bool:
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF)


def terms(text: str) -> List[str]:
    """中文二元分词 + 英文数字整词。**改这里必须同步 store.js 与 bridge.mjs。**

    规则（照抄 JS）：
      · 汉字串：单字（去掉停用字）+ 相邻二字
      · 英文/数字串：整词（去掉停用词）
      · 去重且保序
    """
    out: List[str] = []
    for run in _HAN_RUN.findall((text or "").lower()):
        if _is_han(run[0]):
            chars = list(run)
            for i, ch in enumerate(chars):
                if ch not in _STOP:
                    out.append(ch)
                if i + 1 < len(chars):
                    out.append(ch + chars[i + 1])
        else:
            if run not in _STOP:
                out.append(run)
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def fts_query(text: str) -> str:
    """把自然语言变成 FTS5 MATCH 表达式。

    以前检索直接把原始 query 丢给 MATCH，中文没有空格，FTS5 会把整句当
    一个 token 去匹配 —— 于是"老爸今天加班"一条都命不中（库里存的是二元组）。
    这里改成：切成同样的二元组，用 OR 连接，再交给 bm25() 排序。
    """
    toks = terms(text)
    if not toks:
        return ""
    # FTS5 的列值里 token 之间是用空格分隔的，所以"相邻二字"本身就是一个 token。
    safe = []
    for t in toks:
        if re.fullmatch(r"[a-z0-9_\u4e00-\u9fff]{1,32}", t):
            safe.append(f'"{t}"')
    return " OR ".join(safe)


# ══════════════════════════════════════════════════════════════════════════
# 连接
# ══════════════════════════════════════════════════════════════════════════


def connect(path: str, *, read_only: bool = False, timeout: float = 10.0) -> sqlite3.Connection:
    """打开记忆库。

    `read_only=True` 走 URI 的 mode=ro：**不再把整个库拷到临时目录再读**。
    以前每轮对话拷一次 200MB，现在同一个文件并发读，WAL 模式本来就支持。
    拷库只在真需要快照时用（export_memory 的 VACUUM INTO）。
    """
    if read_only:
        uri = "file:" + path.replace("\\", "/") + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    else:
        conn = sqlite3.connect(path, timeout=timeout)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except Exception:
        return set()


def migrate(path: str) -> Tuple[bool, str]:
    """把一个已有库补齐到当前 schema。**只加表、只加列**，不改也不删。

    返回 (ok, error)。已经是最新时是空操作，可以每次启动都调。

    三步：
      1. 建所有派生的新表（CREATE TABLE IF NOT EXISTS）
      2. 给 facts 补 v3 的四列（ALTER TABLE ADD COLUMN，常数时间）
      3. 推进 user_version

    ⚠ ALTER TABLE 之前必须查 PRAGMA table_info —— SQLite 没有
      "ADD COLUMN IF NOT EXISTS"，重复执行会抛
      "duplicate column name"。这一步被 selftest 的"重复迁移"用例钉住。
    """
    if not path or not os.path.isfile(path):
        return False, "记忆库不存在"
    try:
        conn = sqlite3.connect(path, timeout=10)
        try:
            # ① **先补列**，再建表。
            #   顺序错了会踩这个坑：CREATE TABLE IF NOT EXISTS 对已存在的
            #    旧 facts 表什么都不做（列还是老的），而后面 UPDATE 里
            #    引用了新列 → "no such column: owner"。
            #    所以必须先 ALTER，再跑建表语句（建表语句对老表是空操作，
            #    对新表会建出完整结构）。
            _add_missing_columns(conn)

            # ② 建所有派生的新表（CREATE TABLE IF NOT EXISTS）
            for stmt in DERIVED_STATEMENTS:
                conn.execute(stmt)

            # ③ 老数据没有 owner：归到 legacy，但**保持 private**。
            #    绝不因为迁移就把私密内容变成共享。
            conn.execute(
                "UPDATE facts SET owner = 'legacy:' || bank "
                "WHERE owner IS NULL OR owner = ''")
            conn.execute(
                "UPDATE facts SET platform = 'wechat' "
                "WHERE platform IS NULL OR platform = ''")
            conn.execute(
                "UPDATE facts SET visibility = 'private' "
                "WHERE visibility IS NULL OR visibility = ''")
            # ④ v5：老数据的 origin / topic 一律留空 = "不知道出处 / 没打标签"。
            #    ⚠ 绝不猜。老库里的事实不知道是哪说的，猜成 private 或 group
            #      都会让"能不能外传"这个判定建立在一个编出来的前提上。
            #      留空的结果是：永远不会被说给外人听 —— 这是安全的方向。
            conn.execute(
                "UPDATE facts SET origin = '' "
                "WHERE origin IS NULL")
            conn.execute(
                "UPDATE facts SET topic = '' "
                "WHERE topic IS NULL")
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
            if ver < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
        finally:
            conn.close()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def ensure_schema(path: str) -> Tuple[bool, str]:
    """建库（不存在就建全量）+ 迁移（存在就补齐）。

    ⚠ 两步都会跑，而且**建表之前先补列**。
      只跑 all_statements() 是不够的：CREATE TABLE IF NOT EXISTS 对已经存在的
      旧 facts 表什么都不做，列还是老的 —— 于是写入时报
      "table facts has no column named person"。
      这个坑踩过两次（v3 一次、v4 一次），所以这里直接复用 migrate() 的逻辑，
      不再维护第二份。
    """
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10)
        try:
            # ① 老库补列（先于建表 —— 顺序反了就会踩上面那个坑）
            _add_missing_columns(conn)
            # ② 建表
            for stmt in all_statements():
                conn.execute(stmt)
            # ③ 再补一次：新库刚建出来的表已经是全列，这里是空操作；
            #    但万一 all_statements 里漏了某列，这一步能兜住。
            _add_missing_columns(conn)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
        finally:
            conn.close()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """给已有表补上 v3/v4/v5 新增的列。表不存在就跳过。"""
    existing = _columns(conn, "facts")
    if existing:
        for name, decl in FACT_COLUMNS_V3:
            if name not in existing:
                conn.execute(f"ALTER TABLE facts ADD COLUMN {name} {decl}")
        for name, decl in FACT_COLUMNS_V5:
            if name not in existing:
                conn.execute(f"ALTER TABLE facts ADD COLUMN {name} {decl}")
        for name, decl in FACT_COLUMNS_V6:
            if name not in existing:
                conn.execute(f"ALTER TABLE facts ADD COLUMN {name} {decl}")
    existing_o = _columns(conn, "owners")
    if existing_o:
        for name, decl in OWNER_COLUMNS_V4:
            if name not in existing_o:
                conn.execute(f"ALTER TABLE owners ADD COLUMN {name} {decl}")


# ══════════════════════════════════════════════════════════════════════════
# 锚点（人物）—— 以前四张表建了没人用，这里真正接上
# ══════════════════════════════════════════════════════════════════════════


def ensure_anchor(conn: sqlite3.Connection, bank: str, name: str,
                  aliases: Sequence[str] = ()) -> Optional[int]:
    """按名字拿锚点 id，没有就建。别名一起登记。

    「老爸」「爸爸」「爸」是同一个人的三个叫法 —— 以前各算各的，
    于是检索"老爸"找不到"爸爸"说过的那段。
    """
    name = (name or "").strip()
    if not name:
        return None
    row = conn.execute("SELECT id FROM anchors WHERE bank=? AND name=?",
                       (bank, name)).fetchone()
    if row is not None:
        aid = int(row[0])
    else:
        cur = conn.execute("INSERT INTO anchors(bank,name) VALUES(?,?)",
                           (bank, name))
        aid = int(cur.lastrowid)
    for alias in {name, *[a.strip() for a in aliases if a and a.strip()]}:
        try:
            conn.execute("INSERT OR IGNORE INTO aliases(anchor,alias) VALUES(?,?)",
                         (aid, alias))
        except Exception:
            pass
    return aid


def resolve_anchor(conn: sqlite3.Connection, bank: str, text: str) -> Optional[int]:
    """在一句话里找出提到了哪个已知锚点（最长别名优先）。

    只做本地字符串匹配，不调模型 —— 每轮对话都跑，必须是 O(别名数)。
    """
    text = text or ""
    if not text:
        return None
    rows = conn.execute(
        "SELECT a.anchor, a.alias FROM aliases a JOIN anchors n ON n.id=a.anchor "
        "WHERE n.bank=? ORDER BY LENGTH(a.alias) DESC", (bank,)).fetchall()
    for row in rows:
        alias = row[1]
        if alias and alias in text:
            return int(row[0])
    return None


def link_anchor_chunk(conn: sqlite3.Connection, anchor: Optional[int],
                      chunk: int) -> None:
    if anchor is None:
        return
    try:
        conn.execute("INSERT OR IGNORE INTO anchor_sources(anchor,chunk) VALUES(?,?)",
                     (anchor, chunk))
    except Exception:
        pass


def list_anchors(conn: sqlite3.Connection, bank: str) -> List[Dict[str, Any]]:
    """锚点清单：别名数、挂了多少片段、还有多少条 active 事实。"""
    rows = conn.execute(
        "SELECT n.id, n.name, "
        "(SELECT COUNT(*) FROM aliases a WHERE a.anchor=n.id) AS alias_count, "
        "(SELECT COUNT(*) FROM anchor_sources s WHERE s.anchor=n.id) AS chunks, "
        "(SELECT COUNT(*) FROM facts f WHERE f.bank=n.bank AND f.anchor=n.name "
        " AND f.status='active') AS facts "
        "FROM anchors n WHERE n.bank=? ORDER BY chunks DESC, n.id", (bank,)).fetchall()
    return [{"id": int(r[0]), "name": r[1], "aliases": int(r[2]),
             "chunks": int(r[3]), "facts": int(r[4])} for r in rows]


# ══════════════════════════════════════════════════════════════════════════
# 事实层
# ══════════════════════════════════════════════════════════════════════════

ACTIVE, SUPERSEDED, FORGOTTEN = "active", "superseded", "forgotten"


# ══════════════════════════════════════════════════════════════════════════
# 身份与隔离 —— 记忆库唯一的安全边界
# ══════════════════════════════════════════════════════════════════════════
#
# 她的记忆要分两层看：
#
#   谁在跟我说话（owner）  →  决定"能看见什么"
#   这条事实的级别（visibility）→ 决定"能不能给这个人看"
#
# 两个条件同时成立才能注入。缺任何一个都不行。
#
# ⚠ 最危险的一条：**模型一定会在被追问时试图扩大自己的权限**。
#   所以"能不能升级 visibility"永远不由模型决定，只由主人显式操作。


def make_owner(kind: str, external_id: str, *, platform: str = "wechat") -> str:
    """算 owner 键。`private:<platform>:<id>` 或 `group:<platform>:<id>`。

    ⚠ 只用平台给的稳定 id，绝不用昵称 ——
      昵称会改、会重名、群名片可以随便设。用昵称做 key 等于把
      两个人的记忆混到一起，而且污染不可逆。
    """
    kind = (kind or "").strip().lower()
    if kind not in ("private", "group"):
        raise ValueError("owner kind 只能是 private 或 group")
    eid = (external_id or "").strip()
    if not eid:
        raise ValueError("owner 需要稳定的 external_id")
    plat = (platform or "wechat").strip().lower() or "wechat"
    return f"{kind}:{plat}:{eid}"


def owner_of_private(external_id: str, *, platform: str = "wechat") -> str:
    return make_owner("private", external_id, platform=platform)


def owner_of_group(external_id: str, *, platform: str = "wechat") -> str:
    return make_owner("group", external_id, platform=platform)


def ensure_owner(path: str, bank: str, owner: str, *, display_name: str = "",
                 tier: str = TIER_GUEST, platform: str = "wechat",
                 now: Optional[int] = None) -> Tuple[bool, str]:
    """登记一个 owner（第一次见到这个人时调用）。已存在的只更新 last_seen。

    tier 只由主人/管理员改，这里不提供"自己升级自己"的入口。
    person 列不动 —— 认领是人的动作，不是登记的副作用。
    """
    now = now or _now_ms()
    try:
        conn = connect(path)
    except Exception as exc:
        return False, str(exc)
    try:
        kind = owner.split(":", 1)[0] if ":" in owner else "private"
        with conn:
            conn.execute(
                "INSERT INTO owners(owner,kind,platform,external_id,display_name,tier,"
                "first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(owner) DO UPDATE SET last_seen=excluded.last_seen, "
                "display_name=CASE WHEN excluded.display_name<>'' "
                "THEN excluded.display_name ELSE owners.display_name END",
                (owner, kind, platform, owner.rsplit(":", 1)[-1],
                 (display_name or "")[:80], tier if tier in TIERS else TIER_GUEST,
                 now, now))
        return True, ""
    except Exception as exc:
        return False, str(exc)
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# person —— 跨平台"同一个人"
# ══════════════════════════════════════════════════════════════════════════
#
# 规则只有一条：**认领必须由人做，系统一次都不猜。**
#
# 同一个人在微信是 wxid_xxx、在 B站是 UID 12345，两套 id 在各自平台里
# 就是两个账号，没有任何技术手段能可靠地把它们关联起来 ——
# 昵称会撞会改，头像可以换，让模型判断更是灾难（它一定会说"看起来是同一个人"）。
#
# 所以这里是纯人工操作：主人在界面上点"这两个是同一人"，系统才合。
# 合错的代价（A 看到 B 的记忆）远大于不合的代价（记忆散着）。


def person_of_owner(owner: str) -> str:
    """某个 owner 属于哪个 person。没认领过就返回空串。

    注意：**空串是合法答案**，表示"这个人还没被认领"。
    这不是错误状态 —— 单个平台使用时根本不需要 person 表。
    """
    return ""


def create_person(path: str, display_name: str = "", note: str = "") -> Tuple[bool, str, str]:
    """建一个 person。返回 (ok, person_id, error)。"""
    pid = "p_" + hashlib.sha1(
        f"{display_name}|{_now_ms()}".encode("utf-8")).hexdigest()[:12]
    try:
        conn = connect(path)
    except Exception as exc:
        return False, "", str(exc)
    try:
        now = _now_ms()
        with conn:
            conn.execute(
                "INSERT INTO persons(person,display_name,note,created_at,updated_at) "
                "VALUES(?,?,?,?,?)",
                (pid, (display_name or "")[:80], (note or "")[:400], now, now))
            conn.execute(
                "INSERT INTO person_events(at,kind,person,owner,reason) VALUES(?,?,?,?,?)",
                (now, "create", pid, "", (display_name or "")[:120]))
        return True, pid, ""
    except Exception as exc:
        return False, "", str(exc)
    finally:
        conn.close()


def link_owner(path: str, owner: str, person: str) -> Tuple[bool, str]:
    """把一个平台账号挂到某个 person 上。**这是唯一的合并入口。**

    挂完之后：
      · 该 owner 之前所有的记忆，注入时按 person 汇总（能看到他别的账号的记忆）
      · **已经写下的老事实不搬家** —— facts.person 是写入时记的，保持原样。
        理由见 facts 表注释：记忆归属要稳定，不能因为改绑定就整体迁移。

    如果这个 owner 已经挂在别的 person 上，会先摘下来（一个人只能属于一个人）。
    """
    if not owner or not person:
        return False, "owner / person 不能为空"
    try:
        conn = connect(path)
    except Exception as exc:
        return False, str(exc)
    try:
        with conn:
            if not conn.execute("SELECT 1 FROM persons WHERE person=?",
                                (person,)).fetchone():
                return False, "没有这个 person"
            prev = conn.execute("SELECT person FROM owners WHERE owner=?",
                                (owner,)).fetchone()
            if prev is None:
                return False, "没有这个 owner（还没跟他聊过）"
            conn.execute("UPDATE owners SET person=? WHERE owner=?", (person, owner))
            _now = _now_ms()
            conn.execute(
                "INSERT INTO person_events(at,kind,person,owner,reason) VALUES(?,?,?,?,?)",
                (_now, "link", person, owner,
                 f"from={(prev['person'] or '-')} to={person}"[:200]))
        return True, ""
    except Exception as exc:
        return False, str(exc)
    finally:
        conn.close()


def unlink_owner(path: str, owner: str) -> Tuple[bool, str]:
    """把某个平台账号从 person 上摘下来。记忆不动，只是不再合并展示。"""
    try:
        conn = connect(path)
    except Exception as exc:
        return False, str(exc)
    try:
        with conn:
            row = conn.execute("SELECT person FROM owners WHERE owner=?",
                               (owner,)).fetchone()
            if row is None:
                return False, "没有这个 owner"
            conn.execute("UPDATE owners SET person='' WHERE owner=?", (owner,))
            conn.execute(
                "INSERT INTO person_events(at,kind,person,owner,reason) VALUES(?,?,?,?,?)",
                (_now_ms(), "unlink", row["person"] or "", owner, "manual"))
        return True, ""
    except Exception as exc:
        return False, str(exc)
    finally:
        conn.close()


def list_persons(path: str) -> List[Dict[str, Any]]:
    """列出所有 person 与挂在下面的平台账号。界面用来展示合并关系。"""
    if not path or not os.path.isfile(path):
        return []
    try:
        conn = connect(path, read_only=True)
    except Exception:
        return []
    try:
        persons = conn.execute(
            "SELECT * FROM persons ORDER BY updated_at DESC").fetchall()
        out = []
        for p in persons:
            identities = conn.execute(
                "SELECT owner,platform,display_name,tier FROM owners WHERE person=? "
                "ORDER BY platform, owner", (p["person"],)).fetchall()
            out.append({
                "person": p["person"],
                "name": p["display_name"] or "",
                "note": p["note"] or "",
                "identities": [{
                    "owner": i["owner"], "platform": i["platform"],
                    "name": i["display_name"] or "", "tier": i["tier"],
                } for i in identities],
            })
        # 未认领的账号单独列出，界面才知道"这些还没并"
        loose = conn.execute(
            "SELECT owner,platform,display_name,tier FROM owners "
            "WHERE person IS NULL OR person='' ORDER BY last_seen DESC").fetchall()
        return out + [{
            "person": "", "name": "（未认领）", "note": "",
            "identities": [{
                "owner": i["owner"], "platform": i["platform"],
                "name": i["display_name"] or "", "tier": i["tier"],
            } for i in loose],
        }] if loose else out
    except Exception:
        return []
    finally:
        conn.close()


def owner_persons(path: str, owner: str) -> List[str]:
    """这个 owner 归属于哪些 person 键。

    返回一个**列表**而不是单个值，因为注入时的查询条件是
    `owner IN (...) OR person IN (...)` —— 把"同一个人在所有平台的账号"
    一次性展开，SQL 侧就不用做子查询了。
    """
    if not owner:
        return []
    try:
        conn = connect(path, read_only=True)
    except Exception:
        return [owner]
    try:
        row = conn.execute("SELECT person FROM owners WHERE owner=?",
                           (owner,)).fetchone()
        person = (row["person"] if row else "") or ""
        if not person:
            return [owner]
        rows = conn.execute("SELECT owner FROM owners WHERE person=?",
                            (person,)).fetchall()
        return [r["owner"] for r in rows] or [owner]
    except Exception:
        return [owner]
    finally:
        conn.close()


def set_tier(path: str, owner: str, tier: str) -> Tuple[bool, str]:
    """改一个人的身份等级。**只该由主人操作** —— 界面上属于管理动作。"""
    if tier not in TIERS:
        return False, f"tier 只能是 {TIERS}"
    try:
        conn = connect(path)
    except Exception as exc:
        return False, str(exc)
    try:
        with conn:
            cur = conn.execute("UPDATE owners SET tier=? WHERE owner=?", (tier, owner))
        if not cur.rowcount:
            return False, "没有这个 owner"
        return True, ""
    except Exception as exc:
        return False, str(exc)
    finally:
        conn.close()


def owner_tier(path: str, owner: str, *, default: str = TIER_GUEST) -> str:
    """读一个人的等级。读不到就按最低权限处理。

    ⚠ 默认值必须是 guest 而不是 owner —— 库坏了、行丢了，
      宁可把她当陌生人，也不能当主人。
    """
    try:
        conn = connect(path, read_only=True)
    except Exception:
        return default
    try:
        row = conn.execute("SELECT tier FROM owners WHERE owner=?", (owner,)).fetchone()
        if row is None:
            return default
        return row["tier"] if row["tier"] in TIERS else default
    except Exception:
        return default
    finally:
        conn.close()


def visible_visibilities(tier: str) -> Tuple[str, ...]:
    """这个等级的人**在"别人的内容"里**能看到哪几种级别。

    ⚠ 这里说的是"别的 owner 的东西"。自己 owner 的东西永远全可见，
      由 scope_filter 的第一条保证，跟 tier 无关。

    **主人：全部可见**（v5 改）。理由：数据存在主人自己的电脑上，
    他只是看自己硬盘里的东西，不是"越过安全边界" —— 拦着不让看没有意义，
    真正要拦的是"她会不会把这些说给外人听"，那由 guard 插件和
    topic 白名单管，不在这里管。

    **非主人：只能 shared / public。** private 永远不给。

    ⚠ 这条改动的边界很窄，必须说清楚：
      「主人能看见」≠「她能说出去」。
      看见只影响 UI 展示与提示词注入；说出去由 open_facts_for() 判定，
      两件事分属两个函数，不能混。
    """
    if tier == TIER_OWNER:
        return (VIS_PRIVATE, VIS_SHARED, VIS_PUBLIC)
    return (VIS_SHARED, VIS_PUBLIC)


# ══════════════════════════════════════════════════════════════════════════
# 开放信息 —— 「她能说给外人听什么」
# ══════════════════════════════════════════════════════════════════════════
#
# 和 visible_visibilities 是**两件事**，必须分开：
#
#   visible_visibilities   谁在 UI / 提示词里"看得见"
#   open_facts_for         她"说得出口"，而且是对着非主人说
#
# 一条事实可以同时是"主人看得见"和"她不能对外说" —— 这就是绝大多数事实。
# 反过来不成立：说不出口的东西，本身就得先看得见。


def topic_allowed(topic: str) -> bool:
    """这个话题能不能说给外人听。**白名单以外一律不行。**

    空 topic（没打标签）也返回 False —— 没打标签就是没审过，
    没审过的东西不能外传。这是"宁可不答"的方向。
    """
    t = (topic or "").strip()
    return bool(t) and t in TOPIC_WHITELIST


def open_facts_for(path: str, bank: str, owner: str, *,
                   limit: int = 20) -> List[Dict[str, Any]]:
    """列出某个 owner 名下**允许说给外人听**的事实。

    两个条件同时成立才算：
        ① visibility 是 shared 或 public（主人已经标过"这条可以给外人看"）
        ② topic 在白名单里（这确实是"爱吃什么"这类基础信息）

    这就是「B 问主人爱吃什么 → 她能答 / B 问主人住哪 → 她拒绝」的实现。
    只标了 shared 但话题是"住址"的，照样不出现 —— 因为 ② 挡住了。
    """
    if not (path and os.path.isfile(path) and owner):
        return []
    try:
        conn = connect(path, read_only=True)
    except Exception:
        return []
    try:
        marks = ",".join("?" * len(TOPIC_WHITELIST))
        rows = conn.execute(
            "SELECT * FROM facts WHERE bank=? AND owner=? AND status='active' "
            f"AND visibility IN (?,?) AND topic IN ({marks}) "
            "ORDER BY pinned DESC, confidence DESC, id DESC LIMIT ?",
            (bank, owner, VIS_SHARED, VIS_PUBLIC, *TOPIC_WHITELIST,
             max(1, int(limit)))).fetchall()
        return [_row_to_item(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def scope_filter(owner: str, tier: str,
                 also_owners: Sequence[str] = (),
                 *, strict_owner: bool = False) -> Tuple[str, List[str]]:
    """把 owner + tier 翻成一条 SQL 条件。

    两条规则：

        ① 属于我的：**全部可见**，不管它是什么级别
           —— 别人的私密只对我不可见，我自己的私密我当然看得见。
        ② 不属于我的：按 tier 决定（见 visible_visibilities）

    `also_owners`：同一个人的**其它平台账号**（person 展开的结果）。
    微信里的我看 B站里那个我聊过的东西，靠的就是它。

    `strict_owner=True`：**只**要属于我的，不要 shared 掺进来。
        「查 A 的池」和「以 A 的身份看世界」是两件事：
          · 前者要 strict —— UI 上点开某个人的池，就该只看到他的
          · 后者不要 —— 看世界时，别人的 shared 也算这个世界的一部分

    ⚠ 只传"同一个 person 名下的账号"。**绝不能**把 shared 逻辑和它混起来：
      "我别的账号" 和 "别人的 shared" 是两回事，混了就是越权。
    """
    mine = [owner] + [o for o in (also_owners or ()) if o and o != owner]
    mine_marks = ",".join("?" * len(mine))
    if strict_owner:
        return f"owner IN ({mine_marks})", list(mine)
    vis = visible_visibilities(tier)
    placeholders = ",".join("?" * len(vis))
    clause = f"(owner IN ({mine_marks}) OR visibility IN ({placeholders}))"
    return clause, [*mine, *vis]


@dataclass
class Fact:
    predicate: str
    object: str
    anchor: Optional[str] = None
    confidence: float = 0.6
    source_chunks: List[int] = field(default_factory=list)
    evidence: str = ""
    note: str = ""
    owner: str = ""
    visibility: str = VIS_PRIVATE
    platform: str = "wechat"
    pinned: bool = False
    origin: str = ""
    topic: str = ""

    def __post_init__(self) -> None:
        self.predicate = (self.predicate or "").strip()[:120]
        self.object = (self.object or "").strip()[:1200]
        self.anchor = (self.anchor or "").strip()[:60] or None
        try:
            self.confidence = max(0.0, min(1.0, float(self.confidence)))
        except (TypeError, ValueError):
            self.confidence = 0.6
        self.evidence = (self.evidence or "").strip()[:400]
        self.source_chunks = [int(c) for c in (self.source_chunks or [])][:32]
        # 非法 visibility 直接降回 private —— 失败要往"更安全"的方向倒
        if self.visibility not in VISIBILITIES:
            self.visibility = VIS_PRIVATE
        self.platform = (self.platform or "wechat").strip().lower()[:32]
        self.owner = (self.owner or "").strip()[:200]
        self.pinned = bool(self.pinned)
        # 非法 origin 一律留空 = "不知道在哪说的"。
        # ⚠ 不退回 "private"：那会让"在群里说过"变成"私聊说的"，
        #   是凭空捏造一个更安全的前提，比留空更糟。
        o = (self.origin or "").strip()[:200]
        self.origin = o if (o in ("", "private", "unknown")
                            or o.startswith("group:")) else ""
        # 话题不在已知集合里就留空（= 没打标签 = 不许外传）
        t = (self.topic or "").strip()[:40]
        self.topic = t if t in TOPICS else ""

    @property
    def ok(self) -> bool:
        return bool(self.predicate and self.object and self.object != "null")


def _now_ms() -> int:
    return int(time.time() * 1000)


#: SQL 表达式：拿当前毫秒时间。
#: SQLite 的 unixepoch() 需要 3.38+；老版本退回 julianday 计算。
_NOW_MS_SQL = "CAST(ROUND((julianday('now') - 2440587.5) * 86400000.0) AS INTEGER)"

#: 事实排序用的「取用分数」。SQL 表达式，与 _fresh_score() 同一套公式。
#:
#: 分数 = 置信度 × 衰减 + 使用加成
#:
#:   衰减  0.5 ** (距上次使用天数 / 90)     三个月没用 → 减半
#:   加成  use_count × 0.03                 用得多的浮上来，但有上限
#:
#: ⚠ 两项都用 COALESCE 兜住：老库迁移后这两列是 0，0 的衰减算作 1（全新鲜），
#:   不会因为"从没用过"而全体沉底。这一条很重要 —— 否则升级当天所有老条目
#:   会一起掉到最后，用户会以为记忆全丢了。
_FRESH_ORDER = (
    "(COALESCE(confidence, 0.5) * "
    "CASE WHEN COALESCE(last_used_at,0) = 0 THEN 1.0 ELSE "
    f"POWER(0.5, (({_NOW_MS_SQL} - last_used_at) / 86400000.0) / {FACT_HALF_LIFE_DAYS}) END "
    "+ COALESCE(use_count,0) * 0.03) DESC"
)


def _event(conn: sqlite3.Connection, bank: str, fact_id: Optional[int],
           kind: str, reason: str = "", payload: Optional[dict] = None) -> None:
    conn.execute(
        "INSERT INTO fact_events(bank,fact,kind,at,reason,payload) VALUES(?,?,?,?,?,?)",
        (bank, fact_id, kind, _now_ms(), (reason or "")[:300],
         json.dumps(payload or {}, ensure_ascii=False)[:4000]))


def _find_same(conn: sqlite3.Connection, bank: str, fact: Fact) -> Optional[sqlite3.Row]:
    """同一 owner + 同一主体 + 同一谓词已经有一条 active 的事实，就是"要更新"的那条。

    ⚠ owner 必须参与匹配。以前只看 anchor+predicate，于是
      **A 说"我爸是老师"、B 说"我爸是医生"会互相覆盖** ——
      那是记忆污染，而且是"悄悄把人搞混"那种最难查的污染。

    object 相同 → 重复提及，只涨置信度，不产生新行（幂等）。
    object 不同 → 新认识覆盖旧的：旧行标 superseded，不删。
    """
    base = ("SELECT * FROM facts WHERE bank=? AND status='active' "
            "AND owner=? AND ")
    if fact.anchor:
        return conn.execute(
            base + "anchor=? AND predicate=? ORDER BY id DESC LIMIT 1",
            (bank, fact.owner, fact.anchor, fact.predicate)).fetchone()
    return conn.execute(
        base + "anchor IS NULL AND predicate=? ORDER BY id DESC LIMIT 1",
        (bank, fact.owner, fact.predicate)).fetchone()


_INSERT_FACT = (
    "INSERT INTO facts(bank,anchor,predicate,object,confidence,status,"
    "source_chunks,evidence,created_at,updated_at,note,"
    "owner,visibility,platform,pinned,person,person_at_write,"
    "origin,topic,last_used_at,use_count) "
    "VALUES(?,?,?,?,?,'active',?,?,?,?,?,?,?,?,?,?,?,?,?,0,0)")


def _row_to_item(r: sqlite3.Row) -> Dict[str, Any]:
    """sqlite3.Row → 界面/注入用 dict。

    以前 list_facts 里内联了 20 行这种转换，v5 又要多两个字段；
    收成一个函数，三处调用（list_facts / open_facts_for / 印象页）就不会漂。
    """
    try:
        chunks = json.loads(r["source_chunks"] or "[]")
    except Exception:
        chunks = []
    keys = set(r.keys())
    return {
        "id": int(r["id"]), "anchor": r["anchor"] or "",
        "predicate": r["predicate"], "object": r["object"],
        "confidence": round(float(r["confidence"] or 0), 2),
        "status": r["status"], "supersededBy": r["superseded_by"],
        "createdAt": int(r["created_at"] or 0),
        "updatedAt": int(r["updated_at"] or 0),
        "chunks": chunks, "evidence": r["evidence"] or "",
        "owner": (r["owner"] if "owner" in keys else "") or "",
        "visibility": (r["visibility"] if "visibility" in keys else VIS_PRIVATE),
        "platform": (r["platform"] if "platform" in keys else "wechat"),
        "pinned": bool(r["pinned"]) if "pinned" in keys else False,
        "origin": (r["origin"] if "origin" in keys else "") or "",
        "topic": (r["topic"] if "topic" in keys else "") or "",
        "lastUsedAt": int(r["last_used_at"] or 0) if "last_used_at" in keys else 0,
        "useCount": int(r["use_count"] or 0) if "use_count" in keys else 0,
    }


def _verify_sources(conn: sqlite3.Connection, bank: str,
                    chunk_ids: Sequence[int],
                    allowed_sessions: Optional[Sequence[str]] = None
                    ) -> Tuple[bool, str]:
    """校验事实引用的原始片段**真的存在**，并且属于这个库 / 这个会话。

    这是「任何记忆都要有来源」的执行点。在它之前，"来源"只是一列数字：
    模型写什么就存什么，指一个不存在的 chunk、或者指到别人的会话里，
    都能照单全收 —— 于是它可以凭空造一条事实再给它配一个假来源。

    返回 (ok, error)。chunk 列表为空时返回失败 —— 没有来源的事实不受理。
    """
    ids = [int(c) for c in chunk_ids if str(c).strip()]
    if not ids:
        return False, "没有来源片段"
    marks = ",".join("?" * len(ids))
    params: List[Any] = [bank, *ids]
    sql = (f"SELECT c.id, m.session FROM chunks c "
           f"JOIN messages m ON m.id=c.message "
           f"WHERE m.bank=? AND c.id IN ({marks})")
    if allowed_sessions:
        smarks = ",".join("?" * len(allowed_sessions))
        sql += f" AND m.session IN ({smarks})"
        params += [str(s) for s in allowed_sessions]
    found = {int(r["id"]) for r in conn.execute(sql, params).fetchall()}
    missing = [c for c in ids if c not in found]
    if missing:
        return False, f"来源片段不存在或不属于本次反思：{missing[:4]}"
    return True, ""



def _person_of(conn: sqlite3.Connection, owner: str) -> str:
    """查这个 owner 现在挂在哪个 person 上。查不到返回空串。"""
    if not owner:
        return ""
    try:
        row = conn.execute("SELECT person FROM owners WHERE owner=?",
                           (owner,)).fetchone()
        return (row["person"] if row else "") or ""
    except Exception:
        return ""


def _insert_fact(conn: sqlite3.Connection, bank: str, fact: Fact, now: int) -> int:
    person = _person_of(conn, fact.owner)
    cur = conn.execute(_INSERT_FACT, (
        bank, fact.anchor, fact.predicate, fact.object, fact.confidence,
        json.dumps(fact.source_chunks), fact.evidence, now, now, fact.note,
        fact.owner, fact.visibility, fact.platform, 1 if fact.pinned else 0,
        person, person, fact.origin, fact.topic))
    return int(cur.lastrowid)


def remember(path: str, bank: str, fact: Fact, *,
             verify_sources: bool = True,
             allowed_sessions: Optional[Sequence[str]] = None
             ) -> Tuple[bool, Optional[int], str]:
    """写入一条事实。返回 (ok, fact_id, error)。

    三种结果：
      · **新增**：没有同 owner 同主体同谓词的 active 事实 → INSERT
      · **加固**：有，且 object 完全相同 → 置信度上浮（最多到 0.95），不新增行
      · **纠正**：有，但 object 不同 → 旧行 superseded + superseded_by=新行，
        新行新开一条。**两行都在**，所以「她以前以为是 X」查得到。

    这个三分法是大肥鱼原来完全没有的：原来记错了只能删，删了就什么都不剩。

    ⚠ owner 为空的写入会被拒绝。这不是形式主义 —— 一条不知道属于谁的事实，
      在注入时只能二选一：要么谁都看不到（白记），要么谁都看得到（泄露）。
      所以宁可不收，也不留一条身份不明的事实。

    ⚠ `verify_sources=True`（默认）时，`source_chunks` 必须指向**真实存在、
      且属于本库**的片段，否则整条拒收。这是「任何记忆都要有来源」的执行点：
      在它之前模型可以造一条事实再配一个假来源，而库里无从分辨。

      只有**迁移/修复**这类"事实是从原文倒推来的、源片段另有出处"的场景
      才该传 False，正常写入一律走校验。
    """
    if not fact.ok:
        return False, None, "predicate / object 为空"
    if not fact.owner:
        return False, None, "缺少 owner，拒绝写入（无法判断这条事实属于谁）"
    try:
        conn = connect(path)
    except Exception as exc:
        return False, None, str(exc)
    try:
        now = _now_ms()
        if verify_sources:
            ok_src, err_src = _verify_sources(conn, bank, fact.source_chunks,
                                              allowed_sessions)
            if not ok_src:
                return False, None, err_src
        prev = _find_same(conn, bank, fact)
        with conn:  # 事务
            if prev is not None:
                if (prev["object"] or "") == fact.object:
                    conf = min(0.95, float(prev["confidence"] or 0) + 0.05)
                    # 重复提及时，把新看到的 origin 记进事件 ——
                    # 「这句话他在群里也说过」是有用的事实，不该丢。
                    conn.execute(
                        "UPDATE facts SET confidence=?, updated_at=?, pinned=MAX(pinned,?) "
                        "WHERE id=?",
                        (conf, now, 1 if fact.pinned else 0, prev["id"]))
                    _event(conn, bank, int(prev["id"]), "reinforce",
                           f"重复提及：{fact.predicate}",
                           {"confidence": conf, "origin": fact.origin})
                    return True, int(prev["id"]), ""

                new_id = _insert_fact(conn, bank, fact, now)
                conn.execute(
                    "UPDATE facts SET status=?, superseded_by=?, updated_at=? WHERE id=?",
                    (SUPERSEDED, new_id, now, prev["id"]))
                _event(conn, bank, int(prev["id"]), "correct",
                       f"{prev['object']} → {fact.object}",
                       {"new": new_id, "old_object": prev["object"]})
                return True, new_id, ""

            new_id = _insert_fact(conn, bank, fact, now)
            _event(conn, bank, new_id, "add", fact.predicate,
                   {"object": fact.object, "owner": fact.owner,
                    "visibility": fact.visibility,
                    "origin": fact.origin, "topic": fact.topic})
            return True, new_id, ""
    except Exception as exc:
        return False, None, str(exc)
    finally:
        conn.close()


def remember_both(path: str, bank: str, fact: Fact, *,
                  group_owner: str,
                  group_visibility: str = VIS_SHARED,
                  allowed_sessions: Optional[Sequence[str]] = None
                  ) -> Dict[str, Any]:
    """群聊双写：同一条事实，同时落进「说话人的池」和「群的池」。

    为什么是双份而不是二选一（这是用户定的规则）：
        群里说的话，既是"这个人说过的话"（属于他），
        也是"这个群发生过的事"（属于群）。
        只存一份，必然要丢掉其中一个视角 —— 而两个视角以后都要用：

            A 私聊问「我们上次在群里说的那事」 → 要能从 A 池翻到，
              并且看得出"那是在群里说的"（origin = group:xxx）
            C 群里别人问「这个群之前聊过啥」  → 要能从群池翻到

    ⚠ **群池那份只收"不算私密"的内容。**
      用户原话：「群池只纳入在群里说过、并且不算私密的内容」。
      判定用 visibility 与 topic 两个条件，理由：

        · A 在群里说「我明天去医院」—— 这句话虽然是当着群里说的，
          但它是 A 的私事。写进群池意味着**群池里躺着 A 的健康信息**，
          以后任何人翻群记忆都可能撞见它。
        · 所以：**私密的照常进 A 池（只有 A 和主人看得见），群池不收。**

      具体规则：
        ① A 池那份一定写（visibility 原样）
        ② 群池那份要额外满足 visibility in (shared, public)
           或者 topic 落白名单 —— 两个"公开"信号有一个就够，
           因为主人标 shared 已经是显式授权，不需再打标签。
    ⚠ 两次写入共用一个 `source_chunks` 校验，只校验一次，省一次查询。
    """
    out: Dict[str, Any] = {"ok": False, "personal": None, "group": None,
                           "errors": []}
    # ① 说话人自己的池 —— 永远写
    ok1, fid1, err1 = remember(path, bank, fact,
                               verify_sources=True,
                               allowed_sessions=allowed_sessions)
    out["ok"] = bool(ok1)
    out["personal"] = fid1
    if not ok1:
        out["errors"].append(f"个人池：{err1}")
        return out          # 个人池都没写进去，群池不必写
    # ② 群池 —— 只收"不算私密"的
    publicish = (fact.visibility in (VIS_SHARED, VIS_PUBLIC)
                 or topic_allowed(fact.topic))
    if not publicish:
        out["skipped_group"] = "私密内容不写入群池"
        return out
    g = Fact(
        predicate=fact.predicate, object=fact.object, anchor=fact.anchor,
        confidence=fact.confidence, source_chunks=list(fact.source_chunks),
        evidence=fact.evidence, note=fact.note, owner=group_owner,
        visibility=group_visibility, platform=fact.platform,
        pinned=False,               # 置顶是"我的池里别被挤掉"，群池不继承
        origin=fact.origin, topic=fact.topic,
    )
    ok2, fid2, err2 = remember(path, bank, g,
                               verify_sources=True,
                               allowed_sessions=allowed_sessions)
    out["group"] = fid2
    if not ok2:
        out["errors"].append(f"群池：{err2}")
    return out


def forget_fact(path: str, bank: str, fact_id: int, reason: str = "") -> Tuple[bool, str]:
    """忘掉一条事实。**不删行**，只标 forgotten 并记事件。

    为什么保留：她说「我不记得这个了」和数据库里从来没出现过，
    是两件事。前者以后能被问「你是不是忘了什么」查到。
    """
    try:
        conn = connect(path)
    except Exception as exc:
        return False, str(exc)
    try:
        with conn:
            row = conn.execute("SELECT * FROM facts WHERE id=? AND bank=?",
                               (fact_id, bank)).fetchone()
            if row is None:
                return False, "没有这条事实"
            conn.execute("UPDATE facts SET status=?, updated_at=? WHERE id=?",
                         (FORGOTTEN, _now_ms(), fact_id))
            _event(conn, bank, fact_id, "forget", reason, {"predicate": row["predicate"]})
        return True, ""
    except Exception as exc:
        return False, str(exc)
    finally:
        conn.close()


def list_facts(path: str, bank: str, *, status: str = "", anchor: str = "",
               query: str = "", limit: int = 100, offset: int = 0,
               owner: str = "", tier: str = "",
               also_owners: Sequence[str] = (),
               origin: str = "", topic: str = "",
               strict: bool = False) -> Dict[str, Any]:
    """列/搜事实。

    两种调用方式：
      · **管理视角**（owner 与 tier 都为空）：看得见全部，UI 的管理页用。
      · **说话视角**（传了 owner 和 tier）：只返回这个人有权看到的事实，
        走 scope_filter() 那条唯一的隔离判断。
        `also_owners` 传同一个人的其它平台账号，实现跨平台记忆汇总。

    `strict=True`（v5）：**只**要 owner 名下的，不要别人的 shared 掺进来。
        UI 上点开某个人的池时用它 —— "看他的池"和"以他的身份看世界"
        是两件事，前者不该夹带别人的东西。

    `origin` / `topic`（v5）：精确过滤。"group:xxx" 取某个群里说过的话；
    话题给一个值就只出那一类。两个都空 = 不按这两项过滤。

    ⚠ 管理视角只能给 UI 用。任何注入到提示词的路径**必须**传 owner+tier，
      否则等于把所有人的私密内容摆到台面上。
    """
    if not path or not os.path.isfile(path):
        return {"items": [], "total": 0}
    where, params = ["bank = ?"], [bank]
    if owner and tier:
        clause_extra, extra_params = scope_filter(owner, tier, also_owners,
                                                  strict_owner=strict)
        where.append(clause_extra)
        params += extra_params
    if status in (ACTIVE, SUPERSEDED, FORGOTTEN):
        where.append("status = ?")
        params.append(status)
    if anchor:
        where.append("anchor = ?")
        params.append(anchor)
    if origin:
        where.append("origin = ?")
        params.append(origin)
    if topic:
        where.append("topic = ?")
        params.append(topic)
    if (query or "").strip():
        where.append("(predicate LIKE ? OR object LIKE ? OR evidence LIKE ?)")
        q = f"%{query.strip()}%"
        params += [q, q, q]
    clause = " WHERE " + " AND ".join(where)
    try:
        conn = connect(path, read_only=True)
    except Exception:
        try:
            conn = connect(path)
        except Exception as exc:
            return {"items": [], "total": 0, "error": str(exc)}
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM facts{clause}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM facts{clause} ORDER BY "
            "pinned DESC, CASE status WHEN 'active' THEN 0 ELSE 1 END, "
            # v6：界面列表用「取用分数」，不用 id 倒序。
            #   ⚠ 这个排序只是**给人看的顺序**，界面本来就要能看到全部，
            #     所以这里不改任何可见范围，只改先看到谁。
            + _FRESH_ORDER + " "
            "LIMIT ? OFFSET ?",
            params + [max(1, int(limit)), max(0, int(offset))]).fetchall()
        items = [_row_to_item(r) for r in rows]
        return {"items": items, "total": int(total)}
    except Exception as exc:
        return {"items": [], "total": 0, "error": str(exc)}
    finally:
        conn.close()


def fact_events(path: str, bank: str, limit: int = 60) -> List[Dict[str, Any]]:
    if not path or not os.path.isfile(path):
        return []
    try:
        conn = connect(path, read_only=True)
    except Exception:
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM fact_events WHERE bank=? ORDER BY id DESC LIMIT ?",
            (bank, max(1, int(limit)))).fetchall()
        return [{"id": int(r["id"]), "fact": r["fact"], "kind": r["kind"],
                 "at": int(r["at"] or 0), "reason": r["reason"] or ""} for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# 检索：词法 + 向量 + MMR 融合
# ══════════════════════════════════════════════════════════════════════════


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _mmr(cands: List[Dict[str, Any]], vectors: List[Optional[Sequence[float]]],
         k: int, lambda_: float = 0.72) -> List[Dict[str, Any]]:
    """最大边际相关性：既相关又不重复。

    以前按分数顺序截断，于是同一段对话的三个切片会一起挤进注入预算 ——
    模型看到三遍"今天加班到十点"，不如看到一次加班 + 一次别的事。
    """
    if k <= 0 or not cands:
        return []
    picked: List[int] = []
    remaining = list(range(len(cands)))
    while remaining and len(picked) < k:
        best, best_score = None, -1e9
        for i in remaining:
            rel = float(cands[i].get("score") or 0)
            if vectors and vectors[i] is not None:
                div = max((_cosine(vectors[i], vectors[j])
                           for j in picked if vectors[j] is not None), default=0.0)
            else:
                div = 0.0
            s = lambda_ * rel - (1 - lambda_) * div
            if s > best_score:
                best, best_score = i, s
        if best is None:
            break
        picked.append(best)
        remaining.remove(best)
    return [cands[i] for i in picked]


class MemoryUnavailable(RuntimeError):
    """记忆库打不开 / 结构坏了。**这是降级信号**，不是「没命中」。

    调用方拿到它应该走 L2（最近原文）或 L3（空记忆 + 界面标记），
    而不是把它当成"她确实想不起来"。
    """


def _search_chunks_impl(path: str, bank: str, query: str, *, limit: int = 8,
                        max_chars: int = 2400,
                        session: str = "") -> List[Dict[str, Any]]:
    """原文检索：FTS5(bm25) 排序 → MMR 去冗余 → 按分数填满预算。

    返回 [{chunk, message, role, text, time, score}]。

    与旧实现的差别：
      1. 查询先过分词再进 MATCH（旧实现整句匹配，中文一条都命不中）；
      2. 用 bm25() 排序而不是插入顺序；
      3. 去冗余而不是硬截断；
      4. 只读打开，不拷库。

    ⚠ 库打不开时抛 `MemoryUnavailable`（给降级用）；"没命中"仍然返回 []。

    ⚠ `session` 是**谁在说话**（`wechat:<chat_id>`），不是可选的优化项。
      以前这里的 SQL 只有 `m.bank = ?`，而 bank 恒为 "dafeiyu" ——
      于是「你记得的事」这一段是全库共享的：跟 B 聊天时能检索到 A 的
      私聊原话，降级到最近原文时更是直接取全库最新 N 条。
      README 写着"你不会看到别人的私密内容"，facts 层做到了，原文层没有。
      传空串时保持旧的全库行为，是为了不破坏 CLI 与自检的既有用法。
    """
    if not path or not os.path.isfile(path):
        raise MemoryUnavailable(f"记忆库不存在：{path or '(空路径)'}")
    expr = fts_query(query)
    if not expr:
        return []
    conn = None
    try:
        conn = connect(path, read_only=True)
    except Exception as exc:
        try:
            conn = connect(path)
        except Exception:
            raise MemoryUnavailable(f"记忆库打不开：{exc}") from exc
    try:
        where = "search_index MATCH ? AND m.bank = ?"
        params: List[Any] = [expr, bank]
        if session:
            where += " AND m.session = ?"
            params.append(session)
        params.append(max(20, int(limit) * 6))
        rows = conn.execute(
            "SELECT s.rowid AS chunk, bm25(search_index) AS rank "
            "FROM search_index s JOIN chunks c ON c.id = s.rowid "
            "JOIN messages m ON m.id = c.message "
            f"WHERE {where} "
            "ORDER BY rank LIMIT ?",
            params).fetchall()
        if not rows:
            return []
        idmap = {int(r["chunk"]): float(r["rank"]) for r in rows}
        ids = list(idmap)
        marks = ",".join("?" * len(ids))
        info = conn.execute(
            f"SELECT c.id, c.text, c.message, m.role, "
            f"COALESCE(m.source_time, m.recorded_at) AS t "
            f"FROM chunks c JOIN messages m ON m.id=c.message "
            f"WHERE c.id IN ({marks})", ids).fetchall()
        # bm25 越小越相关（负数），翻成 0..1 的分数
        worst = max(idmap.values()) if idmap else 0.0
        best = min(idmap.values()) if idmap else 0.0
        span = (worst - best) or 1.0
        cands = []
        for r in info:
            raw = idmap.get(int(r["id"]), worst)
            score = 1.0 - (raw - best) / span
            cands.append({"chunk": int(r["id"]), "message": int(r["message"]),
                          "role": r["role"], "text": r["text"], "time": int(r["t"] or 0),
                          "score": round(max(0.0, min(1.0, score)), 4)})
        cands.sort(key=lambda x: -x["score"])
        # ── v6：把"以前帮上过忙"也算进去 ──
        #
        # bm25 只看词频，不看这段对话历史上有没有真的被用上过。
        # 同一段被问三次都命中的片段，比偶然命中一次的更该浮上来 ——
        # 尤其在这个"存了几十万条"的场景里，先验知识比单次匹配更可靠。
        #
        # 权重很小（最多 +0.15），只在分数接近时起作用。
        # 调大就会出现"越用越用"的正反馈：老片段霸榜，新内容永远进不去。
        hitmap = {}
        try:
            for r in conn.execute(
                    f"SELECT chunk, hits FROM chunk_hits WHERE chunk IN ({marks})",
                    ids).fetchall():
                hitmap[int(r["chunk"])] = int(r["hits"] or 0)
        except Exception:
            hitmap = {}
        if hitmap:
            top = max(hitmap.values()) or 1
            for c in cands:
                h = hitmap.get(c["chunk"], 0)
                if h:
                    c["score"] = round(min(1.0, c["score"] + 0.15 * (h / top)), 4)
            cands.sort(key=lambda x: -x["score"])
        picked = _mmr(cands, [None] * len(cands), len(cands))
        out, used = [], 0
        for c in picked:
            if used + len(c["text"]) > max_chars and out:
                break
            out.append(c)
            used += len(c["text"])
        # 记一次命中（只记真的进了返回集的那几条）
        try:
            _bump_chunk_hits(conn, [c["chunk"] for c in out])
        except Exception:
            pass
        return out[:limit]
    except MemoryUnavailable:
        raise
    except Exception as exc:
        raise MemoryUnavailable(f"检索失败：{exc}") from exc
    finally:
        if conn is not None:
            conn.close()


def search_chunks(path: str, bank: str, query: str, *, limit: int = 8,
                  max_chars: int = 2400, session: str = "") -> List[Dict[str, Any]]:
    """兼容入口：任何失败都返回 []。

    老调用点（界面自检、CLI）用它，不需要处理异常；
    需要"区分降级和没命中"的地方用 `search_with_fallback`。
    """
    try:
        return _search_chunks_impl(path, bank, query, limit=limit,
                                   max_chars=max_chars, session=session)
    except Exception:
        return []


def recent_chunks(path: str, bank: str, limit: int = 20,
                  session: str = "") -> List[Dict[str, Any]]:
    """L2 兜底：不查 FTS，直接取最近 N 条原文。

    场景：FTS 索引坏了 / 查询词一个都没命中时，她至少还知道最近聊过什么。
    返回结构与 `search_chunks` 一致，score 按时间递减（最新 1.0）。
    """
    if not path or not os.path.isfile(path):
        raise MemoryUnavailable(f"记忆库不存在：{path or '(空路径)'}")
    conn = None
    try:
        conn = connect(path, read_only=True)
        rows = conn.execute(
            "SELECT c.id AS chunk, c.text, c.message, m.role, "
            "COALESCE(m.source_time, m.recorded_at) AS t "
            "FROM chunks c JOIN messages m ON m.id = c.message "
            "WHERE m.bank = ?"
            + (" AND m.session = ?" if session else "")
            + " ORDER BY t DESC LIMIT ?",
            ((bank, session, max(1, int(limit))) if session
             else (bank, max(1, int(limit))))).fetchall()
        out = []
        n = len(rows) or 1
        for i, r in enumerate(rows):
            out.append({
                "chunk": int(r["chunk"]), "message": int(r["message"]),
                "role": r["role"], "text": r["text"],
                "time": int(r["t"] or 0),
                "score": round(max(0.0, 1.0 - i / n), 4),
            })
        return out
    except MemoryUnavailable:
        raise
    except Exception as exc:
        raise MemoryUnavailable(f"读取最近原文失败：{exc}") from exc
    finally:
        if conn is not None:
            conn.close()


def search_with_fallback(path: str, bank: str, query: str, *,
                         limit: int = 8, max_chars: int = 2400,
                         session: str = ""
                         ) -> Tuple[List[Dict[str, Any]], str]:
    """带降级的检索：返回 (items, level)。

    level:
      · "ok"      正常（含"确实没命中"——那不是降级）
      · "recent"  检索失败，退回最近 N 条原文
      · "empty"   记忆库打不开，只能空记忆

    ⚠ 降级也要守 `session`：`recent_chunks` 是全库取最新 N 条，
      不传 session 的话"兜底"会比正常检索更容易串人。
    """
    try:
        items = _search_chunks_impl(path, bank, query, limit=limit,
                                    max_chars=max_chars, session=session)
        return items, "ok"
    except Exception:
        pass
    try:
        return recent_chunks(path, bank, limit=max(5, min(limit, 20)),
                             session=session), "recent"
    except Exception:
        return [], "empty"


def _bump_chunk_hits(conn: sqlite3.Connection, chunk_ids: Sequence[int]) -> None:
    """给一批片段记一次命中。

    ⚠ 只记**真正进入了返回结果**的片段，不记候选池。
      记候选会让"被搜到过"和"被用上过"混为一谈 —— 而这两件事的区别，
      正是这套权重存在的理由。
    """
    ids = [int(c) for c in chunk_ids if str(c).strip()]
    if not ids:
        return
    now = _now_ms()
    for cid in ids:
        try:
            conn.execute(
                "INSERT INTO chunk_hits(chunk,hits,last_hit) VALUES(?,1,?) "
                "ON CONFLICT(chunk) DO UPDATE SET hits=hits+1, last_hit=excluded.last_hit",
                (cid, now))
        except Exception:
            pass
    try:
        conn.commit()
    except Exception:
        pass


def render_recall(items: List[Dict[str, Any]]) -> str:
    """把检索结果渲染成注入文本 —— 与 bridge_client.render_memory 同一个格式。

    `- 我/对方：<文本>`，一行一条。格式变了两边就不一致（插件那边也是这么约定的）。
    """
    lines = []
    for it in items:
        who = "我" if it.get("role") == "assistant" else "对方"
        text = str(it.get("text") or "").replace("\n", " ")[:160]
        if text:
            lines.append(f"- {who}：{text}")
    return "\n".join(lines)


def recall_text(path: str, bank: str, query: str, *, max_chars: int = 2400,
                session: str = "") -> str:
    """检索并渲染。失败返回空串，绝不抛 —— 记忆坏了不能拖垮聊天。"""
    try:
        return render_recall(search_chunks(path, bank, query,
                                          max_chars=max_chars, session=session))
    except Exception:
        return ""


def facts_for_prompt(path: str, bank: str, *, owner: str, tier: str,
                     limit: int = 8, pinned_limit: int = 4,
                     question: str = "") -> str:
    """把「她认为什么」渲染成注入段。这是原文检索之外的第二条腿。

    原文给她"你说过什么"，事实给她"我知道你是谁、你偏好什么"。

    ⚠ 这是**最容易泄露的入口**，所以规则写死在这里，不给调用方留余地：

      1. owner 和 tier 是**必填**。没有它们就不知道"能看什么"，
         直接返回空串 —— 宁可这次不注入，也不能瞎注入。
      2. 置顶单独走一遍，**独立预算**。置顶的事实不该被普通检索挤掉：
         "你叫她爸爸"和"今天下雨"不该抢同一个名额。
      3. **注入永远是"这个人 + 公共"两条腿，主人不例外。**

         这条和 list_facts 的"主人全可见"**不是一回事**，必须分开：
             list_facts(tier=owner)  → UI 上主人能翻到全部（数据在他硬盘上）
             facts_for_prompt(...)   → 但**不会**把别人的私密塞进提示词

         为什么主人也不许注入别人的私密：
             她跟 A 说话时，如果 B 的私密在提示词里，她就有机会说漏。
             "看得见"是主人的权利，"说得出"是她的行为 —— 后者不看 tier，
             只看这条事实是不是冲着"双方共同知道"去的。
             所以即便 A 是主人，注入的仍是：A 自己的全部 + 所有人的开放信息。
      4. **跨平台**：她自己在这里查出这个人的所有平台账号
         （owner_persons），一起交给 scope_filter。调用方不需要知道
         person 表的存在 —— 少一个参数就少一处漏。
      5. `question`（v5）：本轮对方说了什么。目前只用于**注入侧不提无关的开放信息**
         —— 对方没问到主人时，不主动把主人的爱好塞进去（那反而会让她
         在闲聊里突然提一句"我主人爱吃烧烤"，很不自然）。
         传空串 = 不过滤（旧行为）。
    """
    if not owner or not tier:
        return ""
    # 这个人在所有平台的账号。没认领时就是他自己一个。
    siblings = [o for o in owner_persons(path, owner) if o and o != owner]
    mine = {owner, *siblings}

    # ① 只取"属于这个人的" —— 用 owner 精确过滤，不用 tier。
    #    tier 在这里唯一的作用是**决定要不要额外补开放信息**，
    #    它不能扩大可见范围。写成 list_facts(tier=tier) 的话，
    #    主人 tier 会把全库都捞进来 —— 那正是"跟 A 说话却带着 B 的秘密"。
    #
    # ② v6 取用策略：候选池放大到 limit 的 4 倍，再按"取用分数"（置信度 ×
    #    时间衰减 + 使用次数）排序，最后用本轮问题挑出真正相关的。
    #
    #    为什么不能直接 list_facts(limit=N)：
    #      以前 SQL 是 `ORDER BY pinned DESC, id DESC` —— 最新的在最前。
    #      三年后你有 5000 条事实，每轮只看得到 id 最大的 8 条，
    #      三年前那条"你拔过智齿"永远排在最后，永远进不来。
    #      那不是"记得但想不起来"，那是**排序把它埋了**。
    #
    #    现在：先按"多久没被用到"排（久不用的降权，但不是删除），
    #         再按相关性筛（本轮问的是牙，就优先牙相关的那几条）。
    cand_n = max(24, (limit + pinned_limit) * 4)
    pinned = list_facts(path, bank, status=ACTIVE, owner=owner,
                        tier=TIER_OWNER,          # 只为让 scope_filter 放行 owner 自己
                        also_owners=siblings,
                        limit=cand_n).get("items") or []
    # 上面用 TIER_OWNER 只是为了让 scope_filter 的"我自己的全可见"生效；
    # 结果集里仍然可能夹带 visibility=shared 的别人的东西，
    # 所以下面还要按 owner 再收一刀。
    pinned = [i for i in pinned if i["owner"] in mine]

    # ③ 从候选里挑：置顶必进 → 与问题相关的优先 → 剩下的按取用分数补足
    picked = _pick_facts(pinned, question, limit=limit, pinned_limit=pinned_limit)

    # ④ 追加：全库的开放信息。
    #    B 问「你主人爱吃啥」，靠的就是这一段 —— 开放信息存在主人名下，
    #    但此刻跟她说话的是 B，所以要按"公共资料"的身份补进来。
    have = {i["id"] for i in picked}
    extra = []
    for o in _all_owners(path):
        for it in open_facts_for(path, bank, o, limit=200):
            if it["id"] not in have:
                extra.append(it)
    extra.sort(key=lambda x: -float(x.get("confidence") or 0))
    extra = extra[:4]          # 开放信息是补充，不该挤掉"我自己的事"

    seen, lines = set(), []
    for item in picked + extra:
        fid = item.get("id")
        if fid in seen:
            continue
        seen.add(fid)
        if len(lines) >= limit + pinned_limit + len(extra):
            break
        # "我"= 这个人自己的（任何一个平台账号），"别人"= 别的 owner 分享出来的
        who = item["anchor"] or ("我" if item["owner"] in mine else "别人")
        conf = item["confidence"]
        mark = "" if conf >= 0.7 else "（不确定）"
        star = "★" if item.get("pinned") else ""
        # 群里说过的事标出来 —— 她得知道"这话是在场合里说的"，
        # 否则会把群聊里的公开讨论当成私聊秘密讲出去。
        where = ""
        o = item.get("origin") or ""
        if o.startswith("group:"):
            where = "（群里说的）"
        # 开放信息标出来 —— 让模型知道"这条是对外可以说"的，
        # 而不是自己猜哪条能说。判定已经做完了，这里只是标注。
        shared_mark = ""
        if item["owner"] not in mine and item.get("visibility") in (VIS_SHARED, VIS_PUBLIC):
            shared_mark = "（可以告诉别人）"
        lines.append(f"- {star}{who}｜{item['predicate']}：{item['object']}"
                     f"{mark}{where}{shared_mark}")

    # ⑤ 记一次"用上了"。
    #    注意记的是**真的进了提示词**的那几条，不是候选池 ——
    #    候选里没被选中的那些，这一轮没起作用，不该加分。
    #    失败不影响这一轮：记不上最多是下次排序差一点，不能让聊天失败。
    try:
        mark_used(path, bank, [i["id"] for i in picked] + [i["id"] for i in extra])
    except Exception:
        pass
    return "\n".join(lines)


def _fact_score(item: Dict[str, Any], now: int) -> float:
    """一条事实的「取用分数」。与 SQL 里的 _FRESH_ORDER 同一套公式。

    ⚠ 两处必须一致。改一处不改另一处，界面看到的顺序会和注入用的顺序对不上 ——
      那种不一致最难查，因为两边看起来都"正常"。
    """
    conf = float(item.get("confidence") or 0.5)
    used = int(item.get("lastUsedAt") or 0)
    if used <= 0:
        decay = 1.0                      # 从没用过 = 不算陈旧（见 _FRESH_ORDER 注释）
    else:
        days = max(0.0, (now - used) / 86_400_000.0)
        decay = 0.5 ** (days / FACT_HALF_DAYS_EFFECTIVE)
    return conf * decay + int(item.get("useCount") or 0) * 0.03


#: _fact_score 里的半衰期。与 SQL 常数同一来源，只是这边要 float。
FACT_HALF_DAYS_EFFECTIVE = FACT_HALF_LIFE_DAYS


def _relevance(item: Dict[str, Any], question: str) -> int:
    """这条事实和本轮问题有多相关。0 = 完全不相干。

    只用本地词表匹配，不调模型 —— 每轮都跑，必须是 O(关键词数)。
    用 memory.terms() 切词（与检索同一套），避免"牙疼"和"牙"切不出关系。
    """
    q = (question or "").strip()
    if not q:
        return 0
    qterms = set(terms(q))
    if not qterms:
        return 0
    body = f"{item.get('predicate') or ''} {item.get('object') or ''}"
    hit = qterms & set(terms(body))
    return len(hit)


def _pick_facts(items: List[Dict[str, Any]], question: str, *,
                limit: int, pinned_limit: int) -> List[Dict[str, Any]]:
    """从候选里挑出这一轮该注入的事实。

    三段式，顺序固定：

        ① 置顶的**必进**（最多 pinned_limit 条）
           —— 你手动钉住的东西，不该被任何算法挤掉。
        ② 和本轮问题相关的，按相关度 + 取用分数排
           —— 你说"我牙疼"，三年前"拔过智齿"该浮上来。
        ③ 剩下的位置，按取用分数补足
           —— 保持她"平时知道些什么"的连续性，不是每轮都换一批全新的。

    ⚠ 相关性只在**有 question 时**才算。私聊闲聊问"在吗"时 qterms 很少，
      ② 基本是空，于是全走 ③ —— 行为和以前一样，不会突然变。
    """
    now = _now_ms()
    pinned = [i for i in items if i.get("pinned")][:max(0, int(pinned_limit))]
    rest = [i for i in items if not i.get("pinned")]
    # 取用分数：相关性当主序，分数当次序。
    # 相关性只在"这一条真的和问题有交集"时才有意义，为 0 的不参与 ②。
    scored = []
    for i in rest:
        rel = _relevance(i, question)
        score = _fact_score(i, now)
        scored.append((rel, score, i))
    related = [(r, s, i) for r, s, i in scored if r > 0]
    related.sort(key=lambda x: (-x[0], -x[1]))
    others = [(r, s, i) for r, s, i in scored if r == 0]
    others.sort(key=lambda x: -x[1])

    out: List[Dict[str, Any]] = list(pinned)
    have = {i["id"] for i in out}
    for _r, _s, i in related + others:
        if len(out) >= max(1, int(limit)) + len(pinned):
            break
        if i["id"] in have:
            continue
        have.add(i["id"])
        out.append(i)
    return out


def mark_used(path: str, bank: str, fact_ids: Sequence[int]) -> int:
    """把一批事实标记为"这一轮用上了"：last_used_at=now, use_count+1。

    为什么单独一个函数、还要允许失败：
      这是**排序的输入**，不是事实本身。记不上最多是下次排序差一点，
      绝不能让"记这一次使用"把整轮对话搞崩。
    """
    ids = [int(x) for x in fact_ids if str(x).strip()]
    if not ids:
        return 0
    try:
        conn = connect(path)
    except Exception:
        return 0
    try:
        now = _now_ms()
        marks = ",".join("?" * len(ids))
        with conn:
            conn.execute(
                f"UPDATE facts SET last_used_at=?, use_count=COALESCE(use_count,0)+1 "
                f"WHERE bank=? AND id IN ({marks})",
                (now, bank, *ids))
        return len(ids)
    except Exception:
        return 0
    finally:
        conn.close()


def _all_owners(path: str) -> List[str]:
    """库里所有 owner。用于「开放信息」的全库扫描。

    ⚠ 从 **facts 表**取，不从 owners 表取。
      以前这里查 owners，于是"写过事实但还没登记 owner"的人
      （reflect 直接 remember、没走 ensure_owner 的路径）整个失踪，
      表现为"标了共享但对方看不到" —— 查起来像权限问题，其实是漏人。

    facts 里出现过的 owner 才是真正常识意义上的"有过记忆的人"，
    owners 表只是它的一个索引/补充（存 tier、display_name）。
    """
    try:
        conn = connect(path, read_only=True)
    except Exception:
        return []
    try:
        rows = conn.execute(
            "SELECT DISTINCT owner FROM facts WHERE owner <> ''").fetchall()
        out = [r["owner"] for r in rows]
        try:
            out += [r["owner"] for r in
                    conn.execute("SELECT owner FROM owners").fetchall()]
        except Exception:
            pass
        return list(dict.fromkeys(out))
    except Exception:
        return []
    finally:
        conn.close()


def owner_said_about(path: str, bank: str, answerer_owner: str,
                     about_owner: str, question: str,
                     limit: int = 6) -> str:
    """「对方在问第三个人」时，把**那个第三人**的开放信息渲染出来。

    典型场景：B 问「你主人喜欢吃什么呀」。
    此时 B 面前是 `answerer_owner`（B 自己的池），但要回答的是关于
    `about_owner`（主人 A）的事。

    ⚠ 只给开放信息，而且**只给白名单话题**。
      不问「住哪」时她拿不到「住址」，问了也拿不到 —— 判定在 open_facts_for，
      不靠提示词自觉。
    """
    if not (about_owner and about_owner != answerer_owner):
        return ""
    items = open_facts_for(path, bank, about_owner, limit=max(1, int(limit)))
    if not items:
        return ""
    lines = []
    for it in items:
        lines.append(f"- {it['predicate']}：{it['object']}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════
# 记忆池总览 + 角色印象
# ══════════════════════════════════════════════════════════════════════════


def pool_summary(path: str, bank: str, *, main_owner: str = "") -> Dict[str, Any]:
    """各个记忆池的**数量**，不含任何内容。

    UI 的「其他记忆池」副页只拿这个 —— 列出池名和条数，
    一条内容都不返回。理由：别人的池本来就不该在主人没主动点进去时
    被铺开在屏幕上；而且这个页面现在只是占位。

    返回：
        {"pools": [{"owner","name","kind","facts","messages","isMain"}], ...}
    """
    out: Dict[str, Any] = {"pools": [], "mainOwner": main_owner, "total": 0}
    if not (path and os.path.isfile(path)):
        return out
    try:
        conn = connect(path, read_only=True)
    except Exception:
        return out
    try:
        # ⚠ 这里的"原文条数"要按 owner 直接匹配，不能靠 owners.external_id ——
        #   老库里 external_id 可能是空的（ensure_owner 早期不写它），
        #   用 session=external_id 匹配会一律得 0，看着像"这个池没聊过"。
        #   改成用 owner 的后缀（:wxid_xxx）去撞 messages.session。
        rows = conn.execute(
            "SELECT o.owner, o.kind, o.platform, o.display_name, o.tier, "
            "(SELECT COUNT(*) FROM facts f WHERE f.bank=? AND f.owner=o.owner "
            " AND f.status='active') AS facts "
            "FROM owners o ORDER BY facts DESC", (bank,)).fetchall()
        # 再单独扫一遍 messages，按 owner 后缀归计数
        msg_by_owner: Dict[str, int] = {}
        try:
            for r in conn.execute(
                    "SELECT session, COUNT(*) AS n FROM messages WHERE bank=? "
                    "GROUP BY session", (bank,)).fetchall():
                sess = str(r["session"] or "")
                for prefix in ("wechat:",):
                    if sess.startswith(prefix):
                        key = owner_of_private(sess[len(prefix):])
                        msg_by_owner[key] = msg_by_owner.get(key, 0) + int(r["n"])
                key_g = owner_of_group(sess)
                msg_by_owner[key_g] = msg_by_owner.get(key_g, 0) + int(r["n"])
        except Exception:
            msg_by_owner = {}
        pools = []
        for r in rows:
            # 名字兜底顺序：display_name → owner 的最后一段 → 整个 owner
            # ⚠ 老库里 display_name 可能是空的、external_id 也没写，
            #   直接拿 owner.split 会在 "private:wechat:" 这种残缺键上切出空串，
            #   界面上就显示成 "private:wechat"。所以最后还要再兜一层。
            tail = (r["owner"] or "").rsplit(":", 1)[-1].strip()
            name = r["display_name"] or tail or (r["owner"] or "（未知）")
            pools.append({
                "owner": r["owner"],
                "kind": r["kind"],
                "platform": r["platform"],
                "name": name,
                "tier": r["tier"],
                "facts": int(r["facts"] or 0),
                "messages": int(msg_by_owner.get(r["owner"], 0)),
                "isMain": r["owner"] == main_owner,
            })
        # 有记忆但 owners 表里还没有的（reflect 直接写过、没走 ensure_owner）
        known = {p["owner"] for p in pools}
        for ow in _all_owners(path):
            if ow in known:
                continue
            cnt = 0
            try:
                cnt = conn.execute(
                    "SELECT COUNT(*) FROM facts WHERE bank=? AND owner=? "
                    "AND status='active'", (bank, ow)).fetchone()[0]
            except Exception:
                pass
            kind = "group" if ow.startswith("group:") else "private"
            pools.append({
                "owner": ow, "kind": kind, "platform": "wechat",
                "name": ow.rsplit(":", 1)[-1], "tier": TIER_GUEST,
                "facts": int(cnt), "messages": int(msg_by_owner.get(ow, 0)),
                "isMain": ow == main_owner,
            })
        pools.sort(key=lambda p: (0 if p["isMain"] else 1, -p["facts"]))
        out["pools"] = pools
        out["total"] = len(pools)
        return out
    except Exception:
        return out
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# 画像：她自己归纳的「这个人是什么样」
# ══════════════════════════════════════════════════════════════════════════
#
# 为什么需要这一层（这是 v6 的核心）：
#
#   事实层是**逐条**的。"爱吃辣""养了只猫""做设计"分开存。聊得越久越多，
#   几千条之后每轮只进得去十几条 —— 剩下的等于不存在。
#
#   画像是**一段话**，900 字左右，把几百条事实压成"能整段带进提示词"的量。
#   于是她每轮都完整地知道自己面前这个人是谁，而不是随机抽十几条片段。
#
# 这和"压缩"不是一回事：
#   压缩 = 丢信息。画像**不替代**任何事实 —— 事实一条不少地还在，
#   检索照样能翻到原话。画像是**多出来的一份概述**，和事实并存。
#   所以它不违背"硬盘多大记忆就多大"：硬盘上是一份完整 + 一份概述，
#   而不是完整被换成了概述。
#
# 谁生成：模型。谁否决：你。画像是她的判断，但你可以说"这段不对"。

PROFILE_MAX_CHARS = 900

PROFILE_SYSTEM = """你在帮一个角色写「关于面前这个人，我现在知道什么」。

只输出一段话，不要 JSON、不要标题、不要分条、不要 markdown。

要求：
1. 只用下面给出的「已知事实」，不许补充任何没写的东西。
2. 用一段连贯的话写，像在心里过一遍这个人，不要罗列。
3. 先写最稳的（身份、长期习惯、性格），再写最近的（近况、心情）。
4. 事实之间有矛盾的（比如改过口），以**后面的**为准，不要两边都写。
5. 不超过 400 字。写不下就挑重要的，宁可短。
6. 不要写"主人说""根据记忆"这类元信息，直接写这个人是什么样。
7. 不确定的加"好像"，但不要滥用 —— 大部分事实是明确的。
"""


def _profile_prompt(items: List[Dict[str, Any]], name: str) -> str:
    lines = []
    for i, it in enumerate(items, 1):
        who = it.get("anchor") or "他"
        lines.append(f"[{i}] {who}｜{it['predicate']}：{it['object']}"
                     f"（确认度 {int(round(float(it.get('confidence') or 0) * 100))}%）")
    return (f"这个人叫：{name or '（不知道名字）'}\n\n"
            "已知事实：\n" + "\n".join(lines))


def get_profile(path: str, bank: str, owner: str) -> Dict[str, Any]:
    """读一个人的画像。没有就返回空。"""
    if not (path and os.path.isfile(path) and owner):
        return {"owner": owner, "text": "", "enabled": True, "factsAt": 0}
    try:
        conn = connect(path, read_only=True)
    except Exception:
        return {"owner": owner, "text": "", "enabled": True, "factsAt": 0}
    try:
        row = conn.execute(
            "SELECT * FROM profiles WHERE bank=? AND owner=?",
            (bank, owner)).fetchone()
        if row is None:
            return {"owner": owner, "text": "", "enabled": True, "factsAt": 0}
        return {
            "owner": owner, "text": row["text"] or "",
            "enabled": bool(row["enabled"]),
            "factsAt": int(row["facts_at"] or 0),
            "updatedAt": int(row["updated_at"] or 0),
        }
    except Exception:
        return {"owner": owner, "text": "", "enabled": True, "factsAt": 0}
    finally:
        conn.close()


def profiles_for_prompt(path: str, bank: str, *, owner: str) -> str:
    """把画像渲染成注入段。

    ⚠ 前置条件写死在这里：
      1. 画像必须 enabled —— 你否决过的，一个字都不注入。
      2. 只注入**当前这个人**的画像，不注入别人的。
         别人的画像里必然带着别人的私事，那正是 v5 立起来的那道墙。
    """
    if not owner:
        return ""
    p = get_profile(path, bank, owner)
    if not p.get("text") or not p.get("enabled"):
        return ""
    return p["text"].strip()[:PROFILE_MAX_CHARS]


def should_refresh_profile(path: str, bank: str, owner: str, *,
                           min_growth: int = 40) -> bool:
    """该不该重新归纳画像。

    两个条件满足任一：
      · 从没归纳过
      · 上次归纳之后，这个人的 active 事实又涨了 min_growth 条

    为什么不用"每隔 N 小时"：聊得少的人永远不该重算（浪费 token），
    聊得多的人一小时就能涨 40 条。按增长量算才是对的尺子。
    """
    if not owner:
        return False
    try:
        conn = connect(path, read_only=True)
    except Exception:
        return False
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE bank=? AND owner=? AND status='active'",
            (bank, owner)).fetchone()[0]
        row = conn.execute(
            "SELECT facts_at FROM profiles WHERE bank=? AND owner=?",
            (bank, owner)).fetchone()
        if row is None:
            return int(cur) >= 3      # 少于 3 条事实不必写画像
        return int(cur) - int(row["facts_at"] or 0) >= min_growth
    except Exception:
        return False
    finally:
        conn.close()


def refresh_profile(path: str, bank: str, owner: str, call_model,
                    *, name: str = "", limit: int = 300,
                    now: Optional[int] = None) -> Dict[str, Any]:
    """重新归纳一个人的画像。

    `call_model(system, user) -> str` 由调用方注入，和 reflect 一样 ——
    这一层不碰网络。

    归纳只读**这个人自己的** active 事实，不碰别人的。
    最多取 limit 条（默认 300），按取用分数排 —— 常用的、重要的先入画。
    """
    if not (path and os.path.isfile(path) and owner):
        return {"ok": False, "error": "缺少路径或 owner"}
    now = now or _now_ms()
    try:
        conn = connect(path, read_only=True)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    try:
        rows = conn.execute(
            f"SELECT * FROM facts WHERE bank=? AND owner=? AND status='active' "
            f"ORDER BY pinned DESC, {_FRESH_ORDER} LIMIT ?",
            (bank, owner, max(10, int(limit)))).fetchall()
        items = [_row_to_item(r) for r in rows]
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()
    if not items:
        return {"ok": True, "skipped": True, "reason": "没有事实可归纳"}
    try:
        raw = call_model(PROFILE_SYSTEM, _profile_prompt(items, name))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    text = (raw or "").strip()
    # 模型爱包 markdown / 加标题，剥掉
    text = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    if not text:
        return {"ok": False, "error": "模型返回空"}
    text = text[:PROFILE_MAX_CHARS]
    ok = save_profile(path, bank, owner, text,
                      fact_ids=[i["id"] for i in items], now=now)
    return {"ok": ok[0], "error": ok[1], "chars": len(text),
            "fromFacts": len(items)}


def save_profile(path: str, bank: str, owner: str, text: str, *,
                 fact_ids: Sequence[int] = (), now: Optional[int] = None
                 ) -> Tuple[bool, str]:
    """写入画像。

    ⚠ `enabled` 不在这里改 —— 重新归纳不该顺手把"你否决过"变成"没否决"。
      否则你否决一次，下次刷新又活了，否决就成了摆设。
      要恢复必须显式调 set_profile_enabled(True)。
    """
    if not owner:
        return False, "owner 为空"
    now = now or _now_ms()
    try:
        conn = connect(path)
    except Exception as exc:
        return False, str(exc)
    try:
        with conn:
            old = conn.execute(
                "SELECT created_at, enabled FROM profiles WHERE bank=? AND owner=?",
                (bank, owner)).fetchone()
            created = int(old["created_at"]) if old else now
            enabled = int(old["enabled"]) if old else 1
            conn.execute(
                "INSERT INTO profiles(bank,owner,text,facts_at,fact_ids,enabled,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(bank,owner) DO UPDATE SET "
                "text=excluded.text, facts_at=excluded.facts_at, "
                "fact_ids=excluded.fact_ids, updated_at=excluded.updated_at",
                (bank, owner, (text or "")[:PROFILE_MAX_CHARS],
                 len(fact_ids), json.dumps(list(fact_ids)[:400]), enabled,
                 created, now))
        return True, ""
    except Exception as exc:
        return False, str(exc)
    finally:
        conn.close()


def set_profile_enabled(path: str, bank: str, owner: str,
                        enabled: bool) -> Tuple[bool, str]:
    """启 / 停用画像。停用 = 她不再按这段画像理解你，但画像本身留着。

    为什么不是删：删了下次归纳又生成，你的否决等于没说过。
      留着并置 enabled=0，她才知道"这段你不认"，也才有据可查。
    """
    try:
        conn = connect(path)
    except Exception as exc:
        return False, str(exc)
    try:
        with conn:
            cur = conn.execute(
                "UPDATE profiles SET enabled=?, updated_at=? WHERE bank=? AND owner=?",
                (1 if enabled else 0, _now_ms(), bank, owner))
        if not cur.rowcount:
            return False, "还没有画像"
        return True, ""
    except Exception as exc:
        return False, str(exc)
    finally:
        conn.close()


def impression_board(path: str, bank: str) -> Dict[str, Any]:
    """「角色印象」页的数据：每个人一格，格子里是她的评语 + 开放信息清单。

    结构：
        {"people": [{owner, name, kind, tier, note, open: [...], facts, private}],
         "mainOwner": ""}

    `open` 只装**开放信息**（shared + 白名单话题）—— 这一页的用途是让主人
    逐条审阅"她准备对外说哪些关于我的事"，所以只列会外传的那些。
    私密的数量给一个数字，但不铺开 —— 主人要看全文去记忆页。

    note：印象评语。存在 owners 表旁边的一张表里（见印象表 DDL），
    没有就返回空串，UI 显示输入框。

    profile（v6）：她自己归纳的**画像**。和 note 是两回事：

        note     你手写的评语，模型不参与，她也不看（只是存着给你自己）
        profile  她从事实归纳的概述，**会注入提示词**，你不认可以否决

    两个都返回，界面上分两栏显示，一眼能看出"她以为的"和"你以为的"差在哪。
    """
    out: Dict[str, Any] = {"people": []}
    if not (path and os.path.isfile(path)):
        return out
    try:
        conn = connect(path, read_only=True)
    except Exception:
        return out
    try:
        rows = conn.execute(
            "SELECT * FROM owners ORDER BY last_seen DESC").fetchall()
        # owners 表里没有、但有事实的（reflect 直接写过、没走 ensure_owner），
        # 也要出现在印象页 —— 否则"她记住了这个人，但你翻不到他的画像"。
        known = {r["owner"] for r in rows}
        loose = [o for o in _all_owners(path) if o not in known]
        people = []
        for r in rows:
            owner = r["owner"]
            note = ""
            try:
                nr = conn.execute(
                    "SELECT note FROM impressions WHERE owner=? AND bank=?",
                    (owner, bank)).fetchone()
                note = (nr["note"] if nr else "") or ""
            except Exception:
                note = ""
            prof = get_profile(path, bank, owner)
            opens = open_facts_for(path, bank, owner, limit=60)
            total = 0
            priv = 0
            try:
                total = conn.execute(
                    "SELECT COUNT(*) FROM facts WHERE bank=? AND owner=? "
                    "AND status='active'", (bank, owner)).fetchone()[0]
                priv = conn.execute(
                    "SELECT COUNT(*) FROM facts WHERE bank=? AND owner=? "
                    "AND status='active' AND NOT (visibility IN ('shared','public'))",
                    (bank, owner)).fetchone()[0]
            except Exception:
                pass
            people.append({
                "owner": owner,
                "name": r["display_name"] or r["external_id"] or owner.rsplit(":", 1)[-1],
                "kind": r["kind"], "tier": r["tier"] or TIER_GUEST,
                "note": note,
                "profile": prof.get("text") or "",
                "profileEnabled": bool(prof.get("enabled", True)),
                "profileFactsAt": int(prof.get("factsAt") or 0),
                "profileUpdatedAt": int(prof.get("updatedAt") or 0),
                "open": [{"id": it["id"], "topic": it["topic"],
                          "predicate": it["predicate"], "object": it["object"],
                          "visibility": it["visibility"]} for it in opens],
                "facts": int(total), "private": int(priv),
            })
        for owner in loose:
            prof = get_profile(path, bank, owner)
            try:
                total = conn.execute(
                    "SELECT COUNT(*) FROM facts WHERE bank=? AND owner=? "
                    "AND status='active'", (bank, owner)).fetchone()[0]
            except Exception:
                total = 0
            people.append({
                "owner": owner, "name": owner.rsplit(":", 1)[-1],
                "kind": "group" if owner.startswith("group:") else "private",
                "tier": TIER_GUEST, "note": "",
                "profile": prof.get("text") or "",
                "profileEnabled": bool(prof.get("enabled", True)),
                "profileFactsAt": int(prof.get("factsAt") or 0),
                "profileUpdatedAt": int(prof.get("updatedAt") or 0),
                "open": [], "facts": int(total), "private": int(total),
            })
        out["people"] = people
        return out
    except Exception:
        return out
    finally:
        conn.close()


def set_impression(path: str, bank: str, owner: str, note: str) -> Tuple[bool, str]:
    """写「我对这个人的印象」。这是主人手写的一段话，模型不参与。"""
    if not owner:
        return False, "owner 为空"
    try:
        conn = connect(path)
    except Exception as exc:
        return False, str(exc)
    try:
        now = _now_ms()
        with conn:
            conn.execute(
                "INSERT INTO impressions(bank,owner,note,created_at,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(bank,owner) DO UPDATE SET "
                "note=excluded.note, updated_at=excluded.updated_at",
                (bank, owner, (note or "")[:2000], now, now))
        return True, ""
    except Exception as exc:
        return False, str(exc)
    finally:
        conn.close()


def revoke_open(path: str, bank: str, fact_id: int) -> Tuple[bool, str]:
    """把一条开放信息**撤回**成私密。

    ⚠ 这是"角色印象页点删除"做的事：不是忘掉，只是不再外传。
      和 forget_fact 完全不同 —— 忘掉是"她不再拿它当结论"，
      撤回是"这条结论还在，但不许说给外人"。

    同时清空 topic —— 话题标签本来就是为"能不能外传"服务的，
    撤回了留着它没有意义，反而会让下次误判。
    """
    try:
        conn = connect(path)
    except Exception as exc:
        return False, str(exc)
    try:
        now = _now_ms()
        with conn:
            row = conn.execute("SELECT * FROM facts WHERE id=? AND bank=?",
                               (fact_id, bank)).fetchone()
            if row is None:
                return False, "没有这条事实"
            conn.execute(
                "UPDATE facts SET visibility=?, topic='', updated_at=? WHERE id=?",
                (VIS_PRIVATE, now, fact_id))
            _event(conn, bank, fact_id, "revoke_open",
                   f"撤回开放：{row['predicate']}",
                   {"was": row["visibility"], "topic": row["topic"]})
        return True, ""
    except Exception as exc:
        return False, str(exc)
    finally:
        conn.close()


def delete_fact_pair(path: str, bank: str, fact_id: int) -> Dict[str, Any]:
    """删一条事实，并且**连带删掉它的双写副本**（用户选的 B 方案）。

    群聊双写之后，同一条话在「人池」和「群池」各有一条，但它们
    **不是靠 id 关联的** —— 两条是独立插入的行。所以要靠
    (origin, predicate, object, 时间相近) 找副本。

    ⚠ 只在"确实来自群聊"时才找副本。私聊事实没有副本，
      这一步是空操作，不会误删别人的东西。
    """
    out: Dict[str, Any] = {"ok": False, "deleted": [], "error": ""}
    try:
        conn = connect(path)
    except Exception as exc:
        out["error"] = str(exc)
        return out
    try:
        row = conn.execute("SELECT * FROM facts WHERE id=? AND bank=?",
                           (fact_id, bank)).fetchone()
        if row is None:
            out["error"] = "没有这条事实"
            return out
        ids = [int(row["id"])]
        origin = (row["origin"] if "origin" in row.keys() else "") or ""
        if origin.startswith("group:"):
            # 同一次反思写出的两条：owner 不同、origin 相同、谓词对象相同。
            twins = conn.execute(
                "SELECT id FROM facts WHERE bank=? AND origin=? AND predicate=? "
                "AND object=? AND owner<>? AND status='active'",
                (bank, origin, row["predicate"], row["object"],
                 row["owner"])).fetchall()
            ids += [int(t["id"]) for t in twins]
        now = _now_ms()
        with conn:
            marks = ",".join("?" * len(ids))
            conn.execute(
                f"UPDATE facts SET status=?, updated_at=? WHERE id IN ({marks})",
                (FORGOTTEN, now, *ids))
            for fid in ids:
                _event(conn, bank, fid, "forget", "用户删除",
                       {"pair": len(ids) > 1})
        out.update({"ok": True, "deleted": ids})
        return out
    except Exception as exc:
        out["error"] = str(exc)
        return out
    finally:
        conn.close()





# ══════════════════════════════════════════════════════════════════════════
# 置顶
# ══════════════════════════════════════════════════════════════════════════


def pin_fact(path: str, bank: str, fact_id: int, *, pinned: bool = True) -> Tuple[bool, str]:
    """置顶 / 取消置顶一条事实。

    置顶**只是"别被挤掉"**，不改变可信度、不改 visibility、不改状态。
    别人问起来，置顶的私密事实照样看不到 —— 这两个概念必须分开。
    """
    try:
        conn = connect(path)
    except Exception as exc:
        return False, str(exc)
    try:
        with conn:
            cur = conn.execute("UPDATE facts SET pinned=?, updated_at=? WHERE id=? AND bank=?",
                               (1 if pinned else 0, _now_ms(), fact_id, bank))
        if not cur.rowcount:
            return False, "没有这条事实"
        return True, ""
    except Exception as exc:
        return False, str(exc)
    finally:
        conn.close()


def set_visibility(path: str, bank: str, fact_id: int, visibility: str,
                   *, by: str = "") -> Tuple[bool, str]:
    """改一条事实的共享级别。**这是唯一能把私密变成共享的入口。**

    ⚠ 只该由主人在界面上点。模型、非主人、任何自动流程都不该调它。
      设计上这里不做权限检查（那是调用方的事），但函数名和文档必须
      把"这是危险操作"写清楚 —— 因为这是整个系统里唯一能主动泄露的口子。
    """
    if visibility not in VISIBILITIES:
        return False, f"visibility 只能是 {VISIBILITIES}"
    try:
        conn = connect(path)
    except Exception as exc:
        return False, str(exc)
    try:
        with conn:
            row = conn.execute("SELECT visibility FROM facts WHERE id=? AND bank=?",
                               (fact_id, bank)).fetchone()
            if row is None:
                return False, "没有这条事实"
            conn.execute("UPDATE facts SET visibility=?, updated_at=? WHERE id=?",
                         (visibility, _now_ms(), fact_id))
            _event(conn, bank, fact_id, "visibility",
                   f"{row['visibility']} → {visibility}",
                   {"by": by, "from": row["visibility"], "to": visibility})
        return True, ""
    except Exception as exc:
        return False, str(exc)
    finally:
        conn.close()


def set_topic(path: str, bank: str, fact_id: int, topic: str,
              *, by: str = "") -> Tuple[bool, str]:
    """给一条事实打话题标签。话题决定「这条能不能说给外人听」。

    和 set_visibility 的关系：**两个都要满足才能外传**（见 open_facts_for）。
    所以这里传一个不在集合里的值，等于"撤销开放"，不是报错 ——
    更好用的是 revoke_open()，那个一次把两项都清干净。

    ⚠ 只该由主人在界面上点，或者由反思时**自动打一个保守的标签**。
      模型打的标签错了只会导致"这条不给外人看"，不会反向泄露 ——
      因为它和 visibility 是**与**的关系，不是或。
    """
    t = (topic or "").strip()
    if t and t not in TOPICS:
        return False, f"话题只能是 {TOPICS} 之一，或留空"
    try:
        conn = connect(path)
    except Exception as exc:
        return False, str(exc)
    try:
        with conn:
            row = conn.execute("SELECT topic FROM facts WHERE id=? AND bank=?",
                               (fact_id, bank)).fetchone()
            if row is None:
                return False, "没有这条事实"
            conn.execute("UPDATE facts SET topic=?, updated_at=? WHERE id=?",
                         (t[:40], _now_ms(), fact_id))
            _event(conn, bank, fact_id, "topic",
                   f"{row['topic'] or '-'} → {t or '-'}",
                   {"by": by, "from": row["topic"], "to": t})
        return True, ""
    except Exception as exc:
        return False, str(exc)
    finally:
        conn.close()

REFLECT_SYSTEM = """你在帮一个角色把她记得的事整理成「事实」，而不是复述原文。

只输出一个 JSON 对象，不要任何其它文字、不要 Markdown 代码块。

格式：
{"facts":[{"anchor":"","predicate":"","object":"","confidence":0.6,"chunks":[1],"evidence":"","topic":""}],"skip_reason":""}

规则：
1. **只从下面给出的原文里提炼**，不许补充原文没有的事，不许把她的猜测写成事实。
2. anchor 是"这条事实属于谁"：**对方**的名字/称呼，或留空表示关于角色自己。
   只在原文里真的出现该称呼时才填。
   ⚠ **角色自己的名字不是对方的称呼。** 原文里她自称、或对方在叫她
   （比如对方只发了一句"肥鱼"），都只是打招呼，**不产生任何事实** ——
   尤其不要生成「对方的名字叫 X」这种。只有对方明确自我介绍
   （"我叫…"/"我是…"）、或有人第三人称指认（"他叫…"）时，才记称呼。
3. predicate 是简短的一类属性（6 字以内），例如「饮食偏好」「工作时间」「称呼」
   「生日」「身体状态」「家人」。同一类事实必须用同一个 predicate 词。
4. object 是具体内容，一句话，不要带"她说""好像"这类转述前缀。
5. confidence 0~1：原文明确陈述 0.8~0.9；从语气推测 0.4~0.6；玩笑/气话/客套 0.2~0.3
   或干脆不提。
6. chunks 填支持这条事实的**真实编号**：每行括号里 `chunk` 后面那个数字
   （例：`[3] (chunk 247)` 就填 247，不是填 3）。至少一个。
   **这个编号会被程序拿去核对**，填一个不存在的编号这条事实会被直接丢掉。
7. evidence 摘 20 字以内的原话片段作依据。
8. **不确定就少提**。宁缺毋滥：一次情绪、玩笑、客套不能变成长期标签。
   纯寒暄（"在吗""嗯""好"）不产生任何事实，skip_reason 里说明。
9. 涉及密码、密钥、身份证号、银行卡的内容一律不提。
10. topic 是这条事实讲的**哪一类事**，只能从下面这些词里选一个，
    一个都不像就留空：
        食物 爱好 作息 性格 口头禅 称呼
        住址 工作 行程 身体 金钱 家人 联系人 隐私
        年龄 职业 城市 情感 近况

    ⚠ topic 决定「这条能不能说给别人听」，所以**必须按字面归类**，
      不许为了让它"看起来更安全"而往前面几类上靠。
      拿不准就留空 —— 留空等于不外传，是安全的那一边。
      举几个容易错的：
        「他住在朝阳区」      → 住址（不是"行程"）
        「他今年 28」         → 年龄
        「他在字节上班」      → 工作（不是"职业"，职业是"程序员"这种）
        「他昨天发烧了」      → 身体
        「他昨天加班到十点」  → 行程
"""


def _parse_reflect(raw: str) -> Tuple[List[Fact], str]:
    """解析反思输出。模型爱包 ```json，这里容忍一层围栏。"""
    text = (raw or "").strip()
    fence = re.match(r"^```[A-Za-z0-9_-]*\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except Exception:
        # 退一步：截第一个 { 到最后一个 }
        i, j = text.find("{"), text.rfind("}")
        if i < 0 or j <= i:
            return [], "输出不是 JSON"
        try:
            data = json.loads(text[i:j + 1])
        except Exception:
            return [], "输出不是 JSON"
    if not isinstance(data, dict):
        return [], "输出不是对象"
    facts: List[Fact] = []
    for item in (data.get("facts") or [])[:24]:
        if not isinstance(item, dict):
            continue
        f = Fact(
            predicate=str(item.get("predicate") or ""),
            object=str(item.get("object") or ""),
            anchor=str(item.get("anchor") or ""),
            # confidence 必须是真数字 —— 模型可能给 "高" / null / "0.8"。
            # 以前直接 float() 掉，字符串会静默变成默认值，等于"模型乱写也不报错"。
            confidence=_coerce_confidence(item.get("confidence")),
            source_chunks=[c for c in (item.get("chunks") or []) if isinstance(c, int)],
            evidence=str(item.get("evidence") or ""),
            # visibility 默认 private。**模型不许自己说"这条可以给别人看"** ——
            # 只接受它明确给出的 "shared"，且后面还有一道主人确认（见 desktop）。
            visibility=(VIS_SHARED if str(item.get("visibility") or "").strip().lower()
                        == VIS_SHARED else VIS_PRIVATE),
            # v5：话题由模型按字面归类，认不出就留空（= 不外传）。
            #   ⚠ 这里**不做任何模糊匹配** —— 模型给"吃的东西"这种近似词
            #     一律当没说过。宁可少判一类，也不能把"住址"误判成"食物"。
            topic=str(item.get("topic") or "").strip(),
            # origin 由调用方（reflect_on_session）按会话类型填，不由模型决定。
            # 模型不知道"这句话是在哪个群说的"，让它填等于给它一个编造机会。
        )
        if f.ok:
            facts.append(f)
    return facts, str(data.get("skip_reason") or "")


# ══════════════════════════════════════════════════════════════════════════
# 行为习惯 —— 「说够三次才算」
# ══════════════════════════════════════════════════════════════════════════
#
# 为什么要有这一层：facts 是"模型读一遍就想出来的结论"，一次幻觉就永久入库。
# 习惯是另一回事 —— 它**只数次数，不做解释**：
#
#     同一句话，在 ≥3 个**不同日期**出现过 → 才算一个习惯
#
# 三个设计要点（都从插件那边学来的，每一条都有血的教训）：
#
#   1. `min_count` 与 `min_days` 是**两个独立条件**。
#      只看次数的话，"今天说了三次好困"就成了"她认定你天天熬夜"。
#      必须跨日期 —— 一天说一百遍不算习惯。
#
#   2. **衰减**。三个月前说过五次的事，不该跟昨天说过五次的一样重。
#      这里用半衰期（默认 30 天）折现。
#
#   3. **只给原话样本，不给标签**。不猜"这是饮食偏好还是抱怨"，
#      把样本原样交给模型，让它在本轮语境里自己判断。
#      猜错的标签会跟一辈子，样本不会。

HABIT_MIN_COUNT = 3
HABIT_MIN_DAYS = 3
HABIT_MAX_ITEMS = 24
HABIT_HALF_LIFE_DAYS = 30.0
_HABIT_BUCKET_HOURS = 8       # 归类用的时间桶：早/午/晚/夜


def _day_key(ts_ms: int) -> str:
    """把一个毫秒时间戳变成「本地日期」，用于"不同日期"判定。"""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(ts_ms / 1000.0))
    except Exception:
        return ""


def habit_signature(text: str) -> str:
    """把一句话压成"同一类说法"的签名。

    用分词结果的**前 6 个词**做指纹：足够区分"好困"和"好饿"，
    又不会因为多说两个字就变成新习惯。

    ⚠ 不做语义归类。这里只做"长得像不像"的粗判，
      语义判断交给模型在注入时完成。
    """
    toks = terms(text)
    if not toks:
        return ""
    return " ".join(toks[:6])


def observe_habit(path: str, bank: str, owner: str, text: str, *,
                  category: str = "utterance", at: Optional[int] = None) -> Tuple[bool, str]:
    """记一次习惯观察。同一个签名**重复出现只会累加**，不会新增行。

    这一步很便宜（一次 UPSERT），所以可以每轮都调；重组（衰减 + 截断）
    由 refresh_habits() 按节奏做，不在这里做。
    """
    if not owner or not (text or "").strip():
        return False, "owner / text 为空"
    sig = habit_signature(text)
    if not sig:
        return False, "分词为空"
    at = at or _now_ms()
    day = _day_key(at)
    try:
        conn = connect(path)
    except Exception as exc:
        return False, str(exc)
    try:
        with conn:
            row = conn.execute(
                "SELECT * FROM habits WHERE bank=? AND owner=? AND category=? AND signature=?",
                (bank, owner, category, sig)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO habits(bank,owner,category,signature,samples,count,days,"
                    "first_at,last_at,score,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,1,1,?,?,?,?,?)",
                    (bank, owner, category, sig,
                     json.dumps([[(text or "").strip()[:200], day]], ensure_ascii=False),
                     at, at, 1.0, at, at))
            else:
                try:
                    samples = json.loads(row["samples"] or "[]")
                except Exception:
                    samples = []
                seen_days = {s[1] for s in samples if isinstance(s, list) and len(s) > 1}
                if day not in seen_days:
                    samples.append([(text or "").strip()[:200], day])
                    samples = samples[-8:]        # 每类最多留 8 条原话样本
                    days = int(row["days"] or 0) + 1
                else:
                    days = int(row["days"] or 0)
                conn.execute(
                    "UPDATE habits SET samples=?, count=count+1, days=?, last_at=?, updated_at=? "
                    "WHERE id=?",
                    (json.dumps(samples, ensure_ascii=False), days, at, at, row["id"]))
        return True, ""
    except Exception as exc:
        return False, str(exc)
    finally:
        conn.close()


def refresh_habits(path: str, bank: str, owner: str, *, now: Optional[int] = None,
                   half_life_days: float = HABIT_HALF_LIFE_DAYS,
                   max_items: int = HABIT_MAX_ITEMS) -> Dict[str, Any]:
    """重算评分、丢掉过期的、截断到上限。**按节奏调用，不是每轮。**

    评分 = 次数 × 衰减。衰减用半衰期：
        score = count * 0.5 ** (age_days / half_life_days)

    age 用"最后一次出现到现在"，不是"第一次" —— 一个刚提过的老习惯
    仍然是活的；一个三个月没提过的旧习惯就该沉下去。
    """
    now = now or _now_ms()
    try:
        conn = connect(path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    try:
        rows = conn.execute(
            "SELECT * FROM habits WHERE bank=? AND owner=?", (bank, owner)).fetchall()
        scored = []
        for r in rows:
            age_days = max(0.0, (now - int(r["last_at"] or now)) / 86_400_000.0)
            decay = 0.5 ** (age_days / max(0.1, half_life_days))
            score = float(r["count"] or 0) * decay
            scored.append((score, int(r["id"])))
        scored.sort(key=lambda item: -item[0])
        keep = {fid for _score, fid in scored[:max_items]}
        drop = [fid for _score, fid in scored[max_items:]]
        with conn:
            for score, fid in scored:
                conn.execute("UPDATE habits SET score=?, updated_at=? WHERE id=?",
                             (round(score, 4), now, fid))
            for fid in drop:
                conn.execute("DELETE FROM habits WHERE id=?", (fid,))
        return {"ok": True, "total": len(scored), "kept": len(keep), "dropped": len(drop)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


def habits_for_prompt(path: str, bank: str, *, owner: str, tier: str,
                      min_count: int = HABIT_MIN_COUNT,
                      min_days: int = HABIT_MIN_DAYS,
                      limit: int = 6) -> str:
    """把够格的习惯渲染成注入段。

    够格 = count ≥ min_count **且** days ≥ min_days。

    ⚠ 只输出原话样本，不输出"她认定你 X"这类结论。
      模型拿到样本，自己在本轮语境里判断这是不是饮食偏好。
    """
    if not owner or not tier:
        return ""
    try:
        conn = connect(path, read_only=True)
    except Exception:
        return ""
    try:
        rows = conn.execute(
            "SELECT signature, samples, count, days, score FROM habits "
            "WHERE bank=? AND owner=? AND count>=? AND days>=? "
            "ORDER BY score DESC LIMIT ?",
            (bank, owner, max(1, int(min_count)), max(1, int(min_days)),
             max(1, int(limit)))).fetchall()
    except Exception:
        return ""
    finally:
        conn.close()
    if not rows:
        return ""
    lines = []
    for r in rows:
        try:
            samples = json.loads(r["samples"] or "[]")
        except Exception:
            samples = []
        phrases = [s[0] for s in samples if isinstance(s, list) and s and s[0]][:3]
        if not phrases:
            continue
        joined = " / ".join(f"“{p[:40]}”" for p in phrases)
        lines.append(f"- 说过 {r['count']} 次、跨 {r['days']} 天：{joined}")
    return "\n".join(lines)


def _coerce_confidence(value: Any) -> float:
    """把模型给的 confidence 收敛到 0..1 的浮点。认不出就退回 0.5。

    这个函数存在的理由：reflection 返回的是模型自由输出，字段类型
    完全不可信。校验**只做一件正确的事** —— 认不出就取中位数，
    而不是抛异常让整批反思作废（那样一次格式错误会丢掉一整段记忆）。
    """
    if isinstance(value, bool):
        return 0.5
    if isinstance(value, (int, float)):
        try:
            v = float(value)
        except (TypeError, ValueError):
            return 0.5
        if not math.isfinite(v):
            return 0.5
        return max(0.0, min(1.0, v))
    if isinstance(value, str):
        s = value.strip().rstrip("%")
        try:
            v = float(s)
        except ValueError:
            # "高" / "high" / "0.8" 之类：只认能直接解析的，其余取中位
            low = value.strip().lower()
            if low in ("高", "很高", "high", "strong", "确定"):
                return 0.85
            if low in ("中", "medium", "一般", "可能"):
                return 0.6
            if low in ("低", "很低", "low", "weak", "不确定"):
                return 0.35
            return 0.5
        if s == str(value).strip() and v > 1.0:
            v = v / 100.0          # 模型常把 85 当成百分比给
        if not math.isfinite(v):
            return 0.5
        return max(0.0, min(1.0, v))
    return 0.5


def _ensure_anchor_safe(path: str, bank: str, name: str,
                        chunks: Sequence[int] = ()) -> Tuple[bool, str]:
    """在**独立短连接**里建锚点并挂 chunk，立刻提交。

    为什么不能复用外部那条长连接：`ensure_anchor` 是写操作，一旦在长连接上
    执行就开了一个未提交的写事务（SQLite 的 deferred → RESERVED 锁）。
    此时任何**另一个连接**的写入都会拿到 "database is locked" 并失败。

    反思循环里就是"先 remember（新连接）→ 再 ensure_anchor（长连接）→
    再 remember（新连接）"这个顺序，于是第二条事实永远写不进去，
    而且错误被丢掉，看起来像"模型少给了"。

    短连接 + 立刻提交，把锁持有时间压到一个语句以内。
    """
    try:
        c = connect(path)
    except Exception as exc:
        return False, str(exc)
    try:
        with c:
            aid = ensure_anchor(c, bank, name)
            for chunk in chunks:
                link_anchor_chunk(c, aid, int(chunk))
        return True, ""
    except Exception as exc:
        return False, str(exc)
    finally:
        c.close()


def reflect_on_session(path: str, bank: str, session: str, call_model,
                       *, owner: str = "", tier: str = TIER_GUEST,
                       batch: int = 40, now: Optional[int] = None,
                       self_names: Sequence[str] = (),
                       group_owner: str = "",
                       origin: str = "") -> Dict[str, Any]:
    """对某个会话的新消息做一次反思，把原文沉淀成事实。

    `call_model(system, user) -> str` 由调用方注入（replier.call_model）。
    这样这一层不需要知道模型怎么调、key 在哪，也就不会把网络代码拖进存储层。

    `owner` / `tier`：这个会话属于谁、对方是什么身份。**必须传** ——
    反思出来的事实要打上"属于谁"的印记，否则写不进去（remember 会拒绝）。

    `group_owner`（v5）：当这次反思发生在群里时，传"这个群"的 owner 键。
    传了就会**双写**：说话人的池 + 群的池（见 remember_both）。
    私聊时留空 = 只写一份，行为与以前完全一致。

    `origin`（v5）：这条事实是在哪儿说的。私聊传 "private"，
    群聊传 "group:<群 id>"。由调用方给，**不由模型决定** ——
    模型不知道自己在哪个群说话，让它填等于给它一个编造机会。

    `self_names`：**角色自己**的名字与自称（别人叫她什么）。必须传 ——
    否则模型分不清"对方发来一句「肥鱼」"是打招呼还是自我介绍，
    会把角色自己的名字记成对方的名字（真实踩过：攒出一条
    「对方的名字叫肥鱼」的事实，而"肥鱼"本来就是她）。

    幂等：`reflection_state.last_message` 记住水位，同一条消息不会被反思两次。
    失败不推进水位 —— 下次重试同一批。
    """
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": "找不到记忆库", "added": 0}
    now = now or _now_ms()
    try:
        conn = connect(path)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "added": 0}
    try:
        st = conn.execute(
            "SELECT * FROM reflection_sessions WHERE bank=? AND session=?",
            (bank, session)).fetchone()
        watermark = int(st["last_message"] or 0) if st else 0
        rows = conn.execute(
            "SELECT id, role, text, COALESCE(source_time, recorded_at) AS t "
            "FROM messages WHERE bank=? AND session=? AND id>? "
            "ORDER BY id LIMIT ?", (bank, session, watermark, max(1, batch))).fetchall()
        if not rows:
            return {"ok": True, "added": 0, "repaired": 0, "skipped": True,
                    "reason": "没有新消息"}
        lines, chunk_ids, names = [], [], []
        # 行号 → 这一行真正的 chunk id（一行可能切成多片）。
        # 提示词里每行长这样：`[3] (chunk 247,248) 对方：...` ——
        # 行号和真实 id 是两个不同的数，而核对要的是后者。
        # 这里把映射留住，好在核对时把"模型填了行号"也救回来。
        line_chunks: Dict[int, List[int]] = {}
        for i, r in enumerate(rows, 1):
            who = "她" if r["role"] == "assistant" else "对方"
            chunk_rows = conn.execute(
                "SELECT id FROM chunks WHERE message=?", (int(r["id"]),)).fetchall()
            cids = [int(c[0]) for c in chunk_rows]
            chunk_ids.extend(cids)
            line_chunks[i] = list(cids)
            tag = ",".join(str(c) for c in cids) or "-"
            lines.append(f"[{i}] (chunk {tag}) {who}：{r['text'][:400]}")
        # 锚点表没有 owner 列 —— 锚点是"这个名字指的是谁"，天然共享；
        # 真正的隔离在 facts.owner，不在这里。所以这里不加 owner 条件。
        names = [n[0] for n in conn.execute(
            "SELECT name FROM anchors WHERE bank=?", (bank,)).fetchall()]
        self_line = "、".join(n for n in self_names if n) or "（未配置）"
        user = ("角色自己叫什么：" + self_line
                + "\n（上面这些是**她**的名字，不是对方的。对方在叫她时，"
                  "那只是打招呼，不产生事实。）"
                + "\n\n已知人物（对方这一侧）："
                + ("、".join(names) if names else "（无）")
                + "\n\n原文：\n" + "\n".join(lines))
        try:
            raw = call_model(REFLECT_SYSTEM, user)
        except Exception as exc:
            conn.execute(
                "INSERT INTO reflection_sessions(bank,session,last_message,last_run,last_error) "
                "VALUES(?,?,?,?,?) ON CONFLICT(bank,session) DO UPDATE SET "
                "last_run=excluded.last_run, last_error=excluded.last_error",
                (bank, session, watermark, now, str(exc)[:300]))
            conn.commit()
            return {"ok": False, "error": str(exc), "added": 0}
        facts, skip = _parse_reflect(raw)
        valid_chunks = set(chunk_ids)
        added = repaired = 0
        mirrored = 0
        failed = []
        for f in facts:
            # ⚠ 模型给的编号有两种可能，必须都认：
            #   · 提示词要求填 `chunk` 后面那个**真实 id**（比如 247）；
            #   · 但历史提示词只教了"每行开头的 [n]"，老模型/缓存会填**行号**。
            #
            #   以前这里只认 chunk id。于是只要库里有历史，第 1 行的
            #   chunk id 就不是 1，模型填 `[1]` → `1 ∈ {247,…}` 为假 →
            #   **整条事实被拒收**。facts 表长期为 0、日志成片
            #   "来源编号无效"、她"记得却不完全记得"，根因都在这一行。
            #
            #   交集为空仍然拒收（不能替模型伪造来源）—— 这一点没变。
            picked: List[int] = []
            for c in f.source_chunks:
                if c in valid_chunks:
                    picked.append(c)
                elif c in line_chunks:
                    picked.extend(x for x in line_chunks[c] if x in valid_chunks)
            picked = list(dict.fromkeys(picked))
            if not picked:
                failed.append(f"{f.predicate}: 来源编号无效（{f.source_chunks[:3]}），拒收")
                continue
            f.source_chunks = picked
            # 归属由调用方给定，不由模型决定 —— 模型可能把别人的事写到
            # 另一个人名下，而"归属错了"是比"记错了"更难发现的问题。
            if not f.owner:
                f.owner = owner
            if origin:
                f.origin = origin
            if group_owner:
                res = remember_both(path, bank, f, group_owner=group_owner,
                                    allowed_sessions=[session])
                ok, _fid = bool(res.get("ok")), res.get("personal")
                if res.get("group"):
                    mirrored += 1
                for e in res.get("errors") or []:
                    failed.append(f"{f.predicate}: {e}")
            else:
                ok, _fid, _err = remember(path, bank, f,
                                          verify_sources=True,
                                          allowed_sessions=[session])
                if not ok:
                    failed.append(f"{f.predicate}: {_err}")
            if ok:
                added += 1
                if f.anchor:
                    # ⚠ 锚点必须在**独立的短连接**里写。
                    #   以前这里用 reflect 开头那条长连接，ensure_anchor 一写就开了
                    #   未提交的写事务，下一个 remember（另开连接）直接
                    #   "database is locked" —— 而错误被 _err 吞掉，表现为
                    #   "模型给了 2 条事实，库里只多了 1 条"，且完全无声。
                    ok_a, err_a = _ensure_anchor_safe(path, bank, f.anchor,
                                                      f.source_chunks)
                    if not ok_a:
                        failed.append(f"{f.predicate}: 锚点 {err_a}")
        last_id = int(rows[-1]["id"])
        conn.execute(
            "INSERT INTO reflection_sessions(bank,session,last_message,last_run,last_error) "
            "VALUES(?,?,?,?,'') ON CONFLICT(bank,session) DO UPDATE SET "
            "last_message=excluded.last_message, "
            "last_run=excluded.last_run, last_error=''",
            (bank, session, last_id, now))
        conn.commit()
        return {"ok": True, "added": added, "repaired": repaired,
                "mirrored": mirrored,
                "messages": len(rows), "skip_reason": skip,
                "failed": failed[:8]}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "added": 0}
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# 级联维护：删、重建、迁移、健康检查
# ══════════════════════════════════════════════════════════════════════════

# 所有引用 chunk 的旁表。删 chunk 之前必须先把这些行清掉 ——
# 它们没有建 ON DELETE CASCADE，漏一张就留下幽灵。
CHUNK_SIDE_TABLES: Tuple[str, ...] = (
    "search_index", "anchor_sources", "activations", "groups", "replays", "chunk_meta",
)


def forget_chunk_side_tables(conn: sqlite3.Connection, chunk_ids: Sequence[int]) -> None:
    """把一批 chunk 在**所有**旁表里的痕迹清干净。一处收口，不再各删各的。

    旧实现是 delete_messages 里手写五条 DELETE，少一张表就出幽灵命中；
    以后再加旁表也只需要改这一个常数。

    ⚠ 表不存在要跳过，不能抛：老库可能只建过原文四表（甚至只建过
      messages/chunks），而删消息这条路径**不能因为"少一张派表"就失败** ——
      用户点删，结果什么都没删掉，比留下幽灵更糟。
    """
    ids = [int(c) for c in chunk_ids if str(c).strip()]
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    for table in CHUNK_SIDE_TABLES:
        if not _has_table(conn, table):
            continue
        if table == "search_index":
            # FTS5 的 rowid 就是 chunk.id
            conn.execute(f"DELETE FROM search_index WHERE rowid IN ({marks})", ids)
        else:
            conn.execute(f"DELETE FROM {table} WHERE chunk IN ({marks})", ids)


def rebuild_derived(path: str, *, rebuild_facts: bool = False) -> Dict[str, Any]:
    """用权威层（messages/chunks）重建所有派生层。

    派生层坏了、分词改了、从别的实现导入了一份只含 messages 的库 ——
    都靠这个入口修好，而不是让用户"导出再导入"或者手动 VACUUM。

    不重建事实层时（默认），只重建 FTS 索引、锚点关联与 chunk_meta；
    因为事实是从原文**推理**出来的，重建等于重新推理一次，
    那是 refresh 的活（reflect_on_session），不该在"修索引"里偷偷做。
    """
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": "找不到记忆库"}
    try:
        conn = connect(path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    try:
        for stmt in DERIVED_STATEMENTS:
            conn.execute(stmt)
        conn.execute("DELETE FROM search_index")
        conn.execute("DELETE FROM chunk_meta")
        rows = conn.execute(
            "SELECT c.id, c.text, m.bank, m.role FROM chunks c "
            "JOIN messages m ON m.id=c.message").fetchall()
        n = 0
        with conn:
            for r in rows:
                cid = int(r[0])
                body = r[1] or ""
                conn.execute("INSERT INTO search_index(rowid,terms) VALUES(?,?)",
                             (cid, " ".join(terms(body))))
                conn.execute(
                    "INSERT OR REPLACE INTO chunk_meta(chunk,bank,speaker,token_count,term_blob) "
                    "VALUES(?,?,?,?,?)",
                    (cid, r[2], r[3] or "", len(terms(body)),
                     hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]))
                n += 1
            if rebuild_facts:
                conn.execute("DELETE FROM facts")
                conn.execute("DELETE FROM fact_events")
                conn.execute("DELETE FROM reflection_state")
                # 水位表有两张，漏一张的话重建索引之后旧水位还在，
                # 新消息会被判成"已经提炼过"，事实层重新变成空的。
                conn.execute("DELETE FROM reflection_sessions")
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        return {"ok": True, "chunks": n}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


def health(path: str, bank: str = "") -> Dict[str, Any]:
    """一句话回答「她的记忆现在健康吗」。

    与插件治理页的「结果未知不造假」同一个口径：要么给出可核对的数量，
    要么明确说"读不到"，不把失败显示成 0。
    """
    out: Dict[str, Any] = {"ok": False, "path": path or "", "exists": bool(path and os.path.isfile(path))}
    if not out["exists"]:
        out["message"] = "还没有记忆库"
        return out
    try:
        conn = connect(path, read_only=True)
    except Exception as exc:
        out["message"] = f"打不开：{exc}"
        return out
    try:
        # / 注意：facts 表的列叫 bank，messages/chunks 也都有 bank；
        #   这里按表别名写清楚，不要写成裸 bank —— 单表查询里裸列名会撞。
        has_facts = _has_table(conn, "facts")
        if bank:
            msg = conn.execute("SELECT COUNT(*) FROM messages m WHERE m.bank=?",
                               (bank,)).fetchone()[0]
            chk = conn.execute("SELECT COUNT(*) FROM chunks c JOIN messages m "
                               "ON m.id=c.message WHERE m.bank=?", (bank,)).fetchone()[0]
            act = (conn.execute("SELECT COUNT(*) FROM facts f WHERE f.status='active' "
                                "AND f.bank=?", (bank,)).fetchone()[0]) if has_facts else 0
            sup = (conn.execute("SELECT COUNT(*) FROM facts f WHERE f.status='superseded' "
                                "AND f.bank=?", (bank,)).fetchone()[0]) if has_facts else 0
        else:
            msg = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            chk = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            act = (conn.execute("SELECT COUNT(*) FROM facts WHERE status='active'"
                                ).fetchone()[0]) if has_facts else 0
            sup = (conn.execute("SELECT COUNT(*) FROM facts WHERE status='superseded'"
                                ).fetchone()[0]) if has_facts else 0
        idx = conn.execute("SELECT COUNT(*) FROM search_index").fetchone()[0]
        orphan = conn.execute(
            "SELECT COUNT(*) FROM chunks c LEFT JOIN messages m ON m.id=c.message "
            "WHERE m.id IS NULL").fetchone()[0]
        missing = max(0, int(chk) - int(idx))
        out.update({
            "ok": True, "messages": int(msg), "chunks": int(chk), "indexed": int(idx),
            "missingIndex": missing, "orphanChunks": int(orphan),
            "facts": int(act), "superseded": int(sup),
            "schemaVersion": int(conn.execute("PRAGMA user_version").fetchone()[0]),
        })
        problems = []
        if missing:
            problems.append(f"{missing} 个片段没进索引，检索会漏")
        if orphan:
            problems.append(f"{orphan} 个孤儿片段")
        out["problems"] = problems
        out["message"] = "；".join(problems) if problems else "正常"
        return out
    except Exception as exc:
        out["message"] = f"检查失败：{exc}"
        return out
    finally:
        conn.close()


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (name,)).fetchone()
    return row is not None


# ══════════════════════════════════════════════════════════════════════════
# 命令行入口 —— bridge.mjs 调这一条拿检索结果
# ══════════════════════════════════════════════════════════════════════════
#
#     python -m bigfish.memory --recall <db> --query "文本" --maxChars 2400
#     python -m bigfish.memory --health <db>
#
# stdout 只输出一行 JSON，日志一律走 stderr（与 bridge.mjs 的约定一致）。
# 检索逻辑只有这一份：Node 侧不再有第二套排序，两边不会再漂。

def _cli(argv: Sequence[str]) -> int:
    import sys

    args = list(argv)
    cmd = ""
    db = ""
    query = ""
    max_chars = 2400
    bank = ""
    session = ""
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--recall", "--health", "--search"):
            cmd = a.lstrip("-")
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                db = args[i + 1]
                i += 1
        elif a == "--query" and i + 1 < len(args):
            query = args[i + 1]
            i += 1
        elif a == "--maxChars" and i + 1 < len(args):
            try:
                max_chars = max(200, min(20000, int(args[i + 1])))
            except ValueError:
                pass
            i += 1
        elif a == "--bank" and i + 1 < len(args):
            bank = args[i + 1]
            i += 1
        elif a == "--session" and i + 1 < len(args):
            # 只检索这一个会话的原话。bridge.mjs 会把当前 chat_id 拼成
            # `wechat:<chat_id>` 传下来 —— 不传就是全库（老行为）。
            session = args[i + 1]
            i += 1
        i += 1

    if cmd == "health":
        print(json.dumps(health(db, bank), ensure_ascii=False))
        return 0
    if not db or not query:
        print(json.dumps({"ok": False, "error": "缺少 db 或 query"}, ensure_ascii=False))
        return 2
    items = search_chunks(db, bank or _default_bank(db), query,
                          max_chars=max_chars, session=session)
    print(json.dumps({
        "ok": bool(items),
        "text": render_recall(items),
        "ids": [it["chunk"] for it in items],
        "diagnostics": {"mode": "hybrid", "hits": len(items),
                        "chars": sum(len(it["text"]) for it in items)},
    }, ensure_ascii=False))
    return 0


def _default_bank(db_path: str) -> str:
    """库里的 bank 一般是 dafeiyu；查不到就用它，查得到就取最常见的那个。"""
    try:
        conn = connect(db_path, read_only=True)
        try:
            row = conn.execute(
                "SELECT bank FROM messages GROUP BY bank ORDER BY COUNT(*) DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else "dafeiyu"
        finally:
            conn.close()
    except Exception:
        return "dafeiyu"


if __name__ == "__main__":  # pragma: no cover - 由 bridge.mjs 调用
    import sys

    raise SystemExit(_cli(sys.argv[1:]))
