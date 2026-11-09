"""心情注入自检：五个旋钮真的有人读吗。

跑法：
    python tools/selftest_mood.py

为什么有这个文件
----------------
0.11.0 及以前，心情页上的 `injectMode` / `injectConfidence` / `sigMood` /
`sigDim` / `autoJudge` 只有**写**没有**读** —— 用户调了没反应。
更糟的是 `lastInjectedText` 只在初始化时被写成空串，之后没人写，
于是 `desktop` 的「这会儿的状态」永远拿到空字符串：
心情能改表达预算，却从来没有一句话真的进过提示词。

这个文件把「注入文本怎么来」逐档钉住：
  · off  一个字都不注入，连上一条也撤掉
  · label 只报现在什么心情
  · event 只报值得说的变化（心情动 sigMood 格 / 情绪动 sigDim 格）
  · 置信度低于 injectConfidence 不注入
  · 每天最多换 injectPerDay 次说法，超了撤掉上一条
  · 自动判定可以被 autoJudge 关掉

只用临时目录，不碰真实数据。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

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


BIG = {"moodDelta": -9, "dims": {"sad": 8}, "reason": "他说要走", "confidence": 90}
SMALL = {"moodDelta": 0, "dims": {"happy": 1}, "reason": "随便聊聊", "confidence": 90}
LOWCONF = {"moodDelta": -9, "dims": {"sad": 8}, "reason": "瞎猜", "confidence": 10}


def run_case(MJ, V, cfg, verdict, times=1):
    """清状态 → 写配置 → 判定 N 次 → 返回落盘的那份 slot。"""
    p = V.mood_path(V.resolve_data_root())
    try:
        os.remove(p)
    except Exception:
        pass
    ok, _cfg, err = V.write_mood_config(cfg)
    if not ok:
        raise AssertionError(f"写心情配置失败：{err}")
    for _ in range(times):
        MJ.apply_mood_delta(verdict)
    with open(p, "r", encoding="utf-8") as f:
        d = json.load(f)
    return d["moods"][d["lastActive"]]


def main() -> int:
    print("=" * 66)
    print("心情注入自检")
    print("=" * 66)

    tmp = tempfile.mkdtemp(prefix="bf-moodtest-")
    old_env = os.environ.get("BIGFISH_DATA_ROOT")
    os.environ["BIGFISH_DATA_ROOT"] = tmp
    try:
        from bigfish import mood_judge as MJ
        from bigfish import vault as V

        section("标签：只有一份阈值")
        check("95 → 心情很好", MJ.label_of(95) == "心情很好", MJ.label_of(95))
        check("70 → 心情不错", MJ.label_of(70) == "心情不错", MJ.label_of(70))
        check("39 → 情绪低", MJ.label_of(39) == "情绪低", MJ.label_of(39))
        check("None → 空串", MJ.label_of(None) == "", repr(MJ.label_of(None)))
        from bigfish import bridge_client as BC

        mismatch = [v for v in (None, 0, 24, 25, 39, 40, 54, 55, 69, 70, 84, 85, 100)
                    if MJ.label_of(v) != BC.Mood(value=v).label]
        check("bridge_client 复用同一套阈值", not mismatch, str(mismatch))

        section("event：只报值得说的变化")
        s = run_case(MJ, V, {"injectMode": "event"}, BIG)
        check("大变化会注入", bool(s.get("lastInjectedText")),
              repr(s.get("lastInjectedText")))
        check("注入文本带数值与原因",
              "他说要走" in (s.get("lastInjectedText") or "")
              and "/100" in (s.get("lastInjectedText") or ""),
              repr(s.get("lastInjectedText")))
        s = run_case(MJ, V, {"injectMode": "event"}, SMALL)
        check("小变化不注入", s.get("lastInjectedText") == "",
              repr(s.get("lastInjectedText")))

        section("label：只报心情，不看幅度")
        s = run_case(MJ, V, {"injectMode": "label"}, SMALL)
        check("label 模式小变化也注入", bool(s.get("lastInjectedText")),
              repr(s.get("lastInjectedText")))
        check("label 模式不带原因", "随便聊聊" not in (s.get("lastInjectedText") or ""),
              repr(s.get("lastInjectedText")))

        section("off：一个字都不注入")
        s = run_case(MJ, V, {"injectMode": "off"}, BIG)
        check("off 模式大变化也不注入", s.get("lastInjectedText") == "",
              repr(s.get("lastInjectedText")))
        # off 要连上一条一起撤掉：先 event 注入，再 off 跑一次
        p = V.mood_path(V.resolve_data_root())
        try:
            os.remove(p)
        except Exception:
            pass
        V.write_mood_config({"injectMode": "event"})
        MJ.apply_mood_delta(BIG)
        V.write_mood_config({"injectMode": "off"})
        MJ.apply_mood_delta(SMALL)
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        s = d["moods"][d["lastActive"]]
        check("off 会撤掉上一条注入", s.get("lastInjectedText") == "",
              repr(s.get("lastInjectedText")))

        section("阈值：置信度 / sigMood / sigDim")
        s = run_case(MJ, V, {"injectMode": "event", "injectConfidence": 40}, LOWCONF)
        check("置信度不够不注入", s.get("lastInjectedText") == "",
              repr(s.get("lastInjectedText")))
        s = run_case(MJ, V, {"injectMode": "event", "sigMood": 10, "sigDim": 100}, BIG)
        check("两个阈值都够不着就不注入", s.get("lastInjectedText") == "",
              repr(s.get("lastInjectedText")))
        s = run_case(MJ, V, {"injectMode": "event", "sigMood": 1, "sigDim": 100},
                     {"moodDelta": -3, "dims": {}, "reason": "", "confidence": 90})
        check("sigMood 放低后小变化也注入", bool(s.get("lastInjectedText")),
              repr(s.get("lastInjectedText")))

        section("每日上限")
        s = run_case(MJ, V, {"injectMode": "event", "injectPerDay": 1}, BIG, times=3)
        check("超过每日上限后撤掉上一条", s.get("lastInjectedText") == "",
              repr(s.get("lastInjectedText")))
        s = run_case(MJ, V, {"injectMode": "event", "injectPerDay": 0}, BIG)
        check("上限 0 = 不注入", s.get("lastInjectedText") == "",
              repr(s.get("lastInjectedText")))

        section("维度：增益让强度落在能看出来的区间")
        # 2026-11-09 实测：单次 delta 3~5 直接累加，一天下来七根维度长期
        # 停在 0~10，界面上像"几乎没有情绪"。★3 之后落在 30~50。
        s = run_case(MJ, V, {"injectMode": "off"},
                     {"moodDelta": 0, "dims": {"sad": 8, "worry": 4},
                      "reason": "", "confidence": 90})
        check("sad 8 → 24（×DIM_GAIN）", int(s["dims"]["sad"]) == 24,
              str(s["dims"]))
        check("worry 4 → 12", int(s["dims"]["worry"]) == 12, str(s["dims"]))
        check("没提到的维度保持 0", int(s["dims"].get("happy") or 0) == 0,
              str(s["dims"]))
        # 封顶仍然有效：连续多次判定不会超过 100
        s = run_case(MJ, V, {"injectMode": "off"},
                     {"moodDelta": 0, "dims": {"sad": 10},
                      "reason": "", "confidence": 90}, times=6)
        check("维度封顶 100", int(s["dims"]["sad"]) == 100, str(s["dims"]))

        section("autoJudge：闸门在 desktop 里")
        src = os.path.join(_ROOT, "bigfish", "desktop.py")
        with open(src, "r", encoding="utf-8") as f:
            text = f.read()
        i = text.find("def _judge_mood_async")
        body = text[i:i + 2000] if i >= 0 else ""
        check("_judge_mood_async 读 autoJudge", "autoJudge" in body,
              "没找到闸门" if not body else "闸门缺失")
        check("闸门在起线程之前",
              body.find("autoJudge") < body.find("import threading"),
              "顺序不对：先起了线程才判开关")
    finally:
        if old_env is None:
            os.environ.pop("BIGFISH_DATA_ROOT", None)
        else:
            os.environ["BIGFISH_DATA_ROOT"] = old_env
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 66)
    if FAIL:
        print(f"结果：{PASS}/{PASS + FAIL} 通过，{FAIL} 项失败")
        print("失败项：")
        for name in FAILED:
            print(f"  \u00b7 {name}")
        print("=" * 66)
        return 1
    print(f"结果：{PASS}/{PASS} 全部通过")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        if _s is not None:
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    raise SystemExit(main())
