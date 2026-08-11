/* 澄清表单（T32）：选项卡片直接展示推荐原因、支持自定义输入、必填校验、草稿恢复。 */

import { el, toast } from './dom.js';
import { saveDraft, loadDraft, clearDraft } from './store.js';
import { advance } from './api.js';
import { fieldLabel } from './copy.js';

const DRAFT_NAME = 'clarification-answers';

export function renderClarify(container, view, { projectId, onSubmitted }) {
  const card = view.snapshot?.question_card || { questions: [] };
  const questions = card.questions || [];
  const draft = loadDraft(projectId, DRAFT_NAME)?.value || {};

  const head = el('div', { class: 'section__head' });
  const headText = el('div');
  headText.append(el('h2', { text: '补充关键信息' }), el('p', { text: '这些答案会写回生产任务卡后再继续；可直接选择推荐项，也可以输入自己的答案。' }));
  head.append(headText, el('span', { class: 'badge badge--warning', text: `${questions.length} 项待回答` }));
  container.append(head);

  const restored = Object.keys(draft).length > 0;
  if (restored) container.append(el('p', { class: 'hint', text: '已恢复上次未提交的草稿。' }));

  const form = el('form', { id: 'answer-form', novalidate: 'novalidate' });
  const fields = [];

  questions.forEach((q, i) => {
    const fieldset = el('fieldset', { class: 'field' });
    // T11：question 缺失时不兜底展示英文字段名，映射为中文标签。
    const questionText = q.question || fieldLabel(q.field, '补充信息');
    fieldset.append(el('legend', { text: `${i + 1}. ${questionText}` }));
    if (q.impact) fieldset.append(el('small', { text: q.impact }));

    const cards = el('div', { class: 'option-cards', role: 'radiogroup', 'aria-label': questionText });
    (q.options || []).forEach((opt, j) => {
      const id = `q${i}-opt${j}`;
      const input = el('input', { type: 'radio', name: q.field, id, value: opt.label });
      const label = el('label', { class: 'option-card', for: id });
      label.append(input, el('span', {}, [el('strong', { text: opt.label }), el('span', { text: opt.description || '' })]));
      input.addEventListener('change', () => {
        cards.querySelectorAll('.option-card').forEach((c) => c.classList.remove('is-checked'));
        label.classList.add('is-checked');
        custom.value = '';
        persist();
      });
      if (draft[q.field] && draft[q.field] === opt.label) { input.checked = true; label.classList.add('is-checked'); }
      cards.append(label);
    });
    fieldset.append(cards);

    const custom = el('input', { class: 'input', placeholder: '也可以输入自己的答案', 'aria-label': `${questionText} 自定义答案` });
    if (draft[q.field] && !(q.options || []).some((o) => o.label === draft[q.field])) custom.value = draft[q.field];
    custom.addEventListener('input', () => {
      if (custom.value) cards.querySelectorAll('input[type=radio]').forEach((r) => { r.checked = false; });
      cards.querySelectorAll('.option-card').forEach((c) => c.classList.remove('is-checked'));
      persist();
    });
    const error = el('div', { class: 'field-error', role: 'alert' });
    fieldset.append(custom, error);
    fields.push({ field: q.field, cards, custom, error });
    form.append(fieldset);
  });

  function collect() {
    const answers = {};
    for (const { field, cards, custom } of fields) {
      const checked = cards.querySelector('input[type=radio]:checked');
      answers[field] = String(custom.value.trim() || (checked ? checked.value : '')).trim();
    }
    return answers;
  }
  function persist() { saveDraft(projectId, DRAFT_NAME, collect()); }

  const submit = el('button', { type: 'submit', class: 'btn btn--primary', text: '提交答案并继续' });
  form.append(submit);

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const answers = collect();
    let firstInvalid = null;
    fields.forEach(({ field, error }) => {
      error.textContent = answers[field] ? '' : '请选择或输入答案。';
      if (!answers[field] && !firstInvalid) firstInvalid = field;
    });
    if (firstInvalid) { toast('还有未回答的问题。', 'error'); return; }
    submit.disabled = true;
    try {
      const updated = await advance(projectId, { clarification_answers: answers });
      clearDraft(projectId, DRAFT_NAME);
      toast('答案已提交。');
      onSubmitted?.(updated);
    } catch (error) {
      toast(error.message, 'error');
      submit.disabled = false;
    }
  });

  container.append(form);
}
