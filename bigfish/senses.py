"""[bigfish] 她注意到的：天气、日子、以及你提过但还没到的事。

═══════════════════════════════════════════════════════════════════════
为什么需要
═══════════════════════════════════════════════════════════════════════

到 v0.9.7 为止，她能"感知"到的只有：现在几点、星期几、你说了什么、
她自己睡没睡。

于是她的关心永远是抽象的："怎么了""别太累""早点睡"——
不是不想具体，是**没有具体的东西**。真人说"今天降温了，你穿够没"，
不是因为更关心，是因为她真的看到了窗外。

这一层补三样最便宜、收益最高的：

    天气     一个 HTTP，免 key
    日子     公历节日 + 用户手填的重要日期
    近期事件  从已有 facts 里抽"下周三面试"这类带时间的句子

═══════════════════════════════════════════════════════════════════════
两条硬约束
═══════════════════════════════════════════════════════════════════════

1. **不在拼 prompt 的时候联网。**
   `_compose` 跑在引擎线程上，一次 4 秒超时就是"她突然不说话了"。
   所以这里分两个口：
       render(now)        只读缓存，永不阻塞
       maybe_refresh(now) 后台线程，联网更新缓存
   缓存过期也照用旧值 —— 昨天的天气比"没有天气"好，比卡住更好。

2. **不加依赖。**
   打包版是 PyInstaller，加一个库要改打包脚本、加体积、还可能和
   微信那边的环境打架。天气用 Open-Meteo（免 key、无需注册），
   日历只做公历节日 + 手填日期 —— 农历节日不做，
   因为准确的农历要天文算法，做错比不做更糟
   （把中秋算错一天，她会提前一天说"中秋快乐"）。

═══════════════════════════════════════════════════════════════════════
为什么天气要缓存 30 分钟
═══════════════════════════════════════════════════════════════════════

她一天可能看几十轮。每轮查一次天气 = 几十个请求，而且 IP 会被限流。
30 分钟是"温度不会变太多、但一天里能看出早晚差"的量级。

地理编码（城市名 → 经纬度）缓存 30 天：城市不会搬家。

═══════════════════════════════════════════════════════════════════════
只在有话说的时候出现
═══════════════════════════════════════════════════════════════════════

`render` 的输出很短（40~80 字），而且：

· 没有天气、没有节日、没有近期事件 → 返回空串，这一段完全不出现；
· 节日只在当天出现，不提前一周开始念叨；
· 近期事件只在 0~3 天内出现（再远就是"日程"，不是"注意到"）。

宁可少说一句。她不是天气播报员。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

STATE_NAME = "senses.json"
STATE_VERSION = 1

#: 天气缓存多久（秒）
WEATHER_TTL = 30 * 60
#: 地理编码缓存多久（秒）
GEO_TTL = 30 * 24 * 3600
#: 联网超时（秒）。短一点：这是加分项，不值得等。
HTTP_TIMEOUT = 4.0

#: 公历固定节日。农历节日（春节/端午/中秋）不做 —— 见模块说明。
FIXED_DAYS = {
    (1, 1): "元旦",
    (2, 14): "情人节",
    (3, 8): "妇女节",
    (5, 1): "劳动节",
    (6, 1): "儿童节",
    (10, 1): "国庆节",
    (12, 24): "平安夜",
    (12, 25): "圣诞节",
}

#: WMO 天气码 → 中文。只留日常会用到的，其余归"多云"。
WMO = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "有雾", 48: "雾凇",
    51: "小雨", 53: "小雨", 55: "雨",
    56: "冻雨", 57: "冻雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "阵雨", 82: "强阵雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "雷阵雨伴冰雹",
}


def state_path(root: Optional[str] = None) -> str:
    from bigfish.vault import resolve_data_root

    return os.path.join(root or resolve_data_root(), STATE_NAME)


def load(root: Optional[str] = None) -> Dict[str, Any]:
    try:
        with open(state_path(root), "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {"version": STATE_VERSION, "geo": {}, "weather": {}}
    if not isinstance(raw, dict):
        return {"version": STATE_VERSION, "geo": {}, "weather": {}}
    raw.setdefault("version", STATE_VERSION)
    if not isinstance(raw.get("geo"), dict):
        raw["geo"] = {}
    if not isinstance(raw.get("weather"), dict):
        raw["weather"] = {}
    return raw


def save(state: Dict[str, Any], root: Optional[str] = None) -> bool:
    try:
        from bigfish.vault import _atomic_write_json

        path = state_path(root)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        _atomic_write_json(path, state)
        return True
    except Exception:
        return False


def _now(now: Optional[float]) -> float:
    return float(now if now is not None else time.time())


def _city() -> str:
    """城市从「生活」插件的配置里读。

    为什么放在那里而不是新开一个「感知」页：
    它和"现在几点/醒着没"是同一类事 —— 她所处的环境。
    用户已经在那个页面里配过一次，不该为了一个城市名再开一个页面。
    """
    try:
        from bigfish import plugins as P

        P.ensure_loaded()
        return str(P.config_of("life").get("weather_city") or "").strip()
    except Exception:
        return ""


def _http_json(url: str) -> Optional[Dict[str, Any]]:
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "bigfish/1.0 (+local)"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _geocode(city: str, state: Dict[str, Any], now: float) -> Optional[Dict[str, float]]:
    """城市名 → 经纬度。缓存 30 天。"""
    hit = state.get("geo", {}).get(city)
    if isinstance(hit, dict):
        try:
            if now - float(hit.get("at") or 0) < GEO_TTL:
                return {"lat": float(hit["lat"]), "lon": float(hit["lon"])}
        except (KeyError, TypeError, ValueError):
            pass
    url = ("https://geocoding-api.open-meteo.com/v1/search?"
           + urllib.parse.urlencode({
               "name": city, "count": 1, "language": "zh", "format": "json"}))
    data = _http_json(url)
    results = (data or {}).get("results") or []
    if not results:
        return None
    first = results[0]
    try:
        lat, lon = float(first["latitude"]), float(first["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    state.setdefault("geo", {})[city] = {"lat": lat, "lon": lon, "at": now}
    return {"lat": lat, "lon": lon}


def _weather_text(data: Dict[str, Any]) -> str:
    try:
        cur = data.get("current") or {}
        daily = data.get("daily") or {}
        temp = cur.get("temperature_2m")
        code = int(cur.get("weather_code") or 0)
        label = WMO.get(code, "多云")
        bits = []
        if temp is not None:
            bits.append(f"{round(float(temp))}°C")
        bits.append(label)
        try:
            lo = (daily.get("temperature_2m_min") or [None])[0]
            hi = (daily.get("temperature_2m_max") or [None])[0]
            if lo is not None and hi is not None:
                bits.append(f"{round(float(lo))}~{round(float(hi))}°C")
        except (IndexError, TypeError, ValueError):
            pass
        try:
            pr = (daily.get("precipitation_probability_max") or [None])[0]
            if pr is not None and float(pr) >= 30:
                bits.append(f"降水概率 {round(float(pr))}%")
        except (IndexError, TypeError, ValueError):
            pass
        return "，".join(bits)
    except Exception:
        return ""


def weather_ready() -> bool:
    """天气这一块是否可用（配了城市）。

    城市是用户所在地，不能猜、不能给默认值 —— 猜错比没有更糟：
    她会说"今天下雨"，而你在另一个半球。
    但"没配就永远不提天气"这件事必须让用户看见一次，
    否则他只会觉得"天气功能没做"，而不是"我少填了一个字段"。
    """
    return bool(_city())


def refresh(now: Optional[float] = None, *,
            city: Optional[str] = None,
            root: Optional[str] = None) -> str:
    """联网更新天气缓存。返回新的一句（失败返回空串）。

    只在后台线程调。超时 4 秒，任何失败都静默 ——
    天气是加分项，不是聊天的前提。
    """
    t = _now(now)
    name = (city if city is not None else _city()).strip()
    if not name:
        return ""
    state = load(root)
    cached = state.get("weather") or {}
    try:
        if (cached.get("city") == name
                and t - float(cached.get("at") or 0) < WEATHER_TTL):
            return str(cached.get("line") or "")
    except (TypeError, ValueError):
        pass

    geo = _geocode(name, state, t)
    if not geo:
        save(state, root)
        return ""
    url = ("https://api.open-meteo.com/v1/forecast?"
           + urllib.parse.urlencode({
               "latitude": geo["lat"], "longitude": geo["lon"],
               "current": "temperature_2m,weather_code",
               "daily": ("temperature_2m_max,temperature_2m_min,"
                         "precipitation_probability_max"),
               "timezone": "auto", "forecast_days": 1}))
    data = _http_json(url)
    if not data:
        save(state, root)
        return ""
    line = _weather_text(data)
    if not line:
        save(state, root)
        return ""
    state["weather"] = {"city": name, "at": t, "line": line}
    save(state, root)
    return line


def cached_weather(now: Optional[float] = None, *,
                   root: Optional[str] = None) -> str:
    """只读缓存，永不联网。过期也用旧值 —— 昨天的天气比没有好。"""
    state = load(root)
    cached = state.get("weather") or {}
    line = str(cached.get("line") or "").strip()
    if not line:
        return ""
    try:
        age = _now(now) - float(cached.get("at") or 0)
    except (TypeError, ValueError):
        return line
    # 超过一天就不可信了：昨天的 12°C 今天说出来是错的。
    if age > 24 * 3600:
        return ""
    return line


def weather_line(now: Optional[float] = None, *,
                 root: Optional[str] = None) -> str:
    """给别的模块用的一句天气（日程生成也用它）。不联网。"""
    return cached_weather(now, root=root)


def calendar_line(now: Optional[float] = None) -> str:
    """今天是什么日子。节日当天才返回，不提前念叨。"""
    t = _now(now)
    try:
        import datetime as _dt

        d = _dt.datetime.fromtimestamp(t)
    except Exception:
        return ""
    name = FIXED_DAYS.get((d.month, d.day), "")
    if not name:
        return ""
    return f"今天是{name}。"


def upcoming_from_facts(facts: List[str], *, now: Optional[float] = None,
                        horizon_days: int = 3) -> List[str]:
    """从"你认定的事"里抽出 0~3 天内要发生的事。

    只认绝对日期（X月X日）。"下周""过几天"这类相对说法不抽 ——
    没有基准日就会算错，算错还不如不抽。
    """
    t = _now(now)
    try:
        import datetime as _dt

        today = _dt.datetime.fromtimestamp(t).date()
    except Exception:
        return []
    out: List[str] = []
    seen = set()
    for raw in facts or []:
        text = " ".join(str(raw or "").split())
        if not text:
            continue
        for m in _ABS_DATE.finditer(text):
            try:
                month, day = int(m.group(1)), int(m.group(2))
            except (TypeError, ValueError):
                continue
            try:
                when = today.replace(month=month, day=day)
            except ValueError:
                continue
            delta = (when - today).days
            if 0 <= delta <= horizon_days:
                # 取这条事实的前 24 字当由头，不改写
                hint = text[:24]
                if hint in seen:
                    continue
                seen.add(hint)
                out.append(f"{when.month}月{when.day}日：{hint}")
                break
    return out[:2]


#: 绝对日期：`11月10日` / `11月10号`。相对说法（下周/过几天）不抽 ——
#: 没有基准日就会算错，算错还不如不抽。
_ABS_DATE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")


def render(*, now: Optional[float] = None, facts: Optional[List[str]] = None,
           root: Optional[str] = None) -> str:
    """拼成一段注入文本。没有内容就返回空串（这一段不出现）。"""
    t = _now(now)
    bits: List[str] = []

    weather = cached_weather(t, root=root)
    if weather:
        city = str((load(root).get("weather") or {}).get("city") or "")
        bits.append(f"{city}现在 {weather}。" if city else f"现在 {weather}。")

    cal = calendar_line(t)
    if cal:
        bits.append(cal)

    for line in upcoming_from_facts(facts or [], now=t):
        bits.append(f"你记着 {line}。")

    if not bits:
        return ""
    return ("## 你注意到的\n\n" + "\n".join(f"- {b}" for b in bits)
            + "\n\n（这些是你自己看到/想到的，不是他告诉你的。"
              "没被问到就顺口提一句，不要当成天气预报来播报。）")


def maybe_refresh(now: Optional[float] = None, *,
                  root: Optional[str] = None) -> None:
    """后台刷新入口。频率由调用方控制（world 每 30 分钟一次）。"""
    try:
        refresh(now, root=root)
    except Exception:
        pass
