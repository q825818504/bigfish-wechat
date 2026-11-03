
/* ══════════════════════════════════════════════════════════════════
   插件开关 → 侧边栏可见性
   ══════════════════════════════════════════════════════════════════

   表情包是一个**插件**，不是一个内建页面。插件关着的时候，
   侧边栏就不该有这一项 —— 否则用户会看到一个点进去就提示
   "先去启用插件"的入口，那是设计上的自相矛盾。

   这个函数在任何可能改变插件状态的地方被调用：
     · 启动加载
     · 插件页切换开关
     · 从别的页面回来

   ⚠ 只改可见性，不改数据。表情库的文件一直都在，
      插件关掉只是"她不会发"，不是"她的表情没了"。
*/

async function syncPluginNav() {
  if (!S.bridge) return;
  try {
    const d = JSON.parse(await S.bridge.list_plugins());
    const list = d.plugins || [];
    S.plugEnabled = S.plugEnabled || {};
    for (const p of list) S.plugEnabled[p.id] = !!p.enabled;

    const nav = document.querySelector('.nav-item[data-page="stickers"]');
    const on = !!S.plugEnabled.sticker;
    if (nav) nav.hidden = !on;

    // 插件刚被关掉，而用户正停在表情包页 → 跳走
    const cur = document.querySelector('.page.is-active');
    if (!on && cur && cur.dataset.page === 'stickers') {
      go('plugins');
    }
  } catch (e) {
    // 读不到就当它开着 —— 不要因为一次请求失败把入口藏掉
    console.warn('同步插件导航失败', e);
  }
}

/* ══════════════════════════════════════════════════════════════════
   表情包页
   ══════════════════════════════════════════════════════════════════

   这一页只做**库**：
     · 看：网格预览，每张显示概览（"生气"/"害羞"）
     · 加：拖拽 / 浏览，任意格式，导入时统一转 GIF
     · 改：双击看详情，改概览和详细说明
     · 删

   **不在这里**放开关和频率 —— 那是插件页的事。
   两件事分开的理由：用户想"关掉表情"时该在插件页找到开关，
   而不是先来这一页翻半天。

   ⚠ 缩略图走 file:// 直接加载，不转 base64。
      几十张动图逐张 base64 会把 QWebChannel 拖死。
*/

/* ────────── 状态 ────────── */

S.stickers = { items: [], dir: '', enabled: false };
S.pending = [];          // 待导入：[{path, name}]
S.detailCode = '';       // 当前打开详情的表情代码

/* ────────── 读取 ────────── */

async function loadStickers() {
  if (!S.bridge) return;
  try {
    const d = JSON.parse(await S.bridge.sticker_list());
    if (!d.ok) return toast(d.error || '读取表情库失败');

    S.stickers = d;
    setText('#stickerCount', d.items.length ? `${d.items.length} 张` : '空');

    // 顶部提示：当前能不能发
    const card = $('#stickerHintCard');
    if (card) {
      card.querySelector('.card-desc').innerHTML = d.enabled
        ? '发表情<b style="color:var(--ok)">已开启</b>。开关和频率在 <a href="#" data-goto="plugins" style="color:var(--accent)">插件</a> 页的「表情包」里。'
        : '发表情<b style="color:var(--muted)">还没开启</b>。开关和频率在 <a href="#" data-goto="plugins" style="color:var(--accent)">插件</a> 页的「表情包」里 —— 这一页只管她手里有哪些牌，不管什么时候出。';
    }

    renderStickerGrid(d.items);
  } catch (e) {
    console.error(e);
    toast('读取表情库失败');
  }
}

/* ────────── 渲染 ────────── */

function stickerSrc(path) {
  return 'file:///' + encodeURI(String(path).replace(/\\/g, '/'));
}

function stickerBytes(n) {
  if (!n) return '—';
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1048576).toFixed(1) + ' MB';
}

function renderStickerGrid(items) {
  const box = $('#stickerList');
  if (!items.length) {
    box.innerHTML =
      '<div class="empty-hint">还没有表情。点右上角「添加表情」放几张进来。<br>' +
      '<span style="opacity:.75">任意格式都行，导入时统一转 GIF —— ' +
      '只有 .gif 会被微信认成原生表情（会动、对方能长按添加）。</span></div>';
    return;
  }

  box.innerHTML = items.map((it) => {
    const brief = (it.brief || '').trim();
    const isGif = it.kind === 'sticker';
    const frames = Number(it.frames || 1);
    return `<div class="sticker-item" data-code="${esc(it.code)}" title="双击查看详情">
      <div class="sticker-thumb"><img src="${stickerSrc(it.path)}" alt="" loading="lazy"></div>
      <div class="sticker-meta">
        <div class="sticker-brief${brief ? '' : ' is-empty'}">${esc(brief || '没写说明')}</div>
        <div class="sticker-foot">
          <span class="sticker-kind${isGif ? ' is-gif' : ''}">${esc(it.moodDimLabel || (isGif ? 'gif' : '图片'))} · ${Number(it.intensity || 3)}</span>
          <code style="font-size:10px;color:var(--muted)">${esc(it.code)}</code>
        </div>
      </div>
    </div>`;
  }).join('');

  box.querySelectorAll('.sticker-item').forEach((el) => {
    el.addEventListener('dblclick', () => openStickerDetail(el.dataset.code));
  });
}

