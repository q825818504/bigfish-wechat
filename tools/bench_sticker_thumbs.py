# -*- coding: utf-8 -*-
"""对真实表情库量一次「改前 vs 改后」的界面加载成本。

只在**副本**上跑：缩略图生成会写盘，不动用户的库。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REAL = r"D:\大肥鱼\stickers"


def human(sec: float) -> str:
    return f"{sec * 1000:.0f} ms" if sec < 1 else f"{sec:.2f} s"


def decode_all(paths):
    from PIL import Image

    px = 0
    for p in paths:
        with Image.open(p) as im:
            im.seek(0)
            im.load()
            px += im.size[0] * im.size[1]
    return px


def main():
    tmp = tempfile.mkdtemp(prefix="stk-bench-")
    root = os.path.join(tmp, "root")
    src_dir = os.path.join(root, "stickers")
    os.makedirs(src_dir)
    os.environ["BIGFISH_DATA_ROOT"] = root

    t = time.time()
    for fn in os.listdir(REAL):
        if fn.lower().endswith(".gif"):
            shutil.copy2(os.path.join(REAL, fn), os.path.join(src_dir, fn))
    print(f"拷贝 {len(os.listdir(src_dir))} 张 → {human(time.time() - t)}")

    from bigfish import stickers as S

    # ── 改前：界面拿到的 path 直连 file://，浏览器逐张解码原图 ──
    items = S.list_stickers()
    for it in items:                      # 缩略图字段清掉 = 改前行为
        it.pop("thumb", None)
    paths = [it["path"] for it in items]

    t = time.time()
    px = decode_all(paths)
    before = time.time() - t
    print(f"[改前] 解码全库原图 {len(paths)} 张 / {px / 1e6:.0f} Mpx → {human(before)}")

    # ── 改后：首次补缩略图（一次性，之后都有缓存）──
    t = time.time()
    made = S.ensure_thumbs([it["code"] for it in items])
    print(f"[改后] 首次生成缩略图 {made} 张 → {human(time.time() - t)}")

    items = S.list_stickers()
    thumbs = [it["thumb"] for it in items if it.get("thumb")]
    print(f"        缩略图命中 {len(thumbs)}/{len(items)}")

    t = time.time()
    px2 = decode_all(thumbs)
    after = time.time() - t
    print(f"[改后] 解码缩略图 {len(thumbs)} 张 / {px2 / 1e6:.1f} Mpx → {human(after)}")

    # ── 单纯 list_stickers + JSON 体积 ──
    t = time.time()
    for _ in range(5):
        payload = json.dumps({"ok": True, "items": S.list_stickers()},
                             ensure_ascii=False)
    dt = (time.time() - t) / 5
    print(f"[两版相同] sticker_list 一次 {human(dt)}，JSON {len(payload.encode()) / 1024:.0f} KB")

    thumb_bytes = sum(os.path.getsize(p) for p in thumbs) if thumbs else 0
    orig_bytes = sum(os.path.getsize(p) for p in paths)
    print(f"盘上：原图 {orig_bytes / 1048576:.0f} MB → 缩略图 {thumb_bytes / 1048576:.1f} MB")
    print(f"\n结论：进一次表情页的解码成本 {human(before)} → {human(after)}"
          f"（{before / max(after, 1e-6):.0f}×）")

    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
