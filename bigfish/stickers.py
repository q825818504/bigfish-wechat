"""[bigfish] 表情库 —— 她的弹药库。

═══════════════════════════════════════════════════════════════════════
这个模块只管一件事：**表情文件的存取与描述**
═══════════════════════════════════════════════════════════════════════

不管什么时候发、发几张、要不要发 —— 那是插件的事（plugins/sticker.py）。
这里只负责：

    · 收进来（任意图片格式 → 统一转 GIF）
    · 编个号（每张一个唯一代码，发送时靠它引用）
    · 存描述（概览一句话 + 详细一段，用来判断什么情景该用）
    · 查出来（列表、单张、按代码）
    · 记流水（发过哪些，用于冷却）

═══════════════════════════════════════════════════════════════════════
为什么一律转 GIF
═══════════════════════════════════════════════════════════════════════

微信只把 **.gif 文件**识别成原生表情（local_type 47）。png/jpg 发出去
就是普通图片 —— 带白边、不能长按添加、动图也不会动。

用户的原话：「自动将任意格式的图片转化为gif保存到表情包库」。
这不是偏好，是唯一能让微信认成表情的格式。

代价必须说清楚（转换是有损的）：
    · GIF 最多 256 色 → 渐变会 banding，照片类会糊
    · GIF 只有 1 位透明 → PNG 的半透明边缘会变成硬边
    · 大图转 GIF 体积可能反而变大

所以转换时做了三件事缓解：
    ① 尺寸超过 1024 就等比缩小（微信上限，超过会被当文件发）
    ② 调色板用 ADAPTIVE（按实际颜色分布取，不是固定 216 色）
    ③ 多帧保留（GIF / 动 WebP 都能逐帧转，动图不会变成静图）

═══════════════════════════════════════════════════════════════════════
代码（code）是什么
═══════════════════════════════════════════════════════════════════════

每张表情有一个短代码，形如 `stk_0007`。

为什么不用文件名当标识：文件名是给用户看的，会改；代码是给程序
和模型看的，一辈子不变。模型挑表情时返回代码，比返回中文名可靠 ——
中文名可能重名、可能带空格、可能被用户改掉。

为什么不用语义化前缀（如 `angry_01`）：语义要靠 brief 表达，
而 brief 是可以随时改的。用递增序号最稳，也最短。

═══════════════════════════════════════════════════════════════════════
index.json（schema v2）
═══════════════════════════════════════════════════════════════════════

    {
      "version": 2,
      "seq": 12,                       // 下一个可用序号
      "items": {
        "stk_0007": {
          "code": "stk_0007",
          "file": "stk_0007.gif",      // 相对 stickers/
          "brief": "生气",              // 概览：列表里显示这一句
          "detail": "眉毛倒竖、脸红……", // 详细：双击才显示
          "tags": ["生气", "愤怒"],
          "created": 1699999999,
          "source": "import",          // import / received / generated
          "auto": true,                // brief/detail 是不是模型生成的
          "frames": 4, "w": 512, "h": 512
        }
      }
    }

旧格式（`{文件名: {"desc": "..."}}`）会在第一次读的时候自动迁移 ——
desc 放进 brief，detail 留空。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from typing import Any, Dict, List, Optional, Tuple

from bigfish.vault import WriteDenied, _atomic_write_json, resolve_data_root

# ══════════════════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════════════════

STICKER_DIR = "stickers"
INDEX_NAME = "index.json"
INDEX_VERSION = 2

#: 微信把 >1024 的图当文件发，不再是表情
MAX_DIM = 1024

#: 单张上限。再大会明显卡住输入框渲染
MAX_BYTES = 8 * 1024 * 1024

#: 认得的输入格式（输出一律 .gif）
INPUT_EXT = (".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".ico")

#: 代码格式：stk_ + 4 位以上数字
CODE_RE = re.compile(r"^stk_(\d{4,})$")

#: brief 长度上限（列表里一行放得下）
BRIEF_MAX = 12
#: detail 长度上限
DETAIL_MAX = 200

#: 情绪维度 —— 和 vault.MOOD_DIMS 一一对应。
#:
#: 为什么要这个字段：brief 是自由文本（"高兴"、"无语"、"生气"），
#: 而心情系统只有七个维度。两边对不上，挑表情时就没法按心情匹配 ——
#: 模型只能凭 brief 猜，猜得准不准全看运气。
#:
#: 存 key 而不是中文标签：标签会改，key 不会。标签在显示时查表。
MOOD_DIM_KEYS = ("happy", "calm", "sweet", "heartache",
                 "worry", "sad", "jealous")

#: key → 中文标签。和 vault.MOOD_DIMS 必须一致（有测试断言）。
MOOD_DIM_LABELS = {
    "happy": "开心",
    "calm": "踏实",
    "sweet": "甜蜜",
    "heartache": "心疼",
    "worry": "担心",
    "sad": "难过",
    "jealous": "吃醋",
}

#: 强度默认值。老数据没有这个字段，按"中等"处理。
#:
#: 为什么需要强度：库里可能十张都是「高兴」，但一张是微微一笑、
#: 一张是笑到打滚。光看 brief 模型分不出来，只能随机挑 ——
#: 结果就是心情 90 分的时候发了个"呵呵"，或者心情 30 分的时候发狂喜。
#:
#: 1=很淡  2=轻  3=中等  4=强  5=极强
INTENSITY_DEFAULT = 3


# ══════════════════════════════════════════════════════════════════════
# 路径
# ══════════════════════════════════════════════════════════════════════

def sticker_dir(root: Optional[str] = None) -> str:
    """表情库目录的路径。**不创建它。**

    ⚠ 早先这里无条件 makedirs，结果鲸落删掉数据目录之后，
      随便一次读操作（比如界面刷新）又把空目录建回来了 ——
      "鲸落之后她不存在"这件事就不成立了。

    需要写的时候自己调 ensure_dir()。
    """
    return os.path.join(root or resolve_data_root(), STICKER_DIR)


def ensure_dir(root: Optional[str] = None) -> str:
    """确保目录存在并返回路径。只有写操作才该调它。"""
    d = sticker_dir(root)
    os.makedirs(d, exist_ok=True)
    return d


def index_path(root: Optional[str] = None) -> str:
    return os.path.join(sticker_dir(root), INDEX_NAME)


def abs_path(code_or_file: str, root: Optional[str] = None) -> str:
    """把代码或文件名换成绝对路径。"""
    return os.path.join(sticker_dir(root), _as_file(code_or_file))


def _as_file(code_or_file: str) -> str:
    """代码 → 文件名。已经是文件名就原样返回。"""
    v = str(code_or_file or "").strip()
    if not v:
        return ""
    if os.path.splitext(v)[1]:
        return v                      # 带扩展名，当文件名
    return v + ".gif"                 # 不带扩展名，当代码


# ══════════════════════════════════════════════════════════════════════
# index 读写（含旧格式迁移）
# ══════════════════════════════════════════════════════════════════════

def _empty_index() -> Dict[str, Any]:
    return {"version": INDEX_VERSION, "seq": 1, "items": {}}


def read_index(root: Optional[str] = None) -> Dict[str, Any]:
    """读 index.json。缺失/损坏/旧格式都返回可用结构，不抛。

    旧格式（v1，`{文件名: {desc}}`）就地迁移成 v2 并写回 ——
    迁移只做一次，之后都是 v2。
    """
    p = index_path(root)
    if not os.path.isfile(p):
        return _empty_index()

    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return _empty_index()

    if not isinstance(raw, dict):
        return _empty_index()

    # 已经是 v2
    if raw.get("version") == INDEX_VERSION and isinstance(
            raw.get("items"), dict):
        out = _empty_index()
        out["items"] = raw["items"]
        try:
            out["seq"] = max(1, int(raw.get("seq") or 1))
        except (TypeError, ValueError):
            out["seq"] = 1
        return out

    # 旧格式迁移：{文件名: {"desc": "..."}} 或 {文件名: "..."}
    migrated = _empty_index()
    seq = 1
    for key, val in raw.items():
        if key in ("version", "seq", "items"):
            continue
        if not isinstance(key, str):
            continue
        desc = ""
        if isinstance(val, dict):
            desc = str(val.get("desc") or val.get("brief") or "")
        elif isinstance(val, str):
            desc = val
        code = f"stk_{seq:04d}"
        seq += 1
        migrated["items"][code] = {
            "code": code,
            "file": _as_file(key),
            "brief": desc[:BRIEF_MAX],
            "detail": "",
            "tags": [],
            "created": int(time.time()),
            "source": "legacy",
            "auto": False,
        }
    migrated["seq"] = seq

    # 迁移结果写回，失败也不影响本次使用
    try:
        _atomic_write_json(p, migrated)
    except Exception:
        pass
    return migrated


def write_index(data: Dict[str, Any], root: Optional[str] = None) -> None:
    _atomic_write_json(index_path(root), data)


# ══════════════════════════════════════════════════════════════════════
# 代码生成
# ══════════════════════════════════════════════════════════════════════

def next_code(data: Dict[str, Any]) -> str:
    """取下一个可用代码，并推进 seq。调用方负责 write_index。

    跳过已被占用的号 —— 用户可能手动塞了个 stk_0007.gif 进来。
    """
    seq = int(data.get("seq") or 1)
    items = data.get("items") or {}
    while True:
        code = f"stk_{seq:04d}"
        seq += 1
        if code in items:
            continue
        data["seq"] = seq
        return code


# ══════════════════════════════════════════════════════════════════════
# 格式转换：任意图片 → GIF
# ══════════════════════════════════════════════════════════════════════

def convert_to_gif(src: str, dst: str,
                   max_dim: int = MAX_DIM) -> Tuple[bool, str, Dict[str, Any]]:
    """把任意图片转成 GIF。返回 (ok, 错误, 元信息)。

    元信息：{w, h, frames, converted, why}

    ⚠ 转换是有损的，理由写在模块文档里。这里只保证：
      · 多帧保留（动图不会变静图）
      · 超过 max_dim 等比缩小
      · 透明通道尽量保住（GIF 1 位透明）
    """
    try:
        from PIL import Image
    except Exception as exc:
        return False, f"Pillow 不可用：{exc}", {}

    info: Dict[str, Any] = {}
    try:
        with Image.open(src) as im:
            fmt = (im.format or "").upper()
            n_frames = getattr(im, "n_frames", 1)
            w, h = im.size
            info.update({"w": w, "h": h, "frames": n_frames})

            # 已经是合规 GIF：直接复制，不重编码（避免二次损失）
            if fmt == "GIF" and max(w, h) <= max_dim:
                shutil.copy2(src, dst)
                info["converted"] = False
                info["why"] = "原样保留"
                return True, "", info

            # 需要缩放或转格式
            scale = 1.0
            if max(w, h) > max_dim:
                scale = max_dim / float(max(w, h))
                nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
            else:
                nw, nh = w, h
            info["out_w"], info["out_h"] = nw, nh

            def _prep(frame: "Image.Image") -> "Image.Image":
                f = frame.convert("RGBA")
                if (nw, nh) != f.size:
                    f = f.resize((nw, nh), Image.LANCZOS)
                return f

            if n_frames > 1:
                # 多帧：逐帧转，保留时长
                frames = []
                durations = []
                for i in range(n_frames):
                    im.seek(i)
                    frames.append(_prep(im.copy()))
                    durations.append(int(im.info.get("duration") or 100))
                # GIF 需要 P 模式；带透明的走逐帧调色板
                pal_frames = [_to_palette(f) for f in frames]
                pal_frames[0].save(
                    dst, save_all=True, append_images=pal_frames[1:],
                    duration=durations, loop=0, disposal=2,
                    optimize=False)
                info["frames"] = len(pal_frames)
            else:
                im.seek(0)
                frame = _prep(im.copy())
                _to_palette(frame).save(dst, optimize=False)

            info["converted"] = True
            info["why"] = (f"{fmt}→GIF" if fmt != "GIF" else "GIF 缩放")
            if scale < 1.0:
                info["why"] += f"，缩到 {nw}×{nh}"
            return True, "", info

    except Exception as exc:
        return False, f"转换失败：{exc}", info


def _to_palette(im):
    """RGBA → P（带透明）。

    为什么不直接用 convert("P")：那会把 alpha 全丢掉，
    透明底变成黑底或白底。这里先把全透明像素换成调色板里的
    透明索引，再量化。
    """
    from PIL import Image

    alpha = im.getchannel("A")
    # 二值化 alpha：GIF 只有全透明/全不透明
    mask = alpha.point(lambda v: 255 if v >= 128 else 0)
    p = im.convert("RGB").convert(
        "P", palette=Image.ADAPTIVE, colors=255)
    # 255 号索引留给透明
    p.paste(255, mask=Image.eval(mask, lambda v: 255 - v))
    try:
        p.info["transparency"] = 255
    except Exception:
        pass
    return p


# ══════════════════════════════════════════════════════════════════════
# 查询
# ══════════════════════════════════════════════════════════════════════

def list_stickers(root: Optional[str] = None,
                  with_detail: bool = True) -> List[Dict[str, Any]]:
    """列出表情库，按代码排序。

    两件顺带做的事：
      · 标记 deleted 的条目会尝试真删一次，删掉了就彻底移除
      · 磁盘上有文件但 index 里没有的，自动补一条（source=orphan）——
        用户手动往目录里扔图不该看不见
    """
    d = sticker_dir(root)
    if not os.path.isdir(d):
        return []
    data = read_index(root)
    items = data.get("items") or {}
    out: List[Dict[str, Any]] = []
    seen_files = set()
    changed = False

    for code, meta in sorted(list(items.items())):
        fn = _as_file(meta.get("file") or code)
        p = os.path.join(d, fn)

        if meta.get("deleted"):
            # 上次没删掉（文件被占用），再试一次
            seen_files.add(fn.lower())      # 防止 orphan 补录回来
            try:
                if os.path.isfile(p):
                    os.remove(p)
                items.pop(code, None)
                changed = True
            except Exception:
                pass
            continue

        seen_files.add(fn.lower())
        if not os.path.isfile(p):
            continue
        out.append(_shape(code, meta, p, with_detail))

    # 磁盘上有、index 里没有的
    for fn in sorted(os.listdir(d)):
        low = fn.lower()
        if low == INDEX_NAME or low in seen_files:
            continue
        if os.path.splitext(low)[1] not in INPUT_EXT:
            continue
        p = os.path.join(d, fn)
        if not os.path.isfile(p):
            continue
        code = next_code(data)
        meta = {
            "code": code, "file": fn, "brief": "", "detail": "",
            "tags": [], "created": int(time.time()), "source": "orphan",
            "auto": False,
        }
        items[code] = meta
        changed = True
        out.append(_shape(code, meta, p, with_detail))

    if changed:
        data["items"] = items
        try:
            write_index(data, root)
        except Exception:
            pass
    return out


def _clean_dim(v: Any) -> str:
    """把维度值规范化。不认识的 key 一律当"不归类"。"""
    k = str(v or "").strip().lower()
    return k if k in MOOD_DIM_KEYS else ""


def _clamp_intensity(v: Any) -> int:
    """把强度夹到 1~5。缺失/脏数据都给默认值。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return INTENSITY_DEFAULT
    return max(1, min(5, n))


