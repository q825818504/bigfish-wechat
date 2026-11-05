
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
    // slim=true：不带 detail/tags/path —— 那三项合计 320 KB，
    // 而翻页只显示缩略图和一句话。实测 1048 KB → 约 330 KB。
    const d = JSON.parse(await S.bridge.sticker_list(true));
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
    fillThumbsInBackground(d.items);
  } catch (e) {
    console.error(e);
    toast('读取表情库失败');
  }
}

/* 缩略图补齐：每次一批，直到没有缺的。
 *
 * 为什么放后台、为什么分批：导入 2000 张之后，一次性生成要 10 秒，
 * 那 10 秒里界面是死的。每次 60 张（约 300ms），两次调用之间
 * 浏览器能重绘、用户能点击。
 *
 * 补完的那一批就地换掉 img 的 src —— 不重排整个网格，
 * 否则每批都把 2181 个 DOM 重建一遍，等于没优化。
 */
let _thumbFilling = false;

async function fillThumbsInBackground(items) {
  if (_thumbFilling || !S.bridge) return;
  if (!items.some((it) => !it.thumb)) return;
  _thumbFilling = true;
  try {
    for (let round = 0; round < 200; round++) {
      const r = JSON.parse(await S.bridge.sticker_fill_thumbs(60));
      if (!r.ok || !r.left) break;
      // 不重新拉列表，直接问"现在哪些还有 thumb"
      const d2 = JSON.parse(await S.bridge.sticker_list());
      if (!d2.ok) break;
      S.stickers = d2;
      patchThumbSrcs(d2.items);
    }
  } catch (e) {
    console.warn('补缩略图失败', e);
  } finally {
    _thumbFilling = false;
  }
}

function patchThumbSrcs(items) {
  const box = $('#stickerList');
  if (!box) return;
  for (const it of items) {
    if (!it.thumb) continue;
    const el = box.querySelector(`.sticker-item[data-code="${it.code}"] img`);
    if (el && el.dataset.thumbed !== '1') {
      el.src = stickerSrc(it.thumb);
      el.dataset.thumbed = '1';
    }
  }
}

/* ────────── 渲染 ────────── */

function stickerSrc(path) {
  return 'file:///' + encodeURI(String(path).replace(/\\/g, '/'));
}

/* 网格里的预览一律走缩略图。
 *
 * 以前是原图直读：2181 张 × 平均 0.79 Mpx ≈ 1580 Mpx 的解码，
 * 外加 836 MB 磁盘读 —— 每进一次页面就是十几秒的白屏。
 * 缩略图 256px、静态 WebP，同屏几十张加起来不到 2 Mpx。
 *
 * 没有 thumb 就回退原图：还没补出来的那几张宁可慢一点，不能空白。
 */
function stickerThumb(it) {
  return stickerSrc(it.thumb ? it.thumb : it.path);
}

function stickerBytes(n) {
  if (!n) return '—';
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1048576).toFixed(1) + ' MB';
}

/* ────────── 分页 ──────────
 *
 * 为什么要分页，而不是"一次全画 + content-visibility"：
 *
 *   实测（Chromium 1440×900，2181 格，真界面真数据）：
 *     全量 + content-visibility:auto   p50 22.0ms / p95 25.6ms / 首屏布局 29ms
 *     全量、不开 content-visibility    p50 16.6ms / p95 19.0ms / 首屏布局 99ms
 *     一页 60 张                       p50 16.5ms / p95 17.6ms / 首屏布局 ~15ms
 *
 *   content-visibility 在这里是**负优化**：每格都带 border-radius +
 *   overflow:hidden，浏览器要为每个屏外格子反复做尺寸评估和裁剪，
 *   比老老实实绘制它们还贵。所以它已经从 CSS 里删掉了。
 *
 * ⚠ 是**真翻页**，不是滚到底自动加载。
 *   自动加载的问题不是性能，是手感：用户永远不知道自己在第几屏、
 *   也不知道还有多少，划着划着一堆图冒出来，像永远划不到底。
 *   翻页时页码、总页数都写着，翻到最后一张就是最后一张。
 */
const STICKER_PAGE = 60;
S.stickerPage = 0;           // 当前第几页（0 起）
S.stickerItems = [];         // 当前的全部条目

