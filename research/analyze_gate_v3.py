"""轻量静态分析：找出引用 qt.accessibility.core 的代码位置。

v2 的教训：对整个 .text（104MB / 上亿条指令）做线性反汇编，
          内存和时间都不可控，把宿主拖垮了。

v3 的做法——**不反汇编，只做字节搜索**：

  1. x64 的 RIP 相对寻址，机器码里存的是 32 位 disp。
     `lea reg,[rip+disp32]` = 48 8D xx disp32  (长度 7)
     所以：对 .text 里每个偏移 off，若
         base_va + off + 7 + disp32 == target_va
     则 off 处"可能是"一条指向目标的 lea。

     等价地：对每个 off，算出
         disp = target_va - (base_va + off + 7)
     然后在 .text 里搜索这个 4 字节序列。

     这样只需对**一个 4 字节模式**做 memmem，速度是 C 实现的
     bytes.find()，1 亿字节大约 0.1 秒。

  2. 找不到就换指令长度（6/7/8/9/10），每种长度只需一次搜索。
     总共十来次 bytes.find 扫描，几秒钟内完成。

  3. 对命中的位置，用 capstone 反汇编**那一条指令**确认
     （只反汇编几十字节，不是整个段）。
"""
import os
import struct
import sys

from capstone import Cs, CS_ARCH_X86, CS_MODE_64
from capstone.x86 import X86_OP_MEM, X86_REG_RIP

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
DLL = r"D:\WeChat\Weixin\4.1.10.27\Weixin.dll"
OUT = os.path.join(HERE, "_gate_v3.txt")
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


# ---------------- PE ----------------
with open(DLL, "rb") as f:
    data = f.read()

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

section("0. 基础")
say(f"  文件 {len(data):,} 字节")
say(f"  ImageBase 0x{image_base:X}")

text = next(s for s in sections if s["name"] == ".text")
tdata = data[text["rawptr"]:text["rawptr"] + text["rawsize"]]
tbase_va = image_base + text["vaddr"]
say(f"  .text VA 0x{tbase_va:X}  {len(tdata):,} 字节")

CORE = b"qt.accessibility.core"
core_off = data.find(CORE)
for s in sections:
    if s["rawptr"] <= core_off < s["rawptr"] + s["rawsize"]:
        core_rva = core_off - s["rawptr"] + s["vaddr"]
        break
core_va = image_base + core_rva
section("1. 目标字符串")
say(f"  RVA 0x{core_rva:X}  VA 0x{core_va:X}")


def find_xrefs(target_va, lengths=(7, 8, 9, 10, 6, 11)):
    """用 bytes.find 找出所有 `[rip+disp32]` 指向 target_va 的位置。"""
    hits = []
    for ln in lengths:
        # 假设指令结束于 off+ln，则 disp 位于 off+ln-4
        pat_base = target_va
        # 对 .text 中每个 off，需要 disp = target - (base_va + off + ln)
        # 即 off = target - base_va - ln - disp
        # 取几种常见的 disp 值反推不现实；改为按块扫描：
        # 对每 4 字节窗口算它"若作为 disp 且指令长度 ln"时的目标
        arr = tdata
        n = len(arr)
        # 高效做法：把 disp 的期望值范围算出来，逐 4 字节比较
        # 期望：disp = target_va - (tbase_va + off_end)
        # 其中 off_end = off + ln，off 是 disp 在数组中的偏移
        # => off = target_va - tbase_va - ln - disp
        # 遍历 disp 不可行；改遍历 off 并计算 disp，用 struct 打包后 find
        # 但 disp 依赖 off，无法用固定模式 find。
        #
        # 正确做法：对每个候选 off，disp = target_va - (tbase_va + off + ln)
        # 这是 O(n) 的。n = 1e8，Python 循环太慢。
        #
        # 优化：用 numpy 向量化
        import numpy as np
        buf = np.frombuffer(arr[:n - (n % 4)], dtype=np.uint32)
        offs = np.arange(len(buf), dtype=np.int64) * 4
        end_va = tbase_va + offs + ln
        want = (target_va - end_va) & 0xFFFFFFFF
        match = buf == want.astype(np.uint32)
        for idx in np.nonzero(match)[0]:
            off = int(offs[idx]) - (ln - 4)
            if 0 <= off < n:
                hits.append(off)
    return sorted(set(hits))


