/* ══════════════════════════════════════════════════════════════
   大肥鱼 · 界面逻辑
   ══════════════════════════════════════════════════════════════ */

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

// 顶层 const 不会挂到 window 上，但调试和自测需要能读到状态。
// 显式挂一份，代价是一个全局引用，收益是出问题时能在控制台直接看。
const S = {
  bridge: null,
  running: false,
  chats: [],
  watching: new Set(),
  soul: null,
  today: null,
  todayAsleep: false,
  journalPath: '',
};

window.S = S;

const PAGE_TITLES = {
  overview: '她的今天', chats: '会话', soul: '灵魂',
  mood: '心情', memory: '记忆', impression: '印象',
  stickers: '表情包', voice: '声音', data: '数据目录',
  plugins: '插件', settings: '设置', logs: '日志',
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

/* ── 未保存改动：切页前拦一道 ──
 *
 * 问题：声音 / 模型 / 灵魂 / 心情 / 插件 都是「填完点保存」，
 * 而 go() 直接切页、切过去还会重填 DOM。改了一半切走，输入静默消失，
 * 用户以为已经存上了。
 *
 * 判据用「用户动过」而不是「值和快照不同」：程序回填 value 不触发
 * input/change，所以不会误报；选择即保存的控件列进 AUTOSAVE 跳过。
 * chats 页不在这里管 —— 它有 watchDirty / scopeDirty，免得搜索框一打字就弹窗。
 */
const AUTOSAVE_IDS = new Set([
  'cfgGroupReply', 'cfgGroupAllowAll', 'cfgModelSel', 'cfgChannel',
  'tracePick', 'voiceDevice', 'memRole',
]);
const DIRTY_PAGES = new Set(['soul', 'mood', 'plugins', 'settings', 'voice']);
const PAGE_DIRTY = {};

function pageOf(el) {
  const p = el && el.closest ? el.closest('.page') : null;
  return (p && p.dataset.page) || '';
}

function markDirty(el) {
  if (!el || AUTOSAVE_IDS.has(el.id)) return;
  const page = pageOf(el);
  if (page && DIRTY_PAGES.has(page)) PAGE_DIRTY[page] = true;
}

function markClean(page) {
  if (page) PAGE_DIRTY[page] = false;
}

document.addEventListener('input', (e) => markDirty(e.target), true);
document.addEventListener('change', (e) => markDirty(e.target), true);

/** 每页的主保存按钮。chats 按脏源另算（监听列表 / 会话作用域是两件事）。 */
const PAGE_SAVE = {
  soul: 'btnSoulSave',
  mood: 'btnMoodSave',
  plugins: 'plugSave',
  settings: 'btnSave',
  voice: 'btnVoiceSave',
};

function pageHasDirty(page) {
  if (page === 'chats') {
    return !!(S.watchDirty || (typeof scopeDirty === 'function' && scopeDirty()));
  }
  return !!PAGE_DIRTY[page];
}

function pageSaveButtons(page) {
  if (page === 'chats') {
    return [
      S.watchDirty ? 'btnSaveWatch' : '',
      (typeof scopeDirty === 'function' && scopeDirty()) ? 'scopeSave' : '',
    ].filter(Boolean);
  }
  return [PAGE_SAVE[page] || ''].filter(Boolean);
}

function go(page) {
  const cur = pageOf(document.querySelector('.page.is-active'));
  if (cur && cur !== page && pageHasDirty(cur)) {
    const title = PAGE_TITLES[cur] || cur;
    const save = confirm(
      `「${title}」还有没保存的改动。\n\n`
      + '确定 = 先保存再离开\n'
      + '取消 = 不保存，直接离开');
    if (save) {
      // 保存函数在 click 时就采好了 payload，所以后面切页重填 DOM 不影响这次保存。
      pageSaveButtons(cur).forEach((id) => {
        const b = document.getElementById(id);
        if (b && !b.disabled) b.click();
      });
    }
    markClean(cur);
  }
  const pagesEl = $('#pages');
  if (pagesEl) pagesEl.scrollTop = 0;
  $$('.nav-item').forEach((b) => b.classList.toggle('is-active', b.dataset.page === page));
  $$('.page').forEach((p) => p.classList.toggle('is-active', p.dataset.page === page));
  setText('#breadcrumb', PAGE_TITLES[page] || page);

  if (page === 'overview') loadToday();
  if (page === 'soul' && !S.soul) loadSoul();
  if (page === 'mood') loadMood();
  if (page === 'memory') { loadMemoryStats(); loadMemory(''); loadMemorySessions(); loadMemoryHealth(); loadFacts(0); loadOwners(); loadPersons(); loadPoolSummary(); }
  if (page === 'impression') loadImpression();
  if (page === 'stickers') {
    if (!S.plugEnabled || !S.plugEnabled.sticker) {
      // 插件关着时这一页不该存在。跳回插件页，让用户先开它。
      toast('先在插件页启用「表情包」');
      go('plugins');
      return;
    }
    loadStickers();
  }
  if (page === 'voice') {
    if (!S.plugEnabled || !S.plugEnabled.voice) {
      toast('先在插件页启用「声音」');
      go('plugins');
      return;
    }
    loadVoice();
  }
  if (page === 'data') loadDataRoot();
  if (page === 'plugins') { closePlugin(); loadPlugins(); }
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
  model: ['模型接入', '选一家填 Key'],
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

function recentItemHtml(m) {
  const out = m.dir === 'out';
  const bubble = `<div class="recent-bubble ${out ? 'is-reply' : ''}">
    <span class="bubble-sender">${out ? '她' : esc(m.who || '对方')}</span>${esc(m.text || '')}
  </div>`;
  return `<div class="recent-item ${out ? 'is-out' : ''}">
    <div class="recent-meta">
      <span class="name">${esc(m.who || (out ? '她' : '对方'))}</span>
      <span class="time">${out ? '她的回复' : '收到'}</span>
    </div>
    ${out ? `<div class="recent-reply-row">${bubble}</div>` : bubble}
  </div>`;
}

function renderRecent(items) {
  const box = $('#ovRecent');
  if (!box) return;
  if (!items || !items.length) {
    box.innerHTML = `<div class="empty plan-empty">
      <p>还没有对话</p>
      <small>启动之后，微信里来消息就会出现在这里。</small>
    </div>`;
    setText('#ovRecentState', '等待消息');
    return;
  }
  box.innerHTML = `<div class="recent-list">${items.map(recentItemHtml).join('')}</div>`;
  setText('#ovRecentState', `最近 ${items.length} 条`);
  const foot = $('#ovRecentFoot');
  if (foot) {
    const last = foot.lastElementChild;
    if (last) last.textContent = '最后一条 ' + (items[0].who || '—');
  }
}

function renderTodayPlan(d) {
  const box = $('#ovPlanList');
  if (!box) return;
  const plan = (d.plan || []).filter((x) => x && x.what);
  const note = $('#ovPlanNote');
  const day = d.day || '';
  const planDay = (d.life && d.life.plan_day) || '';
  if (!plan.length) {
    box.innerHTML = `<div class="empty plan-empty">
      <p>今天还没有排日程</p>
      <small>她醒来后会自动排几件小事，至少一件接着昨天。</small>
    </div>`;
    if (note) note.textContent = '还没有今天的安排';
    return;
  }
  if (note) {
    note.textContent = !planDay
      ? '安排没有日期'
      : (planDay === day ? '今天的安排' : `安排来自 ${planDay}`);
  }
  const asleep = !!(d.life && d.life.asleep);
  const now = new Date();
  const minute = now.getHours() * 60 + now.getMinutes();
  const mins = plan.map((x) => {
    const m = /^(\d{1,2}):(\d{2})$/.exec(String(x.at || ''));
    return m ? Number(m[1]) * 60 + Number(m[2]) : -1;
  });
  const ends = plan.map((x) => {
    const m = /^(\d{1,2}):(\d{2})$/.exec(String(x.end || ''));
    return m ? Number(m[1]) * 60 + Number(m[2]) : -1;
  });
  let nowIdx = -1;
  plan.forEach((x, i) => {
    if (x.done || mins[i] < 0) return;
    // 有区间就按区间（start ≤ now < end），没有就退回"已经开始过"。
    // ⚠ 这里的正则以前写成 \d（双重转义），匹配的是字面反斜杠 ——
    //   于是 mins 全是 -1，"现在在做"这个标记从上线起就没亮过。
    const hit = ends[i] > mins[i]
      ? (minute >= mins[i] && minute < ends[i])
      : (mins[i] <= minute);
    if (hit) nowIdx = i;
  });
  box.innerHTML = plan.map((x, i) => {
    // 睡着了就不存在“现在在做”这件事；手头的事由 life.doing 单独说。
    const isNow = !asleep && i === nowIdx;
    const state = x.done ? (x.done_by === 'elapsed' ? '已过去' : '做完了') : (isNow ? '现在在做' : '');
    const from = x.from === 'yesterday' ? '接着昨天' : x.from === 'fact' ? '来自记得的事' : '她自己排的';
    // 有区间就显示 "09:00–09:50"：一天里每件事有起止，才看得出她不是
    // 同时在做三件事。旧数据没 end，就只显示开始时刻。
    const when = x.end ? `${x.at}–${x.end}` : (x.at || '—');
    return `<div class="plan-item ${x.done ? 'is-done' : ''} ${isNow ? 'is-now' : ''}">
      <span class="plan-time">${esc(when)}</span>
      <span class="plan-dot"></span>
      <div class="plan-copy"><div class="plan-title">${esc(x.what)}</div><div class="plan-meta">${from}</div></div>
      <span class="plan-state">${state}</span>
    </div>`;
  }).join('');
}

function renderToday(d) {
  if (!d || !d.ok) return;
  S.today = d;
  const life = d.life || {};
  const mood = d.mood || {};
  const weather = d.weather || {};
  const journal = d.journal || {};
  const afterglow = d.afterglow || {};
  const greeting = d.greeting || '';
  const asleep = !!life.asleep;
  const condition = life.condition || '生活状态未知';
  // 条件里已经有「（精力 78）」时不再把同一个数塞进两个 chip。
  const conditionShort = String(condition).replace(/（[^）]*精力[^）]*）/g, '').trim();

  setText('#ovGreeting', asleep
    ? `${greeting}，她睡着了。`
    : `${greeting}，她正在过自己的今天。`);
  setText('#ovCondition', conditionShort);
  setText('#ovEnergy', life.energy != null ? `精力 ${life.energy}` : '精力 —');
  setText('#ovWake', asleep
    ? (life.wake_at ? `约 ${life.wake_at} 醒` : '睡眠 —')
    // 醒着时报「今天几点醒的」。不能用 wake_at —— 它在醒来后已经被
    // 重算成明早，显示出来会变成"她明早 09:47 醒的"，读着完全不通。
    : (life.just_woke_at ? `${life.just_woke_at} 醒` : (life.wake_at ? `醒来 ${life.wake_at}` : '睡眠 —')));
  S.todayAsleep = asleep;
  setText('#ovDoingLabel', asleep ? '此刻' : '此刻在做');
  setText('#ovDoing', asleep ? '她已经睡了' : (life.doing || '还没有写下手头的事'));
  setText('#ovDoingHint', asleep
    ? (life.wake_at ? `约 ${life.wake_at} 醒` : '睡着')
    : (life.doing ? '来自生活状态' : '—'));
  setText('#ovTodayLabel', `${d.day || '—'} · 周${d.weekday || '—'}`);
  setText('#ovMoodScore', mood.value != null ? `${mood.value} · ${mood.label || ''}` : '—');
  // 「为什么」这一行是这张卡最关键的一格：只有分数是仪表盘，
  // 有原因才是有人在里面。reason 由 mood_judge 在每次判定后写。
  setText('#ovMoodWhy', mood.reason ? String(mood.reason).slice(0, 30) : '—');
  setText('#ovMoodLabel', mood.value != null ? `心情 ${mood.value} · ${mood.label || ''}` : '心情 —');
  setText('#ovAfterglowCount', afterglow.count ? `${afterglow.count} 条` : '无');
  // 两处都取 afterglow.text 会同一段话出现两次；右侧只给“有没有余韵”的
  // 摘要，原文留在「今天的余韵」卡里。
  setText('#ovQuietNote', afterglow.count
    ? '上一轮余韵还在，原文在「今天的余韵」。'
    : (weather.line ? `今天没有额外余韵。${weather.city || '本地'}：${weather.line}` : '今天没有额外余韵。'));
  setText('#ovAfterglow', afterglow.text || '还没有额外的感知；日子、天气和余韵会在这里出现。');
  const moodReasonBtn = $('#ovMoodReason');
  if (moodReasonBtn) moodReasonBtn.title = mood.reason || '去心情页看这句话的来源';
  setText('#ovMetaRuntime', weather.line
    ? `${weather.city || '本地'} · ${weather.line}`
    : '本机生活状态');
  $('#ovMetaRuntime')?.classList.remove('is-error');

  const ww = $('#ovWeatherWrap');
  if (ww) {
    ww.hidden = !weather.line;
    if (weather.line) {
      const parts = String(weather.line).split(/[，,]/);
      setText('#ovWeatherValue', parts[0] || weather.line);
      setText('#ovWeatherCity', weather.city || '');
    }
  }

  const jday = journal.day || '';
  setText('#ovJournalDate', jday || '还没有日记');
  setText('#ovJournal', journal.text
    ? String(journal.text).replace(/^#+\\s*/gm, '').replace(/\\*\\*/g, '').slice(0, 400)
    : '还没有日记。睡前写下的那篇只留在本机。');
  S.journalPath = journal.path || '';

  renderTodayPlan(d);
  renderRecent(d.recent || []);
}

async function loadToday() {
  if (!S.bridge || !S.bridge.get_today) return;
  try {
    const d = JSON.parse(await S.bridge.get_today());
    if (d.ok) {
      renderToday(d);
    } else {
      S.todayAsleep = false;
      setText('#ovMetaRuntime', '生活状态读取失败');
      $('#ovMetaRuntime')?.classList.add('is-error');
    }
  } catch (e) {
    // 不要静默：陈旧内容 + 每 2 秒“更新于”会让人以为数据是新的。
    S.todayAsleep = false;
    setText('#ovMetaRuntime', '生活状态读取失败');
    $('#ovMetaRuntime')?.classList.add('is-error');
  }
}

function tickClock() {
  const d = new Date();
  const wd = '日一二三四五六'[d.getDay()];
  setText('#appDate', `${d.getMonth() + 1}月${d.getDate()}日 周${wd}`);
  setText('#appClock', [d.getHours(), d.getMinutes()]
    .map((n) => String(n).padStart(2, '0')).join(':'));
}

$('#btnRefreshOverview')?.addEventListener('click', async () => {
  await refreshStatus();
  await loadToday();
  toast('已刷新');
});

$('#ovJournalOpen')?.addEventListener('click', async () => {
  if (!S.bridge || !S.journalPath) return toast('还没有可打开的日记');
  try {
    const r = JSON.parse(await S.bridge.open_path(S.journalPath));
    if (r && r.error) toast(r.error);
  } catch (e) { toast('打开失败'); }
});

$('#ovMoodReason')?.addEventListener('click', () => go('mood'));

async function refreshStatus() {
  if (!S.bridge) return;
  try {
    const s = JSON.parse(await S.bridge.get_status());
    S.running = !!s.running;

    const pb = $('#btnPower');
    if (pb) {
      pb.classList.toggle('is-on', s.running);
      const label = pb.querySelector('span');
      if (label) label.textContent = s.running ? '停止' : '启动';
    }

    // 运行中但发送通道不可用 = 只监听不发送。必须当场说，
    // 否则用户以为启动成功了，等她回话等到天亮（2026-11-08 现场）。
    const sendDown = !!s.running && !s.dry && s.sender === false;
    setText('#ovState', s.running
      ? (sendDown ? '运行中 · 发不出去' : '运行中')
      : (s.error ? '出错了' : '未启动'));
    setText('#ovStateHint', s.running
      ? (sendDown
          ? (s.error || '微信窗口没打开：现在只监听、不发送。打开微信窗口后会自动恢复。')
          : (S.todayAsleep
              ? '她已经睡了；这会儿不会主动开口。'
              : '她在听着。微信里有人说话就会回。'))
      : (s.error || '登录并选择会话后，点右侧按钮启动。'));
    setText('#statusText', s.running
      ? (sendDown ? '发不出去' : '运行中')
      : (s.error ? '出错' : '未启动'));
    setDot(s.running && !sendDown ? 'on' : (s.error || sendDown ? 'err' : ''));
    const runDot = $('#ovRunDot');
    if (runDot) {
      runDot.className = 'live-dot ' + (s.running && !sendDown ? 'is-ok' : (s.error || sendDown ? 'is-err' : 'is-off'));
      const rs = runDot.closest('.run-state');
      if (rs) {
        rs.classList.toggle('is-running', !!s.running && !sendDown);
        rs.classList.toggle('is-error', !!(s.error || sendDown));
      }
    }

    const tag = $('#ovPowerTag');
    if (tag) {
      tag.textContent = s.running
        ? (sendDown ? '只监听 · 不发送' : `监听 ${s.watching || 0} 个会话`)
        : '等待启动';
      tag.className = 'tag' + (s.running && !sendDown ? ' ok' : '');
    }

    setText('#ovRecv', String(s.recv ?? 0));
    setText('#ovReply', String(s.reply ?? 0));
    setText('#ovWatch', String(s.watching ?? 0));
    setText('#ovUptime', s.running ? (s.uptime || '0秒') : '—');
    // 账号标签统一由 refreshAccountLabel 管：它调 current_account，
    // 能拿到"未选择"、wxid、昵称三种状态。这里不再直接用 s.account ——
    // 那个字段在没打开数据库时是空的，会把标签刷成「—」。
    refreshAccountLabel();
    setText('#ovUpdated', '更新于 ' + hhmmss());

    if (s.diagnose) renderChecks(s.diagnose);
    // 最近对话只由 get_today 统一绘制。status.recent 的截断长度不同，
    // 每 2 秒重画会让列表在 4 条 / 12 条之间跳变。

    // 补日志：信号可能因为界面还没连上而丢失，这里按 seq 补齐
    if (s.logs && s.logs.length) {
      for (const l of s.logs) {
        if (l.seq <= (S.lastLogSeq || 0)) continue;
        pushLog(l.text, l.kind);
        S.lastLogSeq = l.seq;
      }
    }

    // 概览右侧摘要
    const sum = $('#ovSummary');
    if (sum) {
      if (s.running) {
        sum.innerHTML = `<div class="quiet-row"><span>正在监听</span><strong>${s.watching || 0} 个会话</strong></div>
          <div class="quiet-row"><span>发送通道</span><strong>${sendDown ? '只监听' : '就绪'}</strong></div>`;
      } else {
        sum.innerHTML = `<p class="journal-excerpt">启动后这里会显示正在监听的会话。</p>`;
      }
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

$('#btnRefresh').addEventListener('click', () => {
  refreshStatus();
  if ($('.page[data-page="overview"]')?.classList.contains('is-active')) loadToday();
  toast('已刷新');
});
$('#btnRecheck').addEventListener('click', async () => {
  if (!S.bridge) return;
  const d = JSON.parse(await S.bridge.diagnose());
  renderChecks(d.items || []);
  toast('检测完成');
});
$('#btnDiagnose').addEventListener('click', () => $('#btnRecheck').click());
$('#btnOpenWechat').addEventListener('click', async () => {
  if (!S.bridge) return;
  try {
    // open_wechat 现在返回 {ok, msg}：微信收在托盘时 ShowWindow 可能只叫出
    // 一个"空壳窗口"（点不动、X 也关不掉），必须把恢复路径说给用户。
    const r = JSON.parse(await S.bridge.open_wechat());
    toast(r.msg || (r.ok ? '已尝试打开微信' : '没找到微信窗口'));
  } catch (e) {
    toast('打开微信失败');
  }
});

/* ────────── 会话 ────────── */

function renderChats() {
  const box = $('#chatList');
  const tabs = $('#chatTabs');
  setText('#chatCount', `${S.chats.length} 个`);

  if (!S.chats.length) {
    if (tabs) tabs.innerHTML = '';
    box.innerHTML = `<div class="empty">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <path d="M4 6h16v10H8l-4 4V6z"/></svg>
      <h3>还没有加载会话</h3>
      <p>点右上角「刷新会话」，从你的微信里读取列表</p></div>`;
    return;
  }

  /* 分组做成顶部的一排小板块。
   *
   * 以前是纵向分组标题 —— 180 条里联系人 94 个铺满屏，
   * 想切到群聊得滚过整屏。现在横向排一排，点哪个看哪个。
   */
  const GROUPS = [
    { key: 'contact', label: '联系人' },
    { key: 'group', label: '群聊' },
    { key: 'official', label: '公众号' },
    { key: 'system', label: '系统' },
  ];
  const counts = {};
  for (const c of S.chats) {
    const k = c.kind || 'contact';
    counts[k] = (counts[k] || 0) + 1;
  }
  // 当前选中的组：没选或该组为空时落到第一个有内容的组
  let active = S.chatTab || '';
  if (!active || !counts[active]) {
    active = (GROUPS.find((g) => counts[g.key]) || GROUPS[0]).key;
  }
  S.chatTab = active;

  if (tabs) {
    tabs.innerHTML = GROUPS.filter((g) => counts[g.key]).map((g) => {
      const on = g.key === active ? ' is-active' : '';
      // 该组里已勾选几个，让用户一眼看出"这组有在用的"
      const picked = S.chats.filter(
        (c) => (c.kind || 'contact') === g.key && S.watching.has(c.username)).length;
      return `<button class="chat-tab${on}" data-tab="${g.key}">
        <span>${esc(g.label)}</span>
        <span class="chat-tab-count">${counts[g.key]}</span>
        ${picked ? `<span class="chat-tab-picked">${picked}</span>` : ''}
      </button>`;
    }).join('');
    $$('#chatTabs [data-tab]').forEach((b) => {
      b.addEventListener('click', () => { S.chatTab = b.dataset.tab; renderChats(); });
    });
  }

  const KIND_LABEL = { contact: '私聊', group: '群聊', official: '公众号', system: '系统' };
  const rows = S.chats.filter((c) => (c.kind || 'contact') === active);
  box.innerHTML = rows.map((c) => {
    const on = S.watching.has(c.username);
    const initial = (c.name || '?').trim().charAt(0).toUpperCase();
    const kind = KIND_LABEL[c.kind || 'contact'] || '会话';
    const unread = c.unread ? `${c.unread > 99 ? '99+' : c.unread} 条未读` : '没有未读';
    return `<div class="chat-row${on ? ' is-on' : ''}" data-chat="${esc(c.username)}" role="button" aria-pressed="${on}">
      <span class="chat-avatar">${esc(initial)}</span>
      <div class="chat-main">
        <div class="chat-name">${esc(c.name)}<span class="chat-kind">${kind}</span></div>
        <div class="chat-sub">${unread}</div>
      </div>
      <span class="chat-type">${kind}</span>
      <span class="chat-scope">${on ? '持续监听' : '未监听'}</span>
      <button class="chat-gear" data-gear="${esc(c.username)}"
              title="这个会话的单独设置" aria-label="这个会话的单独设置">⚙</button>
      <span class="chat-toggle" aria-hidden="true"></span>
    </div>`;
  }).join('');

  $$('#chatList [data-chat]').forEach((row) => {
    row.addEventListener('click', () => {
      const uid = row.dataset.chat;
      if (S.watching.has(uid)) S.watching.delete(uid);
      else S.watching.add(uid);
      S.watchDirty = true;
      renderChats();
      updateWatchBar();
    });
  });

  /* ⚙ 单独绑定：它长在行里面，但点它**不能**顺手切换监听开关。
   *
   * 没有 stopPropagation 的话，点齿轮 = 把会话勾上/取消，用户还没
   * 看到弹层就先改了一个他没打算改的东西。
   */
  $$('#chatList [data-gear]').forEach((btn) => {
    btn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const uid = btn.dataset.gear;
      const chat = S.chats.find((c) => c.username === uid) || {};
      openScope(uid, chat.name || uid);
    });
  });
  updateWatchBar();
}

/* ────────── 会话级配置（C-1 三层作用域） ──────────
 *
 * 每个会话可以单独覆盖五个行为开关：
 *   allow_self / group_reply / group_allow_all / max_reply_chars / quiet_hours
 *
 * 界面上每一项都标「本会话」还是「继承自全局」——
 * 不标的话，用户改完全局、发现这个会话没变（因为这里覆盖过），
 * 就会以为"改了没生效"。反过来也一样。
 */

/* `orig` 是打开时（或上次保存后）的快照，用来判断"有没有未保存的修改"。
 * 没有它的话，用户点完「继承」看不出这一项还没落盘。 */
const SCOPE = { chatId: '', name: '', fields: [], draft: {}, orig: {}, problems: [] };

const clone = (v) => JSON.parse(JSON.stringify(v));

async function openScope(chatId, name) {
  if (!S.bridge) return toast('还没连上后端');
  SCOPE.chatId = chatId;
  SCOPE.name = name;
  setText('#scopeTitle', '会话配置 · ' + name);
  setText('#scopeSub', '只对这个会话生效；没设的跟随上一层。');
  setText('#scopeHint', '读取中…');
  $('#scopeHint').className = 'hint-line';
  $('#scopeBody').innerHTML = '';
  $('#scopeMask').hidden = false;
  try {
    const d = JSON.parse(await S.bridge.get_chat_scope(chatId));
    if (!d.ok) {
      setText('#scopeHint', d.error || '读取失败');
      $('#scopeHint').className = 'hint-line is-warn';
      return;
    }
    SCOPE.fields = d.fields || [];
    SCOPE.problems = d.problems || [];
    SCOPE.draft = {};
    for (const f of SCOPE.fields) {
      SCOPE.draft[f.key] = f.overridden ? f.value : null;
    }
    SCOPE.orig = clone(SCOPE.draft);
    renderScope();
  } catch (e) {
    setText('#scopeHint', '读取失败：' + e);
    $('#scopeHint').className = 'hint-line is-warn';
  }
}

function closeScope() {
  $('#scopeMask').hidden = true;
  SCOPE.chatId = '';
}

/* 一项的"现在是谁在生效"。
 *
 * 草稿是 null（跟随）时，说**它会从哪来** —— 注意不是"当前生效层"：
 * 一个本来覆盖过的字段，用户点了「继承」，此时该说的是"将继承自全局"，
 * 而不是它还带着的"本会话"。
 */
function scopeSourceLabel(f) {
  if (SCOPE.draft[f.key] === null) return f.inherit_label || f.layer_label;
  return '本会话';
}

function scopeBoolRow(f) {
  const v = SCOPE.draft[f.key];
  const pick = (val, text) => {
    const on = (val === 'inherit' && v === null)
      || (val === 'true' && v === true)
      || (val === 'false' && v === false);
    return `<button class="seg${on ? ' is-on' : ''}"
      data-scope="${esc(f.key)}" data-val="${val}">${text}</button>`;
  };
  return `<div class="scope-row">
    <div class="scope-label">
      <span>${esc(f.label)}</span>
      <span class="scope-src">${esc(scopeSourceLabel(f))}</span>
    </div>
    <div class="scope-ctl seg-group">
      ${pick('inherit', '继承')}${pick('true', '开')}${pick('false', '关')}
    </div>
  </div>`;
}

/* 值字段（数字 / 列表）。
 *
 * ⚠ 输入框**永远可编辑**。曾经的做法是"继承时禁用"，配上一个只能
 *   切回继承的按钮 —— 结果是一个继承中的字段永远改不成自己设的值，
 *   整条路是死的。现在改成：直接改就是覆盖，「继承」按钮负责撤销覆盖。
 */
function scopeValueRow(f) {
  const v = SCOPE.draft[f.key];
  const inherit = v === null;
  const raw = inherit ? (f.inherit_value ?? f.value) : v;
  const shown = Array.isArray(raw) ? raw.join(', ') : (raw ?? '');
  const type = f.kind === 'int' ? 'number' : 'text';
  // 占位符跟着字段走（后端 FIELDS 里声明），前端不再按类型瞎猜
  const ph = f.placeholder || (f.kind === 'int' ? '例如 300' : '');
  return `<div class="scope-row">
    <div class="scope-label">
      <span>${esc(f.label)}</span>
      <span class="scope-src">${esc(scopeSourceLabel(f))}</span>
    </div>
    <div class="scope-ctl value-ctl">
      <input class="input scope-input" data-scope-input="${esc(f.key)}"
             type="${type}" value="${esc(shown)}"
             placeholder="${esc(ph)}">
      <button class="seg${inherit ? '' : ' is-on'}"
              data-scope="${esc(f.key)}" data-val="inherit"
              ${inherit ? 'disabled title="当前就跟随上一层"' : 'title="恢复跟随上一层"'}>继承</button>
    </div>
  </div>`;
}

function renderScope() {
  const body = $('#scopeBody');
  body.innerHTML = SCOPE.fields.map((f) => (
    f.kind === 'bool' ? scopeBoolRow(f) : scopeValueRow(f)
  )).join('');

  // 布尔三态 + 值字段的「继承」按钮
  //
  // ⚠ 不管当前是不是 disabled 都要绑上。曾经在这里写了
  //   `if (b.disabled) return;`，而「继承中」的撤销按钮天生是 disabled，
  //   于是它的监听器从来没绑过 —— 用户改了值再想撤销，点不动。
  $$('#scopeBody [data-scope]').forEach((b) => {
    b.addEventListener('click', () => {
      if (b.disabled) return;      // 真点了也算：disabled 时浏览器本来就不派发
      const key = b.dataset.scope;
      const val = b.dataset.val;
      const f = SCOPE.fields.find((x) => x.key === key);
      if (!f) return;
      if (val === 'inherit') {
        SCOPE.draft[key] = null;
      } else if (f.kind === 'bool') {
        SCOPE.draft[key] = (val === 'true');
      } else {
        // 从"继承"切到"自己设"：先用继承来的值打底，别给一个空框
        const base = f.inherit_value ?? f.value;
        SCOPE.draft[key] = Array.isArray(base) ? [...base] : base;
      }
      renderScope();
    });
  });

  // 值输入：改就是覆盖，不用先点按钮
  $$('#scopeBody [data-scope-input]').forEach((inp) => {
    inp.addEventListener('input', () => {
      const key = inp.dataset.scopeInput;
      const f = SCOPE.fields.find((x) => x.key === key);
      if (!f) return;
      if (f.kind === 'int') {
        const n = parseInt(inp.value, 10);
        SCOPE.draft[key] = Number.isFinite(n) && n >= 0 ? n : null;
      } else {
        SCOPE.draft[key] = inp.value.split(',')
          .map((s) => s.trim()).filter(Boolean);
      }
      // 只更新来源标签与「继承」按钮，不重绘整块（重绘会把光标弄丢）
      const rowEl = inp.closest('.scope-row');
      const src = rowEl?.querySelector('.scope-src');
      if (src) src.textContent = scopeSourceLabel(f);
      const back = rowEl?.querySelector('[data-val="inherit"]');
      if (back) {
        const nowInherit = SCOPE.draft[key] === null;
        back.classList.toggle('is-on', !nowInherit);
        back.disabled = nowInherit;
      }
    });
  });

  updateScopeDirty();
  if (SCOPE.problems.length) {
    $('#scopeHint').className = 'hint-line is-warn';
    setText('#scopeHint', SCOPE.problems.map((p) => p[1]).join('；'));
  }
}

function scopeDirty() {
  return JSON.stringify(SCOPE.draft) !== JSON.stringify(SCOPE.orig);
}

function updateScopeDirty() {
  const hint = $('#scopeHint');
  const btn = $('#scopeSave');
  if (!hint || !btn) return;
  const dirty = scopeDirty();
  btn.disabled = !dirty;
  btn.textContent = dirty ? '保存' : '已保存';
  if (SCOPE.problems.length) return;   // 诊断信息优先占位
  hint.className = 'hint-line' + (dirty ? ' is-warn' : '');
  setText(hint, dirty ? '有未保存的修改' : '');
}

async function saveScope() {
  if (!S.bridge || !SCOPE.chatId) return;
  const btn = $('#scopeSave');
  if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
  try {
    const d = JSON.parse(await S.bridge.save_chat_scope(
      SCOPE.chatId, JSON.stringify(SCOPE.draft)));
    if (!d.ok) {
      toast(d.error || '保存失败');
      return;
    }
    SCOPE.fields = d.fields || [];
    SCOPE.draft = {};
    for (const f of SCOPE.fields) {
      SCOPE.draft[f.key] = f.overridden ? f.value : null;
    }
    SCOPE.orig = clone(SCOPE.draft);
    renderScope();
    // 下一条消息就按新值走 —— 不用重启，也不用重开弹层
    toast('已保存 · 下一条消息就生效');
  } catch (e) {
    toast('保存失败：' + e);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '保存'; }
  }
}


/* 底部：已选多少个 + 保存按钮。
 *
 * 问题 3 说的是"没有保存配置的功能"。之前每点一下就写盘（syncWatching），
 * 表面上看是"自动保存了"，但用户看不到任何"已保存"的确认，
 * 也不知道改了多久生效 —— 他以为根本没保存。
 * 现在改成：改动只进内存（标记 dirty），点保存或切页才落盘，
 * 并明确显示"有 N 个未保存的改动"。
 */
function updateWatchBar() {
  setText('#watchCount', S.watching.size + ' 个');
  const dirty = !!S.watchDirty;
  const btn = $('#btnSaveWatch');
  if (btn) {
    btn.disabled = !dirty;
    btn.textContent = dirty ? '保存修改' : '已保存';
    btn.classList.toggle('btn-primary', dirty);
  }
  const top = $('#btnSaveWatchTop');
  if (top) {
    top.disabled = !dirty;
    top.textContent = dirty ? '保存修改' : '已保存';
    top.classList.toggle('btn-primary', dirty);
    top.classList.toggle('btn-secondary', !dirty);
  }
  const hint = $('#watchHint');
  if (hint) {
    hint.textContent = S.watchDirty
      ? '有未保存的修改'
      : (S.watching.size ? '这 ' + S.watching.size + ' 个会话会持续监听' : '还没有选择要监听的会话');
    hint.className = 'hint-line' + (S.watchDirty ? ' is-warn' : '');
  }
}

async function saveWatch() {
  if (!S.bridge) return;
  const btn = $('#btnSaveWatch');
  if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
  try {
    const r = JSON.parse(await S.bridge.set_watching(
      JSON.stringify([...S.watching])));
    if (r.error) return toast(r.error);
    S.watchDirty = false;
    toast('已保存 · 这 ' + S.watching.size + ' 个会话会持续监听'
          + (r.hot ? '（已即时生效，不用重启）' : ''));
    updateWatchBar();
  } catch (e) {
    toast('保存失败');
    updateWatchBar();
  }
}

async function syncWatching() {
  if (!S.bridge) return;
  try {
    await S.bridge.set_watching(JSON.stringify([...S.watching]));
  } catch (e) { toast('同步失败'); }
}

/* 当前账号：左下角必须一直显示"我是谁"。
 *
 * 修复前那里读的是 contact 表里自己的 nick_name，实际值是
 * '\x7f\x7f\x7f\x7f'（四个 DEL 控制字符）—— 界面上就是一块空白，
 * 用户根本不知道现在连着哪个微信。
 */
async function refreshAccountLabel() {
  if (!S.bridge || !S.bridge.current_account) return;
  try {
    const d = JSON.parse(await S.bridge.current_account());
    const el = $('#accountLabel');
    const hero = $('#ovAccountChip');
    if (!el) return;
    if (d.open) {
      const show = d.nick || d.wxid || d.account;
      el.textContent = show + (d.nick && d.nick !== d.wxid ? '' : '');
      el.title = 'wxid: ' + (d.wxid || d.account);
      if (hero) hero.textContent = show + ' · 当前账号';
    } else {
      el.textContent = '未选择账号';
      el.title = '点「选择账号」挑一个微信';
      if (hero) hero.textContent = '还没有选择微信账号';
    }
  } catch (e) { /* 静默 */ }
}

/* 账号选择弹窗 —— 多开微信时不让系统替你选 */
async function pickAccount() {
  if (!S.bridge) return toast('后端未就绪');
  let d;
  try {
    d = JSON.parse(await S.bridge.list_accounts());
  } catch (e) { return toast('读取账号失败'); }
  if (d.error) return toast(d.error);
  const list = d.accounts || [];
  S.accountList = list;
  const modal = $('#accountModal');
  const box = $('#accountList');
  if (!modal || !box) return toast('界面缺少账号弹窗');
  if (!list.length) {
    box.innerHTML = '<div class="empty">' +
      '<h3>没有正在运行的微信</h3>' +
      '<p>这个列表只显示<b>已经启动</b>的微信账号 —— ' +
      '连一个没启动的号没有意义，它的消息读不到、也发不出去。<br>' +
      '请先打开微信并登录，再回来点「选择账号」。</p></div>';
  } else {
    // 每行只留必要信息：昵称、wxid、最后活跃。
    // 不再有头像圈（只是一个装饰点，占地方）、
    // 不再有改名按钮（暂时不需要）。
    box.innerHTML = list.map((a) => {
      const title = a.nick || a.wxid;
      const sub = a.nick ? a.wxid + ' · ' : '';
      return `<div class="session-item account-row" data-acc="${esc(a.account)}">
        <div class="session-main">
          <div class="session-name">${esc(title)}
            <span class="tag ok" style="margin-left:8px">● 正在运行</span>
          </div>
          <div class="session-sub">${esc(sub)}最后活跃 ${esc(a.lastActivity)}</div>
        </div>
      </div>`;
    }).join('');
  }
  modal.classList.add('is-open');
}

async function chooseAccount(acc) {
  const modal = $('#accountModal');
  if (modal) modal.classList.remove('is-open');
  try {
    const r = JSON.parse(await S.bridge.open_account(acc));
    if (!r.ok) return toast(r.error || '打开失败');
    toast('已连接：' + (r.nick || r.wxid || acc));
    if (r.restored) {
      // 这个号上次勾过会话 —— 自动恢复，不用重勾
      S.watching = new Set(r.chats || []);
      S.watchDirty = false;
      toast('已恢复上次的 ' + S.watching.size + ' 个会话');
    } else {
      S.watching = new Set();
      S.watchDirty = false;
    }
    await refreshAccountLabel();
    await doLoadChats();
  } catch (e) { toast('打开失败'); }
}

async function doLoadChats() {
  if (!S.bridge) return toast('后端未就绪');
  const btn = $('#btnLoadChats');
  if (btn) { btn.disabled = true; btn.textContent = '读取中…'; }
  try {
    const cur = JSON.parse(await S.bridge.current_account());
    if (!cur.open) {
      toast('先选一个微信账号');
      return pickAccount();
    }
    const d = JSON.parse(await S.bridge.list_chats(300));
    if (d.error) return toast(d.error);
    S.chats = d.chats || [];
    // ⚠ 后端是唯一真相。原来是「只加不减」——换过账号之后，上一个号的
    //   勾选会残留在 S.watching 里，保存时又被写回后端。
    //   有未保存的改动（watchDirty）时才保留本地状态，否则整体替换。
    if (!S.watchDirty) {
      S.watching = new Set(
        S.chats.filter((c) => c.watching).map((c) => c.username));
    }
    renderChats();
    toast(`读到 ${S.chats.length} 个会话`);
  } catch (e) {
    toast('加载失败');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '刷新会话'; }
  }
}

$('#btnPickAccount').addEventListener('click', pickAccount);
if ($('#btnSaveWatch')) $('#btnSaveWatch').addEventListener('click', saveWatch);
if ($('#btnSaveWatchTop')) $('#btnSaveWatchTop').addEventListener('click', saveWatch);
$('#btnLoadChats').addEventListener('click', doLoadChats);
document.addEventListener('click', (e) => {
  const a = e.target.closest('[data-acc]');
  if (a) { chooseAccount(a.dataset.acc); return; }
  if (e.target.closest('#accountModalClose') || e.target.id === 'accountModal') {
    const m = $('#accountModal');
    if (m) m.classList.remove('is-open');
  }
});

$('#cfgGroupReply').addEventListener('change', saveQuick);
$('#cfgGroupAllowAll').addEventListener('change', saveQuick);

/* 会话配置弹层。
 *
 * 点遮罩和按 Esc 都关；「全部恢复继承」只改草稿，仍然要点保存才落盘 ——
 * 和这个项目其它地方一致（D-35：保存必须显式，系统不替你写文件）。
 */
if ($('#scopeClose')) {
  $('#scopeClose').addEventListener('click', closeScope);
  $('#scopeSave').addEventListener('click', saveScope);
  $('#scopeInheritAll').addEventListener('click', () => {
    for (const f of SCOPE.fields) SCOPE.draft[f.key] = null;
    renderScope();
    toast('已全部改为跟随上一层 · 点保存生效');
  });
  $('#scopeMask').addEventListener('click', (e) => {
    if (e.target.id === 'scopeMask') closeScope();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('#scopeMask').hidden) closeScope();
  });
}

async function saveQuick() {
  if (!S.bridge) return;
  const cur = JSON.parse(await S.bridge.get_config());
  cur.group_reply = $('#cfgGroupReply').checked;
  cur.group_allow_all = $('#cfgGroupAllowAll').value
    .split(',').map((s) => s.trim()).filter(Boolean);
  await S.bridge.save_config(JSON.stringify(cur));
  toast('已保存');
}

/* ────────── 灵魂 ──────────
   可读写。写入语义与 dsh-bigfish 插件 /dsh-bigfish/soul 的 POST 完全一致：
   revision 并发检查、locked 价值保护、reset 回出厂。
   界面上改一项就更新内存草稿，点「保存人格」才落盘 —— 不做自动保存，
   因为人格是整卡结构，中途自动保存会把写一半的状态推给下一轮对话。
*/

const TRAIT_DOC = {
  warmth: { label: '温暖', low: '克制，不主动靠近', mid: '关心，但不黏人', high: '明显关心，会主动问候' },
  assertiveness: { label: '主见', low: '倾向附和，很少反驳', mid: '有想法，但不硬顶', high: '明确表达不同意见，会讲理由' },
  playfulness: { label: '顽皮', low: '认真，少开玩笑', mid: '该认真时认真，平时会贫两句', high: '日常爱逗，会起哄' },
  directness: { label: '直接', low: '含蓄，话留三分', mid: '不绕太远，但会顾着分寸', high: '有话直说，少铺垫' },
};

/* 精神上对齐 vault.trait_sentence：<40 低档 / >60 高档 / 其余中档 */
const traitBand = (v) => (v <= 40 ? 'low' : v >= 60 ? 'high' : 'mid');
const traitSentence = (k, v) => {
  const d = TRAIT_DOC[k];
  return d ? `${d.label}：${d[traitBand(v)]}。` : '';
};

async function loadSoul() {
  if (!S.bridge) return;
  try {
    const d = JSON.parse(await S.bridge.get_persona());
    if (d.error && !d.scopeReady) {
      toast(d.error);
      setText('#soulPreview', d.error);
      return;
    }
    S.soul = d;
    renderSoul(d);
  } catch (e) {
    toast('读取灵魂失败');
  }
}

function renderSoul(d) {
  setText('#soulRev', `rev ${d.revision || 1}`);
  setText('#soulName', d.name || '—');
  setText('#soulAddress', d.address || '—');
  $('#soulValueCount').innerHTML = `${(d.values || []).length}<small>条</small>`;
  setText('#soulPathTag', d.path ? d.path.split(/[\\/]/).pop() : '—');
  $('#soulPathTag').title = d.path || '';

  $('#editName').value = d.name || '';
  $('#editAddress').value = d.address || '';
  $('#editAliases').value = d.aliases || '';
  $('#editWhoIAm').value = d.whoIAm || '';
  setText('#whoCount', String((d.whoIAm || '').length));

  renderValues(d.values || []);
  renderTraits(d.traits || {});
  renderNotes(d.selfNotes || []);

  setText('#soulPreview', d.persona || '（空）');
  $('#soulWhoIAm').innerHTML = (d.whoIAm || '（空）')
    .split(/\n{2,}/).filter(Boolean)
    .map((p) => `<p>${esc(p)}</p>`).join('');

  setText('#soulHint', d.exists ? '' : '这个范围还没有人格，正在用默认值。改完保存就会落盘。');
}

function renderValues(values) {
  const box = $('#soulValues');
  if (!values.length) {
    box.innerHTML = '<div class="empty" style="padding:12px 0"><p>还没有。点右上角「+ 添加」。</p></div>';
    return;
  }
  box.innerHTML = values.map((v, i) => `
    <div class="value-edit" data-idx="${i}">
      <input type="text" value="${esc(v.text)}" maxlength="300" data-field="text">
      <div class="value-actions">
        <button class="icon-btn${v.locked ? ' is-locked' : ''}" data-act="lock"
                title="${v.locked ? '已锁定：删除前需先解锁' : '未锁定：可直接删除'}">
          ${v.locked
            ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 018 0v3"/></svg>'
            : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 017-2.6"/></svg>'}
        </button>
        <button class="icon-btn" data-act="del" title="删除">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 6l12 12M18 6L6 18"/></svg>
        </button>
      </div>
    </div>`).join('');
}

function renderTraits(traits) {
  $('#soulTraits').innerHTML = Object.entries(TRAIT_DOC).map(([k, doc]) => {
    const v = typeof traits[k] === 'number' ? traits[k] : 50;
    return `<div class="trait-edit" data-key="${k}">
      <div class="trait-lbl">${doc.label}</div>
      <div class="trait-slider-wrap">
        <input type="range" min="0" max="100" step="1" value="${v}" data-slider>
        <div class="trait-usage">${esc(traitSentence(k, v))}</div>
      </div>
      <div class="trait-num" data-num>${v}</div>
    </div>`;
  }).join('');
}

function renderNotes(notes) {
  const box = $('#soulNotes');
  if (!notes.length) {
    box.innerHTML = '<div class="empty" style="padding:12px 0"><p>还没有。点右上角「+ 添加」。</p></div>';
    return;
  }
  box.innerHTML = notes.map((n, i) => `
    <div class="note-edit" data-idx="${i}">
      <input type="text" value="${esc(n)}" maxlength="500">
      <button class="icon-btn" data-act="del" title="删除">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 6l12 12M18 6L6 18"/></svg>
      </button>
    </div>`).join('');
}

/* ── 灵魂：交互 ── */

$('#editWhoIAm').addEventListener('input', (e) => {
  setText('#whoCount', String(e.target.value.length));
});

// 性格滑杆：拖动时即时更新数字与那句话，不写盘
$('#soulTraits').addEventListener('input', (e) => {
  const slider = e.target.closest('[data-slider]');
  if (!slider) return;
  const row = slider.closest('.trait-edit');
  const v = parseInt(slider.value, 10);
  row.querySelector('[data-num]').textContent = String(v);
  row.querySelector('.trait-usage').textContent = traitSentence(row.dataset.key, v);
});

// 价值：删除 / 解锁 / 添加
$('#soulValues').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-act]');
  if (!btn) return;
  const row = btn.closest('.value-edit');
  const idx = parseInt(row.dataset.idx, 10);
  const vals = (S.soul?.values || []).slice();

  if (btn.dataset.act === 'lock') {
    vals[idx] = { ...vals[idx], locked: !vals[idx].locked };
    S.soul.values = vals;
    renderValues(vals);
    return;
  }
  if (btn.dataset.act === 'del') {
    /* ⚠ locked 的判定要**按当前渲染行**去读，不能读 S.soul.values[idx]。
       两者在「先删过一条、索引已经错位」时会指向不同条目 ——
       守卫就会守错对象，把没锁的拦下来、把锁着的删掉。
       这里直接查被点的那个按钮自己的 class，它才是用户看着的那一行。 */
    if (btn.closest('.value-edit')?.querySelector('.icon-btn.is-locked')) {
      toast('这条是锁定的，先点锁图标解锁');
      return;
    }
    if (vals[idx]?.locked) {
      toast('这条是锁定的，先点锁图标解锁');
      return;
    }
    vals.splice(idx, 1);
    S.soul.values = vals;
    renderValues(vals);
  }
});

