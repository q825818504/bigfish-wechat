"""攒批判定自检 —— 不联网、不花钱，用假模型验解析与失败倒向。

跑法：
    python tools/selftest_listen_judge.py

守四件事：
  1. 摆给模型看的文本要带「距上一条几秒」（分条发的人看得更准）
  2. 模型输出包了 ```json 也能解析
  3. more=true / false 都要如实返回
  4. 任何失败（模型没配好 / 抛异常 / 输出看不懂）→ **按"说完了"处理**

第 4 条是这个模块最重要的约定：判定失败绝不能变成"继续等"，
否则一次网络抖动就能让回复卡到 30 秒上限。
"""
from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, __file__.rsplit("\\", 2)[0])

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \u2713 {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  \u2717 {name}  {detail}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


class FakeCfg:
    """冒充 ModelConfig：只需要 ready。"""

    ready = True
    timeout = 6.0
    max_tokens = 2600


def main() -> int:
    print("=" * 64)
    print("攒批判定自检（假模型，不联网）")
    print("=" * 64)

    from bigfish import listen_judge as LJ
    from bigfish import replier as R

    section("摆给模型看的样子")
    text = LJ.render_lines([
        {"text": "在吗", "gap": 0},
        {"text": "问你个事", "gap": 3.5},
    ], peer="小明")
    check("带序号与说话人", "1. 小明：在吗" in text, text)
    check("带间隔", "3.5 秒" in text, text)
    check("空正文不出现", LJ.render_lines([{"text": "  "}]).strip() == "")

    section("解析：包了代码块也能读")
    check("纯 JSON", bool(LJ._loose_json('{"more": true}')), "None")
    check("包 ```json",
          (LJ._loose_json('```json\n{"more": false, "why": "说完了"}\n```') or {})
          .get("more") is False, "parse failed")
    check("前后带话",
          (LJ._loose_json('好的，结果如下 {"more": true} 就这样') or {})
          .get("more") is True, "parse failed")
    check("不是 JSON", LJ._loose_json("我觉得说完了") is None)
    check("空串", LJ._loose_json("") is None)

    section("判定：more / why")
    real = R.call_model

    def fake(text):
        R.call_model = lambda *a, **k: text  # noqa: ARG005
        try:
            return LJ.judge_more([{"text": "在吗"}], peer="小明", cfg=FakeCfg())
        finally:
            R.call_model = real

    more, why = fake('{"more": true, "why": "只是开场"}')
    check("more=true 如实返回", more is True, f"{more} {why}")
    check("why 带回来", "开场" in why, why)

    more, why = fake('{"more": false, "why": "句号收尾"}')
    check("more=false 如实返回", more is False, f"{more} {why}")

    more, why = fake('```json\n{"more": true}\n```')
    check("代码块里的 JSON 也认", more is True, f"{more} {why}")

    section("失败一律倒向「说完了」")
    check("输出不是 JSON", fake("我觉得他还没说完")[0] is False, "should be False")

    def boom(*a, **k):  # noqa: ARG001
        raise R.ModelError("上游 500", kind="http")

    R.call_model = boom
    try:
        more, why = LJ.judge_more([{"text": "在吗"}], cfg=FakeCfg())
    finally:
        R.call_model = real
    check("模型抛错 → False", more is False, f"{more}")
    check("模型抛错 → 说明原因", "判定失败" in why, why)

    class NotReady:
        ready = False

    more, why = LJ.judge_more([{"text": "在吗"}], cfg=NotReady())
    check("模型没配好 → False", more is False, f"{more}")
    check("模型没配好 → 说明原因", "没配好" in why, why)

    more, why = LJ.judge_more([], cfg=FakeCfg())
    check("没有正文 → False", more is False, f"{more}")

    print()
    if FAIL:
        print(f"结果：{PASS}/{PASS + FAIL} 通过，{FAIL} 项失败")
        for n in FAILED:
            print(f"  · {n}")
        return 1
    print(f"结果：{PASS}/{PASS} 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
