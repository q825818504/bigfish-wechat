#!/usr/bin/env node
/**
 * [bigfish] 灵魂 / 情绪 / 记忆 桥接器。
 *
 * 为什么用 Node 写而不是在 Python 里重写：
 *   dsh-bigfish 的记忆检索有一整套原版逻辑——中文二元分词、半衰期衰减、
 *   session scope 过滤、FTS5 索引。这些用 Python 重写必然产生偏差，
 *   导致「微信里的大肥鱼」和「DSH 里的大肥鱼」记得不一样的事。
 *   这里直接 import 原版模块调用，逻辑零偏差。
 *
 * 用法（由 Python 调用）：
 *   node bridge.mjs context --root <dataRoot> --scope <ws-id> --query "文本"
 *   -> stdout 输出 JSON：{soul, mood, memory, system, context}
 *
 *    node bridge.mjs search --root <dataRoot> --scope <ws-id> --query "文本"
 *   -> 只返回检索到的记忆片段
 *
 * 设计原则：
 *   - **只读**。本脚本不写任何数据，DSH 那边同时在跑也安全。
 *   - 任何一步失败都不阻断其它步，缺什么就少什么，并在 warnings 里说明。
 *   - 输出到 stdout 的必须是纯 JSON，日志一律走 stderr。
 */
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));

function log(...args) {
  console.error('[bridge]', ...args);
}

function parseArgs(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith('--')) {
      const key = a.slice(2);
      const next = argv[i + 1];
      if (next && !next.startsWith('--')) {
        out[key] = next;
        i++;
      } else {
        out[key] = true;
      }
    } else {
      out._.push(a);
    }
  }
  return out;
}

function readJsonSafe(path) {
  try {
    return JSON.parse(readFileSync(path, 'utf-8'));
  } catch (e) {
    log(`读取失败 ${path}: ${e.message}`);
    return null;
  }
}

/** 找 dataRoot 下的 scope 目录 */
function findScope(root, scope) {
  if (scope) {
    const p = join(root, scope);
    if (existsSync(p)) return p;
    // 允许只传短 id
    const alt = join(root, `ws-${scope}`);
    if (existsSync(alt)) return alt;
  }
  // 没指定或找不到时，取**真正有灵魂文件**的那个 ws-* 目录。
  // 实测：dataRoot 下常有多个历史工作区，大多是空目录；
  // 只按 mtime 或字典序挑会挑到空的。
  try {
    const dirs = readdirSync(root)
      .filter((n) => n.startsWith('ws-') && !n.includes('.'))
      .map((n) => join(root, n))
      .filter((p) => existsSync(p));
    if (!dirs.length) return null;

    const withSoul = dirs.filter((d) => {
      try {
        return readdirSync(d).some((f) => f.startsWith('soul-') && f.endsWith('.json'));
      } catch { return false; }
    });
    const pool = withSoul.length ? withSoul : dirs;
    pool.sort((a, b) => {
      try { return statSync(b).mtimeMs - statSync(a).mtimeMs; } catch { return 0; }
    });
    return pool[0];
  } catch {
    return null;
  }
}

/** 从 soul JSON 渲染人格。
 *
 * ⚠️ 文案必须与 bigfish/vault.py 的 TRAIT_DOC 完全一致 ——
 * 那是插件 lib/soul/store.js 里 TRAIT_DOC 的 Python 镜像，也是唯一真源。
 *
 * 这里曾经抄错：mid 档写成「温暖度中：明显关心，会主动问候」，
 * 而插件的 mid 是「关心，但不黏人」。同一个数值，微信里说她是黏人的、
 * DSH 里说她不黏人。抄一份就会漂，所以改文案只改 vault.py，然后同步这里。
 */
const TRAIT_DOC = {
  warmth: { label: '温暖', low: '克制，不主动靠近', mid: '关心，但不黏人', high: '明显关心，会主动问候' },
  assertiveness: { label: '主见', low: '倾向附和，很少反驳', mid: '有想法，但不硬顶', high: '明确表达不同意见，会讲理由' },
  playfulness: { label: '顽皮', low: '认真，少开玩笑', mid: '该认真时认真，平时会贫两句', high: '日常爱逗，会起哄' },
  directness: { label: '直接', low: '含蓄，话留三分', mid: '不绕太远，但会顾着分寸', high: '有话直说，少铺垫' },
};

