/* T3 设置页表单模型回归（契约 §5/§8，Q3-B 三条不可偏离：全字段、中文化、
 * 常用/高级两组）。schema 形态对齐后端 RuntimePolicy.model_json_schema() +
 * main_front.py 的 settings/schema 响应（properties/$defs/current）。 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { flattenSchemaProperties, buildPolicyFormModel, buildPolicyPayload } from '../../frontend/static/js/policyform.js';

/* 与后端当前 RuntimePolicy 同构的测试 schema（含 $defs 嵌套与 const 锁定字段）。 */
function fakeSchema(overrides = {}) {
  return {
    properties: {
      max_auto_questions: { type: 'integer', minimum: 0, maximum: 10, default: 3 },
      stream_model_output: { const: false, type: 'boolean', default: false },
      clarification_total_budget: { type: 'integer', minimum: 0, maximum: 100, default: 10 },
      category_constraint: { $ref: '#/$defs/SkillInvocationPolicyConfig' },
      style_direction: { $ref: '#/$defs/SkillInvocationPolicyConfig' },
      self_check: { $ref: '#/$defs/SelfCheckPolicyConfig' },
      max_render_retries: { const: 0, type: 'integer', default: 0 },
      candidate_concurrency: { type: 'integer', minimum: 1, maximum: 5, default: 5 },
      model_timeout_seconds: { type: 'number', exclusiveMinimum: 0, maximum: 3600, default: 180 },
      image_api_base_url: { type: 'string', default: '' },
      default_output_size: { type: 'string', pattern: '^(\\d{2,5}x\\d{2,5}|[124]K)$', default: '2560x1440' },
      response_format: { enum: ['url', 'b64_json'], type: 'string', default: 'url' },
      watermark: { type: 'boolean', default: false },
      offline_mode: { type: 'boolean', default: false },
      allow_skill_degradation: { type: 'boolean', default: false },
      style_library_root: { type: 'string', default: 'agent-library' },
      ...overrides,
    },
    $defs: {
      SkillInvocationPolicyConfig: {
        type: 'object',
        properties: { release: { enum: ['auto', 'manual'], type: 'string', default: 'auto' } },
      },
      SelfCheckPolicyConfig: {
        type: 'object',
        properties: {
          termination: { enum: ['fix', 'solo'], type: 'string', default: 'solo' },
          fixed_rounds: { type: 'integer', minimum: 1, maximum: 20, default: 2 },
          max_rounds: { type: 'integer', minimum: 1, maximum: 50, default: 4 },
          stop_early_on_pass: { type: 'boolean', default: false },
          release: { enum: ['auto', 'manual'], type: 'string', default: 'auto' },
        },
      },
    },
    current: {
      max_auto_questions: 5,
      category_constraint: { release: 'manual' },
      style_direction: { release: 'auto' },
      self_check: { termination: 'fix', fixed_rounds: 3, max_rounds: 6, stop_early_on_pass: true, release: 'manual' },
      watermark: true,
    },
  };
}

/* ---------- flattenSchemaProperties ---------- */

test('拍平：$ref 嵌套对象展开为点路径，普通字段原样保留', () => {
  const flat = flattenSchemaProperties(fakeSchema().properties, fakeSchema().$defs);
  assert.ok(flat['self_check.termination'], '嵌套对象展开为 self_check.termination');
  assert.equal(flat['self_check.termination'].enum.join(','), 'fix,solo');
  assert.ok(flat.max_auto_questions);
  assert.equal(flat.self_check, undefined, '父级嵌套键不保留');
});

test('拍平：无 $ref 时原样返回；空 properties 安全', () => {
  assert.deepEqual(flattenSchemaProperties({ a: { type: 'string' } }), { a: { type: 'string' } });
  assert.deepEqual(flattenSchemaProperties({}, {}), {});
});

/* ---------- buildPolicyFormModel ---------- */

test('模型：全字段、常用/高级两组且顺序符合契约 §8 基线', () => {
  const model = buildPolicyFormModel(fakeSchema());
  assert.equal(model.all.length, 20, '策略对象展开后，共 20 个表单字段');
  assert.deepEqual(
    model.common.map((f) => f.path),
    ['max_auto_questions', 'clarification_total_budget', 'category_constraint.release',
      'style_direction.release',
      'self_check.termination', 'self_check.fixed_rounds',
      'self_check.max_rounds', 'self_check.stop_early_on_pass', 'self_check.release',
      'candidate_concurrency', 'default_output_size', 'watermark', 'offline_mode'],
  );
  assert.deepEqual(
    model.advanced.map((f) => f.path),
    ['model_timeout_seconds', 'image_api_base_url', 'response_format', 'max_render_retries',
      'allow_skill_degradation', 'style_library_root', 'stream_model_output'],
  );
});

