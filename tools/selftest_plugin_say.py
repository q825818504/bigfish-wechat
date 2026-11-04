"""「说法」插件自检：注册、配置、注入、生效。

跑法：
    python tools/selftest_plugin_say.py

不碰微信、不碰数据目录（用临时 root / monkeypatch），不改你的 plugins.json。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import bigfish.plugins as P  # noqa: E402
from bigfish.app import AppConfig, BigFishApp  # noqa: E402
from bigfish.plugins.say import _render  # noqa: E402

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


# ----------------------------------------------------------------------
class FakeReader:
    def __init__(self) -> None:
        self._db = types.SimpleNamespace(wxid="me", account="t")
        self.rows: list = []

    def messages(self, chat: str, limit: int = 20):
        return self.rows[-limit:]


class FakeSender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def send(self, who: str, text: str):
        self.calls.append((who, text))
        return types.SimpleNamespace(ok=True, detail="")


class FakeSoul:
    name = "大肥鱼"
    address = "爸"


def test_register() -> None:
    section("注册")
    P.ensure_loaded()
    inv = P.inventory()
    ids = [p["id"] for p in inv["plugins"]]
    check("say 已登记", "say" in ids, f"{ids}")
    item = next(p for p in inv["plugins"] if p["id"] == "say")
    check("scope=prompt", item["scope"] == "prompt", f"{item['scope']}")
    check("座位 890", item["order"] == 890, f"{item['order']}")
    check("默认启用", item["enabled"] is True, f"{item['enabled']}")
    check("座位表含 890 说法", 890 in P.PLUGIN_SEATS, f"{P.PLUGIN_SEATS}")
    check("850 仍不可占", 850 in P.BUILTIN_SEATS, f"{P.BUILTIN_SEATS}")


def test_config() -> None:
    section("配置")
    cfg = P.config_of("say")
    expect = {
        "max_parts": 4, "part_max_chars": 50, "per_char": 0.09,
        "cap": 3.5, "total_cap": 12.0, "quote_protect": True,
        "async_confirm": True, "strict_confirm": False,
    }
    check("默认值齐全", cfg == expect, f"{cfg}")

    root = tempfile.mkdtemp()
    try:
        ok, saved, err = P.set_config("say", {"max_parts": 6, "per_char": 0.2}, root=root)
        check("set_config 成功", ok, err)
        check("max_parts 写入", saved.get("max_parts") == 6, f"{saved}")
        check("per_char 写入", abs(float(saved.get("per_char")) - 0.2) < 1e-9, f"{saved}")

        ok, saved, _ = P.set_config("say", {"max_parts": 99}, root=root)
        check("越界被夹到上限 6", saved.get("max_parts") == 6, f"{saved}")

        ok, saved, _ = P.set_config("say", {"不存在的字段": 1}, root=root)
        check("未知字段被丢弃", "不存在的字段" not in saved, f"{saved}")

        ok, saved, _ = P.set_config("say", {"quote_protect": "不是布尔"}, root=root)
        check("布尔字段按真值归一", saved.get("quote_protect") is True, f"{saved}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_render() -> None:
    section("注入")
    text = _render({"config": {}})
    check("含分条标记", "{{split}}" in text, text[:60])
    check("含条数", "最多 4 条" in text, text[:80])
    check("含字数上限", "超过 50 字" in text, text[:80])
    check("不写「首先」", "首先" in text and "不用" in text, text[:80])

    text2 = _render({"config": {"max_parts": 2, "part_max_chars": 30}})
    check("随配置变化", "最多 2 条" in text2 and "超过 30 字" in text2, text2[:80])

    # 关掉插件后不应出现在注入列表里
    root = tempfile.mkdtemp()
    try:
        P.set_enabled("say", False, root=root)
        sections = P.prompt_sections({}, root=root)
        titles = [s[1] for s in sections]
        check("关掉后不注入", "说法" not in titles, f"{titles}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_app_override() -> None:
    section("面板改了，app 跟着变")
    app = BigFishApp(FakeReader(), FakeSender(), FakeSoul(), object(), AppConfig())
    app.build_prompt = lambda conv, msg, body: ("SYS", "USER")  # type: ignore[assignment]

    orig_cfg = P.config_of
    orig_on = P.is_enabled
    try:
        P.is_enabled = lambda pid, root=None: True  # type: ignore[assignment]
        P.config_of = lambda pid, root=None: {      # type: ignore[assignment]
            "max_parts": 2, "part_max_chars": 30, "per_char": 0.2,
            "cap": 4.0, "total_cap": 9.0,
            "quote_protect": False, "async_confirm": False, "strict_confirm": True,
        }
        app._refresh_say_config()
    finally:
        P.config_of = orig_cfg
        P.is_enabled = orig_on

    check("say_max_parts=2", app.cfg.say_max_parts == 2, f"{app.cfg.say_max_parts}")
    check("say_part_chars=30", app.cfg.say_part_chars == 30, f"{app.cfg.say_part_chars}")
    check("strict_confirm=True", app.cfg.strict_confirm is True, f"{app.cfg.strict_confirm}")
    check("per_char=0.2", abs(app._say_per_char - 0.2) < 1e-9, f"{app._say_per_char}")
    check("cap=4.0", abs(app._say_cap - 4.0) < 1e-9, f"{app._say_cap}")
    check("total_cap=9.0", abs(app._say_total_cap - 9.0) < 1e-9, f"{app._say_total_cap}")
    check("quote_protect=False", app._say_quote_protect is False, f"{app._say_quote_protect}")
    check("async_confirm=False", app._say_async_confirm is False, f"{app._say_async_confirm}")


def main() -> int:
    print("=" * 66)
    print("「说法」插件自检（注册 / 配置 / 注入 / 面板生效）")
    print("=" * 66)

    test_register()
    test_config()
    test_render()
    test_app_override()

    print("\n" + "=" * 66)
    if FAIL:
        print(f"结果：{PASS}/{PASS + FAIL} 通过，{FAIL} 项失败")
        for name in FAILED:
            print(f"  \u00b7 {name}")
        return 1
    print(f"结果：{PASS}/{PASS + FAIL} 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
