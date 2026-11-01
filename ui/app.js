/* ══════════════════════════════════════════════════════════════
   大肥鱼 · 界面逻辑
   ══════════════════════════════════════════════════════════════ */

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const S = {
  bridge: null,
  running: false,
  chats: [],
  watching: new Set(),
  soul: null,
};

const PAGE_TITLES = {
  overview: '概览', chats: '会话', soul: '灵魂',
  mood: '心情', memory: '记忆', settings: '设置', logs: '日志',
};

/* ────────── 工具 ────────── */

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const hhmmss = () => {
  const d = new Date();
  return [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map((n) => String(n).padStart(2, '0')).join(':');
};

function toast(text) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = text;
  $('#toastWrap').appendChild(el);
  setTimeout(() => {
    el.classList.add('out');
    setTimeout(() => el.remove(), 240);
  }, 2400);
}

function setDot(kind) {
  $('#statusDot').className = 'dot' + (kind ? ' ' + kind : '');
}

function setText(sel, v) {
  const el = $(sel);
  if (el) el.textContent = v;
}

/* ────────── 导航 ────────── */

function go(page) {
  $$('.nav-item').forEach((b) => b.classList.toggle('is-active', b.dataset.page === page));
  $$('.page').forEach((p) => p.classList.toggle('is-active', p.dataset.page === page));
  setText('#breadcrumb', PAGE_TITLES[page] || page);

  if (page === 'soul' && !S.soul) loadSoul();
  if (page === 'mood') loadMood();
  if (page === 'memory') loadMemory('');
}

$$('.nav-item').forEach((b) => b.addEventListener('click', () => go(b.dataset.page)));

document.addEventListener('click', (e) => {
  const t = e.target.closest('[data-goto]');
  if (t) go(t.dataset.goto);
});

/* ────────── 日志 ────────── */

function pushLog(text, kind = '') {
  const view = $('#logView');
  const line = document.createElement('div');
  line.className = `log-line ${kind}`;
  line.innerHTML =
    `<span class="log-time">${hhmmss()}</span>` +
    `<span class="log-text">${esc(text)}</span>`;
  view.appendChild(line);
  while (view.children.length > 700) view.removeChild(view.firstChild);
  if ($('#logAutoScroll').checked) view.scrollTop = view.scrollHeight;
}

$('#btnClearLog').addEventListener('click', () => { $('#logView').innerHTML = ''; });

/* ────────── 概览 ────────── */

const CHECK_ICONS = {
  ok: '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M2.5 6.2l2.3 2.3 4.7-5"/></svg>',
  warn: '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M6 2.5v4M6 9h.01"/></svg>',
  error: '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M3 3l6 6M9 3l-6 6"/></svg>',
};

const CHECK_LABELS = {
  admin: ['管理员权限', 'UIA 热激活需要'],
  desktop: ['桌面可操作', '锁屏时无法发送'],
  wechat: ['微信进程', '需已登录并可见'],
  db: ['数据库密钥', '聊天记录解密'],
  model: ['模型接口', 'Magpie 网关'],
  soul: ['灵魂数据', 'DSH 插件'],
};

function renderChecks(items) {
  const grid = $('#checkGrid');
  grid.innerHTML = '';
  for (const it of items) {
    const [name, hint] = CHECK_LABELS[it.key] || [it.key, ''];
    const el = document.createElement('div');
    el.className = `check-item ${it.status}`;
    el.innerHTML =
      `<span class="check-icon">${CHECK_ICONS[it.status] || ''}</span>` +
      `<div><div class="check-name">${esc(name)}</div>` +
      `<div class="check-val">${esc(it.value)}</div></div>` +
      `<span></span>`;
    el.title = hint;
    grid.appendChild(el);
  }
}

function renderRecent(items) {
  const box = $('#ovRecent');
  if (!items || !items.length) {
    box.innerHTML = `<div class="empty"><h3>还没有对话</h3>
      <p>启动之后，微信里来消息就会显示在这里</p></div>`;
    return;
  }
  box.innerHTML = items.map((m) => `
    <div class="note-item" style="padding:9px 0;border-bottom:1px solid var(--border-soft)">
      <div style="flex:1;min-width:0">
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:3px">
          <span class="memory-role ${m.dir === 'out' ? 'assistant' : ''}">${esc(m.who)}</span>
          <span class="memory-time">${esc(m.dir === 'out' ? '回复' : '收到')}</span>
        </div>
        <div style="font-size:12px;color:var(--text)">${esc(m.text)}</div>
      </div>
    </div>`).join('');
}