// 价值：编辑文本（用 input 事件同步到内存，不重绘 —— 重绘会丢焦点）
$('#soulValues').addEventListener('input', (e) => {
  const input = e.target.closest('[data-field="text"]');
  if (!input) return;
  const idx = parseInt(input.closest('.value-edit').dataset.idx, 10);
  if (S.soul?.values?.[idx]) S.soul.values[idx].text = input.value;
});

$('#btnAddValue').addEventListener('click', () => {
  if (!S.soul) return toast('还没读到人格');
  const vals = (S.soul.values || []).slice();
  vals.push({ id: 'v' + (vals.length + 1), text: '', locked: false });
  S.soul.values = vals;
  renderValues(vals);
  const last = $('#soulValues').lastElementChild?.querySelector('input');
  if (last) last.focus();
});

// 自我认识：删除 / 编辑 / 添加
$('#soulNotes').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-act="del"]');
  if (!btn) return;
  const idx = parseInt(btn.closest('.note-edit').dataset.idx, 10);
  const notes = (S.soul?.selfNotes || []).slice();
  notes.splice(idx, 1);
  S.soul.selfNotes = notes;
  renderNotes(notes);
});
$('#soulNotes').addEventListener('input', (e) => {
  const idx = parseInt(e.target.closest('.note-edit')?.dataset.idx, 10);
  if (Number.isInteger(idx) && S.soul?.selfNotes?.[idx] !== undefined) {
    S.soul.selfNotes[idx] = e.target.value;
  }
});
$('#btnAddNote').addEventListener('click', () => {
  if (!S.soul) return toast('还没读到人格');
  const notes = (S.soul.selfNotes || []).slice();
  notes.push('');
  S.soul.selfNotes = notes;
  renderNotes(notes);
  const last = $('#soulNotes').lastElementChild?.querySelector('input');
  if (last) last.focus();
});

