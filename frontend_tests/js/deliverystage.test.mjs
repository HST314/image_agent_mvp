/* 交付页「完成」按钮 DOM 行为（审计验收：重进页面 PUBLISHED 禁用态不得有可点击窗口）。
 * 受管 + 已私有落盘 + 有 bundle_id 时，首帧必须是禁用的「确认中…」，
 * 待 delivery.status 返回后才落地为「已完成」（PUBLISHED）或恢复可重试。 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

/* ---- 最小 DOM 假身：只实现 renderDeliveryStage 链路用到的 API ---- */

class FakeTextNode {
  constructor(text) { this.nodeType = 3; this.textContent = String(text); }
}

class FakeClassList {
  constructor(owner) { this.owner = owner; }
  _set() { return new Set(this.owner.className.split(/\s+/).filter(Boolean)); }
  _write(set) { this.owner.className = [...set].join(' '); }
  add(...names) { const s = this._set(); names.forEach((n) => s.add(n)); this._write(s); }
  remove(...names) { const s = this._set(); names.forEach((n) => s.delete(n)); this._write(s); }
  contains(name) { return this._set().has(name); }
}

class FakeElement {
  constructor(tag) {
    this.nodeType = 1;
    this.tagName = tag.toUpperCase();
    this.className = '';
    this.attributes = {};
    this.children = [];
    this.listeners = {};
    this.dataset = {};
    this.disabled = false;
    this._text = '';
    this.classList = new FakeClassList(this);
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() {
    if (this.children.length) return this.children.map((c) => c.textContent).join('');
    return this._text;
  }
  setAttribute(key, value) {
    this.attributes[key] = String(value);
    if (key === 'disabled') this.disabled = true; // 与真实 DOM 的属性→特性反射一致
  }
  getAttribute(key) { return this.attributes[key] ?? null; }
  removeAttribute(key) {
    delete this.attributes[key];
    if (key === 'disabled') this.disabled = false;
  }
  append(...nodes) { for (const n of nodes) this.children.push(n); }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  removeEventListener(name, fn) {
    this.listeners[name] = (this.listeners[name] || []).filter((f) => f !== fn);
  }
  async click() { for (const fn of this.listeners.click || []) await fn({}); }
}

function findByClass(node, cls, out = []) {
  for (const child of node.children || []) {
    if (child.nodeType === 1 && (child.className || '').split(/\s+/).includes(cls)) out.push(child);
    findByClass(child, cls, out);
  }
  return out;
}

const documentFake = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => new FakeTextNode(text),
  querySelector: () => null, // toast 的 #toasts 查找：无区域时静默返回
};

globalThis.document = documentFake;

const { renderDeliveryStage } = await import('../../frontend/static/js/project.js');

/* ---- 用例 ---- */

const managedView = (overrides = {}) => ({
  snapshot: {
    final_asset: { uri: 'artifact://asset_1', sha256: 'sha-final' },
    delivery_envelope: { design_note_markdown: '设计说明' },
  },
  delivery_status: { finalized: true, asset_sha256: 'sha-final', bundle_id: 'bundle-1' },
  ...overrides,
});

function renderCompleteControls(view, opts) {
  const panel = new FakeElement('section');
  renderDeliveryStage(panel, view, { projectId: 'proj-1', ...opts });
  const [wrap] = findByClass(panel, 'delivery-complete');
  assert.ok(wrap, '应渲染交付完成操作区');
  const [status] = findByClass(wrap, 'delivery-complete__status');
  const [button] = findByClass(wrap, 'btn');
  assert.ok(status && button, '应渲染状态行与完成按钮');
  return { status, button };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

test('受管已落盘：首帧禁用「确认中…」，PUBLISHED 后保持禁用显示已完成', async () => {
  let resolveStatus;
  const managedDeliveryStatus = () => new Promise((resolve) => { resolveStatus = resolve; });
  const { status, button } = renderCompleteControls(managedView(), {
    completeManagedDelivery: async () => {},
    managedDeliveryStatus,
  });
  // 关键断言：异步查询尚未返回时按钮已禁用，无可点击窗口。
  assert.equal(button.disabled, true);
  assert.equal(button.textContent, '确认中…');
  assert.equal(status.textContent, '正在确认主系统交付状态…');
  resolveStatus({ status: 'PUBLISHED' });
  await flush();
  assert.equal(button.disabled, true);
  assert.equal(button.textContent, '已完成');
  assert.equal(status.textContent, '最终图片与设计说明已保存到任务共享文件夹。');
  assert.ok(status.classList.contains('is-complete'));
});

test('受管已落盘：查询返回非 PUBLISHED 时恢复可重试', async () => {
  const managedDeliveryStatus = async () => ({ status: 'PENDING' });
  const { status, button } = renderCompleteControls(managedView(), {
    completeManagedDelivery: async () => {},
    managedDeliveryStatus,
  });
  assert.equal(button.disabled, true);
  await flush();
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, '完成');
  assert.equal(status.textContent, '图片与设计说明已在 Image Agent 交付目录；点击完成后复制到任务共享文件夹。');
  assert.equal(status.classList.contains('is-complete'), false);
});

test('受管已落盘：发布态查询失败时恢复可重试', async () => {
  const managedDeliveryStatus = async () => { throw new Error('bridge down'); };
  const { status, button } = renderCompleteControls(managedView(), {
    completeManagedDelivery: async () => {},
    managedDeliveryStatus,
  });
  assert.equal(button.disabled, true);
  await flush();
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, '完成');
  assert.equal(status.textContent, '交付状态确认失败，可点击完成后重试。');
});

test('非受管已落盘：直接呈现已完成，不发起发布态查询', async () => {
  let queried = false;
  const { status, button } = renderCompleteControls(managedView(), {
    completeManagedDelivery: null,
    managedDeliveryStatus: async () => { queried = true; return { status: 'PUBLISHED' }; },
  });
  await flush();
  assert.equal(button.disabled, true);
  assert.equal(button.textContent, '已完成');
  assert.ok(status.classList.contains('is-complete'));
  assert.equal(queried, false);
});

test('未落盘：按钮可点且不发起发布态查询', async () => {
  let queried = false;
  const view = managedView({ delivery_status: { finalized: false } });
  const { status, button } = renderCompleteControls(view, {
    completeManagedDelivery: async () => {},
    managedDeliveryStatus: async () => { queried = true; return { status: 'PUBLISHED' }; },
  });
  await flush();
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, '完成');
  assert.equal(status.textContent, '点击完成，将最终图片和设计说明保存到工程交付目录。');
  assert.equal(queried, false);
});
