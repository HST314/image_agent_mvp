/* 五图画廊（T33）：稳定五槽位、object-fit:contain 不裁切、缩略图/懒加载/缩放预览、
 * 槽位错误与补偿动作；补偿期间成功槽位的 DOM 与图片不被替换（无闪烁）。 */

import { el, icons } from './dom.js';
import { assetUrl } from './api.js';
import { mechanismLabel, candidateLabel } from './copy.js';

export const SLOT_COUNT = 5;

/**
 * 纯函数：由后端候选与风格选择构建五个稳定槽位。
 * candidates 项：{id, candidate_index, uri, sha256, style_id, style_name, ...}
 * styleSelections 项：{style_id, mechanism, reason, task_fit, risk}
 * 缺失的槽位标记为 missing，由后端幂等补偿生成（T08）。
 */
export function buildSlots(candidates = [], styleSelections = []) {
  const byIndex = new Map();
  for (const item of candidates || []) {
    const index = Number.isInteger(item?.candidate_index) ? item.candidate_index : null;
    if (index !== null && index >= 0 && index < SLOT_COUNT) byIndex.set(index, item);
  }
  // 兼容旧检查点没有 candidate_index 的情况：按 id "candidate-N" 推断。
  for (const item of candidates || []) {
    if (item?.candidate_index === undefined || item?.candidate_index === null) {
      const match = /^candidate-(\d+)$/.exec(String(item?.id || ''));
      if (match) {
        const index = Number(match[1]) - 1;
        if (index >= 0 && index < SLOT_COUNT && !byIndex.has(index)) byIndex.set(index, item);
      }
    }
  }
  const stylesById = new Map((styleSelections || []).map((s) => [s.style_id, s]));
  const slots = [];
  for (let index = 0; index < SLOT_COUNT; index += 1) {
    const asset = byIndex.get(index) || null;
    const style = asset ? (stylesById.get(asset.style_id) || null) : (styleSelections?.[index] || null);
    slots.push({
      index,
      status: asset ? 'ready' : 'missing',
      key: asset ? (asset.sha256 || asset.artifact_id || asset.id || `slot-${index}`) : `missing-${index}`,
      asset,
      style,
      // T11：缺 style_name 时不再兜底展示英文 style_id。
      styleName: asset?.style_name || null,
    });
  }
  return slots;
}

function slotFrame(slot, projectId) {
  const frame = el('div', { class: 'slot__frame' });
  if (slot.status === 'ready') {
    const url = assetUrl(projectId, slot.asset);
    if (url) {
      const img = el('img', {
        src: url, loading: 'lazy', decoding: 'async',
        alt: `候选方向 ${slot.index + 1}${slot.styleName ? `（${slot.styleName}）` : ''}`,
      });
      if (/^https?:/.test(url)) img.setAttribute('referrerpolicy', 'no-referrer');
      frame.append(img);
    } else {
      frame.append(el('span', { 'aria-hidden': 'true' }));
      frame.lastChild.innerHTML = icons.image;
    }
  } else {
    frame.append(el('span', { 'aria-hidden': 'true' }));
    frame.lastChild.innerHTML = icons.info;
    frame.append(el('span', { class: 'slot__status slot__status--error', text: '待补齐' }));
  }
  return frame;
}

function slotBody(slot, { selectedId, onSelect, onZoom }) {
  const body = el('div', { class: 'slot__body' });
  body.append(el('span', { class: 'slot__title', text: `方向 ${slot.index + 1}${slot.styleName ? ` · ${slot.styleName}` : ''}` }));
  if (slot.style?.mechanism) {
    // T11："dimension: value" 的英文维度前缀映射为中文（构图/材质/光影…）。
    const mechanism = mechanismLabel(slot.style.mechanism);
    body.append(el('span', { class: 'slot__meta', text: mechanism, title: mechanism }));
  }
  const actions = el('div', { class: 'slot__actions' });
  if (slot.status === 'ready') {
    const selected = selectedId === (slot.asset.id || `candidate-${slot.index + 1}`);
    const select = el('button', {
      type: 'button', class: 'btn btn--secondary', text: selected ? '已选为主图' : '选为主图',
      'aria-pressed': String(selected), dataset: { selectCandidate: 'true' },
    });
    select.addEventListener('click', () => onSelect?.(slot));
    const zoom = el('button', { type: 'button', class: 'btn btn--secondary', text: '放大' });
    zoom.addEventListener('click', () => onZoom?.(slot));
    actions.append(select, zoom);
  } else {
    actions.append(el('span', { class: 'slot__missing-note', text: '本轮生成未返回此方向' }));
  }
  body.append(actions);
  return body;
}

/**
 * 键控更新：key 未变的槽位复用既有 DOM（成功槽在补偿期间不闪烁、不被替换）。
 */
