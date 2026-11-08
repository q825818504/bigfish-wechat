"""手动跑一次「睡前复盘」，不用等到 23 点。

跑法：
    python tools/reflect_now.py            # 只看：调一次模型，打印会写成什么，不落盘
    python tools/reflect_now.py --apply    # 真写：改 soul.json 和 memory.sqlite3
    python tools/reflect_now.py --chat wxid_xxx --apply
    python tools/reflect_now.py --no-model # 不调模型，只打印素材（网断了也能看）

设计约束：
  · **默认不写**。复盘会改灵魂和关系卡，误触发一次就多一条"她的看法"。
    所以默认 dry-run，`--apply` 才落盘 —— 和 proactive 的预演模式同一个思路。
  · 读的是真实数据根（settings.json 的 dataRoot），但 dry-run 只读。
  · 不启动 GUI、不碰微信、不发消息。它只做"复盘"这一件事。

为什么需要它：复盘的正常触发点是入睡前 30 分钟，一天一次。
想验证"她到底会不会改自己"，等到 23 点太慢，而且没法反复试。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _read_config() -> dict:
    for name in ("config.json", os.path.join("dist", "BigFish", "config.json")):
        p = os.path.join(_ROOT, name)
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            continue
    return {}


def _owner_chat(cfg: dict) -> str:
    return str((cfg.get("auto_reply") or {}).get("owner") or "").strip()


def _gather(now: float, *, chat: str = "", owner: str = "") -> dict:
    """从真实数据根取素材。每一块都单独兜底 —— 缺一块不该让整次复盘失败。"""
    from bigfish import bond as B
    from bigfish import history
    from bigfish import life as L
    from bigfish import memory as M
    from bigfish import mood_judge
    from bigfish import plan as PLAN
    from bigfish import proactive as P
    from bigfish import vault as V

    materials: dict = {}

    try:
        life = L.load()
    except Exception:
        life = L.LifeState()

    materials["life"] = life
    materials["plan"] = [
        f"{x.get('at')} {x.get('what')}" + ("（做完了）" if x.get("done") else "")
        for x in (getattr(life, "plan", None) or []) if isinstance(x, dict)]

    mood_rows = []
    try:
        from bigfish import narrative as N

        st = mood_judge.read_mood_state_now() or {}
        materials["mood_now"] = st.get("mood")
        # 只取今天的：timeline[-6:] 会把三天前的记录当成今天。
        mood_rows = N.mood_rows_for_day(st.get("timeline"), now=now)
    except Exception:
        materials["mood_now"] = None
    materials["mood"] = mood_rows

    db = ""
    bank = ""
    try:
        db = V.memory_db_path() or ""
        if db:
            bank = M._default_bank(db) or "dafeiyu"
    except Exception:
        pass
    materials["db"] = db
    materials["bank"] = bank

    facts = []
    if db:
        try:
            rows = M.list_facts(db, bank, status="active", limit=8)
            for row in rows or []:
                if isinstance(row, dict):
                    text = row.get("text") or row.get("statement") or ""
                    if str(text).strip():
                        facts.append(str(text).strip())
        except Exception:
            pass
    materials["facts"] = facts

    materials["talk"] = []
    if chat:
        try:
            materials["talk"] = [f"{w}: {t}" for w, t in
                                 history.load(chat, limit=16)]
        except Exception:
            pass
    if not materials["talk"]:
        # chat_id 拿不到时，直接读最近一份 history 文件。
        #
        # ⚠ 文件名是 chat_id 的 sha1 前 16 位（见 history.path_for），
        #   反推不出来 —— 所以不能"从文件名猜 chat_id 再 load"，
        #   那会二次哈希、永远找不到。内容本身够复盘用，直接读。
        try:
            from bigfish import vault as V

            d = os.path.join(V.resolve_data_root(), history.DIR_NAME)
            files = [os.path.join(d, n) for n in os.listdir(d)
                     if n.endswith(".jsonl")]
            if files:
                newest = max(files, key=os.path.getmtime)
                rows = []
                with open(newest, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            continue
                materials["talk"] = [
                    f"{r.get('who')}: {r.get('text')}" for r in rows[-16:]
                    if isinstance(r, dict)]
                materials["talk_from"] = os.path.basename(newest)
        except Exception:
            pass

    materials["relation"] = {}
    if db:
        try:
            materials["relation"] = {
                "stage": B.get_stage(db, bank, owner),
                "events": [e.get("what") for e in
                           B.list_events(db, bank, owner, limit=5)],
            }
        except Exception:
            pass

    materials["proactive"] = []
    try:
        pro = P.load()
        sent = [f"她说：{r.get('text')}" for r in (pro.recent or [])[-5:]
                if isinstance(r, dict)]
        missed = [f"没人回：{d.get('text')}" for d in (pro.dropped or [])[-3:]
                  if isinstance(d, dict)]
        materials["proactive"] = sent + missed
    except Exception:
        pass

    materials["now"] = now
    return materials


def main() -> int:
    ap = argparse.ArgumentParser(description="手动跑一次睡前复盘")
    ap.add_argument("--apply", action="store_true", help="真写（默认只打印）")
    ap.add_argument("--chat", default="", help="以哪个会话的对话为素材")
    ap.add_argument("--no-model", action="store_true", help="不调模型，只打印素材")
    args = ap.parse_args()

    cfg = _read_config()
    owner_raw = _owner_chat(cfg)
    now = time.time()

    chat = args.chat or owner_raw
    materials = _gather(now, chat=chat,
                        owner=(f"private:wechat:{owner_raw}" if owner_raw else ""))
    materials["chat"] = chat
    materials["owner"] = (f"private:wechat:{owner_raw}" if owner_raw else "")

    from bigfish import life as L
    from bigfish import narrative as N

    life = materials["life"]
    today = getattr(life, "day", "") or L.day_key(now)

    print("=" * 62)
    print("睡前复盘 · 手动触发")
    print("=" * 62)
    print(f"  数据根   : {os.environ.get('BIGFISH_DATA_ROOT') or '(settings.json)'}")
    print(f"  今天     : {today}")
    print(f"  会话     : {materials.get('chat') or '(无)'}")
    print(f"  当前心情 : {materials.get('mood_now')}")
    print(f"  素材     : 计划 {len(materials['plan'])} / 心情 {len(materials['mood'])}"
          f" / 事实 {len(materials['facts'])} / 对话 {len(materials['talk'])}"
          f" / 事件 {len((materials.get('relation') or {}).get('events') or [])}"
          f" / 主动 {len(materials['proactive'])}")
    print(f"  模式     : {'写盘' if args.apply else '只看（不写）'}")

    if args.no_model:
        print("\n--- 素材 ---")
        print(json.dumps({k: v for k, v in materials.items()
                          if k not in ("life", "db", "bank", "now")},
                         ensure_ascii=False, indent=2)[:3000])
        return 0

    enough, why = N.has_material(materials)
    if not enough:
        print(f"\n✗ 素材不够，这次复盘应该跳过：{why}")
        print("  正常运行时，world 也会因此不调模型 —— 允许「今天很平常」。")
        return 4

    from bigfish.replier import ModelConfig, ModelError, call_model

    system, user = N.build_request(today=today, summary_materials=materials)
    print(f"\n  请求长度 : system {len(system)} 字 / user {len(user)} 字")
    print("  调模型中 ...")

    try:
        raw = call_model(ModelConfig.load(), system, user)
    except ModelError as exc:
        print(f"\n✗ 模型调用失败：{exc}")
        print("  检查 Magpie 是否在跑，或 config.json 的 llm 段。")
        return 2

    parsed, err = N.parse(raw)
    print("\n--- 模型原始输出 ---")
    print((raw or "").strip()[:1200])
    print("\n--- 解析结果 ---")
    print(json.dumps(parsed, ensure_ascii=False, indent=2) if parsed
          else f"(解析失败：{err})")

    if not parsed:
        return 3

    if not args.apply:
        print("\n（dry-run：没有写盘。加 --apply 才会改 soul.json / bond_events）")
        return 0

    receipt = N.apply_result(
        parsed, soul_path=None, db=materials.get("db") or "",
        bank=materials.get("bank") or "", owner=materials.get("owner") or "",
        now=now)
    N.mark_done(life, day=today)
    try:
        L.save(life, force=True)
    except Exception:
        pass
    print("\n--- 回执 ---")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    print(f"\n✓ 已写入：{N.summarize_receipt(receipt)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
