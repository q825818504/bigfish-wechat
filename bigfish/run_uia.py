"""大肥鱼 × 微信（UIA 通道）。

完整链路：

    数据库轮询（读）──▶ 大肥鱼（人格+情绪+记忆+模型）──▶ UIA 发送（写）

用法：
    python bigfish/run_uia.py --who 文件传输助手
    python bigfish/run_uia.py --watch-all          # 监听最近 10 个会话
    python bigfish/run_uia.py --who 小麟 --dry     # 只生成不发送

前置：
    1. 管理员权限运行
    2. 微信已登录，主窗口可见（不要收到托盘）
    3. 桌面解锁

Ctrl+C 退出。
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time

for _s in (sys.stdout, sys.stderr):
    if _s is not None:
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from bigfish.bridge_client import load_context           # noqa: E402
from bigfish.soul import Soul, load_soul                # noqa: E402
from bigfish.watcher import Incoming, MessageWatcher    # noqa: E402
from bigfish.wechat_sender import (SendError, UiaSender,  # noqa: E402
                                   desktop_available, is_admin)
from bigfish.wechat import WeChatReader                 # noqa: E402

HR = "=" * 74
HISTORY_LIMIT = 16


class Conversation:
    """单个会话的对话历史（内存里）。"""

    def __init__(self, limit: int = HISTORY_LIMIT):
        self.limit = limit
        self.turns: list[tuple[str, str]] = []

    def add(self, who: str, text: str) -> None:
        self.turns.append((who, text))
        if len(self.turns) > self.limit:
            self.turns = self.turns[-self.limit:]

    def transcript(self, me: str) -> str:
        if not self.turns:
            return "（暂无对话）"
        return "\n".join(f"{w}: {t}" for w, t in self.turns)


class BigFish:
    def __init__(self, reader: WeChatReader, sender: UiaSender,
                 soul: Soul, model_cfg, dry: bool = False,
                 allow_self: bool = False, on_log=None):
        self.reader = reader
        self.sender = sender
        self.soul = soul
        self.cfg = model_cfg
        self.dry = dry
        self.allow_self = allow_self
        self.on_log = on_log or (lambda s: print(s, flush=True))
        self.convs: dict[str, Conversation] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def conv(self, key: str) -> Conversation:
        if key not in self.convs:
            self.convs[key] = Conversation()
        return self.convs[key]

    # ------------------------------------------------------------------
    def build_prompt(self, conv: Conversation, msg: Incoming) -> tuple[str, str]:
        ctx = load_context(query=msg.text or "")
        for w in ctx.warnings:
            self.on_log(f"[bridge] {w}")

        sys_parts = [ctx.persona or self.soul.render_persona()]
        if ctx.mood and ctx.mood.injected_text.strip():
            sys_parts.append("\n## 这会儿的状态\n\n"
                             + ctx.mood.injected_text.strip())
        elif ctx.mood and ctx.mood.value is not None:
            sys_parts.append("\n## 这会儿的状态\n\n"
                             f"- 心情：{ctx.mood.label}"
                             f"（{ctx.mood.value:.0f}/100）")

        user_parts = []
        mem = ctx.render_memory()
        if mem:
            user_parts.append("## 你记得的事\n\n" + mem)

        is_group = msg.chat_id.endswith("@chatroom")
        scene = (f"你在微信群「{msg.chat_name}」里，"
                 f"{msg.sender_name or '某人'} 刚说了话。" if is_group
                 else f"你在微信上和「{msg.chat_name}」聊天。")
        user_parts.append(
            f"## 这一轮\n\n{scene}\n"
            f"以{self.soul.name}的身份回一条消息。\n\n"
            "要求：\n"
            "- 像真人发微信，短，口语，不用 Markdown，不列清单\n"
            "- 不要复述对方说过的话，直接回应\n"
            "- 需要更多信息就一句话追问"
        )
        user_parts.append("## 最近的对话\n\n"
                          + conv.transcript(self.soul.address or "我"))
        return ("\n".join(sys_parts).strip(), "\n\n".join(user_parts))

    # ------------------------------------------------------------------
    def handle(self, msg: Incoming) -> None:
        # 归属判定收口到 bigfish/identity.py（与 app/desktop 同一套）。
        #
        # run_uia 没有接 scopes，自测通道仍然只对 filehelper 生效 —— 这是
        # 它作为原型入口的既有语义，不是漏改。三态仍然生效：
        # 拿不准的私聊直接放弃；群里没有闸门可比对，也按"没叫她"处理。
        from bigfish import identity

        is_group = msg.chat_id.endswith("@chatroom")
        verdict = identity.decide(
            msg.sender_name, identity.self_id_of(self.reader),
            text=msg.raw_content or msg.text,
            chat_id=msg.chat_id, is_group=is_group)
        decision = identity.admit(
            verdict, is_group=is_group,
            allow_self=bool(self.allow_self) and msg.chat_id == "filehelper")
        if not decision.proceed:
            self.on_log(f"[跳过] {msg.chat_name}: {decision.detail}"
                        f"（{decision.reason}）")
            return
        if not (msg.text or "").strip():
            self.on_log(f"[跳过] {msg.chat_name}: 空内容({msg.msg_type})")
            return

        key = msg.chat_id
        with self._lock:
            conv = self.conv(key)
        who_label = msg.sender_name if msg.chat_id.endswith("@chatroom") else msg.chat_name
        conv.add(who_label or "对方", msg.text)
        self.on_log(f"\n[收] {msg.chat_name}"
                    f"{(' / ' + msg.sender_name) if msg.sender_name and msg.chat_id.endswith('@chatroom') else ''}"
                    f"：{msg.preview(60)}")

        system, user = self.build_prompt(conv, msg)

        from bigfish.replier import call_model, ModelError
        t0 = time.time()
        try:
            reply = call_model(self.cfg, system, user)
        except ModelError as exc:
            self.on_log(f"[!] 生成失败: {exc}")
            return
        dt = time.time() - t0
        if not reply.strip():
            self.on_log("[!] 模型返回空")
            return
        self.on_log(f"[想] {dt:.1f}s  in={len(system)+len(user)}字 "
                    f"out={len(reply)}字")

        conv.add(self.soul.address or "我", reply)

        if self.dry:
            self.on_log(f"[预演] 不发送。内容：{reply!r}")
            return

        # 群消息默认不抢话，除非明确点名 —— 这里先简单处理：只回消息目标
        target = msg.chat_name
        try:
            r = self.sender.send(target, reply)
            if r.ok:
                self.on_log(f"[发] ✓ → {target}：{reply[:60]}")
            else:
                self.on_log(f"[发] ✗ {target}：{r.detail}")
        except SendError as exc:
            self.on_log(f"[发] ✗ {exc.kind}: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description="大肥鱼 × 微信（UIA）")
    ap.add_argument("--who", action="append", default=[],
                    help="监听指定会话（可多次）")
    ap.add_argument("--watch-all", type=int, default=0,
                    metavar="N", help="监听最近 N 个会话")
    ap.add_argument("--dry", action="store_true", help="只生成不发送")
    ap.add_argument("--interval", type=float, default=1.2,
                    help="轮询间隔秒")
    ap.add_argument("--allow-self", action="store_true",
                    help="也处理自己发的消息（文件传输助手测试用）")
    args = ap.parse_args()

    print(HR)
    print("大肥鱼 × 微信（UIA 通道）")
    print(HR)

    admin = is_admin()
    print(f"  管理员     : {admin}")
    print(f"  桌面可用   : {desktop_available()}")
    if not admin and not args.dry:
        print()
        print("  [!] 不是管理员，UIA 发送需要提权（热激活要写 gate 字节）。")
        print("      用 --dry 可以只测试生成，不发送。")
        return 1

    # ---- 灵魂 ----
    try:
        soul = load_soul()
    except FileNotFoundError as exc:
        print(f"  [!] {exc}")
        return 1
    print(f"  灵魂       : {soul.name}（rev {soul.revision}）")
    print(f"  称呼       : {soul.address}")

    # ---- 模型 ----
    from bigfish.replier import ModelConfig
    cfg = ModelConfig.load()
    print(f"  模型       : {cfg.preset.name} / {cfg.eff_model}")
    print(f"  接口       : {cfg.url}")
    if not cfg.ready:
        print("  [!] 模型未就绪")
        return 1

    # ---- 数据库 ----
    print()
    print("  打开微信数据库 ...")
    try:
        reader = WeChatReader().open()
    except Exception as exc:
        print(f"  [!] 数据库打不开: {exc}")
        return 1
    print(f"  账号       : {reader._db.account}")
    me = reader.self_info()
    print(f"  昵称       : {me.get('nick_name')}")

    # ---- 身份闸 ----
    # 拿不到自己的 wxid 就不启动：那种状态下所有自环防护都失效（见 A-1）。
    from bigfish import identity
    try:
        identity.require_me(identity.self_id_of(reader), where="run_uia.py")
    except identity.IdentityUnavailable as exc:
        print()
        print(f"  [!] {exc.reason}")
        return 1

    # ---- 监听会话 ----
    watcher = MessageWatcher(reader._db, interval=args.interval)
    if args.watch_all:
        ids = watcher.watch_all_recent(limit=args.watch_all)
        print(f"  监听       : 最近 {len(ids)} 个会话")
        for s in reader.sessions(limit=args.watch_all):
            kind = "群" if s["is_group"] else "人"
            print(f"      [{kind}] {s['name']}")
    else:
        for w in args.who:
            uid = watcher.watch(w)
            print(f"  监听       : {w} -> {uid}")
    if not watcher._chats:
        print("  [!] 没有要监听的会话，用 --who 或 --watch-all")
        return 1

    # ---- 发送器 ----
    sender = UiaSender()
    if not args.dry:
        try:
            sender.wake()
            print(f"  UIA        : 已就绪，当前会话 = {sender.current_chat()!r}")
        except SendError as exc:
            print(f"  [!] UIA 初始化失败: {exc}")
            return 1
    else:
        print("  UIA        : 预演模式，跳过")

    app = BigFish(reader, sender, soul, cfg, dry=args.dry,
                  allow_self=args.allow_self)

    def on_msg(m: Incoming) -> None:
        app.handle(m)

    watcher.on_message = on_msg
    print()
    print(HR)
    print("  开始监听。给对方发消息就会自动回复。Ctrl+C 退出。")
    print(HR)

    watcher.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[bigfish] 停止中 ...")
        watcher.stop()
        print("[bigfish] 已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
