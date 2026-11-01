"""静态分析：从 qt.accessibility.core 出发，找真正的 QAccessible active 标志。

思路（完全离线，只读 Weixin.dll 文件）：

  Qt 的 QAccessible::isActive() 大致长这样：

      static bool isActive()
      {
          if (!active) {                     // ← active 就是这个标志
              active = ...;
              ...
          }
          return active;
      }

  编译成 x64 机器码，通常会看到：
      cmp byte ptr [rip+disp32], 0
      je  ...
  或者
      movzx eax, byte ptr [rip+disp32]
      test eax, eax
      je ...

  而 QAccessible 初始化时会用到 "qt.accessibility.core" 这个 category 字符串
  （用于 qCDebug 或 QLoggingCategory）。所以：

    1. 定位字符串
    2. 找所有引用它的代码位置
    3. 那些代码所在的**函数范围**内，找 `cmp byte ptr [rip+disp], 0`
    4. 那个 disp 指向的地址就是候选 gate

    ��� 4.1.10.27 这种老版本，模式可能不同，所以这一步还要：
    5. 把找到的候选全部列出来，附上所在函数、地址、当前值
    6. 用「地址附近有没有 QAccessible 相关特征」来排序可信度
"""
import os
import re
import struct
import sys

from capstone import Cs, CS_ARCH_X86, CS_MODE_64

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
DLL = r"D:\WeChat\Weixin\4.1.10.27\Weixin.dll"
OUT = os.path.join(HERE, "_gate_analysis.txt")
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


if not os.path.isfile(DLL):
    say(f"找不到 {DLL}")
    sys.exit(1)

section("0. 读取 DLL")
size = os.path.getsize(DLL)
say(f"  {DLL}")
say(f"  {size:,} 字节 ({size/1024/1024:.1f} MB)")
with open(DLL, "rb") as f:
    data = f.read()

# ---------- PE 解析 ----------
section("1. PE 结构")
pe = struct.unpack_from("<I", data, 0x3C)[0]
nsec = struct.unpack_from("<H", data, pe + 6)[0]
opt_size = struct.unpack_from("<H", data, pe + 20)[0]
opt_off = pe + 24
image_base = struct.unpack_from("<Q", data, opt_off + 24)[0]
size_of_image = struct.unpack_from("<I", data, opt_off + 56)[0]

sec_off = opt_off + opt_size
sections = []
for i in range(nsec):
    o = sec_off + i * 40
    name = data[o:o + 8].rstrip(b"\x00").decode("ascii", "replace")
    vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, o + 8)
    chars = struct.unpack_from("<I", data, o + 36)[0]
    sections.append(dict(name=name, vaddr=vaddr, vsize=vsize,
                         rawptr=rawptr, rawsize=rawsize, chars=chars))

say(f"  ImageBase: 0x{image_base:X}")
say(f"  SizeOfImage: 0x{size_of_image:X}")


def off_to_rva(off):
    for s in sections:
        if s["rawptr"] <= off < s["rawptr"] + s["rawsize"]:
            return off - s["rawptr"] + s["vaddr"]
    return None


def rva_to_off(rva):
    for s in sections:
        if s["vaddr"] <= rva < s["vaddr"] + max(s["vsize"], s["rawsize"]):
            d = rva - s["vaddr"]
            if d < s["rawsize"]:
                return s["rawptr"] + d
    return None


def in_section(rva, name):
    for s in sections:
        if s["name"] == name and s["vaddr"] <= rva < s["vaddr"] + s["vsize"]:
            return True
    return False


# ---------- 定位字符串 ----------
section("2. qt.accessibility.core")
CORE = b"qt.accessibility.core"
core_off = data.find(CORE)
if core_off < 0:
    say("  找不到字符串")
    sys.exit(1)
core_rva = off_to_rva(core_off)
say(f"  文件偏移 0x{core_off:X}  RVA 0x{core_rva:X}  "
    f"VA 0x{image_base + core_rva:X}")

# ---------- 找引用 ----------
section("3. 找引用该字符串的代码（RIP 相对）")
text = next((s for s in sections if s["name"] == ".text"), None)
if not text:
    say("  没有 .text 段")
    sys.exit(1)
say(f"  .text: RVA 0x{text['vaddr']:X}  "
    f"大小 0x{text['rawsize']:X} ({text['rawsize']/1024/1024:.1f} MB)")

tdata = data[text["rawptr"]:text["rawptr"] + text["rawsize"]]
tbase = text["vaddr"]

# 扫所有可能的 disp32，看谁指向 core_rva
# x64 的 lea/mov 长度 7（48 8D/8B xx disp32），所以 next_va = insn_rva + 7
# 但不同指令长度不同，这里用「假设指令结束于 off+4」这种通用做法
# 更稳：对命中处反汇编一小段确认真是引用
xrefs = []
for off in range(0, len(tdata) - 4, 1):
    disp = struct.unpack_from("<i", tdata, off)[0]
    for insn_len in (7, 8, 9, 10):
        target = tbase + off + insn_len + disp
        if target == core_rva:
            insn_rva = tbase + off - (insn_len - 4)
            if 0 <= insn_rva:
                xrefs.append((insn_rva, insn_len))
            break