/* ────────── 详情弹窗 ────────── */

function openStickerDetail(code) {
  const it = (S.stickers.items || []).find((x) => x.code === code);
  if (!it) return;
  S.detailCode = code;

  setText('#stickerDetailTitle', it.brief || '（没写说明）');
  $('#stickerDetailImg').src = stickerSrc(it.path);
  setText('#stickerDetailCode', it.code);
  setText('#stickerDetailFile', it.file);
  setText('#stickerDetailSize', stickerBytes(it.size));
  setText('#stickerDetailFrames', String(it.frames || 1) + ' 帧');

  $('#stickerDetailBrief').value = it.brief || '';
  $('#stickerDetailDesc').value = it.detail || '';
  $('#stickerDetailDim').value = it.moodDim || '';
  const inten = Number(it.intensity || 3);
  $('#stickerDetailIntensity').value = inten;
  setText('#stickerDetailIntensityVal', String(inten));

  openModal('stickerDetailModal');
  setTimeout(() => $('#stickerDetailBrief').focus(), 80);
}

async function saveStickerDetail() {
  if (!S.detailCode) return;
  const brief = $('#stickerDetailBrief').value.trim();
  const detail = $('#stickerDetailDesc').value.trim();
  const dim = $('#stickerDetailDim').value || '';
  const inten = Number($('#stickerDetailIntensity').value || 3);
  const r = JSON.parse(await S.bridge.sticker_update(
    S.detailCode, brief, detail, inten, dim));
  if (!r.ok) return toast(r.error || '保存失败');
  toast('已保存');
  closeModal('stickerDetailModal');
  loadStickers();
}

async function deleteStickerDetail() {
  if (!S.detailCode) return;
  const it = (S.stickers.items || []).find((x) => x.code === S.detailCode);
  const label = it && it.brief ? `「${it.brief}」` : S.detailCode;
  if (!confirm(`删掉 ${label}？\n表情文件会从库里移除，不是移进回收站。`)) return;
  const r = JSON.parse(await S.bridge.sticker_delete(S.detailCode));
  if (!r.ok) return toast(r.error || '删除失败');
  if (r.note) toast(r.note);
  else toast('已删除');
  closeModal('stickerDetailModal');
  loadStickers();
}

async function testStickerSend() {
  if (!S.detailCode) return;
  toast('正在发送到文件传输助手…');
  const r = JSON.parse(await S.bridge.sticker_test_send(S.detailCode));
  if (!r.ok) return toast(r.error || '发送失败');
  toast('发出去了，去看微信');
}

/* ────────── 添加弹窗 ────────── */

function openStickerAdd() {
  S.pending = [];
  renderPending();
  $('#stickerAutoDesc').checked = true;
  openModal('stickerAddModal');
}

function addPending(paths) {
  const seen = new Set(S.pending.map((x) => x.path));
  for (const p of paths) {
    if (!p || seen.has(p)) continue;
    S.pending.push({ path: p, name: String(p).split(/[\\/]/).pop() });
    seen.add(p);
  }
  renderPending();
}

function renderPending() {
  const wrap = $('#stickerPending');
  const grid = $('#stickerPendingGrid');
  if (!S.pending.length) {
    wrap.hidden = true;
    grid.innerHTML = '';
    return;
  }
  wrap.hidden = false;
  setText('#stickerPendingCount', `已选 ${S.pending.length} 张`);
  grid.innerHTML = S.pending.map((x, i) => `
    <div class="pending-cell" title="${esc(x.name)}">
      <img src="${stickerSrc(x.path)}" alt="" loading="lazy">
      <button data-idx="${i}" title="移除">×</button>
    </div>`).join('');

  grid.querySelectorAll('button[data-idx]').forEach((b) => {
    b.addEventListener('click', () => {
      S.pending.splice(Number(b.dataset.idx), 1);
      renderPending();
    });
  });
}

async function browseStickerFiles() {
  const r = JSON.parse(await S.bridge.sticker_pick_files());
  if (!r.ok) return toast(r.error || '选择失败');
  if (!r.paths || !r.paths.length) return;
  addPending(r.paths);
}

