# -*- coding: utf-8 -*-
"""表情库去重自测 —— 不碰微信、不调模型。

锁定的是"什么算同一张图"这件事，它不靠任何主观判断：

    · 字节相同                     → 必判重复
    · 同一张图重新导出 / 加不同水印  → 必判重复
    · 同一张图换了配色               → 只提示，不跳过
    · 同一角色的不同表情             → **不能**判重复（这是核心）

真库里的样本是在 2181 张上实测出来的，成对写在这里当回归；
另外造几张合成图，保证不依赖真库也能跑。
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


def _png(path, size=(200, 200), bg=(30, 60, 160), box=None, text_px=0):
    from PIL import Image

    im = Image.new("RGB", size, bg)
    if box:
        a, b, colour = box
        for x in range(a, b):
            for y in range(a, b):
                im.putpixel((x, y), colour)
    if text_px:          # 右下角加一块深色，模拟"加了一行字"
        for x in range(size[0] - text_px, size[0]):
            for y in range(size[1] - text_px // 3, size[1]):
                im.putpixel((x, y), (10, 10, 10))
    im.save(path)
    return path


def main():
    from bigfish import sticker_fp as F

    tmp = tempfile.mkdtemp(prefix="bigfish-fp-")
    try:
        section("字节层：同一个文件")

        a = _png(os.path.join(tmp, "a.png"))
        b = os.path.join(tmp, "b.png")
        shutil.copy2(a, b)
        fa, fb = F.fingerprint(a), F.fingerprint(b)
        chk("拷贝文件 sha 相同", fa["sha"] == fb["sha"])
        lvl, why = F.compare(fa, fb)
        chk("拷贝文件判重复", lvl == 1, (lvl, why))
        chk("理由说得清", "相同" in why, why)

        section("配色 + 构图层：同一张图的不同导出")

        big = _png(os.path.join(tmp, "big.png"), (400, 400), (40, 80, 200),
                   box=(100, 300, (250, 250, 250)))
        small = os.path.join(tmp, "small.png")
        from PIL import Image

        Image.open(big).resize((180, 180)).save(small)
        lvl, why = F.compare(F.fingerprint(big), F.fingerprint(small))
        chk("缩放 45% 仍判重复", lvl == 1, (lvl, why))

        same_jpg = os.path.join(tmp, "same.jpg")
        Image.open(big).save(same_jpg, quality=70)
        lvl, why = F.compare(F.fingerprint(big), F.fingerprint(same_jpg))
        chk("重压缩成 JPEG 仍判重复", lvl == 1, (lvl, why))

        cap = _png(os.path.join(tmp, "cap.png"), (400, 400), (40, 80, 200),
                   box=(100, 300, (250, 250, 250)), text_px=120)
        lvl, why = F.compare(F.fingerprint(big), F.fingerprint(cap))
        chk("加了一条字幕条仍算同一张", lvl in (1, 2), (lvl, why))

        # 整行字幕条（底部 1/4 全是字）：格数太多，这一档**不认**重复。
        # 记在这里是为了说清边界：加了一行字的同一张图只能靠"少格"认出来，
        # 整块替换内容的和"换了表情"在 8×8 上分不开。
        band = _png(os.path.join(tmp, "band.png"), (400, 400), (40, 80, 200),
                    box=(100, 300, (250, 250, 250)), text_px=400)
        lvl_band, why_band = F.compare(F.fingerprint(big), F.fingerprint(band))
        chk("整行字幕条不静默跳过", lvl_band != 1, (lvl_band, why_band))

        section("必须不判重复：同角色的另一张图")

        # 构图相同、配色相同，但中间那块内容换了位置
        other = _png(os.path.join(tmp, "other.png"), (400, 400), (40, 80, 200),
                     box=(160, 340, (250, 250, 250)))
        lvl, why = F.compare(F.fingerprint(big), F.fingerprint(other))
        chk("内容块位置不同 → 不判重", lvl == 0, (lvl, why))

        # 换了配色（同一张图做了反色版）
        dark = _png(os.path.join(tmp, "dark.png"), (400, 400), (210, 190, 60),
                    box=(100, 300, (10, 10, 10)))
        lvl, why = F.compare(F.fingerprint(big), F.fingerprint(dark))
        chk("换配色 → 最多算疑似", lvl in (0, 2), (lvl, why))
        chk("换配色不静默跳过", lvl != 1, (lvl, why))

        section("空指纹不误判")

        empty = {"sha": "", "cg": "", "sh": ""}
        chk("两边都空 → 不算重复", F.compare(empty, empty)[0] == 0)
        chk("一边空 → 不算重复",
            F.compare(empty, F.fingerprint(a))[0] in (0,), F.compare(empty, F.fingerprint(a)))
        chk("缺字段不崩", F.compare({}, {})[0] == 0)

        section("在池子里找重复")

        pool = [
            {"code": "stk_0001", **F.fingerprint(a)},
            {"code": "stk_0002", **F.fingerprint(big)},
        ]
        hit = F.find_duplicate(F.fingerprint(b), pool)
        chk("找到同文件的那条", hit and hit["code"] == "stk_0001", hit)
        hit2 = F.find_duplicate(F.fingerprint(small), pool)
        chk("找到缩放版的那条", hit2 and hit2["code"] == "stk_0002", hit2)
        hit3 = F.find_duplicate(F.fingerprint(cap), pool)
        chk("字幕版找得到（判重或提示）", bool(hit3), hit3)
        chk("字幕版指向正确的那张", hit3 and hit3["code"] == "stk_0002", hit3)
        # 整行字幕条：格数更分散，允许落在"不是重复"那一档 —— 但不能静默跳过
        band_fp = F.fingerprint(band)
        lvl_band = F.compare(F.fingerprint(big), band_fp)[0]
        chk("整行字幕条不会被静默跳过", lvl_band != 1, lvl_band)
        miss = F.find_duplicate(F.fingerprint(other), pool)
        chk("另一张图不误报", miss is None or miss["level"] == 2, miss)

        section("阈值背后的那份实测（真库样本）")

        # 这几对来自 D:\大肥鱼 的真实库，结论是人眼看过的。
        # 库里没有这些文件时跳过，不算失败。
        real = r"D:\大肥鱼\stickers"
        known = [
            ("stk_0644.gif", "stk_1106.gif", 1, "同一张图重新导出"),
            ("stk_0577.gif", "stk_0237.gif", 1, "同一张图不同水印"),
            ("stk_0781.gif", "stk_0875.gif", 0, "同角色不同姿态"),
        ]
        if os.path.isdir(real):
            for x, y, want, label in known:
                px, py = os.path.join(real, x), os.path.join(real, y)
                if not (os.path.isfile(px) and os.path.isfile(py)):
                    print(f"  · 跳过（库里没有）：{x}/{y}")
                    continue
                got, why = F.compare(F.fingerprint(px), F.fingerprint(py))
                chk(f"{label} → 等级 {want}", got == want, (got, why))
        else:
            print(f"  · 跳过：没有真库 {real}")

        section("写进 index 的回填")

        from bigfish import stickers as S

        root = tempfile.mkdtemp(prefix="bigfish-fp-root-")
        os.environ["BIGFISH_DATA_ROOT"] = root
        sd = S.ensure_dir(root)
        for i in range(3):
            _png(os.path.join(sd, f"src{i}.png"), (120, 120),
                 (10 * i + 20, 90, 200))
        for i in range(3):
            S.import_sticker(os.path.join(sd, f"src{i}.png"), root)

        n = F.backfill(root)
        # import_sticker 现在会自己写指纹，所以这里应该是 0；
        # 断言的是"没有遗漏"，不是"写了 3 条"。
        items = S.read_index(root)["items"]
        missing = [c for c, v in items.items()
                   if not (v.get("sha") and v.get("cg") and v.get("sh"))]
        chk("库里没有缺指纹的条目", not missing, (missing, n))
        chk("每条都有 sha", all(v.get("sha") for v in items.values()))
        chk("每条都有 8×8 色格", all(len(v.get("cg") or "") == 8 * 8 * 8
                                     for v in items.values()))
        chk("再回填一次不再重复做", F.backfill(root) == 0)

        codes = sorted(items)
        # ⚠ 别拿第一条去池子里找：三张合成图本来就几乎一样
        #   （只差底色），它被判成"同一张"是对的。
        #   这里要验的是"找不到不存在的图"，所以拿一张全新的来问。
        stranger = _png(os.path.join(tmp, "stranger.png"), (300, 300),
                        (5, 200, 40), box=(10, 60, (0, 0, 0)))
        s_fp = F.fingerprint(stranger)
        dup = F.find_duplicate(s_fp, [{"code": c, **items[c]} for c in codes])
        chk("一张全新的图在池子里找不到", dup is None, dup)
        dup_self = F.find_duplicate(
            {"sha": items[codes[0]].get("sha", ""),
             "cg": items[codes[0]].get("cg", ""),
             "sh": items[codes[0]].get("sh", "")},
            [{"code": codes[0], **items[codes[0]]}])
        chk("自己跟自己比 → 判重复", dup_self and dup_self["code"] == codes[0],
            dup_self)

        shutil.rmtree(root, ignore_errors=True)

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
