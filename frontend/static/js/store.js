/* 应用状态容器 + 表单草稿持久化（T32：草稿在刷新/断线后可恢复）。 */

const listeners = new Set();

export const state = {
  projects: [],
  current: null,      // 当前工程视图（GET /api/projects/{id} 的响应）
  job: null,          // 进行中的后台 job（长任务期间页面仍可操作）
  offline: false,
  managedByHarness: false,
  managedProjectId: null,
  view: 'workspace',  // 顶部导航当前视图：workspace / status
};

export function subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export function emit() { for (const fn of listeners) fn(state); }
export function patch(partial) { Object.assign(state, partial); emit(); }

/* ---- 操作人身份（确认/审批/策略修订共用；localStorage 持久化） ---- */

export function getActor() { try { return localStorage.getItem('studio-actor') || ''; } catch { return ''; } }
export function setActor(value) { try { localStorage.setItem('studio-actor', value); } catch { /* ignore */ } }

/* ---- 草稿（可注入 storage 便于 Node 测试） ---- */

const memoryStorage = new Map();
function defaultStorage() {
  try { if (typeof localStorage !== 'undefined') return localStorage; } catch { /* 隐私模式 */ }
  return {
    getItem: (k) => (memoryStorage.has(k) ? memoryStorage.get(k) : null),
    setItem: (k, v) => memoryStorage.set(k, String(v)),
    removeItem: (k) => memoryStorage.delete(k),
  };
}

export function draftKey(projectId, name) { return `studio-draft:${projectId}:${name}`; }

export function saveDraft(projectId, name, value, storage = defaultStorage()) {
  if (!projectId) return;
  try { storage.setItem(draftKey(projectId, name), JSON.stringify({ value, savedAt: new Date().toISOString() })); } catch { /* 配额满时静默降级 */ }
}

export function loadDraft(projectId, name, storage = defaultStorage()) {
  if (!projectId) return null;
  try {
    const raw = storage.getItem(draftKey(projectId, name));
    if (!raw) return null;
    return JSON.parse(raw);
  } catch { return null; }
}

export function clearDraft(projectId, name, storage = defaultStorage()) {
  if (!projectId) return;
  try { storage.removeItem(draftKey(projectId, name)); } catch { /* ignore */ }
}

/* ---- 幂等键（M1：同一意图在同一检查点的重试复用同一键；提交成功后清除，
 * 检查点推进后指纹变化自动轮换，避免付费动作因响应丢失而重复执行）。 ---- */

function randomId() {
  const random = (globalThis.crypto?.randomUUID ? globalThis.crypto.randomUUID() : `${Date.now()}-${Math.random()}`);
  return String(random).replace(/[^a-zA-Z0-9-]/g, '');
}

/**
 * 取「工程 + 意图 + 指纹」对应的持久化幂等键：指纹不变则复用已存键（重试去重），
 * 指纹变化（输入或检查点不同）则生成并持久化新键。
 */
export function intentIdempotencyKey(projectId, intent, fingerprint = '', storage = defaultStorage()) {
  const name = `idem:${intent}`;
  const existing = loadDraft(projectId, name, storage);
  if (existing && existing.value?.fingerprint === fingerprint && typeof existing.value?.key === 'string') {
    return existing.value.key;
  }
  const key = `${intent}-${randomId()}`.slice(0, 128);
  saveDraft(projectId, name, { key, fingerprint }, storage);
  return key;
}

/** 提交成功后清除对应意图的幂等键，使下一次新意图使用新键。 */
export function clearIntentIdempotencyKey(projectId, intent, storage = defaultStorage()) {
  clearDraft(projectId, `idem:${intent}`, storage);
}