async function refreshStatus() {
  if (!S.bridge) return;
  try {
    const s = JSON.parse(await S.bridge.get_status());
    S.running = !!s.running;

    const pb = $('#btnPower');
    pb.classList.toggle('is-on', s.running);

    setText('#ovState', s.running ? '运行中' : (s.error ? '出错了' : '未启动'));
    setText('#ovStateHint', s.running
      ? '她在听着。微信里有人说话就会回。'
      : (s.error || '登录并选择会话后，点右侧按钮启动。'));
    setText('#statusText', s.running ? '运行中' : (s.error ? '出错' : '未启动'));
    setDot(s.running ? 'on' : (s.error ? 'err' : ''));

    const tag = $('#ovPowerTag');
    tag.textContent = s.running ? `监听 ${s.watching || 0} 个会话` : '等待启动';
    tag.className = 'tag' + (s.running ? ' ok' : '');

    $('#ovRecv').innerHTML = `${s.recv ?? 0}<small>条</small>`;
    $('#ovReply').innerHTML = `${s.reply ?? 0}<small>条</small>`;
    setText('#ovUptime', s.running ? (s.uptime || '0秒') : '—');
    setText('#accountLabel', s.account || '—');
    setText('#ovUpdated', '更新于 ' + hhmmss());

    if (s.diagnose) renderChecks(s.diagnose);
    if (s.recent) renderRecent(s.recent);

    // 概览右侧摘要
    const sum = $('#ovSummary');
    if (s.running && s.watching) {
      sum.innerHTML = `<div class="check-item ok">
        <span class="check-icon">${CHECK_ICONS.ok}</span>
        <div><div class="check-name">正在监听</div>
        <div class="check-val">${s.watching} 个会话</div></div><span></span></div>`;
    } else {
      sum.innerHTML = `<div class="empty" style="padding:16px 0">
        <p>启动后这里会显示正在监听的会话</p></div>`;
    }
  } catch (e) { /* 静默 */ }
}

$('#btnPower').addEventListener('click', async () => {
  if (!S.bridge) return toast('后端未就绪');
  const btn = $('#btnPower');
  btn.disabled = true;
  try {
    if (S.running) {
      await S.bridge.stop_engine();
      toast('已停止');
    } else {
      const r = JSON.parse(await S.bridge.start_engine());
      if (r.ok) toast('已启动');
      else toast(r.error || '启动失败');
    }
  } catch (e) {
    toast('操作失败');
  } finally {
    btn.disabled = false;
    setTimeout(refreshStatus, 500);
  }
});

$('#btnRefresh').addEventListener('click', () => { refreshStatus(); toast('已刷新'); });
$('#btnRecheck').addEventListener('click', async () => {
  if (!S.bridge) return;
  const d = JSON.parse(await S.bridge.diagnose());
  renderChecks(d.items || []);
  toast('检测完成');
});
$('#btnDiagnose').addEventListener('click', () => $('#btnRecheck').click());
$('#btnOpenWechat').addEventListener('click', async () => {
  if (!S.bridge) return;
  await S.bridge.open_wechat();
  toast('已尝试打开微信');
});

/* ────────── 会话 ────────── */

function renderChats() {
  const box = $('#chatList');
  setText('#chatCount', `${S.chats.length} 个`);

  if (!S.chats.length) {
    box.innerHTML = `<div class="empty">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <path d="M4 6h16v10H8l-4 4V6z"/></svg>
      <h3>还没有加载会话</h3>
      <p>点右上角「加载会话」，从你的微信里读取列表</p></div>`;
    return;
  }

  box.innerHTML = '';
  for (const c of S.chats) {
    const on = S.watching.has(c.username);
    const initial = (c.name || '?').trim().charAt(0).toUpperCase();
    const row = document.createElement('div');
    row.className = 'chat-row' + (on ? ' is-on' : '');
    row.innerHTML =
      `<span class="chat-avatar">${esc(initial)}</span>` +
      `<div style="min-width:0">
         <div class="chat-name">${esc(c.name)}</div>
         <div class="chat-sub">${c.is_group ? '群聊' : '联系人'}${c.unread ? ' · ' + c.unread + ' 条未读' : ''}</div>
       </div>` +
      `<span class="tag">${c.is_group ? '群' : '人'}</span>` +
      `<span class="chat-toggle"></span>`;
    row.addEventListener('click', () => {
      if (S.watching.has(c.username)) S.watching.delete(c.username);
      else S.watching.add(c.username);
      renderChats();
      syncWatching();
    });
    box.appendChild(row);
  }
}

