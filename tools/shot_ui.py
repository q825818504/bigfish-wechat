# -*- coding: utf-8 -*-
"""渲染某一页并截图（用真实数据，不启动大肥鱼）。

跑法：
    python tools/shot_ui.py                       # 设置页
    python tools/shot_ui.py --page impression     # 印象页
    python tools/shot_ui.py --page impression out.png

数据从 `data/memory.sqlite3` 直接读，所以看到的就是真机上的样子。
不依赖 bridge：加载完页面后用 JS 把数据灌进去再调渲染函数。
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

from PySide6.QtCore import QTimer, QUrl                     # noqa: E402
from PySide6.QtWebEngineWidgets import QWebEngineView      # noqa: E402
from PySide6.QtWidgets import QApplication                  # noqa: E402

from bigfish import providers as P                          # noqa: E402
from bigfish.replier import ModelConfig                     # noqa: E402

PAGE = "settings"
SCROLL_TO = ""
OUT = os.path.join(_ROOT, "dist", "_ui_shot.png")

args = sys.argv[1:]
if "--page" in args:
    i = args.index("--page")
    if i + 1 < len(args):
        PAGE = args[i + 1]
        args = args[:i] + args[i + 2:]
if "--scroll" in args:
    i = args.index("--scroll")
    if i + 1 < len(args):
        SCROLL_TO = args[i + 1]
        args = args[:i] + args[i + 2:]
if args:
    OUT = args[0]

DATA_ROOT = os.path.join(_ROOT, "dist", "BigFish", "data")


def fake_llm() -> dict:
    mc = ModelConfig(channel="deepseek", model="deepseek-v4-flash",
                     api_key="sk-demo")
    ok, why = mc.status()
    return {
        "channel": mc.channel, "base_url": mc.preset.base_url,
        "model": mc.eff_model, "api_key": mc.api_key,
        "protocol": mc.eff_protocol, "temperature": mc.temperature,
        "max_tokens": mc.max_tokens, "eff_base_url": mc.eff_base_url,
        "url": mc.url, "ready": ok, "status": why,
        "needs_key": mc.needs_key, "preset_name": mc.preset.name,
        "note": mc.preset.note, "key_hint": mc.preset.key_hint,
        "doc": mc.preset.doc, "suggest": list(mc.preset.models),
    }


def real_impression() -> dict:
    """印象页数据（读真实库，昵称是 owners.display_name）。"""
    try:
        from bigfish import memory as M, vault as V

        db = V.memory_db_path(DATA_ROOT)
        if not db:
            return {"people": []}
        d = M.impression_board(db, "dafeiyu")
        people = d.get("people") or []
        # 和 Backend.impression_board 一样：主人置顶
        people.sort(key=lambda p: (0 if p.get("tier") == "owner" else 1,
                                   -int(p.get("facts") or 0)))
        for p in people:
            p["isOwner"] = (p.get("tier") == "owner")
        d["people"] = people
        return d
    except Exception as exc:
        print("读印象数据失败:", exc)
        return {"people": []}


def build_js() -> str:
    if PAGE == "impression":
        payload = real_impression()
        return f"""
        (function() {{
          const d = {json.dumps(payload, ensure_ascii=False)};
          IMPR.people = d.people || [];
          IMPR.active = IMPR.people.length ? IMPR.people[0].owner : '';
          setText('#imprCount', IMPR.people.length + ' 人');
          renderImprTabs();
          renderImprPanes();
          document.querySelectorAll('.nav-item').forEach((b) =>
            b.classList.toggle('is-active', b.dataset.page === 'impression'));
          document.querySelectorAll('.page').forEach((p) =>
            p.classList.toggle('is-active', p.dataset.page === 'impression'));
          window.scrollTo(0, 0);
          return IMPR.people.length;
        }})()
        """

    payload = {
        "channels": P.channels(),
        "protocols": [{"id": p, "label": P.PROTOCOL_LABEL[p]}
                      for p in P.PROTOCOLS],
        "llm": fake_llm(),
        "demoModels": ["deepseek-v4-flash", "deepseek-v4-pro"],
    }
    return f"""
    (function() {{
      const d = {json.dumps(payload, ensure_ascii=False)};
      CHANNELS = d.channels;
      PROTOCOLS = d.protocols;
      const sel = document.querySelector('#cfgChannel');
      sel.innerHTML = '';
      CHANNELS.forEach((ch) => {{
        const o = document.createElement('option');
        o.value = ch.id; o.textContent = ch.name;
        sel.appendChild(o);
      }});
      const psel = document.querySelector('#cfgProtocol');
      psel.innerHTML = '';
      PROTOCOLS.forEach((p) => {{
        const o = document.createElement('option');
        o.value = p.id; o.textContent = p.label;
        psel.appendChild(o);
      }});
      sel.value = d.llm.channel;
      document.querySelector('#cfgBaseUrl').value = d.llm.base_url;
      document.querySelector('#cfgModel').value = d.llm.model;
      document.querySelector('#cfgApiKey').value = d.llm.api_key;
      document.querySelector('#cfgProtocol').value = d.llm.protocol;
      document.querySelector('#cfgTemp').value = d.llm.temperature;
      document.querySelector('#cfgMaxTokens').value = d.llm.max_tokens;
      applyChannel(d.llm.channel, {{ keepValues: true }});
      setModelOptions(d.demoModels, d.llm.model);
      setModelNote('ok', '拉到 ' + d.demoModels.length + ' 个模型，在上面选一个。');
      const tag = document.querySelector('#modelTag');
      tag.textContent = d.llm.preset_name;
      tag.className = 'tag ok';
      document.querySelectorAll('.nav-item').forEach((b) =>
        b.classList.toggle('is-active', b.dataset.page === 'settings'));
      document.querySelectorAll('.page').forEach((p) =>
        p.classList.toggle('is-active', p.dataset.page === 'settings'));
      // 假装刚检查过、发现了新版 —— 截图要展示的是"有更新"的样子
      renderUpdate({{
        has_update: true, latest: '0.5.0', current: '0.4.1',
        published: '2026-11-05T10:00:00Z',
        repo: 'q825818504/bigfish-wechat',
        notes: '模型接入：19 家预设，选一家填 Key 就能用。「获取模型」从这家拉真实列表。',
      }});
      setText('#aboutVersion', 'v0.4.1');
      setText('#updateSource',
        '从 GitHub（q825818504/bigfish-wechat）读取最新版本。发现新版可以一键下载。');
      window.scrollTo(0, 0);
      return CHANNELS.length;
    }})()
    """


def main() -> int:
    app = QApplication(sys.argv[:1])
    view = QWebEngineView()
    view.resize(1360, 1000)
    view.show()

    view.load(QUrl.fromLocalFile(os.path.join(_ROOT, "ui", "index.html")))
    js = build_js()

    def after_load(ok: bool) -> None:
        if not ok:
            print("!! 页面加载失败")
            app.quit()
            return

        def shoot() -> None:
            view.page().runJavaScript(js, 0, lambda n: None)
            if SCROLL_TO:
                QTimer.singleShot(400, lambda: view.page().runJavaScript(
                    f"document.querySelector('#{SCROLL_TO}')"
                    f"?.scrollIntoView({{block:'center'}})"))
            QTimer.singleShot(900, grab)

        def grab() -> None:
            pix = view.grab()
            os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
            pix.save(OUT)
            print("截图:", OUT, pix.width(), "x", pix.height())
            app.quit()

        QTimer.singleShot(900, shoot)

    view.loadFinished.connect(after_load)
    QTimer.singleShot(15000, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
