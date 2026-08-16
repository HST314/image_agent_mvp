/* T35 验收：前端状态/动作与后端状态表一一对应。 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  WORKFLOW_STATES, CAPABILITY_ACTIONS, deriveView, approvalValid, eventLabel,
  skillApprovalActorState, stateLabel, rerunBoundary, RERUN_BOUNDARIES,
  EMPTY_PAYLOAD_CAPABILITIES,
} from '../../frontend/static/js/states.js';

// 后端 v1.7.3 事实表：main_front._capabilities 的全部可能输出。
const BACKEND_CAPABILITIES = [
  'retry', 'approve_category_constraint', 'retry_category_constraint',
  'answer_clarification', 'apply_clarification_safe_defaults',
  'adjust_clarification_budget', 'continue_clarification_after_budget_change',
  'answer_taskbook_revision', 'apply_taskbook_scope_boundaries',
  'regenerate_taskbook', 'edit_taskbook',
  'select_master', 'review_calibration',
  'enter_human_tune',
  'approve_skill_invocations', 'retry_skill_invocations', 'resume_quality_inspection',
  'submit_human_tune', 'start_clarification', 'build_taskbook', 'prepare_style_direction',
  'render_candidates', 'choose_master', 'start_quality_inspection', 'open_final_approval',
  'start_category_match',
  'edit_rework', 'abandon', 'branch', 'inspect',
];

test('八个生产状态与后端 WorkflowRunner.ORDER 一致', () => {
  assert.deepEqual(WORKFLOW_STATES.map((s) => s.id), [
    'category_constraint', 'intake_clarify', 'confirmation_build', 'initial_candidate_generation',
    'master_candidate_selection', 'self_check_iteration', 'human_prompt_iteration', 'final_approval',
  ]);
  assert.equal(stateLabel('initial_candidate_generation'), '艺术风格');
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

test('waiting_clarification_review → 可恢复的 clarify 舞台', () => {
  const v = view(
    { state: 'intake_clarify', phase: 'waiting_clarification_review' },
    {},
    ['answer_clarification', 'adjust_clarification_budget'],
  );
  const result = deriveView(v);
  assert.equal(result.stage, 'clarify');
  assert.equal(result.budgetReview, true);
});

test('confirmation_build + waiting_human_approval → taskbook 舞台', () => {
  const v = view({ state: 'confirmation_build', phase: 'waiting_human_approval' });
  assert.equal(deriveView(v).stage, 'taskbook');
});

test('waiting_taskbook_revision → 可恢复的任务书修订舞台', () => {
  const v = view(
    { state: 'confirmation_build', phase: 'waiting_taskbook_revision' },
    {},
    ['answer_taskbook_revision', 'apply_taskbook_scope_boundaries', 'regenerate_taskbook', 'edit_taskbook'],
  );
  const result = deriveView(v);
  assert.equal(result.stage, 'taskbook');
  assert.equal(result.taskbookRevision, true);
  assert.equal(result.waiting, true);
  assert.deepEqual(result.actions, v.capabilities);
});

test('waiting_master_selection → gallery 舞台', () => {
  const v = view({ state: 'master_candidate_selection', phase: 'waiting_master_selection' });
  assert.equal(deriveView(v).stage, 'gallery');
});

test('waiting_skill_approval → 独立技能调用人工门禁舞台', () => {
  const v = view(
    { state: 'initial_candidate_generation', phase: 'waiting_skill_approval' },
    {},
    ['approve_skill_invocations', 'retry_skill_invocations'],
  );
  assert.equal(deriveView(v).stage, 'skill_approval');
});

test('self_check 等待 + available_actions → disposition（上限分流）', () => {
  const v = view({ state: 'self_check_iteration', phase: 'waiting_human_approval', termination_reason: 'solo_round_limit', available_actions: ['abandon'] });
  assert.equal(deriveView(v).stage, 'disposition');
});

test('残留 available_actions 不得把普通人工门禁误判为轮次上限', () => {
  const v = view({ state: 'self_check_iteration', phase: 'waiting_human_approval', termination_reason: 'manual_release_required', available_actions: ['abandon'] });
  assert.equal(deriveView(v).stage, 'calibration');
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

test('completed / terminated / 显式阶段 / empty 边界', () => {
  assert.equal(deriveView(view({ state: 'final_approval', completed: true })).stage, 'completed');
  assert.equal(deriveView(view({ state: 'self_check_iteration', phase: 'terminated_without_delivery' })).stage, 'terminated');
  assert.equal(deriveView(view({ state: 'confirmation_build', phase: 'task_approved' })).stage, 'taskbook');
  assert.equal(deriveView(view({})).stage, 'empty');
});

test('master_selected + active job 保留主图上下文，不得映射为 resume', () => {
  const result = deriveView({ snapshot: { state: 'master_candidate_selection', phase: 'master_selected' },
    manifest: {}, capabilities: [], active_job: { status: 'running' } });
  assert.equal(result.stage, 'quality_pending');
  assert.equal(result.processing, true);
});

test('内容审核失败回到质检舞台并提供修改恢复动作', () => {
  const result = deriveView(view(
    { state: 'self_check_iteration', inspection: { passed: false } },
    { failed_step: { state: 'self_check_iteration', error: { category: 'content_moderation', message: 'x' } } },
    ['edit_rework', 'abandon'],
  ));
  assert.equal(result.stage, 'calibration');
  assert.equal(result.moderationFailure, true);
});

test('所有生产状态均有明确舞台，永不返回 resume', () => {
  const stages = WORKFLOW_STATES.map(({ id }) => deriveView(view({ state: id })).stage);
  assert.equal(stages.includes('resume'), false);
});

test('approvalValid：actor + revision 哈希双条件', () => {
  const rev = { revision_hash: 'abc' };
  assert.equal(approvalValid({ task_approval: { actor: 'u', revision_hash: 'abc' }, task_revision: rev }), true);
  assert.equal(approvalValid({ task_approval: { actor: 'u', revision_hash: 'abc' }, task_revision: { revision_hash: 'def' } }), false);
  assert.equal(approvalValid({ task_approval: { revision_hash: 'abc' }, task_revision: rev }), false);
  assert.equal(approvalValid({ task_revision: rev }), false);
});

test('技能调用人工门禁：缺少操作人时同步禁用并提供近场提示', () => {
  assert.deepEqual(skillApprovalActorState('  '), {
    actor: '', ready: false,
    message: '请先在状态页「工程信息」填写操作人身份，才能确认或换版。',
  });
  assert.deepEqual(skillApprovalActorState(' reviewer '), {
    actor: 'reviewer', ready: true,
    message: '本次确认或换版将记录操作人：reviewer',
  });
});

test('事件标签有兜底且不泄露未知类型细节', () => {
  assert.equal(eventLabel({ type: 'delivery_frozen' }), '交付已冻结');
  assert.equal(eventLabel({ type: 'step_succeeded', state: 'self_check_iteration' }), '完成节点 · 画面质检');
  assert.equal(eventLabel({ type: 'some_future_event' }), '记录进度');
  // T11（契约 §11）：未知状态不再把英文 state id 原样上屏
  assert.equal(stateLabel('unknown_state'), '状态未知');
});

/* ---- 重跑分支头边界（ready_for_* 相位，与后端 _rewind_stage/_capabilities 对齐） ---- */

