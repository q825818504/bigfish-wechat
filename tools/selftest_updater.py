"""检查更新的回归测试。

跑法：
    python tools/selftest_updater.py

不联网 —— `_get_json` 用桩替换。真联网的那次见 `--live`。

覆盖：
  · 版本比较：v 前缀、预发布、跨段
  · 仓库名归一化：完整 URL / .git / 尾斜杠 / 多余路径
  · check_latest：正常、无新版、304 缓存、404
  · Updater：disabled、repo 为空时回落默认
  · pick_asset：优先 exe 再 zip
  · 配置里没写 repo 也能检查（这是这次修的核心）
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bigfish import updater as U  # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []


def _pick_marks() -> tuple[str, str]:
    try:
        "\u2713".encode(sys.stdout.encoding or "utf-8")
        return "\u2713", "\u2717"
    except (UnicodeEncodeError, LookupError):
        return "[OK]", "[X]"


MARK_OK, MARK_FAIL = _pick_marks()


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  {MARK_OK} {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  {MARK_FAIL} {name}  {detail}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


# ══════════════════════════════════════════════════════════════════
def test_versions() -> None:
    section("版本比较")
    cases = [
        ("v0.5.0", "0.4.1", True),
        ("v0.4.0", "0.4.1", False),
        ("0.4.1", "0.4.1", False),
        ("v1.0.0", "0.9.9", True),
        ("v0.2.0-beta", "0.2.0", False),
        ("v0.2.1", "0.2.0-beta", True),
        ("0.10.0", "0.9.0", True),      # 不能按字符串比
        ("v0.4.1", "v0.4.1", False),
    ]
    for remote, local, want in cases:
        got = U.is_newer(remote, local)
        check(f"{remote} > {local} ? {want}", got == want, f"got={got}")

    check("parse_version 补零", U.parse_version("1.2") == (1, 2, 0, 1, ""),
          str(U.parse_version("1.2")))
    check("空串当 0", U.parse_version("") == (0,), str(U.parse_version("")))


def test_repo() -> None:
    section("仓库名归一化")
    cases = [
        ("https://github.com/a/b", "a/b"),
        ("https://github.com/a/b.git", "a/b"),
        ("http://github.com/a/b/", "a/b"),
        ("github.com/a/b", "a/b"),
        ("git@github.com:a/b.git", "a/b"),
        ("a/b/", "a/b"),
        ("a/b/releases", "a/b"),
        ("  a/b  ", "a/b"),
        ("", ""),
    ]
    for raw, want in cases:
        got = U.normalize_repo(raw)
        check(f"{raw!r} → {want!r}", got == want, f"got={got!r}")

    check("默认仓库非空", bool(U.DEFAULT_REPO), U.DEFAULT_REPO)
    check("默认仓库是 owner/repo 形态",
          U.DEFAULT_REPO.count("/") == 1, U.DEFAULT_REPO)


def _stub(payload, calls=None):
    """替换 _get_json，返回固定 payload。"""
    def fake(url, timeout=15.0, etag_file=""):
        if calls is not None:
            calls.append(url)
        if isinstance(payload, Exception):
            raise payload
        return payload
    return fake


def test_check_latest() -> None:
    section("check_latest（桩，不联网）")
    orig = U._get_json

    release = {
        "tag_name": "v0.5.0",
        "name": "0.5.0 模型接入",
        "body": "这一版做模型接入。",
        "html_url": "https://github.com/a/b/releases/tag/v0.5.0",
        "published_at": "2026-11-05T10:00:00Z",
        "assets": [
            {"name": "BigFish-0.5.0-manifest.json",
             "browser_download_url": "https://x/manifest.json", "size": 10},
            {"name": "BigFish-0.5.0-portable.zip",
             "browser_download_url": "https://x/pkg.zip", "size": 100,
             "digest": "sha256:abc123"},
        ],
    }
    try:
        calls = []
        U._get_json = _stub(release, calls)
        info = U.check_latest("a/b", "0.4.1", "")
        check("有新版", info["has_update"] is True, str(info.get("latest")))
        check("最新版本号去掉 v", info["latest"] == "0.5.0", info["latest"])
        check("带上当前版本", info["current"] == "0.4.1")
        check("带上仓库", info["repo"] == "a/b", info.get("repo"))
        check("解析出 2 个附件", len(info["assets"]) == 2, str(info["assets"]))
        check("sha256 剥掉前缀",
              info["assets"][1]["sha256"] == "abc123",
              info["assets"][1]["sha256"])
        check("带上发布说明", "模型接入" in info["notes"])
        check("请求的是 /releases/latest",
              calls and calls[0].endswith("/releases/latest"), str(calls))

        # 同版本 → 没更新
        U._get_json = _stub({**release, "tag_name": "v0.4.1"})
        info = U.check_latest("a/b", "0.4.1", "")
        check("同版本 → 无新版", info["has_update"] is False)

        # 旧版本 → 没更新（本地更新）
        U._get_json = _stub({**release, "tag_name": "v0.4.0"})
        info = U.check_latest("a/b", "0.4.1", "")
        check("线上更旧 → 无新版", info["has_update"] is False)

        # 304 → cached
        U._get_json = _stub(None)
        info = U.check_latest("a/b", "0.4.1", "")
        check("304 走缓存分支", info.get("cached") is True, str(info))
        check("304 时 latest = current",
              info["latest"] == "0.4.1", info.get("latest"))

        # 空 repo 回落默认
        U._get_json = _stub(release, [])
        calls2 = []
        U._get_json = _stub(release, calls2)
        U.check_latest("", "0.4.1", "")
        check("空 repo 用默认仓库",
              U.DEFAULT_REPO in (calls2[0] if calls2 else ""),
              str(calls2[:1]))
    finally:
        U._get_json = orig


def test_updater() -> None:
    section("Updater 封装")
    import tempfile

    tmp = tempfile.mkdtemp()
    up = U.Updater("", "0.4.1", tmp)
    check("空 repo 构造时回落默认", up.repo == U.DEFAULT_REPO, up.repo)

    up2 = U.Updater("https://github.com/a/b.git", "0.4.1", tmp)
    check("构造时归一化", up2.repo == "a/b", up2.repo)

    up3 = U.Updater("a/b", "0.4.1", tmp, enabled=False)
    info = up3.check()
    check("disabled 时不发请求", info.get("disabled") is True, str(info))

    # pick_asset
    assets = {"assets": [
        {"name": "x-manifest.json", "url": "u1"},
        {"name": "x-portable.zip", "url": "u2"},
        {"name": "x-setup.exe", "url": "u3"},
    ]}
    a = up3.pick_asset(assets)
    check("优先选 exe", a and a["name"].endswith(".exe"), str(a))

    only_zip = {"assets": [{"name": "x.zip", "url": "u"}]}
    a = up3.pick_asset(only_zip)
    check("没有 exe 选 zip", a and a["name"] == "x.zip", str(a))

    check("都没有返回 None", up3.pick_asset({"assets": []}) is None)


def test_read_config() -> None:
    section("读配置")
    import json
    import tempfile

    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "config.json")

    with open(p, "w", encoding="utf-8") as f:
        json.dump({"update": {"repo": "https://github.com/a/b", "enabled": True}},
                  f)
    uc = U.read_update_config(p)
    check("读到 repo", uc.get("repo", "").endswith("a/b"), str(uc))

    with open(p, "w", encoding="utf-8") as f:
        json.dump({}, f)
    uc = U.read_update_config(p)
    check("没有 update 段 → 空 dict", uc == {}, str(uc))

    check("文件不存在 → 空 dict",
          U.read_update_config(os.path.join(tmp, "nope.json")) == {})


def test_cache_and_stale() -> None:
    section("缓存与降级")
    import json
    import tempfile
    import time

    tmp = tempfile.mkdtemp()
    last_path = os.path.join(tmp, "last.json")

    # 造一份"上次查到的"
    last = {
        "has_update": True, "latest": "0.6.0", "current": "0.5.0",
        "repo": U.DEFAULT_REPO, "published": "2026-11-10T10:00:00Z",
        "assets": [{"name": "x.zip", "url": "u", "size": 1, "sha256": ""}],
        "_at": time.time() - U.CHECK_TTL - 10,
    }
    with open(last_path, "w", encoding="utf-8") as f:
        json.dump(last, f)

    orig = U._get_json
    try:
        # 限额用完 → 回退上次结果，并标记 stale
        U._get_json = _stub(RuntimeError(
            "GitHub 查询次数用完了（每小时 60 次），约 21 分钟后恢复。"))
        info = U.check_latest(U.DEFAULT_REPO, "0.5.0", tmp)
        check("查询失败时回退上次结果", info.get("latest") == "0.6.0",
              str(info.get("latest")))
        check("标记 stale", info.get("stale") is True)
        check("带上失败原因", "60 次" in str(info.get("stale_reason")),
              str(info.get("stale_reason")))
        check("附件也保留", bool(info.get("assets")))

        # 缓存新鲜 → 根本不发请求
        last["_at"] = time.time()
        with open(last_path, "w", encoding="utf-8") as f:
            json.dump(last, f)
        calls = []
        U._get_json = _stub({"tag_name": "v9.9.9"}, calls)
        info = U.check_latest(U.DEFAULT_REPO, "0.5.0", tmp)
        check("新鲜缓存不发请求", calls == [], f"calls={calls}")
        check("走缓存时标 cached", info.get("cached") is True)
        check("缓存里是上次的号", info.get("latest") == "0.6.0",
              str(info.get("latest")))

        # 没有缓存 + 查询失败 → 必须抛，不能假装没事
        tmp2 = tempfile.mkdtemp()
        U._get_json = _stub(RuntimeError("网络不可达"))
        try:
            U.check_latest(U.DEFAULT_REPO, "0.5.0", tmp2)
            check("无缓存时失败要抛出", False, "没抛")
        except RuntimeError as exc:
            check("无缓存时失败要抛出", "网络" in str(exc), str(exc))
    finally:
        U._get_json = orig


def test_rate_limit_msg() -> None:
    section("限流提示（不打网络）")
    import inspect

    src = inspect.getsource(U._get_json)
    check("403 单独处理", "exc.code == 403" in src)
    check("识别 X-RateLimit-Remaining", "X-RateLimit-Remaining" in src)
    check("提示里说清 60 次", "每小时 60 次" in src)
    check("提示里给恢复时间", "分钟后恢复" in src)


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    print("检查更新回归测试（bigfish/updater.py）")
    test_versions()
    test_repo()
    test_check_latest()
    test_updater()
    test_read_config()
    test_cache_and_stale()
    test_rate_limit_msg()

    print(f"\n{'=' * 46}")
    total = PASS + FAIL
    if FAIL:
        print(f"失败 {FAIL}/{total}")
        for n in FAILED:
            print(f"  - {n}")
        return 1
    print(f"全部通过 {PASS}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