function stickerCardHtml(it) {
  const brief = (it.brief || '').trim();
  const isGif = it.kind === 'sticker';
  // 页码对解析没有帮助：本页的图一定可见，直接按需加载即可，
  // 不需要 loading="lazy"（它反而会让翻页瞬间出现一片空白）。
  return `<div class="sticker-item" data-code="${esc(it.code)}" title="双击查看详情">
      <div class="sticker-thumb"><img src="${stickerThumb(it)}" alt="" decoding="async"${it.thumb ? ' data-thumbed="1"' : ''}></div>
      <div class="sticker-meta">
        <div class="sticker-brief${brief ? '' : ' is-empty'}">${esc(brief || '没写说明')}</div>
        <div class="sticker-foot">
          <span class="sticker-kind${isGif ? ' is-gif' : ''}">${esc(it.moodDimLabel || (isGif ? 'gif' : '图片'))} · ${Number(it.intensity || 3)}</span>
          <code style="font-size:10px;color:var(--muted)">${esc(it.code)}</code>
        </div>
      </div>
    </div>`;
}

function bindStickerCards(root) {
  root.querySelectorAll('.sticker-item:not([data-bound])').forEach((el) => {
    el.dataset.bound = '1';
    el.addEventListener('dblclick', () => openStickerDetail(el.dataset.code));
  });
}

function stickerPageCount() {
  return Math.max(1, Math.ceil((S.stickerItems || []).length / STICKER_PAGE));
}

/* 渲染某一页。整页替换，不做增量 —— 一页只有 60 个格子，
 * 整页重建 ≈ 15ms，比维护两套追加/回退逻辑省心得多。 */
function renderStickerPage(page) {
  const box = $('#stickerList');
  if (!box) return;
  const total = S.stickerItems.length;
  const pages = stickerPageCount();
  S.stickerPage = Math.max(0, Math.min(pages - 1, page));

  const from = S.stickerPage * STICKER_PAGE;
  const to = Math.min(total, from + STICKER_PAGE);
  const slice = S.stickerItems.slice(from, to);

  box.innerHTML = slice.map(stickerCardHtml).join('');
  bindStickerCards(box);
  updateStickerPager();
  // 翻页后回到顶部，否则会停在上一页的位置，看起来像"少了一半"
  const sc = document.getElementById('pages');
  if (sc) sc.scrollTop = 0;
}

function renderStickerGrid(items) {
  S.stickerItems = items || [];
  S.stickerPage = 0;
  const box = $('#stickerList');
  box.innerHTML = '';

  if (!S.stickerItems.length) {
    box.innerHTML =
      '<div class="empty-hint">还没有表情。点右上角「添加表情」放几张进来。<br>' +
      '<span style="opacity:.75">任意格式都行，导入时统一转 GIF —— ' +
      '只有 .gif 会被微信认成原生表情（会动、对方能长按添加）。</span></div>';
    updateStickerPager();
    return;
  }
  renderStickerPage(0);
}

function updateStickerPager() {
  const bar = $('#stickerMoreBar');
  if (!bar) return;
  const pages = stickerPageCount();
  const total = S.stickerItems.length;
  if (pages <= 1) { bar.hidden = true; return; }
  bar.hidden = false;
  const from = S.stickerPage * STICKER_PAGE + 1;
  const to = Math.min(total, (S.stickerPage + 1) * STICKER_PAGE);
  setText('#stickerMoreText',
    `第 ${S.stickerPage + 1} / ${pages} 页 · ${from}–${to} / ${total}`);
  const prev = $('#btnStickerPrev');
  const next = $('#btnStickerNext');
  if (prev) prev.disabled = S.stickerPage <= 0;
  if (next) next.disabled = S.stickerPage >= pages - 1;
}

$('#btnStickerPrev')?.addEventListener('click', () => renderStickerPage(S.stickerPage - 1));
$('#btnStickerNext')?.addEventListener('click', () => renderStickerPage(S.stickerPage + 1));

/* ────────── 详情弹窗 ────────── */

