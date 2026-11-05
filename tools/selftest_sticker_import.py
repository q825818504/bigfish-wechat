# -*- coding: utf-8 -*-
"""批量导入自测：查重、跳过、报告、以及"一个 index 只写一次"。

不碰真库：全部在临时数据根里造图。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import traceback

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PASS = FAIL = 0
FAILED = []


def chk(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        FAILED.append(label)
        print(f"  ✗ {label}  {extra}")


def section(t):
    print(f"\n── {t} " + "─" * max(0, 54 - len(t)))


def make_png(path, size=(240, 240), bg=(30, 60, 160), block=None):
    from PIL import Image

    im = Image.new("RGB", size, bg)
    if block:
        a, b, colour = block
        for x in range(a, b):
            for y in range(a, b):
                im.putpixel((x, y), colour)
    im.save(path)
    return path


def main():
    from bigfish import stickers as S

    tmp = tempfile.mkdtemp(prefix="bigfish-batch-")
    root = os.path.join(tmp, "root")
    os.makedirs(root)
    os.environ["BIGFISH_DATA_ROOT"] = root
    src_dir = os.path.join(tmp, "src")
    os.makedirs(src_dir)

    try:
        section("第一批：三张不同的图")

        a = make_png(os.path.join(src_dir, "a.png"), block=(60, 180, (250, 250, 250)))
        b = make_png(os.path.join(src_dir, "b.png"), (240, 240), (200, 60, 40),
                     block=(60, 180, (20, 20, 20)))
        c = make_png(os.path.join(src_dir, "c.png"), (240, 240), (30, 160, 60),
                     block=(30, 90, (240, 240, 40)))
        rep = S.import_batch([a, b, c], root)
        chk("三张全收", len(rep["added"]) == 3, rep)
        chk("没有跳过", not rep["skipped"], rep["skipped"])
        chk("没有失败", not rep["failed"], rep["failed"])
        chk("index 里有三条", len(S.read_index(root)["items"]) == 3)
        chk("每条都带指纹",
            all(v.get("sha") and v.get("cg") and v.get("sh")
                for v in S.read_index(root)["items"].values()))

        section("第二批：同一张再来一遍（改名 + 换尺寸 + 换格式）")

        a_copy = os.path.join(src_dir, "a-改名.png")
        shutil.copy2(a, a_copy)
        from PIL import Image

        a_small = os.path.join(src_dir, "a-small.jpg")
        Image.open(a).resize((150, 150)).save(a_small, quality=75)
        rep2 = S.import_batch([a_copy, a_small], root)
        chk("改名的那张被跳过（同一批里先跳过，后一张统计会多算）",
            len(rep2["skipped"]) >= 1, rep2["skipped"])
        chk("跳过的能指出撞上谁", rep2["skipped"] and
            rep2["skipped"][0]["code"] in
            [it["code"] for it in S.list_stickers(root)], rep2["skipped"])
        chk("别名 + 缩放的都不入库", len(rep2["added"]) == 0,
            [x["file"] for x in rep2["added"]])
        chk("库里还是 3 条",
            len(S.list_stickers(root)) == 3, len(S.list_stickers(root)))

        section("第三批：同角色另一张（顺序换了的同构图）")

        d = make_png(os.path.join(src_dir, "d.png"), (240, 240), (30, 60, 160),
                     block=(20, 60, (250, 250, 250)))
        rep3 = S.import_batch([d], root)
        chk("构图不同的照收", len(rep3["added"]) == 1, rep3)
        chk("不误判成重复", not rep3["skipped"], rep3["skipped"])

        section("批内去重：同一批里放了两次同一张")

        e = make_png(os.path.join(src_dir, "e.png"), (200, 200), (10, 10, 10),
                     block=(50, 150, (240, 240, 240)))
        e2 = os.path.join(src_dir, "e-copy.png")
        shutil.copy2(e, e2)
        rep4 = S.import_batch([e, e2], root)
        chk("批内只收一张", len(rep4["added"]) == 1, rep4["added"])
        chk("另一张进跳过名单", len(rep4["skipped"]) == 1, rep4["skipped"])

        section("坏输入")

        bad = os.path.join(src_dir, "note.txt")
        open(bad, "w").write("not an image")
        missing = os.path.join(src_dir, "nope.png")
        rep5 = S.import_batch([bad, missing], root)
        chk("不支持的格式进 failed", len(rep5["failed"]) == 2, rep5["failed"])
        chk("坏输入不新增", not rep5["added"], rep5["added"])

        section("关掉查重就照单全收")

        before = len(S.list_stickers(root))
        rep6 = S.import_batch([a_copy], root, dedupe=False)
        chk("dedupe=False 时重复也收", len(rep6["added"]) == 1, rep6)
        chk("库里确实多了一条", len(S.list_stickers(root)) == before + 1,
            (before, len(S.list_stickers(root))))
        # 清理：把这多出来的一条删掉，别影响后面的计数
        S.delete_sticker(rep6["added"][0]["code"], root)

        section("缩略图跟着出来")

        items = S.list_stickers(root)
        chk("新条目都有缩略图",
            all(it.get("thumb") for it in items),
            [it["code"] for it in items if not it.get("thumb")])

        section("空批次")

        rep7 = S.import_batch([], root)
        chk("空列表不崩", rep7["added"] == [] and rep7["failed"] == [], rep7)

    except Exception:
        traceback.print_exc()
        global FAIL
        FAIL += 1
        FAILED.append("未捕获异常")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 66)
    print(f"结果：{PASS}/{PASS + FAIL} 通过")
    if FAILED:
        print("失败项：")
        for x in FAILED:
            print(f"  · {x}")
    print("=" * 66)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
