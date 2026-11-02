"""[bigfish] 决定性对照：资源管理器写的剪贴板，和我们写的差在哪。

═══════════════════════════════════════════════════════════════════════
已经排除了什么
═══════════════════════════════════════════════════════════════════════

    判据错（U+FFFC）              ❌ 排除
    SendKeys 不走系统队列          ❌ 排除（换 keybd_event 一样）
    截图吃剪贴板                   ❌ 排除（改顺序一样）
    剪贴板格式不全（只有 HDROP）    ❌ 排除（PowerShell 写出五个格式也一样）

现在只剩一个变量没测过：
    **用户手动 Ctrl+C 时，剪贴板里到底有什么**

我们一直在拿"我以为的标准格式"去比，
从没拿"资源管理器真实写出的那份"去比。

═══════════════════════════════════════════════════════════════════════
这个脚本干什么
═══════════════════════════════════════════════════════════════════════

    1. 等你手动复制（脚本会提示，按回车继续）
    2. 把剪贴板**逐格式、逐字节** dump 出来，存成文件
    3. 用我们自己的写法写一遍，再 dump 一次
    4. 两份并排 diff

用法：
    # 管理员
    python tools/clip_diff.py

流程：
    · 脚本启动后会说「请现在去资源管理器复制那张 gif」
    · 你复制完，回车
    · 脚本 dump 完再自己写一份、dump、比对
    · 最后打印差异，并把两份原始字节存到 .tmp/clipdiff/
"""
from __future__ import annotations

import ctypes
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

OUT_DIR = os.path.join(_ROOT, ".tmp", "clipdiff")


# ══════════════════════════════════════════════════════════════════════
# 把剪贴板每个格式的原始字节全 dump 出来
# ══════════════════════════════════════════════════════════════════════

def dump_all() -> dict:
    """返回 {格式名: 原始字节}，外加一个有序的格式列表。

    这里刻意用 GlobalSize + memcpy 拿原始字节，
    不用 GetClipboardData 的 Python 包装 —— 那个会把二进制解成
    字符串或元组，细节就丢了。要比对就必须看字节。
    """
    out = {"order": [], "data": {}, "err": ""}
    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32

    u32.OpenClipboard.argtypes = [ctypes.c_void_p]
    u32.GetClipboardData.restype = ctypes.c_void_p
    u32.GetClipboardData.argtypes = [ctypes.c_uint]
    k32.GlobalSize.restype = ctypes.c_size_t
    k32.GlobalSize.argtypes = [ctypes.c_void_p]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]

    if not u32.OpenClipboard(None):
        out["err"] = "打不开剪贴板"
        return out

    try:
        fmt = 0
        while True:
            fmt = u32.EnumClipboardFormats(fmt)
            if not fmt:
                break
            try:
                buf = ctypes.create_unicode_buffer(256)
                n = u32.GetClipboardFormatNameW(fmt, buf, 256)
                name = buf.value if n else f"std-{fmt}"
            except Exception:
                name = f"std-{fmt}"
            out["order"].append((fmt, name))

            h = u32.GetClipboardData(fmt)
            if not h:
                out["data"][name] = b""
                continue
            size = k32.GlobalSize(h)
            if not size:
                out["data"][name] = b""
                continue
            p = k32.GlobalLock(h)
            if not p:
                out["data"][name] = b""
                continue
            try:
                out["data"][name] = ctypes.string_at(p, size)
            finally:
                k32.GlobalUnlock(h)
    finally:
        u32.CloseClipboard()
    return out


def parse_hdrop(raw: bytes) -> dict:
    """解出 DROPFILES 结构，看 pFiles/fWide/路径都是什么。

    这是最可能出问题的地方：pFiles 偏移差 4、fWide 写成 0、
    路径结尾少一个 \\0 —— 任何一处都能让接收方解析出空列表。
    """
    info = {"size": len(raw), "raw_head": raw[:32].hex() if raw else ""}
    if len(raw) < 20:
        info["err"] = f"太短（{len(raw)} 字节，DROPFILES 至少 20）"
        return info

    class DROPFILES(ctypes.Structure):
        _fields_ = [
            ("pFiles", ctypes.c_uint),
            ("pt_x", ctypes.c_long),
            ("pt_y", ctypes.c_long),
            ("fNC", ctypes.c_int),
            ("fWide", ctypes.c_int),
        ]

    df = DROPFILES.from_buffer_copy(raw[:ctypes.sizeof(DROPFILES)])
    info.update({
        "pFiles": df.pFiles,
        "pt": (df.pt_x, df.pt_y),
        "fNC": df.fNC,
        "fWide": df.fWide,
        "struct_size": ctypes.sizeof(DROPFILES),
    })

    try:
        body = raw[df.pFiles:]
        if df.fWide:
            text = body.decode("utf-16-le", errors="replace")
        else:
            text = body.decode("mbcs", errors="replace")
        info["paths"] = [p for p in text.split("\x00") if p]
    except Exception as exc:
        info["err"] = f"解析路径失败：{exc}"
    return info


