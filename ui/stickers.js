/* ══════════════════════════════════════════════════════════════════
   表情包页
   ══════════════════════════════════════════════════════════════════

   这一页要回答两个问题，顺序不能反：

     1. 她能不能发？（开关 + 频率 + 冷却）—— 用户先关心这个
     2. 她手里有什么？（表情库）—— 有货才谈得上发

   所以布局从上到下就是这两个卡片。

   ⚠ 缩略图用 file:// 直接加载，不走后端读文件转 base64：
      表情库可能有几十张动图，逐张 base64 会把 QWebChannel 拖死。
      file:// 在 QtWebEngine 里读本地图片没问题（同一个进程）。
*/

async function loadStickers() {
  if (!S.bridge) return;
  try {
    const d = JSON.parse(await S.bridge.sticker_list());
    if (d.error) return toast(d.error);

    S.stickers = d;
    const cfg = d.config || {};

    $('#stickerEnabled').checked = !!cfg.enabled;
    $('#stickerRate').value = Math.round((cfg.rate ?? 0.15) * 100);
    $('#stickerCooldown').value = cfg.cooldown ?? 180;
    $('#stickerMode').value = cfg.mode || 'ai';
    onStickerRate();

    setText('#stickerStateTag', cfg.enabled ? '开着' : '关着');
    $('#stickerStateTag').className = 'tag' + (cfg.enabled ? ' ok' : '');

    $('#stickerDirPath').textContent = d.dir || 'stickers\\';
    renderStickerList(d.items || [], d.last);
  } catch (e) {
    toast('读取表情库失败');
  }
}

function onStickerRate() {
  const v = Number($('#stickerRate').value || 0);
  const approx = v <= 0 ? '不会发'
    : v >= 100 ? '每句都可能发'
      : `大约 ${Math.max(1, Math.round(100 / v))} 句一张`;
  setText('#stickerRateVal', `${v}%`);
  const small = $('#stickerRate').closest('.field').querySelector('small');
  if (small) small.textContent = `每一轮回复有多大概率考虑配一张。现在：${approx}。`;
}

function renderStickerList(items, last) {
  const box = $('#stickerList');
  setText('#stickerCount', items.length ? `${items.length} 张` : '空');

  if (!items.length) {
    box.innerHTML =
      '<div class="empty-hint">还没有表情。点右上角「导入…」放几张进来。<br>' +
      '<span style="opacity:.75">只有 .gif 会被微信认成原生表情 —— 会动、对方能长按添加。' +
      'png/jpg 会发成普通图片。</span></div>';
    return;
  }

  box.innerHTML = items.map((it) => {
    const src = 'file:///' + encodeURI(it.path.replace(/\\/g, '/'));
    const isGif = it.kind === 'sticker';
    return `<div class="sticker-item">
      <div class="sticker-thumb"><img src="${src}" alt="" loading="lazy"></div>
      <div class="sticker-meta">
        <div class="sticker-name" title="${esc(it.name)}">${esc(it.name)}</div>
        <input class="sticker-desc" data-name="${esc(it.name)}"
               value="${esc(it.desc || '')}"
               placeholder="她什么时候会用它" maxlength="60">
        <div class="sticker-foot">
          <span class="sticker-kind${isGif ? ' is-gif' : ''}">${isGif ? 'gif 表情' : '图片'}</span>
          <button class="sticker-del" data-del="${esc(it.name)}">删</button>
        </div>
      </div>
    </div>`;
  }).join('');

  box.querySelectorAll('.sticker-desc').forEach((inp) => {
    const save = debounce(async () => {
      const r = JSON.parse(await S.bridge.sticker_set_desc(
        inp.dataset.name, inp.value.trim()));
      if (!r.ok) toast(r.error || '保存说明失败');
    }, 500);
    inp.addEventListener('input', save);
  });

  box.querySelectorAll('.sticker-del').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const name = btn.dataset.del;
      if (!confirm(`删掉「${name}」？\n表情文件会从表情库里移除，不是移进回收站。`)) return;
      const r = JSON.parse(await S.bridge.sticker_delete(name));
      if (!r.ok) return toast(r.error || '删除失败');
      toast('已删除');
      loadStickers();
    });
  });
}

function debounce(fn, ms) {
  let t = 0;
  return (...a) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...a), ms);
  };
}

/* ────────── 绑定 ────────── */

$('#stickerRate')?.addEventListener('input', onStickerRate);

$('#btnStickerSave')?.addEventListener('click', async () => {
  const patch = {
    enabled: $('#stickerEnabled').checked,
    rate: Number($('#stickerRate').value || 0) / 100,
    cooldown: Number($('#stickerCooldown').value || 0),
    mode: $('#stickerMode').value,
  };
  const r = JSON.parse(await S.bridge.sticker_save_config(JSON.stringify(patch)));
  if (!r.ok) return toast(r.error || '保存失败');
  setText('#stickerStateTag', patch.enabled ? '开着' : '关着');
  $('#stickerStateTag').className = 'tag' + (patch.enabled ? ' ok' : '');
  toast(patch.enabled ? '已打开。下一轮对话开始生效'
    : '已关闭。她不会再发表情');
});

$('#btnStickerImport')?.addEventListener('click', async () => {
  const r = JSON.parse(await S.bridge.sticker_import(''));
  if (!r.ok) return toast(r.error || '导入失败');
  const n = (r.added || []).length;
  if (n) toast(`导入 ${n} 张`);
  if ((r.failed || []).length) {
    toast(`${r.failed.length} 张没进来：${r.failed[0].why}`);
  }
  loadStickers();
});

$('#btnStickerRefresh')?.addEventListener('click', loadStickers);

$('#stickerDirPath')?.addEventListener('click', async () => {
  await S.bridge.sticker_open_dir();
});
