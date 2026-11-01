"""分析 gate 字节的准确位置（纯离线，不碰微信进程）。

已知：
  Weixin.dll 基址 VA = 0x7FFF9A170000
  "qt.accessibility.core" 字符串在文件偏移 0x8294D04
  对应 VA = 0x7FFFA2404D04

要做的事：
  1. 从缓存里的映像文件，找到那处字符串
  2. 从字符串往回找谁引用了它（RIP 相对寻址）
  3. 定位 `cmp byte ptr [reg+disp], 0` 之类比较指令里的那个字节地址
  4. 报告：这个字节的 RVA、当前值、所在段、保护属性

全程只读文件，不打开微信进程。
"""
import os
import re
import struct
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGE = os.path.join(HERE, "_weixin_image.bin")
HR = "=" * 74

if not os.path.isfile(IMAGE):
    print(f"找不到映像缓存: {IMAGE}")
    print("请先跑 probe_gate_admin.bat 生成")
    sys.exit(1)


def section(t):
    print()
    print(HR)
    print(t)
    print(HR)


with open(IMAGE, "rb") as f:
    image = f.read()

section("1. 基础信息")
print(f"  映像大小: {len(image):,} 字节")

# PE 头
pe_off = struct.unpack_from("<I", image, 0x3C)[0]
machine = struct.unpack_from("<H", image, pe_off + 4)[0]
nsec = struct.unpack_from("<H", image, pe_off + 6)[0]
opt_size = struct.unpack_from("<H", image, pe_off + 20)[0]
opt_off = pe_off + 24
magic = struct.unpack_from("<H", image, opt_off)[0]
image_base = struct.unpack_from("<Q", image, opt_off + 24)[0]
size_of_image = struct.unpack_from("<I", image, opt_off + 56)[0]

print(f"  Machine: 0x{machine:04X}  PE32+: {magic == 0x20b}")
print(f"  ImageBase: 0x{image_base:X}")
print(f"  SizeOfImage: 0x{size_of_image:X} ({size_of_image:,})")
print(f"  节数: {nsec}")

# 节表
sec_off = opt_off + opt_size
sections = []
for i in range(nsec):
    o = sec_off + i * 40
    name = image[o:o + 8].rstrip(b"\x00").decode("ascii", "replace")
    vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", image, o + 8)
    chars = struct.unpack_from("<I", image, o + 36)[0]
    sections.append({"name": name, "vaddr": vaddr, "vsize": vsize,
                     "rawptr": rawptr, "rawsize": rawsize, "chars": chars})
    print(f"    {name:8s} VA=0x{vaddr:08X} VirtSize=0x{vsize:08X} "
          f"RawPtr=0x{rawptr:08X} RawSize=0x{rawsize:08X} "
          f"Chars=0x{chars:08X}")


def rva_to_off(rva: int) -> int:
    for s in sections:
        if s["vaddr"] <= rva < s["vaddr"] + max(s["vsize"], s["rawsize"]):
            return rva - s["vaddr"] + s["rawptr"]
    return -1


def off_to_rva(off: int) -> int:
    for s in sections:
        if s["rawptr"] <= off < s["rawptr"] + s["rawsize"]:
            return off - s["rawptr"] + s["vaddr"]
    return -1


def read_cstr(off: int, maxlen: int = 128) -> str:
    end = image.find(b"\x00", off, off + maxlen)
    if end < 0:
        end = off + maxlen
    return image[off:end].decode("ascii", "replace")


section("2. 定位 qt.accessibility.core")
needle = b"qt.accessibility.core"
hits = []
i = image.find(needle)
while i != -1:
    hits.append(i)
    i = image.find(needle, i + 1)
print(f"  文件偏移命中: {len(hits)} 处")
for h_off in hits:
    rva = off_to_rva(h_off)
    print(f"    文件偏移 0x{h_off:X}  RVA 0x{rva:X}  "
          f"VA 0x{image_base + rva:X}")
    print(f"      字符串: {read_cstr(h_off)!r}")
    # 看一下前面是不是 Qt 的字符串结构（长度+指针）
    ctx = image[max(0, h_off - 32):h_off]
    print(f"      前置字节: {ctx.hex()}")


section("3. 找引用该字符串的代码")
# x64: lea reg, [rip+disp32]  =>  48 8D xx disp32
#       mov reg, offset         =>  48 8B xx disp32
# 对每个字符串 VA，搜索 disp32 使 target == string_va
refs = []
for h_off in hits:
    target_rva = off_to_rva(h_off)
    if target_rva < 0:
        continue
    print(f"\n  查找引用 RVA 0x{target_rva:X} 的代码 ...")
    # disp32 在指令末尾，target = next_insn_va + disp32
    # 遍历所有可能的指令起点太慢；改为直接在 .text 里扫 disp32
    for s in sections:
        if s["name"] != ".text":
            continue
        text = image[s["rawptr"]:s["rawptr"] + s["rawsize"]]
        for insn_len in (7, 8, 9, 10):
            for m in re.finditer(re.escape(struct.pack("<i", 0)), b""):
                break
        # 直接暴力：对每个 disp32 位置算 target
        base_rva = s["vaddr"]
        step = 1
        found = 0
        for off in range(0, len(text) - 4, step):
            disp = struct.unpack_from("<i", text, off)[0]
            # 假设指令结束于 off+4，则 next_va = base_rva + off + 4
            target = base_rva + off + 4 + disp
            if target == target_rva:
                insn_off = s["rawptr"] + off - 4
                if insn_off >= 0:
                    prev = image[max(0, insn_off - 6):insn_off + 4]
                    refs.append({
                        "insn_file_off": insn_off,
                        "insn_rva": base_rva + off - 4,
                        "prev_bytes": prev.hex(),
                    })
                    found += 1
                    if found > 40:
                        break
        print(f"    .text 中命中 {found} 个 disp32")

print(f"\n  候选引用点合计: {len(refs)}")
for r in refs[:20]:
    print(f"    RVA 0x{r['insn_rva']:X}  前置字节 {r['prev_bytes']}")


section("4. 说明")
print("""
  上面只是「谁引用了这个字符串」，不是 gate 字节本身。

  上游的做法是：
    1. 找到引用点所在的函数
    2. 在函数体里找 `cmp byte ptr [xxx], 0` / `movzx eax, byte ptr [xxx]`
    3. 那个 [xxx] 就是 gate 字节的地址

  这一步用静态分析很难一次做对（需要反汇编器）。
  更可靠的做法是**直接用上游已经验证过的实现**——
  它会自己扫、自己写、自己回读校验。
""")
