/* 澄清表单（T32）：选项卡片直接展示推荐原因、支持自定义输入、必填校验、草稿恢复。 */

import { el, toast } from './dom.js';
import { saveDraft, loadDraft, clearDraft } from './store.js';
import { fieldLabel } from './copy.js';

const DRAFT_NAME = 'clarification-answers';

function text(value) { return String(value ?? '').trim(); }

export function optionNeedsFreeText(question, optionId) {
  return Boolean((question.options || []).find((option) => option.option_id === optionId)?.requires_free_text);
}

/**
 * Restore both the structured draft and the legacy {field: label|string} draft.
 * The legacy branch can be removed after existing browser drafts have naturally expired.
 */
export function normalizeClarificationDraft(card, storedDraft) {
  const questions = card.questions || [];
  const structured = storedDraft?.question_card_id === card.question_card_id ? storedDraft.answers || {} : null;
  const answers = {};
  questions.forEach((question) => {
    const raw = structured?.[question.question_id] ?? storedDraft?.[question.field];
    if (typeof raw === 'string') {
      const option = (question.options || []).find((item) => item.label === raw);
      answers[question.question_id] = {
        selected_option_id: option?.option_id || null,
        free_text: option ? '' : raw,
      };
      return;
    }
    answers[question.question_id] = {
      selected_option_id: text(raw?.selected_option_id) || null,
      free_text: text(raw?.free_text),
    };
  });
  return answers;
}

export function clarificationAnswerError(question, answer) {
  const selectedId = text(answer?.selected_option_id);
  const freeText = text(answer?.free_text);
  const option = (question.options || []).find((item) => item.option_id === selectedId);
  if (selectedId && !option) return '所选选项已失效，请重新选择。';
  if (!selectedId && !freeText) return '请选择一个选项，或填写自己的答案。';
  if (option?.requires_free_text && !freeText) return `选择“${option.label}”后，请填写具体内容。`;
  return '';
}

export function buildClarificationSubmission(card, answersByQuestion) {
  return {
    question_card_id: card.question_card_id,
    answers: (card.questions || []).map((question) => {
      const answer = answersByQuestion[question.question_id] || {};
      return {
        question_id: question.question_id,
        selected_option_id: text(answer.selected_option_id) || null,
        free_text: text(answer.free_text) || null,
        skipped: false,
      };
    }),
  };
}

