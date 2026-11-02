"""生成 v0.2.0 的发布附件：便携版 zip + 文件清单。

产物放到 release/ 下（在 .gitignore 里，不进仓库）。
"""
import hashlib
import json
import os
import shutil
import sys
import zipfile
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, 'dist', 'BigFish')
OUT = os.path.join(ROOT, 'release')
VERSION = '0.2.0'

if not os.path.isdir(DIST):
    sys.exit('dist/BigFish 不存在，先打包')

os.makedirs(OUT, exist_ok=True)

# 打 zip 前把不该发的东西排掉：真实数据一律不进发布包
SKIP_DIRS = {'data', '__pycache__', '.tmp'}
SKIP_FILES = {'config.json', 'settings.json', 'plugins.json'}


def should_skip(rel: str) -> bool:
    parts = rel.replace('\\', '/').split('/')
    if parts[0] in SKIP_DIRS:
        return True
    return parts[-1] in SKIP_FILES and len(parts) <= 2


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
    'excluded': ['data/', 'config.json', 'settings.json', 'plugins.json'],
    'note': '便携版：解压到任意目录，双击 BigFish.exe。需要管理员权限（UIA 发送）。',
}
mp = os.path.join(OUT, f'BigFish-{VERSION}-manifest.json')
with open(mp, 'w', encoding='utf-8', newline='\n') as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)
    f.write('\n')

print(f'大小: {size / 1048576:.1f} MB')
print(f'SHA-256: {h.hexdigest()}')
print(f'清单: {mp}')