def _shape(code: str, meta: Dict[str, Any], path: str,
           with_detail: bool) -> Dict[str, Any]:
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    item = {
        "code": code,
        "file": os.path.basename(path),
        "path": path,
        "size": size,
        "brief": str(meta.get("brief") or ""),
        "tags": list(meta.get("tags") or []),
        "created": int(meta.get("created") or 0),
        "source": str(meta.get("source") or ""),
        "auto": bool(meta.get("auto")),
        "frames": int(meta.get("frames") or 1),
        "intensity": _clamp_intensity(meta.get("intensity")),
        "moodDim": _clean_dim(meta.get("mood_dim")),
        "moodDimLabel": MOOD_DIM_LABELS.get(
            _clean_dim(meta.get("mood_dim")), ""),
        "kind": "sticker" if path.lower().endswith(".gif") else "image",
    }
    if with_detail:
        item["detail"] = str(meta.get("detail") or "")
    return item


def get_sticker(code: str, root: Optional[str] = None) -> Optional[Dict[str, Any]]:
    data = read_index(root)
    meta = (data.get("items") or {}).get(code)
    if not meta:
        return None
    p = abs_path(meta.get("file") or code, root)
    if not os.path.isfile(p):
        return None
    return _shape(code, meta, p, True)


# ══════════════════════════════════════════════════════════════════════
# 写入
# ══════════════════════════════════════════════════════════════════════

