"""[bigfish] 灵魂人格的读取与渲染。

唯一的人格来源是 DSH 大肥鱼灵魂插件的数据文件：`soul-<短键>.json`。
本模块**只负责读和渲染**，写入在 bigfish/vault.py。

设计原则（沿用 dsh-bigfish 的 D-4 决策）：
    render, don't hold —— 从灵魂数据**生成**人格文本，而不是手抄一份。
    手抄的副本会漂移；生成的不会。

    ⚠ 这条原则以前被自己违反了：TRAIT_DOC 在这里抄了一份，四档文案
      和插件不一致（温暖度中写成了「明显关心，会主动问候」，
      插件的 mid 是「关心，但不黏人」）。结果是同一个数值，
      微信里说她是"黏人的"，DSH 里说她是"不黏人的"。
      现在文案只有 vault.TRAIT_DOC 一份，这里直接引用。

如果找不到灵魂文件，调用方应当**明确报错**，不要用内置兜底人格顶替——
那会造出第二个人格源，正是要避免的事。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from bigfish.vault import (
    TRAIT_DOC,
    default_soul,
    existing_path,
    resolve_data_root,
    soul_path,
    trait_sentence,
)

# 兼容旧名字：以前这里叫 TRAIT_DOC 且结构是 {label, low, mid, high}，现在同构
__all__ = [
    "Soul", "TRAIT_DOC", "load_soul", "find_soul_file",
    "default_soul", "soul_file_path", "render_persona_of",
]


@dataclass
class Soul:
    """灵魂数据的内存视图。字段与 soul-*.json 一一对应。"""

    name: str = ""
    address: str = ""
    aliases: str = ""
    who_i_am: str = ""
    values: List[str] = field(default_factory=list)
    values_raw: List[Dict[str, Any]] = field(default_factory=list)
    traits: Dict[str, int] = field(default_factory=dict)
    self_notes: List[str] = field(default_factory=list)
    #: 灵魂的演化年表（复盘每天往这里写一条）。**不参与 render_persona** ——
    #: 原因见 `history_lines()` 的注释：Node 侧不渲染它，Python 侧渲染了
    #: 就会破坏两侧人格一致性（tools/test_persona_parity.py）。
    history: List[Dict[str, Any]] = field(default_factory=list)
    revision: int = 0
    source_path: str = ""
    scope: str = ""

    # ------------------------------------------------------------------
    def trait_lines(self) -> List[str]:
        """把 traits 渲染成人格描述行。

        ⚠ 不输出数字 —— 数值对模型没有语义，它会自行脑补。

        ⚠ 只渲染**文件里确实有**的 trait 键（与 bridge.mjs 一致）。
          以前这里是遍历 TRAIT_DOC 全表，缺失的键用默认值补上 ——
          结果同一个文件，Python 侧多渲染出三条性格、Node 侧不渲染，
          两侧人格文本就不一样了。归一化阶段已经保证落盘的文件
          四个键齐全，所以正常情况下不会缺；但**渲染时**必须诚实反映
          文件内容，否则"她感受到了什么"取决于你从哪条路问她。
        """
        out: List[str] = []
        for key in TRAIT_DOC:
            v = self.traits.get(key)
            if v is None:
                continue
            sentence = trait_sentence(key, v)
            if sentence:
                out.append(f"- {sentence}")
        return out

    def render_persona(self) -> str:
        """渲染成可直接喂给模型的系统提示片段。

        顺序刻意固定：身份 → 珍视 → 性格 → 自己知道的事。
        人格决定「我是谁」，不承载输出格式规则（那些属于协议层）。
        """
        parts: List[str] = []

        header = f"你是{Soul._quote(self.name)}" if self.name else "你是"
        alias = f"对方平时也叫你「{self.aliases}」。" if self.aliases else ""
        if self.address:
            header += f"，称呼对方「{self.address}」。{alias}"
        else:
            header += f"。{alias}"
        parts.append(header)

        if self.who_i_am.strip():
            parts.append("\n## 我是谁\n\n" + self.who_i_am.strip())

        if self.values:
            parts.append("\n## 我珍视的\n\n" +
                         "\n".join(f"- {v}" for v in self.values))

        tl = self.trait_lines()
        if tl:
            parts.append("\n## 我的性格\n\n" + "\n".join(tl))

        if self.self_notes:
            parts.append("\n## 我自己知道的事\n\n" +
                         "\n".join(f"- {n}" for n in self.self_notes))

        return "\n".join(parts).strip()

    @staticmethod
    def _quote(s: str) -> str:
        return f"「{s}」" if s else ""

    def week_line(self) -> str:
        """「这一周」的一句话。没有就返回空串（调用方整段不出现）。

        和 `history_lines()` 分开的原因：一个是"以前"（日），
        一个是"这一周"（周）。混在一段里，读起来像"这周也在以前里"，
        而且周叙事更近、更该单独占一个座位（861）。
        """
        for item in reversed(self.history or []):
            if not isinstance(item, dict):
                continue
            if str(item.get("kind") or "") != "week_summary":
                continue
            week = item.get("week") if isinstance(item.get("week"), dict) else {}
            text = " ".join(str(week.get("week") or "").split())[:60]
            if not text:
                continue
            day = str(item.get("day") or "").strip()
            return f"（{day}）这周：{text}" if day else f"这周：{text}"
        return ""

    def history_lines(self, limit: int = 8, max_chars: int = 250) -> List[str]:
        """「以前」——她自己过过的那些天，压成几行。

        为什么只取 `day_summary`、不取 `evolved`：
          evolved 已经进了 `selfNotes`（"（11-11）关于工作：以前…现在…"），
          这里再渲染一遍就是同一句话出现两次 —— 占预算，也让她像在复读。
          `day_summary` 目前**没有任何注入路径**（复盘写了、界面看了、
          她本人读不到），这条是补上"她有以前"的最小缺口。

        为什么是 8 条、250 字：
          8 天约等于"最近一周半"，够回答"你这几天干嘛了"；
          250 字约 150 token，相对 2400 字的记忆预算只占一成。

        为什么不在 `render_persona()` 里：
          人格文本要过 `tools/test_persona_parity.py`（Python 与 Node
          必须逐字一致），Node 侧没有这段。所以它由调用方（desktop 的
          compose）作为**独立座位 862** 注入，两侧各自决定要不要带。
        """
        out: List[str] = []
        used = 0
        for item in reversed(self.history or []):
            if not isinstance(item, dict):
                continue
            if str(item.get("kind") or "") != "day_summary":
                continue
            text = " ".join(str(item.get("text") or "").split())
            if not text:
                continue
            day = str(item.get("day") or "").strip()
            line = f"（{day}）那天：{text[:40]}" if day else f"那天：{text[:40]}"
            if used + len(line) > max_chars:
                break
            out.append(line)
            used += len(line)
            if len(out) >= limit:
                break
        return list(reversed(out))

    @classmethod
    def from_dict(cls, d: Dict[str, Any], path: str = "") -> "Soul":
        """从原始 JSON 构造。

        ⚠ 这里**不做归一化**，只做类型安全。
          以前走 normalize_soul，它会给缺失的 trait 填默认值 ——
          于是「文件里没有 warmth」到了渲染层变成「温暖：关心，但不黏人」，
          而 Node 侧渲染出来什么都没有。两侧就不是同一个人格了。

          归一化是**写**的时候的事（vault.write_soul_file）；
          **读**的时候必须原样反映文件内容，缺就是缺。
        """
        src = d if isinstance(d, dict) else {}
        ident = src.get("identity") if isinstance(src.get("identity"), dict) else {}

        raw_values = src.get("values")
        values_raw: List[Dict[str, Any]] = []
        if isinstance(raw_values, list):
            for i, v in enumerate(raw_values):
                if isinstance(v, str):
                    if v.strip():
                        values_raw.append({"id": f"v{i + 1}", "text": v.strip(),
                                           "locked": False})
                elif isinstance(v, dict):
                    text = v.get("text")
                    if isinstance(text, str) and text.strip():
                        values_raw.append({
                            "id": str(v.get("id") or f"v{i + 1}"),
                            "text": text.strip(),
                            "locked": v.get("locked") is not False,
                        })

        traits: Dict[str, int] = {}
        raw_traits = src.get("traits")
        if isinstance(raw_traits, dict):
            for k, v in raw_traits.items():
                if k not in TRAIT_DOC:
                    continue
                if isinstance(v, bool):
                    continue          # bool 是 int 的子类，但这里明显是脏数据
                if isinstance(v, (int, float)):
                    traits[k] = max(0, min(100, int(round(v))))

        notes = [str(x).strip() for x in (src.get("selfNotes") or [])
                 if isinstance(x, str) and str(x).strip()]

        # 年表原样保留（读不归一化）；脏条目在 history_lines() 里跳过，
        # 而不是在这里删 —— 读的时候改结构，写回去就丢数据了。
        history = [x for x in (src.get("history") or []) if isinstance(x, dict)]

        try:
            revision = int(src.get("revision") or 0)
        except (TypeError, ValueError):
            revision = 0

        return cls(
            name=str(ident.get("name") or "").strip(),
            address=str(ident.get("address") or "").strip(),
            aliases=str(ident.get("aliases") or "").strip(),
            who_i_am=str(ident.get("whoIAm") or "").strip(),
            values=[v["text"] for v in values_raw],
            values_raw=values_raw,
            traits=traits,
            self_notes=notes,
            history=history,
            revision=revision,
            source_path=path,
            scope=str(src.get("scope") or ""),
        )


# ----------------------------------------------------------------------
# 定位与加载
# ----------------------------------------------------------------------
def soul_file_path(root: Optional[str] = None, key: Optional[str] = None) -> str:
    """灵魂文件路径。整个数据目录只有一份 <数据根>/soul.json。

    `key` 参数留着只为兼容老调用点，已无意义。
    新布局没有这个文件、但老布局（ws-*/*.json）有时，返回老路径 ——
    老用户升级后还能看见自己的人格。
    """
    root = root or resolve_data_root()
    if not root:
        return ""
    return existing_path(root, "soul")


def find_soul_file(root: Optional[str] = None) -> Optional[str]:
    """找一个真正可用的灵魂文件。找不到返回 None。"""
    root = root or resolve_data_root()
    if not root:
        return None
    import os

    p = existing_path(root, "soul")
    if p and os.path.isfile(p):
        return p
    return None


def load_soul(root: Optional[str] = None) -> Soul:
    """加载灵魂数据。找不到文件时抛 FileNotFoundError，绝不返回兜底人格。"""
    root = root or resolve_data_root()
    path = find_soul_file(root)
    if not path:
        raise FileNotFoundError(
            "还没有灵魂文件。\n"
            f"应该在：{soul_path(root)}\n\n"
            "程序启动时会自动创建（优先从 DSH 导入，没有就用出厂模板）。\n"
            "如果这里是空的，可能是数据目录不可写 —— 到「数据目录」页看看原因。"
        )
    import json

    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    return Soul.from_dict(d, path)


def render_persona_of(soul_dict: Dict[str, Any]) -> str:
    """给非 Soul 调用方（bridge、UI 预览）用的快捷函数。"""
    return Soul.from_dict(soul_dict).render_persona()


if __name__ == "__main__":
    import sys

    for _s in (sys.stdout, sys.stderr):
        if _s is not None:
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    s = load_soul()
    print(f"来源: {s.source_path}")
    print(f"revision: {s.revision}")
    print("=" * 70)
    print(s.render_persona())
