"""[bigfish] 请求快照 —— 她这一轮到底看到了什么。

为什么要有这一层
────────────────
0.5.0 之后能配 19 家渠道 / 3 种协议。出问题时最费时间的不是"改"，是**分清
是哪一类问题**：

  · 她答得驴唇不对马嘴 —— prompt 变了，还是模型换了？
  · 某个渠道一直空回复 —— 是这家 API 的格式差异，还是 max_tokens 不够？
  · 记忆明明有，她却不提 —— 检索没命中，还是注入被截断了？

没有快照时只能靠日志猜；有快照时，把那一轮的 system / user / 响应 / 耗时
摊开看，五秒定位。

设计约束
────────
  · **旁路**：写快照失败绝不影响聊天。所有对外函数不抛异常。
  · **脱敏**：只写 base_url，绝不写 api_key（gemini 的 key 在 query 里，
    我们记录的是 cfg.eff_base_url，不含 key）。
  · **有界**：默认保留最近 200 份，按修改时间滚动删除。
  · **可关**：`config.json` 的 `trace.enabled=false` 即完全停写。

目录：`<数据根>/prompts/<YYYYMMDD-HHMMSS-mmm>-<chat>.json`
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from itertools import count
from typing import Any, Dict, List, Optional

#: 默认保留份数（滚动删除最旧的）
DEFAULT_KEEP = 200

_write_lock = threading.Lock()
#: 同一毫秒内多次写入时用它保证文件名严格递增（否则滚动会删错）
_seq = count(1)


# ══════════════════════════════════════════════════════════════════
#  路径与配置
# ══════════════════════════════════════════════════════════════════
def _data_root() -> str:
    """数据根（与 memory.sqlite3 / mood 同一处）。失败退回程序目录。"""
    try:
        from bigfish.vault import resolve_data_root

        root = resolve_data_root()
        if root:
            return root
    except Exception:
        pass
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def trace_dir() -> str:
    return os.path.join(_data_root(), "prompts")


def _config() -> Dict[str, Any]:
    """读 config.json 的 trace 段。读不到就用默认值。"""
    try:
        p = os.path.join(os.path.dirname(_data_root()), "config.json")
        if not os.path.isfile(p):
            p = os.path.join(_data_root(), "config.json")
        if not os.path.isfile(p):
            return {}
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d.get("trace") or {}
    except Exception:
        return {}


def enabled() -> bool:
    return bool(_config().get("enabled", True))


def keep_count() -> int:
    try:
        return max(10, int(_config().get("keep", DEFAULT_KEEP)))
    except Exception:
        return DEFAULT_KEEP


# ══════════════════════════════════════════════════════════════════
#  写快照
# ══════════════════════════════════════════════════════════════════
def _safe_name(s: str, limit: int = 24) -> str:
    s = re.sub(r"[^0-9A-Za-z_\-]+", "_", str(s or "")).strip("_")
    return (s or "chat")[:limit]


def _sections(meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把调用方给的注入段落整理成 [{seat,name,chars}]。"""
    raw = meta.get("sections")
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            out.append({
                "seat": item.get("seat", 0),
                "name": str(item.get("name") or "")[:40],
                "chars": int(item.get("chars") or 0),
            })
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append({"seat": item[0], "name": str(item[1])[:40],
                        "chars": int(item[2]) if len(item) > 2 else 0})
    return out


def _payload(cfg: Any, system: str, user: str, response: str,
             elapsed_ms: float, meta: Dict[str, Any],
             error: Optional[Any]) -> Dict[str, Any]:
    now = time.time()
    return {
        "ts": int(now),
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "chat": str(meta.get("chat") or ""),
        "owner": str(meta.get("owner") or ""),
        "provider": str(getattr(cfg, "channel", "") or ""),
        "protocol": str(getattr(cfg, "eff_protocol", "") or ""),
        "model": str(getattr(cfg, "eff_model", "") or ""),
        "base_url": str(getattr(cfg, "eff_base_url", "") or ""),
        "temperature": getattr(cfg, "temperature", None),
        "max_tokens": getattr(cfg, "max_tokens", None),
        "system_chars": len(system or ""),
        "user_chars": len(user or ""),
        "sections": _sections(meta),
        "system": system or "",
        "user": user or "",
        "response": response or "",
        "elapsed_ms": int(elapsed_ms or 0),
        "error": None if error is None else {
            "kind": getattr(error, "kind", "generic"),
            "status": getattr(error, "status_code", 0),
            "message": str(error)[:500],
        },
    }


