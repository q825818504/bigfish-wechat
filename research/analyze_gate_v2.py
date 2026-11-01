"""静态分析 v2：用 capstone 线性反汇编 .text，找出所有指向目标地址的引用。

v1 的失败：用暴力 disp32 扫描，1 亿字节 × 4 种指令长度的组合既慢又错，
          实际上一个都没命中。

v2 的做法：
  1. 线性反汇编整个 .text（104MB）—— 慢但准确
  2. 收集所有 mem 操作数，若 base == RIP，记下目标地址
  3. 找出：
       a. 指向 "qt.accessibility.core" 的引用（定位 QAccessible 相关代码）
       b. 所有 `byte ptr [rip+x]` 的读取（潜在的 gate）
  4. 交叉：b 中落在 a 附近 ±8KB 的，最可能是 gate

为控制耗时，分两遍：
  第一遍只收集跨段的引���，第二遍细化。
"""
import os
import struct
import sys
import time

from capstone import Cs, CS_ARCH_X86, CS_MODE_64
from capstone.x86 import X86_OP_MEM, X86_REG_RIP

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
DLL = r"D:\WeChat\Weixin\4.1.10.27\Weixin.dll"
OUT = os.path.join(HERE, "_gate_analysis2.txt")
HR = "=" * 74
_lines = []


def say(s=""):
    print(s, flush=True)
    _lines.append(str(s))


def section(t):
    say()
    say(HR)
    say(t)
    say(HR)


size = os.path.getsize(DLL)
section("0. 读取")
say(f"  {DLL}")
say(f"  {size:,} 字节")
with open(DLL, "rb") as f:
    data = f.read()

# PE
pe = struct.unpack_from("<I", data, 0x3C)[0]
nsec = struct.unpack_from("<H", data, pe + 6)[0]
opt_size = struct.unpack_from("<H", data, pe + 20)[0]
opt_off = pe + 24
image_base = struct.unpack_from("<Q", data, opt_off + 24)[0]
sec_off = opt_off + opt_size

sections = []
for i in range(nsec):
    o = sec_off + i * 40
    name = data[o:o + 8].rstrip(b"\x00").decode("ascii", "replace")
    vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, o + 8)
    sections.append(dict(name=name, vaddr=vaddr, vsize=vsize,
                         rawptr=rawptr, rawsize=rawsize))

section("1. 定位")
CORE = b"qt.accessibility.core"
core_off = data.find(CORE)
core_rva = None
for s in sections:
    if s["rawptr"] <= core_off < s["rawptr"] + s["rawsize"]:
        core_rva = core_off - s["rawptr"] + s["vaddr"]
        break
core_va = image_base + core_rva
say(f"  字符串 RVA 0x{core_rva:X}  VA 0x{core_va:X}")

text = next(s for s in sections if s["name"] == ".text")
say(f"  .text RVA 0x{text['vaddr']:X}  RAW 0x{text['rawptr']:X}  "
    f"SIZE 0x{text['rawsize']:X}")

section("2. 线性反汇编 .text（可能要几分钟）")
tdata = data[text["rawptr"]:text["rawptr"] + text["rawsize"]]
tbase_va = image_base + text["vaddr"]

md = Cs(CS_ARCH_X86, CS_MODE_64)
md.detail = True
md.skipdata = True          # 遇到非法字节自动跳过，避免中断

refs_to_core = []           # 指向字符串的指令
byte_reads = []             # byte ptr [rip+x] 读取
count = 0
t0 = time.time()

for insn in md.disasm(tdata, tbase_va):
    count += 1
    if count % 2000000 == 0:
        say(f"    ... 已处理 {count/1e6:.0f}M 条，"
            f"{time.time()-t0:.0f}s")
    for op in insn.operands:
        if op.type != X86_OP_MEM:
            continue
        if op.mem.base != X86_REG_RIP:
            continue
        tgt = insn.address + insn.size + op.mem.disp
        # 指向 accessibility 字符串
        if tgt == core_va:
            refs_to_core.append((insn.address, f"{insn.mnemonic} {insn.op_str}"))
        # 读取单字节
        if op.size == 1:
            byte_reads.append((tgt, insn.address,
                               f"{insn.mnemonic} {insn.op_str}"))

say(f"  反汇编完成：{count:,} 条指令，{time.time()-t0:.0f}s")
say(f"  指向 qt.accessibility.core 的引用: {len(refs_to_core)}")
say(f"  单字节 [rip+x] 读取: {len(byte_reads)}")

section("3. 字符串引用")
for addr, txt in refs_to_core[:20]:
    say(f"  VA 0x{addr:X}  {txt}")

section("4. 交叉分析：附近 ±16KB 内的单字节读取")
if refs_to_core:
    near = []
    for tgt, insn_va, txt in byte_reads:
        for core_ref, _ in refs_to_core:
            if abs(insn_va - core_ref) <= 16384:
                near.append((tgt, insn_va, core_ref, txt))
                break
    say(f"  命中 {len(near)} 处")
    seen = set()
    for tgt, insn_va, core_ref, txt in near:
        if tgt in seen:
            continue
        seen.add(tgt)
        # 读当前值
        off = None
        for s in sections:
            if s["vaddr"] <= (tgt - image_base) < s["vaddr"] + s["vsize"]:
                d = (tgt - image_base) - s["vaddr"]
                if d < s["rawsize"]:
                    off = s["rawptr"] + d
                break
        cur = data[off] if off is not None else None
        flag = " ★" if cur in (0, 1) else ""
        say(f"  目标 VA 0x{tgt:X} (RVA 0x{tgt-image_base:X})  "
            f"当前值={cur}{flag}")
        say(f"      引用指令 @VA 0x{insn_va:X}: {txt}")
else:
    say("  字符串没有被代码引用（可能只通过数据表/初始化引用）")

section("5. 全部布尔型字节读取（值 0/1）")
bools = {}
for tgt, insn_va, txt in byte_reads:
    rva = tgt - image_base
    off = None
    for s in sections:
        if s["vaddr"] <= rva < s["vaddr"] + s["vsize"]:
            d = rva - s["vaddr"]
            if d < s["rawsize"]:
                off = s["rawptr"] + d
            break
    cur = data[off] if off is not None and off < len(data) else None
    if cur in (0, 1):
        bools.setdefault(tgt, []).append((insn_va, txt, cur))

say(f"  共 {len(bools)} 个布尔型候选")
for tgt, lst in list(bools.items())[:40]:
    rva = tgt - image_base
    say(f"  RVA 0x{rva:08X} 值={lst[0][2]} 引用 {len(lst)} 次")
    for insn_va, txt, _ in lst[:2]:
        say(f"      @VA 0x{insn_va:X}: {txt}")

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(_lines))
say()
say(f"  结果: {OUT}")
