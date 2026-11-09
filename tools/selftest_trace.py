"""请求快照回归测试 —— T-2。

跑法：
    python tools/selftest_trace.py

不需要网络。用临时目录，不碰真实 data/prompts。

覆盖：
  · 写出的快照字段齐全（system / user / 响应 / 耗时 / provider / model）
  · **绝不写 api_key**（脱敏）
  · 失败也留一份（error.kind / status / message）
  · enabled=false 时一份都不写
  · 滚动：超过 keep 份只留最新的
  · recent() / stats() 能列出来
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import prompt_trace as T  # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  [X] {name}  {detail}")


def _wait_files(n: int = 1, timeout: float = 3.0) -> list[str]:
    """等后台线程把文件写出来。"""
    end = time.time() + timeout
    while time.time() < end:
        files = _files()
        if len(files) >= n:
            return files
        time.sleep(0.05)
    return _files()


def _files() -> list[str]:
    d = T.trace_dir()
    if not os.path.isdir(d):
        return []
    return [os.path.join(d, n) for n in os.listdir(d) if n.endswith(".json")]


class Cfg:
    channel = "deepseek"
    eff_protocol = "openai"
    eff_model = "deepseek-v4-flash"
    eff_base_url = "https://api.deepseek.com/v1"
    temperature = 1.0
    max_tokens = 2600
    api_key = "sk-SECRET-should-never-be-written"


def main() -> int:
    print("请求快照回归测试（bigfish/prompt_trace.py）")
    tmp = tempfile.mkdtemp(prefix="bigfish-trace-")
    T._data_root = lambda: tmp          # type: ignore[assignment]
    T._config = lambda: {}              # type: ignore[assignment]

    # ── 1. 正常写一份 ──
    print("\n== 写快照 ==")
    p = T.record(cfg=Cfg(), system="你是大肥鱼", user="在吗",
                 response="在呢", elapsed_ms=1234,
                 meta={"chat": "wxid_abc", "owner": "wxid_abc",
                       "sections": [(860, "灵魂", 120), (850, "记忆", 300)]})
    files = _wait_files(1)
    check("返回了路径", bool(p), str(p))
    check("文件落盘", len(files) == 1, str(files))
    d = {}
    if files:
        with open(files[0], "r", encoding="utf-8") as f:
            d = json.load(f)
    check("system 完整", d.get("system") == "你是大肥鱼", str(d.get("system")))
    check("user 完整", d.get("user") == "在吗", str(d.get("user")))
    check("响应完整", d.get("response") == "在呢", str(d.get("response")))
    check("耗时记录", d.get("elapsed_ms") == 1234, str(d.get("elapsed_ms")))
    check("provider 记录", d.get("provider") == "deepseek", str(d.get("provider")))
    check("sections 记录", len(d.get("sections") or []) == 2,
          str(d.get("sections")))
    # 0.11.1：界面要按段展开，所以段名必须能从快照里读回来
    secs = T.sections_of(files[0]) if files else []
    check("sections_of 读回段名",
          [s["name"] for s in secs] == ["灵魂", "记忆"], str(secs))
    check("sections_of 带字数与座位",
          [(s["seat"], s["chars"]) for s in secs] == [(860, 120), (850, 300)],
          str(secs))
    raw = json.dumps(d, ensure_ascii=False)
    check("不含 api_key", "sk-SECRET" not in raw and "api_key" not in raw)

    # ── 2. 失败也留一份 ──
    print("\n== 失败快照 ==")
    err = type("E", (Exception,), {"kind": "http", "status_code": 500})("上游 500")
    T.record(cfg=Cfg(), system="s", user="u", response="",
             elapsed_ms=88, meta={"chat": "wxid_abc"}, error=err)
    files = _wait_files(2)
    check("多了一份", len(files) == 2, str(len(files)))
    latest = sorted(files, key=os.path.getmtime)[-1]
    with open(latest, "r", encoding="utf-8") as f:
        d2 = json.load(f)
    e = d2.get("error") or {}
    check("error.kind", e.get("kind") == "http", str(e))
    check("error.status", e.get("status") == 500, str(e))
    check("error.message", "500" in (e.get("message") or ""), str(e))

    # ── 3. 关掉就不写 ──
    print("\n== enabled=false ==")
    T._config = lambda: {"enabled": False}   # type: ignore[assignment]
    before = len(_files())
    r = T.record(cfg=Cfg(), system="s", user="u", response="x", meta={"chat": "c"})
    time.sleep(0.2)
    check("返回 None", r is None, str(r))
    check("没有新文件", len(_files()) == before, str(len(_files())))

    # ── 4. 滚动保留 ──
    print("\n== 滚动保留 ==")
    T._config = lambda: {"enabled": True, "keep": 10}   # type: ignore[assignment]
    for i in range(14):
        T.record(cfg=Cfg(), system="s", user=f"u{i}", response="x",
                 meta={"chat": f"c{i}"})
    # 写是后台线程：等最后一份落盘、且 prune 跑完（数量收敛到 keep）
    end = time.time() + 5.0
    while time.time() < end:
        names = [os.path.basename(f) for f in _files()]
        if any("-00014-" in n for n in names) and len(names) <= 10:
            break
        time.sleep(0.1)
    files = _files()
    check("不超过 keep", len(files) <= 10, str(len(files)))
    newest = sorted(files, key=os.path.basename)[-1]
    with open(newest, "r", encoding="utf-8") as f:
        check("保留的是最新的", "u13" in f.read(), os.path.basename(newest))

    # ── 5. 列表与统计 ──
    print("\n== 列表 / 统计 ==")
    rows = T.recent(5)
    check("recent 有数据", len(rows) > 0, str(len(rows)))
    check("recent 带 chat", bool(rows and rows[0].get("chat")), str(rows[:1]))
    st = T.stats()
    check("stats.count", st.get("count", 0) > 0, str(st))
    check("stats.bytes", st.get("bytes", 0) > 0, str(st))

    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'=' * 46}")
    total = PASS + FAIL
    if FAIL:
        print(f"失败 {FAIL}/{total}")
        for n in FAILED:
            print(f"  - {n}")
        return 1
    print(f"全部通过 {PASS}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