test('每个重跑边界相位都有节点骨架与已注册的重启能力', () => {
  assert.deepEqual(Object.keys(RERUN_BOUNDARIES).sort(), [
    'ready_for_category_match', 'ready_for_clarification', 'ready_for_final_approval',
    'ready_for_quality_inspection', 'ready_for_style_direction', 'ready_for_taskbook',
  ]);
  for (const [phase, info] of Object.entries(RERUN_BOUNDARIES)) {
    assert.ok(CAPABILITY_ACTIONS[info.capability], `${phase} 的重启能力未注册：${info.capability}`);
    assert.ok(EMPTY_PAYLOAD_CAPABILITIES.has(info.capability), `${phase} 的重启能力必须可空负载启动`);
    assert.equal(CAPABILITY_ACTIONS[info.capability].kind, 'job');
  }
  // 仅最终确认边界不需要骨架（落到节点真实界面，由人工确认继续）。
  assert.equal(RERUN_BOUNDARIES.ready_for_final_approval.skeleton, null);
});

test('rerunBoundary：识别边界、在途状态与可运行性', () => {
  const boundaryView = view(
    { state: 'initial_candidate_generation', phase: 'ready_for_style_direction' },
    {},
    ['prepare_style_direction', 'branch'],
  );
  assert.deepEqual(rerunBoundary(boundaryView), {
    phase: 'ready_for_style_direction', skeleton: 'style',
    capability: 'prepare_style_direction', processing: false, runnable: true,
  });
  // 能力清单缺失重启动作时不可运行（旧后端的死胡同形态）。
  assert.equal(rerunBoundary(view(
    { state: 'initial_candidate_generation', phase: 'ready_for_style_direction' }, {}, ['branch'],
  )).runnable, false);
  // 在途 job → processing。
  assert.equal(rerunBoundary({
    ...boundaryView, active_job: { status: 'running' },
  }).processing, true);
  // 非边界相位返回 null。
  assert.equal(rerunBoundary(view({ state: 'intake_clarify', phase: 'waiting_clarification' })), null);
  assert.equal(rerunBoundary(null), null);
});

test('重跑边界相位仍映射到本节点舞台（不出现通用恢复舞台）', () => {
  const cases = [
    [{ state: 'category_constraint', phase: 'ready_for_category_match' }, 'category'],
    [{ state: 'intake_clarify', phase: 'ready_for_clarification' }, 'clarify'],
    [{ state: 'confirmation_build', phase: 'ready_for_taskbook' }, 'taskbook'],
    [{ state: 'initial_candidate_generation', phase: 'ready_for_style_direction' }, 'style_processing'],
    [{ state: 'self_check_iteration', phase: 'ready_for_quality_inspection' }, 'calibration'],
    [{ state: 'final_approval', phase: 'ready_for_final_approval' }, 'final'],
  ];
  for (const [snapshot, stage] of cases) {
    assert.equal(deriveView(view(snapshot, {}, ['branch'])).stage, stage, snapshot.phase);
  }
});
