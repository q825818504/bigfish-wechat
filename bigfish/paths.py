"""[bigfish] 路径锚点 —— 一个地方算清楚"程序在哪、资源在哪"。

═══════════════════════════════════════════════════════════════════════
为什么需要这个模块
═══════════════════════════════════════════════════════════════════════

PyInstaller 打进去的模块，`__file__` 可能是**相对路径**
（形如 `"bigfish/vault.py"`）。于是

    os.path.dirname(os.path.abspath(__file__))

会按**当前工作目录**解析 —— 算出来的位置取决于用户从哪儿启动 exe，
和 exe 在哪儿没关系。

2026-11-03 实测后果：
    从 dist/BigFish 启动 → 界面路径恰好对了
    从别处启动         → UI_DIR 变成 <安装目录>/ui，界面加载失败，
                         用户看到 Chromium 的「无法访问您的文件」
    数据目录同理       → 会建到启动目录旁边，而不是安装目录

那种错误一个字都不提路径，看起来像文件丢了。

═══════════════════════════════════════════════════════════════════════
两个锚点
═══════════════════════════════════════════════════════════════════════

    app_root()   程序所在目录（用户看得见的那层）
                 打包后 = exe 所在目录；开发态 = 仓库根
                 用途：data/、config.json、settings.json 这些**用户可见**的东西

    res_root()   资源根
                 打包后 = _internal/（PyInstaller 的 sys._MEIPASS）
                 开发态 = 仓库根
                 用途：ui/、bridge/、vendor/ 这些**跟程序走**的东西

为什么要分：打包后它们是两层目录，开发态是同一层。
所有路径判断都该走这两个函数，不要自己拼 `__file__`。

═══════════════════════════════════════════════════════════════════════
用法
═══════════════════════════════════════════════════════════════════════

    from bigfish.paths import app_root, res_root

    UI_DIR      = os.path.join(res_root(), "ui")
    DATA_DIR    = os.path.join(app_root(), "data")
    CONFIG_PATH = os.path.join(app_root(), "config.json")

⚠ 这个模块**不 import 任何 bigfish 子模块** —— 它被所有人依赖，
  自己再去依赖别人就会循环。
"""
from __future__ import annotations

import os
import sys

#: 是否运行在 PyInstaller 打包后的环境里
FROZEN = bool(getattr(sys, "frozen", False))

#: 仓库根（开发态）。打包后没有意义，但仍然算出来备用。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_frozen() -> bool:
    return FROZEN


def app_root() -> str:
    """程序所在目录。

    打包后 = exe 所在目录（`dist/BigFish` 或安装目录）
    开发态 = 仓库根

    用户可见的东西放这里：data/、config.json、settings.json。
    """
    if FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return _REPO_ROOT


def res_root() -> str:
    """资源根。

    打包后 = PyInstaller 的 `sys._MEIPASS`（onedir 下是 `_internal`）
    开发态 = 仓库根

    **绝对路径**，不受启动位置影响 —— 这是它比 `__file__` 可靠的地方。

    `sys._MEIPASS` 理论上一定存在；万一没有（非标准打包），
    退回 `<app_root>/_internal`，再不行就用 app_root。
    """
    if not FROZEN:
        return _REPO_ROOT
    mei = getattr(sys, "_MEIPASS", "")
    if mei and os.path.isdir(mei):
        return os.path.abspath(mei)
    cand = os.path.join(app_root(), "_internal")
    if os.path.isdir(cand):
        return cand
    return app_root()


def res_path(*parts: str) -> str:
    """资源根下的某个路径。`res_path("ui", "index.html")`"""
    return os.path.join(res_root(), *parts)


def app_path(*parts: str) -> str:
    """程序目录下的某个路径。`app_path("data")`"""
    return os.path.join(app_root(), *parts)


def find_res_dir(name: str, marker: str = "") -> str:
    """在几个可能的位置里找资源目录。

    候选顺序：res_root 下 → app_root 下 → 仓库根下。
    `marker` 给了就要求那个文件存在才算命中，否则只看目录存在。

    为什么要多候选：便携版可能把 ui/ 放在 exe 旁边（用户能改），
    也可能是打包进 _internal 的。两种都得支持。
    """
    for base in (res_root(), app_root(), _REPO_ROOT):
        cand = os.path.join(base, name)
        if marker:
            if os.path.isfile(os.path.join(cand, marker)):
                return cand
        elif os.path.isdir(cand):
            return cand
    # 一个都没有：返回首选位置，让调用方自己去报"找不到"
    return os.path.join(res_root(), name)