def save(tag: str, d: dict) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, f"{tag}.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write(f"格式顺序：{d['order']}\n\n")
        for _fmt, name in d["order"]:
            raw = d["data"].get(name, b"")
            f.write(f"--- {name} ({len(raw)} 字节) ---\n")
            f.write(raw[:200].hex() + "\n")
            if name in ("std-15", "CF_HDROP"):
                f.write(repr(parse_hdrop(raw)) + "\n")
            f.write("\n")
    return p


def main() -> int:
    print("=" * 70)
    print("剪贴板对照：资源管理器写的 vs 我们写的")
    print("=" * 70)

    print("\n【第 1 步】请在资源管理器里选中一张 .gif，按 Ctrl+C")
    print("         （随便哪张都行，D:\\大脑\\表情测试\\动图_512.gif 就行）")
    print("\n复制完，按回车继续…")
    try:
        input()
    except EOFError:
        pass

    a = dump_all()
    print(f"\n资源管理器写的：")
    print(f"  格式顺序：{a['order']}")
    for _f, name in a["order"]:
        raw = a["data"].get(name, b"")
        print(f"  {name}: {len(raw)} 字节")
    if "std-15" in a["data"]:
        h = parse_hdrop(a["data"]["std-15"])
        print(f"  HDROP 解析：pFiles={h.get('pFiles')} "
              f"fWide={h.get('fWide')} struct={h.get('struct_size')}")
        print(f"               paths={h.get('paths')}")

    pa = save("A-资源管理器", a)
    print(f"\n  原始字节已存：{pa}")

    # ── 用我们自己的写法写同一张图 ──
    files = parse_hdrop(a["data"].get("std-15", b"")).get("paths") or []
    if not files:
        print("\n✗ 没从剪贴板里解析出路径，无法继续对照")
        return 1
    target = files[0]
    print(f"\n【第 2 步】用我们自己的写法写同一个文件：{target}")

    # 先清空，保证 B 组是干净的
    try:
        import win32clipboard

        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.CloseClipboard()
    except Exception:
        pass
    time.sleep(0.2)

    from bigfish.wechat_sender import UiaSender

    s = UiaSender()
    ok = s._write_clipboard_files(target)
    print(f"  写入：{'成功' if ok else '失败'}")
    time.sleep(0.3)

    b = dump_all()
    print(f"\n我们写的：")
    print(f"  格式顺序：{b['order']}")
    for _f, name in b["order"]:
        raw = b["data"].get(name, b"")
        print(f"  {name}: {len(raw)} 字节")
    if "std-15" in b["data"]:
        h = parse_hdrop(b["data"]["std-15"])
        print(f"  HDROP 解析：pFiles={h.get('pFiles')} "
              f"fWide={h.get('fWide')} struct={h.get('struct_size')}")
        print(f"               paths={h.get('paths')}")

    pb = save("B-我们写的", b)
    print(f"\n  原始字节已存：{pb}")

    # ── 差异 ──
    print("\n" + "=" * 70)
    print("差异")
    print("=" * 70)
    na = {n for _f, n in a["order"]}
    nb = {n for _f, n in b["order"]}
    only_a = na - nb
    only_b = nb - na
    if only_a:
        print(f"  资源管理器有、我们没有：{sorted(only_a)}")
    if only_b:
        print(f"  我们有、资源管理器没有：{sorted(only_b)}")
    if not only_a and not only_b:
        print("  格式种类完全一致")

    ha = parse_hdrop(a["data"].get("std-15", b""))
    hb = parse_hdrop(b["data"].get("std-15", b""))
    for k in ("pFiles", "fWide", "struct_size", "size"):
        if ha.get(k) != hb.get(k):
            print(f"  HDROP.{k}: 资源管理器={ha.get(k)!r}  我们={hb.get(k)!r}")
    if ha.get("paths") == hb.get("paths"):
        print("  HDROP 路径一致")
    else:
        print(f"  HDROP 路径不同：{ha.get('paths')} vs {hb.get('paths')}")

    ra = a["data"].get("std-15", b"")
    rb = b["data"].get("std-15", b"")
    if ra and rb and ra != rb:
        print(f"  HDROP 原始字节不同（{len(ra)} vs {len(rb)} 字节）")
        print(f"    资源管理器前 48 字节：{ra[:48].hex()}")
        print(f"    我们       前 48 字节：{rb[:48].hex()}")
    elif ra == rb:
        print("  HDROP 原始字节完全相同")

    print("\n" + "=" * 70)
    print("接下来：剪贴板里现在是我们写的那份。")
    print("去微信点输入框按 Ctrl+V，能贴进去吗？")
    print("  · 能  → 那问题在脚本的点击/按键，不在剪贴板")
    print("  · 不能 → 字节差异就是根因，看上面哪一行不一样")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
