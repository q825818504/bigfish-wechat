# -*- coding: utf-8 -*-
"""诊断：自己发的消息为什么被判成「别人说的」。

只读，不改任何东西。跑法：
    python tools/diag_self.py
    python tools/diag_self.py 老爸
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bigfish.wechat import WeChatReader, probe_running_accounts  # noqa: E402


def show(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else ""

    show("1. 运行中的账号")
    accs = probe_running_accounts()
    if not accs:
        print("!! 没探测到账号。微信没开？")
        return 1
    for a in accs:
        print(f"  account={a.get('account')!r}  wxid={a.get('wxid')!r}  "
              f"running={a.get('running')}  locked={a.get('lockedDb')}")

    running = [a for a in accs if a.get("running")] or accs

    picked = None
    hits = []
    for a in running:
        acc = a.get("account") or ""
        try:
            r = WeChatReader(account=acc).open()
        except Exception as exc:
            print(f"  打开 {acc!r} 失败: {exc}")
            continue
        db = r._db
        print(f"\n--- 账号 {acc!r}  db.wxid={db.wxid!r} ---")
        try:
            for c in r.sessions(limit=60):
                name = str(c.get("chat_name") or c.get("name") or "")
                uid = str(c.get("username") or c.get("chat_id") or "")
                if target and target in (name, uid):
                    print(f"    >>> 命中 {uid:<34} {name}")
                    hits.append((acc, r, db, uid, name))
        except Exception as exc:
            print(f"    列会话失败: {exc}")

    if not hits:
        print(f"\n!! 没在运行账号里找到会话 {target!r}")
        return 1

    for acc, r, db, uid, name in hits:
        show(f"4. [{acc}] {name!r}（{uid}）最近 14 条   db.wxid={db.wxid}")
        rows = db.get_messages(uid, limit=14)
        hdr = f"  {'lid':>6} {'rsid':>6} {'sender_username':<22} {'self':>5}  text"
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for m in rows:
            shard = m.get("shard", "")
            sid = m.get("sender_id")
            try:
                sender = db.sender_username(shard, sid) if shard else ""
            except Exception as exc:
                sender = f"<err {exc}>"
            is_self = (sender == db.wxid) if sender else False
            raw = str(m.get("content") or "")
            txt = raw.replace("\n", " ")[:40]
            print(f"  {int(m.get('local_id') or 0):>6} {str(sid):>6} "
                  f"{str(sender)[:22]:<22} {str(is_self):>5}  {txt}")

        print(f"\n  -- 各分片 Name2Id 里本机账号 rowid --")
        try:
            for rel in db._message_dbs():
                conn = db._open(rel)
                try:
                    mp = db._name2id_map(conn)
                finally:
                    conn.close()
                mine = [rid for rid, u in mp.items() if u == db.wxid]
                print(f"    {os.path.basename(rel):<26} 共 {len(mp):>3} 人  "
                      f"我的 rowid={mine or '（不在表里）'}")
        except Exception as exc:
            print(f"    失败: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
