"""创建 GitHub Release 并上传附件。

从 .secrets/github_token.txt 读 token（不打印、不落日志）。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
TOKEN_FILE = os.path.join(ROOT, ".secrets", "github_token.txt")
REPO = "q825818504/bigfish-wechat"
TAG = "v0.1.0"
VERSION = "0.1.0"

# 不走系统代理（本机代理未启动）
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)


def token() -> str:
    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()


def api(path: str, method: str = "GET", data=None, raw=False):
    url = "https://api.github.com" + path
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "User-Agent": "bigfish-release",
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token()}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode()) if not raw else r.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"HTTP {exc.code} on {path}\n{detail}") from exc


BODY = """## 大肥鱼 × 微信 v0.1.0

把 DSH 的大肥鱼灵魂接进微信。**用你自己的微信号，不是 Bot。**

### 这一版能做什么

| 环节 | 实现 |
|---|---|
| 读 | SQLCipher 数据库解密，20/20 密钥 |
| 听 | 轮询监听，1.2 秒间隔 |
| 想 | 人格 + 情绪 + 记忆（调 dsh-bigfish 原版模块）+ 模型 |
| 发 | UIA 控件树驱动，数据库回读确认 |

### 实测记录

真实双号对话验证：手机大号发消息 → 电脑小号回复 → 回一句就停。

```
BigFish.exe --selftest     7/7 通过
BigFish.exe --live-send    发送 + 落库确认
```

### 前置条件

- 微信 **4.1.15.53**（4.1.10.27 的 UIA 无法热激活）
- **管理员权限**（热激活要写 1 个 gate 字节）
- **桌面解锁且微信可见**（UIA 是界面操作）

### 防护

- 四层回环防护（`allow_self` / 内容比对 / 归一化 / 频率熔断）
- 群聊只在被 @ 或叫名字时回
- 发送前校验输入框归属，防止发错人

### 已知限制

- 微信重启后程序失联，需手动重启程序
- 群聊未做真实 @ 场景验证
- 语音/表情包发送未实现
- 记忆只读，微信对话还没写回 DSH 记忆库

### 安装

下载 `BigFish-0.1.0-setup.zip`，解压后双击 `install.bat`。
或下载 `BigFish-0.1.0-portable.zip`，解压即用。
"""

ASSETS = [
    ("BigFish-0.1.0-setup.zip",
     os.path.join(DIST, "BigFish-0.1.0-setup.zip")),
    ("BigFish-0.1.0-portable.zip",
     os.path.join(DIST, "BigFish-0.1.0-portable.zip")),
    ("BigFish-0.1.0-manifest.json",
     os.path.join(DIST, "BigFish-0.1.0-manifest.json")),
]


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    print("=" * 66)
    print("  创建 GitHub Release")
    print("=" * 66)

    # 检查附件
    for name, path in ASSETS:
        if not os.path.isfile(path):
            print(f"  [!] 缺少 {path}")
            return 1
        print(f"  {name:34s} {os.path.getsize(path)/1024/1024:7.1f} MB")

    # 已有 Release 就复用
    print("\n[1] 检查是否已有 Release")
    try:
        rel = api(f"/repos/{REPO}/releases/tags/{TAG}")
        print(f"    已存在 #{rel['id']}，复用")
    except RuntimeError as exc:
        if "404" not in str(exc):
            raise
        print("    不存在，创建中 ...")
        rel = api(f"/repos/{REPO}/releases", "POST", {
            "tag_name": TAG,
            "name": f"v{VERSION}",
            "body": BODY,
            "draft": False,
            "prerelease": False,
        })
        print(f"    创建成功 #{rel['id']}")

    upload_url = rel["upload_url"].split("{")[0]
    existing = {a["name"] for a in rel.get("assets", [])}

    # 上传附件
    print("\n[2] 上传附件")
    for name, path in ASSETS:
        if name in existing:
            print(f"    {name} 已存在，跳过")
            continue

        size = os.path.getsize(path)
        digest = sha256_of(path)
        print(f"    {name}  ({size/1024/1024:.1f} MB)")
        print(f"      sha256={digest[:20]}...")

        url = f"{upload_url}?name={urllib.request.quote(name)}"
        with open(path, "rb") as f:
            data = f.read()

        req = urllib.request.Request(url, data=data, method="POST", headers={
            "User-Agent": "bigfish-release",
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token()}",
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(data)),
        })
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                info = json.loads(r.read().decode())
            print(f"      ✓ 上传完成 {time.time()-t0:.0f}s")
            print(f"        {info.get('browser_download_url','')}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            print(f"      ✗ HTTP {exc.code}: {detail}")
            return 1

    # 汇总
    print("\n[3] 最终状态")
    rel = api(f"/repos/{REPO}/releases/tags/{TAG}")
    print(f"    标题: {rel['name']}")
    print(f"    标签: {rel['tag_name']}")
    print(f"    地址: {rel['html_url']}")
    print(f"    附件:")
    for a in rel.get("assets", []):
        print(f"      {a['name']:34s} {a['size']/1024/1024:7.1f} MB")

    print()
    print("=" * 66)
    print("  ✓ Release 发布完成")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
