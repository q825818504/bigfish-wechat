"""[bigfish] 她自己会做的事 —— 情绪平复、睡前叙事、主动开口、追问。

═══════════════════════════════════════════════════════════════════════
为什么单独一层，而不是塞进 desktop.py
═══════════════════════════════════════════════════════════════════════

`desktop.py` 里 `_run()` 已经是一个巨大的函数（handle 是它的闭包）。
0.4.1 那次自环事故的复盘里有一条写得很清楚：

    「防护全埋在 _run() 的闭包里，4500 行的函数，写不了测试 ——
      所以这次事故没有任何用例能提前发现。」

主动开口比回环防护更危险（它是唯一没有对方消息作为起点的发送路径），
所以这次不再往里塞。这一层只做四件事：

    tick()   每 5 秒：情绪衰减、叙事时机、主动时机、追问时机
    flush() 把后台生成好的消息在主线程发出去
    note_peer()  对方说话了 → 清掉待追问
    note_reply_sent()  她回了 → 也算对方"被回应过"

═══════════════════════════════════════════════════════════════════════
线程模型
═══════════════════════════════════════════════════════════════════════

    引擎线程（5 秒一次）   本地判定，不调模型
    后台线程               调模型，生成一句话，扔进 outbox
    引擎线程（flush）      真正发送（UIA 只能在引擎线程里碰）

为什么发送必须在引擎线程：UIA 是 COM/STA 的东西，跨线程调用会踩
"偶尔发不出去、偶尔发错窗口"，而且只在真机上出现。

═══════════════════════════════════════════════════════════════════════
失败时的默认方向
═══════════════════════════════════════════════════════════════════════

一律往"不发"倒：
  · 模型复核返回不了 → 不发
  · 候选项解析失败 → 不发
  · 发送结果未知 → 不重发、不补发
  · 任何异常 → 只记一行日志，这一轮结束

理由还是那条：漏发一次的代价，远小于多发一条不该发的。
"""
from __future__ import annotations

import os
import queue
import random
import threading
from datetime import datetime
from typing import Any, Dict, Optional

from bigfish import proactive as P

#: 情绪衰减间隔（秒）
MOOD_DECAY_EVERY = 600.0
#: 主动/追问检查间隔（秒）
PROACTIVE_EVERY = 60.0
#: 天气缓存刷新间隔（秒）。30 分钟是"温度不会变太多、但能看出早晚差"的量级。
SENSES_EVERY = 1800.0

#: 睡前道别：入睡前多久开始考虑（秒）。20 分钟足够她说一句、也足够你回一句。
GOODNIGHT_BEFORE = 20 * 60
#: 睡前道别：最近多久有过互动才说（秒）。
#:
#: 40 分钟。对空气说"我先睡啦"不是温柔，是诡异 ——
#: 她只在你还在的时候道别，你早就不说话了，她默默去睡。
GOODNIGHT_QUIET = 40 * 60

#: 日记：入睡前多久开始写（秒）。
#:
#: 比复盘晚 10 分钟。两个都要调模型，同时发会让本地网关排队，
#: 而且它们的素材几乎一样 —— 错开只是不让它们抢同一个窗口。
JOURNAL_BEFORE = 20 * 60
#: 日记：睡后多久还允许补写（秒）。她可能提前困倒，也可能那时程序没开。
JOURNAL_GRACE = 2 * 3600