function renderPersona(soul) {
  if (!soul) return '';
  const ident = soul.identity || {};
  const parts = [];
  const name = (ident.name || '').trim();
  const addr = (ident.address || '').trim();
  const aliases = (ident.aliases || '').trim();
  let header = name ? `你是「${name}」` : '你是';
  header += addr ? `，称呼对方「${addr}」。` : '。';
  if (aliases) header += `对方平时也叫你「${aliases}」。`;
  parts.push(header);

  if ((ident.whoIAm || '').trim()) {
    parts.push('\n## 我是谁\n\n' + ident.whoIAm.trim());
  }
  const values = (soul.values || [])
    .map(v => (typeof v === 'string' ? v : v && v.text))
    .filter(Boolean);
  if (values.length) {
    parts.push('\n## 我珍视的\n\n' + values.map(v => `- ${v}`).join('\n'));
  }

  /* ⭐ 性格段**绝不输出数字**。
     数字对模型没有语义，它会自行脑补「65 算高吗」—— 结果同一份人格，
     微信里和 DSH 里表现不一样。这里与 bigfish/soul.py 的 trait_lines()
     以及插件 soul/index.js 的 renderSoulOf 保持同一个口径：只给那句话。
     分档边界也与插件一致：<=40 低 / >=60 高 / 其余中（不是 <40 和 >60）。 */
  const traits = soul.traits || {};
  const traitLines = [];
  for (const [key, doc] of Object.entries(TRAIT_DOC)) {
    const v = traits[key];
    if (typeof v !== 'number') continue;
    const band = v <= 40 ? 'low' : v >= 60 ? 'high' : 'mid';
    traitLines.push(`- ${doc.label}：${doc[band]}。`);
  }
  if (traitLines.length) {
    parts.push('\n## 我的性格\n\n' + traitLines.join('\n'));
  }
  const notes = (soul.selfNotes || []).filter(Boolean);
  if (notes.length) {
    parts.push('\n## 我自己知道的事\n\n' + notes.map(n => `- ${n}`).join('\n'));
  }
  return parts.join('\n').trim();
}

/** 从 mood JSON 提取当前状态 + 可注入文本 */
function extractMood(moodJson, scope) {
  if (!moodJson || !moodJson.moods) return null;
  const m = moodJson.moods[scope] ||
    Object.values(moodJson.moods)[0];
  if (!m) return null;
  return {
    mood: typeof m.mood === 'number' ? Math.round(m.mood * 10) / 10 : null,
    dims: m.dims || {},
    lastReason: m.lastReason || '',
    lastInjectedText: m.lastInjectedText || '',
    updatedAt: m.updatedAt || 0,
  };
}

