"""生成 v0.2.1 的发布附件：便携版 zip + 文件清单。

产物放到 release/ 下（在 .gitignore 里，不进仓库）。
"""
import hashlib
import json
import os
import sys
import zipfile
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, 'dist', 'BigFish')
OUT = os.path.join(ROOT, 'release')


def _read_version() -> str:
    """从 bigfish/__init__.py 读版本号 —— 和 make_release 用同一个真源。"""
    import re

    src = os.path.join(ROOT, 'bigfish', '__init__.py')
    try:
        with open(src, 'r', encoding='utf-8') as f:
            m = re.search(r'__version__\s*=\s*"([^"]+)"', f.read())
        if m:
            return m.group(1)
    except Exception:
        pass
    return '0.0.0'


VERSION = _read_version()

if not os.path.isdir(DIST):
    sys.exit('dist/BigFish 不存在，先打包')

os.makedirs(OUT, exist_ok=True)

# 打 zip 前把不该发的东西排掉：真实数据一律不进发布包
SKIP_DIRS = {'data', '__pycache__', '.tmp', 'wechatauto_db', 'voice'}
SKIP_FILES = {
    'config.json', 'settings.json', 'plugins.json',
    'keys.json', 'keys.json.bak', 'image_keys.json', 'credentials.json',
}
#: 按前缀再拦一遍。`config.json.bak` / `.bak2` / `.pre-rebuild` 这些备份里
#: 同样躺着真实 API Key —— 2026-11-09 实测漏了一个 `config.json.bak` 进包，
#: 是靠发布前反查 zip 名单才发现的。精确名列表永远追不上人的手。
SKIP_PREFIXES = ('config.json', 'settings.json', 'plugins.json', 'keys.json')
SKIP_EXT = {'.sqlite3', '.sqlite3-wal', '.sqlite3-shm', '.wav', '.mp3', '.log'}


def should_skip(rel: str) -> bool:
    parts = rel.replace('\\', '/').split('/')
    if any(part in SKIP_DIRS for part in parts[:-1]):
        return True
    name = parts[-1]
    if name in SKIP_FILES:
        return True
    if any(name.startswith(p) for p in SKIP_PREFIXES):
        return True
    return any(name.lower().endswith(ext) for ext in SKIP_EXT)


files = []
for root, dirs, names in os.walk(DIST):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    for n in names:
        full = os.path.join(root, n)
        rel = os.path.relpath(full, DIST)
        if should_skip(rel):
            continue
        files.append((full, rel))

files.sort(key=lambda x: x[1])

zip_path = os.path.join(OUT, f'BigFish-{VERSION}-portable.zip')
print(f'打包 {len(files)} 个文件 -> {zip_path}')
with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for full, rel in files:
        z.write(full, os.path.join(f'BigFish-{VERSION}', rel))

size = os.path.getsize(zip_path)
h = hashlib.sha256()
with open(zip_path, 'rb') as f:
    for chunk in iter(lambda: f.read(1 << 20), b''):
        h.update(chunk)

manifest = {
    'version': VERSION,
    'built_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    'archive': os.path.basename(zip_path),
    'archive_bytes': size,
    'archive_sha256': h.hexdigest(),
    'file_count': len(files),
    'excluded': [
        'data/', 'wechatauto_db/', 'voice/',
        # 前缀拦：备份里也有真实 Key（.bak / .bak2 / .pre-rebuild）
        'config.json*（含所有 .bak 变体）', 'settings.json*', 'plugins.json*',
        'keys.json*', 'image_keys.json', 'credentials.json',
        '*.sqlite3', '*.wav', '*.mp3', '*.log',
    ],
    'note': '便携版：解压到任意目录，双击 BigFish.exe。需要管理员权限（UIA 发送）。',
}
mp = os.path.join(OUT, f'BigFish-{VERSION}-manifest.json')
with open(mp, 'w', encoding='utf-8', newline='\n') as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)
    f.write('\n')

print(f'大小: {size / 1048576:.1f} MB')
print(f'SHA-256: {h.hexdigest()}')
print(f'清单: {mp}')
