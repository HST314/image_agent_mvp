/* T35 验收：前端状态/动作与后端状态表一一对应。 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  WORKFLOW_STATES, CAPABILITY_ACTIONS, deriveView, approvalValid, eventLabel, stateLabel,
} from '../../frontend/static/js/states.js';

// 后端 v1.7.3 事实表：main_front._capabilities 的全部可能输出。
const BACKEND_CAPABILITIES = [
  'retry', 'answer_clarification', 'select_master', 'review_calibration',
  'resume_quality_inspection', 'submit_human_tune', 'resume', 'branch', 'inspect',
];

test('七个生产状态与后端 WorkflowRunner.ORDER 一致', () => {
  assert.deepEqual(WORKFLOW_STATES.map((s) => s.id), [
    'intake_clarify', 'confirmation_build', 'initial_candidate_generation',
    'master_candidate_selection', 'self_check_iteration', 'human_prompt_iteration', 'final_approval',
  ]);
});

test('服务端每个能力都有且仅有一个前端动作', () => {
  for (const cap of BACKEND_CAPABILITIES) {
    assert.ok(CAPABILITY_ACTIONS[cap], `缺少能力映射：${cap}`);
    assert.equal(CAPABILITY_ACTIONS[cap].id, cap);
  }
  assert.deepEqual(Object.keys(CAPABILITY_ACTIONS).sort(), BACKEND_CAPABILITIES.slice().sort());
});

const view = (snapshot, manifest = {}, capabilities = []) => ({ snapshot, manifest, capabilities });

test('waiting_clarification → clarify 舞台', () => {
  const v = view({ state: 'intake_clarify', phase: 'waiting_clarification' }, {}, ['answer_clarification']);
  assert.equal(deriveView(v).stage, 'clarify');
});

test('confirmation_build + waiting_human_approval → taskbook 舞台', () => {
  const v = view({ state: 'confirmation_build', phase: 'waiting_human_approval' });
  assert.equal(deriveView(v).stage, 'taskbook');
});

test('waiting_master_selection → gallery 舞台', () => {
  const v = view({ state: 'master_candidate_selection', phase: 'waiting_master_selection' });
  assert.equal(deriveView(v).stage, 'gallery');
});

test('self_check 等待 + available_actions → disposition（上限分流）', () => {
  const v = view({ state: 'self_check_iteration', phase: 'waiting_human_approval', available_actions: ['abandon'] });
  assert.equal(deriveView(v).stage, 'disposition');
});

test('self_check 等待无 available_actions → calibration（人工放行）', () => {
  const v = view({ state: 'self_check_iteration', phase: 'waiting_human_approval' });
  assert.equal(deriveView(v).stage, 'calibration');
});

test('waiting_human_tune → annotate 舞台', () => {
  assert.equal(deriveView(view({ state: 'human_prompt_iteration', phase: 'waiting_human_tune' })).stage, 'annotate');
});

test('遗留 waiting_reinspection / additional_rounds_approved → 重新质检', () => {
  assert.equal(deriveView(view({ state: 'human_prompt_iteration', phase: 'waiting_reinspection' })).stage, 'reinspection');
  assert.equal(deriveView(view({ state: 'self_check_iteration', phase: 'additional_rounds_approved' })).stage, 'resume_quality');
});

test('质检通过（calibration_completed）→ final 最终确认', () => {
  const v = view({ state: 'self_check_iteration', phase: 'calibration_completed', termination_satisfied: true });
  assert.equal(deriveView(v).stage, 'final');
});

test('final_approval 人工确认门禁失败也映射到 final 舞台而非错误页', () => {
  const manifest = { failed_step: { state: 'final_approval', error: { message: '最终交付必须经过人工确认。' } } };
  const v = view({ state: 'human_prompt_iteration' }, manifest, ['retry']);
  const d = deriveView(v);
  assert.equal(d.stage, 'final');
  assert.equal(d.viaFailureGate, true);
});

test('其他失败 → failed 舞台并携带 failure', () => {
  const manifest = { failed_step: { state: 'initial_candidate_generation', error: { message: '模型不可用' } } };
  const d = deriveView(view({ state: 'confirmation_build' }, manifest, ['retry']));
  assert.equal(d.stage, 'failed');
  assert.equal(d.failure.state, 'initial_candidate_generation');
});

test('不可重试参数错误不暴露 retry 动作', () => {
  const manifest = { failed_step: { state: 'initial_candidate_generation', error: { message: '图片尺寸不合法', retryable: false } } };
  const d = deriveView(view({ state: 'confirmation_build' }, manifest, []));
  assert.equal(d.stage, 'failed');
  assert.deepEqual(d.actions, []);
});

test('completed / terminated / resume / empty 边界', () => {
  assert.equal(deriveView(view({ state: 'final_approval', completed: true })).stage, 'completed');
  assert.equal(deriveView(view({ state: 'self_check_iteration', phase: 'terminated_without_delivery' })).stage, 'terminated');
  assert.equal(deriveView(view({ state: 'confirmation_build', phase: 'task_approved' })).stage, 'resume');
  assert.equal(deriveView(view({})).stage, 'empty');
});

test('approvalValid：actor + revision 哈希双条件', () => {
  const rev = { revision_hash: 'abc' };
  assert.equal(approvalValid({ task_approval: { actor: 'u', revision_hash: 'abc' }, task_revision: rev }), true);
  assert.equal(approvalValid({ task_approval: { actor: 'u', revision_hash: 'abc' }, task_revision: { revision_hash: 'def' } }), false);
  assert.equal(approvalValid({ task_approval: { revision_hash: 'abc' }, task_revision: rev }), false);
  assert.equal(approvalValid({ task_revision: rev }), false);
});

test('事件标签有兜底且不泄露未知类型细节', () => {
  assert.equal(eventLabel({ type: 'delivery_frozen' }), '交付已冻结');
  assert.equal(eventLabel({ type: 'step_succeeded', state: 'self_check_iteration' }), '完成节点 · 画面质检');
  assert.equal(eventLabel({ type: 'some_future_event' }), '记录进度');
  assert.equal(stateLabel('unknown_state'), 'unknown_state');
});
