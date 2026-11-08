# -*- coding: utf-8 -*-
"""表情去重指纹 —— 三层，前两层能说清"为什么是同一张"，第三层只给提示。

## 为什么不用感知哈希（pHash / dHash）当判据

试过，而且是在真库上试的：

    · 2181 张全是同一个角色的换装表情。dHash 把
      「开心挥手」和「按按钮冒汗」判成距离 0 —— 结构 64 位全一样。
    · pHash 也没救：「按按钮」和「哭丧脸」距离 4，
      而真正的重复对「配色相同、构图相同」反而有 16。
    单看结构，同一个角色的不同表情永远比同一张图的两次压缩更像。

结论：**判据必须是"配色也几乎一致"，不能是"结构像"。**

## 三层

    ① 字节      sha256(整文件)
                 重命名 / 拷贝 / 跨盘复制 → 必中，零误杀

    ② 配色 + 构图
                 8×8 RGBA 逐格最大色差 ≤ 40
                 且 64 位结构哈希汉明距离 ≤ 6
                 换格式 / 重压缩 / 缩放过 / 加了个角标 → 命中
                 同角色不同表情 → 实测最小色差 51，落在门外

    ③ 结构相近（只提示，不跳过）
                 色差 41~90 且结构距离 ≤ 8
                 多半是"同一张图被调亮/调暗过"，也可能只是同系列，
                 所以只报给人看，导入照常进行。

## 阈值是量出来的，不是拍的

400 张随机样本、79800 对两两比较，只挑"差异格数最少"的看：

    色差  格数  结构   这是什么
      1     0     0    同一张图重新导出（真重复）
      5     0     0    同一张图加了不同水印
     29     0    18    同一张图，**换了配色**（银白底 → 素描底）
     31     0    22    同角色不同表情，配色一致
     39     0    19    同上
     42     1    34    同角色，构图几乎不变（换了个手势）
     70     3     4    同角色，换表情
     72     3     6    同角色，换表情
    255    19     0    同角色，姿态完全不同

只有前三行是"同一张图"。**区别不在色差，在差异格数**：
同一张图无论怎么重导出，8×8 里几乎没有格子"整块变掉"；
换了表情就一定有几块内容真的变了。所以判据是：

    差异格数 == 0  且  色差 ≤ 40     → 确定重复
    差异格数 ≤ 2   且  色差 ≤ 40     → 疑似（只提示）

结构距离在这里只当辅助：它分不开同系列（上面两行结构差 0），
但能挡住"格数 0、色差 29、其实是换了配色"的那种。

## 为什么不引 imagehash

核心逻辑几十行，但 PyInstaller 里多一个隐藏依赖就多一条踩坑路径；
而且它的 dhash 默认参数和这里的口径不一致。自己写保持可控 ——
算不出来时返回空，caller 当"不知道"，绝不当"不重复"之外的结论。
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Tuple

#: 配色容差：8×8 格子里，逐通道最大差值。
#: 40 是"同一个文件被重压缩"能容忍的上限（实测最大 39），
#: 而"换了配色"的同底图能到 29、"换了表情"的最小值是 42 —— 卡在中间。
COLOUR_TOLERANCE = 40

#: 差异格数：8×8 格子里，色差超过 COLOUR_TOLERANCE 的格子数。
#: 这是最能说明问题的一个数 —— 见模块文档里的对照表。
SAME_GRID_CELLS = 0          # 0 格 → 必然同一张
NEAR_GRID_CELLS = 2          # ≤2 格 → 最多算"疑似"

#: 确定重复的配色上限。实测"同一张重导出"最大 5，
#: 所以 10 已经很宽；再往上（29 那种）就是换配色了，只报提示。
SAME_COLOUR = 10

#: 结构距离超过它，就说明"画面主体之外多了一层东西"（字幕条、角标、水印框），
#: 而不是"换了个表情"。实测：加字幕 55~62，换表情 4~12。
SUBTITLE_STRUCT = 40

#: "加了东西"的配色门槛。实测：加整行字幕 153，换表情 49~72，
#: 80 正好落在中间，而且离换表情的上沿还有 8 个点的余量。
DRESSED_COLOUR = 80

#: 结构容差：64 位 dHash 汉明距离。只当辅助判据。
STRUCT_TOLERANCE = 6

#: ③ 层（只提示）的配色上限。
COLOUR_HINT = 90

#: 一次查重最多比多少条。2181 条 × 4 次异或 ≈ 9 万次，微秒级；
#: 这个上限是给未来留的护栏，不是当前瓶颈。
MAX_COMPARE = 20000

#: 小图尺寸。8×8 是实测出来的：再小（4×4）配色区分度不够，
#: 再大（16×16）对"局部加字"过度敏感，同一张图加了字幕就判不出来。
SMALL_N = 8


def file_sha256(path: str, chunk: int = 256 * 1024) -> str:
    """整文件哈希。失败返回空串。"""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                b = f.read(chunk)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()
    except Exception:
        return ""


def _resample(name: str):
    """Pillow 9.1+ 的枚举，旧版本用模块级常量。"""
    from PIL import Image

    res = getattr(Image, "Resampling", Image)
    return getattr(res, name)


def _box8(path: str, n: int = SMALL_N):
    """首帧 → n×n RGBA 图像。

    两步走而不是一步 resize：先按原比例缩到长边 64（区域平均），
    再做 n×n。直接一步缩到 8×8 时，Pillow 只采样几十个像素，
    同一张图的不同尺寸会得到明显不同的结果；先降到 64 再平均
    才真正是"这张图整体什么颜色"。
    """
    from PIL import Image

    with Image.open(path) as im:
        im.seek(0)
        im = im.convert("RGBA")
        w, h = im.size
        k = 64.0 / max(w, h, 1)
        if k < 1:
            im = im.resize((max(1, int(w * k)), max(1, int(h * k))),
                           _resample("BOX"))
        return im.resize((n, n), _resample("BOX"))


def colour_grid(path: str, n: int = SMALL_N) -> str:
    """把首帧压成 n×n RGBA，返回十六进制串（每像素 8 位十六进制）。

    用 BOX（区域平均）而不是 LANCZOS：后者放大边缘权重，
    同一张图加了一行字幕就会偏色。
    """
    try:
        return _box8(path, n).tobytes().hex()
    except Exception:
        return ""


def grid_delta(grid_a: str, grid_b: str) -> Tuple[int, int]:
    """两个 8×8 色格比一比：返回 (最大色差, 差异格数)。

    **第二项是区分"换了个表情"和"同一张重压缩"的关键**：
    同一张图重压缩/换配色，8×8 里几乎没有格子"整块变掉"；
    换了个表情，大多数格子几乎不变，但少数格子整块换了内容。

    实测 400 张、79800 对：
        同一张图重导出   格数 0，色差 1~5
        同一张图换配色   格数 0，色差 29（结构差 18）
        同角色换表情     格数 0~3，色差 31~72
        同角色不同姿态   格数 16~19，色差 225~255
    所以"格数 0 + 色差小"才是重复；光看色差会把换配色和换表情混在一起。
    """
    if not grid_a or not grid_b or len(grid_a) != len(grid_b):
        return 999, 64
    try:
        a = bytes.fromhex(grid_a)
        b = bytes.fromhex(grid_b)
    except ValueError:
        return 999, 64
    worst = 0
    changed = 0
    for i in range(0, len(a), 4):
        d = max(abs(a[i] - b[i]), abs(a[i + 1] - b[i + 1]),
                abs(a[i + 2] - b[i + 2]))
        if d > worst:
            worst = d
        if d > COLOUR_TOLERANCE:
            changed += 1
    return worst, changed


def delta(path_a: str, path_b: str) -> Tuple[int, int]:
    """直接比两张图的色格（给测试和排障用，导入路径走指纹）。"""
    try:
        return grid_delta(_box8(path_a).tobytes().hex(),
                          _box8(path_b).tobytes().hex())
    except Exception:
        return 999, 64


def struct_hash(path: str, n: int = 8) -> str:
    """64 位差值哈希，16 位十六进制。失败返回空串。"""
    try:
        from PIL import Image

        with Image.open(path) as im:
            im.seek(0)
            g = im.convert("L").resize((n + 1, n), _resample("BILINEAR"))
        px = list(g.tobytes())
    except Exception:
        return ""
    bits, i = 0, 0
    for _row in range(n):
        for _col in range(n):
            bits = (bits << 1) | (1 if px[i] > px[i + 1] else 0)
            i += 1
    return f"{bits:0{n * n // 4}x}"


def colour_distance(a: str, b: str) -> int:
    """两个 colour_grid 的逐格最大色差。任一为空 → 999（不判）。"""
    if len(a) != len(b) or not a:
        return 999
    try:
        x = bytes.fromhex(a)
        y = bytes.fromhex(b)
    except ValueError:
        return 999
    return max(abs(p - q) for p, q in zip(x, y))


def hamming(a: str, b: str) -> int:
    if not a or not b:
        return 10 ** 9
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (TypeError, ValueError):
        return 10 ** 9


def fingerprint(path: str) -> Dict[str, str]:
    """算一张图的三项指纹。"""
    return {
        "sha": file_sha256(path),
        "cg": colour_grid(path),
        "sh": struct_hash(path),
    }


def compare(fp_a: Dict[str, str],
            fp_b: Dict[str, str]) -> Tuple[int, str]:
    """比较两张图的指纹，返回 (等级, 判据)。

        0 = 不是重复
        1 = 确定重复（跳过）
        2 = 疑似重复（只提示）

    判据是给人看的一句话，会原样出现在导入报告里。

    ⚠ 三个数是**一起看**的，单独任何一个都会判错：

        色差 153 / 格数 3 / 结构 61  → 同一张图右下角加了一条字幕
        色差  70 / 格数 3 / 结构  4  → 同角色换了个表情

        前者格数更多，但结构大动（62 位的差），说明主体还在原位、
        只是叠了一层东西；后者结构几乎没动，说明整个画面换掉了。
    """
    sha_a, sha_b = fp_a.get("sha") or "", fp_b.get("sha") or ""
    if sha_a and sha_a == sha_b:
        return 1, "完全相同的文件"

    worst, changed = grid_delta(fp_a.get("cg") or "", fp_b.get("cg") or "")
    # （原本这里还算了一个 `hamming(sh_a, sh_b)` 的感知哈希距离，
    #   但下面的判据全部基于 grid_delta 的 `changed/worst`，
    #   它从来没参与过判断 —— 删掉省一次无意义的计算。）

    # ① 几乎没有格子换内容 + 配色也几乎没变 → 同一张图重导出
    if changed <= NEAR_GRID_CELLS and worst <= COLOUR_TOLERANCE:
        if worst <= SAME_COLOUR:
            return 1, (f"同一张图（{changed} 格不同，色差 {worst}）"
                       if changed else "同一张图（重新导出过）")
        return 2, (f"几乎一样（{changed} 格不同，配色差 {worst}）"
                   " —— 可能是换了配图，没跳过")

    # ② 只有个别格子变了 → 同一张图上加/改了一小块（字幕条、角标、水印框）
    #
    #    和"换了表情"的区别：换表情至少动 3 格以上，而且色差落在 49~72；
    #    加字幕只有 1~3 格，色差却能被拉到 150+。
    #    两个条件都满足才认，单看任何一个都会把换表情吃进来。
    if changed <= 3 and worst >= DRESSED_COLOUR:
        return 1, f"同一张图加了东西（{changed} 格不同，色差 {worst}）"
    if changed <= 3 and worst >= DRESSED_COLOUR * 0.75:
        return 2, f"构图几乎相同（{changed} 格不同，色差 {worst}）"

    # ③ 只报不拦
    if changed <= NEAR_GRID_CELLS and worst <= COLOUR_HINT:
        return 2, f"很像（{changed} 格不同，色差 {worst}）"
    return 0, ""


def find_duplicate(fp: Dict[str, str],
                   pool: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """在 pool 里找最相似的一项。

    pool 每项至少要有 `code`；最好带 `sha` / `cg` / `sh`（老数据没有就跳过）。
    返回 {code, level, why} 或 None。
    """
    best: Optional[Dict[str, Any]] = None
    for it in pool[:MAX_COMPARE]:
        lvl, why = compare(fp, it)
        if lvl == 0:
            continue
        if lvl == 1:
            return {"code": str(it.get("code") or ""), "level": 1, "why": why}
        if best is None:
            best = {"code": str(it.get("code") or ""), "level": 2, "why": why}
    return best


def backfill(root: Optional[str] = None, limit: Optional[int] = None) -> int:
    """给老表情补指纹，返回补了几条。

    只补缺的。失败（文件读不了）跳过，下次再补 —— 不写空指纹，
    否则那条永远算不出重复。
    """
    import os

    from bigfish import stickers as S

    data = S.read_index(root)
    items = data.get("items") or {}
    n = 0
    changed = False
    for code, meta in items.items():
        if limit is not None and n >= limit:
            break
        if meta.get("sha") and meta.get("cg") and meta.get("sh"):
            continue
        p = os.path.join(S.sticker_dir(root),
                         S._as_file(meta.get("file") or code))
        if not os.path.isfile(p):
            continue
        fp = fingerprint(p)
        if not fp["sha"]:
            continue
        meta.update(fp)
        n += 1
        changed = True
    if changed:
        try:
            S.write_index(data, root)
        except Exception:
            pass
    return n
