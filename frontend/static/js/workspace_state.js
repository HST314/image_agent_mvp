/* 工作区导航状态（Q1-A）：按工程 + 分支 + 检查点保存纯 UI 状态。
 * 权威业务数据始终来自工程视图；这里只恢复滚动、展开项、未提交控件选择、
 * 当前焦点和打开的历史快照。sessionStorage 不可用时降级到内存。 */

const memory = new Map();

export function workspaceStateKey(view) {
  const project = view?.project_id;
  if (!project) return null;
  const manifest = view?.manifest || {};
  const branch = manifest.current_branch || 'main';
  const checkpoint = manifest.current_checkpoint?.checkpoint_id || 'empty';
  return `studio-workspace:${project}:${branch}:${checkpoint}`;
}

function storage() {
  try { if (typeof sessionStorage !== 'undefined') return sessionStorage; } catch { /* ignore */ }
  return {
    getItem: (key) => memory.get(key) || null,
    setItem: (key, value) => memory.set(key, String(value)),
    removeItem: (key) => memory.delete(key),
  };
}

function keyPart(value) {
  const text = String(value || '').trim();
  return text ? encodeURIComponent(text) : null;
}

/** 圈画草稿比通用工作区状态多一层 asset 隔离，避免同一检查点换图后串稿。 */
export function annotationDraftKey({ projectId, branch, checkpointId, assetId } = {}) {
  const parts = [projectId, branch, checkpointId, assetId].map(keyPart);
  if (parts.some((part) => !part)) return null;
  return `studio-annotation:${parts.join(':')}`;
}

export function saveAnnotationDraft(scope, value, target = storage()) {
  const key = annotationDraftKey(scope);
  if (!key) return false;
  const payload = JSON.stringify(value);
  memory.set(key, payload);
  try { target.setItem(key, payload); } catch { /* storage quota/privacy fallback */ }
  return true;
}

export function loadAnnotationDraft(scope, target = storage()) {
  const key = annotationDraftKey(scope);
  if (!key) return null;
  try {
    const raw = target.getItem(key) || memory.get(key);
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}

export function clearAnnotationDraft(scope, target = storage()) {
  const key = annotationDraftKey(scope);
  if (!key) return false;
  memory.delete(key);
  try { target.removeItem?.(key); } catch { /* storage quota/privacy fallback */ }
  return true;
}

export function saveWorkspaceState(view, value, target = storage()) {
  const key = workspaceStateKey(view);
  if (!key) return;
  const payload = JSON.stringify(value);
  memory.set(key, payload);
  try { target.setItem(key, payload); } catch { /* storage quota/privacy fallback */ }
}

export function loadWorkspaceState(view, target = storage()) {
  const key = workspaceStateKey(view);
  if (!key) return null;
  try {
    const raw = target.getItem(key) || memory.get(key);
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}

function controlIdentity(node, index) {
  return `${node.tagName}:${node.id || node.name || node.dataset?.restoreKey || ''}:${index}`;
}

function statefulControls(root) {
  return [...(root?.querySelectorAll?.('input, textarea, select') || [])]
    .filter((node) => node.type !== 'file' && node.type !== 'password');
}

export function captureWorkspaceState(root, view, doc = globalThis.document) {
  if (!root || !view) return null;
  const controlNodes = statefulControls(root);
  const controls = controlNodes.map((node, index) => ({
    key: controlIdentity(node, index),
    value: node.value,
    checked: Boolean(node.checked),
  }));
  const details = [...root.querySelectorAll('details')].map((node) => Boolean(node.open));
  const pressed = [...root.querySelectorAll('[aria-pressed]')].map((node) => node.getAttribute('aria-pressed'));
  const selected = [...root.querySelectorAll('[aria-selected]')].map((node) => node.getAttribute('aria-selected'));
  const scrollRegions = [...root.querySelectorAll('[data-restore-scroll], .stepper, .markdown, .taskbook__editor')]
    .map((node, index) => ({ index, left: node.scrollLeft || 0, top: node.scrollTop || 0 }));
  const active = doc?.activeElement;
  const focusedControl = controlNodes.findIndex((node) => node === active);
  const openDialog = doc?.querySelector?.('dialog.snapshot-dialog[open]');
  const value = {
    scrollY: Number(globalThis.scrollY || 0),
    controls, details, pressed, selected, scrollRegions,
    focusedControl,
    openSnapshot: openDialog?.dataset?.snapshotCheckpoint || null,
  };
  saveWorkspaceState(view, value);
  if (openDialog?.open) openDialog.close();
  return value;
}

export function restoreWorkspaceState(root, view, doc = globalThis.document) {
  const saved = loadWorkspaceState(view);
  if (!root || !saved) return false;
  const controls = statefulControls(root);
  controls.forEach((node, index) => {
    const item = saved.controls?.find((candidate) => candidate.key === controlIdentity(node, index));
    if (!item) return;
    if ('checked' in node) node.checked = Boolean(item.checked);
    if (typeof item.value === 'string') node.value = item.value;
    const EventType = doc?.defaultView?.Event || globalThis.Event;
    if (EventType) node.dispatchEvent?.(new EventType('change', { bubbles: true }));
  });
  [...root.querySelectorAll('details')].forEach((node, index) => { node.open = Boolean(saved.details?.[index]); });
  [...root.querySelectorAll('[aria-pressed]')].forEach((node, index) => {
    const desired = saved.pressed?.[index];
    if (desired != null && node.getAttribute('aria-pressed') !== desired && desired === 'true') node.click?.();
  });
  [...root.querySelectorAll('[aria-selected]')].forEach((node, index) => {
    const desired = saved.selected?.[index];
    if (desired != null && node.getAttribute('aria-selected') !== desired && desired === 'true') node.click?.();
  });
  [...root.querySelectorAll('[data-restore-scroll], .stepper, .markdown, .taskbook__editor')]
    .forEach((node, index) => {
      const position = saved.scrollRegions?.find((item) => item.index === index);
      if (position) { node.scrollLeft = position.left; node.scrollTop = position.top; }
    });
  if (saved.openSnapshot) {
    root.querySelector(`[data-snapshot-checkpoint="${globalThis.CSS?.escape ? CSS.escape(saved.openSnapshot) : saved.openSnapshot}"]`)?.click?.();
  } else if (Number.isInteger(saved.focusedControl) && saved.focusedControl >= 0) {
    controls[saved.focusedControl]?.focus?.({ preventScroll: true });
  }
  globalThis.scrollTo?.({ top: Number(saved.scrollY || 0), behavior: 'instant' });
  return true;
}
