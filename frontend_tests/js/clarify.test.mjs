import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  buildClarificationSubmission,
  clarificationAnswerError,
  normalizeClarificationDraft,
  optionNeedsFreeText,
} from '../../frontend/static/js/clarify.js';

const card = {
  question_card_id: 'card-2',
  questions: [{
    question_id: 'q-mission',
    field: 'mission_name',
    question: '任务名称是什么？',
    options: [
      { option_id: 'known', label: '沿用已有名称', description: '', requires_free_text: false },
      { option_id: 'other', label: '其他（请注明）', description: '', requires_free_text: true },
    ],
  }],
};

test('澄清答案按问题卡与问题 ID 提交，不再把展示标签当作事实', () => {
  assert.deepEqual(buildClarificationSubmission(card, {
    'q-mission': { selected_option_id: 'other', free_text: '夏日轻盈计划' },
  }), {
    question_card_id: 'card-2',
    answers: [{
      question_id: 'q-mission',
      selected_option_id: 'other',
      free_text: '夏日轻盈计划',
      skipped: false,
    }],
  });
});

test('requires_free_text 选项缺少具体内容时阻止提交', () => {
  assert.equal(optionNeedsFreeText(card.questions[0], 'other'), true);
  assert.match(
    clarificationAnswerError(card.questions[0], { selected_option_id: 'other', free_text: '  ' }),
    /填写具体内容/,
  );
  assert.equal(
    clarificationAnswerError(card.questions[0], { selected_option_id: 'other', free_text: '夏日轻盈计划' }),
    '',
  );
});

test('普通选项无需自由文本，自定义回答也可单独提交', () => {
  assert.equal(clarificationAnswerError(card.questions[0], { selected_option_id: 'known', free_text: '' }), '');
  assert.equal(clarificationAnswerError(card.questions[0], { selected_option_id: null, free_text: '由模型拟定' }), '');
});

test('结构化草稿只恢复同一问题卡，并兼容旧版字段到标签草稿', () => {
  const restored = normalizeClarificationDraft(card, {
    question_card_id: 'card-2',
    answers: { 'q-mission': { selected_option_id: 'other', free_text: '名称 A' } },
  });
  assert.deepEqual(restored['q-mission'], { selected_option_id: 'other', free_text: '名称 A' });

  const stale = normalizeClarificationDraft(card, {
    question_card_id: 'card-1',
    answers: { 'q-mission': { selected_option_id: 'other', free_text: '旧名称' } },
  });
  assert.deepEqual(stale['q-mission'], { selected_option_id: null, free_text: '' });

  const legacy = normalizeClarificationDraft(card, { mission_name: '其他（请注明）' });
  assert.deepEqual(legacy['q-mission'], { selected_option_id: 'other', free_text: '' });
});
