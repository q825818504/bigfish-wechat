"""[bigfish] 灵魂人格读取与渲染。

唯一的人格来源是 DSH 大肥鱼灵魂插件的数据文件，本模块**只读不写**。

设计原则（沿用 dsh-bigfish 的 D-4 决策）：
    render, don't hold —— 从灵魂数据**生成**人格文本，而不是手抄一份。
    手抄的副本会漂移；生成的不会。

灵魂数据位置：
    由 `C:\\Users\\Alime\\.dsh\\bigfish\\root.json` 的 dataRoot 指向，
    默认 `D:\\大肥鱼\\灵魂`，其下有 `ws-<id>/soul-ws-<id>.json`。

如果找不到灵魂文件，调用方应当**明确报错**，不要用内置兜底人格顶替——
那会造出第二个人格源，正是要避免的事。
"""
from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# traits 文本表：与 dsh-bigfish/lib/soul/store.js 的 TRAIT_DOC 保持一致。
# 分档阈值：<40 低档，>60 高档，其余中档。
TRAIT_DOC: Dict[str, Dict[str, str]] = {
    "warmth": {
        "label": "温暖",
        "low": "温暖度低：克制，不主动靠近",
        "mid": "温暖度中：明显关心，会主动问候",
        "high": "温暖度高：关心但不黏人",
    },
    "assertiveness": {
        "label": "主见",
        "low": "主见度低：顺从，倾向配合对方",
        "mid": "主见度中：有自己的判断，会讲理由",
        "high": "主见度高：明确表达不同意见，不轻易改口",
    },
    "playfulness": {
        "label": "顽皮",
        "low": "顽皮度低：正经，少玩笑",
        "mid": "顽皮度中：日常爱逗，会起哄",
        "high": "顽皮度高：爱玩闹，常主动制造轻松气氛",
    },
    "directness": {
        "label": "直接",
        "low": "直接度低：说话绕，先铺垫",
        "mid": "直接度中：不绕太远，但会顾着分寸",
        "high": "直接度高：直给结论，少铺垫",
    },
}


@dataclass
class Soul:
    """灵魂数据的内存视图。字段与 soul-ws-*.json 一一对应。"""

    name: str = ""
    address: str = ""
    who_i_am: str = ""
    values: List[str] = field(default_factory=list)
    traits: Dict[str, int] = field(default_factory=dict)
    self_notes: List[str] = field(default_factory=list)
    revision: int = 0
    source_path: str = ""

    # ------------------------------------------------------------------
    def trait_lines(self) -> List[str]:
        """把 traits 数值渲染成人格描述行。"""
        out: List[str] = []
        for key, doc in TRAIT_DOC.items():
            v = self.traits.get(key)
            if v is None:
                continue
            band = "low" if v < 40 else ("high" if v > 60 else "mid")
            out.append(f"- {doc['label']}（{v}/100）：{doc[band]}")
        return out

    def render_persona(self) -> str:
        """渲染成可直接喂给模型的系统提示片段。

        顺序刻意固定：身份 → 珍视 → 性格 → 自己知道的事。
        人格决定「我是谁」，不承载输出格式规则（那些属于协议层）。
        """
        parts: List[str] = []

        header = f"你是{Soul._quote(self.name)}" if self.name else "你是"
        if self.address:
            header += f"，称呼对方「{self.address}」。"
        else:
            header += "。"
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


# ----------------------------------------------------------------------
# 定位与加载
# ----------------------------------------------------------------------
def _dsh_bigfish_root_config() -> Optional[str]:
    """读 DSH 的 bigfish root 配置。"""
    cands = [
        os.path.expandvars(r"%USERPROFILE%\.dsh\bigfish\root.json"),
        os.path.expandvars(r"%APPDATA%\dsh\bigfish\root.json"),
    ]
    for c in cands:
        if os.path.isfile(c):
            try:
                with open(c, "r", encoding="utf-8") as f:
                    data = json.load(f)
                root = data.get("dataRoot")
                if root and os.path.isdir(root):
                    return root
            except Exception:
                continue
    return None


def find_soul_file(data_root: Optional[str] = None) -> Optional[str]:
    """在 dataRoot 下找最新的 soul-ws-*.json。"""
    root = data_root or _dsh_bigfish_root_config()
    if not root:
        return None
    pattern = os.path.join(root, "ws-*", "soul-ws-*.json")
    files = glob.glob(pattern)
    if not files:
        # 兼容 dataRoot 直接就是 ws-* 目录的情况
        files = glob.glob(os.path.join(root, "soul-ws-*.json"))
    if not files:
        return None
    # 按修改时间取最新（多个工作区时以最近更新的为准）
    files.sort(key=os.path.getmtime, reverse=True)
    return files[0]


def load_soul(data_root: Optional[str] = None) -> Soul:
    """加载灵魂数据。找不到文件时抛 FileNotFoundError，绝不返回兜底人格。"""
    path = find_soul_file(data_root)
    if not path:
        raise FileNotFoundError(
            "找不到大肥鱼灵魂文件。请确认 DSH 的 bigfish 插件已初始化，"
            r"通常位于 %USERPROFILE%\.dsh\bigfish\root.json 指向的 dataRoot 下。"
        )
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)

    ident = d.get("identity") or {}
    values = []
    for v in d.get("values") or []:
        if isinstance(v, dict):
            t = v.get("text")
            if t:
                values.append(t)
        elif isinstance(v, str):
            values.append(v)

    return Soul(
        name=(ident.get("name") or "").strip(),
        address=(ident.get("address") or "").strip(),
        who_i_am=(ident.get("whoIAm") or "").strip(),
        values=values,
        traits={k: int(v) for k, v in (d.get("traits") or {}).items()
                if isinstance(v, (int, float))},
        self_notes=[str(x) for x in (d.get("selfNotes") or [])],
        revision=int(d.get("revision") or 0),
        source_path=path,
    )


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
