"""说法层测试菜单。由 `大肥鱼-说法测试.bat` 调用。

为什么菜单在 Python 里而不是 bat 里：
    bat 文件按系统代码页（中文 Windows 是 GBK）解码，写成 UTF-8 会整行乱码，
    连 `cd D:\\大脑\\...` 都会失败。所以 bat 只保留纯 ASCII 的启动逻辑，
    中文全部由这里打印（Python 输出走 UTF-8，bat 已经 chcp 65001）。
"""
from __future__ import annotations

import os
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):
    if _s is not None:
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: (编号, 菜单文字, 要跑的命令)
ITEMS: list[tuple[str, str, list[str]]] = [
    ("1", "看效果      分条 / 节奏 / 攒批 / 确认（秒出，不碰微信）",
     ["tools/demo_say.py"]),
    ("2", "验插件      注册、配置、注入、面板生效",
     ["tools/selftest_plugin_say.py"]),
    ("3", "全部自检    6 组共 449 条断言",
     ["tools/selftest_say.py", "tools/selftest_listen.py",
      "tools/selftest_app_wiring.py", "tools/selftest_plugin_say.py",
      "tools/selftest_plugins.py", "tools/selftest_memory.py"]),
    ("4", "真机预览    读真实配置，打印要发什么（不发送）",
     ["tools/live_test_say.py"]),
    ("5", "真机发送    只发「文件传输助手」，还要手输 yes",
     ["tools/live_test_say.py", "--send"]),
    ("6", "打开目录    在资源管理器里看这些脚本", []),
]


def _banner() -> None:
    print("=" * 60)
    print("  大肥鱼 · 说法层测试")
    print(f"  仓库：{ROOT}")
    print("=" * 60)


def _run(scripts: list[str]) -> None:
    for script in scripts:
        path = os.path.join(ROOT, script.replace("/", os.sep))
        if not os.path.isfile(path):
            print(f"\n  [!] 找不到 {script}")
            return
        print(f"\n── {script} " + "─" * max(0, 40 - len(script)))
        code = subprocess.call([sys.executable, path], cwd=ROOT)
        if code != 0:
            print(f"\n  [!] {script} 退出码 {code}")
            return


def main() -> int:
    while True:
        os.system("cls" if os.name == "nt" else "clear")
        _banner()
        print()
        for key, text, _ in ITEMS:
            print(f"  [{key}] {text}")
        print("  [0] 退出")
        print()
        try:
            choice = input("请选择：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if choice == "0":
            return 0

        for key, _text, cmd in ITEMS:
            if key != choice:
                continue
            if key == "6":
                subprocess.call(["explorer", os.path.join(ROOT, "tools")])
                break
            if key == "5":
                print("\n  注意：会在微信里真的发消息，但只发给「文件传输助手」。")
            _run(cmd)
            print()
            try:
                input("按回车返回菜单 ...")
            except (EOFError, KeyboardInterrupt):
                return 0
            break


if __name__ == "__main__":
    raise SystemExit(main())