async function loadMemoryStore(root, scope) {
  // dsh-bigfish 的 lib/ 路径；允许用 --bigfish 覆盖
  const candidates = [
    process.env.BIGFISH_HOME && join(process.env.BIGFISH_HOME, 'lib/memory/store.js'),
    join(HERE, '..', '..', 'dsh-bigfish', 'lib/memory/store.js'),
    join(HERE, '..', '..', '..', 'dsh-bigfish', 'lib/memory/store.js'),
  ].filter(Boolean);

  let storePath = null;
  for (const c of candidates) {
    const abs = resolve(c);
    if (existsSync(abs)) { storePath = abs; break; }
  }
  if (!storePath) {
    log('找不到 dsh-bigfish/lib/memory/store.js，跳过记忆检索');
    return null;
  }
  try {
    const mod = await import(pathToFileURL(storePath).href);
    return mod.MemoryStore;
  } catch (e) {
    log(`加载 MemoryStore 失败: ${e.message}`);
    return null;
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const cmd = args._[0] || 'context';
  const root = args.root;
  const scope = args.scope;
  const query = args.query || '';
  const maxChars = Number(args.maxChars || 2400);

  if (!root) {
    console.log(JSON.stringify({ ok: false, error: '--root 必填' }));
    return 1;
  }

  const warnings = [];
  const scopeDir = findScope(root, scope);
  // 目录名形如 ws-83ba9d80-ec53-4a24-9245-7ce877a74e70，
  // 而文件名用的是截短 id：soul-ws-83ba9d80-ec53.json
  // 所以不能拿目录名直接拼，要按目录里实际存在的文件名去匹配。
  const scopeId = scopeDir ? scopeDir.split(/[\\/]/).pop() : (scope || '');
  const shortId = scopeId.startsWith('ws-') ? scopeId.slice(0, 15) : scopeId;

  function findDataFile(prefix, ext) {
    if (!scopeDir) return null;
    // 先试截短 id（实际布局），再试全 id，最后扫目录
    for (const cand of [`${prefix}-${shortId}.${ext}`, `${prefix}-${scopeId}.${ext}`]) {
      const p = join(scopeDir, cand);
      if (existsSync(p)) return p;
    }
    try {
      const hit = readdirSync(scopeDir).find(
        (n) => n.startsWith(`${prefix}-`) && n.endsWith(`.${ext}`));
      return hit ? join(scopeDir, hit) : null;
    } catch { return null; }
  }

  // ---------- soul ----------
  let soul = null;
  let persona = '';
  if (scopeDir) {
    const soulFile = findDataFile('soul', 'json');
    soul = soulFile ? readJsonSafe(soulFile) : null;
    if (soul) persona = renderPersona(soul);
    else warnings.push(`找不到灵魂文件 (dir=${scopeDir})`);
  } else {
    warnings.push(`找不到 scope 目录: root=${root} scope=${scope}`);
  }

  // ---------- mood ----------
  let mood = null;
  if (scopeDir) {
    const moodFile = findDataFile('mood', 'json');
    const moodJson = moodFile ? readJsonSafe(moodFile) : null;
    mood = extractMood(moodJson, scopeId);
    if (!mood) warnings.push('找不到可用情绪状态');
  }

  // ---------- memory ----------
  let memory = null;
  if (query && scopeDir) {
    const MemoryStore = await loadMemoryStore(root, scopeId);
    if (MemoryStore) {
      const dbPath = findDataFile('memory', 'sqlite3');
      if (dbPath) {
        // DSH 那边可能正持有这个库（WAL 模式需要写权限）。
        // 复制一份到临时目录再检索：只读、不干扰、也不受锁影响。
        // 复制时连 -wal / -shm 一起带上，否则会丢最近未 checkpoint 的数据。
        let tmpDir = null;
        try {
          // 不用 os.tmpdir()：某些环境对系统临时目录有写限制，
          // 统一放项目内的 .tmp/ 下，路径可预测也好清理。
          const { mkdirSync, copyFileSync, rmSync } = await import('node:fs');
          const baseTmp = process.env.BIGFISH_TMP ||
            join(HERE, '..', '.tmp');
          mkdirSync(baseTmp, { recursive: true });
          tmpDir = join(baseTmp, `mem-${process.pid}-${Date.now()}`);
          mkdirSync(tmpDir, { recursive: true });
          const tmpDb = join(tmpDir, 'memory.sqlite3');
          copyFileSync(dbPath, tmpDb);
          for (const suffix of ['-wal', '-shm']) {
            if (existsSync(dbPath + suffix)) {
              try { copyFileSync(dbPath + suffix, tmpDb + suffix); } catch { /* 可选 */ }
            }
          }
          const store = new MemoryStore(tmpDb);
          try {
            // 不传 sessionIds：库里的 session 形如
            //   session-f1382628-...@1793030256761
            // 而 workspace scope 形如 ws-83ba9d80-ec53-...
            // 两者不是同一套命名，用它过滤会把所有记忆都筛掉。
            // 记忆库本身已按 workspace 分文件，不需要再按 session 收窄。
            const r = store.recall(query, { maxChars });
            memory = { text: r.text, ids: r.ids, diagnostics: r.diagnostics };
          } finally {
            store.db.close();
          }
        } catch (e) {
          warnings.push(`记忆检索失败: ${e.message}`);
        } finally {
          if (tmpDir) {
            try {
              const { rmSync } = await import('node:fs');
              rmSync(tmpDir, { recursive: true, force: true });
            } catch { /* 清理失败不影响结果 */ }
          }
        }
      } else {
        warnings.push(`找不到记忆库: ${dbPath}`);
      }
    }
  }

  // ---------- 组装 ----------
  if (cmd === 'search') {
    console.log(JSON.stringify({ ok: true, memory, warnings }, null, 2));
    return 0;
  }

  console.log(JSON.stringify({
    ok: Boolean(persona),
    scopeId,
    soul,
    mood,
    memory,
    persona,
    // 界面要用的摘要：不把整份 soul/mood 原始 JSON 塞给渲染进程，
    // 只给渲染需要的字段，减少 IPC 体积也避免泄露内部结构。
    summary: {
      name: (soul?.identity?.name || '').trim(),
      address: (soul?.identity?.address || '').trim(),
      revision: soul?.revision ?? 0,
      values: (soul?.values || [])
        .map((v) => (typeof v === 'string' ? v : v?.text))
        .filter(Boolean).length,
      selfNotes: (soul?.selfNotes || []).length,
      personaChars: persona.length,
      traits: soul?.traits || {},
      moodValue: mood?.mood ?? null,
      moodReason: mood?.lastReason || '',
      moodUpdatedAt: mood?.updatedAt ?? 0,
      memoryCount: (memory?.ids || []).length,
    },
    warnings,
  }));
  return 0;
}

main().then(code => process.exit(code)).catch(e => {
  console.log(JSON.stringify({ ok: false, error: String(e && e.message || e) }));
  process.exit(1);
});