async function syncWatching() {
  if (!S.bridge) return;
  try {
    await S.bridge.set_watching(JSON.stringify([...S.watching]));
  } catch (e) { toast('同步失败'); }
}

$('#btnLoadChats').addEventListener('click', async () => {
  if (!S.bridge) return toast('后端未就绪');
  const btn = $('#btnLoadChats');
  btn.textContent = '加载中…';
  try {
    const d = JSON.parse(await S.bridge.list_chats(40));
    if (d.error) return toast(d.error);
    S.chats = d.chats || [];
    for (const c of S.chats) if (c.watching) S.watching.add(c.username);
    renderChats();
    toast(`读到 ${S.chats.length} 个会话`);
  } catch (e) {
    toast('加载失败');
  } finally {
    btn.textContent = '加载会话';
  }
});

$('#cfgGroupReply').addEventListener('change', saveQuick);
$('#cfgGroupAllowAll').addEventListener('change', saveQuick);

async function saveQuick() {
  if (!S.bridge) return;
  const cur = JSON.parse(await S.bridge.get_config());
  cur.group_reply = $('#cfgGroupReply').checked;
  cur.group_allow_all = $('#cfgGroupAllowAll').value
    .split(',').map((s) => s.trim()).filter(Boolean);
  await S.bridge.save_config(JSON.stringify(cur));
  toast('已保存');
}

/* ────────── 灵魂 ────────── */

const TRAIT_DOC = {
  warmth: ['温暖', v => v < 40 ? '克制，不主动靠近' : v > 60 ? '关心但不黏人' : '明显关心，会主动问候'],
  assertiveness: ['主见', v => v < 40 ? '顺从，倾向配合' : v > 60 ? '明确表达不同意见' : '有自己的判断，会讲理由'],
  playfulness: ['顽皮', v => v < 40 ? '正经，少玩笑' : v > 60 ? '爱玩闹，常制造轻松气氛' : '日常爱逗，会起哄'],
  directness: ['直接', v => v < 40 ? '说话绕，先铺垫' : v > 60 ? '直给结论，少铺垫' : '不绕太远，但顾着分寸'],
};

async function loadSoul() {
  if (!S.bridge) return;
  try {
    const d = JSON.parse(await S.bridge.get_persona());
    if (d.error) return toast(d.error);
    S.soul = d;

    setText('#soulRev', `rev ${d.revision}`);
    setText('#soulName', d.name || '—');
    setText('#soulAddress', d.address || '—');
    $('#soulValueCount').innerHTML = `${(d.values || []).length}<small>条</small>`;

    // 我是谁
    const who = $('#soulWhoIAm');
    who.innerHTML = (d.whoIAm || '（空）')
      .split(/\n{2,}/).filter(Boolean)
      .map((p) => `<p>${esc(p)}</p>`).join('');

    // 珍视
    const vals = $('#soulValues');
    vals.innerHTML = (d.values || []).map((v) =>
      `<div class="value-item"><span class="value-key"></span><span>${esc(v)}</span></div>`
    ).join('') || '<div class="empty" style="padding:12px 0"><p>（空）</p></div>';

    // 性格
    const traits = d.traits || {};
    $('#soulTraits').innerHTML = Object.entries(TRAIT_DOC).map(([k, [label, fn]]) => {
      const v = traits[k];
      if (typeof v !== 'number') return '';
      return `<div class="trait-item">
        <div class="trait-name">${label}</div>
        <div class="trait-score">${v}<small>/100</small></div>
        <div class="trait-desc">${esc(fn(v))}</div>
      </div>`;
    }).join('') || '<div class="empty"><p>（无）</p></div>';

    // 自知
    const notes = $('#soulNotes');
    notes.innerHTML = (d.notes || d.selfNotes || []).map((n) =>
      `<div class="note-item">${esc(n)}</div>`
    ).join('') || '<div class="empty" style="padding:12px 0"><p>（空）</p></div>';
  } catch (e) {
    toast('读取灵魂失败');
  }
}

/* ────────── 心情 ────────── */

const DIM_NAMES = {
  happy: '开心', calm: '平静', sweet: '甜',
  heartache: '心软', worry: '担心', sad: '难过', jealous: '吃醋',
};