/* ── 灵魂：保存 ── */

function collectSoul() {
  const values = [];
  $$('#soulValues .value-edit').forEach((row, i) => {
    const text = row.querySelector('[data-field="text"]')?.value?.trim();
    if (!text) return;   // 空条目直接丢，服务端也会丢
    const prev = S.soul?.values || [];
    // 按位置找原来的 id 和 locked，保持稳定
    const orig = prev[findOrigIdx(prev, i)];
    values.push({
      id: (orig && orig.id) || ('v' + (values.length + 1)),
      text,
      locked: !!(orig && orig.locked),
    });
  });

  const traits = {};
  $$('#soulTraits .trait-edit').forEach((row) => {
    traits[row.dataset.key] = parseInt(
      row.querySelector('[data-slider]').value, 10);
  });

  const selfNotes = [];
  $$('#soulNotes .note-edit input').forEach((inp) => {
    const t = inp.value.trim();
    if (t) selfNotes.push(t);
  });

  return {
    identity: {
      name: $('#editName').value.trim() || '大肥鱼',
      address: $('#editAddress').value.trim() || '爸爸',
      aliases: $('#editAliases').value.trim(),
      whoIAm: $('#editWhoIAm').value,
    },
    values,
    traits,
    selfNotes,
  };
}

/* 草稿里删过条目后位置会错位，这里按「文本已存在则复用其 id」找原条目 */
function findOrigIdx(prev, renderedIdx) {
  const row = $$('#soulValues .value-edit')[renderedIdx];
  const text = row?.querySelector('[data-field="text"]')?.value;
  const hit = prev.findIndex((v) => v.text === text);
  return hit >= 0 ? hit : renderedIdx;
}

$('#btnSoulSave').addEventListener('click', async () => {
  if (!S.bridge || !S.soul) return toast('还没读到人格');
  const btn = $('#btnSoulSave');
  btn.disabled = true;
  const hint = $('#soulHint');
  hint.className = 'hint-line';
  try {
    const payload = {
      soul: collectSoul(),
      baseRevision: S.soul.revision || 1,
    };
    const r = JSON.parse(await S.bridge.save_persona(JSON.stringify(payload)));

    if (!r.ok) {
      if (r.code === 'revision-conflict') {
        toast('这份人格在别处被改过了，正在重新载入');
        hint.textContent = r.error || '';
        hint.className = 'hint-line is-err';
        await loadSoul();
        return;
      }
      // 失败必须留在页面上，不能只弹一个会自己消失的 toast ——
      // 用户点完保存什么都没变，却不知道是权限、是磁盘、还是别的什么。
      toast(r.error || '保存失败');
      hint.textContent = '保存失败：' + (r.error || '未知原因');
      hint.className = 'hint-line is-err';
      return;
    }
    markClean('soul');
    toast('已保存');
    hint.textContent = '已保存 ' + hhmmss() + '（rev ' + r.revision + '）';
    hint.className = 'hint-line is-ok';
    await loadSoul();
  } catch (e) {
    toast('保存失败');
    hint.textContent = '保存失败：' + (e && e.message ? e.message : String(e));
    hint.className = 'hint-line is-err';
  } finally {
    btn.disabled = false;
  }
});

$('#btnSoulReset').addEventListener('click', async () => {
  if (!S.bridge) return;
  if (!confirm('把人格恢复成出厂默认？你现在写的内容会被覆盖（revision 会 +1）。')) return;
  try {
    const r = JSON.parse(await S.bridge.save_persona(JSON.stringify({ reset: true })));
    if (!r.ok) return toast(r.error || '恢复失败');
    toast('已恢复默认');
    S.soul = null;
    await loadSoul();
  } catch (e) { toast('恢复失败'); }
});

/* ────────── 心情 ────────── */

const DIM_NAMES = {
  happy: '开心', calm: '踏实', sweet: '甜蜜',
  heartache: '心疼', worry: '担心', sad: '难过', jealous: '吃醋',
};

async function loadMood() {
  if (!S.bridge) return;
  try {
    const d = JSON.parse(await S.bridge.get_mood());
    if (d.error && d.value === undefined) return toast(d.error);

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
    box.innerHTML = Object.entries(DIM_NAMES).map(([k, label]) => {
      // 维度是 0..100，不是 0..1（与 mood_judge 同一套量纲）。
    // 以前写 Math.min(1, v)：9 和 10 都被夹成 1，界面上「不是 0% 就是 100%」。
    const p = Math.max(0, Math.min(100, Number(dims[k] ?? 0))) / 100;
      return `<div class="dim-row">
        <span class="dim-name">${label}</span>
        <span class="dim-track"><span class="dim-fill" style="width:${(p * 100).toFixed(1)}%"></span></span>
        <span class="dim-val">${(p * 100).toFixed(0)}%</span>
      </div>`;
    }).join('') || '<div class="empty"><p>（无数据）</p></div>';

    const b = d.budget || {};
    setText('#moodBudgetParts', b.parts != null ? b.parts : '—');
    setText('#moodBudgetChars', b.chars != null ? b.chars : '—');
    setText('#moodBudgetSilence', b.silence != null ? Number(b.silence).toFixed(2) : '—');
    setText('#moodBudgetMust', b.must_after != null ? b.must_after + ' 次' : '—');
    const budgetState = $('#moodBudgetState');
    if (budgetState) {
      budgetState.textContent = d.value == null ? '—' : '生效中';
      budgetState.className = 'tag ' + (d.value == null ? '' : 'ok');
    }
    const why = [b.reason, b.tone].filter(Boolean).join('；');
    setText('#moodBudgetReason', why || '心情、精力、时间和余韵会一起收紧这一轮。');

    const echo = d.afterglow || {};
    setText('#moodEchoTag', echo.count ? `${echo.count} 条` : '—');
    setText('#moodEchoText', echo.text || '还没有余韵；上一轮的情绪没有留下额外修正。');

    fillMoodConfig(d.config || {});
  } catch (e) {
    toast('读取心情失败');
  }
}

function fillMoodConfig(c) {
  $('#moodBase').value = c.moodBase ?? 70;
  $('#moodFloor').value = c.moodFloor ?? 0;
  $('#moodDecay').value = c.decayHalfHours ?? 12;
  $('#moodPerDay').value = c.injectPerDay ?? 6;
  $('#moodConf').value = c.injectConfidence ?? 40;
  $('#moodSigMood').value = c.sigMood ?? 3;
  $('#moodSigDim').value = c.sigDim ?? 5;
  $('#moodInjectMode').value = c.injectMode || 'event';
  $('#moodAutoJudge').checked = c.autoJudge !== false;
}

$('#btnMoodSave').addEventListener('click', async () => {
  if (!S.bridge) return toast('后端未就绪');
  const patch = {
    moodBase: parseInt($('#moodBase').value, 10),
    moodFloor: parseInt($('#moodFloor').value, 10),
    decayHalfHours: parseInt($('#moodDecay').value, 10),
    injectPerDay: parseInt($('#moodPerDay').value, 10),
    injectConfidence: parseInt($('#moodConf').value, 10),
    sigMood: parseInt($('#moodSigMood').value, 10),
    sigDim: parseInt($('#moodSigDim').value, 10),
    injectMode: $('#moodInjectMode').value,
    autoJudge: $('#moodAutoJudge').checked,
  };
  try {
    const r = JSON.parse(await S.bridge.save_mood_config(JSON.stringify(patch)));
    const h = $('#moodHint');
    if (!r.ok) {
      toast(r.error || '保存失败');
      h.textContent = '保存失败：' + (r.error || '未知原因');
      h.className = 'hint-line is-err';
      return;
    }
    markClean('mood');
    toast('已保存');
    h.textContent = '已保存 ' + hhmmss() + '，下一轮判断生效';
    h.className = 'hint-line is-ok';
    fillMoodConfig(r.config || {});
  } catch (e) {
    toast('保存失败');
    const h = $('#moodHint');
    h.textContent = '保存失败：' + (e && e.message ? e.message : String(e));
    h.className = 'hint-line is-err';
  }
});

$('#btnMoodReset').addEventListener('click', async () => {
  if (!S.bridge) return;
  if (!confirm('把心情拉回基线？情绪维度归零，时间线保留（历史证据不删）。')) return;
  try {
    const r = JSON.parse(await S.bridge.reset_mood());
    if (!r.ok) return toast(r.error || '重置失败');
    toast('已重置');
    await loadMood();
  } catch (e) { toast('重置失败'); }
});

/* ────────── 记忆 ────────── */

const MEM_PAGE = 40;
S.mem = { offset: 0, total: 0, selected: new Set(), mode: 'browse' };

const fmtBytes = (n) => {
  if (!n) return '—';
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1048576).toFixed(1) + ' MB';
};

async function loadMemoryStats() {
  if (!S.bridge) return;
  try {
    const d = JSON.parse(await S.bridge.memory_stats());
    if (d.error) {
      setText('#memTotal', '—');
      setText('#memChunks', '—');
      setText('#memSize', '—');
      return;
    }
    setText('#memTotal', String(d.total ?? '—'));
    setText('#memChunks', String(d.chunks ?? '—'));
    setText('#memSize', fmtBytes(d.size));
    setText('#memDbTag', `共 ${d.total ?? 0} 条 / ${d.sessions ?? 0} 个会话`);
    setText('#memCount', `库里 ${d.total ?? 0} 条`);
    S.mem.total = d.total || 0;
  } catch (e) { /* 静默 */ }
}