def import_sticker(src: str, root: Optional[str] = None,
                   brief: str = "", detail: str = "",
                   source: str = "import",
                   intensity: Optional[int] = None,
                   mood_dim: str = ""
                   ) -> Tuple[bool, str, Dict[str, Any]]:
    """收一张表情进库。返回 (ok, 错误或代码, 条目)。

    流程：转 GIF → 分配代码 → 落盘 → 写 index。
    任何一步失败都不留半成品。
    """
    if not os.path.isfile(src):
        return False, f"文件不存在：{src}", {}
    ext = os.path.splitext(src)[1].lower()
    if ext not in INPUT_EXT:
        return False, f"不支持的格式：{ext}", {}

    d = ensure_dir(root)
    data = read_index(root)
    code = next_code(data)
    dst = os.path.join(d, f"{code}.gif")

    ok, err, info = convert_to_gif(src, dst)
    if not ok:
        # 转换失败时清掉可能的半成品
        try:
            if os.path.isfile(dst):
                os.remove(dst)
        except Exception:
            pass
        return False, err, {}

    try:
        size = os.path.getsize(dst)
    except OSError:
        size = 0
    if size > MAX_BYTES:
        try:
            os.remove(dst)
        except Exception:
            pass
        return False, f"转换后过大（{size / 1024 / 1024:.1f}MB）", {}

    meta = {
        "code": code,
        "file": f"{code}.gif",
        "brief": (brief or "")[:BRIEF_MAX],
        "detail": (detail or "")[:DETAIL_MAX],
        "tags": [],
        "created": int(time.time()),
        "source": source,
        "auto": False,
        "intensity": _clamp_intensity(
            intensity if intensity is not None else INTENSITY_DEFAULT),
        "mood_dim": _clean_dim(mood_dim),
        "frames": int(info.get("frames") or 1),
        "w": int(info.get("out_w") or info.get("w") or 0),
        "h": int(info.get("out_h") or info.get("h") or 0),
    }
    data.setdefault("items", {})[code] = meta
    try:
        write_index(data, root)
    except WriteDenied as exc:
        try:
            os.remove(dst)
        except Exception:
            pass
        return False, str(exc), {}

    return True, code, _shape(code, meta, dst, True)