export function syncSlots(container, slots, options) {
  const existing = new Map([...container.children].map((node) => [node.dataset.key, node]));
  const nextKeys = new Set();
  for (const slot of slots) {
    nextKeys.add(slot.key);
    let node = existing.get(slot.key);
    const selectedId = options.selectedId;
    const pressed = slot.status === 'ready' && selectedId === (slot.asset.id || `candidate-${slot.index + 1}`);
    if (!node) {
      node = el('div', { class: 'slot', role: 'option', 'aria-label': `候选方向 ${slot.index + 1}` });
      node.dataset.key = slot.key;
      node.dataset.candidateId = slot.asset?.id || '';
      node.append(slotFrame(slot, options.projectId), slotBody(slot, options));
      container.append(node);
    } else {
      // 复用节点：仅刷新选择态与动作（不重载 <img>）。
      const body = node.querySelector('.slot__body');
      if (body) body.replaceWith(slotBody(slot, options));
    }
    node.classList.toggle('is-selected', Boolean(pressed));
    node.setAttribute('aria-selected', String(Boolean(pressed)));
  }
  for (const node of [...container.children]) {
    if (!nextKeys.has(node.dataset.key)) node.remove();
  }
}

/** 缩放预览对话框：适配缩放 ↔ 原始尺寸切换。 */
export function openZoomDialog(slot, projectId) {
  const url = assetUrl(projectId, slot.asset);
  if (!url) return;
  const dialog = el('dialog', { class: 'dialog zoom-dialog', 'aria-label': `候选方向 ${slot.index + 1} 预览` });
  const head = el('div', { class: 'dialog__head' });
  head.append(el('h2', { text: `方向 ${slot.index + 1}${slot.styleName ? ` · ${slot.styleName}` : ''}` }));
  const close = el('button', { type: 'button', class: 'icon-btn', 'aria-label': '关闭预览', text: '✕' });
  head.append(close);
  const body = el('div', { class: 'dialog__body' });
  const img = el('img', { src: url, alt: `候选方向 ${slot.index + 1} 大图` });
  if (/^https?:/.test(url)) img.setAttribute('referrerpolicy', 'no-referrer');
  body.append(img);
  const foot = el('div', { class: 'dialog__foot' });
  const sizeInfo = el('span', { class: 'sr-only' });
  const toggle = el('button', { type: 'button', class: 'btn btn--secondary', text: '查看原始尺寸' });
  toggle.addEventListener('click', () => {
    const natural = dialog.classList.toggle('is-natural');
    toggle.textContent = natural ? '适配窗口' : '查看原始尺寸';
  });
  img.addEventListener('load', () => {
    sizeInfo.textContent = `原始尺寸 ${img.naturalWidth}×${img.naturalHeight}`;
    toggle.textContent = `查看原始尺寸（${img.naturalWidth}×${img.naturalHeight}）`;
  });
  foot.append(sizeInfo, toggle);
  dialog.append(head, body, foot);
  close.addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
}

/** 画廊舞台：五槽 + 选择确认栏。 */
export function renderGalleryStage(container, view, { projectId, selectedId, onSelect, onCompensate }) {
  const snapshot = view.snapshot || {};
  const slots = buildSlots(snapshot.candidates, snapshot.style_selections);
  const head = el('div', { class: 'section__head' });
  const headText = el('div');
  headText.append(el('h2', { text: '选择一张当前主图' }), el('p', { text: '五个方向共享任务书与硬约束，仅艺术机制不同；缺失槽位可单独补齐。' }));
  head.append(headText, el('span', { class: 'badge', text: `${slots.filter((s) => s.status === 'ready').length}/${SLOT_COUNT} 已生成` }));
  const grid = el('div', { class: 'gallery-grid', role: 'listbox', 'aria-label': '五个候选方向' });
  let currentSelectedId = selectedId;
  const selectSlot = (slot) => {
    currentSelectedId = slot.asset.id || `candidate-${slot.index + 1}`;
    for (const node of grid.children) {
      const pressed = node.dataset.key === slot.key;
      node.classList.toggle('is-selected', pressed);
      node.setAttribute('aria-selected', String(pressed));
      const button = node.querySelector('[data-select-candidate]');
      if (button) {
        button.setAttribute('aria-pressed', String(pressed));
        button.textContent = pressed ? '已选为主图' : '选为主图';
      }
    }
    note.textContent = `已选择：${candidateLabel(currentSelectedId)}`;
    onSelect?.(slot);
  };
  syncSlots(grid, slots, {
    projectId, selectedId: currentSelectedId,
    onSelect: selectSlot,
    onZoom: (slot) => openZoomDialog(slot, projectId),
  });
  const bar = el('div', { class: 'gallery-select-bar' });
  const confirm = el('button', { type: 'button', class: 'btn btn--primary', id: 'select-button', text: '确认当前主图', disabled: selectedId ? null : 'disabled' });
  const note = el('span', { class: 'badge badge--info', text: selectedId ? `已选择：${candidateLabel(selectedId)}` : '请先选择一个方向' });
  bar.append(confirm, note);
  const missingCount = slots.filter((slot) => slot.status === 'missing').length;
  if (missingCount) {
    const compensate = el('button', {
      type: 'button', class: 'btn btn--secondary',
      text: `补齐缺失方向（${missingCount}）`,
    });
    compensate.addEventListener('click', () => onCompensate?.());
    bar.append(compensate);
  }
  container.append(head, grid, bar);
  return { confirmButton: confirm, slots };
}
