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

import queue
import random
import threading
from datetime import datetime
from typing import Any, Optional

from bigfish import proactive as P

#: 情绪衰减间隔（秒）
MOOD_DECAY_EVERY = 600.0
#: 主动/追问检查间隔（秒）
PROACTIVE_EVERY = 60.0


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

        只做**不需要她愿意说话**的那部分：心情平复。
        主动开口挪去了 tick_active。
        """
        if self._stopped:
            return

        # 情绪平复
        if now - self._last_mood_decay >= MOOD_DECAY_EVERY:
            self._last_mood_decay = now
            try:
                from bigfish import mood_judge

                mood_judge.decay_only()
            except Exception:
                pass

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
            try:
                self._maybe_proactive(now)
            except Exception as exc:
                self._log(f"[主动] 判定异常：{exc}", "err")
            try:
                self._maybe_followup(now)
            except Exception as exc:
                self._log(f"[主动] 追问判定异常：{exc}", "err")

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
        #   `history.append` 存进去的那个值一致 —— 那里存的是 soul.address
        #   （「爸」）。以前这里算的是 soul.name（「大肥鱼」）或 wxid，
        #   两边永远不相等，于是 `candidate_open_loop` 会把**她自己最后说的
        #   那句话**当成"他最后说的是……"。
        soul = self._soul()
        me = "我"
        if soul is not None:
            me = str(getattr(soul, "address", "") or "我")

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
        score = P.combined_score(topic, tone)
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
                    parts, gaps = self.e._say_plan(text)
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
                    parts, gaps = self.e._say_plan(text)
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

            who = "我"
            soul = self._soul()
            if soul is not None:
                who = str(getattr(soul, "address", "") or "我")
            history.append(chat_id, who, text)
        except Exception:
            pass

        try:
            pro = self._ensure_pro()
            if pro is not None:
                if item.get("kind") == "followup":
                    P.mark_followed_up(pro, chat_id=chat_id, now=now, text=text)
                else:
                    P.mark_sent(pro, chat_id=chat_id, source=source, text=text,
                                now=now,
                                anchor_at=float(item.get("anchor_at") or 0),
                                material=item.get("material") or {},
                                sources=item.get("sources") or [])
                P.save(pro)
        except Exception:
            pass

        self._log(f"[主动] ✓ {chat_name}：{text}", "out")
        try:
            self.e.chat_event.emit(chat_name, text[:60], "out")
        except Exception:
            pass