/* 浏览模式：按 id 倒序翻页 */
async function loadMemory(query) {
  if (!S.bridge) return;

  if (query) {
    // 检索模式：走 bridge.mjs → 原版 MemoryStore，分词与衰减一致
    S.mem.mode = 'search';
    try {
      const d = JSON.parse(await S.bridge.search_memory(query, 40));
      if (d.error) return toast(d.error);
      const items = d.items || [];
      setText('#memCount', `命中 ${items.length} 条 / 库里 ${d.total ?? '?'} 条`);
      setText('#memPageInfo', '检索结果');
      $('#memPager').style.display = 'none';

      const box = $('#memList');
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
    return;
  }

  // 浏览模式
  S.mem.mode = 'browse';
  $('#memPager').style.display = '';
  try {
    const role = $('#memRole').value || '';
    const filter = $('#memFilter').value.trim();
    const d = JSON.parse(await S.bridge.browse_memory(
      S.mem.offset, MEM_PAGE, role, filter));
    if (d.error) return toast(d.error);

    const items = d.items || [];
    S.mem.total = d.total || 0;
    const from = S.mem.total ? S.mem.offset + 1 : 0;
    const to = Math.min(S.mem.offset + items.length, S.mem.total);
    setText('#memPageInfo', `${from}–${to} / ${S.mem.total}`);
    setText('#memCount', `库里 ${S.mem.total} 条`);

    const box = $('#memList');
    if (!items.length) {
      box.innerHTML = `<div class="empty">
        <h3>没有记忆</h3>
        <p>${filter ? '这个过滤条件下没有内容' : '这个范围的记忆库是空的'}</p></div>`;
      return;
    }

    box.innerHTML = items.map((m) => `
      <div class="memory-item selectable">
        <input type="checkbox" class="mem-check" data-id="${m.id}"
               ${S.mem.selected.has(m.id) ? 'checked' : ''}>
        <div class="memory-body">
          <div class="memory-meta">
            <span class="memory-role ${m.role === 'assistant' ? 'assistant' : ''}">
              ${m.role === 'assistant' ? esc(S.soul?.name || '她') : '对方'}
            </span>
            <span class="memory-time">${esc(m.time || '')}</span>
            <button class="icon-btn" data-del="${m.id}" title="删掉这条"
                    style="margin-left:6px;width:22px;height:22px">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 6l12 12M18 6L6 18"/></svg>
            </button>
          </div>
          <div class="memory-text">${esc(m.text)}</div>
        </div>
      </div>`).join('');

    // 选中状态同步
    $$('#memList .mem-check').forEach((cb) => {
      cb.addEventListener('change', () => {
        const id = parseInt(cb.dataset.id, 10);
        if (cb.checked) S.mem.selected.add(id);
        else S.mem.selected.delete(id);
        updateMemSelectionUI();
      });
    });
    // 单条删
    $$('#memList [data-del]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const id = parseInt(btn.dataset.del, 10);
        if (!confirm(`删掉这条记忆？删了就找不回来了。`)) return;
        const r = JSON.parse(await S.bridge.delete_memory(JSON.stringify({ ids: [id] })));
        if (!r.ok) return toast(r.error || '删除失败');
        toast(`已删除 ${r.deleted} 条`);
        S.mem.selected.delete(id);
        await loadMemoryStats();
        await loadMemory('');
      });
    });
    updateMemSelectionUI();
  } catch (e) {
    toast('读取记忆失败');
  }
}

function updateMemSelectionUI() {
  const n = S.mem.selected.size;
  const tag = $('#memDbTag');
  if (n > 0) {
    tag.textContent = `已选 ${n} 条`;
    tag.className = 'tag';
    tag.style.cursor = 'pointer';
    tag.title = '点这里删掉选中的';
  } else {
    tag.style.cursor = '';
    tag.title = '';
    tag.className = 'tag';
    const total = S.mem.total;
    tag.textContent = total ? `共 ${total} 条` : '—';
  }
}

$('#memDbTag').addEventListener('click', async () => {
  const ids = [...S.mem.selected];
  if (!ids.length) return;
  if (!confirm(`删掉选中的 ${ids.length} 条记忆？`)) return;
  const r = JSON.parse(await S.bridge.delete_memory(JSON.stringify({ ids })));
  if (!r.ok) return toast(r.error || '删除失败');
  toast(`已删除 ${r.deleted} 条`);
  S.mem.selected.clear();
  await loadMemoryStats();
  await loadMemory('');
});

$('#btnMemSearch').addEventListener('click', () => {
  S.mem.offset = 0;
  loadMemory($('#memQuery').value.trim());
});
$('#memQuery').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    S.mem.offset = 0;
    loadMemory($('#memQuery').value.trim());
  }
});

$('#btnMemBrowse').addEventListener('click', () => {
  S.mem.offset = 0;
  S.mem.selected.clear();
  $('#memQuery').value = '';
  loadMemory('');
});
$('#memFilter').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    S.mem.offset = 0;
    loadMemory('');
  }
});
$('#memRole').addEventListener('change', () => {
  S.mem.offset = 0;
  loadMemory('');
});

$('#btnMemPrev').addEventListener('click', () => {
  S.mem.offset = Math.max(0, S.mem.offset - MEM_PAGE);
  loadMemory('');
});
$('#btnMemNext').addEventListener('click', () => {
  if (S.mem.offset + MEM_PAGE < S.mem.total) {
    S.mem.offset += MEM_PAGE;
    loadMemory('');
  }
});

/* ── 记忆：她认定的事（事实层） ──
 *
 * 和上面「记忆」列表的区别要讲清楚，否则用户分不清能删哪个：
 *   · 原文列表 = 她听过的原话。删了就是没听过。
 *   · 事实列表 = 她自己整理出的认识。可以「忘掉」（不删行，只失效），
 *     也能看到它被哪条新认识取代了 —— 「她以前以为是 X」依然查得到。
 */

S.facts = { offset: 0, total: 0, status: '' };
S.chatTab = '';        // 当前选中的会话分组
S.watchDirty = false;  // 监听列表有没有未保存的改动
S.accountList = [];    // 上次拉到的账号列表（改名用）

async function loadMemoryHealth() {
  if (!S.bridge || !S.bridge.memory_health) return;
  try {
    const d = JSON.parse(await S.bridge.memory_health());
    const box = $('#memHealth');
    if (!box) return;
    if (!d.ok) {
      box.innerHTML = '<span class="health-tag warn">' + esc(d.message || '读不到') + '</span>';
      return;
    }
    const bad = (d.problems || []).length;
    const cls = bad ? 'warn' : 'ok';
    box.innerHTML =
      '<span class="health-tag ' + cls + '">' + esc(bad ? d.problems.join('；') : '索引一致') + '</span>' +
      '<span class="health-tag">原文 ' + d.messages + ' 条 / 片段 ' + d.chunks + '</span>' +
      '<span class="health-tag">认识 ' + d.facts + ' 条（已取代 ' + d.superseded + '）</span>' +
      '<span class="health-tag">schema v' + d.schemaVersion + '</span>';
  } catch (e) { /* 体检失败不影响使用 */ }
}

async function loadFacts(offset) {
  if (!S.bridge || !S.bridge.list_facts) return;
  if (typeof offset === 'number') S.facts.offset = Math.max(0, offset);
  const status = $('#factStatus') ? $('#factStatus').value : '';
  const query = $('#factQuery') ? $('#factQuery').value.trim() : '';
  const box = $('#factList');
  if (!box) return;
  try {
    const d = JSON.parse(await S.bridge.list_facts(status, query, 40, S.facts.offset));
    if (d.error) return toast(d.error);
    const items = d.items || [];
    S.facts.total = d.total || 0;
    const from = S.facts.total ? S.facts.offset + 1 : 0;
    const to = Math.min(S.facts.offset + items.length, S.facts.total);
    setText('#factPageInfo', from + '–' + to + ' / ' + S.facts.total);

    if (!items.length) {
      box.innerHTML = '<div class="empty"><h3>还没有整理出认识</h3>' +
        '<p>聊够几轮后点「现在整理一下」，她会把原话沉淀成认识</p></div>';
      return;
    }
    box.innerHTML = items.map((f) => {
      const dead = f.status !== 'active';
      const replaced = f.supersededBy
        ? '<span class="fact-note">已被 #' + f.supersededBy + ' 取代</span>' : '';
      const conf = '<span class="fact-conf">' + Math.round((f.confidence || 0) * 100) + '%</span>';
      const del = dead ? '' :
        '<button class="icon-btn" data-forget="' + f.id + '" title="忘掉这条（不删历史，只失效）" ' +
        'style="margin-left:6px;width:22px;height:22px">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
        '<path d="M6 6l12 12M18 6L6 18"/></svg></button>';
      // 可见性徽章：私密 / 可共享 / 公开。点它切换 —— 这是唯一能
      // 把私密内容放给别人的口子，所以点了会弹确认。
      const visVal = f.visibility || 'private';
      const visCls = visVal === 'private' ? '' : 'ok';
      const visBadge = dead ? '' :
        '<button class="health-tag ' + visCls + '" data-vis="' + f.id + '" ' +
        'data-target="' + (visVal === 'private' ? 'shared' : 'private') + '" ' +
        'title="点一下改成' + (visVal === 'private' ? '可共享' : '私密') + '">' +
        esc(VIS_LABEL[visVal] || visVal) + '</button>';
      const pinBadge = (f.pinned && !dead) ? '<span class="fact-conf">★</span>' : '';
      const ownerTag = (!dead && f.owner) ?
        '<span class="memory-time">' + esc(f.owner.split(':').pop().slice(0, 12)) +
        '</span>' : '';
      // 话题选择（v5）：
      //   话题 + 可见性**两个都满足**才外传。只标 shared 不选话题 = 不外传。
      //   所以这里不是装饰，它直接决定"她能对别人说什么"。
      const topicSel = dead ? '' :
        '<select class="topic-select" data-topic="' + f.id + '" title="话题决定能不能说给别人听">' +
        '<option value=""' + (!f.topic ? ' selected' : '') + '>话题…</option>' +
        TOPIC_ALL.map((t) =>
          '<option value="' + esc(t.k) + '"' + (f.topic === t.k ? ' selected' : '') + '>' +
          esc(t.k) + (t.open ? '' : '（不外传）') + '</option>').join('') +
        '</select>';
      // 群里说过的事标出来
      const originTag = (f.origin && f.origin.indexOf('group:') === 0) ?
        '<span class="health-tag">群里说的</span>' : '';
      // 真的会外传时给个明显标记
      const openTag = (f.openable && !dead) ?
        '<span class="health-tag ok">会外传</span>' : '';
      // 用过几次（v6）：说明这条是不是她真的在用。
      // 0 次不显示 —— 大多数条目都是 0，全都标出来等于噪音。
      const useTag = (!dead && (f.useCount || 0) > 0) ?
        '<span class="memory-time" title="被带进提示词的次数">用过 ' +
        f.useCount + ' 次</span>' : '';
      return '<div class="memory-item' + (dead ? ' is-dead' : '') + '">' +
        '<div class="memory-body"><div class="memory-meta">' +
        '<span class="memory-role">' + esc(f.anchor || '她') + '</span>' +
        '<span class="memory-time">' + esc(f.predicate) + '</span>' + conf + replaced +
        visBadge + pinBadge + ownerTag + originTag + openTag + useTag + topicSel +
        '<span class="memory-time">' + esc(f.createdText || '') + '</span>' + del +
        '</div><div class="memory-text">' + esc(f.object) + '</div></div></div>';
    }).join('');
  } catch (e) {
    toast('读取认识失败');
  }
}

async function forgetFact(id) {
  const NL = String.fromCharCode(10);
  const ok = confirm('忘掉这条认识？' + NL + NL +
    '不会删掉聊天原话 —— 她只是不再拿它当结论。');
  if (!ok) return;
  try {
    const r = JSON.parse(await S.bridge.forget_fact(id, '用户手动遗忘'));
    if (!r.ok) return toast(r.error || '操作失败');
    toast('已忘掉');
    loadFacts();
    loadMemoryHealth();
  } catch (e) { toast('操作失败'); }
}

async function reflectNow() {
  const btn = $('#btnReflect');
  if (btn) { btn.disabled = true; btn.textContent = '整理中…'; }
  try {
    const r = JSON.parse(await S.bridge.reflect_now());
    if (!r.ok) toast(r.error || '整理失败');
    else toast('新整理出 ' + (r.added || 0) + ' 条认识');
    loadFacts(0);
    loadMemoryHealth();
  } catch (e) {
    toast('整理失败');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '现在整理一下'; }
  }
}

async function repairMemory() {
  const NL = String.fromCharCode(10);
  const ok = confirm('重建检索索引？' + NL + NL +
    '原文和认识都不动，只重排索引。');
  if (!ok) return;
  try {
    const r = JSON.parse(await S.bridge.memory_repair());
    if (!r.ok) return toast(r.error || '修复失败');
    toast('索引已重建（' + r.chunks + ' 个片段）');
    loadMemoryHealth();
  } catch (e) { toast('修复失败'); }
}

/* ── 记忆：会话清单 ── */
async function loadMemorySessions() {
  if (!S.bridge) return;
  try {
    const d = JSON.parse(await S.bridge.list_memory_sessions());
    const rows = d.sessions || [];
    const box = $('#memSessions');
    if (!rows.length) {
      box.innerHTML = '<div class="empty" style="padding:12px 0"><p>这个范围还没有会话记录。</p></div>';
      return;
    }
    box.innerHTML = rows.map((s) => `
      <div class="session-row">
        <span class="session-name" title="${esc(s.session)}">${esc(s.session)}</span>
        <span class="session-meta">${s.count} 条</span>
        <span class="session-meta">${esc(s.newestText || '')}</span>
        <button class="icon-btn" data-session="${esc(s.session)}" title="删掉这个会话的全部记忆">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 6l12 12M18 6L6 18"/></svg>
        </button>
      </div>`).join('');

    $$('#memSessions [data-session]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const sid = btn.dataset.session;
        if (!confirm(`删掉会话「${sid.slice(0, 40)}」留下的全部记忆？\n删了就找不回来了。`)) return;
        const r = JSON.parse(await S.bridge.delete_memory(
          JSON.stringify({ session: sid, confirm: true })));
        if (!r.ok) return toast(r.error || '删除失败');
        toast(`已删除 ${r.deleted} 条`);
        await loadMemoryStats();
        await loadMemorySessions();
        await loadMemory('');
      });
    });
  } catch (e) { /* 静默 */ }
}

/* ── 记忆：维护 ── */
$('#btnMemExport').addEventListener('click', async () => {
  if (!S.bridge) return;
  try {
    const r = JSON.parse(await S.bridge.export_memory(''));
    const h = $('#memMaintainHint');
    if (!r.ok) {
      h.textContent = '导出失败：' + (r.error || '');
      h.className = 'card-desc is-err';
      return toast('导出失败');
    }
    h.textContent = '已导出到 ' + r.path;
    h.className = 'card-desc is-ok';
    toast('已导出');
  } catch (e) { toast('导出失败'); }
});

$('#btnMemImport').addEventListener('click', async () => {
  if (!S.bridge) return;
  let src = '';
  try {
    const picked = JSON.parse(await S.bridge.pick_file(''));
    src = picked.path || '';
  } catch (e) { src = ''; }
  if (!src) {
    src = prompt('输入要导入的 .sqlite3 备份完整路径：') || '';
  }
  if (!src) return;
  if (!confirm('导入会覆盖当前记忆库。现有库会先自动备份成 .bak-时间戳。继续？')) return;
  try {
    const r = JSON.parse(await S.bridge.import_memory(src.trim()));
    const h = $('#memMaintainHint');
    if (!r.ok) {
      h.textContent = '导入失败：' + (r.error || '');
      h.className = 'card-desc is-err';
      return toast('导入失败');
    }
    h.textContent = '已导入到 ' + r.path;
    h.className = 'card-desc is-ok';
    toast('已导入');
    await loadMemoryStats();
    await loadMemory('');
    await loadMemorySessions();
  } catch (e) { toast('导入失败'); }
});

$('#btnMemVacuum').addEventListener('click', async () => {
  if (!S.bridge) return;
  if (!confirm('压缩会短暂独占记忆库，DSH 那边这段时间写不进去。继续？')) return;
  try {
    const r = JSON.parse(await S.bridge.vacuum_memory());
    const h = $('#memMaintainHint');
    if (!r.ok) {
      h.textContent = '压缩失败：' + (r.error || '');
      h.className = 'card-desc is-err';
      return toast('压缩失败');
    }
    h.textContent = '压缩完成';
    h.className = 'card-desc is-ok';
    toast('已压缩');
    await loadMemoryStats();
  } catch (e) { toast('压缩失败'); }
});

/* ── 事实层：交互 ──
 *
 * 这些按钮跟上面「记忆」页的按钮是两套：
 *   原文页 → 删（真的删）
 *   事实页 → 忘掉（只失效）/ 整理（新增认识）/ 修索引（修复派生层）
 */

/* ── 主人与共享 ──
 *
 * 三个动作：
 *   绑定主人   选一个微信会话 → 写配置 + 写 owners 表 → 立刻生效
 *   补平台身份 B站/抖音/手机号，先建记录（暂不合并，见后端注释）
 *   改可见性   把一条事实从 private 改成 shared，**唯一能主动泄露的口子**
 */

const TIER_LABEL = { owner: '主人', known: '熟人', guest: '陌生' };
const VIS_LABEL = { private: '私密', shared: '可共享', public: '公开' };

/* 话题表：和白名单/黑名单必须与 bigfish/memory.py 的 TOPIC_* 一致。
 * open=true 的是白名单（可以外传），false 的是黑名单与灰区（一律不外传）。
 *
 * ⚠ 这里只是**展示**。真正的判定在 Python 侧（open_facts_for / topic_allowed），
 *   前端说"可以"没用 —— 后端不认就是不认。这样即使有人改了 JS，
 *   也改不出一个泄露口。 */
const TOPIC_ALL = [
  { k: '食物', open: true }, { k: '爱好', open: true }, { k: '作息', open: true },
  { k: '性格', open: true }, { k: '口头禅', open: true }, { k: '称呼', open: true },
  { k: '住址', open: false }, { k: '工作', open: false }, { k: '行程', open: false },
  { k: '身体', open: false }, { k: '金钱', open: false }, { k: '家人', open: false },
  { k: '联系人', open: false }, { k: '隐私', open: false },
  { k: '年龄', open: false }, { k: '职业', open: false }, { k: '城市', open: false },
  { k: '情感', open: false }, { k: '近况', open: false },
];

async function loadOwners() {
  if (!S.bridge || !S.bridge.owner_candidates) return;
  try {
    const d = JSON.parse(await S.bridge.owner_candidates());
    const sel = $('#ownerPick');
    const list = $('#ownerList');
    if (!sel || !list) return;

    // ① 下拉：微信会话
    const chats = d.chats || [];
    sel.innerHTML = chats.length
      ? chats.map((c) => {
          const tag = c.isCurrent ? '（当前主人）' : '';
          return '<option value="' + esc(c.wxid) + '">' +
            esc(c.name) + tag + '</option>';
        }).join('')
      : '<option value="">（没有可选的私聊会话）</option>';

    // ② 健康行
    const h = $('#ownerHealth');
    const cur = d.current || '';
    if (h) {
      h.innerHTML = cur
        ? '<span class="health-tag ok">主人：' + esc(cur) + '</span>'
        : '<span class="health-tag warn">还没设主人 —— 所有人按陌生处理</span>';
    }
    const tag = $('#ownerTag');
    if (tag) tag.textContent = cur ? '已绑定' : '未绑定';

    // ③ 列表：所有登记过的人
    const known = d.known || [];
    if (!known.length) {
      list.innerHTML = '<div class="empty" style="padding:12px 0">' +
        '<p>还没有记录到任何人。跟她聊几句，或者从上面选一个主人。</p></div>';
      return;
    }
    list.innerHTML = known.map((o) => {
      const isOwner = o.tier === 'owner';
      const cls = isOwner ? 'ok' : '';
      return '<div class="session-item">' +
        '<div class="session-main">' +
        '<div class="session-name">' + esc(o.name || o.externalId) +
        '<span class="health-tag ' + cls + '" style="margin-left:8px">' +
        esc(TIER_LABEL[o.tier] || o.tier) + '</span>' +
        '<span class="health-tag" style="margin-left:4px">' +
        esc(o.platform) + '</span></div>' +
        '<div class="session-sub">' + esc(o.externalId) + '</div>' +
        '</div>' +
        (isOwner ? '' :
          '<button class="btn btn-sm" data-tier="' + esc(o.owner) +
          '">设为主人</button>') +
        '</div>';
    }).join('');
  } catch (e) {
    toast('读取主人信息失败');
  }
}

