"""热激活 UIA 控件树 —— 只写 1 个字节，带回读校验与自动还原。

你要清楚这个脚本会做什么：

  往微信进程的内存里写 1 个字节：0 -> 1
  这个字节是 Qt 的「无障碍开关」(QAccessible active byte)。
  它本来是 Windows 读屏软件（讲述人/NVDA）用来让微信暴露控件树的，
  微信把它默认关着。拨开之后，微信开始对外提供 mmui::* 控件节点。

会做的动作（按顺序）：
  1. OpenProcess 拿到 VM_READ | VM_WRITE | VM_OPERATION
  2. 定位候选字节地址，读出当前值
  3. VirtualProtectEx 把那一页改成可写
  4. WriteProcessMemory 写 1
  5. 立刻回读 UIA 树，看根节点是不是变成了 mmui::MainWindow
  6. 校验通过 → 保留；不通过 → 写回原值，还原保护属性
  7. 关闭句柄，退出

不会做的动作：
  - 不注入 DLL，不 LoadLibrary
  - 不创建远程线程
  - 不修改 Weixin.dll 或任何文件
  - 不在进程里留任何代码
  - 不留持久化（微信重启后字节自动恢复 0）

用法：
  probe_uia_wake.bat            # 预演：只报告，不写
  probe_uia_wake.bat --write    # 执行写入
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

OUT = os.path.join(HERE, "_wake_result.txt")
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

# 上游在 4.1.10.27 上扫出来的候选（离线扫描 + 值判断得出的）
# RVA 0xA142BC7，当前值 0，符合「布尔开关」特征
CANDIDATE_RVA = 0xA142BC7

PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_QUERY_INFORMATION = 0x0400

PAGE_READWRITE = 0x04
PAGE_EXECUTE_READWRITE = 0x40
PAGE_READONLY = 0x02
PAGE_EXECUTE_READ = 0x20


def find_weixin():
    """返回 (pid, dll_base, dll_size, dll_path)"""
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

    class MODULEENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD),
            ("th32ProcessID", wt.DWORD), ("GlblcntUsage", wt.DWORD),
            ("ProccntUsage", wt.DWORD), ("modBaseAddr", ctypes.c_void_p),
            ("modBaseSize", wt.DWORD), ("hModule", wt.HMODULE),
            ("szModule", ctypes.c_wchar * 256),
            ("szExePath", ctypes.c_wchar * 260),
        ]

    snap = k32.CreateToolhelp32Snapshot(0x00000008 | 0x00000010, pid)
    if not snap or snap == -1:
        return None
    base = size = 0
    path = ""
    me = MODULEENTRY32W()
    me.dwSize = ctypes.sizeof(MODULEENTRY32W)
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


def uia_root_class():
    """看当前 UIA 根节点的类名，用于判断树有没有物化。"""
    try:
        import uiautomation as auto
        hwnd = ctypes.windll.user32.FindWindowW("Qt51514QWindowIcon", None)
        if not hwnd:
            # 热激活后类名可能变了，退而求其次找标题「微信」的窗口
            hwnd = ctypes.windll.user32.FindWindowW(None, "微信")
        if not hwnd:
            return None
        ctrl = auto.ControlFromHandle(hwnd)
        return getattr(ctrl, "ClassName", None) if ctrl else None
    except Exception:
        return None


def count_mmui_nodes(max_depth=4, limit=20):
    try:
        import uiautomation as auto
        hwnd = ctypes.windll.user32.FindWindowW("Qt51514QWindowIcon", None)
        if not hwnd:
            hwnd = ctypes.windll.user32.FindWindowW(None, "微信")
        if not hwnd:
            return []
        root = auto.ControlFromHandle(hwnd)
        found = []

        def walk(c, d=0):
            if d > max_depth or len(found) >= limit:
                return
            try:
                for ch in c.GetChildren():
                    cn = getattr(ch, "ClassName", "") or ""
                    nm = getattr(ch, "Name", "") or ""
                    if "mmui" in cn.lower():
                        found.append((d, cn, nm[:40]))
                        if len(found) >= limit:
                            return
                    walk(ch, d + 1)
            except Exception:
                pass

        walk(root)
        return found
    except Exception:
        return []


def main():
    do_write = "--write" in sys.argv

    section("0. 环境")
    say(f"  管理员: {bool(ctypes.windll.shell32.IsUserAnAdmin())}")
    say(f"  模式  : {'写入' if do_write else '预演（不写）'}")

    found = find_weixin()
    if not found:
        say("  [!] 没找到微信")
        return 1
    pid, base, size, path = found
    say(f"  PID   : {pid}")
    say(f"  DLL   : {path}")
    say(f"  基址  : 0x{base:X}   大小: {size:,}")

    target_va = base + CANDIDATE_RVA
    say(f"  目标字节 VA: 0x{target_va:X}  (base + 0x{CANDIDATE_RVA:X})")

    section("1. 打开进程")
    access = (PROCESS_VM_READ | PROCESS_QUERY_INFORMATION
              | (PROCESS_VM_WRITE | PROCESS_VM_OPERATION if do_write else 0)) \
        if do_write else (PROCESS_VM_READ | PROCESS_QUERY_INFORMATION)
    h = k32.OpenProcess(access, False, pid)
    say(f"  OpenProcess(0x{access:X}) -> {h}  err={ctypes.get_last_error()}")
    if not h:
        say("  失败")
        return 1

    try:
        section("2. 读当前值")
        buf = ctypes.create_string_buffer(1)
        br = ctypes.c_size_t(0)
        ok = k32.ReadProcessMemory(h, ctypes.c_void_p(target_va), buf, 1,
                                   ctypes.byref(br))
        if not ok:
            say(f"  读取失败 err={ctypes.get_last_error()}")
            return 1
        orig = buf.raw[0]
        say(f"  当前字节值: {orig} (0x{orig:02X})")

        section("3. 记录 UIA 状态（写之前）")
        before_root = uia_root_class()
        before_mmui = count_mmui_nodes()
        say(f"  根节点类名: {before_root!r}")
        say(f"  mmui 节点数: {len(before_mmui)}")

        if not do_write:
            section("4. 预演结束")
            say("  没有写入任何东西。")
            say("  要真正执行，用: probe_uia_wake.bat --write")
            return 0

        if orig == 1:
            say("\n  字节已经是 1 了，树应该本来就物化了，无需再写。")
            return 0

        section("4. 写入")
        # 改保护
        old_prot = wt.DWORD(0)
        ok = k32.VirtualProtectEx(h, ctypes.c_void_p(target_va), 1,
                                  PAGE_EXECUTE_READWRITE,
                                  ctypes.byref(old_prot))
        say(f"  VirtualProtectEx -> {bool(ok)} 旧保护=0x{old_prot.value:X} "
            f"err={ctypes.get_last_error()}")
        if not ok:
            return 1

        try:
            one = ctypes.create_string_buffer(b"\x01", 1)
            bw = ctypes.c_size_t(0)
            ok = k32.WriteProcessMemory(h, ctypes.c_void_p(target_va), one, 1,
                                        ctypes.byref(bw))
            say(f"  WriteProcessMemory -> {bool(ok)} 写入 {bw.value} 字节 "
                f"err={ctypes.get_last_error()}")
            if not ok:
                return 1

            # 回读
            time.sleep(0.4)
            check = ctypes.create_string_buffer(1)
            k32.ReadProcessMemory(h, ctypes.c_void_p(target_va), check, 1,
                                  ctypes.byref(check))
            say(f"  回读值: {check.raw[0]}")

            section("5. 校验 UIA 树")
            after_root = None
            after_mmui = []
            for attempt in range(6):
                time.sleep(0.5)
                after_root = uia_root_class()
                after_mmui = count_mmui_nodes()
                say(f"  第 {attempt+1} 次检查: 根={after_root!r} "
                    f"mmui节点={len(after_mmui)}")
                if after_mmui or (after_root and "mmui" in str(after_root).lower()):
                    break

            success = bool(after_mmui) or (
                after_root and "mmui" in str(after_root).lower())

            section("6. 结果")
            if success:
                say("  ✅ 成功：mmui 控件树已物化")
                say(f"     根节点: {after_root!r}")
                say(f"     节点数: {len(after_mmui)}")
                for d, c, n in after_mmui[:10]:
                    say(f"       {'  '*d}{c}  {n!r}")
                say("\n  字节保留为 1。微信重启后会恢复 0。")
            else:
                say("  ❌ 失败：树没有物化，正在还原字节 ...")
                back = ctypes.create_string_buffer(bytes([orig]), 1)
                k32.WriteProcessMemory(h, ctypes.c_void_p(target_va), back, 1,
                                       ctypes.byref(bw))
                k32.VirtualProtectEx(h, ctypes.c_void_p(target_va), 1,
                                     old_prot.value, ctypes.byref(old_prot))
                say("  已还原。")
        finally:
            try:
                k32.VirtualProtectEx(h, ctypes.c_void_p(target_va), 1,
                                     old_prot.value, ctypes.byref(old_prot))
            except Exception:
                pass
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
