# -*- coding: utf-8 -*-
"""清理表情库里"字节完全相同"的重复项。

用法：
    python tools/dedupe_stickers.py            # 只报告，不动文件
    python tools/dedupe_stickers.py --apply    # 真删

规则：
    · 只处理 sha256 完全相同的条目（不碰"相似"，那个要人看）
    · 每组保留编号最小的那张，其余删除
    · 被删条目的 brief/detail/tags 会**并到**保留项上（不丢说明）
    · 删之前把整个 stickers 目录备份到 <数据根>/stickers-backup-<时间>/
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bigfish import stickers as S  # noqa: E402


def groups(root: str):
    items = S.read_index(root).get("items") or {}
    by_sha = defaultdict(list)
    for code, meta in items.items():
        sha = meta.get("sha") or ""
        if sha:
            by_sha[sha].append(code)
    return {k: sorted(v) for k, v in by_sha.items() if len(v) > 1}, items


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真删")
    ap.add_argument("--root", default=None)
    args = ap.parse_args()

    root = args.root or S.resolve_data_root()
    g, items = groups(root)
    if not g:
        print("没有字节完全相同的重复。")
        return 0

    total = sum(len(v) - 1 for v in g.values())
    print(f"发现 {len(g)} 组重复，共 {total} 张多余：\n")
    plan = []
    for sha, codes in sorted(g.items(), key=lambda kv: kv[1][0]):
        keep, drop = codes[0], codes[1:]
        print(f"  保留 {keep}  ←  删除 {', '.join(drop)}")
        plan.append((keep, drop))

    if not args.apply:
        print("\n（只报告。加 --apply 才会真删）")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(root, f"stickers-backup-{stamp}")
    shutil.copytree(S.sticker_dir(root), backup)
    print(f"\n已备份 → {backup}")

    data = S.read_index(root)
    items = data.setdefault("items", {})
    removed = 0
    for keep, drop in plan:
        k = items.get(keep) or {}
        for code in drop:
            meta = items.pop(code, None) or {}
            # 说明并过去，别因为去重丢描述
            if meta.get("brief") and not k.get("brief"):
                k["brief"] = meta["brief"]
            if meta.get("detail") and not k.get("detail"):
                k["detail"] = meta["detail"]
            if meta.get("tags") and not k.get("tags"):
                k["tags"] = meta["tags"]
            try:
                os.remove(os.path.join(S.sticker_dir(root),
                                       S._as_file(meta.get("file") or code)))
            except Exception:
                pass
            S.drop_thumb(code, root)
            removed += 1
        items[keep] = k

    S.write_index(data, root)
    print(f"已删除 {removed} 张，库现在是 {len(items)} 条。")
    print("要回滚：把备份目录改名回 stickers，再把 index.json 一起放回去。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
