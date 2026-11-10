/* ══════════════════════════════════════════════════════════════════
   声音页（插件启用后才出现）
   ══════════════════════════════════════════════════════════════════

   这一页只做配置：API 地址 / Key / 模型 / 音色 / 情绪参数。
   它**不合成** —— 合成由回复链路按需调用，避免打开页面就花积分。

   "测试连接" 只 GET /v1/models，不消耗生成额度。
*/

const VOICE = { data: null };

function voiceNum(sel, fallback) {
  const v = parseFloat($(sel)?.value);
  return Number.isFinite(v) ? v : fallback;
}

function fillVoiceSelect(sel, items, value) {
  const el = $(sel);
  if (!el) return;
  el.innerHTML = (items || []).map((it) => {
    const id = typeof it === 'string' ? it : it.id;
    const label = typeof it === 'string' ? it : it.name;
    return `<option value="${esc(id)}">${esc(label)}</option>`;
  }).join('');
  if (value !== undefined && value !== null) {
    if (value && !Array.from(el.options).some((o) => o.value === value)) {
      const opt = document.createElement('option');
      opt.value = value;
      opt.textContent = value;
      el.appendChild(opt);
    }
    el.value = value;
  }
}

function setVoiceState(state, ok, tagText) {
  const tag = $('#voiceTag');
  const text = $('#voiceState');
  if (text) text.textContent = state;
  if (tag) {
    tag.className = 'tag ' + (ok ? 'ok' : 'warn');
    tag.textContent = tagText || state;
  }
}

function setDriverState(text, ok) {
  const tag = $('#voiceDriverTag');
  if (tag) {
    tag.className = 'tag ' + (ok ? 'ok' : 'warn');
    tag.textContent = text;
  }
}