def _write(payload: Dict[str, Any], path: str) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        pass


def _prune(keep: int) -> None:
    """删最旧的，只留 keep 份。

    排序按**文件名**（`YYYYMMDD-HHMMSS-mmm-...`），不按 mtime ——
    Windows 的 mtime 精度只有 100ms 左右，同一批写入会撞在一起，
    按 mtime 排会把刚写的那份当成最旧的删掉。
    """
    try:
        d = trace_dir()
        files = [n for n in os.listdir(d) if n.endswith(".json")]
        if len(files) <= keep:
            return
        files.sort()
        for n in files[: len(files) - keep]:
            try:
                os.remove(os.path.join(d, n))
            except OSError:
                pass
    except Exception:
        pass


def record(*, cfg: Any, system: str, user: str, response: str = "",
           elapsed_ms: float = 0.0, meta: Optional[Dict[str, Any]] = None,
           error: Optional[Any] = None) -> Optional[str]:
    """写一份快照，返回路径（未写则 None）。**不抛异常**。"""
    try:
        if not enabled():
            return None
        meta = meta or {}
        now = time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        stamp += f"-{int((now % 1) * 1000):03d}-{next(_seq) % 100000:05d}"
        name = f"{stamp}-{_safe_name(meta.get('chat'))}.json"
        path = os.path.join(trace_dir(), name)
        payload = _payload(cfg, system, user, response, elapsed_ms, meta, error)

        # 后台写：快照是旁路，不该让聊天等磁盘
        def work() -> None:
            with _write_lock:
                _write(payload, path)
                _prune(keep_count())

        threading.Thread(target=work, name="bigfish-trace",
                         daemon=True).start()
        return path
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════
#  查
# ══════════════════════════════════════════════════════════════════
def recent(limit: int = 20) -> List[Dict[str, Any]]:
    """最近 N 份快照的摘要（不含正文，给界面列表用）。"""
    out: List[Dict[str, Any]] = []
    try:
        d = trace_dir()
        if not os.path.isdir(d):
            return out
        files = [os.path.join(d, n) for n in os.listdir(d)
                 if n.endswith(".json")]
        files.sort(key=lambda p: os.path.basename(p), reverse=True)
        for p in files[: max(1, int(limit))]:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    d0 = json.load(f)
            except Exception:
                continue
            out.append({
                "path": p,
                "time": d0.get("time", ""),
                "chat": d0.get("chat", ""),
                "provider": d0.get("provider", ""),
                "model": d0.get("model", ""),
                "elapsed_ms": d0.get("elapsed_ms", 0),
                "system_chars": d0.get("system_chars", 0),
                "user_chars": d0.get("user_chars", 0),
                "response_chars": len(d0.get("response") or ""),
                "error": (d0.get("error") or {}).get("kind") or "",
            })
    except Exception:
        pass
    return out


def sections_of(path: str) -> List[Dict[str, Any]]:
    """读一份快照的段落表（只要段名和字数，不要正文）。

    给设置页回答「这一轮的字数花在哪了」。以前只给一个总字数，
    看到 system 8000 字也不知道是哪一段涨的。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            d0 = json.load(f)
    except Exception:
        return []
    raw = d0.get("sections")
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        out.append({
            "seat": int(s.get("seat") or 0),
            "name": str(s.get("name") or "")[:40],
            "chars": int(s.get("chars") or 0),
        })
    return out


def stats() -> Dict[str, Any]:
    """给界面看的一行摘要：开了没、存了几份、多大、**能不能写**。"""
    d = trace_dir()
    files: List[str] = []
    try:
        if os.path.isdir(d):
            files = [os.path.join(d, n) for n in os.listdir(d)
                     if n.endswith(".json")]
    except Exception:
        pass
    total = 0
    for p in files:
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    writable, werr = probe()
    return {"enabled": enabled(), "dir": d, "count": len(files),
            "bytes": total, "keep": keep_count(),
            "writable": writable, "write_error": werr}


def probe() -> tuple:
    """真写一次探针，判断快照目录能不能落盘。

    为什么需要它：exe 未签名时，某些安全策略会拒绝对既有目录的写入
    （实测：同一目录 python 能写、BigFish.exe 不能）。这时快照会**静默**
    写不进去 —— 用户只会觉得"这里怎么一直是空的"。
    """
    d = trace_dir()
    p = os.path.join(d, ".probe.tmp")
    try:
        os.makedirs(d, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("x")
    except Exception as exc:
        return False, str(exc)
    try:
        os.remove(p)
    except OSError:
        pass
    return True, ""
