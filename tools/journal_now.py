"""手动写一篇今晚的日记，不用等到睡前。

跑法：
    python tools/journal_now.py            # 只看：调一次模型，打印写成什么，不落盘
    python tools/journal_now.py --apply    # 真写：生成 journal/YYYY-MM-DD.md
    python tools/journal_now.py --day 2026-11-07 --apply   # 补写某一天
    python tools/journal_now.py --no-model # 不调模型，只看素材

和 reflect_now.py 的分工：
    reflect_now  写灵魂（我改了什么看法）
    journal_now  写日记（今天我是怎么过的）

日记**不限字数**，也没有 JSON 契约。所以它比复盘宽松得多：
只要有一句话真实对话，就允许写。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)


def _narrative_summary(day: str) -> str:
    """复盘今天写下的那句总结（如果跑过）。"""
    try:
        from bigfish import vault as V

        raw, _e, _err = V.read_soul_file(V.soul_path())
        for item in reversed(raw.get("history") or []):
            if not isinstance(item, dict):
                continue
            if item.get("kind") == "day_summary" and str(item.get("day")) == day[5:]:
                return str(item.get("text") or "")
    except Exception:
        pass
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="手动写一篇日记")
    ap.add_argument("--apply", action="store_true", help="真写文件（默认只打印）")
    ap.add_argument("--chat", default="", help="以哪个会话为素材")
    ap.add_argument("--day", default="", help="写哪一天（默认今天）")
    ap.add_argument("--no-model", action="store_true", help="不调模型，只打印素材")
    args = ap.parse_args()

    from reflect_now import _gather, _owner_chat, _read_config

    cfg = _read_config()
    owner_raw = _owner_chat(cfg)
    now = time.time()
    chat = args.chat or owner_raw

    from bigfish import journal as J
    from bigfish import life as L

    day = args.day or L.day_key(now)
    materials = _gather(now, chat=chat,
                        owner=(f"private:wechat:{owner_raw}" if owner_raw else ""))
    materials["chat"] = chat
    materials["owner"] = (f"private:wechat:{owner_raw}" if owner_raw else "")
    materials["day"] = day

    try:
        from bigfish import senses

        materials["weather"] = senses.cached_weather(now)
    except Exception:
        materials["weather"] = ""
    materials["narrative_summary"] = _narrative_summary(day)

    # 手头在做什么 + 今天做过的事。和 world._gather_day_materials 对齐 ——
    # 少这一块，空白日只能写"今天什么都没发生"，而其实她收拾过屋子。
    try:
        from bigfish import life as L2

        st = L2.load()
        materials["doing"] = str(getattr(st, "doing", "") or "")
        trace = []
        for item in (getattr(st, "day_trace", None) or []):
            if not isinstance(item, dict) or not item.get("doing"):
                continue
            try:
                clock = time.strftime("%H:%M", time.localtime(
                    float(item.get("at") or 0)))
            except Exception:
                clock = ""
            trace.append(f"{clock} {item.get('doing')}".strip())
        materials["trace"] = trace[-10:]
    except Exception:
        materials.setdefault("doing", "")
        materials.setdefault("trace", [])

    print("=" * 62)
    print("每日日记 · 手动触发")
    print("=" * 62)
    print(f"  日期     : {day}")
    print(f"  会话     : {chat or '(最近一份 history)'}")
    print(f"  素材     : 对话 {len(materials['talk'])} / 心情 {len(materials['mood'])}"
          f" / 事实 {len(materials['facts'])}"
          f" / 事件 {len((materials.get('relation') or {}).get('events') or [])}")
    print(f"  模式     : {'写盘' if args.apply else '只看（不写）'}")

    if args.no_model:
        _sys, user = J.build_request(day=day, materials=materials)
        print("\n--- 素材 ---")
        print(user[:2000])
        return 0

    enough, why = J.has_material(materials)
    if not enough:
        print(f"\n✗ 素材不够，今天不写：{why}")
        return 4

    from bigfish.replier import ModelConfig, ModelError, call_model

    system, user = J.build_request(day=day, materials=materials)
    print(f"\n  请求长度 : system {len(system)} 字 / user {len(user)} 字")
    print("  调模型中 ...")

    try:
        raw = call_model(ModelConfig.load(), system, user)
    except ModelError as exc:
        print(f"\n✗ 模型调用失败：{exc}")
        return 2

    text = J.clean(raw)
    print("\n--- 日记正文 ---")
    print(text)
    print(f"\n  （{len(text)} 字）")

    if not args.apply:
        print("\n（dry-run：没有写文件。加 --apply 才落盘）")
        return 0

    ok, path, chars = J.write(day, text, meta={"chars": len(text)})
    if not ok:
        print("\n✗ 写文件失败")
        return 3
    print(f"\n✓ 已写入 {path}（{chars} 字）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
