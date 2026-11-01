"""离线扫描 gate 字节的候选地址（不碰微信进程）。

用上游同一个正则模式在本地 Weixin.dll 上扫一遍，
把命中的 RVA、disp 值、以及推算出的实际字节地址都列出来。

这一步只是「预演」，真正的写入仍然由上游模块完成
（它会写 + 回读校验 + 失败还原）。
"""
import os
import re
import struct
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DLL = r"D:\WeChat\Weixin\4.1.10.27\Weixin.dll"
HR = "=" * 74

PATTERN = re.compile(
    rb"\x48\x85\xc9\x0f\x84....\x80\x3d(?P<disp>.{4})"
    rb"\x00\x0f\x84",
    re.DOTALL,
)
CORE = b"qt.accessibility.core"


def section(t):
    print()
    print(HR)
    print(t)
    print(HR)


if not os.path.isfile(DLL):
    print(f"找不到 {DLL}")
    sys.exit(1)

section("0. 文件")
size = os.path.getsize(DLL)
print(f"  路径: {DLL}")
print(f"  大小: {size:,} 字节 ({size/1024/1024:.1f} MB)")

print("  读取中 ...")
with open(DLL, "rb") as f:
    data = f.read()
print(f"  已读入 {len(data):,}")

# ---- PE 节表 ----
section("1. PE 结构")
pe = struct.unpack_from("<I", data, 0x3C)[0]
machine = struct.unpack_from("<H", data, pe + 4)[0]
nsec = struct.unpack_from("<H", data, pe + 6)[0]
opt_size = struct.unpack_from("<H", data, pe + 20)[0]
opt_off = pe + 24
image_base = struct.unpack_from("<Q", data, opt_off + 24)[0]
size_of_image = struct.unpack_from("<I", data, opt_off + 56)[0]
print(f"  Machine=0x{machine:04X}  ImageBase=0x{image_base:X}")
print(f"  SizeOfImage=0x{size_of_image:X} ({size_of_image:,})")

sec_off = opt_off + opt_size
sections = []
for i in range(nsec):
    o = sec_off + i * 40
    name = data[o:o + 8].rstrip(b"\x00").decode("ascii", "replace")
    vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, o + 8)
    chars = struct.unpack_from("<I", data, o + 36)[0]
    sections.append(dict(name=name, vaddr=vaddr, vsize=vsize,
                         rawptr=rawptr, rawsize=rawsize, chars=chars))
    print(f"    {name:8s} VA=0x{vaddr:08X} Raw=0x{rawptr:08X} "
          f"Size=0x{rawsize:08X} Chars=0x{chars:08X}")


def off_to_rva(off):
    for s in sections:
        if s["rawptr"] <= off < s["rawptr"] + s["rawsize"]:
            return off - s["rawptr"] + s["vaddr"]
    return None


def rva_to_off(rva):
    for s in sections:
        if s["vaddr"] <= rva < s["vaddr"] + max(s["vsize"], s["rawsize"]):
            return rva - s["vaddr"] + s["rawptr"]
    return None


# ---- 扫字符串 ----
section("2. qt.accessibility.core")
core_off = data.find(CORE)
print(f"  文件偏移: 0x{core_off:X}")
core_rva = off_to_rva(core_off)
print(f"  RVA: 0x{core_rva:X}  VA: 0x{image_base + core_rva:X}")

# ---- 扫模式 ----
section("3. gate 模式匹配")
print(f"  模式: 48 85 C9 0F 84 ?? ?? ?? ?? 80 3D <disp32> 00 0F 84")
hits = list(PATTERN.finditer(data))
print(f"  命中: {len(hits)} 处")

cands = []
for i, m in enumerate(hits):
    m_rva = off_to_rva(m.start())
    d_off = m.start("disp")
    d_rva = off_to_rva(d_off)
    if m_rva is None or d_rva is None:
        continue
    disp = struct.unpack_from("<i", data, d_off)[0]
    # cmp byte ptr [rip+disp], 0  —— 指令结束于 disp 之后 4 字节
    insn_end_rva = d_rva + 4
    target_rva = insn_end_rva + disp
    t_off = rva_to_off(target_rva)
    cur = data[t_off] if t_off is not None and t_off < len(data) else None
    cands.append(dict(idx=i, match_rva=m_rva, disp=disp,
                      target_rva=target_rva, current=cur))
    print(f"  [{i}] 指令 RVA=0x{m_rva:X}  disp=0x{disp & 0xFFFFFFFF:08X}")
    print(f"      目标 RVA=0x{target_rva:X}  VA=0x{image_base + target_rva:X}"
          f"  当前值={cur if cur is not None else 'N/A'}")

section("4. 判断")
if not cands:
    print("  没有候选 —— 这个版本的字节码与上游模式不一致，动态扫描走不通。")
elif len(cands) == 1:
    print("  只有一个候选，可信度高。")
    c = cands[0]
    print(f"  它就是 QACCESSIBLE_ACTIVE_RVA 的候选值：0x{c['target_rva']:X}")
    print(f"  当前字节值 = {c['current']}（0 表示树未物化）")
else:
    print(f"  有 {len(cands)} 个候选，需要逐个热写 + 回读校验（上游实现会自己做）。")

section("5. 下一步")
print("  如果候选的当前值是 0，写 1 之后 mmui 树应该物化。")
print("  这一步交给上游 AccessibilityGate 执行，它会：")
print("    - 写 1 → 回读校验 → 通过则保留")
print("    - 不通过 → 还原原字节 → 试下一个候选")