async function doStickerImport() {
  if (!S.pending.length) return toast('还没选图');
  const btn = $('#btnStickerDoImport');
  const autoDesc = $('#stickerAutoDesc').checked;

  btn.disabled = true;
  btn.textContent = '导入中…';

  try {
    // ① 导入（转 GIF、编号、落盘）
    const paths = S.pending.map((x) => x.path);
    const r = JSON.parse(await S.bridge.sticker_add(JSON.stringify(paths)));
    if (!r.ok) {
      toast(r.error || '导入失败');
      return;
    }
    const added = r.added || [];
    const failed = r.failed || [];
    if (failed.length) {
      toast(`${failed.length} 张没进来：${failed[0].why}`);
    }
    if (!added.length) {
      toast('一张都没导入成功');
      return;
    }

    // ② 自动写说明（可选，慢）
    if (autoDesc) {
      const codes = added.map((x) => x.code);
      let done = 0;
      for (const code of codes) {
        btn.textContent = `写说明 ${++done}/${codes.length}…`;
        try {
          await S.bridge.sticker_autodesc(JSON.stringify([code]));
        } catch (e) {
          // 单张失败不影响其他
          console.warn('自动备注失败', code, e);
        }
      }
    }

    toast(`导入 ${added.length} 张${autoDesc ? '，说明已写好' : ''}`);
    closeModal('stickerAddModal');
    loadStickers();
  } finally {
    btn.disabled = false;
    btn.textContent = '开始导入';
  }
}

/* ────────── 弹窗开关 ────────── */

function openModal(id) {
  const el = document.getElementById(id);
  if (el) el.classList.add('is-open');
}
function closeModal(id) {
  const el = document.getElementById(id);
  if (el) el.classList.remove('is-open');
}

/* ────────── 绑定 ────────── */

$('#btnStickerAdd')?.addEventListener('click', openStickerAdd);
$('#btnStickerOpenDir')?.addEventListener('click', async () => {
  const r = JSON.parse(await S.bridge.sticker_open_dir());
  if (!r.ok) toast(r.error || '打不开目录');
});

$('#btnStickerBrowse')?.addEventListener('click', browseStickerFiles);
$('#btnStickerClearPending')?.addEventListener('click', () => {
  S.pending = [];
  renderPending();
});
$('#btnStickerDoImport')?.addEventListener('click', doStickerImport);

$('#stickerDetailIntensity')?.addEventListener('input', () => {
  setText('#stickerDetailIntensityVal',
    $('#stickerDetailIntensity').value);
});

$('#btnStickerDetailSave')?.addEventListener('click', saveStickerDetail);
$('#btnStickerDetailDelete')?.addEventListener('click', deleteStickerDetail);
$('#btnStickerDetailTest')?.addEventListener('click', testStickerSend);

document.querySelectorAll('[data-close]').forEach((b) => {
  b.addEventListener('click', () => closeModal(b.dataset.close));
});

// 点遮罩关闭
document.querySelectorAll('.modal').forEach((m) => {
  m.addEventListener('click', (e) => {
    if (e.target === m) m.classList.remove('is-open');
  });
});

// Esc 关闭最上层的弹窗
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  const open = [...document.querySelectorAll('.modal.is-open')].pop();
  if (open) open.classList.remove('is-open');
});

/* ────────── 拖放 ──────────
   ══════════════════════════════════════════════════════════════════

   主路径是 Python 侧：bigfish/dropcatch.py 拦 QEvent.Drop，
   从 QMimeData 拿路径，再调这里的 window.__onDropFiles(paths)。

   为什么不靠 HTML5 的 dataTransfer.files：
     · QWebEngineView 默认 acceptDrops=False，系统层面就拒了，
       页面里的 dragover 根本收不到（鼠标显示禁止图标）
     · 就算收到，QtWebEngine 的 File 对象也没有 path 属性 ——
       拿不到路径就没法转 GIF

   下面那套 dragenter/dragover 只负责**视觉反馈**（虚线框高亮），
   真正拿到文件靠的是 __onDropFiles。两条都留着：
   Python 侧万一没装上，用户至少还能看到框亮了然后失败，
   而不是完全没反应。

   ══════════════════════════════════════════════════════════════════ */

// Python 侧拖放落点
window.__onDropFiles = function (paths) {
  const modal = document.getElementById('stickerAddModal');
  if (!modal || !modal.classList.contains('is-open')) return;   // 不在添加页就忽略
  if (!Array.isArray(paths) || !paths.length) return;

  const zone = document.getElementById('stickerDropZone');
  if (zone) zone.classList.remove('is-hot');

  addPending(paths);
  toast(`拖进来 ${paths.length} 张`);
};

(function bindStickerDrop() {
  const zone = $('#stickerDropZone');
  if (!zone) return;

  const inModal = () => {
    const m = document.getElementById('stickerAddModal');
    return m && m.classList.contains('is-open');
  };
  const stop = (e) => { e.preventDefault(); e.stopPropagation(); };

  ['dragenter', 'dragover'].forEach((ev) => {
    window.addEventListener(ev, (e) => {
      if (!inModal()) return;
      stop(e);
      zone.classList.add('is-hot');
    });
  });

  window.addEventListener('dragleave', (e) => {
    if (e.relatedTarget === null) zone.classList.remove('is-hot');
  });

  // 兜底：万一 Python 侧没拦住，这里试一次 HTML5 的路径
  window.addEventListener('drop', (e) => {
    if (!inModal()) return;
    stop(e);
    zone.classList.remove('is-hot');

    const files = [...(e.dataTransfer?.files || [])];
    const paths = files.map((f) => f.path).filter(Boolean);
    if (paths.length) {
      addPending(paths);
    } else if (files.length) {
      toast('拿不到文件路径，请用「浏览…」按钮');
    }
  });
})();
