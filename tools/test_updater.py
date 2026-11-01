"""自动更新链路测试 —— 用本地 HTTP 服务模拟 GitHub Releases。

验证什么：
  1. check_latest 能不能正确解析 Release JSON
  2. 版本比较（有新版 / 无新版）
  3. 下载 + SHA256 校验（含校验失败的情况）
  4. schedule_replace 生成的 updater.cmd 内容是否正确

不验证什么：
  实际的「等进程退出 → 替换 → 重启」这一段需要真实进程配合，
  这里只检查脚本逻辑，不真正执行替换。
"""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import socket
import sys
import threading
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bigfish import updater  # noqa: E402

HR = "=" * 68
TMP = os.path.join(ROOT, ".tmp", "updtest")
os.makedirs(TMP, exist_ok=True)

# 假的「新版安装包」
FAKE_PAYLOAD = b"FAKE INSTALLER PAYLOAD " * 1000
FAKE_SHA = hashlib.sha256(FAKE_PAYLOAD).hexdigest()


def section(t):
    print()
    print(HR)
    print(t)
    print(HR)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        # 路径形如 /repos/test/repo/releases/latest
        if self.path.rstrip("/").endswith("/releases/latest"):
            body = json.dumps({
                "tag_name": "v0.2.0",
                "name": "0.2.0",
                "body": "测试版本\n- 修复若干问题",
                "html_url": "https://example.com/release",
                "published_at": "2026-11-01T10:00:00Z",
                "assets": [{
                    "name": "BigFish-0.2.0-setup.exe",
                    "browser_download_url":
                        f"http://127.0.0.1:{PORT}/file/setup.exe",
                    "size": len(FAKE_PAYLOAD),
                    "digest": f"sha256:{FAKE_SHA}",
                }],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/file/setup.exe"):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(FAKE_PAYLOAD)))
            self.end_headers()
            self.wfile.write(FAKE_PAYLOAD)
        else:
            self.send_response(404)
            self.end_headers()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT = free_port()


def main() -> int:
    ok = 0
    fail = 0

    def check(label, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  [OK  ] {label}")
        else:
            fail += 1
            print(f"  [FAIL] {label}  {extra}")

    # ── 1. 版本比较 ──
    section("1. 版本比较")
    cases = [
        ("1.2.3", "1.2.2", True),
        ("1.2.3", "1.2.3", False),
        ("v0.2.0", "0.1.0", True),
        ("0.1.0", "0.1.0", False),
        ("1.0.0-beta", "1.0.0", False),
        ("2.0", "1.99.99", True),
    ]
    for remote, local, expect in cases:
        check(f"{remote} vs {local} -> {expect}",
              updater.is_newer(remote, local) == expect)

    # ── 2. 起本地服务 ──
    section("2. 模拟 GitHub API")
    srv = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    print(f"  服务地址 http://127.0.0.1:{PORT}")
    time.sleep(0.3)

    # 把 GITHUB_API 指到本地
    orig_api = updater.GITHUB_API
    updater.GITHUB_API = f"http://127.0.0.1:{PORT}/repos/{{repo}}/releases/latest"

    try:
        # ── 3. 检查更新 ──
        section("3. 检查更新")
        info = updater.check_latest("test/repo", "0.1.0", TMP)
        check("识别到有新版", info.get("has_update") is True, str(info))
        check("版本号 0.2.0", info.get("latest") == "0.2.0", info.get("latest"))
        check("有 1 个 asset", len(info.get("assets") or []) == 1)
        a = (info.get("assets") or [{}])[0]
        check("asset 名正确", a.get("name", "").endswith(".exe"))
        check("sha256 已解析", a.get("sha256") == FAKE_SHA)

        # 同版本不该报更新
        info2 = updater.check_latest("test/repo", "0.2.0", TMP)
        check("同版本不报更新", info2.get("has_update") is False)

        # ── 4. 下载 ──
        section("4. 下载与校验")
        dest = os.path.join(TMP, "setup.exe")
        if os.path.isfile(dest):
            os.unlink(dest)
        updater.download(a["url"], dest, a["sha256"])
        check("文件已下载", os.path.isfile(dest))
        check("内容一致",
              open(dest, "rb").read() == FAKE_PAYLOAD)

        # 错误 SHA 应该被拒
        bad = os.path.join(TMP, "bad.exe")
        try:
            updater.download(a["url"], bad, "0" * 64)
            check("错误 SHA 被拒绝", False, "没有抛异常")
        except RuntimeError as exc:
            check("错误 SHA 被拒绝", "不匹配" in str(exc))
            check("坏文件已删除", not os.path.isfile(bad))

        # ── 5. 替换脚本 ──
        section("5. 替换脚本生成")
        target = os.path.join(TMP, "BigFish.exe")
        with open(target, "wb") as f:
            f.write(b"OLD VERSION")
        script = updater.schedule_replace(dest, target,
                                          os.path.join(TMP, "upd.log"))
        check("脚本已生成", os.path.isfile(script))
        body = open(script, "r", encoding="utf-8", errors="replace").read()
        check("含等待进程结束", "WAIT_PID" in body or str(os.getpid()) in body)
        check("含备份步骤", ".old" in body)
        check("含重启", "start" in body.lower())
        # 不真正执行替换，避免把测试目录搞乱
        print(f"  脚本路径: {script}")
        print("  （不执行，避免真实替换）")

    finally:
        updater.GITHUB_API = orig_api
        srv.shutdown()

    section("结果")
    print(f"  通过 {ok} / 失败 {fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