def update_sticker(code: str, root: Optional[str] = None,
                   brief: Optional[str] = None,
                   detail: Optional[str] = None,
                   tags: Optional[List[str]] = None,
                   auto: Optional[bool] = None,
                   intensity: Optional[int] = None,
                   mood_dim: Optional[str] = None) -> Tuple[bool, str]:
    """改一张表情的描述。只改传进来的字段。"""
    data = read_index(root)
    items = data.get("items") or {}
    meta = items.get(code)
    if not meta:
        return False, "没有这张表情"

    if brief is not None:
        meta["brief"] = str(brief).strip()[:BRIEF_MAX]
    if detail is not None:
        meta["detail"] = str(detail).strip()[:DETAIL_MAX]
    if tags is not None:
        meta["tags"] = [str(t).strip() for t in tags if str(t).strip()][:8]
    if auto is not None:
        meta["auto"] = bool(auto)
    if intensity is not None:
        meta["intensity"] = _clamp_intensity(intensity)
    if mood_dim is not None:
        meta["mood_dim"] = _clean_dim(mood_dim)
    meta["updated"] = int(time.time())

    items[code] = meta
    data["items"] = items
    try:
        write_index(data, root)
    except WriteDenied as exc:
        return False, str(exc)
    return True, ""


def delete_sticker(code: str, root: Optional[str] = None) -> Tuple[bool, str]:
    """删掉一张表情（文件 + index）。

    ⚠ 文件删不掉时**仍然从 index 移除**，只是留一句提示。

    为什么这样设计：Windows 上文件被占用就删不掉（`WinError 32`），
    而 UI 里正在预览那张图时，QtWebEngine 可能持有句柄。
    这时候如果整个删除失败，用户点了删除却什么都没变，会以为程序坏了。

    从 index 移除之后，那张图在库里就不存在了（列表、发送都读不到），
    磁盘上留个孤儿文件不影响使用，下次同名导入会被 next_code 跳过。
    """
    data = read_index(root)
    items = data.get("items") or {}
    meta = items.get(code)
    fn = _as_file(meta.get("file") if meta else code)
    p = os.path.join(sticker_dir(root), fn)

    removed = False
    note = ""
    if os.path.isfile(p):
        # 重试几次：UI 预览的句柄通常在几百毫秒内释放
        for attempt in range(4):
            try:
                os.remove(p)
                removed = True
                break
            except Exception:
                if attempt < 3:
                    time.sleep(0.2)

    if meta:
        if removed:
            items.pop(code, None)
        else:
            # 文件删不掉 → 立个墓碑。
            #
            # Windows 上文件被占用时，os.remove 和 os.replace 都会失败
            # （QtWebEngine 预览那张图时会持有句柄）。这时候如果整个
            # 删除失败，用户点了删除却什么都没变，会以为程序坏了。
            #
            # 所以标记 deleted=true：列表跳过它、发送读不到它，
            # 但条目留在 items 里，下次 list 时再试一次真删。
            meta["deleted"] = True
            meta["deleted_at"] = int(time.time())
            items[code] = meta
            note = f"文件被占用，已标记删除（下次自动清理）：{fn}"
        data["items"] = items
        try:
            write_index(data, root)
        except Exception as exc:
            return False, str(exc)

    if meta is None and not removed:
        return False, "没有这张表情"
    return True, note