section("2. 搜索引用（numpy 向量化）")
import time
import numpy as np

t0 = time.time()
refs = []
for ln in (7, 8, 9, 10, 6, 11):
    buf = np.frombuffer(tdata[:(len(tdata) // 4) * 4], dtype=np.uint32)
    end_offs = np.arange(len(buf), dtype=np.int64) * 4
    end_va = tbase_va + end_offs + ln
    want = (core_va - end_va) & 0xFFFFFFFF
    match = buf == want.astype(np.uint32)
    idxs = np.nonzero(match)[0]
    for idx in idxs:
        insn_off = int(end_offs[idx]) - (ln - 4)
        if 0 <= insn_off < len(tdata):
            refs.append((insn_off, ln))

say(f"  扫描耗时 {time.time()-t0:.2f}s")
say(f"  原始命中 {len(refs)} 处")

# 用 capstone 验证每条命中是否真的是 rip 相对访问
md = Cs(CS_ARCH_X86, CS_MODE_64)
md.detail = True
verified = []
for insn_off, ln in refs:
    va = tbase_va + insn_off
    code = tdata[insn_off:insn_off + 16]
    insns = list(md.disasm(code, va))
    if not insns:
        continue
    i0 = insns[0]
    ok = False
    for op in i0.operands:
        if op.type == X86_OP_MEM and op.mem.base == X86_REG_RIP:
            tgt = i0.address + i0.size + op.mem.disp
            if tgt == core_va:
                ok = True
    if ok:
        verified.append((va, f"{i0.mnemonic} {i0.op_str}", i0.size))

say(f"  capstone 验证通过 {len(verified)} 处")
for va, txt, sz in verified[:20]:
    say(f"    VA 0x{va:X} (RVA 0x{va-image_base:X})  len={sz}  {txt}")

if not verified:
    section("结论")
    say("  没有代码直接引用这个字符串。")
    say("  可能它只出现在数据初始化里（如 QLoggingCategory 的静态构造）。")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(_lines))
    sys.exit(0)

# ---------------- 在引用点附近找 byte 读取 ----------------
section("3. 在引用点 ±8KB 内找单字节 rip 读取")


def scan_window(center_va, span=8192):
    lo_va = max(tbase_va, center_va - span)
    hi_va = min(tbase_va + len(tdata), center_va + span)
    lo_off = lo_va - tbase_va
    code = tdata[lo_off:lo_off + (hi_va - lo_va)]
    out = []
    for insn in md.disasm(code, lo_va):
        for op in insn.operands:
            if op.type == X86_OP_MEM and op.mem.base == X86_REG_RIP and op.size == 1:
                tgt = insn.address + insn.size + op.mem.disp
                out.append({
                    "target_va": tgt,
                    "insn_va": insn.address,
                    "text": f"{insn.mnemonic} {insn.op_str}",
                })
    return out


cand = {}
for va, txt, sz in verified:
    for r in scan_window(va):
        cand.setdefault(r["target_va"], []).append(r)

say(f"  候选字节地址: {len(cand)} 个")

rows = []
for tgt_va, lst in cand.items():
    rva = tgt_va - image_base
    off = None
    for s in sections:
        if s["vaddr"] <= rva < s["vaddr"] + s["vsize"]:
            d = rva - s["vaddr"]
            if d < s["rawsize"]:
                off = s["rawptr"] + d
            break
    cur = data[off] if off is not None and off < len(data) else None
    rows.append((rva, cur, lst))

rows.sort(key=lambda x: (0 if x[1] in (0, 1) else 1, -len(x[2])))

section("4. 候选（值 0/1 的优先）")
for rva, cur, lst in rows[:30]:
    flag = " ★" if cur in (0, 1) else ""
    say(f"  RVA 0x{rva:08X}  值={cur}{flag}  被读 {len(lst)} 次")
    for r in lst[:2]:
        say(f"      @VA 0x{r['insn_va']:X}: {r['text']}")

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(_lines))
say()
say(f"  结果: {OUT}")