async function openStickerDetail(code) {
  const base = (S.stickers.items || []).find((x) => x.code === code);
  if (!base) return;
  S.detailCode = code;

  // 先用列表里就有的字段把弹窗填上（不让用户看空白），
  // 再补一次完整信息（detail / tags / 原图路径），通常 1 ms 内回来。
  setText('#stickerDetailTitle', base.brief || '（没写说明）');
  $('#stickerDetailImg').src = stickerSrc(base.thumb || base.path);
  setText('#stickerDetailCode', base.code);
  setText('#stickerDetailFile', base.file);
  setText('#stickerDetailSize', stickerBytes(base.size));
  setText('#stickerDetailFrames', String(base.frames || 1) + ' 帧');
  $('#stickerDetailBrief').value = base.brief || '';
  $('#stickerDetailDesc').value = '';
  $('#stickerDetailDim').value = base.moodDim || '';

  openModal('stickerDetailModal');

  let it = base;
  try {
    const r = JSON.parse(await S.bridge.sticker_get(code));
    if (r.ok && r.item) it = r.item;
  } catch (e) {
    console.warn('取详情失败，用列表里的字段兜底', e);
  }
  // 弹窗可能已经被关掉 / 换了另一张 —— 别把旧数据写进去
  if (S.detailCode !== code) return;

  setText('#stickerDetailTitle', it.brief || '（没写说明）');
  $('#stickerDetailImg').src = stickerSrc(it.path || it.thumb);
  setText('#stickerDetailSize', stickerBytes(it.size));
  $('#stickerDetailBrief').value = it.brief || '';
  $('#stickerDetailDesc').value = it.detail || '';
  $('#stickerDetailDim').value = it.moodDim || '';
  const inten = Number(it.intensity || 3);
  $('#stickerDetailIntensity').value = inten;
  setText('#stickerDetailIntensityVal', String(inten));

  // 记住完整版，保存/删除时用它，避免列表里只有精简字段
  S.detailItem = it;
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
  // 已选列表也用缩略图：2000 张原图在 180px 的滚动框里，
  // 光是解码就能把这个弹窗拖死。
  // 这里没有 bridge 往返，退化成原图 —— 但那条路径只在"刚选完、还没导入"时走。
  grid.innerHTML = S.pending.map((x, i) => `
    <div class="pending-cell" title="${esc(x.name)}">
      <img src="${stickerSrc(x.path)}" alt="" loading="lazy" decoding="async">
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
    // ① 导入（查重 → 转 GIF → 编号 → 落盘 → 做缩略图）
    //
    // 分批：以前 2000 张一次塞进去，这一个 await 要等十几分钟，
    // 期间按钮虽然写着"导入中…"，界面却连滚动都卡。
    // 现在 40 张一往返，每批之间浏览器能重绘，进度是真的在动。
    //
    // ⚠ 批次别调太小：后端每批只写一次 index（1MB 左右），
    //   20 张一批的话 2000 张要写 100 次。
    const paths = S.pending.map((x) => x.path);
    const BATCH = 40;
    const added = [];
    const failed = [];
    const skipped = [];
    const near = [];
    for (let i = 0; i < paths.length; i += BATCH) {
      const slice = paths.slice(i, i + BATCH);
      btn.textContent = `导入 ${Math.min(i + BATCH, paths.length)}/${paths.length}…`;
      const r = JSON.parse(await S.bridge.sticker_add(JSON.stringify(slice)));
      if (!r.ok) {
        toast(r.error || '导入失败');
        break;
      }
      added.push(...(r.added || []));
      failed.push(...(r.failed || []));
      skipped.push(...(r.skipped || []));
      near.push(...(r.near || []));
      // 让浏览器喘一口气，进度文字才画得出来
      await new Promise((res) => setTimeout(res, 0));
    }
    showImportReport({ added, failed, skipped, near });
    if (!added.length && !skipped.length) {
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
        if (done % 5 === 0) await new Promise((res) => setTimeout(res, 0));
      }
    }

    closeModal('stickerAddModal');
    loadStickers();
  } finally {
    btn.disabled = false;
    btn.textContent = '开始导入';
  }
}

/* 导入报告：新增 / 重复跳过 / 相似提示 / 失败，四类分开说。
 *
 * 为什么要单独一块而不是一句 toast：
 *   「2000 张里跳过 380 张重复」这件事，用户需要知道**具体是哪几张**，
 *   否则会怀疑图丢了。这里先给一行汇总，再把明细写进日志区。 */
function showImportReport({ added, failed, skipped, near }) {
  const bits = [];
  if (added.length) bits.push(`新增 ${added.length} 张`);
  if (skipped.length) bits.push(`跳过重复 ${skipped.length} 张`);
  if (failed.length) bits.push(`失败 ${failed.length} 张`);
  if (near.length) bits.push(`相似 ${near.length} 张（已收）`);
  toast(bits.length ? bits.join('，') : '没有变化');

  const L = (t, k) => pushLog(`[表情] ${t}`, k);
  if (skipped.length) {
    L(`跳过 ${skipped.length} 张重复：` +
      skipped.slice(0, 8).map((s) => `${s.file}（同 ${s.code}，${s.why}）`).join('；') +
      (skipped.length > 8 ? ` …… 还有 ${skipped.length - 8} 张` : ''), '');
  }
  if (near.length) {
    L(`${near.length} 张很像库里已有的图，仍然收进来了：` +
      near.slice(0, 8).map((s) => `${s.file}（近 ${s.code}，${s.why}）`).join('；'), '');
  }
  if (failed.length) {
    L(`${failed.length} 张没进来：` +
      failed.slice(0, 8).map((s) => `${s.file}（${s.why}）`).join('；'), 'err');
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