async function bindOwner(ownerKey) {
  if (!S.bridge) return;
  // ownerKey 形如 private:wechat:wxid_xxx；也接受纯 wxid
  let platform = 'wechat';
  let eid = ownerKey || '';
  if (eid.indexOf(':') >= 0) {
    const parts = eid.split(':');
    platform = parts[1] || 'wechat';
    eid = parts.slice(2).join(':');
  }
  if (!eid) return toast('先选一个人');
  const ok = confirm('把「' + eid + '」设为主人？' + String.fromCharCode(10) +
    String.fromCharCode(10) +
    '主人能看到全部记忆（数据在你电脑上）。' +
    String.fromCharCode(10) + '原来的主人会降为「熟人」。');
  if (!ok) return;
  try {
    const r = JSON.parse(await S.bridge.bind_owner(platform, eid));
    if (!r.ok) return toast(r.error || '绑定失败');
    toast('已设为主人，立刻生效');
    loadOwners();
    loadFacts();
  } catch (e) { toast('绑定失败'); }
}

async function addIdentity() {
  if (!S.bridge) return;
  const plat = $('#idPlatform') ? $('#idPlatform').value : '';
  const val = $('#idValue') ? $('#idValue').value.trim() : '';
  if (!val) return toast('先填 id');
  try {
    const r = JSON.parse(await S.bridge.add_identity(plat, val, false));
    if (!r.ok) return toast(r.error || '添加失败');
    toast('已添加（' + plat + '）');
    if ($('#idValue')) $('#idValue').value = '';
    loadOwners();
  } catch (e) { toast('添加失败'); }
}

/* ── 记忆池分栏 ──
 *
 * 只切显示，不改变任何数据。默认永远是「主人」那一栏 ——
 * 其他池只给数量占位，不给内容。
 */
function switchMemTab(name) {
  document.querySelectorAll('#memTabs .subtab').forEach((b) => {
    b.classList.toggle('is-active', b.dataset.memtab === name);
  });
  document.querySelectorAll('[data-mempane]').forEach((p) => {
    p.classList.toggle('is-active', p.dataset.mempane === name);
  });
  if (name === 'others') loadPoolSummary();
}

async function loadPoolSummary() {
  if (!S.bridge || !S.bridge.pool_summary) return;
  try {
    const d = JSON.parse(await S.bridge.pool_summary());
    if (d.error) return toast(d.error);
    const pools = d.pools || [];
    const others = pools.filter((p) => !p.isMain);
    setText('#poolNum', others.length);
    setText('#poolTag', others.length + ' 个池');
    const box = $('#poolList');
    if (!box) return;
    if (!others.length) {
      box.innerHTML = '<div class="empty"><p>还没有别人的记忆池 —— ' +
        '等你跟她以外的人聊过，这里就会有。</p></div>';
      return;
    }
    // ⚠ 只显示「名字 + 条数」，不显示任何事实内容。
    //    这是刻意留白的：别人的池不该在主人没主动点开时被铺开。
    box.innerHTML = others.map((p) => {
      const kind = p.kind === 'group' ? '群' : '人';
      return '<div class="pool-item">' +
        '<div class="pool-name">' + esc(p.name) + '</div>' +
        '<div class="pool-meta"><span class="health-tag">' + kind + '</span>' +
        '<span class="health-tag">' + p.facts + ' 条认识</span>' +
        '<span class="health-tag">' + p.messages + ' 条原文</span>' +
        '<span class="memory-time">' + esc(p.platform || '') + '</span></div>' +
        '</div>';
    }).join('');
  } catch (e) { /* 池总览失败不影响主流程 */ }
}

/* ── 话题 ──
 *
 * 打话题 = 声明"这条讲的是哪类事"。它和可见性是**与**的关系：
 *   可见性 shared + 白名单话题  → 可以外传
 *   其余任何组合              → 不外传
 * 所以打错话题最多是"这条不给外人看"，不会反向泄露。
 */
async function setTopic(factId, topic) {
  if (!S.bridge || !S.bridge.set_fact_topic) return;
  const t = TOPIC_ALL.find((x) => x.k === topic);
  if (topic && t && !t.open) {
    const NL = String.fromCharCode(10);
    const ok = confirm('「' + topic + '」不在可以外传的话题里。' + NL + NL +
      '选了它这条仍然不会说给别人听 —— 只是记一下它是哪类事。继续？');
    if (!ok) { loadFacts(); return; }   // 取消就重画，把下拉框选回去
  }
  try {
    const r = JSON.parse(await S.bridge.set_fact_topic(Number(factId), topic || ''));
    if (!r.ok) return toast(r.error || '修改失败');
    toast(topic ? ('已标记为「' + topic + '」') : '已清空话题（不外传）');
    loadFacts();
  } catch (e) { toast('修改失败'); }
}

/* ── 角色印象页 ──
 *
 * 一人一页，横向切换，主人永远第一页。
 * 每页两块：
 *   ① 你手写的评语（模型不参与，纯文本框）
 *   ② 她会对别人说的开放信息（实时从 facts 查，不存副本）
 *
 * 「点删除」= 撤回开放，不是忘掉：
 *   事实还在，她照样拿它当结论，只是不再对别人提。
 */
let IMPR = { people: [], active: '' };

async function loadImpression() {
  if (!S.bridge || !S.bridge.impression_board) return;
  try {
    const d = JSON.parse(await S.bridge.impression_board());
    if (d.error) return toast(d.error);
    IMPR.people = d.people || [];
    setText('#imprCount', IMPR.people.length + ' 人');
    renderImprTabs();
    renderImprPanes();
  } catch (e) { toast('读取印象失败'); }
}

function renderImprTabs() {
  const box = $('#imprTabs');
  if (!box) return;
  if (!IMPR.people.length) {
    box.innerHTML = '';
    setText('#imprPeopleCount', '0 人');
    const panes = $('#imprPanes');
    if (panes) {
      panes.innerHTML = '<div class="empty"><h3>还没有人</h3>' +
        '<p>跟她聊过之后，这里会一个一个人出来</p></div>';
    }
    return;
  }
  // 主人固定第一个
  const sorted = IMPR.people.slice().sort((a, b) =>
    (a.isOwner ? 0 : 1) - (b.isOwner ? 0 : 1));
  IMPR.people = sorted;
  if (!IMPR.active || !sorted.some((p) => p.owner === IMPR.active)) {
    IMPR.active = sorted[0].owner;
  }
  setText('#imprPeopleCount', sorted.length + ' 人');
  box.innerHTML = sorted.map((p) => {
    const active = p.owner === IMPR.active ? ' is-active' : '';
    const name = p.name || p.owner;
    const initial = String(name).trim().charAt(0) || '人';
    const note = String(p.note || '').trim();
    const sub = note
      ? note.slice(0, 34)
      : `${p.facts || 0} 条认识 · ${p.private || 0} 条私密`;
    return '<button class="impr-person' + active + '" data-impr="' +
      esc(p.owner) + '">' +
      '<span class="impr-avatar">' + esc(initial) + '</span>' +
      '<span class="impr-person-copy"><strong>' + esc(name) +
      (p.isOwner ? ' <span class="tag ok">主人</span>' : '') +
      '</strong><small>' + esc(sub) + '</small></span>' +
      '<span class="impr-person-count">' + (p.facts || 0) + '</span>' +
      '</button>';
  }).join('');
}

function renderImprPanes() {
  const box = $('#imprPanes');
  if (!box || !IMPR.people.length) return;
  const p = IMPR.people.find((x) => x.owner === IMPR.active) || IMPR.people[0];
  if (!p) return;
  const opens = p.open || [];
  const openRows = opens.length
    ? opens.map((o) => (
      '<div class="open-item">' +
      '<div class="open-body">' +
      '<span class="health-tag ok">' + esc(o.topic) + '</span>' +
      '<span class="memory-role">' + esc(o.predicate) + '</span>' +
      '<span class="open-text">' + esc(o.object) + '</span>' +
      '</div>' +
      '<button class="btn btn-sm" data-revoke="' + o.id + '" title="撤回：她不再对别人提这条">' +
      '删除</button>' +
      '</div>')).join('')
    : '<div class="empty" style="padding:14px 0"><p>' +
      '还没有她会说出去的事。<br>' +
      '去「记忆 → 她认定的事」，把想公开的那条改成「可共享」并选一个话题。</p></div>';

  const main =
    '<div class="card impr-main-card">' +
    '<div class="card-head"><h2>' + esc(p.name || p.owner) +
    (p.isOwner ? ' <span class="health-tag ok">主人</span>' : '') + '</h2>' +
    '<span class="tag">' + p.facts + ' 条认识 / ' + p.private + ' 条私密</span></div>' +

    // ── 她的画像（v6）──
    //
    // 这是她**自己**从事实归纳的一段话，每轮都会带进提示词。
    // 和下面「你手写的评语」区别：那段她看不见，这段她天天在用。
    buildProfileBlock(p) +

    '<div class="field full" style="margin-bottom:16px">' +
    '<label>你怎么看这段关系（手写，模型不参与，她也不看）</label>' +
    '<textarea id="imprNote" rows="3" placeholder="比如：话不多，但每次说饿都是真的饿了">' +
    esc(p.note || '') + '</textarea>' +
    '<div class="path-row" style="margin-top:8px">' +
    '<button class="btn btn-primary btn-sm" id="btnSaveNote">保存评语</button>' +
    '<span class="memory-time" id="noteHint"></span>' +
    '</div></div>' +
    '<div class="card-head" style="margin-top:6px"><h2>她对别人会说的</h2>' +
    '<span class="tag">' + opens.length + ' 条</span></div>' +
    '<div class="card-desc" style="margin-bottom:10px">' +
    '这些是<b>可以</b>说出去的。住址、行程、身体、钱、家人一律不在里面 —— ' +
    '就算你标了「可共享」，话题不对也不外传。' +
    '</div>' +
    '<div class="open-list">' + openRows + '</div>' +
    '</div>';

  const side =
    '<aside class="impr-side">' +
    '<div class="card impr-bond-card">' + buildBondBlock(p) + '</div>' +
    buildEventTimeline(p) +
    '<div class="card impr-rules">' +
    '<div class="card-head"><h2>她会不会说出去</h2><span class="tag">规则</span></div>' +
    '<div class="rule-row"><span>会说</span><p>爱好、作息、性格、口头禅、称呼；还必须在开放话题白名单里。</p></div>' +
    '<div class="rule-row"><span>不会说</span><p>住址、工作、行程、身体、金钱、家人、联系人、年龄、情绪和近况。</p></div>' +
    '<div class="rule-row"><span>依据</span><p>关系卡每一条都必须能在原话里找到出处；找不到就写不进来。</p></div>' +
    '</div>' +
    '</aside>';

  box.innerHTML = '<div class="impr-workspace">' + main + side + '</div>';
}

function buildEventTimeline(p) {
  const events = p.events || [];
  if (!events.length) return '';
  const rows = events.map((e) => (
    '<div class="event-row">' +
    '<span class="event-dot"></span>' +
    '<div class="event-copy"><span class="event-kind">' + esc(e.kindLabel || '事件') +
    '</span><strong>' + esc(e.what || '') + '</strong>' +
    (e.evidence ? '<small>依据：「' + esc(e.evidence) + '」</small>' : '') +
    '</div>' +
    '<time>' + esc(e.atText || '') + '</time>' +
    '</div>')).join('');
  return '<div class="card impr-timeline-card">' +
    '<div class="card-head"><h2>关系时间线</h2><span class="tag">' +
    events.length + ' 条</span></div>' + rows + '</div>';
}

function buildProfileBlock(p) {
  const on = p.profileEnabled !== false;
  const has = !!(p.profile || '').trim();
  const head =
    '<div class="card-head" style="margin-top:6px">' +
    '<h2>她的画像</h2>' +
    '<span class="tag' + (on ? ' ok' : ' warn') + '">' +
    (has ? (on ? '在用' : '已停用') : '还没生成') + '</span></div>';

  const desc =
    '<div class="card-desc" style="margin-bottom:10px">' +
    '这是她<b>自己</b>从上面那些认识里归纳的一段话，每一轮对话都会带上。' +
    '刷新会重新归纳（要调一次模型）。觉得哪里不对就停用 —— ' +
    '停用后她不再按这段理解你，但这段话还留着，将来还能重新生成。' +
    '</div>';

  const body = has
    ? '<div class="profile-text' + (on ? '' : ' is-off') + '">' +
      esc(p.profile).replace(/\n/g, '<br>') + '</div>'
    : '<div class="empty" style="padding:14px 0"><p>' +
      (p.facts >= 3
        ? '认识够了，点「重新归纳」让她写一段'
        : '认识太少，先多聊几句（至少 3 条认识）') + '</p></div>';

  const meta = (has && p.profileUpdatedAt)
    ? '<div class="memory-time" style="margin-top:8px">更新于 ' +
      esc(new Date(p.profileUpdatedAt).toLocaleString('zh-CN')) +
      '｜基于 ' + (p.profileFactsAt || 0) + ' 条认识</div>'
    : '';

  const btns =
    '<div class="path-row" style="margin-top:10px">' +
    '<button class="btn btn-primary btn-sm" data-profile-refresh="' +
    esc(p.owner) + '">重新归纳</button>' +
    (has ? '<button class="btn btn-sm" data-profile-toggle="' + esc(p.owner) +
      '" data-to="' + (on ? '0' : '1') + '">' +
      (on ? '停用这段画像' : '重新启用') + '</button>' : '') +
    '<span class="memory-time" id="profileHint"></span>' +
    '</div>';

  return head + desc + body + meta + btns;
}

async function saveImprNote() {
  const el = $('#imprNote');
  const p = IMPR.people.find((x) => x.owner === IMPR.active);
  if (!el || !p || !S.bridge) return;
  try {
    const r = JSON.parse(await S.bridge.set_impression(p.owner, el.value.trim()));
    if (!r.ok) return toast(r.error || '保存失败');
    p.note = el.value.trim();
    setText('#noteHint', '已保存');
    setTimeout(() => setText('#noteHint', ''), 2000);
  } catch (e) { toast('保存失败'); }
}

async function revokeOpen(factId) {
  const NL = String.fromCharCode(10);
  const ok = confirm('不再对别人提这条？' + NL + NL +
    '她不会忘掉它 —— 只是不再说给外人听。');
  if (!ok) return;
  try {
    const r = JSON.parse(await S.bridge.revoke_open(Number(factId)));
    if (!r.ok) return toast(r.error || '操作失败');
    toast('已撤回');
    loadImpression();
    loadFacts();
  } catch (e) { toast('操作失败'); }
}

async function setVisibility(factId, vis) {
  if (!S.bridge) return;
  const label = VIS_LABEL[vis] || vis;
  const ok = confirm('把这条改成「' + label + '」？' + String.fromCharCode(10) +
    String.fromCharCode(10) +
    (vis === 'private'
      ? '私密：只有归属的那个人能看到。'
      : '可共享：**其他任何人**都会被允许看到它。想清楚再点。'));
  if (!ok) return;
  try {
    const r = JSON.parse(await S.bridge.set_fact_visibility(String(factId), vis));
    if (!r.ok) return toast(r.error || '修改失败');
    toast('已改为「' + label + '」');
    loadFacts();
  } catch (e) { toast('修改失败'); }
}

/* ── 跨平台身份组 ──
 *
 * 每个身份组 = 一个人在他所有平台的账号。
 * 挂上去之后，facts_for_prompt 会自动展开成"这个人所有账号"，记忆汇总。
 */

async function loadPersons() {
  if (!S.bridge || !S.bridge.list_persons) return;
  try {
    const d = JSON.parse(await S.bridge.list_persons());
    const list = $('#personList');
    const sel = $('#linkPerson');
    if (!list) return;
    const persons = (d.persons || []).filter((p) => p.person);
    const loose = (d.persons || []).find((p) => !p.person);

    // 下拉
    if (sel) {
      sel.innerHTML = persons.length
        ? persons.map((p) => '<option value="' + esc(p.person) + '">' +
            esc(p.name || p.person) + '</option>').join('')
        : '<option value="">（先建一个身份组）</option>';
    }
    const tag = $('#personTag');
    if (tag) tag.textContent = persons.length + ' 个';

    // 列表
    let html = '';
    for (const p of persons) {
      const ids = (p.identities || []).map((i) =>
        '<div class="session-item" style="padding-left:12px">' +
        '<div class="session-main">' +
        '<div class="session-name">' + esc(i.name || i.owner.split(':').pop()) +
        '<span class="health-tag" style="margin-left:6px">' +
        esc(i.platform) + '</span>' +
        '<span class="health-tag" style="margin-left:4px">' +
        esc(TIER_LABEL[i.tier] || i.tier) + '</span></div>' +
        '<div class="session-sub">' + esc(i.owner) + '</div></div>' +
        '<button class="btn btn-sm" data-unlink="' + esc(i.owner) +
        '">摘下来</button></div>').join('');
      html += '<div class="session-item" style="flex-direction:column;align-items:stretch">' +
        '<div class="session-main"><div class="session-name">' +
        esc(p.name || p.person) + '<span class="health-tag ok" style="margin-left:6px">' +
        (p.identities || []).length + ' 个账号</span></div>' +
        '<div class="session-sub">' + esc(p.note || '') + '</div></div>' +
        ids + '</div>';
    }
    if (loose && (loose.identities || []).length) {
      html += '<div class="session-item" style="flex-direction:column;align-items:stretch">' +
        '<div class="session-main"><div class="session-name">还没认领的账号' +
        '<span class="health-tag warn" style="margin-left:6px">' +
        loose.identities.length + '</span></div>' +
        '<div class="session-sub">每个账号的记忆各自独立，互不可见</div></div>' +
        loose.identities.slice(0, 20).map((i) =>
          '<div class="session-item" style="padding-left:12px">' +
          '<div class="session-main"><div class="session-name">' +
          esc(i.name || i.owner.split(':').pop()) +
          '<span class="health-tag" style="margin-left:6px">' + esc(i.platform) +
          '</span></div><div class="session-sub">' + esc(i.owner) + '</div></div>' +
          '</div>').join('') + '</div>';
    }
    list.innerHTML = html || '<div class="empty" style="padding:12px 0">' +
      '<p>还没有任何账号。跟她聊几句，或从上面挂一个。</p></div>';
  } catch (e) {
    toast('读取身份组失败');
  }
}

async function createPerson() {
  const name = $('#personName') ? $('#personName').value.trim() : '';
  if (!S.bridge) return;
  try {
    const r = JSON.parse(await S.bridge.create_person(name || '未命名'));
    if (!r.ok) return toast(r.error || '创建失败');
    toast('已建立「' + (name || '未命名') + '」');
    if ($('#personName')) $('#personName').value = '';
    loadPersons();
  } catch (e) { toast('创建失败'); }
}

async function linkPerson() {
  if (!S.bridge) return;
  const pid = $('#linkPerson') ? $('#linkPerson').value : '';
  const plat = $('#linkPlatform') ? $('#linkPlatform').value : '';
  const val = $('#linkValue') ? $('#linkValue').value.trim() : '';
  if (!pid) return toast('先建一个身份组');
  if (!val) return toast('填一下平台 id');
  try {
    const r = JSON.parse(await S.bridge.link_person(plat, val, pid));
    if (!r.ok) return toast(r.error || '挂载失败');
    toast('已挂上去 · 记忆将从这刻起汇总');
    if ($('#linkValue')) $('#linkValue').value = '';
    loadPersons();
    loadFacts();
  } catch (e) { toast('挂载失败'); }
}

async function unlinkPerson(owner) {
  if (!S.bridge) return;
  const ok = confirm('把这个账号从身份组上摘下来？' + String.fromCharCode(10) +
    String.fromCharCode(10) +
    '记忆不会被删，只是不再跟另一个平台的账号合并展示。');
  if (!ok) return;
  try {
    const r = JSON.parse(await S.bridge.unlink_person(owner));
    if (!r.ok) return toast(r.error || '摘除失败');
    toast('已摘下');
    loadPersons();
    loadFacts();
  } catch (e) { toast('摘除失败'); }
}