# ══════════════════════════════════════════════════════════════════════
# 发送流水（冷却用，不进记忆库）
# ══════════════════════════════════════════════════════════════════════

def usage_path(root: Optional[str] = None) -> str:
    return os.path.join(root or resolve_data_root(), "sticker-usage.json")


def record_sent(chat_id: str, code: str, root: Optional[str] = None) -> None:
    """记一笔"她在哪个会话发过哪张"。

    只做冷却和"别连着发同一张"。**不进记忆库** ——
    表情不是她说的话，塞进记忆会污染检索。
    """
    p = usage_path(root)
    try:
        data = {}
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        if not isinstance(data, dict):
            data = {}
        rec = data.setdefault(chat_id, {})
        rec[code] = int(rec.get(code, 0)) + 1
        data["_last"] = {"chat": chat_id, "code": code,
                         "at": int(time.time())}
        _atomic_write_json(p, data)
    except Exception:
        pass


def read_usage(chat_id: str = "", root: Optional[str] = None) -> Dict[str, Any]:
    p = usage_path(root)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data.get(chat_id, {}) if chat_id else data
    except Exception:
        return {}


def last_sent(chat_id: str, root: Optional[str] = None) -> Dict[str, Any]:
    """最近一次在这个会话发的表情 {chat, code, at}，没有就空字典。"""
    data = read_usage("", root)
    rec = data.get("_last") if isinstance(data, dict) else None
    if isinstance(rec, dict) and rec.get("chat") == chat_id:
        return rec
    return {}
