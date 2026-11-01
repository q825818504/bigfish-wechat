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
  mood: '心情', memory: '记忆', data: '数据目录',
  settings: '设置', logs: '日志',
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
  if (page === 'memory') { loadMemoryStats(); loadMemory(''); loadMemorySessions(); }
  if (page === 'data') loadDataRoot();
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
      const p = Math.max(0, Math.min(1, Number(dims[k] ?? 0)));
      return `<div class="dim-row">
        <span class="dim-name">${label}</span>
        <span class="dim-track"><span class="dim-fill" style="width:${(p * 100).toFixed(1)}%"></span></span>
        <span class="dim-val">${(p * 100).toFixed(0)}%</span>
      </div>`;
    }).join('') || '<div class="empty"><p>（无数据）</p></div>';

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

/* ────────── 数据目录 ────────── */

async function loadDataRoot() {
  if (!S.bridge) return;
  try {
    const d = JSON.parse(await S.bridge.get_data_root());
    if (d.error) return toast(d.error);
    S.data = d;

    $('#dataRootPath').value = d.root || '';
    setText('#dataRootTag', d.custom ? '自定义' : '默认');
    setText('#dataRootFile', `配置文件：${d.rootFile || '—'}`);
    $('#dataRootTarget').value = '';

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
  toast('已重新盘点');
});

$('#btnWipe').addEventListener('click', async () => {
  if (!S.bridge) return toast('后端未就绪');
  if (!confirm('清空心情和全部记忆？\n\n灵魂（她是谁）会保留。\n删之前会自动备份到数据目录旁边。')) return;
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
  } catch (e) {
    toast('清空失败');
  } finally {
    btn.disabled = false;
  }
});

/* ── 数据根：打开 / 选择 / 只改指向 / 搬家 ── */
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

$('#btnDataRootPreview').addEventListener('click', async () => {
  const target = $('#dataRootTarget').value.trim();
  if (!target) return toast('先填目标目录');
  try {
    const d = JSON.parse(await S.bridge.inspect_move(target));
    const h = $('#dataRootHint');
    if (!d.ok) {
      h.textContent = '检查失败：' + (d.error || '');
      h.className = 'hint-line is-err';
      return;
    }
    const files = d.items.reduce((n, it) => n + (it.files || 0), 0);
    let msg = `要搬 ${d.items.length} 项 / ${files} 个文件 / ${fmtBytes(d.totalBytes)}`;
    if (d.conflicts.length) {
      msg += `　⚠ 目标里已存在同名项 ${d.conflicts.length} 个：${d.conflicts.slice(0, 5).join(', ')}`;
    }
    if (d.dstWritable === false) {
      msg += `　⚠ 目标目录不能写（${d.dstWriteError}）—— 搬过去也用不了`;
    }
    h.textContent = msg;
    h.className = 'hint-line' + ((d.conflicts.length || d.dstWritable === false) ? ' is-err' : ' is-ok');
  } catch (e) { toast('检查失败'); }
});

$('#btnDataRootSave').addEventListener('click', async () => {
  const target = $('#dataRootTarget').value.trim();
  if (!target) return toast('先填目标目录');
  if (!confirm(`把数据根指向 ${target}？\n原有数据不会搬过去 —— 那边没有灵魂记忆就都读不到。\n\n要连同数据一起搬请用「搬过去」。`)) return;
  try {
    const r = JSON.parse(await S.bridge.set_data_root(target));
    const h = $('#dataRootHint');
    if (!r.ok) {
      h.textContent = '失败：' + (r.error || '');
      h.className = 'hint-line is-err';
      return toast('保存失败');
    }
    h.textContent = '数据根已改为 ' + r.root;
    h.className = 'hint-line is-ok';
    toast('已保存');
    S.soul = null;
    await loadDataRoot();
  } catch (e) { toast('保存失败'); }
});

$('#btnDataRootMove').addEventListener('click', async () => {
  const target = $('#dataRootTarget').value.trim();
  if (!target) return toast('先填目标目录');
  try {
    const d = JSON.parse(await S.bridge.inspect_move(target));
    if (!d.ok) return toast(d.error || '检查失败');
    if (d.conflicts.length) {
      if (!confirm(`目标里已有 ${d.conflicts.length} 个同名项，会被源覆盖：\n` +
                   d.conflicts.slice(0, 8).join('\n') + '\n\n继续？')) return;
    }
    if (!confirm(`连同数据一起搬到 ${target}？\n\n源目录会被改名成 .old-时间戳 留着，不直接删。`)) return;

    const h = $('#dataRootHint');
    h.textContent = '正在搬…';
    h.className = 'hint-line';

    const r = JSON.parse(await S.bridge.apply_move(target));
    if (!r.ok) {
      h.textContent = '搬家失败：' + (r.error || '') + '（数据根未改动）';
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
  if (!confirm('数据根恢复成默认位置？原来那个目录不会被删。')) return;
  try {
    const r = JSON.parse(await S.bridge.set_data_root(''));
    if (!r.ok) return toast(r.error || '失败');
    toast('已恢复默认');
    S.soul = null;
    await loadDataRoot();
  } catch (e) { toast('失败'); }
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