const bindPersonUI = () => {
  const on = (sel, ev, fn) => {
    const el = $(sel);
    if (el) el.addEventListener(ev, fn);
  };
  on('#btnCreatePerson', 'click', createPerson);
  on('#btnLinkPerson', 'click', linkPerson);
  document.addEventListener('click', (e) => {
    const u = e.target.closest('[data-unlink]');
    if (u) unlinkPerson(u.dataset.unlink);
  });
};

const bindOwnerUI = () => {
  const on = (sel, ev, fn) => {
    const el = $(sel);
    if (el) el.addEventListener(ev, fn);
  };
  on('#btnBindOwner', 'click', () => {
    const sel = $('#ownerPick');
    if (sel) bindOwner(sel.value);
  });
  on('#btnAddIdentity', 'click', addIdentity);
  document.addEventListener('click', (e) => {
    const t = e.target.closest('[data-tier]');
    if (t) { bindOwner(t.dataset.tier); return; }
    const v = e.target.closest('[data-vis]');
    if (v) setVisibility(v.dataset.vis, v.dataset.target);
  });
};

const bindFactUI = () => {
  const on = (sel, ev, fn) => {
    const el = $(sel);
    if (el) el.addEventListener(ev, fn);
  };
  on('#btnFactRefresh', 'click', () => loadFacts(0));
  on('#btnReflect', 'click', reflectNow);
  on('#btnMemRepair', 'click', repairMemory);
  on('#factStatus', 'change', () => loadFacts(0));
  on('#factQuery', 'keydown', (e) => {
    if (e.key === 'Enter') loadFacts(0);
  });
  on('#btnFactPrev', 'click', () => loadFacts(Math.max(0, S.facts.offset - 40)));
  on('#btnFactNext', 'click', () => {
    if (S.facts.offset + 40 < S.facts.total) loadFacts(S.facts.offset + 40);
  });
  document.addEventListener('click', (e) => {
    const t = e.target.closest('[data-forget]');
    if (!t) return;
    forgetFact(Number(t.dataset.forget));
  });
  // 话题下拉：改了立刻存，不是等刷新
  document.addEventListener('change', (e) => {
    const s = e.target.closest('[data-topic]');
    if (!s) return;
    setTopic(s.dataset.topic, s.value);
  });
  // 记忆池分栏
  document.querySelectorAll('#memTabs .subtab').forEach((b) => {
    b.addEventListener('click', () => switchMemTab(b.dataset.memtab));
  });
};
bindFactUI();
bindOwnerUI();
bindPersonUI();

/* ────────── 印象页 ──────────
 *
 * 横向分页：一人一页。切页只重画，不重新拉数据 ——
 * 数据每次进页面时拉一次就够，来回切不该反复打后端。
 */
(function bindImpressionUI() {
  document.addEventListener('click', (e) => {
    const tab = e.target.closest('[data-impr]');
    if (tab) {
      IMPR.active = tab.dataset.impr;
      renderImprTabs();
      renderImprPanes();
      return;
    }
    const rv = e.target.closest('[data-revoke]');
    if (rv) { revokeOpen(rv.dataset.revoke); return; }
    const pr = e.target.closest('[data-profile-refresh]');
    if (pr) { refreshProfile(pr.dataset.profileRefresh); return; }
    const pt = e.target.closest('[data-profile-toggle]');
    if (pt) { toggleProfile(pt.dataset.profileToggle, pt.dataset.to === '1'); return; }
    const save = e.target.closest('#btnSaveNote');
    if (save) saveImprNote();
  });
})();

/* ── 画像：重新归纳 / 停用 ──
 *
 * 归纳要调一次模型（2~10 秒），所以后端是后台线程，这里只等一个"已开始"。
 * 过几秒自动重读一次 —— 比让用户手动点刷新友好，也不用做进度条。
 */
async function refreshProfile(owner) {
  if (!S.bridge || !S.bridge.refresh_profile) return;
  setText('#profileHint', '正在归纳…');
  try {
    const r = JSON.parse(await S.bridge.refresh_profile(owner));
    if (!r.ok) { setText('#profileHint', r.error || '归纳失败'); return; }
    // 模型调用要几秒，等一会儿再重读；读早了会看到旧内容
    setTimeout(() => { loadImpression(); setText('#profileHint', ''); }, 9000);
  } catch (e) { setText('#profileHint', '归纳失败'); }
}

async function toggleProfile(owner, enable) {
  if (!S.bridge || !S.bridge.set_profile_enabled) return;
  if (!enable) {
    const NL = String.fromCharCode(10);
    const ok = confirm('停用这段画像？' + NL + NL +
      '她将不再按这段理解你 —— 但这段话还留着，随时能重新启用。');
    if (!ok) return;
  }
  try {
    const r = JSON.parse(await S.bridge.set_profile_enabled(owner, enable));
    if (!r.ok) return toast(r.error || '操作失败');
    toast(enable ? '已重新启用' : '已停用');
    loadImpression();
  } catch (e) { toast('操作失败'); }
}

/* ────────── 数据目录 ────────── */

async function loadDataRoot() {
  if (!S.bridge) return;
  try {
    const d = JSON.parse(await S.bridge.get_data_root());
    if (d.error) return toast(d.error);
    S.data = d;

    $('#dataRootPath').value = d.root || '';
    setText('#dataRootTag', d.isDefault ? '默认位置' : '自定义');
    setText('#dataRootFile',
      d.isDefault
        ? `默认位置。想换到别的盘就在下面填。`
        : `自定义位置。默认是 ${d.defaultRoot || '安装目录/data'}`);

    // 可写性：不可写就直接在页面上说清楚，别等用户点保存才失败
    const hint = $('#dataRootHint');
    if (d.writable === false) {
      hint.innerHTML = '<strong>这个数据目录现在写不进去。</strong>' +
        '改人格、改心情参数都会失败。<br>' +
        '可以换一个普通目录（用下面「搬过去」连同数据一起搬），' +
        '或者用管理员身份重新打开本程序。<br>' +
        `<span style="opacity:.7">系统返回：${esc(d.writeError || '')}</span>`;
      hint.className = 'hint-line is-err';
    } else if (hint.className.includes('is-err')) {
      hint.textContent = '';
      hint.className = 'hint-line';
    }

    renderLayout(d);
    await refreshWipePlan(true);
    await refreshWhalePlan();
  } catch (e) { toast('读取数据目录失败'); }
}

/* 三样东西各在哪、有没有。没有「份」的概念。 */
function renderLayout(d) {
  const box = $('#layoutList');
  const rows = [
    ['灵魂', d.soul, '她是谁。改这个不会动记忆。'],
    ['心情', d.mood, '此刻怎样。清空只影响这个。'],
    ['记忆', d.memory, '她记得的事。'],
  ];

  const fmt = (n) => {
    if (!n) return '';
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1048576).toFixed(1) + ' MB';
  };

  box.innerHTML = rows.map(([label, info, desc]) => {
    const ok = info && info.exists;
    return `<div class="session-row">
      <span class="session-name" style="font-family:var(--body-font);font-size:12px">${label}</span>
      <span class="session-meta" style="flex:1;text-align:left;padding-left:10px">${esc(desc)}</span>
      <span class="session-meta">${ok ? esc(info.name) : '（还没有）'}</span>
      <span class="session-meta">${ok ? fmt(info.bytes) : '—'}</span>
      <span class="tag${ok ? ' ok' : ''}">${ok ? '在' : '无'}</span>
    </div>`;
  }).join('');

  // 老布局残留提示
  if (d.legacy && d.legacy.found) {
    box.innerHTML += `<div class="session-row" style="margin-top:8px">
      <span class="session-name" style="font-family:var(--body-font);font-size:12px;color:var(--warn)">老布局</span>
      <span class="session-meta" style="flex:1;text-align:left;padding-left:10px">
        发现 <code>${esc(d.legacy.key)}</code> 目录，里面还有人格式数据
      </span>
      <button class="btn btn-sm" id="btnMigrate">搬进新布局</button>
    </div>`;
    const b = $('#btnMigrate');
    if (b) b.addEventListener('click', async () => {
      b.disabled = true;
      try {
        const r = JSON.parse(await S.bridge.migrate_legacy());
        if (!r.ok) { toast(r.error || '迁移失败'); return; }
        toast('已搬过来');
        S.soul = null;
        await loadDataRoot();
      } catch (e) { toast('迁移失败'); }
      finally { b.disabled = false; }
    });
  }

  setText('#layoutTag', d.writable === false ? '不可写' : '正常');
}

/* ── 清空 ── */

async function refreshWipePlan(silent) {
  if (!S.bridge) return;
  try {
    const d = JSON.parse(await S.bridge.inspect_wipe());
    const el = $('#wipePlan');
    if (!d.ok) {
      el.textContent = '盘点失败：' + (d.error || '');
      el.className = 'card-desc is-err';
      return;
    }
    if (!d.items.length) {
      el.textContent = '没有可清的东西 —— 已经是干净的了。';
      el.className = 'card-desc';
      return;
    }
    const del = d.items.filter((i) => i.action === '删除');
    const keep = d.items.filter((i) => i.action === '保留');
    const parts = [];
    if (keep.length) parts.push('保留：' + keep.map((i) => i.name).join('、'));
    if (del.length) {
      const files = del.reduce((n, i) => n + (i.files || 1), 0);
      parts.push(`删除 ${del.length} 项 / ${files} 个文件 / ${fmtBytes(d.totalBytes)}`);
    }
    el.textContent = parts.join('　|　');
    el.className = 'card-desc';
  } catch (e) {
    if (!silent) toast('盘点失败');
  }
}

$('#btnWipePreview').addEventListener('click', () => {
  refreshWipePlan(false);
  refreshWhalePlan();
  toast('已重新盘点');
});

$('#btnWipe').addEventListener('click', async () => {
  if (!S.bridge) return toast('后端未就绪');
  if (!confirm('清空心情和全部记忆？\n\n灵魂（她是谁）会保留 —— 她还在，只是忘了。\n删之前会自动备份。')) return;
  const btn = $('#btnWipe');
  btn.disabled = true;
  const h = $('#wipeHint');
  try {
    const r = JSON.parse(await S.bridge.apply_wipe());
    if (!r.ok) {
      h.textContent = '清空失败：' + (r.error || '');
      h.className = 'card-desc is-err';
      return toast('清空失败');
    }
    h.textContent = r.backup ? '已清空。备份在 ' + r.backup : '已清空。';
    h.className = 'card-desc is-ok';
    toast('已清空');
    S.soul = null;
    await loadDataRoot();
    await refreshWipePlan(true);
    refreshWhalePlan();
  } catch (e) {
    toast('清空失败');
  } finally {
    btn.disabled = false;
  }
});

/* ── 鲸落 ── */

async function refreshWhalePlan() {
  if (!S.bridge) return;
  try {
    const d = JSON.parse(await S.bridge.whale_fall_status());
    const el = $('#whalePlan');
    if (!d.ok) {
      el.textContent = '盘点失败：' + (d.error || '');
      el.className = 'card-desc is-err';
      return;
    }
    const items = d.items || [];
    if (d.isEmpty || !items.length) {
      el.textContent = '数据目录已经是空的 —— 她本来就不在这里。';
      el.className = 'card-desc';
      return;
    }
    const files = items.reduce((n, i) => n + (i.files || 1), 0);
    el.innerHTML =
      `将删除 <strong>${items.length} 项 / ${files} 个文件 / ${fmtBytes(d.totalBytes)}</strong>` +
      `：${items.slice(0, 6).map((i) => esc(i.name)).join('、')}` +
      (items.length > 6 ? ' …' : '') +
      `<br>备份会放在：<code>${esc(d.backupTo || '')}</code>`;
    el.className = 'card-desc';
  } catch (e) { /* 静默 */ }
}

$('#btnWhale').addEventListener('click', async () => {
  if (!S.bridge) return toast('后端未就绪');
  const typed = ($('#whaleConfirm').value || '').trim();
  if (typed !== '鲸落') {
    toast('请先输入「鲸落」两个字');
    $('#whaleConfirm').focus();
    return;
  }
  if (!confirm('最后一次确认：\n\n灵魂、心情、记忆全部删除。\n她不存在了，留下的只有一份备份。\n\n继续？')) return;

  const btn = $('#btnWhale');
  btn.disabled = true;
  const h = $('#whaleHint');
  try {
    const r = JSON.parse(await S.bridge.whale_fall(typed));
    if (!r.ok) {
      h.textContent = '删除失败：' + (r.error || '');
      h.className = 'card-desc is-err';
      return toast('鲸落失败');
    }
    const n = (r.deleted || []).length;
    h.innerHTML = `已删除 ${n} 项。` +
      (r.backup ? `<br>备份在 <code>${esc(r.backup)}</code>` : '');
    h.className = 'card-desc is-ok';
    toast('鲸落完成');
    $('#whaleConfirm').value = '';
    S.soul = null;
    await loadDataRoot();
    await refreshWipePlan(true);
    await refreshWhalePlan();
  } catch (e) {
    toast('鲸落失败');
  } finally {
    btn.disabled = false;
  }
});

/* ── 数据根：打开 / 选择 / 只改指向 / 搬家 / 恢复默认 ── */
$('#btnDataRootOpen').addEventListener('click', async () => {
  const p = $('#dataRootPath').value;
  if (!p) return;
  try {
    await S.bridge.open_path(p);
  } catch (e) { toast('打不开'); }
});

$('#btnDataRootPick').addEventListener('click', async () => {
  try {
    const r = JSON.parse(await S.bridge.pick_directory($('#dataRootPath').value));
    if (r.path) $('#dataRootTarget').value = r.path;
    else if (r.error) toast(r.error);
  } catch (e) { toast('选择失败'); }
});

/* 只改指向：不搬文件。新目录是空的，所以要说清楚。 */
$('#btnDataRootSave').addEventListener('click', async () => {
  const target = $('#dataRootTarget').value.trim();
  if (!target) return toast('先填目标目录');
  if (!confirm(`把数据目录改到：\n${target}\n\n` +
               '**只改指向，不搬文件**。如果那个目录是空的，' +
               '打开会看到一份全新的（出厂）人格。\n\n' +
               '要带着她一起过去请用「搬过去」。')) return;

  const btn = $('#btnDataRootSave');
  btn.disabled = true;
  const h = $('#dataRootHint');
  try {
    const r = JSON.parse(await S.bridge.set_data_root(target));
    if (!r.ok) {
      h.textContent = '改不了：' + (r.error || '');
      h.className = 'hint-line is-err';
      return toast('保存失败');
    }
    h.textContent = '已改到 ' + r.root;
    h.className = 'hint-line is-ok';
    toast('已保存');
    S.soul = null;
    await loadDataRoot();
  } catch (e) { toast('保存失败'); }
  finally { btn.disabled = false; }
});

/* 搬家：先复制，复制成功了才改指向。 */
$('#btnDataRootMove').addEventListener('click', async () => {
  const target = $('#dataRootTarget').value.trim();
  if (!target) return toast('先填目标目录');
  try {
    const d = JSON.parse(await S.bridge.inspect_move(target));
    if (!d.ok) return toast(d.error || '检查失败');

    const files = d.items.reduce((n, it) => n + (it.files || 0), 0);
    let msg = `把 ${d.items.length} 项 / ${files} 个文件 / ${fmtBytes(d.totalBytes)} ` +
              `搬到：\n${d.dst}`;
    if (d.conflicts.length) {
      msg += `\n\n⚠ 目标里已有 ${d.conflicts.length} 个同名项，会被覆盖：\n` +
             d.conflicts.slice(0, 8).join('\n');
    }
    if (d.dstWritable === false) {
      msg += `\n\n⚠ 目标目录写不进去（${d.dstWriteError}）—— 搬过去也用不了`;
    }
    msg += '\n\n源目录会被改名成 .old-时间戳 留着，不直接删。继续？';
    if (!confirm(msg)) return;

    const h = $('#dataRootHint');
    h.textContent = '正在搬…';
    h.className = 'hint-line';

    const r = JSON.parse(await S.bridge.apply_move(target));
    if (!r.ok) {
      h.textContent = '搬家失败：' + (r.error || '') + '（数据目录未改动）';
      h.className = 'hint-line is-err';
      return toast('搬家失败');
    }
    h.textContent = '已搬到 ' + r.root;
    h.className = 'hint-line is-ok';
    toast('已搬家');
    S.soul = null;
    await loadDataRoot();
  } catch (e) { toast('搬家失败'); }
});

$('#btnDataRootDefault').addEventListener('click', async () => {
  if (!confirm(`恢复成默认位置：\n${S.data?.defaultRoot || '安装目录/data'}\n\n` +
               '原来的目录不会被删。')) return;
  try {
    const r = JSON.parse(await S.bridge.set_data_root(''));
    if (!r.ok) return toast(r.error || '失败');
    toast('已恢复默认');
    S.soul = null;
    await loadDataRoot();
  } catch (e) { toast('失败'); }
});

/* ────────── 设置 ────────── */

/* 模型接入。
 *
 * 流程照抄各家客户端（也照抄 magpie）：填地址 + Key → **从厂商拉真实模型
 * 列表** → 选一个。预设给的只是"地址和协议的默认值"，模型名不能靠猜 ——
 * 各家自己出什么模型，只有它自己的 /models 知道。
 *
 * 第一版这里写的是硬编码的模型建议值（datalist），那是偷工减料：
 * 模型一更新就过期，而且"你这把 Key 能用哪些"本来就不是常量。
 */
let CHANNELS = [];
let PROTOCOLS = [];
let FETCHED_MODELS = [];

function channelById(id) {
  return CHANNELS.find((c) => c.id === id) || CHANNELS[CHANNELS.length - 1];
}

/** 把模型列表灌进下拉。当前值不在列表里也保留 —— 别让用户以为丢了。 */
function setModelOptions(models, current) {
  const sel = $('#cfgModelSel');
  sel.innerHTML = '';

  if (!models || !models.length) {
    const o = document.createElement('option');
    o.value = current || '';
    o.textContent = current || '— 先点右边「获取模型」 —';
    sel.appendChild(o);
    return;
  }

  const seen = new Set();
  models.forEach((m) => {
    if (seen.has(m)) return;
    seen.add(m);
    const o = document.createElement('option');
    o.value = m;
    o.textContent = m;
    sel.appendChild(o);
  });

  if (current && !seen.has(current)) {
    const o = document.createElement('option');
    o.value = current;
    o.textContent = current + '（不在这个列表里）';
    sel.insertBefore(o, sel.firstChild);
  }
  const pick = current || models[0] || '';
  sel.value = pick;
  if (!current) $('#cfgModel').value = pick;
}

/** 渠道变了：填地址/协议，并清掉上一个渠道的模型列表。 */
function applyChannel(id, { keepValues = false } = {}) {
  const ch = channelById(id);
  if (!ch) return;

  if (!keepValues) {
    $('#cfgBaseUrl').value = ch.base_url || '';
    $('#cfgProtocol').value = ch.protocol || 'openai';
    FETCHED_MODELS = [];
    setModelOptions([], '');
    $('#cfgModel').value = '';
  }

  $('#channelNote').textContent = ch.note || '选一家，粘上 Key 就能用。';
  const keyBox = $('#cfgApiKey');
  keyBox.placeholder = ch.needs_key ? 'sk-…' : '这一家不需要 Key';
  keyBox.disabled = !ch.needs_key;
  $('#keyNote').textContent = ch.needs_key
    ? (ch.key_hint ? `从 ${ch.key_hint} 拿。` : '这一家需要 API Key。')
    : '本地服务，不需要 Key。';

  const hint = $('#keyHint');
  if (ch.doc) {
    hint.href = ch.doc;
    hint.textContent = ch.needs_key ? '去哪拿？' : '官网';
    hint.classList.remove('hidden');
  } else {
    hint.classList.add('hidden');
  }
}