async function loadMood() {
  if (!S.bridge) return;
  try {
    const d = JSON.parse(await S.bridge.get_mood());
    if (d.error) return toast(d.error);

    const score = Math.round(d.value ?? 0);
    setText('#moodScore', d.value == null ? '—' : score);
    setText('#moodLabel', d.label || '—');
    setText('#moodReason', d.reason || '');
    setText('#moodUpdated', d.updated ? '更新于 ' + d.updated : '—');
    setText('#moodInjected', d.injected || '（无）');

    // 环形进度：周长 264
    const ring = $('#moodRing');
    const pct = Math.max(0, Math.min(100, score)) / 100;
    ring.setAttribute('stroke-dashoffset', String(264 * (1 - pct)));

    // 维度
    const dims = d.dims || {};
    const box = $('#moodDims');
    const rows = Object.entries(DIM_NAMES).map(([k, label]) => {
      const raw = Number(dims[k] ?? 0);
      // dims 是 0-1 的小数，展示成百分比
      const p = Math.max(0, Math.min(1, raw));
      return `<div class="dim-row">
        <span class="dim-name">${label}</span>
        <span class="dim-track"><span class="dim-fill" style="width:${(p * 100).toFixed(1)}%"></span></span>
        <span class="dim-val">${(p * 100).toFixed(0)}%</span>
      </div>`;
    }).join('');
    box.innerHTML = rows || '<div class="empty"><p>（无数据）</p></div>';
  } catch (e) {
    toast('读取心情失败');
  }
}

/* ────────── 记忆 ────────── */

async function loadMemory(query) {
  if (!S.bridge) return;
  try {
    const d = JSON.parse(await S.bridge.search_memory(query || '', 40));
    if (d.error) return toast(d.error);

    const box = $('#memList');
    const items = d.items || [];
    setText('#memCount', `${items.length} 条`);
    setText('#memDbTag', d.total != null ? `库里共 ${d.total} 条` : '—');

    if (!items.length) {
      box.innerHTML = `<div class="empty">
        <h3>没有匹配的记忆</h3>
        <p>换一个词试试。记忆检索用的是中文二元分词，整句也能命中。</p></div>`;
      return;
    }

    box.innerHTML = items.map((m) => `
      <div class="memory-item">
        <div class="memory-meta">
          <span class="memory-role ${m.role === 'assistant' ? 'assistant' : ''}">
            ${m.role === 'assistant' ? esc(S.soul?.name || '她') : '对方'}
          </span>
          <span class="memory-time">${esc(m.time || '')}</span>
        </div>
        <div class="memory-text">${esc(m.text)}</div>
      </div>`).join('');
  } catch (e) {
    toast('检索失败');
  }
}

$('#btnMemSearch').addEventListener('click', () => loadMemory($('#memQuery').value.trim()));
$('#memQuery').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') loadMemory($('#memQuery').value.trim());
});

/* ────────── 设置 ────────── */

async function loadSettings() {
  if (!S.bridge) return;
  try {
    const c = JSON.parse(await S.bridge.get_config());
    $('#cfgEndpoint').value = c.endpoint || '';
    $('#cfgModel').value = c.model || '';
    $('#cfgApiKey').value = c.api_key || '';
    $('#cfgTemp').value = c.temperature ?? 1.0;
    $('#cfgMaxTokens').value = c.max_tokens ?? 1600;
    $('#cfgInterval').value = c.interval ?? 1.2;
    $('#cfgMaxChars').value = c.max_reply_chars ?? 300;
    $('#cfgQuiet').value = (c.quiet_hours || []).join(',');
    $('#cfgAllowSelf').checked = !!c.allow_self;
    $('#cfgGroupReply').checked = c.group_reply !== false;
    $('#cfgGroupAllowAll').value = (c.group_allow_all || []).join(',');

    const tag = $('#modelTag');
    tag.textContent = c.model ? c.model.split('/').pop() : '未配置';
    tag.className = 'tag ok';
  } catch (e) { toast('读取设置失败'); }
}