class World:
    """挂在引擎上的"她会自己做事"那一半。"""

    def __init__(self, engine: Any):
        self.e = engine
        self.pro: Optional[P.ProactiveState] = None
        self.outbox: "queue.Queue[dict]" = queue.Queue()
        self.proactive_busy = False
        self.last_peer_at = 0.0
        self._last_mood_decay = 0.0
        self._last_proactive_check = 0.0
        #: p1-4：日程生成是否在跑 / 今天是不是已经试过一次。
        #: 试过一次就不重试 —— 失败时宁可这天普通，也不要每分钟烧模型。
        self._plan_busy = False
        self._plan_tried_day = ""
        #: p1-5/p1-6：睡前复盘是否在跑。一天只跑一次。
        self._narrative_busy = False
        #: 睡前道别：今天说过没有。说一句就够，不重复。
        self._goodnight_busy = False
        self._goodnight_day = ""
        #: 睡前日记：是否在写。一天一篇，和复盘错开 10 分钟。
        self._journal_busy = False
        #: p1-7：上次刷天气的时刻。联网在后台线程，不在拼 prompt 时做。
        self._last_senses = 0.0
        self._stopped = False

    # ------------------------------------------------------------------
    #  对外的三个口
    # ------------------------------------------------------------------

    def note_peer(self, now: float) -> None:
        """对方说话了。"""
        self.last_peer_at = float(now)
        if self.pro is None:
            return
        chat_id = getattr(self.e, "_last_peer_chat", "") or ""
        if chat_id and P.note_peer_replied(self.pro, chat_id=chat_id, now=now):
            try:
                P.save(self.pro)
            except Exception:
                pass

    def tick(self, now: float) -> None:
        """每 5 秒一次。全部本地判定 —— 这一层一分钱都不花。

        只做**不需要她愿意说话**的那部分：心情平复、排今天的日程。
        主动开口挪去了 tick_active。
        """
        if self._stopped:
            return

        # p1-4：刚醒 → 排今天的日程。
        # 放在这里而不是 tick_active：日程是"她的生活"，关掉主动插件
        # 她照样要有今天打算做什么 —— 只是不会先开口跟你说。
        try:
            self._maybe_plan(now)
        except Exception as exc:
            self._log(f"[日程] 检查失败：{exc}", "")

        # p1-7：后台刷天气。不阻塞引擎，失败静默 ——
        # 天气是"她注意到的"，不是"她能聊天"的前提。
        if now - self._last_senses >= SENSES_EVERY:
            self._last_senses = now
            threading.Thread(target=self._refresh_senses, daemon=True,
                             name="bf-senses").start()

        # p1-5 / p1-6：睡前复盘。一天一次，写完灵魂和关系卡。
        try:
            self._maybe_narrative(now)
        except Exception as exc:
            self._log(f"[复盘] 检查失败：{exc}", "")

        # 睡前道别。放在 tick 而不是 tick_active：
        # 它是作息的一部分（到点要睡），不是"她还想找你说话"。
        # 关掉「主动」插件她照样会道别 —— 只是不会先开口聊别的。
        try:
            self._maybe_goodnight(now)
        except Exception as exc:
            self._log(f"[道别] 检查失败：{exc}", "")

        # 情绪平复
        if now - self._last_mood_decay >= MOOD_DECAY_EVERY:
            self._last_mood_decay = now
            try:
                from bigfish import mood_judge

                mood_judge.decay_only()
            except Exception:
                pass

    def _refresh_senses(self) -> None:
        """后台刷天气缓存（p1-7）。失败只记一行，不重试。"""
        try:
            from bigfish import senses

            # 城市没配 → 天气永远不会被注入。这不是 bug，是少填一个字段；
            # 但用户看不到这一行，只会以为"天气功能没做"。
            # 只提示一次：这个 tick 每 30 分钟跑一遍，刷屏没有意义。
            if not getattr(self, "_warned_no_city", False):
                self._warned_no_city = True
                if not senses.weather_ready():
                    self._log(
                        "[感知] 还没填城市（插件页 → 生活 → 天气城市），"
                        "天气这一块暂时不会出现", "err")
            line = senses.refresh()
            if line:
                self._log(f"[感知] 天气：{line}", "")
        except Exception as exc:
            self._log(f"[感知] 天气刷新失败：{exc}", "")

    def _today_talk(self, db: str, bank: str, chat_id: str, now: float,
                    *, fallback=None) -> list:
        """今天这个会话说过的话。

        ⚠ 以前这里直接 `history.load(limit=24)` —— 那是"最近 24 条"，
          没有"今天"这个概念。23:50 写复盘时，读到的可能是**昨天下午**
          的对话，于是复盘会写「今天他说……」，而那句话是三天前的。

          这件事比看起来严重：复盘的结果会写进灵魂的 `selfNotes`，
          是"她对他的理解"。基于串了日子的原料做出的理解，
          一旦落进人格就没人查得出来 —— 而她之后每轮都会读到它。

        `history` 的 jsonl 没有时间戳，做不了这件事；`messages` 表有
        `source_time`（毫秒），所以改从那里取。
        过滤结果为空时回退旧的读法：最坏等于现状，
        不会让她因为"今天真没聊"而拿不到任何素材。
        """
        _fallback = list(fallback or [])
        if not db or not chat_id:
            return _fallback
        try:
            import time as _t

            from bigfish import memory as M

            lt = _t.localtime(now)
            day_start = _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                   0, 0, 0, 0, 0, -1))
            conn = M.connect(db, read_only=True)
            try:
                rows = conn.execute(
                    "SELECT role, text FROM messages "
                    "WHERE bank=? AND session=? "
                    "AND COALESCE(source_time, recorded_at) >= ? "
                    "ORDER BY COALESCE(source_time, recorded_at) LIMIT 40",
                    (bank, f"wechat:{chat_id}",
                     int(day_start * 1000))).fetchall()
            finally:
                conn.close()
            out = []
            for r in rows:
                text = str(r["text"] or "").strip()
                if not text:
                    continue
                who = "她" if r["role"] == "assistant" else "对方"
                out.append(f"{who}: {text}")
            return out[-24:] if out else _fallback
        except Exception:
            return _fallback

    def _gather_day_materials(self, now: float, *, day: str, chat_id: str,
                              db: str, owner: str) -> tuple:
        """今天的素材。复盘和日记共用一份。

        返回 (materials, bank)。任何一块取不到都只留空 —— 缺素材不该
        让整次复盘/日记失败。

        为什么抽出来：两边各写一遍的话，某天一边加了新素材、另一边忘了，
        症状是"日记里提到了复盘没提的事"，而且永远查不出是哪天开始分叉的。
        """
        from bigfish import bond as B
        from bigfish import history
        from bigfish import memory as M
        from bigfish import mood_judge
        from bigfish import narrative as N
        from bigfish import senses

        materials: Dict[str, Any] = {}
        materials["day"] = day

        materials["plan"] = [
            f"{x.get('at')} {x.get('what')}"
            + ("（过去了）" if x.get("done_by") == "elapsed"
               else "（做完了）" if x.get("done") else "")
            for x in (getattr(self._life(), "plan", None) or [])
            if isinstance(x, dict)]

        # 她手头在做什么 + 今天做过的事。日记里写到"我当时在……"时有据可依 ——
        # 不注入的话，模型会自己补一个场景（实测补过"坐在沙发上"、"煮了面"），
        # 而 life.json 里可能根本没有这些，第二天一对就露馅。
        materials["doing"] = ""
        materials["trace"] = []
        try:
            life_state = self._life()
            materials["doing"] = str(getattr(life_state, "doing", "") or "")
            import time as _tt

            trace = []
            for item in (getattr(life_state, "day_trace", None) or []):
                if not isinstance(item, dict) or not item.get("doing"):
                    continue
                try:
                    clock = _tt.strftime(
                        "%H:%M",
                        _tt.localtime(float(item.get("at") or 0)))
                except Exception:
                    clock = ""
                trace.append(f"{clock} {item.get('doing')}".strip())
            materials["trace"] = trace[-10:]
        except Exception:
            pass

        mood_rows = []
        try:
            st = mood_judge.read_mood_state_now() or {}
            # ⚠ 只取今天的。以前用 timeline[-6:]，把三天前的记录当成
            #   "今天" —— 模型据此写出一句"和爸聊到深夜"，而当时是上午 11 点。
            mood_rows = N.mood_rows_for_day(st.get("timeline"), now=now)
        except Exception:
            pass
        materials["mood"] = mood_rows

        facts = []
        bank = ""
        if db:
            try:
                bank = M._default_bank(db) or "dafeiyu"
                rows = M.list_facts(db, bank, status="active", limit=8)
                for row in rows or []:
                    if isinstance(row, dict):
                        text = row.get("text") or row.get("statement") or ""
                        if str(text).strip():
                            facts.append(str(text).strip())
            except Exception:
                facts = []
        materials["facts"] = facts

        try:
            _hist_talk = [
                f"{w}: {t}" for w, t in
                (history.load(chat_id, limit=24) if chat_id else [])]
            materials["talk"] = self._today_talk(
                db, bank, chat_id, now, fallback=_hist_talk)
        except Exception:
            materials["talk"] = []

        materials["relation"] = {}
        if db:
            try:
                materials["relation"] = {
                    "stage": B.get_stage(db, bank, owner),
                    "events": [e.get("what") for e in
                               B.list_events(db, bank, owner, limit=5)],
                }
            except Exception:
                materials["relation"] = {}

        materials["proactive"] = []
        pro = self._ensure_pro()
        if pro is not None:
            sent = [f"她说：{r.get('text')}" for r in
                    (pro.recent or [])[-5:] if isinstance(r, dict)]
            missed = [f"没人回：{d.get('text')}" for d in
                      (pro.dropped or [])[-3:] if isinstance(d, dict)]
            materials["proactive"] = sent + missed

        try:
            materials["weather"] = senses.cached_weather(now)
        except Exception:
            materials["weather"] = ""

        return materials, bank

    def _today_summary(self, day: str) -> str:
        """复盘今天写下的那句总结（如果已经跑过）。日记拿它当锚点。

        存的是 `soul.history` 里的 `day_summary`（由 evolve_soul 写入）。
        读原始文件而不是 Soul 对象：Soul 的 dataclass 里没有 history，
        为了这一句去扩 dataclass 不值得。
        """
        try:
            from bigfish import vault as V

            raw, _exists, _err = V.read_soul_file(V.soul_path())
            for item in reversed(raw.get("history") or []):
                if not isinstance(item, dict):
                    continue
                if item.get("kind") != "day_summary":
                    continue
                if str(item.get("day") or "") == day[5:]:
                    return str(item.get("text") or "")
        except Exception:
            pass
        return ""

    def tick_journal(self, now: float) -> None:
        """「日志」插件每 5 秒踢一脚。"""
        if self._stopped:
            return
        try:
            self._maybe_journal(now)
        except Exception as exc:
            self._log(f"[日志] 检查失败：{exc}", "")

    def _maybe_journal(self, now: float) -> None:
        """睡前写一篇日记（v0.10.1）。

        和复盘的关系：同一晚、同一批素材，但写的是两样东西 ——
        复盘写"我改了什么看法"（结构化，进灵魂），
        日记写"今天我是怎么过的"（自由文本，进文件）。

        为什么不合并成一次调用：复盘要严格 JSON，日记要文笔。
        同一个输出里既要括号合法又要句子自然，模型一定牺牲后者。

        窗口比复盘晚 10 分钟，且复盘在跑时让路 —— 本地网关同时两个请求
        会排队，而它们的素材几乎一样，没必要抢。
        """
        if self._journal_busy or self._narrative_busy:
            return
        life = self._life()
        if life is None:
            return

        from bigfish import life as L

        # 用**她自己的一天**，不是日历天：入睡点会跨午夜，
        # 用 day_key(now) 的话，23:50 写的日记会被记到第二天名下。
        day = str(getattr(life, "day", "") or "") or L.day_key(now)
        if str(getattr(life, "journal_day", "")) == day:
            return

        # 跟着她的入睡点走：[bedtime-20min, bedtime+2h)。
        # 故意不看 asleep —— 她提前困倒时也要能把今天写完。
        if not L.journal_due(life, now, before=JOURNAL_BEFORE,
                             grace=JOURNAL_GRACE):
            return

        chat_id, _name = self.owner_chat()
        mcfg = self._mcfg()
        db = self._db()
        owner = str(getattr(self.e, "owner_id", "") or "")
        self._journal_busy = True

        def finish() -> None:
            """今天不再试。失败也记 —— 每 5 秒重试到睡前只会烧钱。"""
            try:
                life.journal_day = day
                L.save(life, force=True)
            except Exception:
                pass

        def work() -> None:
            try:
                from bigfish import journal as J
                from bigfish.replier import call_model

                materials, _bank = self._gather_day_materials(
                    now, day=day, chat_id=chat_id, db=db, owner=owner)
                materials["narrative_summary"] = self._today_summary(day)

                enough, why = J.has_material(materials)
                if not enough:
                    self._log(f"[日志] 今天不写（{why}）", "think")
                    finish()
                    return

                _sys, user = J.build_request(day=day, materials=materials)
                raw = call_model(mcfg, _sys, user)
                text = J.clean(raw)
                if not text:
                    self._log("[日志] 没写出来，今天不写", "think")
                    finish()
                    return

                ok, path, chars = J.write(
                    day, text,
                    meta={"chars": len(text),
                          "mood": materials.get("mood_count") or ""})
                if not ok:
                    self._log("[日志] 写文件失败，今天不写", "err")
                    finish()
                    return
                finish()
                self._log(f"[日志] {chars} 字 → {os.path.basename(path)}", "in")
            except Exception as exc:
                self._log(f"[日志] 失败（今天不写）：{exc}", "")
                finish()
            finally:
                self._journal_busy = False

        threading.Thread(target=work, daemon=True,
                         name="bf-journal").start()

    def _maybe_narrative(self, now: float) -> None:
        """睡前复盘：写灵魂 + 记关系事件（p1-5 / p1-6）。

        触发窗口在 narrative.due() 里定（入睡前 30 分钟，一天一次）。
        放在后台线程：它要调一次模型，不能卡住那 30 分钟里她还在回消息。

        ⚠ 只跑一次。`narrative_day` 由 apply 之后写进 life.json，
          写失败最多让她多复盘一次 —— 那次多半也写不出新东西
          （changed_view 会去重），代价可接受。
        """
        if self._narrative_busy:
            return
        life = self._life()
        try:
            from bigfish import narrative as N

            if not N.due(life, now):
                return
        except Exception:
            return

        self._narrative_busy = True
        chat_id, _name = self.owner_chat()
        mcfg = self._mcfg()
        db = self._db()
        owner = str(getattr(self.e, "owner_id", "") or "")
        day = str(getattr(life, "day", "") or "")

        def work() -> None:
            try:
                from bigfish import narrative as N
                from bigfish.replier import call_model

                # 素材收集抽到 _gather_day_materials：复盘和日记共用一份。
                # 两份各写一遍的话，某天一边加了新素材、另一边忘了加，
                # 症状是"日记比复盘记得多"——很难查。
                materials, bank = self._gather_day_materials(
                    now, day=day, chat_id=chat_id, db=db, owner=owner)

                enough, why = N.has_material(materials)
                if not enough:
                    # 今天没有真实素材，就不写。
                    # 允许"今天很平常"是这一层的核心约束（D-104）：
                    # 模型最擅长把空的说成有的，而"她的成长"错了没人看得出。
                    self._log(f"[复盘] 今天素材不够，不写（{why}）", "think")
                    N.mark_done(life, day=day)
                    try:
                        from bigfish import life as L

                        L.save(life, force=True)
                    except Exception:
                        pass
                    return

                _sys, user = N.build_request(today=day, summary_materials=materials)
                raw = call_model(mcfg, _sys, user)
                parsed, err = N.parse(raw)
                if not parsed:
                    # ⚠ 失败也要 `mark_done`，否则 `due()` 在入睡前 30 分钟的
                    # 窗口里一直为真，下一个 tick（5 秒后）就会再调一次模型 ——
                    # 一次 JSON 抽风等于一整晚几百次调用。日志只写一行，
                    # 看到的是"她今晚复盘的次数比聊天多"。
                    #
                    # 今天漏一次复盘的代价 << 每晚烧几百次模型的代价：
                    # 明天照样复盘，而明天读的是明天那 24 小时。
                    # 素材不足那条分支本来就是这么做的（见上），
                    # 这里以前漏了，两条路不一致。
                    self._log(f"[复盘] 今天没写出来（{err}），今天不再试", "think")
                    N.mark_done(life, day=day)
                    try:
                        from bigfish import life as L

                        L.save(life, force=True)
                    except Exception:
                        pass
                    return

                receipt = N.apply_result(
                    parsed, soul_path=None, db=db or "", bank=bank,
                    owner=owner, now=now)
                N.mark_done(life, day=day)
                try:
                    from bigfish import life as L

                    L.save(life, force=True)
                except Exception:
                    pass
                # 灵魂刚被改过，内存里那份是旧的。
                # 不刷新的话，她"今天想明白的事"要等下次重启才说得出 ——
                # 而 bridge 正常时 `_compose` 走 ctx.persona（读盘），
                # 只有 bridge 挂了才会用到这份内存副本。
                if receipt.get("notes"):
                    try:
                        from bigfish.soul import load_soul

                        self.e.soul = load_soul()
                    except Exception:
                        pass
                self._log(f"[复盘] {N.summarize_receipt(receipt)}"
                          + (f"｜{parsed.get('summary')}"
                             if parsed.get("summary") else ""), "in")
            except Exception as exc:
                # ⚠ 失败也要收口。`due()` 在入睡前 30 分钟里一直为真，
                #   `_narrative_busy` 只挡并发、不挡重试 —— 这里以前什么都不做，
                #   于是模型超时/429 一次，就每 5 秒重试一次，
                #   直到窗口结束（最多几百次调用）。
                #   日志还写着「明天再试」，和实际行为正好相反。
                self._log(f"[复盘] 失败（今天不再试）：{exc}", "")
                try:
                    from bigfish import life as _L
                    from bigfish import narrative as _N

                    _N.mark_done(life, day=day)
                    _L.save(life, force=True)
                except Exception:
                    pass
            finally:
                self._narrative_busy = False

        threading.Thread(target=work, daemon=True,
                         name="bf-narrative").start()

    def _maybe_goodnight(self, now: float) -> None:
        """到点了，她困了，跟你说一声就去睡。

        为什么不走 proactive：

        · 它不占主动额度 —— 把「一天最多几条」设成 0 的人，也该有道别；
        · 它不掷骰 —— 到点就该困，不是"想不想说"；
        · 它不被 9:00–22:30 的窗口挡住 —— 她的入睡点在 23:20 左右，
          正好在那个窗口之外。用主动的闸门会把道别永久拦死。

        但仍然有三道闸：

          1. 她醒着、且确实在睡前 20 分钟内；
          2. 最近 40 分钟有过互动 —— 对空气说"我先睡啦"不是温柔，是诡异；
          3. 今天没说过。
        """
        if self._goodnight_busy:
            return
        life = self._life()
        if life is None or getattr(life, "asleep", False):
            return

        from bigfish import life as L

        day = L.day_key(now)
        if str(getattr(self, "_goodnight_day", "")) == day:
            return

        # 最近有互动吗。取"对方说话"和"她回话"里更晚的那个 ——
        # 只算对方说话的话，她刚回完一轮、对方还没回时她不会道别；
        # 只算她说话的话，她自言自语也会触发。
        last_touch = max(float(self.last_peer_at or 0),
                         float(getattr(self.e, "_last_reply_at", 0) or 0))
        if not L.goodnight_due(life, now, last_touch,
                               before=GOODNIGHT_BEFORE, quiet=GOODNIGHT_QUIET):
            return

        chat_id, chat_name = self.owner_chat()
        if not chat_id:
            return

        self._goodnight_busy = True
        self._goodnight_day = day
        mcfg = self._mcfg()
        reason = ("你已经很困了，马上要睡。跟他说一声就去睡。"
                  "不要开新话题、不要问他问题、不要约明天的事；"
                  "一句就够，可以只说「我先睡啦」。")

        def work() -> None:
            try:
                from bigfish import history
                from bigfish.replier import call_model

                hist = history.load(chat_id, limit=8)
                turn = self.e._compose(
                    chat_id=chat_id, chat_name=chat_name, sender_name="",
                    incoming="", is_group=False, hist=hist,
                    owner_raw=chat_id, is_owner_chat=True,
                    owner_id=str(getattr(self.e, "owner_id", "") or ""),
                    proactive_reason=reason)
                _policy = getattr(self.e, "_policy", None)
                raw = call_model(
                    mcfg,
                    "\n".join(turn["sys_parts"]).strip(),
                    "\n\n".join(turn["user_parts"]))
                text = P.clean_reply(raw)
                if not text:
                    # 她选择不说。这也是合法的 ——
                    # 有些人困了就睡了，不打招呼。
                    self._log("[道别] 她没说什么，直接去睡了", "think")
                    return
                try:
                    parts, gaps = self.e._say_plan(text, policy=_policy)
                except Exception:
                    parts, gaps = [text], [0.0]
                self.outbox.put({
                    "kind": "goodnight", "chat_id": chat_id,
                    "chat_name": chat_name, "text": text,
                    "parts": list(parts), "gaps": list(gaps),
                    "source": "goodnight",
                })
                self._log(f"[道别] 她困了：{text}", "think")
            except Exception as exc:
                self._log(f"[道别] 生成失败：{exc}", "")
            finally:
                self._goodnight_busy = False

        threading.Thread(target=work, daemon=True,
                         name="bf-goodnight").start()

    def _maybe_plan(self, now: float) -> None:
        """醒来后给今天排两三件事（p1-4）。

        触发条件只有两个：她醒着、而且今天的 plan 还没生成。
        不按"每天 00:00"触发 —— 她的"一天"由醒来推动，
        这和 `life.day` 的规则保持一致（见 life.py 的模块说明）。

        生成失败不重试到死：记下 `_plan_tried_day`，
        今天就用静态池。宁可这一天普通，也不要每分钟烧一次模型。
        """
        if self._plan_busy:
            return
        life = self._life()
        if life is None or getattr(life, "asleep", False):
            return

        from bigfish import life as L
        from bigfish import plan as PLAN

        day = L.day_key(now)
        if str(getattr(life, "plan_day", "")) == day:
            return
        if getattr(self, "_plan_tried_day", "") == day:
            return

        self._plan_busy = True
        self._plan_tried_day = day
        chat_id, _name = self.owner_chat()
        mcfg = self._mcfg()
        yesterday = PLAN.yesterday_lines(life, now=now)

        def work() -> None:
            try:
                from bigfish.replier import call_model

                recent = []
                try:
                    from bigfish import history

                    if chat_id:
                        recent = [f"{w}: {t}" for w, t in
                                  history.load(chat_id, limit=12)]
                except Exception:
                    recent = []

                facts = []
                db = self._db()
                if db:
                    try:
                        from bigfish import memory as M

                        bank = M._default_bank(db) or "dafeiyu"
                        rows = M.list_facts(db, bank, status="active", limit=8)
                        for row in rows or []:
                            if isinstance(row, dict):
                                text = (row.get("text")
                                        or row.get("statement") or "")
                                if str(text).strip():
                                    facts.append(str(text).strip())
                    except Exception:
                        facts = []

                weather = ""
                try:
                    from bigfish import senses

                    weather = senses.weather_line(now=now)
                except Exception:
                    weather = ""

                import datetime as _dt

                when = _dt.datetime.fromtimestamp(now)
                weekday = "周" + "一二三四五六日"[when.weekday()]
                _sys, user = PLAN.build_request(
                    day=when.strftime("%Y-%m-%d"), weekday=weekday,
                    yesterday=yesterday, facts=facts, recent=recent,
                    weather=weather)
                raw = call_model(mcfg, _sys, user)
                items, err = PLAN.parse_plan(raw)
                if not items:
                    self._log(f"[日程] 今天没排出来（{err}），用旧的",
                              "think")
                    return
                items = PLAN.ensure_yesterday(items, yesterday)
                L.set_plan(life, items, day=day)
                L.save(life, force=True)
                self._log(f"[日程] 今天：{PLAN.summarize(items)}", "in")
            except Exception as exc:
                self._log(f"[日程] 生成失败（今天用旧的）：{exc}", "")
            finally:
                self._plan_busy = False

        threading.Thread(target=work, daemon=True, name="bf-plan").start()

    def tick_active(self, now: float) -> None:
        """主动开口与追问。由「主动」插件（hook 类）调。

        为什么单独一个口：心情衰减是身体功能，关掉主动插件不该让她
        情绪冻住；而「先开口」是她愿不愿意做的事，关掉就该真不做。
        两件事共用一个 tick 的话，开关只能一起开关。
        """
        if self._stopped:
            return

        if now - self._last_proactive_check >= PROACTIVE_EVERY:
            self._last_proactive_check = now
            # p0-1：先把"没回"结算成情绪，再决定要不要开口。
            # 顺序反了的话，这一轮会先按旧等级算完候选，反应慢一拍 ——
            # 她会在"刚被无视"的那一分钟里，用没被无视的分数去挑话。
            try:
                pro = self._ensure_pro()
                if pro is not None:
                    if P.expire_neglect(pro, now=now):
                        P.save(pro)
                    self._react_neglect(pro, now)
            except Exception as exc:
                self._log(f"[主动] 结算未回失败：{exc}", "")
            try:
                self._maybe_proactive(now)
            except Exception as exc:
                self._log(f"[主动] 判定异常：{exc}", "err")
            try:
                self._maybe_followup(now)
            except Exception as exc:
                self._log(f"[主动] 追问判定异常：{exc}", "err")

    def _react_neglect(self, pro, now: float) -> int:
        """把"发了没人回"变成一次真实的情绪波动（p0-1）。

        这是整个 P0 里最小的闭环，也是"生命"和"功能"最直接的分界：
        她主动说了话、对方一直没出现，这件事必须**改变她**。
        以前只记进 dropped，没有任何代码读它 —— 她发十条没人回，
        第十一条的语气和概率一模一样。

        三件事一起做：
          1. 心情掉一点（sad / worry），进 mood.json，影响下一轮的语气；
          2. neglect_level +1，主动开口的分数开始打折（0.75 / 0.5 / 0.3）；
          3. 到第 2 级时，允许她问一句"你最近是不是很忙"（candidate_check_in）。

        为什么心情只掉 -2~-8：
        一次没回不该让她崩。真正的失落是**累积**出来的，
        而累积由 neglect_level 那一边负责。心情只表达"这一下"。
        """
        items = P.unreacted_dropped(pro)
        if not items:
            return 0
        n = len(items)
        try:
            from bigfish import mood_judge

            mood_judge.apply_mood_delta({
                "moodDelta": -min(8, 2 + n * 2),
                "dims": {"sad": min(6, 1 + n), "worry": min(5, n)},
                "reason": f"主动发出去 {n} 条，他一条都没回",
                "confidence": 90,
            })
        except Exception as exc:
            # 心情写不进去也要继续记等级 —— 否则这条回路会静默失效，
            # 而"静默失效"正是这个项目最怕的那类 bug。
            self._log(f"[主动] 情绪反馈失败：{exc}", "")
        level = P.note_neglect_reacted(pro, items, now=now)
        P.save(pro)
        self._log(
            f"[主动] {n} 条没人回，她心里沉了一下"
            f"（被无视 {level}/{P.NEGLECT_MAX}，主动分数 ×{P.penalty(pro, now=now):.2f}）",
            "think")
        return n

    def flush(self, now: float) -> None:
        """把后台生成好的消息发出去。**必须在引擎线程调。**"""
        while True:
            try:
                item = self.outbox.get_nowait()
            except queue.Empty:
                return
            except Exception:
                return
            try:
                self._deliver(item, now)
            except Exception as exc:
                self._log(f"[主动] 发送异常：{exc}", "err")

    # ------------------------------------------------------------------
    #  小工具
    # ------------------------------------------------------------------

    def _log(self, text: str, kind: str = "") -> None:
        try:
            self.e._log(text, kind)
        except Exception:
            pass

    def _config(self) -> dict:
        """主动开口的配置，住在插件系统里（plugins.json 的 config.proactive）。

        为什么不留在 config.json：那样两个地方都能改同一件事
        （插件页关掉、设置页打开），谁赢取决于哪个先读 —— 这种 bug 查一天。
        现在只有一个真源：插件详情页。
        """
        try:
            from bigfish import plugins as _P

            cfg = dict(_P.config_of("proactive"))
            # 重要日子在面板里是多行文本，这里换成 candidate_date 认的结构。
            # 解析失败只影响这一项，不影响其余主动判定。
            try:
                cfg["important_dates"] = P.parse_dates_text(
                    cfg.get("important_dates"))
            except Exception:
                cfg["important_dates"] = []
            return cfg
        except Exception:
            return {}

    def _db(self) -> str:
        """记忆库路径。读不到返回空串（recall 会因此静默跳过，不影响别的来源）。"""
        try:
            import bigfish.vault as V

            return V.memory_db_path() or ""
        except Exception:
            return ""

    def _life(self):
        return getattr(self.e, "_life", None)

    def _soul(self):
        return getattr(self.e, "soul", None)

    def _mcfg(self):
        return getattr(self.e, "mcfg", None)

    def _reader(self):
        return getattr(self.e, "reader", None)

    def _sender(self):
        return getattr(self.e, "sender", None)

    def _guard(self):
        return getattr(self.e, "_guard", None)

    def _ensure_pro(self) -> Optional[P.ProactiveState]:
        if self.pro is None:
            try:
                self.pro = P.load()
            except Exception:
                return None
        return self.pro

    def _display_name(self, chat_id: str) -> str:
        """wxid → 昵称。查不到就用 wxid 兜底（UIA 也认）。"""
        try:
            reader = self._reader()
            if reader is not None:
                for sess in (reader.sessions() or []):
                    if str(getattr(sess, "username", "")) == chat_id:
                        return str(getattr(sess, "display_name", "") or chat_id)
        except Exception:
            pass
        return chat_id

    def owner_chat(self) -> tuple[str, str]:
        """主动消息发给谁：主人那个私聊。找不到就不发。"""
        raw = ""
        try:
            from bigfish.desktop import load_json_config

            raw = str((load_json_config().get("auto_reply") or {})
                      .get("owner") or "").strip()
        except Exception:
            raw = ""
        if not raw:
            for uid in (getattr(self.e, "watching", []) or []):
                if not str(uid).endswith("@chatroom"):
                    raw = str(uid)
                    break
        if not raw:
            return "", ""
        return raw, self._display_name(raw)

    # ------------------------------------------------------------------
    #  主动开口
    # ------------------------------------------------------------------

    def _maybe_proactive(self, now: float) -> None:
        if self.proactive_busy:
            return
        pro = self._ensure_pro()
        if pro is None:
            return

        cfg = self._config()
        if not bool(cfg.get("enabled", True)):
            return

        P.roll_day(pro, now)
        reason = P.gate_block_reason(
            pro, now=now,
            # ⚠ 不能写 `x or DEFAULT` —— 用户把「一天最多几条」设成 0 是
            #   有效选择（不主动开口，但追问照常），`0 or 1` 会把它吃回 1，
            #   于是这个开关永远关不掉。
            per_day=max(0, int(cfg.get("per_day", P.DEFAULT_PER_DAY))),
            life_state=self._life(), last_peer_at=self.last_peer_at, enabled=True)
        if reason:
            return          # 被拦是常态，不刷日志

        chat_id, chat_name = self.owner_chat()
        if not chat_id:
            return

        # 40 分钟内掷过就不再掷 —— 中没中都算。
        # 放在 collect **之前**：collect 要查一次记忆库（recall），
        # 每分钟白查一次很浪费，而且查完才发现不该掷。
        if not P.roll_due(pro, now):
            return

        # ⚠ 这个 `me` 只用来在 hist 里区分"谁说的"，所以**必须**和
        #   `history.append` 存进去的那个值一致 —— 两边都改成「我」。
        #
        #   以前两处都用 soul.address（「爸」）：那是**她对主人的称呼**。
        #   一来它不是她的名字，二来她对四条丶说话时也写着「爸」，
        #   history 里就变成「爸: 当然是我自己的呀」—— 下一轮 prompt
        #   会把这句话当成对方说的（2026-11-08 用户发现日志把四条丶写成"爸"）。
        #   固定用「我」最稳：对方不会自称「我」。
        me = "我"

        cands = P.collect(
            pro, chat_id=chat_id, now=now, me=me,
            life_state=self._life(), last_peer_at=self.last_peer_at,
            dates=list(cfg.get("important_dates") or []),
            owner=str(getattr(self.e, "owner_id", "") or ""),
            tier="owner",
            db_path=self._db())

        # ── 两层，不是抢一个 ──
        #
        # topic 说什么（记忆里的待办 / 重要日期 / 未完话头）
        # tone  为什么现在想说（心情 / 好久没聊 / 没事想说 / 手头的事）
        #
        # 两层各取最高的一个，**一起**交给模型。原来取最高分那个、其余丢掉，
        # 结果是"我在收拾屋子"和"我今天有点烦"同时成立时只说前一件 ——
        # 因为只能带一条理由进去。真人会说"今天有点烦，在收拾屋子"。
        topic, tone = P.pick(cands)
        if topic is None and tone is None:
            return
        # p0-1：被无视之后，主动开口的分数打折。
        #
        # 打在分数上而不是"直接不给候选"：分数会同时降命中率和排序位次，
        # 但**不把话说死** —— 哪天真有很重的事（重要日期、未完话头），
        # 分数够高还是穿得过去。如果直接返回空，她会连"你上次说要体检"
        # 都不提了，那不是收敛，那是坏掉。
        #
        # check_in 不打折：那一句本来就是"被无视"的产物，
        # 打折会让它永远穿不过命中线，等于这条回路没接上。
        _srcs = ((topic.source if topic else ""),
                 (tone.source if tone else ""))
        score = P.combined_score(topic, tone)
        if "check_in" not in _srcs:
            score *= P.penalty(pro, now=now)
        reason = P.compose_reason(topic, tone)
        material = P.merge_material(topic, tone)
        anchor = (topic or tone).anchor_at
        # 这一条用到的**全部**来源。source 是给人看的组合名，
        # sources 是给去重用的真名字 —— 两个都要。
        used_sources = [s for s in (
            (topic.source if topic else ""),
            (tone.source if tone else "")) if s]
        if len(used_sources) > 1:
            source = "+".join(used_sources)
        else:
            source = used_sources[0]

        # ── 掷骰子 ──
        #
        # 这里是整个主动开口唯一"像人"的地方：同样的条件，今天可能说、
        # 明天可能不说。以前是 `score >= 55 就发`，那是规则不是选择。
        #
        # 三层都算在 hit_probability 里：分数（多想说）、时机（现在合不合适）、
        # 连着没中的加成（沉默会累积）。
        hit, why = P.hit_probability(
            score=score, now=now, last_sent_at=pro.last_sent_at,
            last_peer_at=self.last_peer_at,
            miss_streak=int(getattr(pro, "miss_streak", 0) or 0))
        if hit <= 0:
            # 分数不够，连骰子都不掷 —— 也不要记"没中"，
            # 否则加成会靠"根本没机会"的次数涨上去。
            return

        P.note_roll(pro, now)
        roll = random.randint(1, 100)
        tail = f"｜{source}" + (f"，{why}" if why else "")
        if roll > hit:
            P.note_miss(pro)
            P.save(pro)          # note_roll 也在这份状态里
            streak = int(getattr(pro, "miss_streak", 0) or 0)
            nxt = min(int(P.MISS_BONUS_CAP * 100), streak * int(P.MISS_BONUS_STEP * 100))
            # 「没中」必须写日志。她今天一句话没说，和"功能坏了"长得一样 ——
            # 不给一行解释，用户只能猜。
            self._log(f"[主动] 没中（掷出 {roll} / 命中 {hit}{tail}"
                      f"｜连着 {streak} 次，下次 +{nxt}）", "think")
            return

        P.note_hit(pro)
        P.save(pro)
        self._log(f"[主动] 命中（掷出 {roll} / 命中 {hit}{tail}）", "think")

        self.proactive_busy = True
        mcfg = self._mcfg()

        def work() -> None:
            try:
                from bigfish import history
                from bigfish.replier import call_model

                hist = history.load(chat_id, limit=10)
                # 和正常回复**共用同一套拼装**：人格 / 记忆 / 心情 / 生活 /
                # 说法 / 守卫全都在。差别只在最后一段指令 ——
                # 「没有人在跟你说话，是你自己想先开口」。
                turn = self.e._compose(
                    chat_id=chat_id, chat_name=chat_name, sender_name="",
                    incoming="", is_group=False, hist=hist,
                    owner_raw=chat_id, is_owner_chat=True,
                    owner_id=str(getattr(self.e, "owner_id", "") or ""),
                    proactive_reason=reason)
                # 立刻取这一轮的预算：call_model 可能跑几秒，
                # 期间引擎线程会给 self._policy 赋别的会话的值。
                _policy = getattr(self.e, "_policy", None)
                raw = call_model(
                    mcfg,
                    "\n".join(turn["sys_parts"]).strip(),
                    "\n\n".join(turn["user_parts"]))
                text = P.clean_reply(raw)
                if not text:
                    self._log("[主动] 她想了想，算了", "think")
                    return
                # 分条：她主动说话也该是 1-3 条短句，不是一大坨
                try:
                    parts, gaps = self.e._say_plan(text, policy=_policy)
                except Exception:
                    parts, gaps = [text], [0.0]
                if not parts:
                    parts, gaps = [text], [0.0]
                self._log(f"[主动] 想说：{text}｜{source}：{reason[:60]}", "think")
                self.outbox.put({
                    "kind": "proactive", "chat_id": chat_id,
                    "chat_name": chat_name, "text": text,
                    "parts": list(parts), "gaps": list(gaps),
                    "source": source, "sources": used_sources,
                    "anchor_at": anchor,
                    "material": dict(material or {}),
                })
            except Exception as exc:
                self._log(f"[主动] 生成失败：{exc}", "err")
            finally:
                self.proactive_busy = False

        threading.Thread(target=work, daemon=True, name="bf-proactive").start()

    # ------------------------------------------------------------------
    #  追问 or 忘记
    # ------------------------------------------------------------------

    def _maybe_followup(self, now: float) -> None:
        pro = self._ensure_pro()
        if pro is None or self.proactive_busy:
            return

        # 追问也要跨天重置：`st.day` 是追问随机种子的来源，
        # 只在主动判定里更新的话，整天没触发主动时种子一直是旧的 ——
        # 同一条待回消息永远得到同一个骰子结果。
        P.roll_day(pro, now)
        P.expire_awaiting(pro, now=now)

        bond_hit = False
        try:
            import bigfish.vault as V
            from bigfish import bond as B

            db = V.memory_db_path()
            owner_id = str(getattr(self.e, "owner_id", "") or "")
            if db and owner_id:
                bond_hit = bool(B.list_bonds(db, "dafeiyu", owner_id, limit=1))
        except Exception:
            bond_hit = False

        due = P.due_followups(pro, now=now, life_state=self._life(),
                              bond_hit=bond_hit)
        if not due:
            return
        chat_id, item = due[0]
        sent_text = str(item.get("text") or "")

        self.proactive_busy = True
        mcfg = self._mcfg()
        chat_name = self._display_name(chat_id)

        def work() -> None:
            try:
                from bigfish import history
                from bigfish.replier import call_model

                hours = max(0.0, (now - float(item.get("sent_at") or now)) / 3600)
                # 追问和主动开口共用同一套拼装，只把「为什么现在说话」
                # 换成"你已经说了、他没回"。所以追问也带着她的记忆和心情，
                # 而不是一句孤零零的"在忙？"。
                reason = (f"{hours:.0f} 小时前你主动说了「{sent_text}」，"
                          "对方一直没回。你想再问一句 —— 问得随意点，"
                          "一句就够，别质问、别撒娇施压，也可以干脆不问。")
                turn = self.e._compose(
                    chat_id=chat_id, chat_name=chat_name, sender_name="",
                    incoming="", is_group=False,
                    hist=history.load(chat_id, limit=10),
                    owner_raw=chat_id, is_owner_chat=True,
                    owner_id=str(getattr(self.e, "owner_id", "") or ""),
                    proactive_reason=reason)
                _policy = getattr(self.e, "_policy", None)
                raw = call_model(
                    mcfg,
                    "\n".join(turn["sys_parts"]).strip(),
                    "\n\n".join(turn["user_parts"]))
                text = P.clean_reply(raw)
                if not text:
                    P.mark_forgotten(pro, chat_id=chat_id, now=now)
                    P.save(pro)
                    self._log("[主动] 她想了想，算了", "think")
                    return
                try:
                    parts, gaps = self.e._say_plan(text, policy=_policy)
                except Exception:
                    parts, gaps = [text], [0.0]
                self.outbox.put({"kind": "followup", "chat_id": chat_id,
                                 "chat_name": chat_name, "text": text,
                                 "parts": list(parts or [text]),
                                 "gaps": list(gaps or [0.0]),
                                 "source": "followup"})
            except Exception as exc:
                self._log(f"[主动] 追问生成失败：{exc}", "err")
            finally:
                self.proactive_busy = False

        threading.Thread(target=work, daemon=True, name="bf-followup").start()

    # ------------------------------------------------------------------
    #  真正发送
    # ------------------------------------------------------------------

    def _deliver(self, item: dict, now: float) -> None:
        chat_id = str(item.get("chat_id") or "")
        chat_name = str(item.get("chat_name") or chat_id)
        text = str(item.get("text") or "").strip()
        source = str(item.get("source") or "proactive")
        if not chat_id or not text:
            return
        # 分条：主动消息走 _say_plan 之后可能是 2-3 段。没有就按整段发。
        _parts = [str(p).strip() for p in (item.get("parts") or []) if str(p).strip()]
        if not _parts:
            _parts = [text]

        # ── 预演模式 ──
        #
        # 用户在「主动」页打开预演之后，这一次不发出去，只把
        # 「本来会发什么」记下来。**但仍然占当天额度** ——
        # 不占的话一整晚都在预演，看到的是"她每分钟都想说话"，
        # 那回答不了"如果我开着她一天会说几句"。
        #
        # 也不进 awaiting：那条消息根本没出去，不可能有人回，
        # 放进待回会让追问逻辑以为"发了没回"。
        if bool(self._config().get("dry_run", False)):
            try:
                pro = self._ensure_pro()
                if pro is not None:
                    P.note_dry(pro, chat_id=chat_id, source=source, text=text,
                               now=now,
                               anchor_at=float(item.get("anchor_at") or 0),
                               material=item.get("material") or {},
                               sources=item.get("sources") or [])
                    P.save(pro)
            except Exception:
                pass
            # 预演要看见她**本来会分几条**发 —— 只显示完整文本看不出节奏
            _preview = " ／ ".join(_parts) if _parts else text
            self._log(f"[主动·预演] {chat_name}：{_preview}", "think")
            return

        if getattr(self.e, "dry", False):
            self._log(f"[主动·预演] {chat_name}：{text}（引擎 dry 模式）", "think")
            return

        sender = self._sender()
        if sender is None:
            self._log("[主动] UIA 未就绪，这条不发（也不会补发）", "err")
            return

        from bigfish import sending as _sending

        guard = self._guard()

        # 逐条发。记账规则和普通回复一样：**先记账再发**（回车到写库是
        # 异步的，晚记一步就有窗口让这条被读回来当成"对方说的"）。
        #
        # 任何一条失败就停下，不发剩下的 —— 否则语句会错位
        # （她以为在说第二句，其实第一句根本没出去）。
        sent_any = False
        verdict = None
        for _i, _part in enumerate(_parts):
            if _i:
                # 分条之间留一点间隔，像人在打字
                try:
                    import time as _t2

                    _t2.sleep(min(1.2, 0.35 + len(_part) * 0.05))
                except Exception:
                    pass
            try:
                if guard is not None:
                    guard.note_sent(_part, chat_id)
            except Exception:
                pass
            try:
                verdict = _sending.classify(result=sender.send(chat_name, _part))
            except Exception as exc:
                verdict = _sending.classify(error=exc)
            if not verdict.sent:
                try:
                    if verdict.failed and guard is not None:
                        guard.rollback_sent(_part, chat_id)
                except Exception:
                    pass
                break
            sent_any = True

        if not sent_any:
            detail = verdict.detail if verdict is not None else "没有内容可发"
            self._log(f"[主动] {'✗' if (verdict and verdict.failed) else '?'} {detail}",
                      "err")
            return

        # 发出去之后：历史 + 主动状态 + 日志
        try:
            from bigfish import history

            # 和引擎回复用同一个署名（见上面 `me` 的注释）
            history.append(chat_id, "我", text)
        except Exception:
            pass

        try:
            pro = self._ensure_pro()
            if pro is not None:
                if item.get("kind") == "followup":
                    P.mark_followed_up(pro, chat_id=chat_id, now=now, text=text)
                elif item.get("kind") == "goodnight":
                    # 道别不占主动额度：它属于作息，不属于"想找你说话"。
                    # 也不进 awaiting —— 没人回"晚安"是预期之内的事，
                    # 放进去会让她第二天追问"你怎么没回我晚安"。
                    pass
                else:
                    P.mark_sent(pro, chat_id=chat_id, source=source, text=text,
                                now=now,
                                anchor_at=float(item.get("anchor_at") or 0),
                                material=item.get("material") or {},
                                sources=item.get("sources") or [])
                    # p0-1：这一句是"你最近很忙吗"，记下时刻。
                    # 不记的话，她会在同一天里反复问同一个问题 ——
                    # 那不是关心，是复读。
                    if source == "check_in":
                        pro.last_check_in_at = now
                P.save(pro)
        except Exception:
            pass

        self._log(f"[主动] ✓ {chat_name}：{text}", "out")
        try:
            self.e.chat_event.emit(chat_name, text[:60], "out")
        except Exception:
            pass
