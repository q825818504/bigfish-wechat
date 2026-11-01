# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

产物结构（onedir，比 onefile 启动快很多，且 WebEngine 在 onefile 下容易崩）：

    dist/大肥鱼/
        大肥鱼.exe          主程序
        _internal/          所有依赖（Python、Qt、WebEngine）
        ui/                 界面文件（index.html / style.css / app.js）
        bridge/             Node 桥接脚本
        config.example.json
        README.md

为什么 onedir 而不是 onefile：
    QtWebEngine 需要从磁盘加载 .pak / .bin 资源，onefile 会把它们解压到
    临时目录，每次启动都解压 200MB，且杀软容易拦。onedir 只解压一次。

为什么 ui/ 和 bridge/ 放在 exe 外面：
    界面要能改（换配色、调布局），bridge.mjs 要能被 Node 直接执行。
    打进包里就动不了了。
"""
import os
import sys

from PyInstaller.utils.hooks import collect_data_files

ROOT = os.path.abspath(os.path.dirname(os.path.abspath(SPECPATH)))

# ── 数据文件 ──
datas = []

# 界面
datas.append((os.path.join(ROOT, "ui"), "ui"))

# Node 桥接（作为外部脚本被 node 调用，不打进 exe）
datas.append((os.path.join(ROOT, "bridge"), "bridge"))

# 配置模板与说明
for name in ("config.example.json", "README.md"):
    p = os.path.join(ROOT, name)
    if os.path.isfile(p):
        datas.append((p, "."))

# vendor 里的 wechatauto（源码形式，被 import）
vendor = os.path.join(ROOT, "vendor", "wechatauto-replica", "wechatauto")
if os.path.isdir(vendor):
    datas.append((vendor, "wechatauto"))

# PySide6 WebEngine 的运行时资源
try:
    datas += collect_data_files("PySide6", subdir="resources")
except Exception:
    pass

# ── 隐式导入 ──
hiddenimports = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebChannel",
    "PySide6.QtNetwork",
    "bigfish.soul",
    "bigfish.render",
    "bigfish.wechat",
    "bigfish.bridge_client",
    "bigfish.replier",
    "bigfish.watcher",
    "bigfish.wechat_sender",
    "bigfish.updater",
    "wechatauto",
    "wechatauto.db",
    "wechatauto.uia_driver",
    "wechatauto.accessibility",
    "wechatauto.sender",
]

# ── 排除（减小体积）──
excludes = [
    "tkinter", "matplotlib", "scipy", "pandas",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore",
    "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtSerialPort", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtDesigner", "PySide6.QtHelp",
]

a = Analysis(
    [os.path.join(ROOT, "bigfish", "desktop.py")],
    pathex=[ROOT, os.path.join(ROOT, "vendor", "wechatauto-replica")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# ── 剔除无用资源，压缩体积 ──
# PySide6 会把整套 Qt 都带进来（432M），其中很大一部分我们的应用用不到。
# 这里在打包之前把它们从 TOC 里摘掉，而不是打包后再删文件——
# 后删会让 COLLECT 的清单对不上，PyInstaller 下次构建会重新收集。
_DROP_NAMES = {
    # ── 只删「确定安全」的东西 ──
    # 教训：Qt 组件间的运行时依赖是隐性的。实测两轮：
    #   删 Qt6Qml      -> QtWebChannel 加载失败
    #   删 Qt6OpenGLWidgets / Qt6Pdf -> QtWebEngineWidgets 加载失败
    # 所以这里只保留确定与运行无关的几项。

    # WebEngine 的 DevTools 调试资源（73M + 12M）—— 正式版不需要
    "qtwebengine_devtools_resources.debug.pak",
    "qtwebengine_devtools_resources.pak",
    "v8_context_snapshot.debug.bin",

    # WebEngine 调试版资源包
    "qtwebengine_resources.debug.pak",
    "qtwebengine_resources_100p.debug.pak",
    "qtwebengine_resources_200p.debug.pak",
}

_DROP_DIR_PARTS = (
    # 翻译文件只保留中文
    ("translations", "qtwebengine_locales"),
)

_DROP_TRANSLATION_KEEP = ("zh", "en")


def _keep(entry) -> bool:
    name = entry[0].replace("\\", "/")
    base = os.path.basename(name)
    if base in _DROP_NAMES:
        return False

    lower = name.lower()
    # PySide6 自带的多语言翻译：只留中文和英文
    if "/pyside6/translations/" in lower or lower.startswith("pyside6/translations/"):
        stem = os.path.splitext(base)[0].lower()
        if not any(k in stem for k in _DROP_TRANSLATION_KEEP):
            return False

    # 其它 Qt 组件的翻译
    if "qtwebengine_locales" in lower:
        if not any(k in base.lower() for k in ("zh", "en-us", "en-gb")):
            return False

    # 用不到的 Qt 模块翻译
    for junk in ("_ja", "_ko", "_de", "_fr", "_es", "_it", "_ru",
                 "_pt", "_tr", "_pl", "_cs", "_nl", "_sv", "_fi",
                 "_ar", "_he", "_th", "_vi", "_uk", "_da", "_nb"):
        if base.endswith(junk + ".qm"):
            return False

    return True


a.binaries = [b for b in a.binaries if _keep(b)]
a.datas = [d for d in a.datas if _keep(d)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BigFish",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # GUI 程序，不弹黑窗
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, "ui", "icon.ico")
        if os.path.isfile(os.path.join(ROOT, "ui", "icon.ico")) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="BigFish",
)
