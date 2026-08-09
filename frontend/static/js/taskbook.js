/* 任务书模块（T32）：Markdown 安全预览/编辑、确认门禁、编辑后确认失效立即可见、
 * 草稿自动保存与恢复。确认动作经 advance 携带 task_approved + actor（一期手工入口）。 */

import { el, toast } from './dom.js';
import { renderMarkdownInto } from './markdown.js';
import { approvalValid } from './states.js';
import { saveDraft, loadDraft, clearDraft } from './store.js';
import { advance } from './api.js';

const DRAFT_NAME = 'taskbook-markdown';

export function renderTaskbook(container, view, { projectId, actor, onChanged }) {
  const snapshot = view.snapshot || {};
  const markdown = snapshot.task_markdown || '';
  const valid = approvalValid(snapshot);
  const approval = snapshot.task_approval;

  const head = el('div', { class: 'section__head' });
  const headText = el('div');
  headText.append(el('h3', { text: '创作任务书' }), el('p', { text: '由生产后端生成的可审计 Markdown；任何修改都会使既有确认失效。' }));
  const status = valid
    ? el('span', { class: 'badge badge--success', text: `已确认 · ${approval?.actor || ''}` })
    : el('span', { class: 'badge badge--warning', text: approval ? '确认已失效' : '待确认' });
  head.append(headText, status);

  const preview = el('div', { class: 'markdown-body' });
  renderMarkdownInto(preview, markdown);

  /* 编辑区：草稿随输入自动保存，刷新后可恢复 */
  const editor = el('textarea', { class: 'input', id: 'taskbook-editor', 'aria-label': '编辑任务书 Markdown', style: 'display:none;min-height:220px' });
  editor.value = markdown;
  const draft = loadDraft(projectId, DRAFT_NAME);
  const draftBadge = el('span', { class: 'badge badge--info draft-badge', text: '草稿已恢复', style: 'display:none' });
  if (draft && typeof draft.value === 'string' && draft.value !== markdown) {
    editor.value = draft.value;
  }
  const editError = el('div', { class: 'field-error', role: 'alert' });

  const editBtn = el('button', { type: 'button', class: 'btn btn--secondary', text: '编辑任务书' });
  const saveBtn = el('button', { type: 'button', class: 'btn btn--primary', text: '保存修改', style: 'display:none' });
  const cancelBtn = el('button', { type: 'button', class: 'btn btn--secondary', text: '取消', style: 'display:none' });
  const invalidateHint = el('p', { class: 'hint', style: 'display:none', text: '内容已修改：保存后需重新人工确认才能进入付费步骤。' });

  let editing = false;
  const setEditing = (flag) => {
    editing = flag;
    preview.style.display = flag ? 'none' : '';
    editor.style.display = flag ? '' : 'none';
    editBtn.style.display = flag ? 'none' : '';
    saveBtn.style.display = flag ? '' : 'none';
    cancelBtn.style.display = flag ? '' : 'none';
    invalidateHint.style.display = 'none';
    if (flag) {
      if (draft && draft.value !== markdown) draftBadge.style.display = '';
      editor.focus();
    } else {
      draftBadge.style.display = 'none';
    }
  };

  editor.addEventListener('input', () => {
    saveDraft(projectId, DRAFT_NAME, editor.value);
    // 编辑后确认失效立即可见（无需等待后端往返）
    invalidateHint.style.display = editor.value !== markdown ? '' : 'none';
  });

  editBtn.addEventListener('click', () => setEditing(true));
  cancelBtn.addEventListener('click', () => { editor.value = markdown; clearDraft(projectId, DRAFT_NAME); setEditing(false); });
  saveBtn.addEventListener('click', async () => {
    editError.textContent = '';
    if (editor.value.trim() === '') { editError.textContent = '任务书不能为空。'; return; }
    saveBtn.disabled = true;
    try {
      const updated = await advance(projectId, { edited_markdown: editor.value });
      clearDraft(projectId, DRAFT_NAME);
      toast('任务书已保存为新修订，需重新确认。');
      onChanged?.(updated);
    } catch (error) {
      editError.textContent = error.message;
    } finally {
      saveBtn.disabled = false;
    }
  });

  /* 确认区 */
  const approveRow = el('div', { class: 'button-row', style: 'margin-top:12px' });
  const approveBtn = el('button', { type: 'button', class: 'btn btn--primary', text: '确认任务书并继续' });
  approveBtn.disabled = valid || !actor;
  approveBtn.addEventListener('click', async () => {
    approveBtn.disabled = true;
    try {
      const updated = await advance(projectId, { task_approved: true, actor });
      toast('任务书已确认。');
      onChanged?.(updated);
    } catch (error) {
      toast(error.message, 'error');
      approveBtn.disabled = false;
    }
  });
  approveRow.append(approveBtn);
  if (!valid && !actor) approveRow.append(el('small', { text: '请先在上方"当前决策"区填写操作人身份。' }));

  const row = el('div', { class: 'button-row', style: 'margin-top:12px' }, [editBtn, saveBtn, cancelBtn]);
  container.append(head, preview, editor, editError, draftBadge, invalidateHint, row);
  if (snapshot.state === 'confirmation_build') container.append(approveRow);
  return { setEditing };
}
