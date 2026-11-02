"""[bigfish] 表情包自测 —— 不碰微信、不调模型，只验逻辑。

它锁定的是三件最容易悄悄坏掉的事：

  1. 表情库的增删读描述（vault 那层）
  2. 引擎的冷却/概率/文件检查（哪一层否决了，都必须是"安静地不发"）
  3. sticker 插件的挑选入口（没有库就不发；冷却中就不发）

⚠ 刻意**不测真发**：真发要开微信、要管理员、会抢焦点。
  那部分只能手测。这里测的是"该不该发"这个决策本身。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

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
    d = tempfile.mkdtemp(prefix="bigfish-sticker-")
    os.environ["BIGFISH_DATA_ROOT"] = d
    return d


def _make_gif(path, size=64, frames=3):
    from PIL import Image

    imgs = []
    for i in range(frames):
        im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        im.paste((255, 200, 0, 255), (i * 4, i * 4, size, size))
        imgs.append(im)
    imgs[0].save(path, save_all=True, append_images=imgs[1:],
                 duration=100, loop=0)
    return path


def main():
    root = _tmp_root()
    try:
        from bigfish import vault

        section("配置：默认关，且归一化兜底")
        cfg = vault.read_sticker_config(root)
        chk("默认关闭", cfg["enabled"] is False)
        chk("默认概率 0.15", abs(cfg["rate"] - 0.15) < 1e-9)
        chk("默认冷却 180", cfg["cooldown"] == 180)
        chk("默认模式 ai", cfg["mode"] == "ai")

        ok, err = vault.write_sticker_config({"rate": 5}, root)
        chk("写了越界概率也接受", ok, err)
        chk("概率被夹到 1.0", vault.read_sticker_config(root)["rate"] == 1.0)

        ok, _ = vault.write_sticker_config({"rate": "abc"}, root + "_nope")
        chk("脏数据不崩", isinstance(ok, bool))

        # 手改坏 JSON：读回来必须是默认值，不能让引擎整轮崩
        bad = os.path.join(root, vault.STICKER_CONFIG_NAME)
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        cfg2 = vault.read_sticker_config(root)
        chk("坏 JSON 回退默认", cfg2["enabled"] is False and cfg2["mode"] == "ai")

        section("表情库：导入、描述、删除")
        chk("空库返回空表", vault.list_stickers(root) == [])

        src = _make_gif(os.path.join(root, "src.gif"))
        ok, dst = vault.import_sticker(src, root)
        chk("导入成功", ok and os.path.isfile(dst), dst)
        chk("落在 stickers/ 下",
            os.path.dirname(dst) == vault.sticker_dir(root))

        lib = vault.list_stickers(root)
        chk("库里有 1 张", len(lib) == 1, lib)
        chk("识别为表情", lib[0]["kind"] == "sticker")
        chk("初始无描述", lib[0]["desc"] == "")

        ok, _ = vault.import_sticker(src, root)
        chk("重名不覆盖", ok and len(vault.list_stickers(root)) == 2)

        name = os.path.basename(dst)
        ok, _ = vault.write_sticker_desc(name, "得意地笑", root)
        chk("写描述成功", ok)
        lib = {e["name"]: e for e in vault.list_stickers(root)}
        chk("描述读得回来", lib[name]["desc"] == "得意地笑")

        ok, _ = vault.write_sticker_desc(name, "   ", root)
        lib = {e["name"]: e for e in vault.list_stickers(root)}
        chk("空描述等于清除", lib[name]["desc"] == "")

        png = os.path.join(root, "x.txt")
        with open(png, "w") as f:
            f.write("not an image")
        ok, err = vault.import_sticker(png, root)
        chk("非图片格式被拒", not ok, err)
        chk("拒绝原因可读", "格式" in err, err)

        ok, _ = vault.delete_sticker(name, root)
        chk("删除成功", ok and not os.path.exists(dst))
        chk("删完只剩 1 张", len(vault.list_stickers(root)) == 1)

        section("使用记录：冷却靠它")
        vault.record_sticker_sent("filehelper", "a.gif")
        usage = vault.sticker_usage("")
        chk("记录了 last", usage.get("_last", {}).get("name") == "a.gif")
        chk("记录了会话", usage.get("_last", {}).get("chat") == "filehelper")
        chk("按会话查得到计数",
            vault.sticker_usage("filehelper").get("a.gif") == 1)

        section("媒体检查：发之前的门槛")
        from bigfish.wechat_sender import check_media, classify_media

        chk("gif 判表情", classify_media("a.GIF") == "sticker")
        chk("png 判图片", classify_media("a.png") == "image")
        chk("zip 判文件", classify_media("a.zip") == "file")

        g = _make_gif(os.path.join(root, "ok.gif"))
        ok, why = check_media(g)
        chk("正常 gif 可发", ok, why)
        ok, why = check_media("")
        chk("空路径被拒", not ok)
        ok, why = check_media(os.path.join(root, "missing.gif"))
        chk("不存在被拒", not ok and "不存在" in why, why)

        empty = os.path.join(root, "empty.gif")
        open(empty, "wb").close()
        ok, why = check_media(empty)
        chk("空文件被拒", not ok, why)

        section("插件：没有库就不发 / 冷却 / 随机模式")
        from bigfish import plugins

        plugins.ensure_loaded()
        inv = plugins.inventory(root)
        ids = {p["id"]: p for p in inv["plugins"]}
        chk("sticker 已登记", "sticker" in ids, list(ids))
        chk("scope 是 sticker", ids["sticker"]["scope"] == "sticker")
        chk("默认不启用", ids["sticker"]["enabled"] is False)

        ctx = {"chat_id": "filehelper", "user_text": "哈哈", "reply_text": "笑死"}
        # 没启用 → collect_sticker 不给东西
        got = plugins.collect_sticker(ctx, root)
        chk("未启用时不发", got == "")

        plugins.set_enabled("sticker", True, root)
        # 启用但库里有图（前面删了一张还剩一张）
        lib = vault.list_stickers(root)
        chk("库里有货", len(lib) >= 1, lib)

        from bigfish.plugins import sticker as st

        # 冷却中：刚发过 → 不发
        vault.record_sticker_sent("filehelper", "x.gif")
        os.environ["BIGFISH_DATA_ROOT"] = root
        got = st.stamp({**ctx, "config": {"mode": "random", "cooldown": 600}})
        chk("冷却中不发", got == "", got)

        # 冷却为 0 → 允许，且随机模式必给一个存在的路径
        got = st.stamp({**ctx, "config": {"mode": "random", "cooldown": 0}})
        chk("冷却解除后会给路径", bool(got) and os.path.isfile(got), got)
        chk("给的是 gif", got.lower().endswith(".gif"), got)

        section("插件：尺寸过界的图不选")
        big = _make_gif(os.path.join(root, "big.gif"), size=2048, frames=1)
        ok, dst_big = vault.import_sticker(big, root)
        chk("大图能进库", ok, dst_big)
        usable = [e for e in vault.list_stickers(root) if st._usable(e)]
        chk("2048 的图被排除",
            all(e["name"] != os.path.basename(dst_big) for e in usable),
            [e["name"] for e in usable])

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
        shutil.rmtree(root + "_nope", ignore_errors=True)

    print("\n" + "=" * 66)
    print(f"结果：{PASS}/{PASS + FAIL} 通过")
    if FAILED:
        print("失败项：")
        for x in FAILED:
            print(f"  · {x}")
    print("=" * 66)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
