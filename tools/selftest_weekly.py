"""周叙事体检：due / 素材 / 解析 / 写入 / 渲染。

跑法：
    python tools/selftest_weekly.py

不调模型、不联网、不碰用户真实数据（临时目录在 .tmp/ 下）。

## 守哪几条底线

1. **不许早写**：少于 3 天轨迹、或距上次不到 7 天，都不写。
2. **允许空**：模型输出全空是合法结果（不编），`parse` 返回 None 而不是
   造一条出来。
3. **只追加**：`apply_result` 只往 `history` 末尾加一条，
   `selfNotes` / `values` / `identity` 一个字不动。
4. **失败不刷屏**：写失败/解析失败都要能 `mark_done`，否则 due 永远为真。
5. **渲染独立**：`week_line()` 只出"这一周"，`history_lines()` 只出"以前"，
   两者不互相串。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import vault  # noqa: E402
from bigfish import weekly as W  # noqa: E402
from bigfish.soul import Soul  # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []


def _pick_marks() -> tuple[str, str]:
    try:
        "\u2713".encode(sys.stdout.encoding or "utf-8")
        return "\u2713", "\u2717"
    except (UnicodeEncodeError, LookupError):
        return "[OK]", "[X]"


OK_MARK, BAD_MARK = _pick_marks()


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  {OK_MARK} {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  {BAD_MARK} {name}" + (f"  ({detail})" if detail else ""))


class _Life:
    """够用的假 life：只有 history_trace / weekly_day。"""

    def __init__(self, trace=None, weekly_day=""):
        self.history_trace = list(trace or [])
        self.weekly_day = weekly_day


def _trace(days: int, per_day: int = 3, base: float | None = None) -> list:
    base = base if base is not None else time.time()
    out = []
    for d in range(days):
        t = base - d * 86400
        day = time.strftime("%Y-%m-%d", time.localtime(t))
        for i in range(per_day):
            out.append({"at": t + i * 60, "day": day, "doing": f"第{d}天第{i}件事"})
    out.sort(key=lambda x: x["at"])
    return out


def main() -> int:
    print("=" * 60)
    print("  周叙事体检 · weekly")
    print("=" * 60)
    print()

    # ── 1. due ──
    print("── 1 · 什么时候该写 ──")
    check("2 天轨迹不写", W.due(_Life(_trace(2)), time.time()) is False)
    check("3 天轨迹写", W.due(_Life(_trace(3)), time.time()) is True)
    check("空轨迹不写", W.due(_Life([]), time.time()) is False)

    now = time.time()
    six = time.strftime("%Y-%m-%d", time.localtime(now - 6 * 86400))
    seven = time.strftime("%Y-%m-%d", time.localtime(now - 7 * 86400))
    check("上次 6 天前：不写", W.due(_Life(_trace(7), six), now) is False)
    check("上次 7 天前：写", W.due(_Life(_trace(7), seven), now) is True)
    check("上次日期坏了：按轨迹判断",
          W.due(_Life(_trace(3), "not-a-date"), now) is True)

    # ── 2. 素材 ──
    print()
    print("── 2 · 素材 ──")
    life = _Life(_trace(10, per_day=4))
    mats = W.build_materials(life, now=now)
    check("最多 7 天", len(mats["days"]) == 7, str(len(mats["days"])))
    check("每天一行", all("：" in x for x in mats["days"]))
    check("同天去重", len(mats["days"][0].split(" / ")) == 4, mats["days"][0])

    dup = [{"at": now, "day": "2026-11-11", "doing": "在看书"},
           {"at": now + 1, "day": "2026-11-11", "doing": "在看书"}]
    mats2 = W.build_materials(_Life(dup), now=now)
    check("完全重复只留一次", mats2["days"][0].count("在看书") == 1, str(mats2["days"]))

    check("2 天素材不够", W.has_material({"days": ["a", "b"]})[0] is False)
    check("3 天素材够", W.has_material({"days": ["a", "b", "c"]})[0] is True)

    # ── 3. 请求与解析 ──
    print()
    print("── 3 · 请求与解析 ──")
    sys_p, user_p = W.build_request(mats)
    check("请求含轨迹", "这七天的轨迹" in user_p)
    check("请求不超预算", len(user_p) <= W.MAX_CHARS, str(len(user_p)))

    ok_payload = json.dumps({
        "week": "这周收拾了屋子，见了两回卖鱼的",
        "did": ["收拾屋子", "买菜"],
        "met": ["卖鱼的摊子空着"],
        "mood": "有点闷",
        "unfinished": ["照片没整理"],
        "together": ["他加班到很晚"],
    }, ensure_ascii=False)
    parsed, err = W.parse(ok_payload)
    check("正常解析", parsed is not None, err)
    check("字段齐", parsed and parsed["week"].startswith("这周"))
    check("列表截断到上限", parsed and len(parsed["did"]) == 2)

    empty_ok = json.dumps({"week": "这周没什么特别的", "did": [], "met": [],
                           "mood": "", "unfinished": [], "together": []},
                          ensure_ascii=False)
    parsed2, _ = W.parse(empty_ok)
    check("空数组合法", parsed2 is not None and parsed2["did"] == [])

    check("全空 → None", W.parse(json.dumps({"week": "", "did": []}))[0] is None)
    check("坏 JSON → None", W.parse("not json")[0] is None)
    check("空响应 → None", W.parse("")[0] is None)
    check("带代码块也能解析",
          W.parse('```json\n{"week":"还行","did":[]}\n```')[0] is not None)

    # ── 4. 写入 ──
    print()
    print("── 4 · 写入 ──")
    tmp = os.path.join(_ROOT, ".tmp", "selftest_weekly")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    soul_path = os.path.join(tmp, "soul.json")
    base_soul = {
        "identity": {"name": "大肥鱼", "address": "爸", "whoIAm": "我是大肥鱼。"},
        "values": [{"id": "v1", "text": "真诚", "locked": True}],
        "traits": {"warmth": 60, "curiosity": 70, "energy": 55, "caution": 45},
        "selfNotes": ["（11-01）手写的一条"],
        "history": [{"at": 1, "kind": "day_summary", "day": "11-10", "text": "那天的事"}],
    }
    with open(soul_path, "w", encoding="utf-8") as f:
        json.dump(base_soul, f, ensure_ascii=False)

    ok, werr = W.apply_result(soul_path, parsed, now=now)
    check("写入成功", ok, werr)
    with open(soul_path, encoding="utf-8") as f:
        after = json.load(f)
    hist = after.get("history") or []
    check("多了一条", len(hist) == 2, str(len(hist)))
    check("kind 正确", hist[-1].get("kind") == "week_summary")
    check("原有 day_summary 还在", hist[0].get("kind") == "day_summary")
    check("selfNotes 没动", after.get("selfNotes") == base_soul["selfNotes"])
    check("values 没动", after.get("values") == base_soul["values"])
    # ⚠ 不断言整个 identity 相等：`write_soul_file` 会归一化（补 aliases 等
    #   默认字段），那是写盘时的契约，不是这次改动引入的。
    _ident = after.get("identity") or {}
    check("identity 名字没动", _ident.get("name") == "大肥鱼", str(_ident))
    check("identity 人设没动", _ident.get("whoIAm") == "我是大肥鱼。", str(_ident))

    # ── 5. 渲染 ──
    print()
    print("── 5 · 渲染 ──")
    s = Soul.from_dict(after)
    line = s.week_line()
    check("week_line 有内容", bool(line), line)
    check("week_line 带「这周」", "这周" in line, line)
    check("week_line 不超 80 字", len(line) <= 80, str(len(line)))
    check("history_lines 只出「以前」（不含这周）",
          all("这周" not in x for x in s.history_lines()))

    # 没有 week_summary 时
    s2 = Soul.from_dict(base_soul)
    check("没有周叙事时 week_line 为空", s2.week_line() == "")

    # ── 6. mark_done ──
    print()
    print("── 6 · 收口 ──")
    life3 = _Life(_trace(3))
    W.mark_done(life3, day="2026-11-11")
    check("mark_done 写进 weekly_day", life3.weekly_day == "2026-11-11")
    check("mark_done 之后不再 due",
          W.due(life3, time.time()) is False)

    shutil.rmtree(tmp, ignore_errors=True)

    # ── 出口 ──
    print()
    print("=" * 60)
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
