/* 任务书模块（T32）：Markdown 安全预览/编辑、确认门禁、编辑后确认失效立即可见、
 * 草稿自动保存与恢复。确认动作经 advance 携带 task_approved + actor（一期手工入口）。 */

import { el, toast } from './dom.js';
import { renderMarkdownInto } from './markdown.js';
import { approvalValid } from './states.js';
import { saveDraft, loadDraft, clearDraft } from './store.js';
import { advance } from './api.js';
import { taskbookDisplayMarkdown, fieldLabel } from './copy.js';
import {
  buildClarificationSubmission,
  clarificationAnswerError,
  normalizeClarificationDraft,
} from './clarify.js';

const DRAFT_NAME = 'taskbook-markdown';
const REVISION_DRAFT_NAME = 'taskbook-revision-answers';

/* 任务书修订（waiting_taskbook_revision）：错误信息贴近相关字段，就地给出
 * 补充剩余项 / 应用明确默认或范围边界 / 重新生成 / 手动编辑四类恢复动作。 */
function renderRevisionPanel(container, view, { projectId, jobRunner, onEdit }) {
  const snapshot = view.snapshot || {};
  const capabilities = Array.isArray(view.capabilities) ? view.capabilities : [];
  const reason = snapshot.taskbook_revision_reason || '任务书仍需人工处理后才能进入确认。';
  const fields = Array.isArray(snapshot.taskbook_revision_fields) ? snapshot.taskbook_revision_fields : [];
  const scopeFields = Array.isArray(snapshot.taskbook_scope_boundary_fields)
    ? snapshot.taskbook_scope_boundary_fields : [];
  const card = snapshot.question_card || { questions: [] };
  const questions = capabilities.includes('answer_taskbook_revision') ? card.questions || [] : [];

  const panel = el('section', { class: 'taskbook-revision', role: 'status' });
  panel.append(
    el('strong', { text: '任务书需要人工修订，已填写内容不会丢失。' }),
    el('span', { text: reason }),
  );
  if (fields.length) {
    panel.append(el('p', { class: 'hint',
      text: `涉及条目：${fields.map((field) => fieldLabel(field, field)).join('、')}` }));
  }
  container.append(panel);

  if (questions.length) {
    const storedDraft = loadDraft(projectId, REVISION_DRAFT_NAME)?.value || {};
    const draft = normalizeClarificationDraft(card, storedDraft);
    const form = el('form', { id: 'taskbook-revision-form', novalidate: 'novalidate' });
    const fieldsState = [];
    questions.forEach((q, i) => {
      const fieldset = el('fieldset', { class: 'field' });
      const questionText = q.question || fieldLabel(q.field, '补充信息');
      fieldset.append(el('legend', { text: `${i + 1}. ${questionText}` }));
      if (q.impact) fieldset.append(el('small', { text: q.impact }));
      const errorId = `rev-q${i}-error`;
      const customId = `rev-q${i}-free-text`;
      const cards = el('div', {
        class: 'option-cards', role: 'radiogroup', 'aria-label': questionText,
        'aria-describedby': errorId,
      });
      (q.options || []).forEach((opt, j) => {
        const id = `rev-q${i}-opt${j}`;
        const input = el('input', { type: 'radio', name: `question-${q.question_id}`, id, value: opt.option_id });
        const label = el('label', { class: 'option-card', for: id });
        label.append(input, el('span', {}, [el('strong', { text: opt.label }), el('span', { text: opt.description || '' })]));
        input.addEventListener('change', () => {
          cards.querySelectorAll('.option-card').forEach((c) => c.classList.remove('is-checked'));
          label.classList.add('is-checked');
          custom.required = Boolean(opt.requires_free_text);
          persist();
        });
        if (draft[q.question_id]?.selected_option_id === opt.option_id) { input.checked = true; label.classList.add('is-checked'); }
        cards.append(label);
      });
      fieldset.append(cards);
      const custom = el('input', {
        class: 'input clarify-free-text', id: customId, maxlength: '2000',
        placeholder: '填写具体内容，或输入选项中没有的答案',
        'aria-describedby': errorId,
      });
      custom.value = draft[q.question_id]?.free_text || '';
      custom.required = Boolean((q.options || []).find((opt) => opt.option_id === draft[q.question_id]?.selected_option_id)?.requires_free_text);
      custom.addEventListener('input', persist);
      const error = el('div', { class: 'field-error', id: errorId, role: 'alert', 'aria-live': 'polite' });
      fieldset.append(custom, error);
      fieldsState.push({ question: q, cards, custom, error });
      form.append(fieldset);
    });
    function collect() {
      const answers = {};
      for (const { question, cards, custom } of fieldsState) {
        const checked = cards.querySelector('input[type=radio]:checked');
        answers[question.question_id] = {
          selected_option_id: checked?.value || null,
          free_text: custom.value.trim(),
        };
      }
      return answers;
    }
    function persist() {
      saveDraft(projectId, REVISION_DRAFT_NAME, { question_card_id: card.question_card_id, answers: collect() });
    }
    const submit = el('button', { type: 'submit', class: 'btn btn--primary', text: '提交补充内容并重新生成任务书' });
    form.append(submit);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const answers = collect();
      let firstInvalid = null;
      fieldsState.forEach((field) => {
        const message = clarificationAnswerError(field.question, answers[field.question.question_id]);
        field.error.textContent = message;
        if (message && !firstInvalid) firstInvalid = field;
      });
      if (firstInvalid) {
        toast('请补全标记的问题后再提交。', 'error');
        return;
      }
      submit.disabled = true;
      submit.textContent = '正在提交并重新生成…';
      try {
        const job = await jobRunner.start(
          { clarification_answers: buildClarificationSubmission(card, answers) },
          { intent: 'taskbook-revision-answers', operation: '提交补充内容并重新生成任务书' },
        );
        if (job) clearDraft(projectId, REVISION_DRAFT_NAME);
      } catch (error) {
        toast(error.message, 'error');
        submit.disabled = false;
        submit.textContent = '提交补充内容并重新生成任务书';
      }
    });
    container.append(form);
  }

  const actions = el('div', { class: 'clarification-recovery', 'aria-label': '任务书修订操作' });
  if (capabilities.includes('apply_taskbook_scope_boundaries') && scopeFields.length) {
    const boundaries = el('button', { type: 'button', class: 'btn btn--secondary',
      text: `应用明确默认或范围边界（${scopeFields.map((field) => fieldLabel(field, field)).join('、')}）` });
    boundaries.addEventListener('click', () => jobRunner.start(
      { taskbook_action: 'apply_scope_boundaries' },
      { intent: 'taskbook-scope-boundaries', operation: '应用任务书明确默认或范围边界' },
    ));
    actions.append(boundaries);
  }
  if (capabilities.includes('regenerate_taskbook')) {
    const regenerate = el('button', { type: 'button', class: 'btn btn--secondary', text: '重新生成任务书' });
    regenerate.addEventListener('click', () => jobRunner.start(
      { taskbook_action: 'regenerate' },
      { intent: 'taskbook-regenerate', operation: '重新生成任务书' },
    ));
    actions.append(regenerate);
  }
  if (capabilities.includes('edit_taskbook')) {
    const edit = el('button', { type: 'button', class: 'btn btn--secondary', text: '手动编辑任务书' });
    edit.addEventListener('click', () => onEdit?.());
    actions.append(edit);
  }
  if (actions.children.length) container.append(actions);
}

export function renderTaskbook(container, view, { projectId, actor, onChanged, jobRunner }) {
  const snapshot = view.snapshot || {};
  const revision = snapshot.phase === 'waiting_taskbook_revision';
  const markdown = revision
    ? (snapshot.taskbook_revision_draft || snapshot.task_markdown || '')
    : (snapshot.task_markdown || '');
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
  if (snapshot.state === 'confirmation_build' && !revision) actionBar.append(approveCopy, approveBtn);

  container.append(preview, editorShell, actionBar);
  if (revision) {
    const revisionHost = el('div');
    container.prepend(revisionHost);
    renderRevisionPanel(revisionHost, view, {
      projectId,
      jobRunner,
      onEdit: () => setEditing(true),
    });
  }
  return { setEditing };
}
