"""升级微信前的状态记录与备份清单。

只读 + 生成本地快照，不改任何微信文件。

要记录：
  1. 当前微信版本、安装路径、数据目录
  2. 各账号目录大小、最后修改时间
  3. 数据库文件清单与大小（用于升级后对比有没有丢数据）
  4. 备份建议清单
"""
import glob
import json
import os
import shutil
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_JSON = os.path.join(ROOT, "upgrade_snapshot.json")
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


def dir_size(path, limit_files=100000):
    total = 0
    n = 0
    for base, dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
                n += 1
                if n > limit_files:
                    return total, n
            except OSError:
                pass
    return total, n


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


snapshot = {
    "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "weixin_install": {},
    "data_root": "",
    "accounts": [],
    "dbs_per_account": {},
}

section("1. 安装信息")
exe = r"D:\WeChat\Weixin\Weixin.exe"
if os.path.isfile(exe):
    st = os.stat(exe)
    snapshot["weixin_install"] = {
        "exe": exe,
        "size": st.st_size,
        "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
    }
    say(f"  {exe}")
    say(f"  大小 {human(st.st_size)}")

    # 版本目录
    verdirs = glob.glob(os.path.join(os.path.dirname(exe), "*"))
    for d in verdirs:
        if os.path.isdir(d):
            name = os.path.basename(d)
            sz, n = dir_size(d, 5000)
            say(f"  版本目录 {name}: {human(sz)} / {n} 文件")
            snapshot["weixin_install"].setdefault("version_dirs", {})[name] = {
                "size": sz, "files": n}
else:
    say("  找不到 Weixin.exe")

section("2. 数据目录")
roots = [
    r"C:\Users\Alime\Documents\xwechat_files",
    r"D:\xwechat_files",
]
for r in roots:
    if os.path.isdir(r):
        snapshot["data_root"] = r
        say(f"  {r}")
        break

if snapshot["data_root"]:
    root = snapshot["data_root"]
    accounts = sorted(glob.glob(os.path.join(root, "wxid_*")))
    for acc in accounts:
        if not os.path.isdir(acc):
            continue
        name = os.path.basename(acc)
        dbdir = os.path.join(acc, "db_storage")
        sz, n = dir_size(dbdir) if os.path.isdir(dbdir) else (0, 0)
        newest = 0
        if os.path.isdir(dbdir):
            for base, _, files in os.walk(dbdir):
                for f in files:
                    try:
                        newest = max(newest, os.path.getmtime(
                            os.path.join(base, f)))
                    except OSError:
                        pass
        dbs = []
        if os.path.isdir(dbdir):
            for base, _, files in os.walk(dbdir):
                for f in files:
                    if f.endswith(".db"):
                        p = os.path.join(base, f)
                        try:
                            dbs.append({
                                "rel": os.path.relpath(p, dbdir),
                                "size": os.path.getsize(p),
                            })
                        except OSError:
                            pass
        snapshot["accounts"].append({
            "name": name,
            "db_storage_size": sz,
            "db_storage_files": n,
            "newest_mtime": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(newest)) if newest else "",
            "db_count": len(dbs),
        })
        snapshot["dbs_per_account"][name] = sorted(
            dbs, key=lambda x: -x["size"])

        say(f"  [{name}]")
        say(f"    db_storage: {human(sz)} / {n} 文件 / {len(dbs)} 个 .db")
        say(f"    最近修改: {snapshot['accounts'][-1]['newest_mtime']}")
        for d in sorted(dbs, key=lambda x: -x["size"])[:5]:
            say(f"      {d['rel']:48s} {human(d['size'])}")

section("3. 关键文件位置（升级后要核对这些还在）")
key_paths = [
    (r"C:\Users\Alime\Documents\xwechat_files", "聊天数据根目录"),
    (r"D:\WeChat\Weixin", "安装目录"),
    (os.path.expandvars(r"%APPDATA%\Tencent\xwechat"), "配置与日志"),
    (os.path.expandvars(r"%APPDATA%\Tencent\xwechat\config"), "账号配置"),
]
for p, label in key_paths:
    exists = os.path.isdir(p)
    say(f"  {'OK ' if exists else 'MISS'} {label}")
    say(f"       {p}")

section("4. 备份建议")
say("  升级前建议手动备份（升级一般不删数据，但保险）：")
say()
say(f"  1. 账号配置（很小，几 KB）")
say(f"     {os.path.expandvars(r'%APPDATA%')}\\Tencent\\xwechat\\config")
say()
say(f"  2. 聊天数据（很大，几十 GB）")
say(f"     {snapshot['data_root']}")
say()
say("     如果空间够，整个目录复制一份最稳。")
say("     空间不够的话，至少保留原目录不要删——微信升级通常原地保留。")

section("5. 快照已保存")
with open(OUT_JSON, "w", encoding="utf-8") as f:
    json.dump(snapshot, f, ensure_ascii=False, indent=2)
say(f"  {OUT_JSON}")
say("  升级后可以再跑一次，对比 db_count 有没有变化。")

with open(os.path.join(HERE, "_upgrade_snapshot.txt"), "w",
          encoding="utf-8") as f:
    f.write("\n".join(_lines))