function setTestResult(kind, text) {
  const el = $('#testResult');
  el.className = 'test-result' + (kind ? ' ' + kind : '');
  el.textContent = text;
}

function setModelNote(kind, text) {
  const el = $('#modelNote');
  el.textContent = text;
  // 它是 <small>，跟着 .field > small 的样式走。这里只改颜色，
  // 换 className 会把那套样式丢掉。
  el.style.color = kind === 'ok' ? 'var(--ok)'
    : kind === 'err' ? 'var(--error)' : '';
}

/** 当前表单值装成后端要的 payload。 */
function llmPayload() {
  return {
    channel: $('#cfgChannel').value,
    base_url: $('#cfgBaseUrl').value.trim(),
    model: $('#cfgModel').value.trim(),
    api_key: $('#cfgApiKey').value,
    protocol: $('#cfgProtocol').value,
    temperature: parseFloat($('#cfgTemp').value) || 1.0,
    max_tokens: parseInt($('#cfgMaxTokens').value, 10) || 2600,
  };
}

/** 拉模型列表。成功就灌进下拉；失败只提示，不挡手填。 */
async function fetchModels(quiet = false) {
  if (!S.bridge) return;
  const btn = $('#btnFetchModels');
  btn.disabled = true;
  setModelNote('', '正在获取…（最多 20 秒）');
  try {
    const r = JSON.parse(await S.bridge.fetch_models(JSON.stringify(llmPayload())));
    if (!r.ticket) {
      setModelNote('err', r.error || '获取失败');
      return;
    }
    for (let i = 0; i < 22; i++) {
      await new Promise((res) => setTimeout(res, 1000));
      const p = JSON.parse(await S.bridge.poll_fetch(r.ticket));
      if (p.done) {
        if (p.ok) {
          FETCHED_MODELS = p.models || [];
          setModelOptions(FETCHED_MODELS, $('#cfgModel').value.trim());
          setModelNote('ok', `拉到 ${FETCHED_MODELS.length} 个模型，在上面选一个。`);
          if (!quiet) toast(`拿到 ${FETCHED_MODELS.length} 个模型`);
        } else {
          setModelNote('err', (p.text || '获取失败') + '　—— 也可以直接在下面手填。');
          if (!quiet) toast('获取失败');
        }
        return;
      }
      setModelNote('', `正在获取…（${i + 1}s）`);
    }
    setModelNote('err', '等太久了，检查地址和网络。也可以直接手填。');
  } catch (e) {
    setModelNote('err', '获取出错：' + (e.message || e));
  } finally {
    btn.disabled = false;
  }
}

$('#btnFetchModels')?.addEventListener('click', () => fetchModels());

$('#cfgModelSel')?.addEventListener('change', (e) => {
  if (e.target.value) $('#cfgModel').value = e.target.value;
});

$('#cfgModel')?.addEventListener('input', (e) => {
  const v = e.target.value.trim();
  const sel = $('#cfgModelSel');
  if ([...sel.options].some((o) => o.value === v)) sel.value = v;
});

$('#cfgChannel')?.addEventListener('change', async (e) => {
  applyChannel(e.target.value);
  setTestResult('', '换了渠道，先「获取模型」再「测试连接」。');
  // 本地服务（Magpie / Ollama / LM Studio）不用填 Key，直接拉一次更顺
  const ch = channelById(e.target.value);
  if (ch && !ch.needs_key && ch.base_url) {
    await fetchModels(true);
  }
});

async function loadSettings() {
  if (!S.bridge) return;
  try {
    const c = JSON.parse(await S.bridge.get_config());

    CHANNELS = c.channels || [];
    PROTOCOLS = c.protocols || [];

    const sel = $('#cfgChannel');
    if (sel.options.length !== CHANNELS.length) {
      sel.innerHTML = '';
      CHANNELS.forEach((ch) => {
        const o = document.createElement('option');
        o.value = ch.id;
        o.textContent = ch.name;
        sel.appendChild(o);
      });
    }
    const psel = $('#cfgProtocol');
    if (psel.options.length !== PROTOCOLS.length) {
      psel.innerHTML = '';
      PROTOCOLS.forEach((p) => {
        const o = document.createElement('option');
        o.value = p.id;
        o.textContent = p.label;
        psel.appendChild(o);
      });
    }

    const llm = c.llm || {};
    sel.value = llm.channel || 'deepseek';
    $('#cfgBaseUrl').value = llm.base_url || '';
    $('#cfgModel').value = llm.model || '';
    $('#cfgApiKey').value = llm.api_key || '';
    $('#cfgProtocol').value = llm.protocol || 'openai';
    $('#cfgTemp').value = llm.temperature ?? 1.0;
    $('#cfgMaxTokens').value = llm.max_tokens ?? 2600;
    applyChannel(sel.value, { keepValues: true });
    // ⚠ 每次重新读配置都要清空上次拉到的列表。
    //   不清的话，换了渠道/重载之后下拉里还挂着别家的模型 ——
    //   用户会以为"这个渠道也有这些模型"（实测踩过：切回 Magpie 之后
    //   下拉里还在显示上一个自定义供应商的 agnes-*）。
    FETCHED_MODELS = [];
    setModelOptions([], llm.model || '');

    setModelNote('', '点「获取模型」从这家拉真实列表。拉不到就直接手填。');

    $('#cfgInterval').value = c.interval ?? 1.2;
    $('#cfgMaxChars').value = c.max_reply_chars ?? 300;
    $('#cfgQuiet').value = (c.quiet_hours || []).join(',');
    $('#cfgAllowSelf').checked = !!c.allow_self;
    $('#cfgGroupReply').checked = c.group_reply !== false;
    $('#cfgGroupAllowAll').value = (c.group_allow_all || []).join(',');

    const tag = $('#modelTag');
    tag.textContent = llm.ready
      ? (llm.preset_name || '已配置')
      : (llm.status || '未配置');
    tag.className = 'tag ' + (llm.ready ? 'ok' : 'warn');

    setTestResult('', '真发一次请求验证。填完先点这个。');
    refreshModelHealth();
    refreshTraceList();
  } catch (e) { toast('读取设置失败'); }
}

$('#btnTestModel')?.addEventListener('click', async () => {
  if (!S.bridge) return toast('后端未就绪');
  const btn = $('#btnTestModel');
  btn.disabled = true;
  setTestResult('', '正在连接…（最多等 25 秒）');
  try {
    const r = JSON.parse(await S.bridge.test_model(JSON.stringify(llmPayload())));
    if (!r.ticket) {
      setTestResult('err', r.error || '启动测试失败');
      return;
    }
    // 轮询。测试要真发一次请求，同步做会把窗口冻住，所以走票据。
    for (let i = 0; i < 30; i++) {
      await new Promise((res) => setTimeout(res, 1000));
      const p = JSON.parse(await S.bridge.poll_test(r.ticket));
      if (p.done) {
        setTestResult(p.ok ? 'ok' : 'err', p.text || '');
        toast(p.ok ? '连接正常' : '连接失败');
        return;
      }
      setTestResult('', `正在连接…（${i + 1}s）`);
    }
    setTestResult('err', '等太久了，可能网络卡住或者地址不对');
  } catch (e) {
    setTestResult('err', '测试出错：' + (e.message || e));
  } finally {
    btn.disabled = false;
  }
});

$('#btnSave').addEventListener('click', async () => {
  if (!S.bridge) return toast('后端未就绪');
  const p = {
    llm: {
      channel: $('#cfgChannel').value,
      base_url: $('#cfgBaseUrl').value.trim(),
      model: $('#cfgModel').value.trim(),
      api_key: $('#cfgApiKey').value,
      protocol: $('#cfgProtocol').value,
      temperature: parseFloat($('#cfgTemp').value) || 1.0,
      max_tokens: parseInt($('#cfgMaxTokens').value, 10) || 2600,
    },
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
      markClean('settings');
      toast('已保存');
      const h = $('#saveHint');
      h.textContent = '已保存 ' + hhmmss();
      h.classList.add('show');
      setTimeout(() => h.classList.remove('show'), 2000);
      loadSettings();
    } else toast(r.error || '保存失败');
  } catch (e) { toast('保存失败'); }
});

/* ────────── 更新 ──────────
 *
 * 两条信息必须同时可读：**当前版本** 和 **最新版本**。
 * 只报"发现新版"而不说自己在哪一版，用户没法判断该不该装。
 */
function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function renderUpdate(d) {
  const row = $('#updateRow');
  if (!row) return;
  const top = $('#btnUpdate');          // 顶栏那个图标按钮
  if (!d || !d.has_update) {
    row.hidden = true;
    if (top) top.hidden = true;
    return;
  }
  row.hidden = false;
  if (top) {
    top.hidden = false;
    top.title = `发现新版本 ${d.latest}（当前 ${d.current}）`;
  }
  const when = fmtDate(d.published);
  $('#updateTitle').textContent =
    `新版本 ${d.latest}（当前 ${d.current}${when ? '，' + when : ''}）`;
  const notes = (d.notes || '').trim();
  let hint = notes
    ? notes.split('\n').slice(0, 3).join(' ').slice(0, 160)
    : '点右边下载。装完需要手动关一下程序。';
  // 走的是上次缓存 / 查不到时的旧结果，得说清楚，别让人以为是刚查到的
  if (d.stale) hint = `（上次查到的结果，本次没查到：${d.stale_reason || '网络不通'}）` + hint;
  else if (d.cached) hint = '（本轮走缓存）' + hint;
  $('#updateNotes').textContent = hint;
}

async function checkUpdate(manual = false) {
  if (!S.bridge || !S.bridge.check_update) return;
  const btn = $('#btnCheckUpdate');
  if (manual && btn) { btn.disabled = true; btn.textContent = '检查中'; }
  try {
    const d = JSON.parse(await S.bridge.check_update());
    if (d.repo) {
      const el = $('#updateSource');
      if (el) el.textContent = `从 GitHub（${d.repo}）读取最新版本。发现新版可以一键下载。`;
    }
    if (d.disabled) {
      if (manual) toast('自动更新已关闭（config.json 的 update.enabled）');
      return;
    }
    if (d.error) {
      if (manual) toast(d.error);
      renderUpdate(null);
      return;
    }
    renderUpdate(d);
    if (!d.has_update) {
      if (manual) {
        toast(d.cached
          ? `已是最新（${d.current}，本轮走缓存）`
          : `已是最新（${d.current}）`);
      }
      return;
    }
    S.updateInfo = d;
    if (manual || !d.cached) toast(`发现新版本 ${d.latest}`);
  } catch (e) {
    if (manual) toast('检查失败：' + (e.message || e));
  } finally {
    if (manual && btn) { btn.disabled = false; btn.textContent = '检查'; }
  }
}

$('#btnCheckUpdate')?.addEventListener('click', () => checkUpdate(true));

/** 刷新「她这轮看到了什么」：列表 + 可写性 + 模型健康。 */
async function refreshTraceList() {
  const sel = $('#tracePick');
  const note = $('#traceNote');
  if (!sel || !S.bridge || !S.bridge.prompt_trace_list) return;
  try {
    const d = JSON.parse(await S.bridge.prompt_trace_list());
    const st = d.stats || {};
    const items = d.items || [];
    sel.innerHTML = '';
    items.forEach((it) => {
      const o = document.createElement('option');
      o.value = it.path;
      const err = it.error ? `｜失败:${it.error}` : '';
      o.textContent = `${it.time}｜${it.chat || '-'}｜${it.elapsed_ms}ms｜` +
        `${it.user_chars}字${err}`;
      sel.appendChild(o);
    });
    if (!items.length) {
      const o = document.createElement('option');
      o.value = '';
      o.textContent = '（还没有快照）';
      sel.appendChild(o);
    }
    if (note) {
      if (st.writable === false) {
        note.textContent = '快照目录写不进去：' + (st.write_error || '') +
          '。把 BigFish.exe 加进杀软白名单，或用管理员运行。';
      } else if (st.enabled === false) {
        note.textContent = '快照已关闭（config.json 的 trace.enabled=false）。';
      } else {
        note.textContent = `最近 ${items.length} 份，共 ${st.count || 0} 份 / ` +
          `${Math.round((st.bytes || 0) / 1024)} KB，保留最近 ${st.keep || 200} 份。`;
      }
    }
    refreshTraceSections(sel.value);
  } catch (e) { /* 设置页不该因为快照失败而报错 */ }
}

/** 画「这一轮的字数花在哪」：每段一行，段名 / 字数 / 占比。
 *
 * 以前这里只有一个总字数，看到 system 8000 字也不知道是哪一段涨的 ——
 * 人格？记忆？还是插件新塞了一段？只能打开 JSON 自己数。
 */
async function refreshTraceSections(path) {
  const box = $('#traceSections');
  if (!box) return;
  box.innerHTML = '';
  if (!path || !S.bridge || !S.bridge.prompt_trace_sections) return;
  try {
    const d = JSON.parse(await S.bridge.prompt_trace_sections(path));
    const secs = d.sections || [];
    if (!secs.length) {
      box.textContent = '这份快照没有段落信息（0.11.1 之前写的）。';
      return;
    }
    const total = secs.reduce((a, s) => a + (s.chars || 0), 0) || 1;
    secs.forEach((s) => {
      const pct = Math.round(((s.chars || 0) / total) * 100);
      const row = document.createElement('div');
      row.className = 'trace-sec';
      row.innerHTML =
        `<span class="trace-sec-name">${esc(s.name || ('座位 ' + s.seat))}</span>` +
        `<span class="trace-sec-bar" style="width:${Math.max(4, Math.min(180, pct * 1.8))}px"></span>` +
        `<span class="trace-sec-num">${s.chars || 0} 字 · ${pct}%</span>`;
      box.appendChild(row);
    });
  } catch (e) { box.textContent = ''; }
}

$('#tracePick')?.addEventListener('change', (e) => refreshTraceSections(e.target.value));

/** 模型最近一次调用是否失败 —— 回答「她为什么没回」。 */
async function refreshModelHealth() {
  const el = $('#modelHealth');
  if (!el || !S.bridge || !S.bridge.model_health) return;
  try {
    const d = JSON.parse(await S.bridge.model_health());
    const last = d.last || {};
    if (last.message) {
      el.textContent = `上次调用失败（${last.time}）：${last.message}`;
      el.style.color = '#c66';
    } else {
      el.textContent = '最近一次模型调用正常。';
      el.style.color = '';
    }
  } catch (e) { el.textContent = ''; }
}

async function openTrace(path) {
  if (!S.bridge || !S.bridge.prompt_trace_open) return toast('当前版本不支持');
  try {
    const r = JSON.parse(await S.bridge.prompt_trace_open(path || ''));
    if (!r.ok) return toast('打不开：' + (r.error || '未知'));
    toast(path ? '已用默认程序打开快照' : '已打开快照目录');
  } catch (e) { toast('打开失败：' + e); }
}
$('#btnTraceView')?.addEventListener('click', () => {
  const p = $('#tracePick')?.value || '';
  if (!p) return toast('还没有快照');
  openTrace(p);
});
$('#btnTraceDir')?.addEventListener('click', () => openTrace(''));

/** 真正执行下载替换。顶栏图标和卡片按钮都走这里。 */
async function doUpdate() {
  const d = S.updateInfo;
  if (!d) return toast('没有可用更新，先点「检查」');
  const ok = confirm(
    `发现新版本 ${d.latest}\n\n` +
    `当前版本 ${d.current}\n\n` +
    `现在下载并安装？下载完成后需要手动关闭本程序，`
    + `关掉后会自动替换。`);
  if (!ok) return;

  toast('开始下载…（两百兆左右，看网速）');
  try {
    const r = JSON.parse(await S.bridge.apply_update());
    if (r.ok) {
      toast('下载完成。关闭本程序后会自动替换并重启。');
      $('#updateNotes').textContent =
        '已下载完成。关掉本程序，替换脚本会把新版装上去。';
    } else {
      toast(r.error || '更新失败');
    }
  } catch (e) {
    toast('更新失败：' + (e.message || e));
  }
}

// 顶栏那个图标按钮：跳到设置页的更新那一行，不直接下载 ——
// 图标一点就开始下载两百兆，太容易误触。
$('#btnUpdate')?.addEventListener('click', () => {
  go('settings');
  const row = $('#updateRow');
  if (row && !row.hidden) row.scrollIntoView({ block: 'center' });
});

$('#btnDoUpdate')?.addEventListener('click', doUpdate);

/* ────────── 插件 ────────── */

const PLUG = { data: null, current: null, draft: {} };

/** 每种形态一个小图标（内联 SVG，和侧栏同一套线条风格） */
const PLUG_ICONS = {
  prompt: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M5 6h14M5 11h10M5 16h7"/></svg>',
  gate: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 7h16M4 17h16M12 7v10"/><circle cx="12" cy="12" r="2"/></svg>',
  hook: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12h11M12 7l5 5-5 5"/></svg>',
  // 表情包插件：一张笑脸。和其它三类视觉上要能分开 ——
  // 它是唯一一个会往聊天里"发东西"的插件。
  sticker: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><rect x="3.5" y="3.5" width="17" height="17" rx="4.5"/><path d="M9 10h.01M15 10h.01" stroke-linecap="round"/><path d="M9 14.5c.9 1.1 1.9 1.6 3 1.6s2.1-.5 3-1.6" stroke-linecap="round"/></svg>',
};

function plugById(id) {
  return ((PLUG.data && PLUG.data.plugins) || []).find((p) => p.id === id) || null;
}

async function loadPlugins() {
  if (!S.bridge) return;
  try {
    const d = JSON.parse(await S.bridge.list_plugins());
    if (d && d.error) {
      $('#plugRows').innerHTML =
        `<div class="empty"><h3>读不到插件清单</h3><p>${esc(d.error)}</p></div>`;
      return;
    }
    PLUG.data = d;
    renderPluginList();
    renderPluginSeats();
    // 入口可见性也吃这份数据。不在这里同步，就会出现「插件页显示已启用、
    // 侧边栏还是没有表情包入口」——两处各存一份的老问题。
    if (typeof syncPluginNav === 'function') syncPluginNav();
    // 详情页开着时同步刷新（改完开关后回填）
    if (PLUG.current) openPlugin(PLUG.current);
  } catch (e) {
    $('#plugRows').innerHTML =
      `<div class="empty"><h3>读不到插件清单</h3><p>${esc(e.message || e)}</p></div>`;
  }
}

function renderPluginList() {
  const rows = $('#plugRows');
  const list = (PLUG.data && PLUG.data.plugins) || [];
  const on = list.filter((p) => p.enabled).length;
  setText('#plugCount', list.length ? `已启用 ${on} / ${list.length}` : '');

  if (!list.length) {
    rows.innerHTML = '<div class="empty" style="padding:28px 0"><p>还没有登记任何插件。</p></div>';
    return;
  }

  rows.innerHTML = list.map((p) => `
    <div class="plug-row" data-plug="${esc(p.id)}">
      <div class="plug-icon">${PLUG_ICONS[p.scope] || PLUG_ICONS.prompt}</div>
      <button class="plug-main" type="button" data-open="${esc(p.id)}">
        <div class="plug-title">${esc(p.title)}${
          p.seat ? `<span class="plug-seat">${esc(p.seat)}</span>` : ''}</div>
        <div class="plug-desc">${esc(p.desc)}</div>
        <div class="plug-meta">
          <span>${esc(p.scopeLabel)}</span>
          ${p.enabled ? '<span class="plug-dot">·</span><span class="plug-state">已启用</span>' : ''}
        </div>
      </button>
      <label class="switch-control" title="${p.enabled ? '停用' : '启用'}">
        <input type="checkbox" data-toggle="${esc(p.id)}" ${p.enabled ? 'checked' : ''}>
        <span class="switch-track"></span>
      </label>
    </div>`).join('');
}

function renderPluginSeats() {
  const box = $('#plugSeats');
  const seats = (PLUG.data && PLUG.data.seats) || [];
  box.innerHTML = seats.map((s) => `
    <span class="plug-seatchip${s.builtin ? ' is-builtin' : ''}"
          title="${esc(s.note)}${s.builtin ? '（内建，不可关）' : ''}">
      <b>${s.order}</b>${esc(s.label)}
    </span>`).join('');
}

