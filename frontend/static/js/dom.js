/* DOM 基础工具与安全渲染助手。所有用户/后端文本一律经 escapeHtml 或 textContent 进入页面。 */

export const $ = (s, r = document) => r.querySelector(s);
export const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const ESCAPE = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
export function escapeHtml(value = '') {
  return String(value).replace(/[&<>"']/g, (c) => ESCAPE[c]);
}
export const escapeAttr = escapeHtml;

/** 以 DOM API 建元素；attrs 中 text/html 之外的值一律经 setAttribute。 */
export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined) continue;
    if (key === 'text') node.textContent = value;
    else if (key === 'class') node.className = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, String(value));
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

export const icons = {
  image: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 15-5-5L5 20"/></svg>',
  info: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/></svg>',
  error: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="m9 9 6 6M15 9l-6 6"/></svg>',
  empty: '<svg viewBox="0 0 24 24"><path d="M4 7h16M4 12h16M4 17h10"/></svg>',
};

export function iconHtml(name) { return icons[name] || icons.info; }

export function toast(message, type = 'info') {
  const region = $('#toasts');
  if (!region) return;
  const item = el('div', {
    class: `toast ${type === 'error' ? 'toast--error' : ''}`,
    role: type === 'error' ? 'alert' : 'status',
    text: message,
  });
  region.append(item);
  setTimeout(() => item.remove(), 5000);
}

/** T35 统一加载/空态/错误态组件。 */
export function stateBlock(kind, title, detail = '', action = null) {
  const block = el('div', { class: `state-block ${kind === 'error' ? 'state-block--error' : ''}`, role: kind === 'error' ? 'alert' : 'status' });
  const iconName = kind === 'error' ? 'error' : kind === 'loading' ? 'info' : 'empty';
  block.append(el('div', { class: 'state-block__icon' }));
  block.firstChild.innerHTML = iconHtml(iconName);
  if (kind === 'loading') block.firstChild.append(el('span', { class: 'spinner', 'aria-hidden': 'true' }));
  block.append(el('h3', { text: title }));
  if (detail) block.append(el('p', { text: detail }));
  if (action) block.append(action);
  return block;
}

export function formatDate(value) {
  if (!value) return '—';
  try {
    return new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(value));
  } catch { return String(value); }
}

/** 统一的内容卡片（标题 + 副标题），各页面共用。 */
export function sectionPanel(title, subtitle = '') {
  const panel = el('section', { class: 'panel section ia-section' });
  const head = el('div', { class: 'section__head' });
  const text = el('div');
  text.append(el('h2', { text: title }));
  if (subtitle) text.append(el('p', { text: subtitle }));
  head.append(text);
  panel.append(head);
  return panel;
}