say(f"  命中 {len(xrefs)} 处")
# 去重
seen = set()
uniq = []
for rva, ln in xrefs:
    if rva in seen:
        continue
    seen.add(rva)
    uniq.append(rva)
say(f"  去重后 {len(uniq)} 处")

md = Cs(CS_ARCH_X86, CS_MODE_64)
md.detail = True

for rva in uniq[:12]:
    o = rva_to_off(rva)
    if o is None:
        continue
    code = data[o:o + 16]
    insns = list(md.disasm(code, image_base + rva))
    if insns:
        i0 = insns[0]
        say(f"  RVA 0x{rva:X}: {i0.mnemonic} {i0.op_str}")
        # 确认操作数是 rip 相对
        for op in i0.operands:
            if op.type == 3:  # X86_OP_MEM
                if op.mem.base == 41:  # X86_REG_RIP
                    say(f"      rip 相对，目标 VA 0x{image_base + rva + i0.size + op.mem.disp:X}")

# ---------- 在引用点附近找 gate ----------
section("4. 在引用点前后 ±4KB 内反汇编，找 cmp/movzx byte [rip+..], 0")
GATE_PATTERNS = []


def scan_region(start_rva, span=4096):
    """从 start_rva - span 开始反汇编，找读取 rip 相对字节的指令。"""
    lo = max(text["vaddr"], start_rva - span)
    hi = min(text["vaddr"] + text["rawsize"], start_rva + span)
    o = rva_to_off(lo)
    if o is None:
        return []
    code = data[o:o + (hi - lo)]
    results = []
    for insn in md.disasm(code, image_base + lo):
        # cmp byte ptr [rip+x], 0   ->  mnemonic='cmp', ops: mem(rip), imm 0
        if insn.mnemonic in ("cmp", "test") and len(insn.operands) >= 1:
            op = insn.operands[0]
            if op.type == 3 and op.size == 1 and op.mem.base == 41:
                tgt_va = insn.address + insn.size + op.mem.disp
                tgt_rva = tgt_va - image_base
                results.append({
                    "insn_va": insn.address,
                    "insn_rva": insn.address - image_base,
                    "target_rva": tgt_rva,
                    "target_va": tgt_va,
                    "text": f"{insn.mnemonic} {insn.op_str}",
                })
        # movzx reg, byte ptr [rip+x]
        if insn.mnemonic == "movzx" and len(insn.operands) == 2:
            src = insn.operands[1]
            if src.type == 3 and src.size == 1 and src.mem.base == 41:
                tgt_va = insn.address + insn.size + src.mem.disp
                tgt_rva = tgt_va - image_base
                results.append({
                    "insn_va": insn.address,
                    "insn_rva": insn.address - image_base,
                    "target_rva": tgt_rva,
                    "target_va": tgt_va,
                    "text": f"{insn.mnemonic} {insn.op_str}",
                })
    return results


all_hits = {}
for rva in uniq:
    hits = scan_region(rva)
    for h in hits:
        key = h["target_rva"]
        if key not in all_hits:
            all_hits[key] = {"target_rva": key, "target_va": h["target_va"],
                             "refs": []}
        all_hits[key]["refs"].append(h)

say(f"  找到 {len(all_hits)} 个「被读的字节」候选")

# ---------- 读每个候选的当前值 ----------
section("5. 候选详情（含 DLL 文件里的当前字节值）")
rows = []
for tgt_rva, info in all_hits.items():
    o = rva_to_off(tgt_rva)
    cur = data[o] if o is not None and o < len(data) else None
    rows.append({
        "rva": tgt_rva,
        "va": info["target_va"],
        "cur": cur,
        "in_section": next((s["name"] for s in sections
                            if s["vaddr"] <= tgt_rva < s["vaddr"] + s["vsize"]),
                           "?"),
        "refs": info["refs"],
    })

# 排序：值为 0/1 的优先（布尔开关），其次引用次数多的
rows.sort(key=lambda r: (0 if r["cur"] in (0, 1) else 1,
                         -len(r["refs"])))

for r in rows[:25]:
    flag = " ★" if r["cur"] in (0, 1) else ""
    say(f"  RVA 0x{r['rva']:08X}  值={r['cur']!r:<6} 段={r['in_section']:8s}"
        f" 引用={len(r['refs'])}{flag}")
    for h in r["refs"][:3]:
        say(f"      ← {h['text']}  @RVA 0x{h['insn_rva']:X}")

section("6. 说明")
say("  值为 0 或 1 的候选是布尔开关，最可能是 gate。")
say("  下一步：对排在前面的候选逐个热写 + 校验树是否物化。")

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(_lines))
say()
say(f"  结果已写入: {OUT}")