export function renderClarify(container, view, { projectId, jobRunner }) {
  const card = view.snapshot?.question_card || { questions: [] };
  const questions = card.questions || [];
  const budgetReview = view.snapshot?.phase === 'waiting_clarification_review';
  const capabilities = Array.isArray(view.capabilities) ? view.capabilities : [];
  const storedDraft = loadDraft(projectId, DRAFT_NAME)?.value || {};
  const draft = normalizeClarificationDraft(card, storedDraft);

  const head = el('div', { class: 'section__head' });
  const headText = el('div');
  headText.append(
    el('h2', { text: budgetReview ? '人工补充剩余阻塞项' : '补充关键信息' }),
    el('p', { text: budgetReview
      ? '自动提问预算已用完；补充下列信息后系统会重新检查是否可以生成任务书。'
      : '这些答案会写回生产任务卡后再继续；可直接选择推荐项，也可以输入自己的答案。' }),
  );
  head.append(headText, el('span', { class: 'badge badge--warning', text: `${questions.length} 项待回答` }));
  container.append(head);

  if (budgetReview) {
    const review = el('div', { class: 'clarification-review', role: 'status' });
    review.append(
      el('strong', { text: '流程已安全暂停，已填写内容不会丢失。' }),
      el('span', { text: view.snapshot?.clarification_review_reason
        || '仍有阻塞信息需要人工补充，不能直接跳过进入任务书。' }),
    );
    container.append(review);
  }

  const restored = Object.values(draft).some((answer) => answer.selected_option_id || answer.free_text);
  if (restored) container.append(el('p', { class: 'hint', text: '已恢复上次未提交的草稿。' }));

  const form = el('form', { id: 'answer-form', novalidate: 'novalidate' });
  const fields = [];

  questions.forEach((q, i) => {
    const fieldset = el('fieldset', { class: 'field' });
    // T11：question 缺失时不兜底展示英文字段名，映射为中文标签。
    const questionText = q.question || fieldLabel(q.field, '补充信息');
    fieldset.append(el('legend', { text: `${i + 1}. ${questionText}` }));
    if (q.impact) fieldset.append(el('small', { text: q.impact }));

    const errorId = `q${i}-error`;
    const customId = `q${i}-free-text`;
    const cards = el('div', {
      class: 'option-cards', role: 'radiogroup', 'aria-label': questionText,
      'aria-describedby': errorId,
    });
    (q.options || []).forEach((opt, j) => {
      const id = `q${i}-opt${j}`;
      const input = el('input', { type: 'radio', name: `question-${q.question_id}`, id, value: opt.option_id });
      const label = el('label', { class: 'option-card', for: id });
      label.append(input, el('span', {}, [el('strong', { text: opt.label }), el('span', { text: opt.description || '' })]));
      input.addEventListener('change', () => {
        cards.querySelectorAll('.option-card').forEach((c) => c.classList.remove('is-checked'));
        label.classList.add('is-checked');
        custom.required = Boolean(opt.requires_free_text);
        helper.textContent = opt.requires_free_text ? `选择“${opt.label}”后必须填写具体内容。` : '可选：补充选项中未涵盖的具体要求。';
        field.error.textContent = '';
        cards.setAttribute('aria-invalid', 'false');
        custom.setAttribute('aria-invalid', 'false');
        persist();
        if (opt.requires_free_text) custom.focus();
      });
      if (draft[q.question_id]?.selected_option_id === opt.option_id) { input.checked = true; label.classList.add('is-checked'); }
      cards.append(label);
    });
    fieldset.append(cards);

    const selectedDraftOption = (q.options || []).find((opt) => opt.option_id === draft[q.question_id]?.selected_option_id);
    const customLabel = el('label', { for: customId, text: '补充说明' });
    const custom = el('input', {
      class: 'input clarify-free-text', id: customId, maxlength: '2000',
      placeholder: '填写具体内容，或输入选项中没有的答案',
      'aria-describedby': `${customId}-help ${errorId}`,
    });
    custom.value = draft[q.question_id]?.free_text || '';
    custom.required = Boolean(selectedDraftOption?.requires_free_text);
    const helper = el('small', {
      id: `${customId}-help`,
      text: selectedDraftOption?.requires_free_text
        ? `选择“${selectedDraftOption.label}”后必须填写具体内容。`
        : '可选：补充选项中未涵盖的具体要求。',
    });
    custom.addEventListener('input', () => {
      if (custom.getAttribute('aria-invalid') === 'true') validateField(field);
      persist();
    });
    custom.addEventListener('blur', () => validateField(field));
    const error = el('div', { class: 'field-error', id: errorId, role: 'alert', 'aria-live': 'polite' });
    const field = { question: q, cards, custom, error };
    fieldset.append(customLabel, custom, helper, error);
    fields.push(field);
    form.append(fieldset);
  });

  function collect() {
    const answers = {};
    for (const { question, cards, custom } of fields) {
      const checked = cards.querySelector('input[type=radio]:checked');
      answers[question.question_id] = {
        selected_option_id: checked?.value || null,
        free_text: custom.value.trim(),
      };
    }
    return answers;
  }
  function persist() {
    saveDraft(projectId, DRAFT_NAME, { question_card_id: card.question_card_id, answers: collect() });
  }

  function validateField(field) {
    const answers = collect();
    const message = clarificationAnswerError(field.question, answers[field.question.question_id]);
    field.error.textContent = message;
    field.cards.setAttribute('aria-invalid', message ? 'true' : 'false');
    field.custom.setAttribute('aria-invalid', message ? 'true' : 'false');
    return message;
  }

  const submit = el('button', { type: 'submit', class: 'btn btn--primary',
    text: budgetReview ? '补充剩余项并继续' : '提交答案并继续' });
  form.append(submit);

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const answers = collect();
    let firstInvalid = null;
    fields.forEach((field) => {
      const message = validateField(field);
      if (message && !firstInvalid) firstInvalid = field;
    });
    if (firstInvalid) {
      toast('请补全标记的问题后再提交。', 'error');
      const selected = firstInvalid.cards.querySelector('input[type=radio]:checked');
      (selected && optionNeedsFreeText(firstInvalid.question, selected.value) ? firstInvalid.custom : firstInvalid.cards.querySelector('input[type=radio]') || firstInvalid.custom).focus();
      return;
    }
    submit.disabled = true;
    submit.textContent = '正在提交并重新分析…';
    form.setAttribute('aria-busy', 'true');
    try {
      const job = await jobRunner.start(
        { clarification_answers: buildClarificationSubmission(card, answers) },
        { intent: 'answer-clarification', operation: '提交答案并重新分析' },
      );
      if (job) clearDraft(projectId, DRAFT_NAME);
    } catch (error) {
      toast(error.message, 'error');
      submit.disabled = false;
      submit.textContent = budgetReview ? '补充剩余项并继续' : '提交答案并继续';
      form.removeAttribute('aria-busy');
    }
  });

  container.append(form);

  if (budgetReview) {
    const actions = el('div', { class: 'clarification-recovery',
      'aria-label': '澄清恢复操作' });
    if (capabilities.includes('apply_clarification_safe_defaults')) {
      const defaults = el('button', { type: 'button', class: 'btn btn--secondary',
        text: '采用允许的安全默认' });
      defaults.addEventListener('click', () => jobRunner.start(
        { clarification_action: 'apply_safe_defaults' },
        { intent: 'clarification-safe-defaults', operation: '应用澄清安全默认值' },
      ));
      actions.append(defaults);
    }
    if (capabilities.includes('continue_clarification_after_budget_change')) {
      const continueButton = el('button', { type: 'button', class: 'btn btn--primary',
        text: '按新预算继续提问' });
      continueButton.addEventListener('click', () => jobRunner.start(
        { clarification_action: 'continue_after_budget_change' },
        { intent: 'clarification-budget-continue', operation: '按新预算继续澄清' },
      ));
      actions.append(continueButton);
    }
    if (actions.children.length) container.append(actions);
  }
}