function openPlugin(id) {
  const p = plugById(id);
  if (!p) return;
  PLUG.current = id;
  PLUG.draft = { ...(p.config || {}) };

  $('#plugListWrap').hidden = true;
  $('#plugDetailWrap').hidden = false;
  // 详情自己有完整标题，page-heading 再来一遍就重复了
  const head = document.querySelector('.page[data-page="plugins"] .page-heading');
  if (head) head.hidden = true;
  setText('#breadcrumb', '插件 / ' + p.title);

  $('#plugIcon').innerHTML = PLUG_ICONS[p.scope] || PLUG_ICONS.prompt;
  setText('#plugTitle', p.title);
  setText('#plugDesc', p.desc);

  const meta = [];
  meta.push(`<span>${esc(p.scopeLabel)}</span>`);
  if (p.seat) meta.push(`<span class="plug-seat">座位 ${esc(p.seat)}</span>`);
  meta.push(`<span class="plug-state${p.enabled ? '' : ' is-off'}">${p.enabled ? '已启用' : '未启用'}</span>`);
  if (p.configFields && p.configFields.length) {
    meta.push(`<span>${p.configFields.length} 项可调</span>`);
  }
  $('#plugMeta').innerHTML = meta.join('');

  $('#plugToggle').checked = !!p.enabled;

  renderPluginWhy(p);
  renderPluginFields(p);
  renderPluginLadder(p);
  refreshPluginPreview();

  setText('#plugHint', '');
  $('#plugSave').disabled = true;
}

/** ① 它做什么：为什么需要 + 会影响什么 / 不会做什么 */
function renderPluginWhy(p) {
  // 说明文字里允许 **加粗** —— 先转义再替换，顺序不能反
  const fmt = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

  const why = $('#plugWhy');
  if (p.why) {
    why.innerHTML = p.why.split('\n').filter(Boolean)
      .map((line) => `<p>${fmt(line)}</p>`).join('');
    why.hidden = false;
  } else {
    why.hidden = true;
  }

  const fill = (sel, list, empty) => {
    const box = $(sel);
    if (list && list.length) {
      box.innerHTML = list.map((x) => `<li>${fmt(x)}</li>`).join('');
    } else {
      box.innerHTML = `<li class="is-empty">${esc(empty)}</li>`;
    }
  };
  fill('#plugAffects', p.affects, '未声明');
  fill('#plugLimits', p.limits, '未声明');
}

/** ② 她实际收到的：调后端实时渲染 */
async function refreshPluginPreview() {
  const id = PLUG.current;
  const box = $('#plugPreview');
  if (!id || !S.bridge) return;
  const p = plugById(id);

  if (p && p.scope !== 'prompt') {
    box.textContent = '这个插件不往提示词里加内容（它是「'
      + (p.scopeLabel || p.scope) + '」类），所以没有注入原文。';
    setText('#plugPreviewWhere', '');
    setText('#plugPreviewNote', '');
    return;
  }

  setText('#plugPreviewWhere', p && p.seat ? `拼在 ${p.seat}` : '');
  box.textContent = '渲染中…';
  try {
    const r = JSON.parse(await S.bridge.preview_plugin(JSON.stringify({ id })));
    if (!r.ok) {
      box.textContent = '渲染失败：' + (r.error || '未知错误');
      return;
    }
    box.textContent = r.text || '（这一段这一轮不注入）';
    setText('#plugPreviewNote', p && p.enabled
      ? '用当前时间与当前参数渲染'
      : '⚠ 插件已停用——上面是「启用后会注入」的样子，现在并没有生效');
  } catch (e) {
    box.textContent = '渲染失败：' + (e.message || e);
  }
}

/** ④ 在提示词里的位置：把身份序画成阶梯，标出这一层 */
function renderPluginLadder(p) {
  const box = $('#plugLadder');
  const seats = (PLUG.data && PLUG.data.seats) || [];
  if (!seats.length) { box.innerHTML = ''; return; }
  const mine = p.order;
  box.innerHTML = seats.map((s) => {
    const isMine = mine && s.order === mine;
    const cls = 'plug-step'
      + (s.builtin ? ' is-builtin' : '')
      + (isMine ? ' is-mine' : '');
    return `<div class="${cls}">
      <span class="plug-step-order">${s.order}</span>
      <span class="plug-step-label">${esc(s.label)}</span>
      <span class="plug-step-note">${esc(s.note)}</span>
      ${isMine ? '<span class="plug-step-here">← 这个插件</span>' : ''}
      ${s.builtin ? '<span class="plug-step-tag">内建</span>' : ''}
    </div>`;
  }).join('');
}

/** 把字段的默认值翻成人类可读（select 要把 value 换成 label） */
function plugDefaultLabel(f) {
  const d = f.default;
  if (d === undefined || d === null || d === '') {
    return f.type === 'text' ? '（空）' : '（无）';
  }
  if (f.type === 'boolean') return d ? '开' : '关';
  if (f.type === 'select') {
    const hit = (f.options || []).find((o) => o.value === d);
    return hit ? (hit.label || hit.value) : String(d);
  }
  return String(d);
}

/** 每个字段的「事实行」：默认值 / 类型 / 范围 / 可选值 */
function plugFieldFacts(f) {
  const facts = [`默认：${plugDefaultLabel(f)}`];
  if (f.type === 'number' && (f.min != null || f.max != null)) {
    facts.push(`范围：${f.min ?? '—'} ~ ${f.max ?? '—'}`);
  }
  if (f.type === 'text' && f.maxLength) {
    facts.push(`最多 ${f.maxLength} 字`);
  }
  if (f.optionsHint) {
    facts.push(`可选：${f.optionsHint}`);
  }
  return facts;
}

function renderPluginFields(p) {
  const box = $('#plugFields');
  const fields = p.configFields || [];
  setText('#plugFieldCount', fields.length ? `${fields.length} 项` : '没有可调参数');

  if (!fields.length) {
    box.innerHTML = `<div class="field full"><small>这个插件没有可调参数 ——
      它的内容是一段固定文本，改坏了比不加更糟，所以只留开关。</small></div>`;
    return;
  }
  box.innerHTML = fields.map((f) => {
    const v = PLUG.draft[f.key];
    let control = '';
    if (f.type === 'boolean') {
      control = `<label class="switch-control" style="margin-top:2px">
        <input type="checkbox" data-field="${esc(f.key)}" ${v ? 'checked' : ''}>
        <span class="switch-track"></span></label>`;
    } else if (f.type === 'select') {
      const opts = (f.options || []).map((o) =>
        `<option value="${esc(o.value)}" ${o.value === v ? 'selected' : ''}>${esc(o.label || o.value)}</option>`);
      control = `<select data-field="${esc(f.key)}">${opts.join('')}</select>`;
    } else if (f.type === 'number') {
      control = `<input type="number" data-field="${esc(f.key)}" value="${esc(v)}"
        ${f.min != null ? `min="${f.min}"` : ''} ${f.max != null ? `max="${f.max}"` : ''}
        ${f.step != null ? `step="${f.step}"` : ''}>`;
    } else if (f.type === 'text') {
      control = `<textarea data-field="${esc(f.key)}" rows="3"
        ${f.maxLength ? `maxlength="${f.maxLength}"` : ''}
        placeholder="留空即用默认">${esc(v || '')}</textarea>`;
    } else {
      control = `<input type="text" data-field="${esc(f.key)}" value="${esc(v || '')}">`;
    }
    return `<div class="field${f.type === 'text' ? ' full' : ''}">
      <label>${esc(f.label)}<span class="plug-field-type">${esc(f.typeLabel || f.type)}</span></label>
      ${control}
      ${f.note ? `<small>${esc(f.note)}</small>` : ''}
      <div class="plug-field-facts">${
        plugFieldFacts(f).map((x) => `<span>${esc(x)}</span>`).join('')}</div>
    </div>`;
  }).join('');
}

function closePlugin() {
  PLUG.current = null;
  PLUG.draft = {};
  $('#plugDetailWrap').hidden = true;
  $('#plugListWrap').hidden = false;
  const head = document.querySelector('.page[data-page="plugins"] .page-heading');
  if (head) head.hidden = false;
  setText('#breadcrumb', PAGE_TITLES.plugins);
}

async function togglePlugin(id, enabled) {
  if (!S.bridge) return;
  try {
    const r = JSON.parse(await S.bridge.set_plugin_enabled(
      JSON.stringify({ id, enabled })));
    if (!r.ok) {
      toast(r.error || '切换失败');
      await loadPlugins();      // 回滚到真实状态，不做「假装成功」
      return;
    }
    PLUG.data = r;
    renderPluginList();
    renderPluginSeats();
    if (PLUG.current === id) openPlugin(id);
    if (typeof syncPluginNav === 'function') syncPluginNav();
    toast(enabled ? '已启用，下一轮对话起生效' : '已停用，下一轮对话起不再注入');
  } catch (e) {
    toast('切换失败：' + (e.message || e));
    await loadPlugins();
  }
}

async function savePluginConfig() {
  const id = PLUG.current;
  if (!id || !S.bridge) return;
  const p = plugById(id);
  if (!p) return;
  const patch = {};
  for (const f of (p.configFields || [])) {
    if (PLUG.draft[f.key] !== undefined) patch[f.key] = PLUG.draft[f.key];
  }
  try {
    const r = JSON.parse(await S.bridge.save_plugin_config(
      JSON.stringify({ id, config: patch })));
    if (!r.ok) { setText('#plugHint', r.error || '保存失败'); return; }
    PLUG.data = r;
    renderPluginList();
    renderPluginSeats();
    openPlugin(id);           // 里面会重新渲染预览
    setText('#plugHint', '已保存');
    markClean('plugins');
    toast('已保存');
  } catch (e) {
    setText('#plugHint', '保存失败：' + (e.message || e));
  }
}

/* 事件委托：列表的开关与进入详情、详情页的字段改动 */

document.addEventListener('click', (e) => {
  const open = e.target.closest('[data-open]');
  if (open) { openPlugin(open.dataset.open); return; }
  if (e.target.closest('#plugBack')) { closePlugin(); return; }
  if (e.target.closest('#plugSave')) { savePluginConfig(); return; }
  if (e.target.closest('#plugPreviewRefresh')) { refreshPluginPreview(); return; }
});

document.addEventListener('change', (e) => {
  const t = e.target;

  const tg = t.closest('[data-toggle]');
  if (tg) { togglePlugin(tg.dataset.toggle, tg.checked); return; }

  if (t.id === 'plugToggle' && PLUG.current) {
    togglePlugin(PLUG.current, t.checked);
    return;
  }

  const f = t.closest('[data-field]');
  if (f && PLUG.current) {
    const key = f.dataset.field;
    PLUG.draft[key] = t.type === 'checkbox' ? t.checked
      : t.type === 'number' ? Number(t.value) : t.value;
    const p = plugById(PLUG.current);
    const changed = p && (p.configFields || []).some(
      (fd) => PLUG.draft[fd.key] !== (p.config || {})[fd.key]);
    $('#plugSave').disabled = !changed;
    setText('#plugHint', '');
  }
});

/* 输入框用 input 事件才能实时启用保存按钮（change 要失焦才触发） */
document.addEventListener('input', (e) => {
  const f = e.target.closest('[data-field]');
  if (!f || !PLUG.current) return;
  if (e.target.type === 'checkbox' || e.target.tagName === 'SELECT') return;
  const key = f.dataset.field;
  PLUG.draft[key] = e.target.type === 'number' ? Number(e.target.value) : e.target.value;
  const p = plugById(PLUG.current);
  const changed = p && (p.configFields || []).some(
    (fd) => PLUG.draft[fd.key] !== (p.config || {})[fd.key]);
  $('#plugSave').disabled = !changed;
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

  if (b.log_message) {
    b.log_message.connect((t, k) => {
      // 信号来的日志没有 seq，用它标记「已通过信号显示」；
      // 轮询补日志时会按 seq 判断，这里不推进 lastLogSeq，
      // 允许重复也不会刷屏（内容相同可接受）。
      pushLog(t, k);
    });
  }
  if (b.chat_event) {
    b.chat_event.connect((who, text, dir) => {
      const box = $('#ovRecent');
      if (!box) return;
      if (box.querySelector('.empty') || !box.querySelector('.recent-list')) {
        box.innerHTML = '<div class="recent-list"></div>';
      }
      const list = box.querySelector('.recent-list');
      list.insertAdjacentHTML('afterbegin', recentItemHtml({ who, text, dir }));
      while (list.children.length > 8) list.removeChild(list.lastChild);
      setText('#ovRecentState', '刚刚有一条新消息');
    });
  }

  tickClock();
  await loadSettings();
  await refreshAccountLabel();
  await refreshStatus();
  await loadToday();

  // 侧边栏要看插件开没开 —— 表情包是插件，关着就不该有那一项。
  // 必须在首屏渲染之前跑完，否则会先闪一下再消失。
  if (typeof syncPluginNav === 'function') await syncPluginNav();

  // 状态轮询：可见时 3 秒，最小化/切走时 15 秒。
  //
  // 以前是一律 2 秒。挂机三小时 = 5400 次跨进程调用，每次都在
  // Chromium 里留一点对象 —— 2026-11-11 那次 0xc0000005 闪退，
  // 跑了 3 小时 48 分，时间对得上。
  // 最小化时谁也不需要看状态，那段压根没必要 2 秒问一次。
  let _statusTimer = null;
  function scheduleStatus() {
    const ms = document.hidden ? 15000 : 3000;
    if (_statusTimer) clearInterval(_statusTimer);
    _statusTimer = setInterval(refreshStatus, ms);
  }
  document.addEventListener('visibilitychange', scheduleStatus);
  scheduleStatus();
  setInterval(loadToday, 60000);
  setInterval(tickClock, 1000);

  // 版本号 + 启动时静默检查一次更新
  try {
    const v = JSON.parse(await b.get_version());
    setText('#aboutVersion', 'v' + (v.version || '?'));
    setText('#brandVersion', 'v' + (v.version || '?'));
  } catch (e) { /* 忽略 */ }
  setTimeout(() => checkUpdate(false), 2500);
}

window.addEventListener('DOMContentLoaded', () => {
  tickClock();
  pushLog('界面已载入');
  bootBridge();
});

/* ════════════════════════════════════════════════════════════════════
   关系卡（印象页里的一栏）
   ════════════════════════════════════════════════════════════════════

   主动开口没有页面了 —— 它是「插件」页里的一张卡（hook 类）：
   开关、参数、预览都是插件页现成的，不必再养一套界面。

   关系卡也没有页面 —— 它并进了「印象」。理由和提示词那边一样：
   「我叫他老爸」和「他是个嘴硬心软的人」都是"我对这个人的认识"，
   分两个地方摆，用户不知道该改哪个。

   每条都必须能对上原话，所以卡片上一定摆出「依据」——
   用户可以自己判断这条是不是编的。

   下线 ≠ 删除：
     下线  她不提了，卡还在（记错了用这个）
     删除  真删（这件事不该存在才用）
*/

function buildBondBlock(p) {
  const bonds = p.bonds || [];
  const head =
    '<div class="card-head" style="margin-top:6px">' +
    '<h2>我们之间</h2>' +
    '<span class="tag">' + (bonds.length ? bonds.length + ' 条' : '还没有') +
    '</span></div>';

  const desc =
    '<div class="card-desc" style="margin-bottom:10px">' +
    '这些是<b>从原话里长出来的</b>：她怎么称呼这个人、有什么当时没说出口。' +
    '每一条都对应某一句真实说过的话，对不上的写不进来。' +
    '她会带着这些跟你说话 —— 措辞不对可以直接改，依据那句原话不会变。' +
    '</div>';

  if (!bonds.length) {
    return head + desc +
      '<div class="empty" style="padding:14px 0"><p>' +
      '还没有。称呼变过几次、有话说了一半、吵完架又和好——' +
      '这些会慢慢长出来，不用催。</p></div>';
  }

  const byKind = {};
  bonds.forEach((it) => { (byKind[it.kind] = byKind[it.kind] || []).push(it); });
  const rows = ['address', 'unsaid'].filter((k) => byKind[k]).map((k) =>
    byKind[k].map((it) => (
      '<div class="open-item">' +
      '<div class="open-body">' +
      '<span class="health-tag">' + esc(it.kindLabel || k) + '</span>' +
      '<span class="open-text">' + esc(it.text) + '</span>' +
      '<small class="memory-role">依据：「' + esc(it.evidence) + '」' +
      '</small>' +
      '</div>' +
      '<button class="btn btn-sm" data-bondedit="' + it.id + '">改</button>' +
      '<button class="btn btn-sm" data-bondretire="' + it.id + '">下线</button>' +
      '<button class="btn btn-sm" data-bonddel="' + it.id + '">删除</button>' +
      '</div>')).join('')).join('');

  return head + desc + rows;
}

function startBondEdit(id, btn) {
  // 就地编辑：把这一行的文字换成输入框，不动其他行。
  // 为什么不做弹窗：改一句话不该跳出去，改完立刻能看到上下文对不对。
  const row = btn.closest('.open-item');
  if (!row) return;
  const body = row.querySelector('.open-body');
  const span = row.querySelector('.open-text');
  if (!body || !span) return;

  const cur = span.textContent;
  body.innerHTML =
    '<input type="text" class="bond-edit-input" value="' + esc(cur) + '" maxlength="200">' +
    '<small class="memory-role">改动只影响她怎么概括，依据那句原话不会变</small>';
  const input = body.querySelector('.bond-edit-input');
  if (!input) return;
  input.focus();
  input.select();

  const done = async (save) => {
    if (save) {
      const text = input.value.trim();
      if (!text) { toast('不能改成空的'); return; }
      try {
        const r = JSON.parse(await S.bridge.bond_update(JSON.stringify(
          { id: id, action: 'edit', text: text })));
        if (r.error) { toast(r.error); return; }
        toast('已改');
      } catch (e) { toast('改失败：' + (e.message || e)); return; }
    }
    await loadImpression();
  };

  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); done(true); }
    if (ev.key === 'Escape') { ev.preventDefault(); done(false); }
  });
  input.addEventListener('blur', () => {
    // 失焦不自动保存 —— 改成什么算什么，容易误存
  });

  // 保存 / 取消两个按钮换到 action 区
  const actions = row.querySelectorAll('button');
  actions.forEach((b) => { b.hidden = true; });
  const box = document.createElement('div');
  box.style.display = 'flex';
  box.style.gap = '6px';
  const ok = document.createElement('button');
  ok.className = 'btn btn-sm';
  ok.textContent = '保存';
  ok.addEventListener('click', () => done(true));
  const no = document.createElement('button');
  no.className = 'btn btn-sm';
  no.textContent = '取消';
  no.addEventListener('click', () => done(false));
  box.appendChild(ok);
  box.appendChild(no);
  row.appendChild(box);
}

async function bondAction(id, action) {
  if (!S.bridge || !S.bridge.bond_update) return;
  if (action === 'delete' && !confirm('真删？删了就找不回来了。\n如果只是觉得她记错了，用「下线」。')) return;
  try {
    const r = JSON.parse(await S.bridge.bond_update(JSON.stringify({ id: id, action: action })));
    if (r.error) { toast(r.error); return; }
    toast(action === 'delete' ? '已删除' : '已下线');
    await loadImpression();
  } catch (e) { toast('操作失败：' + (e.message || e)); }
}

/* ── 事件委托：关系卡按钮是动态渲染的，不用逐个绑定 ── */
document.addEventListener('click', (ev) => {
  const t = ev.target.closest('[data-bondedit],[data-bondretire],[data-bonddel]');
  if (!t) return;
  if (t.dataset.bondedit !== undefined) {
    startBondEdit(Number(t.dataset.bondedit), t);
    return;
  }
  if (t.dataset.bondretire !== undefined) {
    bondAction(Number(t.dataset.bondretire), 'retire');
    return;
  }
  if (t.dataset.bonddel !== undefined) {
    bondAction(Number(t.dataset.bonddel), 'delete');
    return;
  }
});