test('模型：中文名与说明小字来自映射层，英文键名不上屏', () => {
  const model = buildPolicyFormModel(fakeSchema());
  const field = model.all.find((f) => f.path === 'self_check.termination');
  assert.equal(field.label, '自检终止方式');
  assert.ok(field.help.includes('固定轮次'));
  assert.deepEqual(field.options.map((o) => o.label), ['固定轮次', '按质量判定']);
});

test('模型：schema 新增未知字段落入高级组末尾且中文兜底', () => {
  const model = buildPolicyFormModel(fakeSchema({ brand_new_option: { type: 'integer', default: 1 } }));
  const extra = model.advanced[model.advanced.length - 1];
  assert.equal(extra.path, 'brand_new_option');
  assert.equal(extra.label, '其他策略项', '未收录英文键必须中文兜底（§11）');
  assert.equal(model.common.some((f) => f.path === 'brand_new_option'), false);
});

test('模型：控件类型与约束——enum/boolean/number/fixed/text', () => {
  const model = buildPolicyFormModel(fakeSchema());
  const byPath = Object.fromEntries(model.all.map((f) => [f.path, f]));
  assert.equal(byPath['self_check.release'].kind, 'enum');
  assert.equal(byPath['category_constraint.release'].kind, 'enum');
  assert.equal(byPath['style_direction.release'].kind, 'enum');
  assert.equal(byPath.watermark.kind, 'boolean');
  assert.equal(byPath.candidate_concurrency.kind, 'number');
  assert.equal(byPath.candidate_concurrency.min, 1);
  assert.equal(byPath.candidate_concurrency.max, 5);
  assert.equal(byPath.candidate_concurrency.integer, true);
  assert.equal(byPath.model_timeout_seconds.integer, false, 'float 非整数步长');
  assert.equal(byPath.stream_model_output.kind, 'fixed');
  assert.equal(byPath.stream_model_output.disabled, true, '常量字段只读');
  assert.equal(byPath.max_render_retries.kind, 'fixed');
  assert.equal(byPath.image_api_base_url.kind, 'text');
  assert.equal(byPath.default_output_size.pattern, '^(\\d{2,5}x\\d{2,5}|[124]K)$');
});

test('模型：当前值优先于 schema 默认值', () => {
  const model = buildPolicyFormModel(fakeSchema());
  const byPath = Object.fromEntries(model.all.map((f) => [f.path, f]));
  assert.equal(byPath.max_auto_questions.value, 5, '取自 current');
  assert.equal(byPath['self_check.termination'].value, 'fix', '嵌套 current 取值');
  assert.equal(byPath.watermark.value, true);
  assert.equal(byPath.candidate_concurrency.value, 5, 'current 缺失时取 schema default');
  assert.equal(byPath.stream_model_output.value, false, 'fixed 取 const');
});

/* ---------- buildPolicyPayload ---------- */

test('负载：点路径还原嵌套、类型收敛、fixed 恒为常量', () => {
  const model = buildPolicyFormModel(fakeSchema());
  const values = {
    max_auto_questions: '4',
    clarification_total_budget: '12',
    'category_constraint.release': 'manual',
    'style_direction.release': 'auto',
    'self_check.termination': 'fix',
    'self_check.fixed_rounds': '2',
    'self_check.max_rounds': '8',
    'self_check.stop_early_on_pass': true,
    'self_check.release': 'auto',
    candidate_concurrency: '3',
    default_output_size: '2K',
    watermark: false,
    offline_mode: true,
    model_timeout_seconds: '120.5',
    image_api_base_url: 'https://img.internal',
    response_format: 'b64_json',
    allow_skill_degradation: false,
    style_library_root: 'agent-library',
  };
  const policy = buildPolicyPayload(model.all, values);
  assert.equal(policy.max_auto_questions, 4, '数字字符串收敛为 Number');
  assert.equal(policy.model_timeout_seconds, 120.5, '浮点保留');
  assert.deepEqual(policy.self_check, {
    termination: 'fix', fixed_rounds: 2, max_rounds: 8, stop_early_on_pass: true, release: 'auto',
  }, '点路径还原为嵌套对象');
  assert.deepEqual(policy.category_constraint, { release: 'manual' });
  assert.deepEqual(policy.style_direction, { release: 'auto' });
  assert.equal(policy.stream_model_output, false, 'fixed 不读表单，恒为 const');
  assert.equal(policy.max_render_retries, 0);
  assert.equal(policy.watermark, false);
  assert.equal(policy.offline_mode, true);
});

test('负载：整数字段截断小数；非法数字抛中文校验错（含字段中文名）', () => {
  const model = buildPolicyFormModel(fakeSchema());
  const base = Object.fromEntries(model.all.map((f) => [f.path, f.kind === 'boolean' ? false : String(f.value ?? '')]));
  const truncated = buildPolicyPayload(model.all, { ...base, candidate_concurrency: '3.9' });
  assert.equal(truncated.candidate_concurrency, 3);
  assert.throws(
    () => buildPolicyPayload(model.all, { ...base, candidate_concurrency: 'abc' }),
    /「候选图并发数」需要填写数字/,
  );
});
