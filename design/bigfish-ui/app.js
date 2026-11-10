/* 大肥鱼 UI 设计稿 · 页面切换与交互演示。没有后端，不访问真实数据。 */
(function () {
  const D = window.DEMO;
  const UI = window.DEMO_UI;
  const root = document.getElementById('page-root');
  const crumb = document.getElementById('crumb-title');
  const modalRoot = document.getElementById('modal-root');
  const toastRoot = document.getElementById('toast-root');
  const titles = {
    overview: '她的今天', chats: '会话', soul: '灵魂', mood: '心情', memory: '记忆',
    impression: '印象', stickers: '表情包', voice: '声音', data: '数据目录',
    plugins: '插件', settings: '设置', logs: '日志'
  };
  let currentPage = 'overview';

  const icon = (name) => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const switchEl = (checked) => `<label class="switch"><input type="checkbox" ${checked ? 'checked' : ''}><span class="switch-track"></span></label>`;

  function render(page, focus = false) {
    if (!window.PAGES || !window.PAGES[page]) return;
    currentPage = page;
    document.querySelectorAll('.nav-item[data-page]').forEach((b) => {
      const on = b.dataset.page === page;
      b.classList.toggle('is-active', on);
      b.setAttribute('aria-current', on ? 'page' : 'false');
    });
    crumb.textContent = titles[page] || '大肥鱼';
    root.innerHTML = window.PAGES[page]();
    root.scrollTop = 0;
    if (focus) root.focus({ preventScroll: true });
  }

  function toast(message, isError = false) {
    if (!toastRoot) return;
    const el = document.createElement('div');
    el.className = 'toast' + (isError ? ' is-error' : '');
    el.textContent = message;
    toastRoot.appendChild(el);
    window.setTimeout(() => el.remove(), 2800);
  }

  function closeModal() {
    modalRoot.innerHTML = '';
    document.body.style.overflow = '';
  }

  function modalShell(title, body, foot = '', wide = false) {
    return `<div class="modal-backdrop" data-modal-backdrop>
      <section class="modal ${wide ? 'modal--wide' : ''}" role="dialog" aria-modal="true" aria-labelledby="modal-title" data-ui>
        <header class="modal-head"><div><h2 id="modal-title">${title}</h2></div><button class="modal-close" data-close-modal aria-label="关闭">${icon('x')}</button></header>
        <div class="modal-body">${body}</div>
        ${foot ? `<footer class="modal-foot">${foot}</footer>` : ''}
      </section>
    </div>`;
  }

  function openModal(html) {
    modalRoot.innerHTML = html;
    document.body.style.overflow = 'hidden';
    const focusable = modalRoot.querySelector('button, input, select, textarea, [tabindex]');
    if (focusable) window.setTimeout(() => focusable.focus({ preventScroll: true }), 20);
  }

  function scopeModal(chatName) {
    const fields = D.scope.fields;
    const overrideCount = fields.filter((f) => f.state === 'override').length;
    const rows = fields.map((f) => {
      const isBool = f.kind === 'bool';
      const isOverride = f.state === 'override';
      const source = isOverride ? '本会话覆盖' : `继承自账号 · ${esc(f.global)}`;
      const valueControl = isBool
        ? `<button class="seg-btn ${!isOverride ? 'is-on' : ''}">继承</button><button class="seg-btn ${isOverride && f.value === true ? 'is-on' : ''}">开</button><button class="seg-btn ${isOverride && f.value === false ? 'is-on' : ''}">关</button>`
        : isOverride
          ? `<input class="input" style="width:118px;height:28px;font-size:10px" value="${esc(f.value)}"><button class="seg-btn">恢复继承</button>`
          : `<span class="scope-readonly">${esc(f.global)}</span><span class="scope-lock">${icon('lock')}</span>`;
      return `<div class="scope-field ${isOverride ? 'is-override' : ''}">
        <div class="scope-copy"><strong>${esc(f.label)}</strong><span class="scope-source ${isOverride ? 'is-override' : ''}">${source}</span></div>
        <div class="scope-value">${valueControl}</div>
      </div>`;
    }).join('');
    openModal(modalShell(`会话配置 · ${esc(chatName)}`, `
      <p class="note" style="margin-bottom:12px">${icon('info')}只对这个会话生效；没设置的项跟随上一层。琥珀色行是本会话真正覆盖的值。</p>
      <div class="scope-chain" style="margin-bottom:13px"><span class="chain-node">全局</span>${icon('chevron-right')}<span class="chain-node">示例账号</span>${icon('chevron-right')}<span class="chain-node">${esc(chatName)}</span></div>
      ${rows}
      <div class="note" style="margin-top:12px">${icon('shield')}「处理自己发的消息」只对文件传输助手生效；这是自测通道。保存后下一条消息就生效。</div>
    `, `<span class="modal-note">${overrideCount} 项本会话覆盖 · ${fields.length - overrideCount} 项继承</span><button class="btn btn-sm btn-secondary" data-action="toast" data-message="已全部恢复继承（示例）">全部恢复继承</button><button class="btn btn-sm btn-primary" data-action="toast" data-message="会话配置已保存（示例）">保存</button>`, true));
  }

  function moodSettingsModal() {
    const m = D.mood;
    openModal(modalShell('判断参数', `
      <div class="form-grid">
        <div class="field"><label>心情基线</label><input class="input" value="${m.baseline}"><small>没事发生时她回到的位置。</small></div>
        <div class="field"><label>心情下限</label><input class="input" value="${m.floor}"><small>0 = 不限制。</small></div>
        <div class="field"><label>平复半衰期</label><select class="select"><option>12 小时</option><option>24 小时</option></select><small>隔一夜还留着余温。</small></div>
        <div class="field"><label>每天注入上限</label><input class="input" value="${m.perDay}"><small>0 = 不说心情。</small></div>
        <div class="field"><label>可信度门槛</label><input class="input" value="${m.confidence}"><small>低于它只记账、不告诉她。</small></div>
        <div class="field"><label>显著涨跌 · 总心情</label><input class="input" value="${m.sigMood}"><small>够不着只记账。</small></div>
        <div class="field"><label>显著涨跌 · 单维</label><input class="input" value="${m.sigDim}"><small>某一根突然一下也算。</small></div>
        <div class="field"><label>告诉她的方式</label><select class="select"><option>event · 底子 + 由头</option><option>label · 只说心情</option><option>off · 不告诉她</option></select><small>event 最像人。</small></div>
      </div>
      <div class="row-between" style="margin-top:15px"><div class="switch-copy"><strong>每轮对话后自动判断</strong><span>关掉后只靠自然衰减</span></div>${switchEl(true)}</div>
    `, `<span class="modal-note">会写入本机 mood-config.json</span><button class="btn btn-sm btn-secondary" data-close-modal>取消</button><button class="btn btn-sm btn-primary" data-action="save-mood">保存参数</button>`, true));
  }

  function stickerModal(id) {
    const s = D.stickers.items.find((x) => x.id === id) || D.stickers.items[0];
    const style = `--sx:${(s.cell[0] * 100 / 3).toFixed(3)}%;--sy:${(s.cell[1] * 100 / 2).toFixed(3)}%`;
    openModal(modalShell(esc(s.label), `
      <div class="sticker-preview" style="${style}"></div>
      <div class="sticker-tags" style="margin-top:0">${s.mood} · 强度 ${s.strength} / 5</div>
      <div class="detail-list" style="margin-top:12px"><div class="detail-item"><span class="k">文件</span><span class="v">demo_${s.id}.gif · 128 KB</span></div><div class="detail-item"><span class="k">情绪</span><span class="v">${s.mood} · ${s.strength}</span></div><div class="detail-item"><span class="k">最近发出</span><span class="v">11-08 22:14 · 阿澄</span></div><div class="detail-item"><span class="k">说明</span><span class="v">合成示例表情</span></div></div>
      <div class="field" style="margin-top:14px"><label>概览</label><input class="input" value="${s.label}"><small>最多 12 字；她挑图时会用这句。</small></div>
      <div class="field" style="margin-top:10px"><label>强度</label><input class="input" value="${s.strength} / 5"></div>
    `, `<button class="btn btn-sm btn-ghost" data-action="toast" data-message="已发送到文件传输助手（示例）">试发一张</button><span class="modal-note"></span><button class="btn btn-sm btn-danger" data-action="toast" data-message="删除需要确认（示例）">删除</button><button class="btn btn-sm btn-primary" data-action="toast" data-message="表情说明已保存（示例）">保存</button>`));
  }

  function memoryFactModal(id) {
    const f = D.memory.facts.find((x) => x.id === id) || D.memory.facts[0];
    openModal(modalShell(f.title, `
      <div class="row" style="margin-bottom:10px">${f.status === 'active' ? '<span class="tag tag--leaf">现在有效</span>' : f.status === 'superseded' ? '<span class="tag tag--sun">已被取代</span>' : '<span class="tag">已忘掉</span>'}<span class="tag tag--outline">${esc(f.predicate)}</span><span class="tag tag--outline">${f.visibility === 'private' ? '私密' : '可共享'}</span></div>
      <p style="font-size:12.5px;line-height:1.7;color:var(--ink-soft)">${esc(f.object)}</p>
      <div class="fact-source" style="margin-top:12px"><strong>来源</strong>${esc(f.source)}</div>
      <div class="detail-list" style="margin-top:12px"><div class="detail-item"><span class="k">归属</span><span class="v">${esc(f.owner)}</span></div><div class="detail-item"><span class="k">置信度</span><span class="v">${f.confidence}%</span></div><div class="detail-item"><span class="k">使用</span><span class="v">${f.use} 次</span></div><div class="detail-item"><span class="k">纠正历史</span><span class="v">${f.supersededBy ? esc(f.supersededBy) : '无'}</span></div></div>
    `, `<button class="btn btn-sm btn-ghost" data-close-modal>关闭</button><button class="btn btn-sm btn-secondary" data-action="toast" data-message="纠正会新增认识并保留旧行（示例）">纠正</button><button class="btn btn-sm btn-primary" data-action="toast" data-message="已忘掉结论，原话仍在（示例）">忘掉</button>`));
  }

  function snapshotModal() {
    const s = D.settings.trace;
    openModal(modalShell('她这轮看到了什么', `
      <div class="row" style="justify-content:space-between;margin-bottom:12px"><span class="faint">11-09 09:38 · 阿澄 · 4.2 秒</span><span class="tag tag--leaf">完整快照</span></div>
      <div class="snapshot-sections">${s.sections.map((x) => `<div class="snapshot-section"><span class="snapshot-section-label">${esc(x.name)}</span><span class="snapshot-section-bar"><span style="width:${x.ratio}%;background:var(--${x.color})"></span></span><span class="snapshot-section-chars">${x.chars} 字</span></div>`).join('')}</div>
      <div class="inject-preview" style="margin-top:14px">## 你现在\n\n现在是星期一早上 09:42。你 07:52 醒的，精神还可以。\n手头正在做：整理昨天的对话。\n\n## 你注意到的\n\n- 杭州现在 18°C，多云，风有点凉。\n- 今天是周一。\n\n## 我珍视的\n\n- 真诚，不拿漂亮话糊弄人。\n- 答应过的事要记得。\n\n（后续为记忆、最近对话与守卫段；快照保留模型实际收到的完整正文。）</div>
      <div class="note" style="margin-top:12px">${icon('info')}旧格式快照可能没有段落信息；目录不可写或 trace 关闭时也会在这里说明原因。</div>
    `, `<span class="modal-note">最近 20 份 · 保留上限 200</span><button class="btn btn-sm btn-ghost" data-action="toast" data-message="打开快照 JSON（示例）">打开 JSON</button><button class="btn btn-sm btn-primary" data-close-modal>关闭</button>`, true));
  }

  function wipeModal() {
    const w = D.data.wipe;
    openModal(modalShell('清空心情与记忆', `
      <div class="note note--danger">${icon('alert')}她还在，只是忘了。灵魂和珍视条目保留；这一步会先自动备份。</div>
      <div class="wipe-summary"><div class="wipe-col"><strong>保留</strong><ul>${w.keep.map((x) => `<li>${esc(x)}</li>`).join('')}</ul></div><div class="wipe-col"><strong>删除 · ${w.files} 个文件 · ${w.size}</strong><ul>${w.remove.map((x) => `<li>${esc(x)}</li>`).join('')}</ul></div></div>
      <div class="note" style="margin-top:12px">${icon('folder')}备份位置会在执行后显示在页面上；删之前可以先「看看会删什么」。</div>
    `, `<button class="btn btn-sm btn-secondary" data-close-modal>取消</button><button class="btn btn-sm btn-danger" data-action="confirm-wipe">确认清空</button>`));
  }

  function whaleModal() {
    openModal(modalShell('最后一次确认', `
      <div class="note note--danger">${icon('alert')}这会删除灵魂、心情、记忆、参数、表情与日记。她不存在了，留下的只有一份强制备份。</div>
      <div class="whale-files" style="background:var(--surface-2);color:var(--muted);font-family:var(--mono);font-size:10px;line-height:1.6;padding:10px;border-radius:8px;margin-top:12px">${esc(D.data.whale.sample)}</div>
      <div class="field" style="margin-top:14px"><label>输入「鲸落」两个字确认</label><input class="input" id="whale-input" placeholder="鲸落"><small>不是清理，是告别。执行前会再强制备份一次。</small></div>
    `, `<span class="modal-note">不可逆</span><button class="btn btn-sm btn-secondary" data-close-modal>留下她</button><button class="btn btn-sm btn-danger" data-action="confirm-whale">确认鲸落</button>`));
  }

  function accountModal() {
    openModal(modalShell('选择要连接的微信', `
      <p class="note" style="margin-bottom:12px">${icon('info')}多开时程序不会替你做选择。这里只列出正在运行的微信账号。</p>
      <div class="memory-row is-active"><span class="avatar avatar--teal">示</span><div class="memory-row-main"><div class="memory-row-title">示例账号 <span class="tag tag--leaf">当前</span></div><div class="memory-row-sub">wxid_demo · 刚刚活跃</div></div><span class="memory-row-time">在线</span></div>
      <div class="memory-row"><span class="avatar avatar--amber">备</span><div class="memory-row-main"><div class="memory-row-title">备用账号</div><div class="memory-row-sub">wxid_demo2 · 2 分钟前</div></div><span class="memory-row-time">在线</span></div>
      <div class="note" style="margin-top:12px">${icon('shield')}换号后会恢复该账号上次的监听列表，不会沿用当前账号的勾选。</div>
    `, `<button class="btn btn-sm btn-secondary" data-close-modal>取消</button><button class="btn btn-sm btn-primary" data-action="toast" data-message="已连接示例账号（示例）">使用这个账号</button>`));
  }

  function notificationsModal() {
    openModal(modalShell('运行提醒', `
      <div class="status-list">
        <div class="status-line is-warn">${icon('alert')}<span class="status-name">语音气泡未配置</span><span class="status-value">不影响文字回复</span></div>
        <div class="status-line">${icon('check-circle')}<span class="status-name">发送通道就绪</span><span class="status-value">09:41</span></div>
        <div class="status-line">${icon('info')}<span class="status-name">提示词快照已开启</span><span class="status-value">最近 20 份</span></div>
      </div>
      <p class="note" style="margin-top:12px">${icon('shield')}这里不显示真实系统通知，只是设计稿中的运行提醒入口。</p>
    `, `<button class="btn btn-sm btn-primary" data-close-modal>知道了</button>`));
  }

  function soulPreviewModal() {
    openModal(modalShell('她会收到的人格段', `
      <div class="inject-preview" style="max-height:480px">你是「大肥鱼」，称呼对方「阿澄」。对方平时也叫你「鱼鱼、肥鱼」。\n\n## 我是谁\n\n${esc(D.soul.whoIAm)}\n\n## 我珍视的\n\n- 真诚，不拿漂亮话糊弄人\n- 答应过的事要记得\n- 难过可以，但别拿它伤人\n- 好好吃饭，好好睡觉\n\n## 我的性格\n\n- 温暖：关心，但不黏人。\n- 主见：有想法，但不硬顶。\n- 顽皮：该认真时认真，平时会贫两句。\n- 直接：有话直说，但会看场合。\n\n## 我自己知道的事\n\n- 对方忙的时候，安静比关心更合适。\n- （11-08）关于他：以前觉得他不太在意，现在发现只是累了。</div>
      <div class="note" style="margin-top:12px">${icon('info')}这是按预算裁剪后的实际注入段。完整渲染效果可在设计稿的插件页查看。</div>
    `, `<button class="btn btn-sm btn-primary" data-close-modal>关闭</button>`, true));
  }

  function handleAction(action, el) {
    if (!action) return;
    if (action === 'toast') { toast(el.dataset.message || '演示操作'); return; }
    if (action === 'refresh') { toast('界面已刷新（示例）'); render(currentPage); return; }
    if (action === 'account') { accountModal(); return; }
    if (action === 'notifications') { notificationsModal(); return; }
    if (action === 'scope') { scopeModal(el.dataset.chat || '阿澄'); return; }
    if (action === 'mood-settings') { moodSettingsModal(); return; }
    if (action === 'memory-fact') { memoryFactModal(el.dataset.fact); return; }
    if (action === 'sticker-detail') { stickerModal(el.dataset.sticker); return; }
    if (action === 'snapshot') { snapshotModal(); return; }
    if (action === 'wipe-confirm') { wipeModal(); return; }
    if (action === 'whale-confirm') { whaleModal(); return; }
    if (action === 'preview-soul') { soulPreviewModal(); return; }
    if (action === 'confirm-wipe') { closeModal(); toast('已清空心情与记忆；备份已保留（示例）'); return; }
    if (action === 'confirm-whale') {
      const input = document.getElementById('whale-input');
      if (!input || input.value.trim() !== '鲸落') { toast('请先输入「鲸落」两个字', true); input?.focus(); return; }
      closeModal(); toast('鲸落完成；强制备份已保留（示例）'); return;
    }
    if (action === 'save-soul') { toast('人格已保存；下一轮对话生效（示例）'); return; }
    if (action === 'save-mood') { toast('心情参数已保存（示例）'); return; }
    if (action === 'save-voice') { toast('声音设置已保存（示例）'); return; }
    if (action === 'save-settings') { toast('设置已保存（示例）'); return; }
    if (action === 'save-plugin') { toast('插件配置已保存；下一轮对话生效（示例）'); return; }
    if (action === 'set-voice-policy') {
      D.voice.strategy = el.dataset.policy;
      render('voice');
      toast(`已切换为「${el.textContent.trim().split('\n')[0]}」策略（示例）`);
      return;
    }
    if (action === 'select-chat') { toast(`${el.dataset.chat} 已选中（示例）`); return; }
    if (action === 'select-plugin') { toast('已选中插件详情（示例）'); return; }
    if (action === 'pause-logs') {
      el.innerHTML = `${icon('play')}继续自动滚动`;
      el.dataset.action = 'resume-logs';
      toast('已暂停自动滚动（示例）'); return;
    }
    if (action === 'resume-logs') {
      el.innerHTML = `${icon('clock')}暂停自动滚动`;
      el.dataset.action = 'pause-logs';
      toast('已继续自动滚动（示例）'); return;
    }
    if (action === 'clear-logs') {
      const rows = document.getElementById('log-rows');
      if (rows) rows.innerHTML = '<div class="empty-state"><span class="empty-icon">' + icon('logs') + '</span><h3>当前视图已清空</h3><p>不会删除磁盘上的日志。</p></div>';
      toast('已清空当前日志视图（示例）'); return;
    }
  }

  document.addEventListener('click', (event) => {
    const close = event.target.closest('[data-close-modal]');
    if (close) { closeModal(); return; }
    if (event.target.matches('[data-modal-backdrop]')) { closeModal(); return; }

    const nav = event.target.closest('.nav-item[data-page]');
    if (nav) { render(nav.dataset.page); return; }

    const goto = event.target.closest('[data-goto]');
    if (goto) { render(goto.dataset.goto, true); return; }

    if (event.target.closest('.switch')) {
      event.stopPropagation();
      toast('开关状态已改变（示例，不会写入真实配置）');
      return;
    }

    const memTab = event.target.closest('[data-memory-tab]');
    if (memTab) { UI.memoryTab = memTab.dataset.memoryTab; render('memory'); return; }
    const setTab = event.target.closest('[data-settings-tab]');
    if (setTab) { UI.settingsTab = setTab.dataset.settingsTab; render('settings'); return; }
    const voiceTab = event.target.closest('[data-voice-tab]');
    if (voiceTab) { UI.voiceTab = voiceTab.dataset.voiceTab; render('voice'); return; }
    const dataTab = event.target.closest('[data-data-tab]');
    if (dataTab) { UI.dataTab = dataTab.dataset.dataTab; render('data'); return; }
    const logFilter = event.target.closest('[data-log-filter]');
    if (logFilter) { UI.logFilter = logFilter.dataset.logFilter; render('logs'); return; }

    const chatFilter = event.target.closest('[data-chat-filter]');
    if (chatFilter && currentPage === 'chats') {
      const group = chatFilter.dataset.chatFilter;
      chatFilter.closest('.segmented').querySelectorAll('button').forEach((b) => b.classList.toggle('is-active', b === chatFilter));
      document.querySelectorAll('[data-chat-filter-row]').forEach((row) => {
        row.style.display = group === 'all' || row.dataset.chatFilterRow === group ? '' : 'none';
      });
      return;
    }

    const actionEl = event.target.closest('[data-action]');
    if (actionEl) { event.preventDefault(); event.stopPropagation(); handleAction(actionEl.dataset.action, actionEl); return; }
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && modalRoot.querySelector('.modal-backdrop')) closeModal();
    if (event.key === 'Tab' && modalRoot.querySelector('.modal')) {
      const focusables = Array.from(modalRoot.querySelectorAll('button, input, select, textarea, [tabindex]:not([tabindex="-1"])')).filter((el) => !el.disabled);
      if (!focusables.length) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });

  render('overview');
})();
