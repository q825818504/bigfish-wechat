"""[bigfish] 表情包真机验证 —— 真的往微信发一张，然后去数据库确认。

这是唯一能回答「这台机器上她到底能不能发表情」的测试。
其他测试都是离线验逻辑，这个是真的写微信。

它会做四件事：
  1. 造一张 3 帧动图（放在临时目录，不进你的表情库）
  2. 用 UiaSender.send_media 发到「文件传输助手」
  3. 回读数据库，确认落库的**类型是「动画表情」**而不是「图片」
  4. 无论成败都把临时文件删掉

为什么必须验「类型」：
    发出去不等于发对了。如果微信把它当图片收（local_type 3），
    对方看到的是一张带白边的图，不能长按添加，也不会动。
    表情是 local_type 47。这一条是整条路线的成败判据。

用法：
    python tools/live_sticker_test.py            # 发到文件传输助手
    python tools/live_sticker_test.py --dry      # 只看检查项，不发

前置：管理员权限、微信已登录、桌面解锁、微信没收到托盘。
"""
from __future__ import annotations

import os
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
    return cond


def make_gif(path: str, size: int = 512, frames: int = 4) -> str:
    """造一张明显在动的动图。静止的图验不出「表情是否还动」。"""
    from PIL import Image, ImageDraw
    import math

    imgs = []
    for i in range(frames):
        t = i / frames * 2 * math.pi
        im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        cx, cy = size // 2, size // 2 + int(math.sin(t) * size * 0.05)
        r = int(size * 0.36)
        d.ellipse([cx - r, cy - r, cx + r, cy + r],
                  fill=(255, 214, 102, 255), outline=(60, 45, 20, 255),
                  width=max(2, size // 64))
        ex = int(math.cos(t) * size * 0.05)
        er = max(3, size // 16)
        for sx in (-1, 1):
            px = cx + ex + sx * int(size * 0.13)
            py = cy - int(size * 0.05)
            d.ellipse([px - er, py - er, px + er, py + er], fill=(30, 30, 30, 255))
        mw = int(size * 0.16)
        d.arc([cx - mw, cy + int(size * 0.04), cx + mw, cy + int(size * 0.10) + 8],
              0, 180, fill=(60, 45, 20, 255), width=max(2, size // 64))
        imgs.append(im)
    imgs[0].save(path, save_all=True, append_images=imgs[1:],
                 duration=120, loop=0, disposal=2)
    return path


def main() -> int:
    dry = "--dry" in sys.argv
    print("=" * 68)
    print("表情包真机验证")
    print("=" * 68)

    tmpdir = tempfile.mkdtemp(prefix="bigfish-live-sticker-")
    gif = os.path.join(tmpdir, "live_test.gif")
    try:
        make_gif(gif)
        from PIL import Image

        with Image.open(gif) as im:
            n = getattr(im, "n_frames", 1)
            size = im.size
        print(f"\n素材：{size[0]}×{size[1]}，{n} 帧，"
              f"{os.path.getsize(gif) / 1024:.1f}KB")
        chk("动图真的有多帧", n >= 2, f"只有 {n} 帧")

        from bigfish.wechat_sender import (SendError, UiaSender,
                                           classify_media, desktop_available,
                                           is_admin)

        print("\n── 环境 ──")
        chk("管理员权限", is_admin(), "UIA 热激活需要提权")
        chk("桌面已解锁", desktop_available(), "锁屏时 UIA 无法操作")
        chk("gif 判为表情", classify_media(gif) == "sticker")

        if dry:
            print("\n--dry：只验环境，不发送。")
            return _summary()

        print("\n── 发送 ──")
        t0 = time.time()
        try:
            s = UiaSender()
            s.wake()
            chk("UIA 就绪", s.ready)

            # 发送前先把基线 seq 记下来 —— 后面回读要靠它区分"新的"和"旧的"
            before_seq = 0
            try:
                from wechatauto import WeChatDB

                db = WeChatDB()
                hits = db.search_contact("文件传输助手")
                chat = hits[0]["username"] if hits else "filehelper"
                msgs = db.get_messages(chat, limit=1)
                before_seq = int(msgs[0]["sort_seq"]) if msgs else 0
                print(f"     基线 seq = {before_seq}（chat={chat}）")
            except Exception as exc:
                print(f"     （基线读取跳过：{exc}）")

            r = s.send_media("文件传输助手", gif)
            dt = time.time() - t0
            chk("send_media 返回成功", bool(r.ok),
                getattr(r, "detail", "") or "")
            print(f"     耗时 {dt:.1f}s")

            # ---- 判据：库里落的是不是「动画表情」 ----
            print("\n── 回读数据库（判据：类型必须是「动画表情」）──")
            try:
                from wechatauto import WeChatDB

                db = WeChatDB()
                hits = db.search_contact("文件传输助手")
                chat2 = hits[0]["username"] if hits else "filehelper"
                rows = db.get_messages(chat2, limit=6)
                rows = sorted(rows, key=lambda x: int(x.get("sort_seq") or 0),
                              reverse=True)
                fresh = [x for x in rows
                         if int(x.get("sort_seq") or 0) > before_seq]
                print(f"     发送后新增 {len(fresh)} 条")
                for x in fresh[:3]:
                    print(f"       · type={x.get('type')!r} "
                          f"sender_id={x.get('sender_id')}")
                got = [str(x.get("type") or "") for x in fresh[:2]]
                is_sticker = "动画表情" in got
                chk("新增消息里有「动画表情」", is_sticker,
                    f"读到 {got}。如果是「图片」，说明微信没把它当表情，"
                    f"动图也不会动；如果一条都没有，说明粘贴没生效")
            except Exception as exc:
                chk("回读数据库", False, str(exc))
        except SendError as exc:
            chk("发送无异常", False, f"{exc}")
            return _summary()
        except Exception as exc:
            chk("发送无异常", False, f"{exc}")
            traceback.print_exc()
            return _summary()

        print("\n── 手动确认（我看不到你的屏幕）──")
        print("  打开微信文件传输助手，看最新那条：")
        print("   · 是不是方方正正、贴着气泡边 → 表情")
        print("   · 是不是带白边、能点开大图 → 图片（不对）")
        print("   · 它动不动？")
        print("   · 长按有没有「添加」→ 有就是原生表情")

    finally:
        try:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

    return _summary()


def _summary() -> int:
    print("\n" + "=" * 68)
    print(f"结果：{PASS}/{PASS + FAIL} 通过")
    if FAILED:
        print("失败：")
        for x in FAILED:
            print(f"  · {x}")
    print("=" * 68)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
