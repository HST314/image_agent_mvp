/* 任务书模块（T32）：Markdown 安全预览/编辑、确认门禁、编辑后确认失效立即可见、
 * 草稿自动保存与恢复。确认动作经 advance 携带 task_approved + actor（一期手工入口）。 */

import { el, toast } from './dom.js';
import { renderMarkdownInto } from './markdown.js';
import { approvalValid } from './states.js';
import { saveDraft, loadDraft, clearDraft } from './store.js';
import { advance } from './api.js';
import { taskbookDisplayMarkdown } from './copy.js';

const DRAFT_NAME = 'taskbook-markdown';

export function renderTaskbook(container, view, { projectId, actor, onChanged, jobRunner }) {
  const snapshot = view.snapshot || {};
  const markdown = snapshot.task_markdown || '';
  const valid = approvalValid(snapshot);
  const approval = snapshot.task_approval;
  const status = valid
    ? el('span', { class: 'badge badge--success', text: `已确认 · ${approval?.actor || ''}` })
    : el('span', { class: 'badge badge--warning', text: approval ? '确认已失效' : '待确认' });

  const preview = el('article', {
    class: 'markdown-body taskbook__document',
    role: 'document',
    'aria-label': '创作任务书正文',
  });
  /* T11：预览中将后端以任务卡字段名生成的事实标签（如 audience）替换为中文；
   * 编辑区仍持有原始 Markdown，保存回写后端的数据不受影响（任务书整体重构属 T4）。 */
  renderMarkdownInto(preview, taskbookDisplayMarkdown(markdown));

  /* 编辑区：草稿随输入自动保存，刷新后可恢复 */
  const editor = el('textarea', {
    class: 'input taskbook__editor',
    id: 'taskbook-editor',
    'aria-label': '编辑任务书 Markdown',
  });
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
  const fullscreenBtn = el('button', {
    type: 'button',
    class: 'btn btn--secondary taskbook__fullscreen',
    'aria-pressed': 'false',
    'aria-label': '进入全屏编辑',
  });
  const fullscreenIcon = el('span', { 'aria-hidden': 'true' });
  fullscreenIcon.innerHTML = '<svg viewBox="0 0 24 24"><path d="M8 3H3v5M16 3h5v5M8 21H3v-5M16 21h5v-5"/></svg>';
  const fullscreenLabel = el('span', { text: '全屏编辑' });
  fullscreenBtn.append(fullscreenIcon, fullscreenLabel);
  const invalidateHint = el('p', { class: 'hint', style: 'display:none', text: '内容已修改：保存后需重新人工确认才能进入付费步骤。' });

  const editorToolbar = el('div', { class: 'taskbook__editor-toolbar' }, [
    el('strong', { text: '编辑任务书' }),
    fullscreenBtn,
  ]);
  const editorActions = el('div', { class: 'button-row taskbook__editor-actions' }, [saveBtn, cancelBtn]);
  const editorFeedback = el('div', { class: 'taskbook__editor-feedback' }, [draftBadge, invalidateHint, editError]);
  const editorShell = el('div', {
    class: 'taskbook__editor-shell',
    role: 'region',
    'aria-label': '任务书编辑器',
    style: 'display:none',
  }, [editorToolbar, editor, editorFeedback, editorActions]);

  let editing = false;
  let actionBar = null;
  const setFullscreen = (flag) => {
    editorShell.classList.toggle('is-fullscreen', flag);
    fullscreenBtn.setAttribute('aria-pressed', String(flag));
    fullscreenBtn.setAttribute('aria-label', flag ? '退出全屏编辑' : '进入全屏编辑');
    fullscreenLabel.textContent = flag ? '退出全屏' : '全屏编辑';
    if (flag) editor.focus();
  };
  const setEditing = (flag) => {
    editing = flag;
    preview.style.display = flag ? 'none' : '';
    editorShell.style.display = flag ? '' : 'none';
    if (actionBar) actionBar.style.display = flag ? 'none' : '';
    saveBtn.style.display = flag ? '' : 'none';
    cancelBtn.style.display = flag ? '' : 'none';
    invalidateHint.style.display = 'none';
    if (flag) {
      if (draft && draft.value !== markdown) draftBadge.style.display = '';
      editor.focus();
    } else {
      setFullscreen(false);
      draftBadge.style.display = 'none';
    }
  };

  editor.addEventListener('input', () => {
    saveDraft(projectId, DRAFT_NAME, editor.value);
    // 编辑后确认失效立即可见（无需等待后端往返）
    invalidateHint.style.display = editor.value !== markdown ? '' : 'none';
  });

  editBtn.addEventListener('click', () => setEditing(true));
  fullscreenBtn.addEventListener('click', () => setFullscreen(!editorShell.classList.contains('is-fullscreen')));
  editorShell.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && editorShell.classList.contains('is-fullscreen')) {
      event.preventDefault();
      setFullscreen(false);
    }
  });
  cancelBtn.addEventListener('click', () => { editor.value = markdown; clearDraft(projectId, DRAFT_NAME); setEditing(false); });
  saveBtn.addEventListener('click', async () => {
    editError.textContent = '';
    if (editor.value.trim() === '') { editError.textContent = '任务书不能为空。'; return; }
    saveBtn.disabled = true;
    saveBtn.textContent = '正在保存…';
    try {
      const updated = await advance(projectId, { edited_markdown: editor.value });
      clearDraft(projectId, DRAFT_NAME);
      setFullscreen(false);
      toast('任务书已保存为新修订，需重新确认。');
      onChanged?.(updated);
    } catch (error) {
      editError.textContent = error.message;
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = '保存修改';
    }
  });

  /* 确认区 */
  const approveCopy = el('div', { class: 'taskbook__approve-copy' }, [
    el('strong', { text: '任务书内容无误？' }),
    el('small', { text: '下一步会开始生成候选图，并可能产生模型调用费用。' }),
  ]);
  const approveBtn = el('button', { type: 'button', class: 'btn btn--primary', text: '确认任务书，开始生成候选图' });
  approveBtn.disabled = valid || !actor;
  approveBtn.addEventListener('click', async () => {
    approveBtn.disabled = true;
    if (jobRunner) {
      const job = await jobRunner.start({ task_approved: true, actor }, { intent: 'task' });
      if (!job) approveBtn.disabled = false;
      return;
    }
    try {
      const updated = await advance(projectId, { task_approved: true, actor });
      toast('任务书已确认。');
      onChanged?.(updated);
    } catch (error) {
      toast(error.message, 'error');
      approveBtn.disabled = false;
    }
  });
  if (!valid && !actor) approveCopy.append(el('small', { class: 'field-error', text: '请先在状态页「工程信息」填写操作人身份。' }));

  const editControls = el('div', { class: 'button-row taskbook__edit-controls' }, [editBtn, status]);
  actionBar = el('div', { class: 'taskbook__actions' }, [editControls]);
  if (snapshot.state === 'confirmation_build') actionBar.append(approveCopy, approveBtn);

  container.append(preview, editorShell, actionBar);
  return { setEditing };
}
