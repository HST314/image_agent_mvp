/* 设置模块（T35 模块边界；T31 的 Schema 驱动设置页由后端交付后接管数据源）。
 * 当前消费工程视图中的 runtime_policy 与既有修订契约（修订必建新分支）。 */

import { el, toast } from './dom.js';
import { revisePolicy } from './api.js';

export function renderSettings(container, view, { projectId, onChanged }) {
  const tpl = document.getElementById('tpl-policy-section');
  const section = tpl.content.firstElementChild.cloneNode(true);
  const values = section.querySelector('.policy-section__values');
  const policy = view.runtime_policy || {};
  for (const [key, value] of Object.entries(policy)) {
    values.append(el('dt', { text: key }), el('dd', { text: typeof value === 'object' ? JSON.stringify(value) : String(value) }));
  }
  section.querySelector('[data-revise-policy]').addEventListener('click', () => openRevisionDialog(view, { projectId, onChanged }));
  container.append(section);
}

function openRevisionDialog(view, { projectId, onChanged }) {
  const dialog = el('dialog', { class: 'dialog', 'aria-labelledby': 'policy-dialog-title' });
  const head = el('div', { class: 'dialog__head' });
  head.append(el('h2', { id: 'policy-dialog-title', text: '修订运行策略' }));
  const close = el('button', { type: 'button', class: 'icon-btn', 'aria-label': '关闭对话框', text: '✕' });
  head.append(close);

  const body = el('div', { class: 'dialog__body' });
  body.append(el('p', { class: 'hint', text: '修订会创建新分支，仅影响该分支的后续运行；旧分支与历史保持不变。' }));
  const editor = el('textarea', { class: 'input', 'aria-label': '策略 JSON', style: 'min-height:260px' });
  editor.value = JSON.stringify(view.runtime_policy || {}, null, 2);
  const error = el('div', { class: 'field-error', role: 'alert' });
  const actorField = el('div', { class: 'field' });
  const actorInput = el('input', { class: 'input', id: 'policy-actor', required: 'required', placeholder: '例如：zhangsan' });
  actorField.append(el('label', { for: 'policy-actor', text: '确认人身份' }), actorInput);
  body.append(editor, error, actorField);

  const foot = el('div', { class: 'dialog__foot' });
  const cancel = el('button', { type: 'button', class: 'btn btn--secondary', text: '取消' });
  const confirm = el('button', { type: 'button', class: 'btn btn--primary', text: '确认并创建修订分支' });
  foot.append(cancel, confirm);
  dialog.append(head, body, foot);

  close.addEventListener('click', () => dialog.close());
  cancel.addEventListener('click', () => dialog.close());
  confirm.addEventListener('click', async () => {
    error.textContent = '';
    let policy;
    try { policy = JSON.parse(editor.value); } catch { error.textContent = '策略 JSON 无效。'; return; }
    const actor = actorInput.value.trim();
    if (!actor) { error.textContent = '请填写确认人身份。'; actorInput.focus(); return; }
    confirm.disabled = true;
    try {
      const result = await revisePolicy(projectId, { policy, actor, confirmed: true });
      toast(`已创建分支 ${result.branch}`);
      dialog.close();
      onChanged?.(result.project);
    } catch (err) {
      error.textContent = err.message;
      confirm.disabled = false;
    }
  });
  dialog.addEventListener('close', () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
  editor.focus();
}
