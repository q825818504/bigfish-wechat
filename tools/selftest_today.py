"""今日桥接的边界：跨天日程、真实日期、预算。

这是回归测试，不是演示数据：
  · life.day 停在昨天时，day/weekday 仍要是真实今天；
  · 昨天的 plan 不能被当成「今天没排」丢掉；
  · get_mood 的预算用 policy 真算，不是界面随手写死。

不写盘、不调模型。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bigfish.desktop as D  # noqa: E402
from bigfish import afterglow, bridge_client, journal, life, senses, vault  # noqa: E402


class FakeLife:
    day = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    asleep = True
    energy = 6
    doing = "昨晚没写完的那页"
    plan_day = day
    plan = [{"at": "14:00", "what": "去超市", "done": False,
             "from": "self", "shareable": True}]
    day_trace = []
    wake_at = 0
    bedtime = 0
    sleep_debt = 0
    asleep_since = 0


class FakeMood:
    value = 72
    label = "心情不错"
    last_reason = "测试"
    injected_text = "测试注入"
    dims = {"happy": 46, "calm": 62}
    updated_at = 0


def main() -> int:
    old_life = life.load
    old_mood = bridge_client.load_mood_only
    old_weather = senses.cached_weather
    old_senses = senses.load
    old_days = journal.list_days
    old_read = journal.read
    old_path = journal.path_for
    old_after_load = afterglow.load
    old_after_active = afterglow.active
    old_after_summary = afterglow.summary
    old_after_effect = afterglow.current_effect
    old_cfg = vault.read_mood_config
    try:
        life.load = lambda: FakeLife()
        bridge_client.load_mood_only = lambda: FakeMood()
        senses.cached_weather = lambda now=None: "18°C，多云"
        senses.load = lambda: {"weather": {"city": "杭州"}}
        journal.list_days = lambda limit=7: []
        journal.read = lambda day: ""
        journal.path_for = lambda day: ""
        afterglow.load = lambda: {}
        afterglow.active = lambda state, now=None: []
        afterglow.summary = lambda now=None: ""
        afterglow.current_effect = lambda now=None: {}
        vault.read_mood_config = lambda: {"moodBase": 70}

        class Dummy:
            _recent = []

        today = json.loads(D.Backend.get_today(Dummy()))
        assert today["day"] == dt.date.today().isoformat(), today["day"]
        assert today["life_day"] == FakeLife.day, today["life_day"]
        assert len(today["plan"]) == 1, today["plan"]
        assert today["life"]["asleep"] is True

        mood = json.loads(D.Backend.get_mood(Dummy()))
        b = mood["budget"]
        assert b["parts"] >= 1 and b["chars"] >= 8, b
        assert 0 <= b["silence"] <= 0.6, b
        print("通过：day/life_day 分离，昨日 plan 保留，预算有真实值")
        return 0
    finally:
        life.load = old_life
        bridge_client.load_mood_only = old_mood
        senses.cached_weather = old_weather
        senses.load = old_senses
        journal.list_days = old_days
        journal.read = old_read
        journal.path_for = old_path
        afterglow.load = old_after_load
        afterglow.active = old_after_active
        afterglow.summary = old_after_summary
        afterglow.current_effect = old_after_effect
        vault.read_mood_config = old_cfg


if __name__ == "__main__":
    raise SystemExit(main())
