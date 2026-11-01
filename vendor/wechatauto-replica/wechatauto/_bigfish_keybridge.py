"""[bigfish] 管理员权限密钥提取桥。

背景（实测数据，Windows 10 / 微信 4.1.10.27）：
    OpenProcess(PROCESS_VM_READ) 在普通权限下返回 err=5 ACCESS_DENIED，
    在管理员权限下 OK。因此读取数据库密钥必须提权。

为什么不做「整个程序提权」：
    1. 整个程序提权后，拖拽文件、写普通用户目录、调用外部工具都会变麻烦；
    2. wechatauto 的发送路径（GUI/OCR）在提权会话里反而容易出问题；
    3. 权限越大暴露面越大，只有「读密钥」这一步真正需要它。

设计：
    父进程（普通权限）
        └─ 检查缓存是否有可用密钥
            └─ 没有 → 通过 ShellExecuteW(runas) 启动一个短命的
               「管理员子进程」只做一件事：扫描微信进程内存，
               把 {rel: hex_key} 写到一个临时文件
                └─ 父进程读临时文件，继续用普通权限跑其余一切

子进程入口是 `python -m wechatauto._bigfish_keybridge --dump <out>`。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from typing import Dict, Optional

IS_WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------------------
# 权限检测
# ---------------------------------------------------------------------------
def is_admin() -> bool:
    """当前进程是否已提升。"""
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 管理员子进程：只做「扫描 → 落盘」
# ---------------------------------------------------------------------------
def dump_keys_as_admin(timeout: float = 180.0) -> Optional[Dict[str, str]]:
    """以管理员身份运行一次密钥扫描，返回 {rel: hex_key}。

    失败（用户拒绝 UAC / 超时 / 扫描不到）返回 None，绝不抛异常——
    调用方可以据此回退到其它策略或给出可读的提示。
    """
    if not IS_WINDOWS:
        return None

    fd, out_path = tempfile.mkstemp(prefix="bf_keys_", suffix=".json")
    os.close(fd)
    try:
        os.unlink(out_path)  # 让子进程自己创建，避免空文件被误读
    except OSError:
        pass

    # 子进程用同一个解释器，保证依赖一致
    py = sys.executable
    pkg = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    vec = os.path.join(os.path.dirname(pkg), ".venv", "Scripts", "python.exe")
    if os.path.exists(vec):
        py = vec

    script = os.path.abspath(__file__)

    # 用 -Wait 等它跑完；提权失败的返回码由 PowerShell 抛出
    ps = (
        "$ErrorActionPreference='Stop';"
        f"$p = Start-Process -FilePath '{py}' "
        f"-ArgumentList '-X','utf8','{script}','--dump','{out_path}' "
        "-Verb RunAs -Wait -PassThru -WindowStyle Hidden;"
        "exit $p.ExitCode"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", ps],
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return None

    # 等文件落盘（子进程 close 之后可能有一点点延迟）
    for _ in range(20):
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            break
        time.sleep(0.1)

    try:
        with open(out_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        keys = data.get("keys") or {}
        return {k: v for k, v in keys.items() if v}
    except Exception:
        return None
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 子进程入口
# ---------------------------------------------------------------------------
def _run_dump(out_path: str) -> int:
    """管理员子进程的真正工作：扫描微信进程内存取密钥，写入 out_path。"""
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    result = {"ok": False, "keys": {}, "error": "", "pids": []}
    try:
        # 延迟导入：确保在 sys.path 就绪后再加载 db 模块
        from wechatauto.db import WeChatDB

        probe = WeChatDB.__new__(WeChatDB)
        pids = probe._find_weixin_pids()
        result["pids"] = pids
        if not pids:
            result["error"] = "未找到 Weixin.exe 进程窗口"
        else:
            db = WeChatDB()
            keys = db.extract_keys()
            result["keys"] = {rel: k.hex() for rel, k in keys.items()}
            result["ok"] = bool(keys)
            if not keys:
                result["error"] = "扫描完成但未取到可用密钥"
    except Exception as exc:  # noqa: BLE001 - 子进程必须把错误带回父进程
        import traceback
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
    except Exception:
        return 2
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    if "--dump" in sys.argv:
        idx = sys.argv.index("--dump")
        target = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else ""
        if not target:
            sys.exit(3)
        sys.exit(_run_dump(target))
    else:
        print("用法: python -m wechatauto._bigfish_keybridge --dump <out.json>")
        sys.exit(0)