$('#btnSave').addEventListener('click', async () => {
  if (!S.bridge) return toast('后端未就绪');
  const p = {
    endpoint: $('#cfgEndpoint').value.trim(),
    model: $('#cfgModel').value.trim(),
    api_key: $('#cfgApiKey').value,
    temperature: parseFloat($('#cfgTemp').value) || 1.0,
    max_tokens: parseInt($('#cfgMaxTokens').value, 10) || 1600,
    interval: parseFloat($('#cfgInterval').value) || 1.2,
    max_reply_chars: parseInt($('#cfgMaxChars').value, 10) || 300,
    quiet_hours: $('#cfgQuiet').value.split(',')
      .map((s) => parseInt(s.trim(), 10))
      .filter((n) => Number.isInteger(n) && n >= 0 && n <= 23),
    allow_self: $('#cfgAllowSelf').checked,
    group_reply: $('#cfgGroupReply').checked,
    group_allow_all: $('#cfgGroupAllowAll').value
      .split(',').map((s) => s.trim()).filter(Boolean),
  };
  try {
    const r = JSON.parse(await S.bridge.save_config(JSON.stringify(p)));
    if (r.ok) {
      toast('已保存');
      const h = $('#saveHint');
      h.textContent = '已保存 ' + hhmmss();
      h.classList.add('show');
      setTimeout(() => h.classList.remove('show'), 2000);
      loadSettings();
    } else toast(r.error || '保存失败');
  } catch (e) { toast('保存失败'); }
});

/* ────────── 更新 ────────── */

async function checkUpdate(manual = false) {
  if (!S.bridge) return;
  try {
    const d = JSON.parse(await S.bridge.check_update());
    if (d.disabled) return;
    if (d.error) {
      if (manual) toast(d.error);
      return;
    }
    if (!d.has_update) {
      if (manual) toast(`已是最新（${d.latest || '未知'}）`);
      return;
    }
    // 有新版：亮出顶栏按钮
    $('#btnUpdate').hidden = false;
    $('#btnUpdate').title = `发现新版 ${d.latest}`;
    toast(`发现新版本 ${d.latest}`);
    S.updateInfo = d;
  } catch (e) {
    if (manual) toast('检查失败');
  }
}

$('#btnCheckUpdate').addEventListener('click', () => checkUpdate(true));

$('#btnUpdate').addEventListener('click', async () => {
  const d = S.updateInfo;
  if (!d) return toast('没有可用更新');
  const ok = confirm(
    `发现新版本 ${d.latest}\n\n` +
    `当前版本 ${d.current}\n\n` +
    `现在下载并安装？安装完成后需要手动关闭本程序。`);
  if (!ok) return;

  toast('开始下载…');
  const r = JSON.parse(await S.bridge.apply_update());
  if (r.ok) {
    toast('下载完成。关闭本程序后会自动替换并重启。');
  } else {
    toast(r.error || '更新失败');
  }
});

/* ────────── 后端桥接 ────────── */

function bootBridge() {
  if (typeof QWebChannel === 'undefined') return pushLog('QWebChannel 未加载', 'err');
  if (typeof qt === 'undefined' || !qt.webChannelTransport) {
    return pushLog('不在 Qt 容器里运行，界面进入离线模式', 'err');
  }
  new QWebChannel(qt.webChannelTransport, (channel) => {
    const b = channel.objects.backend;
    if (!b) return pushLog('后端对象未注册', 'err');
    onBridgeReady(b);
  });
}

async function onBridgeReady(b) {
  S.bridge = b;
  pushLog('后端已连接', 'in');

  if (b.log_message) b.log_message.connect((t, k) => pushLog(t, k));
  if (b.chat_event) {
    b.chat_event.connect((who, text, dir) => {
      const box = $('#ovRecent');
      if (box.querySelector('.empty')) box.innerHTML = '';
      const el = document.createElement('div');
      el.className = 'note-item';
      el.style.padding = '9px 0';
      el.style.borderBottom = '1px solid var(--border-soft)';
      el.innerHTML = `<div style="flex:1;min-width:0">
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:3px">
          <span class="memory-role ${dir === 'out' ? 'assistant' : ''}">${esc(who)}</span>
          <span class="memory-time">${dir === 'out' ? '回复' : '收到'}</span>
        </div>
        <div style="font-size:12px">${esc(text)}</div></div>`;
      box.prepend(el);
      while (box.children.length > 10) box.removeChild(box.lastChild);
    });
  }

  await loadSettings();
  await refreshStatus();
  setInterval(refreshStatus, 2000);

  // 版本号 + 启动时静默检查一次更新
  try {
    const v = JSON.parse(await b.get_version());
    setText('#aboutVersion', 'v' + (v.version || '?'));
  } catch (e) { /* 忽略 */ }
  setTimeout(() => checkUpdate(false), 2500);
}

window.addEventListener('DOMContentLoaded', () => {
  pushLog('界面已载入');
  bootBridge();
});
