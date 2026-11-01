"""逐个试全部 gate 候选，用「树是否真的物化」判定成败。

教训：上一次只凭「当前值=0」就认定候选 0 是 gate，结果写进去树毫无反应。
    「值像开关」不等于「它是开关」。唯一可信的判据是
    **写完之后 mmui 控件树有没有真的出现** —— 这正是上游实现的做法。

本脚本：
  对每个候选依次执行
    读原值 -> 改保护 -> 写 1 -> 回读 -> 检查 UIA 树
      成功 -> 保留，停止
      失败 -> 写回原值，还原保护，试下一个
  全部失败 -> 保证所有字节都回到原始状态

安全性：
  每个候选失败都还原，结束时再整体确认一遍。
  不注入、不建线程、不改文件。
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "vendor", "wechatauto-replica"))

OUT = os.path.join(HERE, "_wake_all.txt")
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


k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.restype = wt.HANDLE

# 离线扫描得到的三个候选（RVA）以及它们当时的原始值
CANDIDATES = [
    (0x0A142BC7, 0x00, "候选0：值0，上次试过无效"),
    (0x0A85C50F, 0x08, "候选1：值8"),
    (0x0A85C50F, 0x08, "候选2：与候选1同址"),
]

PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_QUERY_INFORMATION = 0x0400
PAGE_EXECUTE_READWRITE = 0x40


def find_weixin():
    u = ctypes.windll.user32
    pids = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, lp):
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cls, 256)
        if "Qt51514" in cls.value:
            pid = wt.DWORD(0)
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            h = k32.OpenProcess(0x1000, False, pid.value)
            if h:
                buf = ctypes.create_unicode_buffer(1024)
                sz = wt.DWORD(1024)
                if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(sz)):
                    if buf.value.lower().endswith("weixin.exe"):
                        pids.append(pid.value)
                k32.CloseHandle(h)
        return True

    u.EnumWindows(cb, 0)
    pids = sorted(set(pids))
    if not pids:
        return None
    pid = pids[0]

    class ME32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD),
            ("th32ProcessID", wt.DWORD), ("GlblcntUsage", wt.DWORD),
            ("ProccntUsage", wt.DWORD), ("modBaseAddr", ctypes.c_void_p),
            ("modBaseSize", wt.DWORD), ("hModule", wt.HMODULE),
            ("szModule", ctypes.c_wchar * 256),
            ("szExePath", ctypes.c_wchar * 260),
        ]

    snap = k32.CreateToolhelp32Snapshot(0xC, pid)
    if not snap or snap == -1:
        return None
    base = size = 0
    path = ""
    me = ME32()
    me.dwSize = ctypes.sizeof(ME32)
    ok = k32.Module32FirstW(snap, ctypes.byref(me))
    while ok:
        if me.szModule.lower() == "weixin.dll":
            base = me.modBaseAddr or 0
            size = me.modBaseSize
            path = me.szExePath
            break
        ok = k32.Module32NextW(snap, ctypes.byref(me))
    k32.CloseHandle(snap)
    return (pid, base, size, path) if base else None


def uia_state():
    """返回 (根节点类名, mmui 节点列表)"""
    try:
        import uiautomation as auto
        hwnd = ctypes.windll.user32.FindWindowW("Qt51514QWindowIcon", None)
        if not hwnd:
            hwnd = ctypes.windll.user32.FindWindowW(None, "微信")
        if not hwnd:
            return None, []
        root = auto.ControlFromHandle(hwnd)
        if not root:
            return None, []
        found = []

        def walk(c, d=0):
            if d > 4 or len(found) >= 15:
                return
            try:
                for ch in c.GetChildren():
                    cn = getattr(ch, "ClassName", "") or ""
                    if "mmui" in cn.lower():
                        found.append((d, cn, (getattr(ch, "Name", "") or "")[:36]))
                        if len(found) >= 15:
                            return
                    walk(ch, d + 1)
            except Exception:
                pass

        walk(root)
        return getattr(root, "ClassName", None), found
    except Exception as e:
        return f"ERR:{e}", []


def read_byte(h, va):
    buf = ctypes.create_string_buffer(1)
    br = ctypes.c_size_t(0)
    if k32.ReadProcessMemory(h, ctypes.c_void_p(va), buf, 1, ctypes.byref(br)):
        return buf.raw[0]
    return None


def write_byte(h, va, value):
    """写一个字节，返回 (ok, 旧保护)。调用方负责还原保护。"""
    old = wt.DWORD(0)
    if not k32.VirtualProtectEx(h, ctypes.c_void_p(va), 1,
                                PAGE_EXECUTE_READWRITE, ctypes.byref(old)):
        return False, None
    data = ctypes.create_string_buffer(bytes([value]), 1)
    bw = ctypes.c_size_t(0)
    ok = bool(k32.WriteProcessMemory(h, ctypes.c_void_p(va), data, 1,
                                     ctypes.byref(bw)))
    return ok, old.value


def restore_prot(h, va, old_prot):
    try:
        tmp = wt.DWORD(0)
        k32.VirtualProtectEx(h, ctypes.c_void_p(va), 1, old_prot,
                             ctypes.byref(tmp))
    except Exception:
        pass


def main():
    section("0. 环境")
    say(f"  管理员: {bool(ctypes.windll.shell32.IsUserAnAdmin())}")

    found = find_weixin()
    if not found:
        say("  [!] 没找到微信")
        return 1
    pid, base, size, path = found
    say(f"  PID: {pid}")
    say(f"  DLL: {path}")
    say(f"  基址: 0x{base:X}")

    access = (PROCESS_VM_READ | PROCESS_QUERY_INFORMATION
              | PROCESS_VM_WRITE | PROCESS_VM_OPERATION)
    h = k32.OpenProcess(access, False, pid)
    say(f"  OpenProcess -> {h}  err={ctypes.get_last_error()}")
    if not h:
        return 1

    tried = []          # (rva, orig_value, old_prot) 用于最后兜底还原
    winner = None

    try:
        # ---- 先核对上一轮有没有留下痕迹 ----
        section("0.5 检查上次写入是否已完全还原")
        clean = True
        for rva, expect, desc in CANDIDATES:
            va = base + rva
            cur = read_byte(h, va)
            ok_mark = "OK" if cur == expect else "!! 不一致"
            say(f"  RVA 0x{rva:X}  当前={cur}  原本={expect}  {ok_mark}")
            if cur != expect:
                clean = False
                say(f"     -> 写回 {expect}")
                write_byte(h, va, expect)
        say(f"  状态: {'干净' if clean else '已修正'}")

        for idx, (rva, expect, desc) in enumerate(CANDIDATES, 1):
            va = base + rva
            section(f"候选 {idx}: RVA 0x{rva:X}  ({desc})")
            say(f"  VA: 0x{va:X}")

            orig = read_byte(h, va)
            say(f"  当前值: {orig}")
            if orig is None:
                say("  读不到，跳过")
                continue

            # 试写 1
            ok, old_prot = write_byte(h, va, 1)
            if not ok:
                say(f"  写入失败 err={ctypes.get_last_error()}")
                if old_prot is not None:
                    restore_prot(h, va, old_prot)
                continue
            tried.append((rva, orig, old_prot))
            say(f"  已写 1（旧保护 0x{old_prot:X}），等待树物化 ...")

            root_cls, nodes = None, []
            for attempt in range(5):
                time.sleep(0.5)
                root_cls, nodes = uia_state()
                say(f"    [{attempt+1}] 根={root_cls!r} mmui={len(nodes)}")
                if nodes:
                    break

            if nodes:
                say()
                say("  ✅ 树物化了！这个候选是真的 gate。")
                winner = (rva, orig, old_prot)
                for d, c, n in nodes[:10]:
                    say(f"     {'  '*d}{c}  {n!r}")
                break
            else:
                say("  ✗ 无反应，还原中 ...")
                write_byte(h, va, orig)
                restore_prot(h, va, old_prot)
                time.sleep(0.2)
                now = read_byte(h, va)
                say(f"    还原后值: {now}")

        section("结果")
        if winner:
            say(f"  成功：RVA 0x{winner[0]:X}（原值 {winner[1]}）")
            say("  字节保留为 1。微信重启后会回到原值。")
        else:
            say("  全部候选都失败。")
            say("  正在确认所有试点都已还原 ...")
            for rva, orig, old_prot in tried:
                va = base + rva
                cur = read_byte(h, va)
                if cur is not None and cur != orig:
                    say(f"    RVA 0x{rva:X} 值 {cur} != {orig}，写回")
                    write_byte(h, va, orig)
                    if old_prot is not None:
                        restore_prot(h, va, old_prot)
                else:
                    say(f"    RVA 0x{rva:X} 已还原（值 {cur}）")
            say()
            say("  结论：这个版本的 gate 不在已知候选里，")
            say("        需要更精确的静态分析才能定位。")
        return 0
    finally:
        k32.CloseHandle(h)


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        import traceback
        traceback.print_exc()
        code = 1
    finally:
        try:
            with open(OUT, "w", encoding="utf-8") as f:
                f.write("\n".join(_lines))
        except OSError:
            pass
        try:
            input("\n按回车退出...")
        except EOFError:
            pass
    sys.exit(code)
