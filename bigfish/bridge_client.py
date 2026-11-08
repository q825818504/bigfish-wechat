"""[bigfish] 灵魂桥接 —— 调用 Node 侧 bridge.mjs，拿完整的人格/情绪/记忆。

为什么绕一层 Node：
    dsh-bigfish 的记忆检索有中文二元分词、半衰期衰减、FTS5 索引、
    session scope 过滤一整套逻辑。在 Python 里重写必然产生偏差，
    结果是「微信里的大肥鱼」和「DSH 里的大肥鱼」记得不一样的事。
    bridge.mjs 直接 import 原版模块调用，逻辑零偏差。

设计：bridge.mjs 只读不写。DSH 那边同时在跑也安全。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from bigfish.paths import app_root, res_root  # noqa: E402

_HERE = os.path.join(res_root(), "bigfish")
_ROOT = res_root()

# 打包后 bridge/ 在 exe 同级目录，不在包内。
# 依次尝试：安装目录 / 仓库根 / 包目录。
def _find_bridge() -> str:
    cands = []
    if getattr(sys, "frozen", False):
        cands.append(os.path.dirname(sys.executable))
    cands += [_ROOT, _HERE]
    for base in cands:
        p = os.path.join(base, "bridge", "bridge.mjs")
        if os.path.isfile(p):
            return p
    return os.path.join(_ROOT, "bridge", "bridge.mjs")


_BRIDGE = _find_bridge()


@dataclass
class Mood:
    """情绪状态（来自 mood-ws-*.json）。"""

    value: Optional[float] = None
    dims: Dict[str, float] = field(default_factory=dict)
    last_reason: str = ""
    injected_text: str = ""
    updated_at: int = 0

    @property
    def label(self) -> str:
        """把数值翻成一句人话，便于模型理解。"""
        v = self.value
        if v is None:
            return ""
        if v >= 85:
            return "心情很好"
        if v >= 70:
            return "心情不错"
        if v >= 55:
            return "心情还行"
        if v >= 40:
            return "有点平淡"
        if v >= 25:
            return "情绪低"
        return "情绪很低"

    def active_dims(self, threshold: float = 15.0) -> List[str]:
        """活跃维度，带上强度。

        ⚠ 量纲是 **0..100**，不是 0..1。
          `mood_judge` 判定时给的是 -10..10 的增量，累加后 clamp 到 0..100；
          这里以前按 0..1 处理（阈值 0.15 + `{v:.0%}`），于是
          `happy=45` 会被格式化成 "4500%"，而 `worry=3` 也被判成活跃。
          提示词里出现过「偏开心7000%、甜蜜3500%」——比不给情绪更伤，
          因为她会照着这个比例去演。
        """
        names = {
            "happy": "开心", "calm": "平静", "sweet": "甜",
            "heartache": "心軟", "worry": "担心",
            "sad": "难过", "jealous": "吃醋",
        }
        out = []
        for k, v in sorted(self.dims.items(), key=lambda x: -x[1]):
            if v >= threshold:
                out.append(f"{names.get(k, k)}{v:.0f}")
        return out


@dataclass
class Context:
    """一次对话所需的全部上下文。"""

    persona: str = ""
    mood: Optional[Mood] = None
    memory_text: str = ""
    memory_ids: List[int] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    scope_id: str = ""
    #: 记忆降级状态："" 正常 / "recent" 用了本地最近原文 / "empty" 记忆不可用
    degraded: str = ""

    def render_system(self) -> str:
        """渲染成 system prompt：人格 + 情绪底色。"""
        parts = [self.persona] if self.persona else []
        if self.mood and self.mood.injected_text.strip():
            # lastInjectedText 是 DSH 自己生成的注入文本，
            # 它明确说了「别把这段念出来」，直接复用比我们另编一段更准。
            parts.append("\n## 这会儿的状态\n\n" + self.mood.injected_text.strip())
        elif self.mood and self.mood.value is not None:
            dims = self.mood.active_dims()
            line = f"- 心情：{self.mood.label}（{self.mood.value:.0f}/100）"
            if dims:
                line += f"，偏{'、'.join(dims)}"
            parts.append("\n## 这会儿的状态\n\n" + line)
        return "\n".join(parts).strip()

    def render_memory(self) -> str:
        """把检索到的记忆渲染成可读片段。"""
        if not self.memory_text:
            return ""
        try:
            items = json.loads(self.memory_text)
        except (json.JSONDecodeError, TypeError):
            return self.memory_text
        lines = []
        for it in items:
            if not isinstance(it, dict):
                continue
            who = "我" if it.get("role") == "assistant" else "对方"
            text = str(it.get("text") or "").replace("\n", " ")[:160]
            if text:
                lines.append(f"- {who}：{text}")
        return "\n".join(lines)


def find_node() -> Optional[str]:
    for name in ("node", "node.exe"):
        p = shutil.which(name)
        if p:
            return p
    for cand in (
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Program Files (x86)\nodejs\node.exe",
    ):
        if os.path.isfile(cand):
            return cand
    return None


def _load_context_bridge(query: str = "", data_root: str = "",
                         scope: str = "", timeout: float = 25.0,
                         chat: str = "") -> Context:
    """调用 bridge.mjs 取上下文。

    任何失败都返回带 warnings 的空 Context，不抛异常——
    微信这条线上宁可少一点上下文，也不能因为记忆检索崩掉整轮对话。
    """
    ctx = Context()

    node = find_node()
    if not node:
        ctx.warnings.append("找不到 node，跳过灵魂/情绪/记忆")
        return ctx
    if not os.path.isfile(_BRIDGE):
        ctx.warnings.append(f"找不到桥接脚本: {_BRIDGE}")
        return ctx

    root = data_root or _default_data_root()
    if not root:
        ctx.warnings.append("找不到 DSH dataRoot 配置")
        return ctx

    cmd = [node, _BRIDGE, "context", "--root", root]
    if scope:
        cmd += ["--scope", scope]
    if chat:
        cmd += ["--chat", chat]
    if query:
        cmd += ["--query", query]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        ctx.warnings.append("桥接超时")
        return ctx
    except OSError as exc:
        ctx.warnings.append(f"启动桥接失败: {exc}")
        return ctx

    raw = (r.stdout or "").strip()
    if not raw:
        ctx.warnings.append(f"桥接无输出 stderr={r.stderr[:200]}")
        return ctx
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        ctx.warnings.append(f"桥接输出不是 JSON: {raw[:200]}")
        return ctx

    ctx.persona = d.get("persona") or ""
    ctx.scope_id = d.get("scopeId") or ""
    ctx.warnings.extend(d.get("warnings") or [])

    m = d.get("mood")
    if isinstance(m, dict):
        ctx.mood = Mood(
            value=m.get("mood"),
            dims={k: float(v) for k, v in (m.get("dims") or {}).items()
                  if isinstance(v, (int, float))},
            last_reason=m.get("lastReason") or "",
            injected_text=m.get("lastInjectedText") or "",
            updated_at=int(m.get("updatedAt") or 0),
        )

    mem = d.get("memory")
    if isinstance(mem, dict):
        ctx.memory_text = mem.get("text") or ""
        ctx.memory_ids = list(mem.get("ids") or [])

    return ctx


def _local_memory(query: str, chat: str = "") -> Tuple[str, str]:
    """L2：桥接拿不到记忆时，用本地 Python 记忆库兜底。

    返回 (memory_text_json, level)。level: "" / "recent" / "empty"。

    ⚠ `chat` 是必须传的：这条兜底会走 `search_with_fallback`，
      而它的降级分支是"全库最近 N 条原文"。不传会话就是拿别人的私聊
      兜底 —— 比正常检索更容易串人。
    """
    try:
        from bigfish import memory as M, vault as V

        db = V.memory_db_path()
        if not (db and os.path.isfile(db)):
            return "", "empty"
        bank = ""
        try:
            bank = M._default_bank(db) or ""
        except Exception:
            bank = ""
        items, _level = M.search_with_fallback(
            db, bank or "dafeiyu", query or "", limit=8, max_chars=2400,
            session=f"wechat:{chat}" if chat else "")
        if not items:
            # 桥接挂了、本地也没捞到 → 这一轮只能空记忆
            return "", "empty"
        text = json.dumps(
            [{"role": it.get("role"), "text": it.get("text")} for it in items],
            ensure_ascii=False)
        # 只要走到这里就是"本地兜底"，不管本地是精确命中还是最近原文
        return text, "recent"
    except Exception:
        return "", "empty"


def load_context(query: str = "", data_root: str = "",
                 scope: str = "", timeout: float = 25.0,
                 chat: str = "") -> Context:
    """取上下文；桥接失败时自动降级到本地记忆。

    degraded: "" 正常 / "recent" 用了本地最近原文 / "empty" 记忆不可用

    ⚠ `chat` 必须由调用方给（`chat_id`）。它只用于一件事：
      把记忆检索收窄到"当前这个会话"。不给就是全库检索，
      跟 B 聊天时能捞到 A 的私聊原话 —— README 承诺过不会这样。
    """
    ctx = _load_context_bridge(query=query, data_root=data_root,
                               scope=scope, timeout=timeout, chat=chat)
    # 桥接成功且有记忆，或桥接没报错（只是没命中）→ 不降级
    if ctx.memory_text or not ctx.warnings:
        return ctx
    text, level = _local_memory(query, chat)
    if text:
        ctx.memory_text = text
        ctx.warnings.append("桥接无记忆，已用本地原文兜底")
    ctx.degraded = level
    return ctx


def _default_data_root() -> str:
    """读数据根。统一走 vault.resolve_data_root —— 与插件同一套规则。

    以前这里是"猜 D:\\大肥鱼\\灵魂"，猜不中就返回空串。用户改了数据根之后
    微信那边就再也读不到灵魂，还以为是插件坏了。
    """
    from bigfish.vault import resolve_data_root

    return resolve_data_root()


def _scope_dir_of(root: str, scope: str = "") -> Optional[str]:
    """当前范围的目录。优先按 scope.json 推，推不出再扫。"""
    _ = scope      # 老参数，新布局没有范围概念
    from bigfish.vault import legacy_layout

    lg = legacy_layout(root)
    return (lg or {}).get("dir") or None


def _pick_scope_dir(root: str) -> Optional[str]:
    """在 dataRoot 下挑出真正有数据的 workspace 目录。

    实测：dataRoot 里可能有多个 ws-* 目录（历史工作区），大多数是空的。
    判据是**有没有 soul-*.json** —— 灵魂是唯一必存的文件，
    有灵魂的才是正在用的工作区。
    """
    import glob

    dirs = sorted(glob.glob(os.path.join(root, "ws-*")))
    dirs = [d for d in dirs if os.path.isdir(d)]
    if not dirs:
        return None

    with_soul = [d for d in dirs if glob.glob(os.path.join(d, "soul-*.json"))]
    if with_soul:
        # 多个都有灵魂时取最近修改的那个
        with_soul.sort(
            key=lambda d: max(
                (os.path.getmtime(f) for f in glob.glob(os.path.join(d, "*"))),
                default=0), reverse=True)
        return with_soul[0]
    return dirs[0]


def load_mood_only(data_root: str = "") -> Optional[Mood]:
    """只读情绪状态，不触发记忆检索。

    界面上的「心情」页用它——那里不需要检索开销。
    路径解析走 vault，保证和灵魂、记忆是**同一个范围**。
    """
    from bigfish.vault import existing_path

    root = data_root or _default_data_root()
    if not root:
        return None

    path = existing_path(root, "mood")
    if not path or not os.path.isfile(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return None

    moods = d.get("moods") or {}
    if not isinstance(moods, dict) or not moods:
        return None
    # 先按 lastActive 取，取不到退回任意一份。
    # ⚠ 以前这里写的是 `moods.get(key) or ...` —— `key` 是上一版
    #   「按范围分家」遗留下来的变量，函数签名里早就没有它，于是
    #   **只要 mood.json 里真有数据，这一行就 NameError**。
    #   因为之前测试环境里 mood.json 一直是空的（if not moods: return None
    #   先短路了），这个 bug 从来没暴露过。真实用户一有心情数据就炸，
    #   而炸点在"心情页"和自检里，会被误当成"心情功能坏了"。
    active = str(d.get("lastActive") or "")
    m = moods.get(active) or next(iter(moods.values()), None)
    if not isinstance(m, dict):
        return None
    return Mood(
        value=m.get("mood"),
        dims={k: float(v) for k, v in (m.get("dims") or {}).items()
              if isinstance(v, (int, float))},
        last_reason=m.get("lastReason") or "",
        injected_text=m.get("lastInjectedText") or "",
        updated_at=int(m.get("updatedAt") or 0),
    )


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        if _s is not None:
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    q = sys.argv[1] if len(sys.argv) > 1 else "爸爸"
    c = load_context(query=q)

    print("=" * 74)
    print(f"scope: {c.scope_id}")
    print(f"warnings: {c.warnings}")
    print("=" * 74)
    print("---- system ----")
    print(c.render_system())
    print()
    print("---- memory ----")
    print(c.render_memory() or "（无）")
