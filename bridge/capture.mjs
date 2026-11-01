#!/usr/bin/env node
/**
 * [bigfish] 记忆写回 —— 把微信对话记进 dsh-bigfish 的记忆库。
 *
 * 为什么必须走原版 capture()：
 *   记忆写入不是简单 INSERT。它要在同一个事务里完成三件事：
 *     1. 写 messages 表（原文，无损）
 *     2. 按 640/800 字符切 chunks（重叠窗口，便于检索时定位上下文）
 *     3. 用原版 terms() 分词写 FTS5 索引
 *   自己写 JSON 或另建表，DSH 那边就检索不到——那不是「记住」，是「存着」。
 *
 * 为什么路径走环境变量而不是命令行参数：
 *   中文路径（D:\大肥鱼\...）经由 subprocess 的 argv 传递时会被编码破坏，
 *   Node 收到的是错误路径，于是新建了一个空库，再对空库执行
 *   `PRAGMA journal_mode=WAL` 就报 "attempt to write a readonly database"——
 *   症状完全不像它的真实原因，排查成本极高。
 *   改用 UTF-16 的进程环境块传路径，不受任何命令行编码影响。
 *
 * 输入：
 *   BF_ROOT   dataRoot（必填）
 *   BF_SCOPE  workspace scope（可选）
 *   BF_JSON   输入 JSON 文件路径（必填），内容是记录数组
 *   BF_DB     直接指定库文件（可选，优先于 BF_ROOT/BF_SCOPE）
 *
 * 输出（stdout，单行 JSON）：
 *   {"ok":true,"written":N,"skipped":N,"errors":[...],"dbPath":"..."}
 */
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));

function findScopeDir(root, scope) {
  if (scope) {
    for (const c of [join(root, scope), join(root, `ws-${scope}`)]) {
      if (existsSync(c)) return c;
    }
  }
  try {
    const dirs = readdirSync(root)
      .filter((n) => n.startsWith('ws-') && !n.includes('.'))
      .map((n) => join(root, n))
      .filter((p) => existsSync(p));
    dirs.sort((a, b) => {
      try { return statSync(b).mtimeMs - statSync(a).mtimeMs; } catch { return 0; }
    });
    return dirs[0] || null;
  } catch {
    return null;
  }
}

function findFile(dir, prefix, ext) {
  if (!dir) return null;
  try {
    const hit = readdirSync(dir).find(
      (n) => n.startsWith(`${prefix}-`) && n.endsWith(`.${ext}`));
    return hit ? join(dir, hit) : null;
  } catch { return null; }
}

async function loadMemoryStore() {
  const cands = [
    process.env.BIGFISH_HOME && join(process.env.BIGFISH_HOME, 'lib', 'memory', 'store.js'),
    join(HERE, '..', '..', 'dsh-bigfish', 'lib', 'memory', 'store.js'),
    join(HERE, '..', '..', '..', 'dsh-bigfish', 'lib', 'memory', 'store.js'),
    join(HERE, '..', '..', 'dsh-bigfish', 'lib', 'memory', 'store.js'),
  ].filter(Boolean);
  for (const c of cands) {
    const abs = resolve(c);
    if (existsSync(abs)) {
      const mod = await import(pathToFileURL(abs).href);
      if (mod.MemoryStore) return mod.MemoryStore;
    }
  }
  return null;
}

function out(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

async function main() {
  const root = process.env.BF_ROOT || '';
  const scope = process.env.BF_SCOPE || '';
  const jsonPath = process.env.BF_JSON || '';
  const explicitDb = process.env.BF_DB || '';

  if (!jsonPath) {
    out({ ok: false, error: 'BF_JSON 未设置' });
    return 1;
  }

  // ---- 定位数据库 ----
  let dbPath = explicitDb;
  if (!dbPath) {
    if (!root) {
      out({ ok: false, error: 'BF_ROOT 未设置' });
      return 1;
    }
    const scopeDir = findScopeDir(root, scope);
    if (!scopeDir) {
      out({ ok: false, error: `找不到 scope 目录 (root=${root} scope=${scope})` });
      return 1;
    }
    dbPath = findFile(scopeDir, 'memory', 'sqlite3');
    if (!dbPath) {
      out({ ok: false, error: `找不到记忆库 (dir=${scopeDir})` });
      return 1;
    }
  }
  if (!existsSync(dbPath)) {
    out({ ok: false, error: `记忆库不存在: ${dbPath}` });
    return 1;
  }

  // ---- 读输入 ----
  let items;
  try {
    items = JSON.parse(readFileSync(jsonPath, 'utf-8'));
  } catch (e) {
    out({ ok: false, error: `读取输入失败: ${e.message}` });
    return 1;
  }
  if (!Array.isArray(items)) {
    out({ ok: false, error: '输入必须是数组' });
    return 1;
  }

  const MemoryStore = await loadMemoryStore();
  if (!MemoryStore) {
    out({ ok: false, error: '找不到 MemoryStore（确认 dsh-bigfish 已安装或设 BIGFISH_HOME）' });
    return 1;
  }

  // ---- 写入 ----
  const result = { ok: true, written: 0, skipped: 0, errors: [], dbPath };
  let store = null;
  try {
    store = new MemoryStore(dbPath);
    for (const it of items) {
      try {
        const id = store.capture({
          session: String(it.session || ''),
          eventKey: String(it.eventKey || ''),
          role: it.role === 'assistant' ? 'assistant' : 'user',
          content: String(it.text ?? ''),
          sourceTime: Number.isSafeInteger(it.sourceTime) ? it.sourceTime : null,
          interrupted: false,
        });
        if (id) result.written += 1;
        else result.skipped += 1;
      } catch (e) {
        result.errors.push(`${it.eventKey}: ${e.message}`);
      }
    }
  } catch (e) {
    result.ok = false;
    result.errors.push(`打开记忆库失败: ${e.message} (db=${dbPath})`);
  } finally {
    try { store?.db?.close(); } catch { /* ignore */ }
  }

  out(result);
  return result.ok ? 0 : 1;
}

main().then((c) => process.exit(c)).catch((e) => {
  out({ ok: false, error: String((e && e.message) || e) });
  process.exit(1);
});
