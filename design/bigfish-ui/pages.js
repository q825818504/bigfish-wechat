/* 大肥鱼 · 全功能 UI 设计稿。演示数据全部来自 data.js。 */
(function () {
  const D = window.DEMO;
  const UI = window.DEMO_UI = window.DEMO_UI || { memoryTab: 'facts', settingsTab: 'model', voiceTab: 'policy', dataTab: 'location' };
  const icon = (name, cls = '') => `<svg class="icon ${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const tag = (text, tone = '', cls = '') => `<span class="tag ${tone ? 'tag--' + tone : ''} ${cls}">${esc(text)}</span>`;
  const switchEl = (checked, label = '') => `<label class="switch" title="${esc(label)}"><input type="checkbox" ${checked ? 'checked' : ''} aria-label="${esc(label)}"><span class="switch-track"></span></label>`;
  const avatar = (text, tone = '', cls = '') => `<span class="avatar avatar--${tone} ${cls}">${esc(text)}</span>`;
  const statusIcon = (ok, name = 'check') => `<span class="status-icon">${icon(ok ? 'check' : 'alert')}</span>`;
  const field = (label, control, hint = '', cls = '') => `<div class="field ${cls}"><label>${esc(label)}</label>${control}${hint ? `<small>${hint}</small>` : ''}</div>`;
  const metric = (label, value, unit, note, iconName) => `<div class="metric"><div class="metric-label">${iconName ? icon(iconName) : ''}${esc(label)}</div><div class="metric-value">${esc(value)}${unit ? `<small>${esc(unit)}</small>` : ''}</div><div class="metric-note">${esc(note || '')}</div></div>`;
  const stickerStyle = (cell) => `--sx:${(cell[0] * 100 / 3).toFixed(3)}%;--sy:${(cell[1] * 100 / 2).toFixed(3)}%`;

  function weatherGlyph() {
    return `<svg class="weather-glyph" viewBox="0 0 96 62" aria-hidden="true">
      <circle cx="66" cy="22" r="12" fill="#f2d58c"/>
      <g fill="none" stroke="#f2d58c" stroke-width="2.2" stroke-linecap="round">
        <path d="M66 3v6M66 35v5M83 22h6M44 22h6M79 9l4-4M49 39l-4 4M80 35l4 4M50 9l-4-4"/>
      </g>
      <path d="M26 50h41c7 0 12-5 12-11s-5-11-12-11c-1 0-3 0-4 .3C59.5 20.7 53 16 45 16c-11 0-20 9-20 20v1.5c-5 1.6-8 5-8 9.5 0 5.4 4 9 9 9Z" fill="#f8fbf9" fill-opacity=".18" stroke="#f8fbf9" stroke-width="2.2"/>
      <path d="M30 54h28" stroke="#d2e1dc" stroke-width="1.5" stroke-linecap="round" opacity=".6"/>
    </svg>`;
  }

  function moodGlyph() {
    return `<svg class="mood-glyph" viewBox="0 0 110 74" aria-hidden="true">
      <circle cx="76" cy="28" r="14" fill="#f2d58c"/>
      <g fill="none" stroke="#f2d58c" stroke-width="2.2" stroke-linecap="round">
        <path d="M76 5v7M76 44v7M54 28h7M91 28h8M61 13l5 5M86 38l5 5M91 13l5-5M61 43l-5 5"/>
      </g>
      <path d="M15 56h49c8 0 14-6 14-13s-6-13-14-13c-1.5 0-3 .2-4.4.6C56 22.7 48 17.8 38 17.8c-13.6 0-24.6 10.6-24.6 23.7v2C7 45.6 3 49.7 3 55c0 6.4 5 10.8 12 10.8Z" fill="#f8fbf9" fill-opacity=".13" stroke="#f8fbf9" stroke-width="2.2"/>
    </svg>`;
  }

  function voiceWave() {
    const heights = [5, 12, 7, 22, 10, 28, 15, 31, 8, 18, 34, 13, 24, 9, 19, 6, 15, 26, 11, 22, 7, 14, 5];
    const points = heights.map((h, i) => `M${6 + i * 7} ${36 - h / 2}V${36 + h / 2}`).join(' ');
    return `<svg viewBox="0 0 168 42" preserveAspectRatio="none" aria-hidden="true"><path d="${points}"/></svg>`;
  }

  function pageHead(title, desc, actions = '', meta = '') {
    return `<div class="page-head">
      <div class="page-head-copy"><h1>${esc(title)}</h1><p>${desc}</p>${meta ? `<div class="page-meta">${meta}</div>` : ''}</div>
      <div class="page-actions">${actions}</div>
    </div>`;
  }

  function emptyState(title, text, iconName = 'folder') {
    return `<div class="empty-state"><span class="empty-icon">${icon(iconName)}</span><h3>${esc(title)}</h3><p>${esc(text)}</p></div>`;
  }

  function stickerCard(item, selected = false) {
    return `<button class="sticker-card ${selected ? 'is-selected' : ''}" data-action="sticker-detail" data-sticker="${item.id}" type="button">
      <span class="sticker-thumb" style="${stickerStyle(item.cell)}" aria-hidden="true"></span>
      <span class="sticker-meta"><span class="name">${esc(item.label)}</span><span class="mood">${esc(item.mood)}</span><span class="sticker-strength">${'●'.repeat(Math.min(item.strength, 5))}</span></span>
    </button>`;
  }

  function overview() {
    const { run, life, mood, schedule, environment, chats } = D;
    const recent = [
      { who: '阿澄', initial: '澄', tone: 'teal', time: '09:38', user: '报告终于改完了，脑子已经空了', reply: '那就先别想了。今晚早点睡。', state: '已确认' },
      { who: '阿澄', initial: '澄', tone: 'teal', time: '08:58', user: '早，今天降温了', reply: '嗯，出门记得加件外套。', state: '已确认' }
    ];
    const hero = `<article class="today-hero" aria-labelledby="today-title">
      <div class="hero-main">
        <div class="hero-avatar"><img src="assets/avatar.png" alt="大肥鱼"></div>
        <div class="hero-copy">
          <h2 class="hero-title" id="today-title">早上好，她现在<span>心情不错</span>，<br>正在过自己的今天。</h2>
          <p class="hero-note">${esc(mood.reason)}</p>
          <div class="hero-chips">
            <span class="hero-chip">${icon('mood')}清醒 · ${life.energy} 精力</span>
            <span class="hero-chip">${icon('clock')}07:52 醒来</span>
            <span class="hero-chip">${icon('soul')}关系：亲近</span>
          </div>
        </div>
        <div class="hero-weather">
          ${weatherGlyph()}
          <strong>${life.weather.temp}</strong>
          <span>${esc(life.weather.city)} · ${esc(life.weather.text)}</span>
        </div>
      </div>
      <div class="doing-strip">
        <div class="doing-box">
          <span class="doing-icon">${icon('soul')}</span>
          <div><div class="doing-label">此刻在做</div><div class="doing-text">${esc(life.doing)}</div></div>
          <span class="doing-time">${esc(life.doingNote.split('，')[0])}</span>
        </div>
      </div>
    </article>`;

    const scheduleCard = `<section class="card schedule-card" aria-labelledby="schedule-title">
      <div class="card-title-row"><div><h2 id="schedule-title">今天的安排</h2><div class="card-title-note">醒来时排的 3 件事，至少一件接着昨天。</div></div><button class="text-link" data-action="toast" data-message="日程由她自己排，不可代排">${icon('chevron-right')} 日程插件</button></div>
      <div class="daylight" aria-label="今日光线时间带，现在 09:42">
        <div class="daylight-labels"><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div>
        <div class="daylight-bar">
          <span class="daylight-seg daylight-seg--dawn"></span><span class="daylight-seg daylight-seg--morning"></span><span class="daylight-seg daylight-seg--noon"></span><span class="daylight-seg daylight-seg--evening"></span><span class="daylight-seg daylight-seg--night"></span>
          <span class="daylight-now"><span class="daylight-now-note">现在</span></span>
        </div>
        <div class="daylight-note"><span>晨光</span><span>今天醒得比平时早一点</span><span>夜色</span></div>
      </div>
      <div class="schedule-list">
        ${schedule.map((s) => `<div class="schedule-row ${s.state === 'doing' ? 'is-doing' : ''}"><span class="schedule-time">${s.time}</span><span class="schedule-mark ${s.state === 'doing' ? 'is-doing' : ''}"></span><div class="schedule-main"><div class="schedule-title">${esc(s.title)}</div><div class="schedule-source">${esc(s.source)}</div></div><span class="schedule-state">${esc(s.note)}</span></div>`).join('')}
      </div>
    </section>`;

    const recentCard = `<section class="card recent-card" aria-labelledby="recent-title">
      <div class="card-title-row"><div><h2 id="recent-title">最近发生</h2><div class="card-title-note">每一条都已确认送到微信。</div></div><button class="text-link" data-goto="logs">查看全部 ${icon('arrow-up-right')}</button></div>
      <div class="recent-list">
        ${recent.map((r) => `<div class="recent-item"><div class="recent-meta">${avatar(r.initial, r.tone)}<span class="name">${r.who}</span><span class="time">${r.time}</span></div><div class="recent-bubble"><span class="bubble-sender">${r.who}</span>${esc(r.user)}</div><div class="recent-reply-row"><div class="recent-bubble is-reply"><span class="bubble-sender">她</span>${esc(r.reply)}</div></div></div>`).join('')}
      </div>
      <div class="recent-foot"><div class="status-line"><span class="status-icon">${icon('check-circle')}</span><span class="status-name">发送账本已对齐</span><span class="status-value">最后一条 09:41</span></div></div>
    </section>`;

    const runCard = `<section class="card run-card" aria-labelledby="run-title">
      <div class="run-top"><div><div class="run-state"><span class="live-dot"></span>正在运行</div><h2 class="run-title" id="run-title">她的微信还在线</h2><div class="run-sub">${run.account} · 监听 ${D.watchedChats.length} 个会话</div></div><div class="run-time">已运行<br><strong>${run.uptime}</strong></div></div>
      <div class="run-stats"><div class="run-stat"><strong>${run.received}</strong><span>收到消息</span></div><div class="run-stat"><strong>${run.replied}</strong><span>已回复</span></div><div class="run-stat"><strong>09:41</strong><span>最近发送</span></div></div>
    </section>`;

    const envCard = `<section class="card env-card" aria-labelledby="env-title">
      <div class="card-title-row"><div><h2 id="env-title">运行环境</h2><div class="card-title-note">运行中不代表发送通道就绪，这里分开看。</div></div><span class="badge-state is-ok">发送通道就绪</span></div>
      <div class="status-list">
        ${environment.map((e) => `<div class="status-line ${e.state === 'warn' ? 'is-warn' : ''}">${statusIcon(e.state !== 'warn', e.state === 'warn' ? 'alert' : 'check')}<span class="status-name">${e.label}</span><span class="status-value">${e.value}</span></div>`).join('')}
      </div>
    </section>`;

    const insight = `<section class="card insight-card" aria-labelledby="notice-title">
      <div class="card-title-row"><div><h2 id="notice-title">她注意到的</h2><div class="card-title-note">后台缓存的感知，不联网即席编造。</div></div>${icon('sun')}</div>
      <div class="stack">${life.noticing.map((n) => `<div class="status-line"><span class="status-icon">${icon('check')}</span><span class="status-name">${esc(n)}</span></div>`).join('')}</div>
      <div class="insight-foot">${icon('file')}日记 11-08 · ${esc(life.journal.excerpt.slice(0, 22))}…<span class="spacer"></span><button class="text-link" data-action="toast" data-message="示例稿不打开本地目录">打开 journal</button></div>
    </section>`;

    return `<section class="page" data-page="overview">
      ${pageHead('她的今天', '不是等你说句话才存在的角色。先看看她此刻怎样，再决定要不要改什么。', `<button class="btn btn-secondary" data-goto="chats">${icon('chats')}查看会话</button><button class="btn btn-primary" data-action="toast" data-message="已刷新运行状态（示例）">${icon('refresh')}刷新状态</button>`, `<span class="badge-state is-ok">运行中</span><span>·</span><span>发送通道就绪</span><span>·</span><span>模型 DeepSeek</span>`)}
      <div class="overview-layout">
        <div class="overview-main">${hero}<div class="overview-lower">${scheduleCard}${recentCard}</div></div>
        <aside class="overview-side">${runCard}${envCard}${insight}</aside>
      </div>
      <div class="today-details">
        <div class="mini-panel"><div class="mini-head">${icon('memory')}今天她记住的</div><div class="mini-main">“报告终于改完了” · 他的语气明显松下来。</div><div class="mini-foot"><span class="tag tag--leaf">新认识</span><span>来源：09:38 的原话 · 私密</span></div></div>
        <div class="mini-panel mini-panel--dark"><div class="mini-head">${icon('file')}只属于她的日记</div><div class="mini-main">${esc(life.journal.excerpt)}</div><div class="mini-foot">11-08 23:06 · 不进入提示词 · <button class="text-link" style="color:#dfc77e" data-action="toast" data-message="打开 journal 目录（示例）">打开</button></div></div>
        <div class="mini-panel mini-panel--warm"><div class="mini-head">${icon('calendar')}明天想接着做</div><div class="mini-main">把没看完的那本书收个尾，剩下的时间留白。</div><div class="mini-foot">来自今天的日程 · 她醒来后再排</div></div>
      </div>
      <div class="page-foot-note">${icon('shield')}界面示例 · 会话、记忆与数值均为合成数据；真实数据只在本机。</div>
    </section>`;
  }

  function chats() {
    const chatCard = `<section class="card card--flush chat-card">
      <div class="chat-toolbar"><div class="segmented" data-segmented="chat-group"><button class="is-active" data-chat-filter="all">全部</button><button data-chat-filter="contacts">联系人</button><button data-chat-filter="groups">群聊</button><button data-chat-filter="official">公众号</button></div><span class="meta">6 个会话 · 2 个监听</span><button class="btn btn-sm btn-ghost" data-action="toast" data-message="正在读取会话（示例）">${icon('refresh')}刷新</button></div>
      <div class="chat-list">
        ${D.chats.map((c, i) => `<div class="chat-row ${i === 0 ? 'is-active' : ''}" data-chat-filter-row="${c.group}" data-action="select-chat" data-chat="${esc(c.name)}"><span class="avatar avatar--${c.color}">${esc(c.initial)}</span><div class="chat-main"><div class="chat-name">${esc(c.name)} ${c.kind === 'group' ? tag('群聊', 'outline') : c.kind === 'official' ? tag('公众号') : tag('私聊')}</div><div class="chat-preview">${esc(c.preview)}</div></div><span class="chat-time">${c.time}${c.unread ? `<span class="unread">${c.unread}</span>` : ''}</span><span class="chat-scope">${esc(c.scope)}</span><span class="chat-actions">${switchEl(c.watching, c.name + ' 监听')}<button class="icon-btn" data-action="scope" data-chat="${esc(c.name)}" title="会话配置">${icon('settings')}</button></span></div>`).join('')}
      </div>
      <div class="chat-footer"><div class="watch-note">${icon('check-circle')}<span><strong>${D.watchedChats.length} 个会话</strong>正在监听；改动后由右上角「保存监听」提交</span></div><span class="tag tag--leaf">已保存</span></div>
    </section>`;

    return `<section class="page" data-page="chats">
      ${pageHead('会话', '只有被勾选的会话才会自动回复。齿轮打开三层作用域：全局 → 微信账号 → 这个会话。', `<button class="btn btn-secondary" data-action="account">${icon('impression')}选择账号</button><button class="btn btn-secondary" data-action="toast" data-message="已读取 6 个会话（示例）">${icon('refresh')}刷新会话</button><button class="btn btn-primary" data-action="toast" data-message="监听列表已保存（示例）">${icon('check')}保存监听</button>`, `<span class="badge-state is-ok">示例账号在线</span><span>·</span><span>${D.chats.length} 个会话</span>`)}
      <div class="chat-layout">
        <div class="chat-main-col">${chatCard}<div class="chat-bottom-strip"><div class="mini-panel"><div class="mini-head">${icon('check-circle')}监听策略</div><div class="mini-main">2 个会话</div><div class="mini-sub">私聊每条都回；群聊只回被叫到的消息</div></div><div class="mini-panel"><div class="mini-head">${icon('clock')}安静时段</div><div class="mini-main">0–6 点</div><div class="mini-sub">固定不开放配置：半夜不主动打扰</div></div><div class="mini-panel"><div class="mini-head">${icon('shield')}发送账本</div><div class="mini-main">sent / failed / unknown</div><div class="mini-sub">unknown 不自动重发；避免对方收到两遍</div></div></div></div>
        <aside class="side-stack">
          <section class="card account-card">
            <div class="card-title-row"><div><h2>当前账号</h2><div class="card-title-note">多开时只列出正在运行的微信。</div></div><button class="text-link" data-action="account">切换 ${icon('chevron-right')}</button></div>
            <div class="account-main">${avatar('示', 'teal', 'avatar--lg')}<div><strong>示例账号</strong><span>wxid_demo · 微信 4.1.15.53</span></div></div>
            <div class="account-stats"><div class="account-stat"><strong>2</strong><span>监听中</span></div><div class="account-stat"><strong>6</strong><span>可读会话</span></div><div class="account-stat"><strong>3</strong><span>未读</span></div></div>
          </section>
          <section class="card scope-card">
            <div class="card-title-row"><div><h2>这个会话的规则</h2><div class="card-title-note">没设置的项跟随上一层。</div></div>${tag('阿澄', 'teal')}</div>
            <div class="scope-chain">${D.scope.chain.map((x, i) => i ? icon('chevron-right') + `<span class="chain-node">${esc(x)}</span>` : `<span class="chain-node">${esc(x)}</span>`).join('')}</div>
            <div class="scope-mini-list">${D.scope.fields.map((f) => `<div class="scope-mini"><span class="name">${esc(f.label)}</span><span class="value">${f.state === 'override' ? esc(f.value) : '跟随'}</span><span class="source">${f.state === 'override' ? '本会话' : '继承'}</span></div>`).join('')}</div>
            <div class="form-actions" style="margin-top:12px"><button class="btn btn-sm btn-secondary" data-action="scope" data-chat="阿澄">${icon('sliders')}编辑作用域</button><span class="hint-inline">${icon('info')}未保存不生效</span></div>
          </section>
          <section class="card notice-card">
            <div class="card-title-row"><div><h2>监听不等于无条件回复</h2><div class="card-title-note">群聊默认只在被 @ 或叫名字时回。</div></div>${icon('shield')}</div>
            <div class="note">${icon('info')}「处理自己发的消息」只对文件传输助手生效，是自测通道，不会在私聊里把她的回复当新消息读回去。</div>
          </section>
        </aside>
      </div>
      <div class="page-foot-note">${icon('info')}示例数据 · 真实账号、昵称和未读数不会离开本机。</div>
    </section>`;
  }

  function soul() {
    return `<section class="page" data-page="soul">
      ${pageHead('灵魂', '她是谁的唯一来源。写在这里的话，下一轮对话就会带着它开口。', `<span class="badge-state is-idle">revision ${D.soul.revision}</span><button class="btn btn-secondary" data-action="toast" data-message="恢复默认需要确认（示例）">恢复默认</button><button class="btn btn-primary" data-action="save-soul">${icon('check')}保存人格</button>`, `<span>人格文件 · soul.json</span><span>·</span><span>最近保存 今天 09:28</span>`)}
      <div class="soul-layout">
        <div class="soul-main">
          <section class="card">
            <div class="card-head"><h2>身份</h2><span class="card-head-note">这些词会被她自然地说出口</span></div>
            <div class="identity-grid">
              ${field('名字', `<input class="input" value="${esc(D.soul.name)}">`, '她自己叫什么。')}
              ${field('称呼对方', `<input class="input" value="${esc(D.soul.address)}">`, '她怎么叫你。')}
              ${field('昵称 / 别名', `<input class="input" value="${esc(D.soul.aliases)}">`, '逗号分隔。')}
            </div>
          </section>
          <section class="card persona-card">
            <div class="card-head"><div><h2>我是谁</h2><div class="card-title-note">写成她自己的口吻，不是说明文档。</div></div><span class="tag tag--warm">人格主体</span></div>
            <textarea class="textarea" aria-label="我是谁">${esc(D.soul.whoIAm)}</textarea>
            <div class="persona-meta"><span>拖动或直接编辑；保存后下一轮生效。</span><span class="numeric">${D.soul.whoIAm.length} / 6000 字</span></div>
          </section>
          <section class="card">
            <div class="card-head"><h2>我珍视的</h2><span class="card-head-note">锁定的条目先解锁才能删除</span></div>
            <div class="values-wrap">${D.soul.values.map((v) => `<span class="value-chip ${v.locked ? 'is-locked' : ''}">${v.locked ? icon('lock') : icon('lock-open')}${esc(v.text)}</span>`).join('')}<button class="btn btn-sm btn-ghost" data-action="toast" data-message="添加一条珍视条目（示例）">${icon('plus')}添加</button></div>
          </section>
          <section class="card">
            <div class="card-head"><h2>我的性格</h2><span class="card-head-note">数值不会原样进提示词，每一档都翻成一句话。</span></div>
            <div class="trait-list">${D.soul.traits.map((t) => `<div class="trait-row"><span class="trait-name">${esc(t.label)}</span><input class="trait-slider" type="range" min="0" max="100" value="${t.value}" aria-label="${esc(t.label)}"><span class="trait-desc">${esc(t.desc)}</span></div>`).join('')}</div>
          </section>
        </div>
        <aside class="soul-side">
          <section class="card">
            <div class="card-head"><h2>她自己知道的事</h2><button class="text-link" data-action="toast" data-message="添加一条行为边界（示例）">添加 ${icon('plus')}</button></div>
            <div class="notes-list">${D.soul.notes.map((n) => `<div class="note-item ${n.auto ? 'is-auto' : ''}"><span class="note-date">${esc(n.date)}</span><div class="note-text">${esc(n.text)}</div>${n.locked ? `<span class="tag tag--sun">手写</span>` : `<button class="icon-btn" data-action="toast" data-message="删除自动条目（示例）" title="删除">${icon('trash')}</button>`}</div>`).join('')}</div>
            <div class="note" style="margin-top:11px">${icon('info')}自动复盘只追加，手写条目永远不删；上限 12 条。</div>
          </section>
          <section class="card injection-card">
            <div class="injection-head"><div><div class="card-title">她会收到的</div><div class="card-sub">system prompt · 按预算裁剪后的分段</div></div>${tag('注入预览', 'sun')}</div>
            <div style="margin-top:13px">${D.soul.injection.map((s) => `<div class="injection-section"><span class="injection-name">${esc(s.title)}</span><span class="injection-bar"><span style="width:${s.ratio}%"></span></span><span class="injection-chars">${s.chars} 字</span></div>`).join('')}</div>
            <div class="injection-footer"><span>总计 ${D.soul.injection.reduce((a, b) => a + b.chars, 0)} 字</span><button class="text-link" style="color:#e3cc8c" data-action="preview-soul">查看原文 ${icon('arrow-up-right')}</button></div>
          </section>
          <section class="card">
            <div class="card-head"><h2>她最近的变化</h2><span class="card-head-note">都能追到由头</span></div>
            <div class="timeline">${D.soul.selfHistory.map((h) => `<div class="timeline-item"><span class="timeline-dot"></span><div class="timeline-content"><div class="top"><strong>${esc(h.date)} · 复盘</strong><span class="date">有来源</span></div><p>${esc(h.from)} → ${esc(h.to)}</p></div></div>`).join('')}</div>
          </section>
        </aside>
      </div>
    </section>`;
  }

  function mood() {
    const score = D.mood.score;
    return `<section class="page" data-page="mood">
      ${pageHead('心情', '不是装饰分数。它会改变这一轮最多说几条、写多长、能不能不接话。', `<span class="badge-state is-ok">09:38 刚更新</span><button class="btn btn-secondary" data-action="reset-mood">${icon('refresh')}重置到基线</button><button class="btn btn-primary" data-action="save-mood">${icon('check')}保存参数</button>`, `<span>七维情绪</span><span>·</span><span>表达预算已生效</span>`)}
      <div class="mood-layout">
        <div class="mood-main">
          <section class="mood-hero" aria-labelledby="mood-score-title">
            <div class="mood-hero-copy">
              <div class="mood-score-row"><span class="mood-score">${score}</span><span class="mood-of">/ 100</span></div>
              <div class="mood-label" id="mood-score-title">${esc(D.mood.label)} · ${esc(D.mood.shortLabel)}</div>
              <p class="mood-reason">${esc(D.mood.reason)}</p>
              <div class="mood-updated">${esc(D.mood.updated)} · 基线 ${D.mood.baseline}</div>
            </div>
            <div class="mood-score-side">${moodGlyph()}<div class="mood-glyph-note">晴间多云<br>余温还在</div></div>
          </section>
          <section class="card">
            <div class="card-head"><h2>情绪维度</h2><span class="card-head-note">0–100 · 同一标尺</span></div>
            <div class="mood-dims">${D.mood.dims.map((d) => `<div class="mood-dim"><span class="mood-dim-label"><i class="dot"></i>${esc(d.label)}</span><span class="progress progress--${d.color}"><span style="width:${d.value}%"></span></span><span class="mood-dim-value">${d.value}</span></div>`).join('')}</div>
            <div class="mood-trend" aria-hidden="true"><svg viewBox="0 0 420 46" preserveAspectRatio="none"><line x1="0" y1="34" x2="420" y2="34"/><path d="M8 32 C58 31, 82 28, 120 28 S190 23, 250 22 S330 18, 412 17"/><text x="8" y="44">08:10</text><text x="360" y="44">现在</text></svg></div>
          </section>
          <section class="card mood-prompt-card"><div class="card-head"><h2>这句话会进入下一轮</h2><span class="card-head-note">不报数值，只说感受与由头</span></div><div class="mini-quote">“${esc(D.mood.reason)}”</div><div class="mood-prompt-meta"><span>${icon('info')} event 模式 · 底子 + 发生了什么</span><span>约 39 字 · 今天第 2 次注入</span></div></section>
        </div>
        <aside class="mood-side">
          <section class="card budget-card">
            <div class="card-head"><h2>这一轮的预算</h2><span class="tag tag--teal">生效中</span></div>
            <p class="budget-intro">心情 ${score}、精力 ${D.life.energy}、现在是上午。她还有余力说话，但比心情很好时收一点。</p>
            <div class="budget-grid"><div class="budget-cell"><strong>${D.mood.budget.maxParts} 条</strong><span>最多分条</span></div><div class="budget-cell"><strong>${D.mood.budget.maxChars} 字</strong><span>整轮字数</span></div><div class="budget-cell"><strong>${D.mood.budget.canSkip}</strong><span>可以不接话</span></div><div class="budget-cell"><strong>${D.mood.budget.forceAfter} 次</strong><span>不接后强制回</span></div></div>
            <div class="hint-inline" style="margin-top:9px">${icon('info')}${esc(D.mood.budget.note)}</div>
          </section>
          <section class="card afterglow-card">
            <div class="afterglow-type">${icon('sun')}余韵 · ${esc(D.mood.afterglow.type)}<span class="spacer"></span><span class="faint">${esc(D.mood.afterglow.expires)}</span></div>
            <p class="afterglow-text">${esc(D.mood.afterglow.text)}</p>
            <div class="afterglow-foot"><span>来自刚才那一轮</span><button class="text-link" data-action="toast" data-message="余韵会在到期后自己淡掉">查看规则</button></div>
          </section>
          <section class="card">
            <div class="card-head"><h2>判断参数</h2><button class="text-link" data-action="mood-settings">全部参数 ${icon('arrow-up-right')}</button></div>
            <div class="form-grid">
              ${field('心情基线', `<input class="input" value="70">`)}
              ${field('平复半衰期', `<select class="select"><option>12 小时</option><option>24 小时</option></select>`)}
              ${field('每天注入上限', `<input class="input" value="6 次">`)}
              ${field('告诉她的方式', `<select class="select"><option>event · 说底子 + 由头</option><option>label · 只说心情</option></select>`)}
            </div>
            <div style="margin-top:11px">${`<label class="switch"><input type="checkbox" checked><span class="switch-track"></span></label>`}<span style="font-size:10px;color:var(--muted);margin-left:7px">每轮对话后自动判断心情</span></div>
            <div class="param-foot"><span class="hint-inline">${icon('info')}关掉后只按半衰期回落</span><button class="btn btn-sm btn-secondary" data-action="save-mood">保存</button></div>
          </section>
        </aside>
      </div>
      <div class="page-foot-note">${icon('info')}示例数据 · 七维分数与表达预算都由本机真实状态生成，不会写进她的聊天正文。</div>
    </section>`;
  }

  const memoryTabs = [
    ['facts', '她认定的事', 37],
    ['raw', '原文记录', '1,842'],
    ['identity', '身份与主人', ''],
    ['maintenance', '维护', '']
  ];

  function memoryTabsHtml() {
    return `<div class="tab-bar">${memoryTabs.map(([id, label, count]) => `<button class="tab ${UI.memoryTab === id ? 'is-active' : ''}" data-memory-tab="${id}">${label}${count ? `<span class="count">${count}</span>` : ''}</button>`).join('')}</div>`;
  }

  function factRow(f, active) {
    const stateTag = f.status === 'active' ? tag('有效', 'leaf') : f.status === 'superseded' ? tag('已取代', 'sun') : tag('已忘掉');
    return `<div class="memory-row ${active ? 'is-active' : ''}" data-action="memory-fact" data-fact="${f.id}"><span class="memory-row-mark">${icon(f.top ? 'memory' : 'check')}</span><div class="memory-row-main"><div class="memory-row-title">${esc(f.title)} ${stateTag}</div><div class="memory-row-sub">${esc(f.object)}</div><div class="memory-row-meta"><span>${esc(f.predicate)}</span><span>·</span><span>${esc(f.owner)}</span><span>·</span><span>${f.visibility === 'private' ? '私密' : '可共享'}</span></div></div><span class="memory-row-time">${esc(f.time)}</span></div>`;
  }

  function factDetail(f) {
    return `<section class="card fact-detail">
      <div class="fact-detail-head"><span class="fact-icon">${icon('memory')}</span><div class="fact-detail-title"><h2>${esc(f.title)}</h2><div class="meta">${esc(f.predicate)} · ${esc(f.time)} · 置信度 ${f.confidence}%</div></div></div>
      <p class="fact-detail-body">${esc(f.object)}</p>
      <div class="fact-source"><strong>来源 · 可以核对</strong>${esc(f.source)}</div>
      <div class="detail-list" style="margin-top:11px">
        <div class="detail-item"><span class="k">归属</span><span class="v">${esc(f.owner)}</span></div>
        <div class="detail-item"><span class="k">可见性</span><span class="v">${f.visibility === 'private' ? '私密 · 不外传' : `可共享 · ${esc(f.topic)}`}</span></div>
        <div class="detail-item"><span class="k">状态</span><span class="v">${f.status === 'active' ? '现在有效' : f.status === 'superseded' ? '已被新认识取代' : '已忘掉 · 原话仍在'}</span></div>
        <div class="detail-item"><span class="k">使用</span><span class="v">在提示词里出现 ${f.use} 次</span></div>
        ${f.supersededBy ? `<div class="detail-item"><span class="k">后来改成</span><span class="v">${esc(f.supersededBy)}</span></div>` : ''}
      </div>
      <div class="detail-actions"><button class="btn btn-sm btn-secondary" data-action="toast" data-message="纠正会新增一条，不删除旧认识（示例）">${icon('edit')}纠正</button><button class="btn btn-sm btn-ghost" data-action="toast" data-message="忘掉后仍保留原话与历史（示例）">${icon('eye')}忘掉</button><button class="btn btn-sm btn-ghost" data-action="toast" data-message="已在原会话中定位（示例）">看原话</button></div>
    </section>`;
  }

  function rawPane() {
    const rows = [
      { role: '对方', text: '报告终于改完了，脑子已经空了', time: '今天 09:38', session: '阿澄' },
      { role: '她', text: '那就先别想了。今晚早点睡。', time: '今天 09:41', session: '阿澄' },
      { role: '对方', text: '早，今天降温了', time: '今天 08:58', session: '阿澄' },
      { role: '她', text: '嗯，出门记得加件外套。', time: '今天 08:59', session: '阿澄' },
      { role: '对方', text: '周六还是老地方？', time: '今天 09:21', session: '周末饭搭子' },
      { role: '对方', text: '这个版本我先看一遍', time: '昨天 22:14', session: '设计组 · 临时' }
    ];
    return `<div class="memory-list-card">
      <div class="memory-list-head">${memoryTabsHtml()}</div>
      <div class="memory-toolbar"><div class="memory-search">${icon('search')}<input class="input" placeholder="按内容过滤原文…"></div><select class="select" style="width:104px;height:33px;font-size:10px"><option>全部角色</option><option>对方说的</option><option>她说的</option></select><button class="btn btn-sm btn-secondary" data-action="toast" data-message="已刷新原文（示例）">刷新</button></div>
      <div class="memory-rows">${rows.map((r) => `<div class="memory-row"><span class="memory-row-mark">${icon(r.role === '她' ? 'soul' : 'chats')}</span><div class="memory-row-main"><div class="memory-row-title">${r.role} · ${esc(r.session)}</div><div class="memory-row-sub">${esc(r.text)}</div><div class="memory-row-meta"><span>${r.role === '她' ? '她的回复' : '对方原话'}</span><span>·</span><span>可核对</span></div></div><span class="memory-row-time">${esc(r.time)}</span></div>`).join('')}</div>
      <div class="memory-footer"><span>显示 1–6 / 共 1,842 条</span><span class="row"><button class="btn btn-sm btn-ghost">上一页</button><button class="btn btn-sm btn-ghost">下一页</button></span></div>
    </div>`;
  }

  function identityPane() {
    return `<div class="memory-list-card"><div class="memory-list-head">${memoryTabsHtml()}</div><div style="padding:16px 18px">
      <div class="card-title-row"><div><h2>主人与共享</h2><div class="card-title-note">主人能看全部记录；其他人只看被标成可共享的基础信息。</div></div>${tag('主人 · 阿澄', 'teal')}</div>
      <div class="note">${icon('shield')}私密内容不会因为开了群聊就进入群池。共享还要同时命中话题白名单。</div>
      <div class="identity-grid" style="margin-top:14px">${field('主人', `<select class="select"><option>阿澄 · 示例私聊</option></select>`, '绑定时立即生效。')}${field('补一个平台身份', `<select class="select"><option>微信</option><option>B 站</option><option>抖音</option><option>手机号</option></select>`, '这里只是新建，不会自动合并。')}${field('平台 ID', `<input class="input" placeholder="UID / 手机号">`, '用占位符，不读取真实账号。')}</div>
      <div class="card-head" style="margin-top:20px"><h2>身份组</h2><span class="card-head-note">手动认领，跨平台汇总记忆</span></div>
      <div class="status-line"><span class="avatar avatar--teal">澄</span><span class="status-name">阿澄 · 微信 wxid_demo</span><span class="tag tag--teal">主人</span><span class="status-value">33 条认识</span></div>
      <div class="status-line"><span class="avatar avatar--rain">B</span><span class="status-name">B 站 UID 000000</span><span class="tag">未挂接</span><span class="status-value">待认领</span></div>
      <div class="note" style="margin-top:12px">${icon('info')}挂接后只汇总新记忆，既有事实不搬家；摘下账号也不会删除任何记忆。</div>
    </div></div>`;
  }

  function maintenancePane() {
    return `<div class="memory-list-card"><div class="memory-list-head">${memoryTabsHtml()}</div><div style="padding:16px 18px">
      <div class="card-title-row"><div><h2>维护</h2><div class="card-title-note">独立 sqlite 备份；导入前先自动备份当前库。</div></div>${tag('索引一致', 'leaf')}</div>
      <div class="status-list"><div class="status-line">${statusIcon(true)}<span class="status-name">检索索引</span><span class="status-value">一致 · 孤儿 0</span></div><div class="status-line">${statusIcon(true)}<span class="status-name">数据库大小</span><span class="status-value">12.8 MB</span></div><div class="status-line">${statusIcon(true)}<span class="status-name">最近体检</span><span class="status-value">11-07 21:16</span></div></div>
      <div class="maintenance-actions" style="margin-top:14px"><button class="btn btn-sm btn-secondary" data-action="toast" data-message="导出独立 sqlite（示例）">${icon('download')}导出备份</button><button class="btn btn-sm btn-secondary" data-action="toast" data-message="导入前会先备份现有库（示例）">${icon('upload')}从备份导入</button><button class="btn btn-sm btn-secondary" data-action="toast" data-message="压缩会短暂独占记忆库（示例）">压缩回收空间</button><button class="btn btn-sm btn-ghost" data-action="toast" data-message="正在重建检索索引（示例）">${icon('refresh')}修索引</button></div>
      <div class="note" style="margin-top:14px">${icon('info')}删原文 ≠ 忘事实。原文删了就找不回来；忘掉事实只让结论失效，历史和原话仍在。</div>
    </div></div>`;
  }

  function memory() {
    const { stats, facts } = D.memory;
    const side = UI.memoryTab === 'facts' ? factDetail(facts[0]) : UI.memoryTab === 'raw' ? `<section class="card"><div class="card-head"><h2>原文详情</h2>${tag('可核对', 'leaf')}</div><div class="empty-state"><span class="empty-icon">${icon('chats')}</span><h3>点一条原文</h3><p>查看角色、来源会话与时间。原文删除后不可恢复。</p></div></section>` : UI.memoryTab === 'identity' ? `<section class="card"><div class="card-head"><h2>记忆池隔离</h2>${tag('只给数量', 'sun')}</div><div class="pool-mini">${avatar('饭', 'amber')}<span class="name">周末饭搭子</span><span class="count">18 条</span></div><div class="pool-mini">${avatar('设', 'rain')}<span class="name">设计组 · 临时</span><span class="count">9 条</span></div><div class="note" style="margin-top:12px">${icon('eye')}别人的私事不会在你没主动点开时铺在屏幕上。目前只显示池数量。</div></section>` : `<section class="card"><div class="card-head"><h2>备份状态</h2>${tag('自动', 'leaf')}</div><div class="detail-list"><div class="detail-item"><span class="k">最近备份</span><span class="v">11-08 23:10</span></div><div class="detail-item"><span class="k">保留策略</span><span class="v">导入前自动 .bak</span></div><div class="detail-item"><span class="k">原文</span><span class="v">1,842 条</span></div><div class="detail-item"><span class="k">事实</span><span class="v">37 条</span></div></div></section>`;
    return `<section class="page" data-page="memory">
      ${pageHead('记忆', '她听过什么、认定什么、怎么纠正。原文删除与事实忘掉是两件事。', `<button class="btn btn-secondary" data-action="toast" data-message="导出独立 sqlite（示例）">${icon('download')}导出备份</button><button class="btn btn-primary" data-action="toast" data-message="开始整理最近的原话（示例）">${icon('memory')}现在整理</button>`, `<span>${stats.facts} 条认识</span><span>·</span><span>${stats.messages} 条原文</span><span>·</span><span>索引一致</span>`)}
      <div class="memory-stats">${metric('原文消息', stats.messages, '条', '在库', 'chats')}${metric('检索片段', stats.chunks, '个', '供检索', 'search')}${metric('库大小', stats.size.replace(' MB', ''), 'MB', '本地 sqlite', 'data')}${metric('有效认识', stats.facts, '条', '事实层', 'memory')}${metric('已取代 / 忘掉', `${stats.superseded} / ${stats.forgotten}`, '', '历史仍可查', 'refresh')}</div>
      <div class="memory-layout">
        <div class="memory-main">
          ${UI.memoryTab === 'facts' ? `<div class="memory-list-card"><div class="memory-list-head">${memoryTabsHtml()}</div><div class="memory-toolbar"><div class="memory-search">${icon('search')}<input class="input" placeholder="比如：旧书店、周三、报告…"></div><select class="select" style="width:102px;height:33px;font-size:10px"><option>全部状态</option><option>现在有效</option><option>已被取代</option><option>已忘掉</option></select><button class="btn btn-sm btn-secondary" data-action="toast" data-message="检索到 3 条相关认识（示例）">${icon('search')}检索</button></div><div class="memory-rows">${facts.map((f, i) => factRow(f, i === 0)).join('')}</div><div class="memory-footer"><span>显示 1–${facts.length} / 共 ${stats.facts} 条</span><span class="row"><button class="btn btn-sm btn-ghost">上一页</button><button class="btn btn-sm btn-ghost">下一页</button></span></div></div>` : UI.memoryTab === 'raw' ? rawPane() : UI.memoryTab === 'identity' ? identityPane() : maintenancePane()}
        </div>
        <aside class="memory-side">
          ${side}
          <section class="card maintenance-card"><div class="card-head"><h2>会话记忆</h2><span class="card-head-note">按会话删除</span></div>${D.memory.sessions.map((s) => `<div class="pool-mini">${avatar(s.name.slice(0, 1), s.kind === '群聊' ? 'amber' : 'teal')}<span class="name">${esc(s.name)}<span class="faint" style="font-size:8px; margin-left:4px">${esc(s.kind)}</span></span><span class="count">${s.count} 条</span></div>`).join('')}</section>
        </aside>
      </div>
      <div class="page-foot-note">${icon('shield')}示例数据 · 可见性双闸门：条目可共享 + 话题在白名单，才可能说给别人听。</div>
    </section>`;
  }

  function impression() {
    const p = D.impression.active;
    return `<section class="page" data-page="impression">
      ${pageHead('印象', '她怎么看这个人，以及她准备对别人说什么。画像、评语和证据型关系卡是三套不同的东西。', `<button class="btn btn-secondary" data-action="toast" data-message="画像已按最新 33 条认识重新归纳（示例）">${icon('refresh')}重新归纳</button><button class="btn btn-primary" data-action="toast" data-message="评语只存本地，模型不会看到（示例）">${icon('check')}保存评语</button>`, `<span>2 个人</span><span>·</span><span>主人固定第一</span>`)}
      <div class="impression-layout">
        <aside class="card people-card" style="grid-row: span 2">
          <div class="people-head"><strong>人物</strong><span class="faint">2 人</span></div>
          <div class="people-list">${D.impression.people.map((person, i) => `<button class="person-tab ${i === 0 ? 'is-active' : ''}" data-action="toast" data-message="切换到 ${esc(person.name)} 的印象（示例）">${avatar(person.initial, person.color)}<span class="person-tab-copy"><strong>${esc(person.name)}</strong><span>${person.facts} 条认识 · ${person.updated}</span></span><span class="stage">${esc(person.stage)}</span></button>`).join('')}</div>
          <div class="people-empty">一人一页。关系阶段是描述，不是好感度，也不解锁任何权限。</div>
        </aside>
        <div class="impression-main">
          <section class="card">
            <div class="person-hero">${avatar(p.initial, 'teal', 'avatar--xl')}<div class="person-hero-main"><h2>${esc(p.name)}</h2><div class="sub">${tag('主人', 'teal')}${tag(p.stage, 'leaf')}<span>${p.facts} 条认识 · ${p.privateFacts} 条私密</span></div><div class="sub">你们的关系从 09-14 开始有画像 · 今天 09:38 有更新</div></div><div class="portrait-actions"><button class="btn btn-sm btn-secondary" data-action="toast" data-message="画像已停用，事实与原文都还在（示例）">${icon('minus')}停用画像</button></div></div>
            <div class="portrait-box"><p>${esc(p.portrait)}</p><div class="foot"><span>画像由 33 条认识归纳，每轮都会带进提示词</span><span>09-14 更新</span></div></div>
            <div class="comment-box"><div class="label">你怎么看这段关系 <span class="hint-inline">${icon('eye')}她看不到 · 模型不参与</span></div><textarea class="textarea">${esc(p.comment)}</textarea><div class="row" style="justify-content:flex-end; margin-top:6px"><span class="hint-inline" style="margin-right:auto">${icon('check')}已保存 · 不会进提示词</span><button class="btn btn-sm btn-primary" data-action="toast" data-message="评语已保存（示例）">保存评语</button></div></div>
            <div class="comment-box" style="margin-top:12px"><div class="label">她对别人会说的 <span class="tag tag--sun">需要双闸门</span></div><div class="open-info-list" style="margin-top:5px">${p.openInfo.map((x) => `<div class="open-info">${icon('eye')}<div class="info-main"><div class="info-text">${esc(x.text)}</div><div class="info-meta"><span>${esc(x.topic)} · ${esc(x.predicate)}</span><span>·</span><span>${x.visibility === 'private' ? '仅主人可见' : '可共享'}</span></div></div><button class="btn btn-sm btn-ghost" data-action="toast" data-message="撤回开放信息，事实与原文仍保留（示例）">撤回</button></div>`).join('')}</div></div>
          </section>
        </div>
        <aside class="impression-side">
          <section class="card">
            <div class="card-head"><h2>你们之间</h2><span class="card-head-note">带原话依据</span></div>
            <div class="bond-list">${p.bonds.map((b) => `<div class="bond-item"><div class="bond-label">${icon('soul')}${esc(b.label)}${tag(b.kind === 'address' ? '称呼' : '没说出口', b.kind === 'address' ? 'teal' : 'rose')}</div><div class="bond-text">${esc(b.text)}</div><div class="bond-evidence">依据：${esc(b.evidence)}</div></div>`).join('')}</div>
          </section>
          <section class="card">
            <div class="card-head"><h2>关系时间线</h2><span class="card-head-note">事件不需要原话证据</span></div>
            <div class="timeline">${p.events.map((e) => `<div class="timeline-item"><span class="timeline-dot is-${e.kind}"></span><div class="timeline-content"><div class="top"><strong>${esc(e.label)}</strong><span class="date">${esc(e.date)}</span></div><p>${esc(e.text)}</p></div></div>`).join('')}</div>
          </section>
        </aside>
        <section class="card privacy-band"><div class="mini-panel"><div class="mini-head">${icon('eye')}她会说出去</div><div class="mini-main">爱好、作息、性格、口头禅、称呼；还必须同时在开放话题白名单里。</div></div><div class="mini-panel"><div class="mini-head">${icon('lock')}她不会说出去</div><div class="mini-main">住址、工作、行程、身体、金钱、家人、联系人、年龄、情感和近况。</div></div><div class="mini-panel"><div class="mini-head">${icon('shield')}关系卡依据</div><div class="mini-main">每条措辞都必须能在原话里找到依据；“没说出口的话”也只是她的猜测，不是事实。</div></div></section>
      </div>
      <div class="page-foot-note">${icon('shield')}示例数据 · 头像、昵称、原话均为合成内容。</div>
    </section>`;
  }

  function stickers() {
    const st = D.stickers;
    return `<section class="page" data-page="stickers">
      ${pageHead('表情包', '她手里有什么，就能发什么。开关与频率在插件页，这一页只管她的收藏。', `<button class="btn btn-secondary" data-action="toast" data-message="打开表情目录（示例）">${icon('folder')}打开目录</button><button class="btn btn-primary" data-action="toast" data-message="导入图片会自动转成 GIF（示例）">${icon('plus')}添加表情</button>`, `<span>${st.total} 张收藏</span><span>·</span><span>一页 12 张</span>`)}
      <div class="sticker-layout">
        <div class="sticker-main">
          <section class="card card--flush">
            <div class="chat-toolbar sticker-toolbar"><div class="segmented"><button class="is-active">全部</button><button>常用</button><button>情绪</button><button>最近导入</button></div><span class="meta">${st.total} 张 · 去重完成</span><button class="btn btn-sm btn-ghost" data-action="toast" data-message="正在检查重复和缩略图（示例）">${icon('refresh')}刷新</button></div>
            <div class="sticker-grid-wrap"><div class="sticker-grid">${st.items.map((s, i) => stickerCard(s, i === 0)).join('')}</div><div class="sticker-pager"><span>显示 1–12 / 共 ${st.total} 张</span><div class="row"><button class="btn btn-sm btn-ghost" disabled>‹ 上一页</button><button class="btn btn-sm btn-secondary" data-action="toast" data-message="下一页（示例）">下一页 ›</button></div></div></div>
          </section>
          <section class="card notice-card"><div class="status-line"><span class="status-icon">${icon('info')}</span><span class="status-name">图片会统一转成 GIF，微信才会认成原生表情；重复的会被跳过，不会覆盖。</span><button class="text-link" data-goto="plugins">去插件页设置频率 ${icon('chevron-right')}</button></div></section>
        </div>
        <aside class="sticker-side">
          <section class="card sticker-detail-card">
            <div class="card-head"><h2>选中的一张</h2><span class="card-head-note">双击可打开完整说明</span></div>
            <div class="sticker-preview" style="${stickerStyle(st.items[0].cell)}" role="img" aria-label="示例表情：${st.items[0].label}"></div>
            <div class="sticker-detail-title"><strong>${st.items[0].label}</strong><span class="sticker-strength">${'●'.repeat(st.items[0].strength)}${'○'.repeat(5 - st.items[0].strength)}</span></div>
            <div class="sticker-tags">${tag(st.items[0].mood, 'rose')}${tag(`强度 ${st.items[0].strength} / 5`, 'sun')}${tag('日常', 'outline')}</div>
            <div class="sticker-config"><div class="stat-line"><span class="k">文件</span><span class="v">demo_001.gif · 128 KB</span></div><div class="stat-line"><span class="k">情绪 / 强度</span><span class="v">${esc(st.items[0].mood)} · ${st.items[0].strength}</span></div><div class="stat-line"><span class="k">最近发出</span><span class="v">11-08 22:14</span></div></div>
            <div class="form-actions"><button class="btn btn-sm btn-secondary" data-action="toast" data-message="发到文件传输助手（示例）">试发一张</button><button class="btn btn-sm btn-ghost" data-action="toast" data-message="编辑备注与强度（示例）">编辑说明</button></div>
          </section>
          <section class="card">
            <div class="card-head"><h2>发送状态</h2>${tag(st.enabled ? '已启用' : '未启用', st.enabled ? 'leaf' : 'sun')}</div>
            <div class="plugin-status"><span class="icon-wrap">${icon('sliders')}</span><div class="copy"><strong>表情开关在插件页</strong><span>频率、冷却、挑图方式都在那里</span></div><button class="text-link" data-goto="plugins">前往 ${icon('chevron-right')}</button></div>
            <div class="sticker-config"><div class="stat-line"><span class="k">每一轮考虑配图</span><span class="v">15%</span></div><div class="stat-line"><span class="k">同会话冷却</span><span class="v">3 分钟</span></div><div class="stat-line"><span class="k">怎么挑</span><span class="v">${esc(st.mode)}</span></div></div>
            <div class="note" style="margin-top:12px">${icon('info')}停用只影响发送，收藏不会丢。</div>
          </section>
        </aside>
      </div>
      <div class="page-foot-note">${icon('stickers')}表情缩略图为合成示例；真实收藏只存本机。</div>
    </section>`;
  }

  function voiceHero() {
    return `<section class="voice-hero"><h2>她今天会用自己的声音，<br>但不会把同一句话念两遍。</h2><p>命中语音后只发语音；合成失败、超过 50 秒或气泡准备失败时才回到文字。</p><div class="voice-wave">${voiceWave()}</div><div class="voice-hero-note">${esc(D.voice.voiceName)}<br>${esc(D.voice.voiceId)}</div></section>`;
  }

  function voicePolicyPane() {
    const v = D.voice;
    const policies = [
      ['off', '关闭', '只发文字'],
      ['request', '按需', '你明确要求时'],
      ['auto', '自动判断', '场景 + 概率'],
      ['probability', '按概率', '固定命中率'],
      ['always', '每条都发', '只用于测试'],
      ['preview', '预演', '只记日志，不出声']
    ];
    return `<div class="voice-layout"><div class="voice-main">${voiceHero()}
      <section class="card"><div class="card-head"><h2>什么时候出声</h2><span class="card-head-note">当前：自动判断 · 只在自然场景</span></div><div class="voice-policy-grid">${policies.map(([id, name, desc]) => `<button class="policy-card ${v.strategy === id ? 'is-active' : ''}" data-action="set-voice-policy" data-policy="${id}"><span class="policy-name">${name}${icon('check', 'policy-check')}</span><span class="policy-desc">${desc}</span></button>`).join('')}</div>
      <div class="form-grid" style="margin-top:14px">${field('自动判断的命中率', `<input class="input" value="${v.probability}">`, '0.20 ≈ 五次里一次；模型明确写标记时不受它限制。')}${field('同一会话冷却', `<input class="input" value="${v.cooldown}">`, '控制连着发语音的密度。')}${field('每天上限', `<input class="input" value="${v.dailyLimit}">`, '0 = 不限；这是积分消耗的最后一道闸。')}${field('连续语音上限', `<input class="input" value="${v.consecutive}">`, '超过后强制换文字。')}</div>
      <div class="field" style="margin-top:12px"><label>场景 <span class="hint-inline">未勾选的一律发文字</span></label><div class="voice-scenes">${['亲密 / 撒娇','想念 / 安慰','低落 / 受伤','开心 / 好消息','日常 / 道歉','主动','深夜 / 刚醒','拌嘴 / 冲突'].map((s) => `<span class="scene-chip ${v.scenes.some((x) => x.startsWith(s.split(' / ')[0])) ? 'is-on' : ''}">${s}</span>`).join('')}</div></div>
      <div class="param-foot"><span class="hint-inline">${icon('info')}命中后不会再发同内容文字，避免对方收到两遍。</span><button class="btn btn-sm btn-primary" data-action="save-voice">保存策略</button></div></section></div>
      <aside class="voice-side"><section class="card"><div class="card-head"><h2>发送形式</h2>${tag(v.sendAs === 'file' ? '音频文件' : '语音气泡', 'rain')}</div><div class="delivery-row"><span class="state-icon">${icon('file')}</span><div class="state-copy"><strong>音频文件</strong><span>最稳，不需要虚拟声卡；先跑通这条</span></div><span class="badge-state is-ok">当前</span></div><div class="delivery-row"><span class="state-icon">${icon('voice')}</span><div class="state-copy"><strong>微信语音气泡</strong><span>实验链路；需要 VB-CABLE，录音期间独占微信</span></div><span class="badge-state is-warn">未选</span></div><div class="note note--warn" style="margin-top:10px">${icon('alert')}语音气泡未配置，不影响文字回复。装好声卡后再切换。</div><div class="form-actions"><button class="btn btn-sm btn-secondary" data-action="toast" data-message="打开声音设置（示例）">配置声卡</button><button class="btn btn-sm btn-ghost" data-action="toast" data-message="测试连接只查模型列表，不合成、不扣积分（示例）">测试声音</button></div></section>
      <section class="card"><div class="card-head"><h2>失败时怎么收尾</h2><span class="card-head-note">每一步都想清楚</span></div><div class="form-ladder"><div class="ladder-step"><span class="num">1</span><span>合成失败 / 超过 50 秒 → 只发文字。</span></div><div class="ladder-step"><span class="num">2</span><span>气泡还没按录音键 → 退发同一份 WAV，不重复合成。</span></div><div class="ladder-step is-danger"><span class="num">3</span><span>已按过录音键 → 结果未知，不补文字、不补文件。</span></div></div><div class="note" style="margin-top:10px">${icon('shield')}发送未知不会自动重试，避免对方收到两遍。</div></section></aside></div>`;
  }

  function voiceConnectionPane() {
    const v = D.voice;
    return `<div class="voice-layout"><div class="voice-main"><section class="card"><div class="card-head"><h2>模型接入</h2>${tag(v.ready ? '已配置' : '待配置', v.ready ? 'leaf' : 'sun')}</div><div class="provider-banner"><span class="provider-mark">B</span><div class="provider-copy"><strong>${esc(v.model)}</strong><span>固定音色身份，语速锁定 1.00；情绪靠本地导演层。</span></div>${tag('Breeze', 'rain')}</div><div class="form-grid" style="margin-top:14px"><div class="field full"><label>接口地址</label><input class="input" value="https://api.breeze.blue"></div><div class="field full"><label>API Key <a href="#" onclick="return false">去哪拿？</a></label><input class="input" value="brz_••••••••••••••••" type="password"></div>${field('模型', `<select class="select"><option>${esc(v.model)}</option></select>`)}${field('音色 ID', `<input class="input" value="${esc(v.voiceId)}">`, '音色是身份，别每轮换。')}${field('语言', `<input class="input" value="zh">`)}${field('音色基线', `<select class="select"><option>${esc(v.profile)}</option></select>`)}</div><div class="form-actions"><button class="btn btn-sm btn-secondary" data-action="toast" data-message="声音模型连接正常 · 未合成音频（示例）">${icon('play')}测试连接</button><span class="hint-inline">只查模型列表，不消耗积分</span></div></section>
    <section class="card"><div class="card-head"><h2>表达参数</h2><span class="card-head-note">固定值不会被随意改动</span></div><div class="form-grid">${field('语速', `<input class="input" value="1.00" readonly>`)}${field('Guidance', `<input class="input" value="${v.guidance}">`)}${field('最长一轮', `<input class="input" value="${v.maxChars} 字">`)}${field('超时', `<input class="input" value="${v.timeout}">`)}</div><div class="param-foot"><span class="hint-inline">${icon('file')}缓存 ${v.cacheUsed}</span><button class="btn btn-sm btn-ghost" data-action="toast" data-message="清空声音缓存（示例）">清空缓存</button></div></section></div><aside class="voice-side"><section class="card"><div class="card-head"><h2>语音气泡准备</h2>${tag('已安装', 'leaf')}</div><div class="voice-device"><span class="device-icon">${icon('headphones')}</span><div class="device-main"><strong>VB-CABLE 已检测到</strong><span>${esc(v.driver.input)} → ${esc(v.driver.output)}</span></div>${icon('check-circle')}</div><div class="detail-list" style="margin-top:10px"><div class="detail-item"><span class="k">播放端</span><span class="v">CABLE Input</span></div><div class="detail-item"><span class="k">微信麦克风</span><span class="v">CABLE Output</span></div><div class="detail-item"><span class="k">录音快捷键</span><span class="v">${esc(v.hotkey)}</span></div></div><div class="form-actions"><button class="btn btn-sm btn-secondary" data-action="toast" data-message="刷新声卡（示例）">刷新声卡</button><button class="btn btn-sm btn-ghost" data-action="toast" data-message="打开 Windows 声音设置（示例）">打开系统设置</button></div></section><section class="card note-card"><div class="note note--warn">${icon('alert')}气泡链路已跑通，但仍依赖微信版本、控件名、快捷键和声卡状态。语音页会明确显示准备失败发生在哪一步。</div></section></aside></div>`;
  }

  function voiceDeliveryPane() {
    return `<div class="voice-layout"><div class="voice-main"><section class="card"><div class="card-head"><h2>发送与回退</h2><span class="card-head-note">命中语音后只发语音，不发同内容文字。</span></div><div class="form-grid">${field('发送形式', `<select class="select"><option>音频文件（稳，推荐先跑）</option><option>微信语音气泡（需 VB-CABLE）</option></select>`)}${field('虚拟声卡播放端', `<select class="select"><option>CABLE Input</option></select>`)}${field('微信录音快捷键', `<input class="input" value="${D.voice.hotkey}">`)}${field('语音最长字数', `<input class="input" value="220 字 · 合成后超过 50 秒改文字">`)}</div><div class="card-head" style="margin-top:18px"><h2>内容过滤</h2><span class="card-head-note">念出来没有意义的内容回文字</span></div><div class="voice-scenes">${['含链接 → 文字','含代码 → 文字','表格 → 文字','清单 → 文字','允许 [[voice]] / [[text]]','语音失败补文字'].map((x) => `<span class="scene-chip is-on">${x}</span>`).join('')}</div><div class="card-head" style="margin-top:18px"><h2>范围</h2></div><div class="voice-scenes">${['群聊也发语音（默认关）','只对主人发'].map((x) => `<span class="scene-chip">${x}</span>`).join('')}</div><div class="param-foot"><span class="hint-inline">${icon('info')}主动消息目前仍不走语音。</span><button class="btn btn-sm btn-primary" data-action="save-voice">保存设置</button></div></section></div><aside class="voice-side"><section class="card"><div class="card-head"><h2>最近一次</h2>${tag('只发文字', 'leaf')}</div><div class="delivery-row"><span class="state-icon">${icon('mic-off')}</span><div class="state-copy"><strong>未命中语音场景</strong><span>这一轮只发文字 · 09:38</span></div></div><div class="note" style="margin-top:12px">${icon('voice')}预演模式只记日志，不会真的合成；确认策略前可以先用它看几天。</div></section></aside></div>`;
  }

  function voice() {
    const tab = UI.voiceTab;
    const content = tab === 'connection' ? voiceConnectionPane() : tab === 'delivery' ? voiceDeliveryPane() : voicePolicyPane();
    return `<section class="page" data-page="voice">
      ${pageHead('声音', '她自己的声音。接入、什么时候出声、失败怎么收尾，都在这一页。', `<span class="badge-state is-ok">${D.voice.ready ? '模型已配置' : '待配置'}</span><button class="btn btn-secondary" data-action="toast" data-message="声音连接正常（示例）">${icon('play')}测试声音</button><button class="btn btn-primary" data-action="save-voice">${icon('check')}保存声音</button>`, `<span>Breeze TTS 2</span><span>·</span><span>voice_only</span><span>·</span><span>${D.voice.voiceId}</span>`)}
      <div class="tab-bar"><button class="tab ${tab === 'policy' ? 'is-active' : ''}" data-voice-tab="policy">什么时候出声</button><button class="tab ${tab === 'connection' ? 'is-active' : ''}" data-voice-tab="connection">接入与音色</button><button class="tab ${tab === 'delivery' ? 'is-active' : ''}" data-voice-tab="delivery">发送与回退</button></div>
      ${content}
      <div class="page-foot-note">${icon('info')}示例数据 · 声音只在本机合成与落盘，Key 与真实音色 ID 不进入设计稿。</div>
    </section>`;
  }

  function dataLocationPane() {
    const d = D.data;
    return `<div class="data-layout"><div class="data-main"><section class="card"><div class="card-head"><h2>所在位置</h2>${tag(d.writable ? '可写' : '不可写', d.writable ? 'leaf' : 'danger')}</div><div class="path-box"><span class="folder-icon">${icon('folder')}</span><div class="path-copy"><strong>${esc(d.root)}</strong><span>${esc(d.writableNote)} · 默认目录可改</span></div><button class="icon-btn" data-action="toast" data-message="打开数据目录（示例）" title="打开数据目录" aria-label="打开数据目录">${icon('arrow-up-right')}</button></div><div class="form-grid" style="margin-top:14px"><div class="field full"><label>换成别的目录</label><div class="input-group"><input class="input" value="D:\\大肥鱼\\data" aria-label="目标目录"><button class="btn btn-sm btn-secondary" data-action="toast" data-message="浏览文件夹（示例）">浏览…</button></div><small>只改指向不会搬文件；要连数据一起过去，用「搬过去」。</small></div></div><div class="form-actions"><button class="btn btn-sm btn-primary" data-action="toast" data-message="先盘点再搬，失败不改指向（示例）">${icon('folder')}搬过去（连同数据）</button><button class="btn btn-sm btn-secondary" data-action="toast" data-message="只改指向（示例）">只改指向</button><button class="btn btn-sm btn-ghost" data-action="toast" data-message="恢复默认目录（示例）">恢复默认</button></div></section>
    <section class="card"><div class="card-head"><h2>这里就是她</h2><span class="card-head-note">一个数据目录 = 一份灵魂 + 心情 + 记忆</span></div><div class="data-files">${d.files.map((f) => `<div class="data-file"><span class="data-file-icon">${icon(f.icon)}</span><div class="data-file-main"><strong>${esc(f.name)}</strong><span>${esc(f.label)}</span></div><span class="size">${esc(f.size)}</span><span class="state">${tag('正常', 'leaf')}</span></div>`).join('')}</div><div class="note" style="margin-top:12px">${icon('shield')}换机器或备份：直接拷贝整个 data/，不要只拷 memory.sqlite3。</div></section></div><aside class="data-side"><section class="card disk-card"><div class="disk-top"><div><span>数据目录占用</span><strong>${d.size}</strong></div><span>128.4 MB<br>含备份</span></div><div class="disk-bar"><span style="width:35%;background:var(--teal)"></span><span style="width:27%;background:var(--rain)"></span><span style="width:18%;background:var(--sun)"></span><span style="width:12%;background:var(--rose)"></span></div><div class="disk-legend"><span><i style="background:var(--teal)"></i>记忆</span><span><i style="background:var(--rain)"></i>日记 / 表情</span><span><i style="background:var(--sun)"></i>备份</span><span><i style="background:var(--rose)"></i>其他</span></div><div class="stat-line" style="margin-top:10px"><span class="k">备份状态</span><span class="v">${esc(d.backup)}</span></div></section><section class="card danger-zone"><div class="card-head"><h2>清空</h2>${tag('保留灵魂', 'sun')}</div><p class="danger-copy">她还在，只是忘了。删除心情和全部记忆，会先自动备份。</p><div class="danger-actions"><button class="btn btn-sm btn-ghost" data-action="toast" data-message="盘点：42 个文件 / 12.9 MB（示例）">看看会删什么</button><button class="btn btn-sm btn-danger" data-action="wipe-confirm">清空心情与记忆</button></div></section><section class="card whale-card"><div class="card-head"><h2>鲸落</h2>${tag('不可逆', 'danger')}</div><p class="whale-copy">她不存在了。灵魂、心情、记忆、参数全部删掉，目录回到从没来过她的状态。</p><div class="whale-files">${esc(d.whale.sample)}</div><button class="btn btn-sm btn-danger" data-action="whale-confirm">${icon('alert')}进行鲸落</button></section></aside></div>`;
  }

  function dataDangerPane() {
    const d = D.data;
    return `<div class="data-danger-layout">
      <section class="card danger-preflight"><div class="card-head"><h2>动手之前</h2><span class="card-head-note">已确认备份与可写性</span></div><div class="preflight-grid"><div class="status-line">${statusIcon(true)}<span class="status-name">当前目录</span><span class="status-value">${esc(d.root)}</span></div><div class="status-line">${statusIcon(true)}<span class="status-name">自动备份</span><span class="status-value">11-08 23:10</span></div><div class="status-line">${statusIcon(true)}<span class="status-name">可写性</span><span class="status-value">刚刚校验</span></div></div><div class="note" style="margin-top:10px">${icon('info')}删除前会强制备份到数据目录旁边。先确认当前目录，再选择下面两个不同层级的操作。</div></section>
      <div class="danger-pair">
        <section class="card danger-zone"><div class="card-head"><h2>清空心情与记忆</h2>${tag('可恢复', 'sun')}</div><p class="danger-copy">保留灵魂、人格与珍视条目；删除心情、余韵、全部记忆和印象。删之前自动备份。</p><div class="wipe-summary"><div class="wipe-col"><strong>会保留</strong><ul>${d.wipe.keep.map((x) => `<li>${esc(x)}</li>`).join('')}</ul></div><div class="wipe-col"><strong>会删除 · ${d.wipe.files} 个文件 · ${d.wipe.size}</strong><ul>${d.wipe.remove.map((x) => `<li>${esc(x)}</li>`).join('')}</ul></div></div><div class="danger-actions"><button class="btn btn-sm btn-ghost" data-action="toast" data-message="已重新盘点（示例）">重新盘点</button><button class="btn btn-sm btn-danger" data-action="wipe-confirm">清空心情与记忆</button></div></section>
        <section class="card whale-card"><div class="card-head"><h2>鲸落</h2>${tag('不可逆', 'danger')}</div><p class="whale-copy">删除灵魂、心情、记忆、参数与收藏。她不存在了，目录回到从没来过她的状态。</p><div class="whale-files">${esc(d.whale.sample)}</div><div class="stat-line whale-count"><span class="k">将删除</span><span class="v">${d.whale.files} 个文件 · ${d.whale.size}</span></div><div class="note whale-warning">${icon('alert')}需要输入「鲸落」两个字，再进行最后一次确认。</div><button class="btn btn-sm btn-danger" data-action="whale-confirm">${icon('alert')}进行鲸落</button></section>
      </div>
      <section class="card after-card"><div class="card-head"><h2>执行之后</h2><span class="card-head-note">两种操作留下的东西不同</span></div><div class="after-grid"><div><strong>清空 · 她还在</strong><span>灵魂、人格与珍视条目保留；心情、余韵、记忆和印象清空。还会继续过日子。</span></div><div><strong>鲸落 · 她不存在了</strong><span>程序回到从未有她的目录；只留下执行前强制生成的一份备份。</span></div><div><strong>日记 · 本地 Markdown</strong><span>不进入提示词。清空会保留，鲸落会连同 journal/ 一起删除。</span></div></div></section>
    </div>`;
  }

  function data() {
    const tab = UI.dataTab;
    return `<section class="page" data-page="data">
      ${pageHead('数据目录', '一个数据目录就是一份她。换目录、搬家、清空和鲸落，后果必须一眼看懂。', `<span class="badge-state is-ok">可写</span><button class="btn btn-secondary" data-action="toast" data-message="打开数据目录（示例）">${icon('folder')}打开目录</button><button class="btn btn-primary" data-action="toast" data-message="已创建新备份（示例）">${icon('download')}立即备份</button>`, `<span>D:\\大肥鱼\\data</span><span>·</span><span>128.4 MB</span>`)}
      <div class="tab-bar"><button class="tab ${tab === 'location' ? 'is-active' : ''}" data-data-tab="location">位置与备份</button><button class="tab ${tab === 'danger' ? 'is-active' : ''}" data-data-tab="danger">清空与鲸落</button></div>
      ${tab === 'danger' ? dataDangerPane() : dataLocationPane()}
      <div class="page-foot-note">${icon('shield')}示例路径 · 不会访问真实数据目录。</div>
    </section>`;
  }

  function plugins() {
    const pl = D.plugins;
    const detail = pl.detail;
    return `<section class="page" data-page="plugins">
      ${pageHead('插件', '她由哪些部分拼起来。关掉只停止影响，不删除它的数据；开关只有你能改。', `<span class="badge-state is-ok">${pl.enabled} / ${pl.installed} 已启用</span><button class="btn btn-secondary" data-action="toast" data-message="已重新读取插件清单（示例）">${icon('refresh')}刷新</button>`, `<span>身份序</span><span>·</span><span>下一轮生效</span>`)}
      <div class="plugins-layout">
        <div class="plugins-left">
        <section class="card card--flush plugin-list-card"><div class="plugin-list-head"><h2 class="section-title">已安装</h2><span class="plugin-count">${pl.enabled} 已启用 / ${pl.installed} 个</span><span class="spacer"></span><span class="hint-inline">${icon('info')}开关失败会回滚</span></div><div class="plugin-rows">${pl.items.map((p, i) => `<div class="plugin-row ${i === 0 ? 'is-active' : ''}" data-action="select-plugin" data-plugin="${p.id}"><span class="plugin-icon plugin-icon--${p.accent}">${icon(p.icon)}</span><div class="plugin-main"><div class="plugin-name">${esc(p.name)} ${p.order ? `<span class="plugin-seat">${p.order}</span>` : ''}</div><div class="plugin-desc">${esc(p.desc)}</div></div><span class="plugin-scope">${esc(p.scope)} · ${esc(p.fields)}</span>${switchEl(p.enabled, p.name + ' 启用')}</div>`).join('')}</div></section>
        <section class="card seat-rail-card"><div class="card-head"><h2>身份序</h2><span class="card-head-note">数字越靠后，语气分量越高；插件只能填空位。</span></div><div class="seat-rail">${pl.seats.map((s) => `<div class="seat-node ${s.builtin ? 'is-builtin' : ''} ${s.order === 870 ? 'is-current' : ''}"><span class="seat-order">${s.order}</span><strong>${esc(s.name)}</strong><span>${esc(s.note)}</span></div>`).join('')}</div></section>
        </div>
        <aside class="plugins-side"><section class="card detail-hero"><div class="detail-title-row"><span class="plugin-icon plugin-icon--sun" style="width:37px;height:37px;border-radius:10px">${icon(detail.icon)}</span><div class="detail-title-copy"><h2>${esc(detail.name)}</h2><p>${esc(detail.desc)}</p><div class="detail-meta">${tag(`座位 ${detail.order}`, 'teal')}${tag(detail.scope, 'outline')}${tag('已启用', 'leaf')}</div></div>${switchEl(true, '生活插件')}</div><div class="detail-points"><div><strong>会影响什么</strong><ul>${detail.affects.map((x) => `<li>${esc(x)}</li>`).join('')}</ul></div><div><strong>不会做什么</strong><ul>${detail.limits.map((x) => `<li>${esc(x)}</li>`).join('')}</ul></div></div></section>
          <section class="card"><div class="card-head"><h2>她实际收到的</h2><span class="card-head-note">拼在 ${detail.order}</span></div><div class="inject-preview-label"><span>system · 生活段</span><span>实时渲染</span></div><div class="inject-preview">${esc(detail.preview)}</div><div class="param-foot"><span class="hint-inline">${icon('info')}用当前参数与时间渲染</span><button class="btn btn-sm btn-ghost" data-action="toast" data-message="已重新渲染预览（示例）">重新渲染</button></div></section>
          <section class="card"><div class="card-head"><h2>可自定义的配置</h2><span class="card-head-note">${detail.config.length} 项</span></div><div class="plugin-config">${detail.config.map((c) => `<div class="detail-item"><span class="k">${esc(c.label)}<span>${esc(c.type)}</span></span><span class="v">${esc(c.value)}</span></div>`).join('')}</div><button class="btn btn-sm btn-primary" style="margin-top:10px" data-action="save-plugin">保存配置</button></section>
        </aside>
      </div>
      <div class="page-foot-note">${icon('shield')}示例数据 · 插件清单与座位来自真实产品结构；所有参数均为合成值。</div>
    </section>`;
  }

  function settingsModelPane() {
    const s = D.settings;
    return `<div class="settings-layout"><div class="settings-main"><section class="card"><div class="card-head"><h2>模型</h2>${tag('已连接', 'leaf')}</div><div class="provider-banner"><span class="provider-mark">D</span><div class="provider-copy"><strong>${esc(s.provider)}</strong><span>${esc(s.providerNote)}</span></div><button class="text-link" data-action="toast" data-message="切换渠道（示例）">更换 ${icon('chevron-down')}</button></div><div class="form-grid" style="margin-top:14px"><div class="field full"><label>API Key <a href="#" onclick="return false">去哪拿？</a></label><input class="input" type="password" value="${esc(s.apiKey)}"><small>Key 只存本机 config.json。</small></div><div class="field full"><label>接口地址</label><input class="input" value="${esc(s.baseUrl)}"><small>选预设时自动填好；中转站、自建服务改这里。</small></div><div class="field full"><label>模型</label><div class="input-group"><select class="select"><option>${esc(s.model)}</option><option>deepseek-chat</option><option>deepseek-reasoner</option></select><button class="btn btn-sm btn-secondary" data-action="toast" data-message="已获取 3 个真实模型（示例）">${icon('refresh')}获取模型</button></div><small>从这家 /models 拉真实列表，不写死建议值。</small></div>${field('协议', `<select class="select"><option>${esc(s.protocol)}</option></select>`)}${field('温度', `<input class="input" value="${esc(s.temperature)}">`)}${field('最大 token', `<input class="input" value="${esc(s.maxTokens)}">`)}</div><div class="form-actions"><button class="btn btn-sm btn-secondary" data-action="toast" data-message="连接正常 · 1.8 秒（示例）">${icon('play')}测试连接</button><span class="hint-inline">真实发一次请求验证，不重试 4xx</span></div></section>
    <section class="card pipeline-card" style="margin-top:18px"><div class="card-head"><h2>一次回复怎样发生</h2><span class="card-head-note">每一段都能在快照和日志里核对</span></div><div class="pipeline-steps"><div class="pipeline-step"><span class="pipeline-icon">${icon('chats')}</span><strong>收到</strong><span>攒批，确认他说完了</span><span class="pipeline-time">6–30 秒</span></div><span class="pipeline-arrow">${icon('chevron-right')}</span><div class="pipeline-step"><span class="pipeline-icon">${icon('memory')}</span><strong>拼装</strong><span>灵魂 / 记忆 / 生活 / 心情</span><span class="pipeline-time">本地即时</span></div><span class="pipeline-arrow">${icon('chevron-right')}</span><div class="pipeline-step"><span class="pipeline-icon">${icon('send')}</span><strong>发送</strong><span>分条、停顿、三态回执</span><span class="pipeline-time">3.2 秒</span></div></div></section>
    </div>
    <aside class="settings-side"><section class="card"><div class="card-head"><h2>模型健康</h2>${tag(s.health.state === 'ok' ? '正常' : '异常', s.health.state === 'ok' ? 'leaf' : 'danger')}</div><div class="model-health">${icon('check-circle')}${esc(s.health.text)}</div><div class="detail-list" style="margin-top:10px"><div class="detail-item"><span class="k">最近错误</span><span class="v">无</span></div><div class="detail-item"><span class="k">请求超时</span><span class="v">90 秒</span></div><div class="detail-item"><span class="k">流式</span><span class="v">未启用</span></div></div></section><section class="card snapshot-card"><div class="snapshot-title"><div><strong>她这轮看到了什么</strong><div class="hint-inline">${icon('file')}最近 20 份 · 312 KB</div></div>${tag('已开启', 'leaf')}</div><select class="select snapshot-select"><option>${esc(s.trace.selected)}</option><option>11-09 08:58 · 阿澄 · 3.6 秒</option></select><div class="snapshot-sections">${s.trace.sections.map((x) => `<div class="snapshot-section"><span class="snapshot-section-label">${esc(x.name)}</span><span class="snapshot-section-bar"><span style="width:${x.ratio}%;background:var(--${x.color})"></span></span><span class="snapshot-section-chars">${x.chars} 字</span></div>`).join('')}</div><div class="snapshot-foot">${icon('folder')}保留上限 200 份 · 目录可写 · 旧格式兼容</div><div class="snapshot-actions"><button class="btn btn-sm btn-secondary" data-action="snapshot">查看快照</button><button class="btn btn-sm btn-ghost" data-action="toast" data-message="打开快照目录（示例）">打开目录</button></div></section><section class="card"><div class="card-head"><h2>版本与更新</h2><span class="card-head-note">${esc(s.update.current)}</span></div><div class="update-line"><div><strong>当前版本</strong><span>${esc(s.update.current)}</span></div><span class="spacer"></span>${tag('最新', 'leaf')}</div><div class="update-line"><div><strong>检查更新</strong><span>从 GitHub Releases 读取</span></div><span class="spacer"></span><button class="btn btn-sm btn-ghost" data-action="toast" data-message="已是最新版本（示例）">检查</button></div><div class="update-line"><div><strong>自动更新</strong><span>端到端验证仍有限制，不直接替用户重装</span></div></div></section></aside></div>`;
  }

  function settingsReplyPane() {
    const s = D.settings;
    return `<div class="settings-layout"><div class="settings-main"><section class="card"><div class="card-head"><h2>回复策略</h2><span class="card-head-note">这些参数直接改变她什么时候开口、说多长。</span></div><div class="form-grid">${field('轮询间隔', `<input class="input" value="${s.reply.interval}">`, '越小越灵敏，越费 CPU。')}${field('回复长度上限', `<input class="input" value="${s.reply.maxChars}">`, '微信聊天宜短，300 字足够。')}${field('安静时段', `<input class="input" value="${s.reply.quiet}">`, '留空表示不启用；逗号分隔的整点。')}${field('处理自己发的消息', `<select class="select"><option>关 · 仅文件传输助手允许</option><option>开</option></select>`, '这是自测通道，不影响其他会话。')}</div><div class="reply-switch-list"><div class="row-between"><div class="switch-copy"><strong>群聊只在被叫到时回</strong><span>群里只有 @ 或消息开头叫名字才回</span></div>${switchEl(true)}</div><div class="row-between"><div class="switch-copy"><strong>无条件的群</strong><span>这里列出的群不受上面限制</span></div><button class="btn btn-sm btn-ghost" data-action="toast" data-message="编辑无条件群（示例）">未设置</button></div></div><div class="param-foot"><span class="hint-inline">${icon('info')}两个群设置改动后立即保存，其余项点保存设置。</span><button class="btn btn-sm btn-primary" data-action="save-settings">保存设置</button></div></section><section class="card"><div class="card-head"><h2>版本与更新</h2>${tag('最新', 'leaf')}</div><div class="update-line"><div><strong>${esc(s.update.current)}</strong><span>构建于 ${esc(s.update.date)} · 当前已是最新版本</span></div><span class="spacer"></span><button class="btn btn-sm btn-ghost" data-action="toast" data-message="已是最新版本（示例）">检查</button></div><div class="note" style="margin-top:10px">${icon('info')}更新不直接替用户重装；下载后由用户关闭程序再替换，系统会保留数据。</div></section></div><aside class="settings-side"><section class="card"><div class="card-head"><h2>回复预算</h2><span class="card-head-note">从她的情绪来</span></div><div class="stat-line"><span class="k">当前心情</span><span class="v">72 · 心情不错</span></div><div class="stat-line"><span class="k">最多分条</span><span class="v">3 条</span></div><div class="stat-line"><span class="k">整轮字数</span><span class="v">30 字</span></div><div class="stat-line"><span class="k">不接许可</span><span class="v">0.08</span></div><div class="note" style="margin-top:12px">${icon('info')}数值只用来算预算，不会原样写进 prompt。</div></section><section class="card"><div class="card-head"><h2>最近回复</h2><span class="card-head-note">真实行为，不是指标</span></div><div class="delivery-row"><span class="state-icon">${icon('check-circle')}</span><div class="state-copy"><strong>阿澄 · 2 条短句</strong><span>09:41 · L1 / L2 已确认</span></div><span class="badge-state is-ok">sent</span></div><div class="delivery-row"><span class="state-icon">${icon('clock')}</span><div class="state-copy"><strong>最近 3 次都按时回复</strong><span>平均生成 1.8 秒 · 投递 1.4 秒</span></div></div></section></aside></div>`;
  }

  function settingsDiagnosticsPane() {
    const s = D.settings;
    return `<div class="settings-layout"><div class="settings-main"><section class="card snapshot-card"><div class="snapshot-title"><div><strong>她这轮看到了什么</strong><div class="hint-inline">${icon('file')}每轮请求快照 · 最近 20 份 · 312 KB</div></div><div class="row">${tag('目录可写', 'leaf')}${tag('旧格式兼容', 'outline')}</div></div><select class="select snapshot-select"><option>${esc(s.trace.selected)}</option><option>11-09 08:58 · 阿澄 · 3.6 秒</option><option>11-09 08:10 · 文件传输助手 · 2.9 秒</option></select><div class="snapshot-sections" style="margin-top:15px">${s.trace.sections.map((x) => `<div class="snapshot-section"><span class="snapshot-section-label">${esc(x.name)}</span><span class="snapshot-section-bar"><span style="width:${x.ratio}%;background:var(--${x.color})"></span></span><span class="snapshot-section-chars">${x.chars} 字</span></div>`).join('')}</div><div class="prompt-preview" style="margin-top:15px"><div class="inject-preview-label"><span>system · 前 600 字</span><span>只读快照</span></div><div class="inject-preview">## 你现在\n\n现在是星期一早上 09:42。你 07:52 醒的，精神还可以。\n手头正在做：整理昨天的对话。\n\n## 你注意到的\n\n- 杭州现在 18°C，多云，风有点凉。\n- 今天是周一。\n\n## 我珍视的\n\n- 真诚，不拿漂亮话糊弄人。\n... 快照保留完整原文，不截断模型实际输入。</div></div><div class="snapshot-actions"><button class="btn btn-sm btn-secondary" data-action="snapshot">查看完整快照</button><button class="btn btn-sm btn-ghost" data-action="toast" data-message="打开快照目录（示例）">打开目录</button><span class="hint-inline">最近失败：无</span></div></section></div><aside class="settings-side"><section class="card"><div class="card-head"><h2>诊断开关</h2>${tag('已开启', 'leaf')}</div><div class="detail-list"><div class="detail-item"><span class="k">最近 20 份</span><span class="v">保留 200 份</span></div><div class="detail-item"><span class="k">快照目录</span><span class="v">可写</span></div><div class="detail-item"><span class="k">旧格式</span><span class="v">兼容 · 仅缺段落信息</span></div><div class="detail-item"><span class="k">模型最近失败</span><span class="v">无</span></div></div><button class="btn btn-sm btn-secondary" style="margin-top:11px" data-action="toast" data-message="已刷新快照列表（示例）">${icon('refresh')}刷新列表</button></section><section class="card"><div class="card-head"><h2>为什么她没回</h2><span class="card-head-note">先分清后端的哪种状态</span></div><div class="status-list"><div class="status-line">${statusIcon(true)}<span class="status-name">模型请求</span><span class="status-value">成功</span></div><div class="status-line">${statusIcon(true)}<span class="status-name">发送通道</span><span class="status-value">就绪</span></div><div class="status-line">${statusIcon(true)}<span class="status-name">监听会话</span><span class="status-value">2 个</span></div><div class="status-line is-warn">${statusIcon(false, 'alert')}<span class="status-name">语音气泡</span><span class="status-value">未配置</span></div></div></section></aside></div>`;
  }

  function settings() {
    const tab = UI.settingsTab;
    const content = tab === 'reply' ? settingsReplyPane() : tab === 'diagnostics' ? settingsDiagnosticsPane() : settingsModelPane();
    return `<section class="page" data-page="settings">
      ${pageHead('设置', '模型、自动回复和诊断都从这里进。改动有草稿保护，保存失败会留在页面上。', `<span class="badge-state is-ok">模型已连接</span><button class="btn btn-secondary" data-action="toast" data-message="设置已刷新（示例）">${icon('refresh')}刷新</button><button class="btn btn-primary" data-action="save-settings">${icon('check')}保存设置</button>`, `<span>v0.11.2</span><span>·</span><span>最近保存 09:28</span>`)}
      <div class="tab-bar"><button class="tab ${tab === 'model' ? 'is-active' : ''}" data-settings-tab="model">模型与回复</button><button class="tab ${tab === 'reply' ? 'is-active' : ''}" data-settings-tab="reply">自动回复</button><button class="tab ${tab === 'diagnostics' ? 'is-active' : ''}" data-settings-tab="diagnostics">版本与诊断</button></div>
      ${content}
      <div class="page-foot-note">${icon('shield')}Key 为占位符；真实配置文件不会进入设计稿。</div>
    </section>`;
  }

  function logs() {
    const filter = window.DEMO_UI.logFilter || 'all';
    const typeGroup = { send: 'send', listen: 'send', mood: 'relation', compose: 'all', active: 'relation', budget: 'relation', afterglow: 'relation', life: 'life', voice: 'voice', stop: 'error' };
    const filtered = D.logs.filter((l) => filter === 'all' || typeGroup[l.type] === filter || (filter === 'life' && l.type === 'plan'));
    return `<section class="page" data-page="logs">
      ${pageHead('日志', '她是怎样回应的，都能在这里找到原因。预算、余韵、主动和声音不是四条装饰线。', `<div class="segmented"><button class="is-active">实时</button><button>最近 200 行</button></div><button class="btn btn-secondary" data-action="pause-logs">${icon('clock')}暂停自动滚动</button><button class="btn btn-ghost" data-action="clear-logs">${icon('trash')}清空</button>`, `<span>共 ${D.logs.length} 行</span><span>·</span><span>最多保留 700 行</span>`)}
      <div class="logs-layout">
        <div class="logs-main">
          <section class="card log-card">
            <div class="log-toolbar"><span class="section-title">运行日志</span><span class="spacer"></span><span class="hint-inline">${icon('check-circle')}自动滚动</span></div>
            <div class="log-filters">${[['all','全部'],['send','收发'],['relation','心情 / 关系'],['life','生活'],['voice','声音'],['error','异常']].map(([id, name]) => `<button class="filter-chip ${filter === id ? 'is-active' : ''}" data-log-filter="${id}">${name}</button>`).join('')}</div>
            <div class="log-rows" id="log-rows">${filtered.map((l) => `<div class="log-row"><span class="log-time">${esc(l.time)}</span><span class="log-type log-type--${l.type}">${esc(l.label)}</span><span class="log-message">${esc(l.text)}</span><span class="log-detail">${esc(l.detail)}</span></div>`).join('')}</div>
          </section>
        </div>
        <aside class="logs-side">
          <section class="card log-summary"><div class="card-head"><h2>这次运行</h2><span class="card-head-note">从启动到现在</span></div><div class="log-counts">${D.logCounts.map((c) => `<div class="log-count"><strong>${c.value}</strong><span>${esc(c.label)}</span></div>`).join('')}</div><div class="note" style="margin-top:11px">${icon('info')}日志里的拦截很多不会刷屏：硬闸门每天会挡掉大量“本来就不该发”的时刻，默认不记。</div></section>
          <section class="card"><div class="card-head"><h2>怎么读</h2><span class="card-head-note">事件不是指标</span></div><div class="status-list"><div class="status-line"><span class="status-icon">${icon('clock')}</span><span class="status-name">预算</span><span class="status-value">这一轮能说多少</span></div><div class="status-line"><span class="status-icon">${icon('wave')}</span><span class="status-name">余韵</span><span class="status-value">还留着什么</span></div><div class="status-line"><span class="status-icon">${icon('send')}</span><span class="status-name">主动</span><span class="status-value">为什么先开口 / 没开口</span></div><div class="status-line"><span class="status-icon">${icon('voice')}</span><span class="status-name">声音</span><span class="status-value">这一轮为什么改文字</span></div></div><div class="log-legend"><span class="legend-item"><i class="legend-dot"></i>正常</span><span class="legend-item"><i class="legend-dot legend-dot--sun"></i>判定</span><span class="legend-item"><i class="legend-dot legend-dot--rose"></i>情感</span><span class="legend-item"><i class="legend-dot legend-dot--rain"></i>声音</span></div></section>
          <section class="card note-card"><div class="note note--warn">${icon('alert')}发送结果有 sent / failed / unknown；unknown 不自动重发，避免对方收到两遍。</div></section>
        </aside>
      </div>
      <div class="page-foot-note">${icon('info')}示例日志 · 时间戳、会话与数值均为合成内容。</div>
    </section>`;
  }

  window.PAGES = { overview, chats, soul, mood, memory, impression, stickers, voice, data, plugins, settings, logs };
})();