async function refreshVoiceDevices() {
  if (!S.bridge) return;
  try {
    const d = JSON.parse(await S.bridge.get_voice_devices());
    if (!d.ok) {
      setDriverState('检测失败', false);
      setText('#voiceDriverHint', d.error || '读不到声卡');
      return;
    }
    const sel = $('#voiceDevice');
    const keep = sel.value;
    sel.innerHTML = '<option value="">— 未选择 —</option>' + (d.outputs || []).map(
      (x) => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join('');
    if (keep) sel.value = keep;
    const cable = (d.outputs || []).find((x) => /cable/i.test(x.name || ''));
    if (!sel.value && cable) sel.value = String(cable.id);

    const btn = $('#btnInstallVbCable');
    if (d.cable) {
      setDriverState('已安装', true);
      setText('#voiceDriverHint',
        `CABLE 已就绪：播放 ${ (d.outputs || []).length } 个 / 录音 ${ (d.inputs || []).length } 个。微信麦克风选 CABLE Output。`);
      if (btn) { btn.disabled = true; btn.textContent = 'VB-CABLE 已安装'; }
    } else if (d.package && d.package.ok) {
      setDriverState('未安装', false);
      setText('#voiceDriverHint',
        `内置驱动包已校验（${d.package.files || 0} 个文件），点左边一键安装。`);
      if (btn) { btn.disabled = false; btn.textContent = '一键安装 VB-CABLE'; }
    } else {
      setDriverState('驱动包异常', false);
      const bad = [...(d.package?.missing || []), ...(d.package?.bad || [])];
      setText('#voiceDriverHint', '内置驱动包不完整：' + (bad.slice(0, 3).join('；') || '未知'));
      if (btn) btn.disabled = true;
    }
  } catch (e) {
    setDriverState('检测失败', false);
    setText('#voiceDriverHint', e.message || String(e));
  }
}

async function loadVoice() {
  if (!S.bridge) return;
  try {
    const d = JSON.parse(await S.bridge.get_voice_config());
    if (!d.ok) {
      setVoiceState(d.error || '读取失败', false, '读取失败');
      return;
    }
    VOICE.data = d;
    const s = d.settings || {};
    $('#voiceBaseUrl').value = s.base_url || '';
    $('#voiceApiKey').value = s.api_key || '';
    $('#voiceModel').value = s.model || 'breeze-tts-2-multilingual';
    $('#voiceVoice').value = s.voice || '';
    $('#voiceLanguage').value = s.language || 'zh';
    $('#voiceSpeed').value = Number(s.speed ?? 1).toFixed(2);
    $('#voiceGuidance').value = Number(s.guidance ?? 2).toFixed(1);
    $('#voiceVolume').value = Number(s.volume ?? 1).toFixed(2);
    $('#voiceMaxChars').value = s.max_turn_chars ?? 400;
    $('#voiceCacheMb').value = s.cache_mb ?? 200;
    $('#voiceTimeout').value = s.timeout ?? 90;
    $('#voiceHotkey').value = s.hotkey || 'Ctrl+Alt';
    const devices = d.devices || [];
    const devSel = $('#voiceDevice');
    devSel.innerHTML = '<option value="">— 未选择 —</option>' + devices.map(
      (x) => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join('');
    devSel.value = s.device || '';
    const cable = devices.some((x) => /cable/i.test(x.name || ''));
    const devHint = $('#voiceDeviceHint');
    if (devHint) {
      devHint.innerHTML = cable
        ? '选 <b>CABLE Input</b>；微信麦克风要选 <b>CABLE Output</b>。只在气泡模式用。'
        : '没检测到 CABLE Input。语音气泡需要先安装 VB-CABLE 并重启；当前只能发音频文件。';
    }
    if (s.device && devSel.value !== String(s.device)) {
      const opt = document.createElement('option');
      opt.value = s.device;
      opt.textContent = s.device;
      devSel.appendChild(opt);
      devSel.value = s.device;
    }

    const p = s.policy || {};
    $('#voiceStrategy').value = p.mode || 'preview';
    $('#voiceSendAs').value = p.send_as || 'file';
    syncVoiceModes();
    syncVoiceSendTag();
    $('#voiceProbability').value = p.probability ?? 0.2;
    $('#voiceCooldown').value = p.cooldown ?? 600;
    $('#voiceDailyLimit').value = p.daily_limit ?? 12;
    $('#voiceConsecutive').value = p.consecutive_limit ?? 2;
    $('#voicePolicyMaxChars').value = p.max_chars ?? 220;
    $('#voiceGroup').checked = !!p.group_enabled;
    $('#voiceOwnerOnly').checked = !!p.owner_only;
    $('#voiceFallback').checked = p.fallback_text !== false;
    $('#voiceTagOverride').checked = p.tag_override !== false;
    $('#voiceFilterUrl').checked = p.filter_url !== false;
    $('#voiceFilterCode').checked = p.filter_code !== false;
    $('#voiceFilterTable').checked = p.filter_table !== false;
    $('#voiceFilterList').checked = p.filter_list !== false;

    const scenes = p.scenes || {};
    const sceneOff = new Set(['conflict', 'group_public']);
    document.querySelectorAll('.voice-scene').forEach((cb) => {
      const keys = (cb.dataset.scenes || '').split(',').filter(Boolean);
      const vals = keys.map((k) => (scenes[k] === undefined
        ? !sceneOff.has(k) : !!scenes[k]));
      cb.checked = vals.every(Boolean);
      cb.indeterminate = vals.some(Boolean) && !vals.every(Boolean);
    });

    fillVoiceSelect('#voiceProfile', d.profiles || [], s.profile || '');
    if (!Array.from($('#voiceModel').options).some((o) => o.value === s.model)) {
      const opt = document.createElement('option');
      opt.value = s.model;
      opt.textContent = s.model;
      $('#voiceModel').appendChild(opt);
      $('#voiceModel').value = s.model;
    } else {
      $('#voiceModel').value = s.model || 'breeze-tts-2-multilingual';
    }

    const cache = d.cache || {};
    setText('#voiceCacheInfo',
      `${cache.files || 0} 个文件 / ${cache.mb || 0} MB`);
    setText('#voiceSaveHint', '');
    setVoiceState(d.enabled ? '已启用' : '插件未启用', d.enabled,
      d.ready ? '已配置' : '待配置');
    await refreshVoiceDevices();
  } catch (e) {
    setVoiceState('读取失败', false, '读取失败');
    toast('读取声音配置失败');
  }
}

function voicePayload() {
  const scenes = {
    group_public: !!$('#voiceGroup').checked,
  };
  document.querySelectorAll('.voice-scene').forEach((cb) => {
    (cb.dataset.scenes || '').split(',').filter(Boolean).forEach((k) => {
      scenes[k] = !!cb.checked;
    });
  });
  return {
    base_url: $('#voiceBaseUrl').value.trim(),
    api_key: $('#voiceApiKey').value,
    model: $('#voiceModel').value,
    voice: $('#voiceVoice').value.trim(),
    language: $('#voiceLanguage').value.trim() || 'zh',
    profile: $('#voiceProfile').value,
    speed: voiceNum('#voiceSpeed', 1.0),
    guidance: voiceNum('#voiceGuidance', 2.0),
    volume: voiceNum('#voiceVolume', 1.0),
    max_turn_chars: parseInt($('#voiceMaxChars').value, 10) || 400,
    cache_mb: parseInt($('#voiceCacheMb').value, 10) || 0,
    timeout: voiceNum('#voiceTimeout', 90),
    device: $('#voiceDevice').value,
    hotkey: $('#voiceHotkey').value.trim() || 'Ctrl+Alt',
    policy: {
      mode: $('#voiceStrategy').value,
      delivery: 'voice_only',
      send_as: $('#voiceSendAs').value,
      probability: voiceNum('#voiceProbability', 0.2),
      cooldown: parseInt($('#voiceCooldown').value, 10) || 0,
      daily_limit: parseInt($('#voiceDailyLimit').value, 10) || 0,
      consecutive_limit: parseInt($('#voiceConsecutive').value, 10) || 0,
      max_chars: parseInt($('#voicePolicyMaxChars').value, 10) || 220,
      max_parts: 0,
      group_enabled: $('#voiceGroup').checked,
      owner_only: $('#voiceOwnerOnly').checked,
      filter_url: $('#voiceFilterUrl').checked,
      filter_code: $('#voiceFilterCode').checked,
      filter_table: $('#voiceFilterTable').checked,
      filter_list: $('#voiceFilterList').checked,
      tag_override: $('#voiceTagOverride').checked,
      fallback_text: $('#voiceFallback').checked,
      scenes,
    },
  };
}

$('#btnVoiceSave')?.addEventListener('click', async () => {
  if (!S.bridge) return toast('后端未就绪');
  const btn = $('#btnVoiceSave');
  btn.disabled = true;
  setText('#voiceSaveHint', '正在保存…');
  try {
    const r = JSON.parse(await S.bridge.save_voice_config(
      JSON.stringify(voicePayload())));
    if (!r.ok) {
      setText('#voiceSaveHint', r.error || '保存失败');
      toast('保存失败');
      return;
    }
    setText('#voiceSaveHint', '已保存');
    if (typeof markClean === 'function') markClean('voice');
    toast('声音设置已保存');
    await loadVoice();
  } catch (e) {
    setText('#voiceSaveHint', '保存出错：' + (e.message || e));
  } finally {
    btn.disabled = false;
  }
});

$('#btnVoiceTest')?.addEventListener('click', async () => {
  if (!S.bridge) return toast('后端未就绪');
  const btn = $('#btnVoiceTest');
  const out = $('#voiceTestResult');
  btn.disabled = true;
  out.className = 'test-result';
  out.textContent = '正在连接…（只查模型列表，不合成）';
  try {
    const r = JSON.parse(await S.bridge.test_voice(
      JSON.stringify(voicePayload())));
    if (!r.ticket) {
      out.className = 'test-result err';
      out.textContent = r.text || r.error || '启动测试失败';
      return;
    }
    for (let i = 0; i < 25; i++) {
      await new Promise((res) => setTimeout(res, 1000));
      const p = JSON.parse(await S.bridge.poll_test(r.ticket));
      if (p.done) {
        out.className = 'test-result ' + (p.ok ? 'ok' : 'err');
        out.textContent = p.text || '';
        toast(p.ok ? '声音模型连接正常' : '声音模型连接失败');
        return;
      }
      out.textContent = `正在连接…（${i + 1}s）`;
    }
    out.className = 'test-result err';
    out.textContent = '等太久了，检查地址/代理/Key';
  } catch (e) {
    out.className = 'test-result err';
    out.textContent = '测试出错：' + (e.message || e);
  } finally {
    btn.disabled = false;
  }
});

$('#btnVoiceClearCache')?.addEventListener('click', async () => {
  if (!S.bridge) return toast('后端未就绪');
  if (!window.confirm('清空声音缓存？下次同一句会重新合成（会花积分）。')) return;
  const r = JSON.parse(await S.bridge.clear_voice_cache());
  if (!r.ok) return toast(r.error || '清空失败');
  toast(`已删除 ${r.deleted || 0} 个文件`);
  await loadVoice();
});

$('#btnInstallVbCable')?.addEventListener('click', async () => {
  if (!S.bridge) return toast('后端未就绪');
  const btn = $('#btnInstallVbCable');
  btn.disabled = true;
  setText('#voiceDriverHint', '正在启动安装程序…');
  try {
    const r = JSON.parse(await S.bridge.install_vb_cable());
    if (!r.ok) {
      setText('#voiceDriverHint', r.error || '安装启动失败');
      btn.disabled = false;
      return;
    }
    if (r.already) {
      toast('VB-CABLE 已经安装');
      await refreshVoiceDevices();
      return;
    }
    if (r.started) {
      toast('已弹出 UAC，请允许安装');
      setText('#voiceDriverHint', '等待安装完成…装完可能需要重启电脑/微信。');
      for (let i = 0; i < 30; i++) {
        await new Promise((res) => setTimeout(res, 2000));
        await refreshVoiceDevices();
        const hint = $('#voiceDriverHint')?.textContent || '';
        if (hint.includes('已就绪')) return;
        setText('#voiceDriverHint', `等待驱动生效…（${(i + 1) * 2}s）`);
      }
      setText('#voiceDriverHint', '安装程序已启动。若设备仍未出现，请重启电脑后点「刷新声卡」。');
      btn.disabled = false;
      return;
    }
    toast(r.message || '安装完成');
    setText('#voiceDriverHint', r.message || '安装完成，请刷新声卡');
    await refreshVoiceDevices();
  } catch (e) {
    setText('#voiceDriverHint', '安装出错：' + (e.message || e));
    btn.disabled = false;
  }
});

$('#btnRefreshVoiceDevices')?.addEventListener('click', async () => {
  await refreshVoiceDevices();
  toast('已刷新声卡');
});

$('#btnOpenSoundSettings')?.addEventListener('click', async () => {
  if (!S.bridge) return;
  const r = JSON.parse(await S.bridge.open_voice_sound_settings());
  if (!r.ok) toast(r.error || '打不开声音设置');
});

/* ────────── 页内 tab 与策略卡 ────────── */

function showVoicePane(name) {
  document.querySelectorAll('#voiceTabs [data-voice-tab]').forEach((b) => {
    b.classList.toggle('is-active', b.dataset.voiceTab === name);
  });
  document.querySelectorAll('[data-voice-pane]').forEach((p) => {
    p.hidden = p.dataset.voicePane !== name;
  });
}

const VOICE_MODE_LABEL = {
  off: '关闭', request: '按需', auto: '自动判断',
  probability: '按概率', always: '每条都发', preview: '预演',
};

function syncVoiceModes() {
  const value = $('#voiceStrategy')?.value || 'preview';
  document.querySelectorAll('#voiceModeGrid .voice-mode').forEach((b) => {
    b.classList.toggle('is-active', b.dataset.mode === value);
  });
  setText('#voiceWhenTag', VOICE_MODE_LABEL[value] || value);
}

function syncVoiceSendTag() {
  const value = $('#voiceSendAs')?.value || 'file';
  const tag = $('#voiceSendTag');
  if (tag) tag.textContent = value === 'bubble' ? '语音气泡' : '音频文件';
}

$('#voiceTabs')?.addEventListener('click', (e) => {
  const tab = e.target.closest('[data-voice-tab]');
  if (tab) showVoicePane(tab.dataset.voiceTab);
});

$('#voiceModeGrid')?.addEventListener('click', (e) => {
  const card = e.target.closest('[data-mode]');
  if (!card) return;
  const sel = $('#voiceStrategy');
  if (sel) sel.value = card.dataset.mode;
  syncVoiceModes();
});
$('#voiceStrategy')?.addEventListener('change', syncVoiceModes);
$('#voiceSendAs')?.addEventListener('change', syncVoiceSendTag);
$('#btnVoiceTestTop')?.addEventListener('click', () => $('#btnVoiceTest')?.click());
