/* 应用状态容器 + 表单草稿持久化（T32：草稿在刷新/断线后可恢复）。 */

const listeners = new Set();

export const state = {
  projects: [],
  current: null,      // 当前工程视图（GET /api/projects/{id} 的响应）
  job: null,          // 进行中的后台 job（长任务期间页面仍可操作）
  offline: false,
};

export function subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export function emit() { for (const fn of listeners) fn(state); }
export function patch(partial) { Object.assign(state, partial); emit(); }

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
