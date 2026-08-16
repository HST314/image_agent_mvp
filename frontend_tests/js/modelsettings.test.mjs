/* 设置页「模型」标签页纯逻辑回归：下拉选项按能力分组过滤、当前绑定反查。 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildModelOptions, currentEntryId } from '../../frontend/static/js/modelsettings.js';

const LIBRARY = {
  text_models: [
    { id: 'deepseek-v4-flash-260425', label: 'DeepSeek V4 Flash', provider: 'ark', model: 'deepseek-v4-flash-260425', description: '文本推理' },
  ],
  vlm_models: [
    { id: 'doubao-seed-evolving', label: '豆包 Seed Evolving', provider: 'ark', model: 'doubao-seed-evolving', description: '' },
  ],
  image_models: [
    { id: 'doubao-seedream-5-0-260128', label: '豆包 Seedream 5.0', provider: 'ark', model: 'doubao-seedream-5-0-260128', description: '文生图' },
  ],
};

test('能力匹配：生图阶段下拉只列出 image_models 分组', () => {
  const stateEntry = {
    state: 'initial_candidate_generation',
    model_role: 'text_to_image_model',
    group: 'image_models',
    binding: { state: 'initial_candidate_generation', model_role: 'text_to_image_model', provider: 'ark', model: 'doubao-seedream-5-0-260128', parameters: {}, fallback_model: null },
  };
  const options = buildModelOptions(LIBRARY, stateEntry);
  assert.equal(options.length, 1, '下拉只含文生图分组，不混入文本/VLM 模型');
  assert.equal(options[0].value, 'doubao-seedream-5-0-260128');
  assert.equal(options[0].selected, true, '当前绑定按 provider+model 反查并选中');
  assert.ok(options[0].label.includes('文生图'), '带说明小字');
});

test('当前绑定不在模型库：追加占位项提示选择', () => {
  const stateEntry = {
    state: 'intake_clarify', model_role: 'reasoning_llm', group: 'text_models',
    binding: { state: 'intake_clarify', model_role: 'reasoning_llm', provider: 'ark', model: 'some-other-model', parameters: {}, fallback_model: null },
  };
  const options = buildModelOptions(LIBRARY, stateEntry);
  assert.equal(options[0].value, '');
  assert.ok(options[0].label.includes('不在模型库'));
  assert.equal(options[0].selected, true);
  assert.equal(options[1].value, 'deepseek-v4-flash-260425');
});

test('空库安全：下拉为空不报错', () => {
  const options = buildModelOptions({}, { state: 'intake_clarify', group: 'text_models', binding: null });
  assert.deepEqual(options, []);
  assert.equal(currentEntryId(null, 'text_models', null), '');
});
