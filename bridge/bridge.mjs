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

/**
 * ① 本地融合检索：交给 Python 的 `bigfish.memory.search_chunks()`。
 *
 * 为什么不在这里用 node:sqlite 自己查：排序（bm25→0..1）、MMR 去冗余、
 * 按 maxChars 填预算这三件事已经写在 memory.py 里，并有 selftest 钉住。
 * 在 JS 里再实现一遍就是第二套语义 —— 正是这次重构要消灭的东西。
 *
 * 调用方式：`python -m bigfish.memory --recall <db> <query> <maxChars>`，
 * stdout 一行 JSON，stderr 一律忽略（Python 那边的日志）。
 * 找不到 python / 超时 / 输出不是 JSON —— 一律返回 null，让调用方退回 ②。
 */
async function localRecall(root, dbPath, query, maxChars, warnings) {
  const py = process.env.BIGFISH_PYTHON ||
    (process.platform === 'win32' ? 'python' : 'python3');
  // ⚠ 必须显式要求 Python 用 UTF-8 写 stdout。
  //
  //   Node 这边按 utf-8 读（下面 spawnSync 的 encoding），而 Windows 上
  //   Python 默认按 cp936 写 —— 两边一对不上，记忆原文里每个汉字都会变成
  //   U+FFFD。`tools/start.bat` 设了 PYTHONUTF8=1 所以走 bat 时看不出来，
  //   但**双击 exe 启动时没有这个变量**，于是打包版的记忆检索全是乱码。
  //   不要再依赖调用方的环境：这里就是唯一的地基。
  const env = { ...process.env,
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
    PYTHONPATH: [join(HERE, '..'), process.env.PYTHONPATH || '']
    .filter(Boolean).join(process.platform === 'win32' ? ';' : ':') };
  try {
    const { spawnSync } = await import('node:child_process');
    const r = spawnSync(py, ['-m', 'bigfish.memory', '--recall', dbPath,
      '--query', query, '--maxChars', String(maxChars)],
      { encoding: 'utf-8', env, timeout: 15000, windowsHide: true });
    const raw = (r.stdout || '').trim();
    if (!raw) {
      if (r.error) warnings.push(`本地检索不可用: ${r.error.message}`);
      return null;
    }
    const d = JSON.parse(raw);
    if (!d || !d.ok || !d.text) return null;
    return { text: d.text, ids: d.ids || [], diagnostics: d.diagnostics || { mode: 'hybrid' } };
  } catch (e) {
    warnings.push(`本地检索失败: ${e.message}`);
    return null;
  }
}

/** ② 退路：import 原版 MemoryStore，拷库到临时目录再检索。 */
async function legacyRecall(root, scopeId, query, maxChars, dbPath, warnings) {
  const MemoryStore = await loadMemoryStore(root, scopeId);
  if (!MemoryStore) return null;
  let tmpDir = null;
  try {
    const { mkdirSync, copyFileSync, rmSync } = await import('node:fs');
    const baseTmp = process.env.BIGFISH_TMP || join(HERE, '..', '.tmp');
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
      const r = store.recall(query, { maxChars });
      return { text: r.text, ids: r.ids, diagnostics: r.diagnostics };
    } finally {
      store.db.close();
    }
  } catch (e) {
    warnings.push(`记忆检索失败: ${e.message}`);
    return null;
  } finally {
    if (tmpDir) {
      try {
        const { rmSync } = await import('node:fs');
        rmSync(tmpDir, { recursive: true, force: true });
      } catch { /* 清理失败不影响结果 */ }
    }
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
  // 而老布局的文件名用的是截短 id：soul-ws-83ba9d80-ec53.json
  // 所以不能拿目录名直接拼，要按目录里实际存在的文件名去匹配。
  const scopeId = scopeDir ? scopeDir.split(/[\\/]/).pop() : (scope || '');
  const shortId = scopeId.startsWith('ws-') ? scopeId.slice(0, 15) : scopeId;

  /* ⭐ 存储布局：新布局一个数据根 = 一份，文件直接住在 <root>/ 下：
   *      <root>/soul.json   <root>/mood.json   <root>/memory.sqlite3
   *    老布局是 <root>/<ws-xxx>/soul-ws-xx.json 那套。
   *    两个都要认 —— 用户的机器上有可能是其中任何一种。
   *
   *    优先新布局：那是本程序写入的地方，也是"现在生效"的那份。
   */
  const NEW_NAMES = { soul: 'soul.json', mood: 'mood.json', memory: 'memory.sqlite3' };

  function findDataFile(prefix, ext) {
    // ① 新布局
    const direct = join(root, NEW_NAMES[prefix] || `${prefix}.${ext}`);
    if (existsSync(direct)) return direct;

    // ② 老布局
    if (!scopeDir) return null;
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

  /* ⚠ 以前这里三个块都套着 `if (scopeDir)`。
     新布局根本没有 ws-* 目录，scopeDir 是 null —— 于是灵魂、心情、记忆
     全被跳过，静默返回空人格。findDataFile 里已经优先查新路径了，
     这个门禁却把它挡在门外。
     现在改成"能找到文件就处理"，布局判断交给 findDataFile。 */

  // ---------- soul ----------
  let soul = null;
  let persona = '';
  {
    const soulFile = findDataFile('soul', 'json');
    soul = soulFile ? readJsonSafe(soulFile) : null;
    if (soul) persona = renderPersona(soul);
    else warnings.push(`找不到灵魂文件 (root=${root}${scopeDir ? `, dir=${scopeDir}` : ''})`);
  }

  // ---------- mood ----------
  let mood = null;
  {
    const moodFile = findDataFile('mood', 'json');
    const moodJson = moodFile ? readJsonSafe(moodFile) : null;
    // 新布局的 mood.json 是按范围键存 moods 的；取 lastActive 或第一个
    mood = extractMood(moodJson, scopeId || (moodJson && moodJson.lastActive) || '');
    if (!mood) warnings.push('找不到可用情绪状态');
  }

  // ---------- memory ----------
  //
  // 检索的顺序（改过一次，旧的只有第 ② 条）：
  //   ① 本地融合检索：直接调 `bigfish.memory.search_chunks()`，一次 Python
  //      调用拿回已经排好序、去过冗余、按预算填满的结果。
  //   ② 原来那条 Node 侧 import 原版 MemoryStore 的路径，保留作退路 ——
  //      本地库还没建索引、或 Python 侧不可用时才会走到这里。
  //
  // 为什么要把检索搬到 Python：旧实现每次都把整个库**拷到临时目录**再打开
  // （因为 DSH 可能持有 WAL 写锁）。库到了 200MB 就是每轮 200MB 的读 IO。
  // 现在主进程用 mode=ro 只读打开，WAL 本来就允许多读者，不用拷。
  let memory = null;
  if (query) {
    const dbPath = findDataFile('memory', 'sqlite3');
    if (!dbPath) {
      warnings.push('找不到记忆库');
    } else {
      memory = await localRecall(root, dbPath, query, maxChars, warnings);
      if (!memory) {
        memory = await legacyRecall(root, scopeId, query, maxChars, dbPath, warnings);
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
