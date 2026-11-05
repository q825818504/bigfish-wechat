"""[bigfish] 表情库自测 —— 不碰微信、不调模型，只验逻辑。

2026-11-03 重构后重写。这一版锁的是新东西：

  1. 任意格式 → GIF（含多帧、缩放、透明）
  2. 每张一个唯一代码
  3. 两级描述（brief 概览 / detail 详细）
  4. 旧 schema 迁移
  5. 删除时的墓碑（文件被占用不能变成"删了还在"）
  6. 插件侧：开关、冷却、候选挑选

⚠ 刻意**不测真发**：真发要开微信、要管理员、会抢焦点。
  那部分只能手测（tools/live_sticker_test.py）。
  这里测的是"库和判断"本身。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
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


def section(title):
    print(f"\n── {title} " + "─" * max(0, 56 - len(title)))


def _tmp_root():
    d = tempfile.mkdtemp(prefix="bigfish-stk-")
    os.environ["BIGFISH_DATA_ROOT"] = d
    return d


def _mk_png(path, size=(200, 200), color=(255, 200, 0, 255), alpha=True):
    from PIL import Image

    mode = "RGBA" if alpha else "RGB"
    Image.new(mode, size, color).save(path)
    return path


def _mk_gif(path, frames=3, size=(256, 256)):
    from PIL import Image

    imgs = [Image.new("RGB", size, (i * 60, 100, 200)) for i in range(frames)]
    imgs[0].save(path, save_all=True, append_images=imgs[1:],
                 duration=120, loop=0)
    return path


def main():
    root = _tmp_root()
    try:
        import bigfish.stickers as S
        import bigfish.vault as vault

        section("代码生成：唯一、递增")
        data = S.read_index(root)
        c1 = S.next_code(data)
        c2 = S.next_code(data)
        chk("格式是 stk_NNNN", c1.startswith("stk_") and len(c1) == 8, c1)
        chk("两次不同", c1 != c2, f"{c1} vs {c2}")
        chk("递增", int(c2[4:]) == int(c1[4:]) + 1)

        section("格式转换：任意图 → GIF")
        d = tempfile.mkdtemp(prefix="stk-src-")
        png = _mk_png(os.path.join(d, "a.png"))
        gif = _mk_gif(os.path.join(d, "b.gif"), frames=4)
        big = _mk_png(os.path.join(d, "c.png"), size=(2000, 1000))
        webp = os.path.join(d, "e.webp")
        try:
            from PIL import Image
            fr = [Image.new("RGB", (300, 300), (i * 70, 120, 60))
                  for i in range(3)]
            fr[0].save(webp, save_all=True, append_images=fr[1:],
                       duration=100, loop=0)
        except Exception:
            webp = ""

        ok, code, item = S.import_sticker(png, root)
        chk("PNG 导入成功", ok and item.get("file", "").endswith(".gif"),
            item.get("file"))
        chk("输出真的是 GIF", _is_gif(item.get("path")), item.get("path"))

        ok, code_gif, item_gif = S.import_sticker(gif, root)
        chk("GIF 导入成功", ok)
        chk("多帧保留", int(item_gif.get("frames") or 1) == 4,
            f"frames={item_gif.get('frames')}")

        ok, code_big, item_big = S.import_sticker(big, root)
        chk("大图导入成功", ok)
        w, h = _size(item_big.get("path"))
        chk("缩到 1024 以内", max(w, h) <= 1024, f"{w}x{h}")
        chk("保持宽高比", abs((w / h) - 2.0) < 0.05, f"{w}x{h}")

        if webp:
            ok, code_webp, item_webp = S.import_sticker(webp, root)
            chk("动 WebP 导入成功", ok)
            chk("动 WebP 帧数保留", int(item_webp.get("frames") or 1) == 3,
                f"frames={item_webp.get('frames')}")

        section("透明：GIF 的 1 位透明")
        tr = os.path.join(d, "tr.png")
        from PIL import Image as _I
        from PIL import ImageDraw as _D
        im = _I.new("RGBA", (160, 160), (0, 0, 0, 0))
        _D.Draw(im).ellipse([20, 20, 140, 140], fill=(255, 0, 0, 255))
        im.save(tr)
        ok, code_tr, item_tr = S.import_sticker(tr, root)
        chk("透明 PNG 导入成功", ok)
        with _I.open(item_tr["path"]) as g:
            rgba = g.convert("RGBA")
            chk("角落透明", rgba.getpixel((3, 3))[3] == 0,
                rgba.getpixel((3, 3)))
            chk("中心不透明", rgba.getpixel((80, 80))[3] == 255,
                rgba.getpixel((80, 80)))

        section("描述：概览 + 详细")
        ok, err = S.update_sticker(code, root, brief="生气",
                                   detail="眉毛倒竖，对方说了让你不爽的话时用")
        chk("写描述成功", ok, err)
        got = S.get_sticker(code, root)
        chk("概览读得回", got.get("brief") == "生气")
        chk("详细读得回", "眉毛倒竖" in (got.get("detail") or ""))

        ok, err = S.update_sticker(code, root, brief="x" * 30)
        chk("概览被截断", len(S.get_sticker(code, root)["brief"]) <= 12,
            S.get_sticker(code, root)["brief"])

        ok, err = S.update_sticker("stk_9999", root, brief="x")
        chk("改不存在的返回失败", not ok)

        section("列表：排序、orphan 补录")
        items = S.list_stickers(root)
        chk("列表非空", len(items) >= 4, len(items))
        codes = [x["code"] for x in items]
        chk("按代码排序", codes == sorted(codes), codes)

        # 手动往目录里扔一张，应该被补录
        orphan = _mk_png(os.path.join(d, "manual.gif"), size=(64, 64),
                         alpha=False)
        shutil.copy2(orphan, os.path.join(S.sticker_dir(root), "manual.gif"))
        items2 = S.list_stickers(root)
        chk("orphan 被补录", len(items2) == len(items) + 1, len(items2))
        manual = [x for x in items2 if x["file"] == "manual.gif"]
        chk("orphan 标了来源", manual and manual[0]["source"] == "orphan")

        section("删除：文件被占用也不能'删了还在'")
        victim = S.list_stickers(root)[0]
        vpath = victim["path"]
        handle = _open_hold(vpath)      # 故意持有句柄
        ok, note = S.delete_sticker(victim["code"], root)
        chk("删除返回成功", ok, note)
        left = [x for x in S.list_stickers(root) if x["code"] == victim["code"]]
        chk("列表里已经看不到", not left, left)
        _close_hold(handle)
        # 句柄释放后，下一次 list 应该把文件真删掉
        S.list_stickers(root)
        chk("句柄释放后文件被清理", not os.path.exists(vpath), vpath)

        section("schema 迁移：v1 → v2")
        mig = tempfile.mkdtemp(prefix="stk-mig-")
        sd = os.path.join(mig, S.STICKER_DIR)
        os.makedirs(sd)
        _mk_png(os.path.join(sd, "old.gif"), size=(64, 64), alpha=False)
        with open(os.path.join(sd, S.INDEX_NAME), "w", encoding="utf-8") as f:
            json.dump({"old.gif": {"desc": "老格式的说明"}}, f,
                      ensure_ascii=False)
        items3 = S.list_stickers(mig)
        chk("旧格式能读出来", len(items3) == 1, items3)
        chk("desc 迁移成 brief",
            items3 and items3[0]["brief"] == "老格式的说明",
            items3[0]["brief"] if items3 else "")
        with open(os.path.join(sd, S.INDEX_NAME), encoding="utf-8") as f:
            idx = json.load(f)
        chk("index 已升级到 v2", idx.get("version") == 2, idx.get("version"))
        shutil.rmtree(mig, ignore_errors=True)

        section("插件：开关、冷却、候选")
        from bigfish import plugins

        plugins.ensure_loaded()
        inv = plugins.inventory(root)
        ids = {p["id"]: p for p in inv["plugins"]}
        chk("sticker 已登记", "sticker" in ids)
        chk("scope 是 sticker", ids["sticker"]["scope"] == "sticker")
        cfg = plugins.config_of("sticker", root)
        chk("默认频率 0.15", abs(float(cfg.get("rate", 0)) - 0.15) < 1e-9, cfg)
        chk("默认冷却 180", int(cfg.get("cooldown", 0)) == 180, cfg)

        from bigfish.plugins import sticker as st

        chk("should_learn 空输入为假", st.should_learn({}) is False)
        chk("should_learn 无关话为假",
            st.should_learn({"user_text": "今天天气不错",
                             "chat_id": "x"}) is False)
        # 没发过表情时，纠正话也不该触发
        chk("没发过表情时不学",
            st.should_learn({"user_text": "这个表情不是这么用的",
                             "chat_id": "nobody"}) is False)

        section("情绪维度与强度")
        chk("维度和 vault 一致",
            tuple(S.MOOD_DIM_KEYS) == tuple(k for k, _ in vault.MOOD_DIMS))
        chk("标签和 vault 一致",
            S.MOOD_DIM_LABELS == dict(vault.MOOD_DIMS))

        # 脏数据不能崩
        chk("脏维度被清空", S._clean_dim("hAppy") == "happy")
        chk("不认识的维度清空", S._clean_dim("angry") == "")
        chk("None 维度清空", S._clean_dim(None) == "")

        chk("强度夹到 1~5（低）", S._clamp_intensity(0) == 1)
        chk("强度夹到 1~5（高）", S._clamp_intensity(99) == 5)
        chk("强度默认 3", S._clamp_intensity(None) == 3)
        chk("强度默认 3（脏）", S._clamp_intensity("abc") == 3)

        # ⚠ 用当前还在库里的第一张 —— 前面的删除测试可能已经删掉了 code
        alive = [x["code"] for x in S.list_stickers(root)]
        chk("库里还有图可测", bool(alive), alive)
        probe = alive[0] if alive else code
        ok, err = S.update_sticker(probe, root, mood_dim="happy", intensity=5)
        chk("写维度与强度", ok, err)
        got = S.get_sticker(probe, root)
        chk("维度读得回", got.get("moodDim") == "happy", got.get("moodDim"))
        chk("维度标签", got.get("moodDimLabel") == "开心")
        chk("强度读得回", got.get("intensity") == 5, got.get("intensity"))

        ok, err = S.update_sticker(probe, root, mood_dim="")
        chk("清空维度", ok and S.get_sticker(probe, root)["moodDim"] == "")

        section("缩略图：网格不该解码原图")
        # 导入时已经顺手做过一张；剩下几张还没做
        codes_all = [x["code"] for x in S.list_stickers(root)]
        thumbs = [x for x in S.list_stickers(root) if x.get("thumb")]
        chk("导入时生成了缩略图", bool(thumbs), codes_all)
        if thumbs:
            tp = thumbs[0]["thumb"]
            chk("缩略图路径在 _thumbs 下",
                S.THUMB_DIR in tp, tp)
            chk("缩略图是 WebP（体积只有 PNG 的 1/20）",
                tp.lower().endswith(".webp"), tp)
            chk("缩略图文件存在", os.path.isfile(tp), tp)
            from PIL import Image as _Im
            with _Im.open(tp) as im:
                chk("缩略图不超过 THUMB_PX",
                    max(im.size) <= S.THUMB_PX, im.size)
                chk("缩略图是静态单帧（不循环播放）",
                    getattr(im, "n_frames", 1) == 1)
                orig_kb = os.path.getsize(thumbs[0]["path"]) / 1024
                thumb_kb = os.path.getsize(tp) / 1024
                chk("缩略图比原图小一个数量级",
                    thumb_kb * 4 < orig_kb, (orig_kb, thumb_kb))

        # 没缩略图的那些：ensure_thumb 要能补出来，而且**不会**被当成孤儿
        missing = [x["code"] for x in S.list_stickers(root) if not x.get("thumb")]
        made = S.ensure_thumbs(missing, root)
        chk("补出来的数量对得上", made == len(missing), (made, len(missing)))
        chk("补完就没有缺的了",
            not [x for x in S.list_stickers(root) if not x.get("thumb")])

        before_n = len(S.list_stickers(root))
        now_thumbs = [x["thumb"] for x in S.list_stickers(root) if x.get("thumb")]
        chk("_thumbs 里的图没被补录进 index",
            len(S.list_stickers(root)) == before_n and bool(now_thumbs),
            before_n)

        # ⚠ 回归点：以前 list_stickers 会把 _thumbs 里的 .png 当孤儿，
        #   每刷新一次多一批。这里连刷三次，条目数必须不变。
        for _ in range(3):
            S.list_stickers(root)
        chk("连刷三次库条目不膨胀",
            len(S.list_stickers(root)) == before_n,
            len(S.list_stickers(root)))

        # 删图要顺手删缩略图，否则缓存变孤儿
        if codes_all:
            victim_code = codes_all[-1]
            tp2 = S.thumb_path(victim_code, root)
            if not os.path.isfile(tp2):
                S.ensure_thumb(victim_code, root)
            S.delete_sticker(victim_code, root)
            chk("删图顺手删缩略图", not os.path.isfile(tp2), tp2)

        section("插件：心情怎么压成给模型看的话")
        from bigfish.plugins.sticker import _mood_line

        line = _mood_line({"mood": {"value": 72, "label": "心情不错",
                                    "dims": {"happy": 45, "sweet": 12,
                                             "sad": 0}}})
        chk("带分数", "72" in line, line)
        chk("带标签", "心情不错" in line, line)
        chk("带活跃维度", "开心" in line and "甜蜜" in line, line)
        chk("零值维度不出现", "难过" not in line, line)
        chk("空心情返回空串", _mood_line({}) == "")
        chk("脏心情不崩", isinstance(_mood_line({"mood": "abc"}), str))

        section("使用记录：冷却靠它")
        S.record_sent("filehelper", "stk_0001", root)
        last = S.last_sent("filehelper", root)
        chk("记下最近一次", last.get("code") == "stk_0001", last)
        chk("按会话查得到", S.read_usage("filehelper", root).get("stk_0001") == 1)
        chk("别的会话查不到", S.last_sent("other", root) == {})

        shutil.rmtree(d, ignore_errors=True)

    except Exception:
        global FAIL
        FAIL += 1
        FAILED.append("未捕获异常")
        traceback.print_exc()
        print("\n  ✗ 自测中途抛异常（见上）")
    finally:
        try:
            os.environ.pop("BIGFISH_DATA_ROOT", None)
        except Exception:
            pass
        shutil.rmtree(root, ignore_errors=True)

    print("\n" + "=" * 66)
    print(f"结果：{PASS}/{PASS + FAIL} 通过")
    if FAILED:
        print("失败项：")
        for x in FAILED:
            print(f"  · {x}")
    print("=" * 66)
    return 0 if FAIL == 0 else 1


# ── 小工具 ──

def _is_gif(path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(6) in (b"GIF87a", b"GIF89a")
    except Exception:
        return False


def _size(path):
    try:
        from PIL import Image

        with Image.open(path) as im:
            return im.size
    except Exception:
        return (0, 0)


def _open_hold(path):
    """开一个句柄不放，模拟"文件被占用"。"""
    try:
        return open(path, "rb")
    except Exception:
        return None


def _close_hold(h):
    try:
        if h:
            h.close()
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
