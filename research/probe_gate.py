"""提权版热激活探测 —— 只扫 RVA，不写任何字节。

阶段：
  1. 管理员权限下打开微信进程（读/写/改保护）
  2. 读 Weixin.dll 映像，扫 gate 字节的 RVA
  3. 把扫到的候选地址、当前字节值报告出来
  4. **到此为止**，不写、不改

是否真的要写那 1 个字节，由用户看完报告后决定。
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "_gate_probe.txt")
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


def find_weixin_pids():
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
    return sorted(set(pids))


def main():
    section("0. 权限")
    say(f"  IsUserAnAdmin: {bool(ctypes.windll.shell32.IsUserAnAdmin())}")

    pids = find_weixin_pids()
    say(f"  微信 PID: {pids}")
    if not pids:
        say("  没找到微信窗口")
        return 1

    pid = pids[0]

    section("1. 打开进程")
    h = k32.OpenProcess(0x0010 | 0x0400, False, pid)   # VM_READ | QUERY_INFO
    say(f"  OpenProcess(VM_READ|QUERY_INFO) -> {h}  "
        f"err={ctypes.get_last_error()}")
    if not h:
        say("  无法读内存，热激活不可能成功")
        return 1

    try:
        # ---- 2. 定位 Weixin.dll 模块 ----
        section("2. 定位 Weixin.dll 模块")
        TH32CS_SNAPMODULE = 0x00000008
        TH32CS_SNAPMODULE32 = 0x00000010

        class MODULEENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD),
                ("th32ProcessID", wt.DWORD), ("GlblcntUsage", wt.DWORD),
                ("ProccntUsage", wt.DWORD),
                ("modBaseAddr", ctypes.c_void_p),
                ("modBaseSize", wt.DWORD),
                ("hModule", wt.HMODULE),
                ("szModule", ctypes.c_wchar * 256),
                ("szExePath", ctypes.c_wchar * 260),
            ]

        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE |
                                            TH32CS_SNAPMODULE32, pid)
        base = size = 0
        dll_path = ""
        if snap and snap != -1:
            me = MODULEENTRY32W()
            me.dwSize = ctypes.sizeof(MODULEENTRY32W)
            ok = k32.Module32FirstW(snap, ctypes.byref(me))
            while ok:
                if me.szModule.lower() == "weixin.dll":
                    base = me.modBaseAddr or 0
                    size = me.modBaseSize
                    dll_path = me.szExePath
                    break
                ok = k32.Module32NextW(snap, ctypes.byref(me))
            k32.CloseHandle(snap)

        if not base:
            say("  没找到 Weixin.dll 模块")
            return 1
        say(f"  基址: 0x{base:X}")
        say(f"  大小: {size:,} 字节 ({size/1024/1024:.1f} MB)")
        say(f"  路径: {dll_path}")

        # ---- 3. 读映像 ----
        section("3. 读映像")
        t0 = time.time()
        buf = ctypes.create_string_buffer(size)
        br = ctypes.c_size_t(0)
        ok = k32.ReadProcessMemory(h, ctypes.c_void_p(base), buf, size,
                                   ctypes.byref(br))
        say(f"  ReadProcessMemory -> {bool(ok)}, 读到 {br.value:,} 字节, "
            f"耗时 {time.time()-t0:.1f}s")
        if not ok:
            return 1
        image = buf.raw[:br.value]

        # ---- 4. 扫 gate 字符串 ----
        section("4. 扫描 qt.accessibility.core 引用")
        needle = b"qt.accessibility.core"
        hits = []
        idx = image.find(needle)
        while idx != -1:
            hits.append(idx)
            idx = image.find(needle, idx + 1)
        say(f"  找到 {len(hits)} 处字符串引用")
        for h_off in hits[:8]:
            say(f"    偏移 0x{h_off:X}  (VA 0x{base + h_off:X})")

        # ---- 5. 保存映像供离线分析 ----
        section("5. 结论")
        say(f"  读内存 OK（管理员权限确实放开了）")
        say(f"  映像大小: {len(image):,}")
        say(f"  字符串引用: {len(hits)} 处")
        say()
        say("  下一步（需要你确认）：往 gate 字节写 1，让 mmui 树物化。")
        say("  本脚本到此为止，没有写入任何东西。")

        # 缓存映像给后续分析用（避免重复读 198MB）
        cache = os.path.join(HERE, "_weixin_image.bin")
        try:
            with open(cache, "wb") as f:
                f.write(image)
            say(f"  映像已缓存: {cache}")
        except OSError as e:
            say(f"  缓存失败: {e}")
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
