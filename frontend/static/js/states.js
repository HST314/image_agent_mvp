/* 状态/动作映射（T35 验收：与后端状态表一一对应）。
 *
 * 后端事实来源（v1.7.3）：
 * - agent_core.workflow_runner.ORDER：八个生产状态（加上前置品类约束）；
 * - WorkflowRunner.next_state / calibrator.calibration_loop：phase 语义；
 * - main_front._capabilities：服务端能力清单，前端只把这些能力映射为动作，
 *   不自行发明后端没有的入口。
 *
 * deriveView 为纯函数：输入 project view（snapshot/manifest/capabilities），
 * 输出工作台"当前决策"区的舞台标识与可用动作，供视图层渲染。
 */

export const WORKFLOW_STATES = [
  { id: 'category_constraint', label: '品类约束' },
  { id: 'intake_clarify', label: '需求澄清' },
  { id: 'confirmation_build', label: '任务书' },
  { id: 'initial_candidate_generation', label: '艺术风格' },
  { id: 'master_candidate_selection', label: '主图选择' },
  { id: 'self_check_iteration', label: '画面质检' },
  { id: 'human_prompt_iteration', label: '人工修改' },
  { id: 'final_approval', label: '最终交付' },
];

export const STATE_LABELS = Object.fromEntries(WORKFLOW_STATES.map((s) => [s.id, s.label]));

/** 服务端能力 → 前端动作（一一对应，不增不减）。 */
export const CAPABILITY_ACTIONS = {
  retry: { id: 'retry', label: '从上一成功点重试', kind: 'job' },
  approve_category_constraint: { id: 'approve_category_constraint', label: '确认品类约束', kind: 'job' },
  retry_category_constraint: { id: 'retry_category_constraint', label: '重新匹配品类', kind: 'job' },
  answer_clarification: { id: 'answer_clarification', label: '提交答案并继续', kind: 'job' },
  apply_clarification_safe_defaults: { id: 'apply_clarification_safe_defaults', label: '采用允许的安全默认', kind: 'job' },
  continue_clarification_after_budget_change: { id: 'continue_clarification_after_budget_change', label: '按新预算继续', kind: 'job' },
  answer_taskbook_revision: { id: 'answer_taskbook_revision', label: '提交补充内容', kind: 'job' },
  apply_taskbook_scope_boundaries: { id: 'apply_taskbook_scope_boundaries', label: '应用明确默认或范围边界', kind: 'job' },
  regenerate_taskbook: { id: 'regenerate_taskbook', label: '重新生成任务书', kind: 'job' },
  edit_taskbook: { id: 'edit_taskbook', label: '手动编辑任务书', kind: 'ui' },
  approve_skill_invocations: { id: 'approve_skill_invocations', label: '确认技能调用并继续', kind: 'job' },
  retry_skill_invocations: { id: 'retry_skill_invocations', label: '换一版技能调用结果', kind: 'job' },
  select_master: { id: 'select_master', label: '确认当前主图', kind: 'job' },
  review_calibration: { id: 'review_calibration', label: '人工处置', kind: 'ui' },
  enter_human_tune: { id: 'enter_human_tune', label: '进入人工微调', kind: 'ui' },
  resume_quality_inspection: { id: 'resume_quality_inspection', label: '开始重新质检', kind: 'job' },
  submit_human_tune: { id: 'submit_human_tune', label: '提交微调', kind: 'ui' },
  start_clarification: { id: 'start_clarification', label: '开始需求澄清', kind: 'job' },
  build_taskbook: { id: 'build_taskbook', label: '生成任务书', kind: 'job' },
  prepare_style_direction: { id: 'prepare_style_direction', label: '准备艺术风格', kind: 'job' },
  render_candidates: { id: 'render_candidates', label: '生成候选图', kind: 'job' },
  choose_master: { id: 'choose_master', label: '进入主图选择', kind: 'job' },
  start_quality_inspection: { id: 'start_quality_inspection', label: '开始画面质检', kind: 'job' },
  open_final_approval: { id: 'open_final_approval', label: '进入最终确认', kind: 'job' },
  start_category_match: { id: 'start_category_match', label: '开始匹配品类约束', kind: 'job' },
  edit_rework: { id: 'edit_rework', label: '修改建议后执行', kind: 'job' },
  abandon: { id: 'abandon', label: '终止且不交付', kind: 'job' },
  branch: { id: 'branch', label: '查看分支', kind: 'ui' },
  inspect: { id: 'inspect', label: '查看交付', kind: 'ui' },
};

const FINAL_APPROVAL_HINT = '人工确认';

/* 重跑分支头边界相位（与 storage.project_store._rewind_stage、main_front._capabilities
 * 对齐）：边界落在哪个节点，主区就显示哪个节点的控件骨架；capability 是该节点
 * 的空负载重启动作。skeleton 为 null 的边界（最终确认）落到节点真实界面，不需要
 * 骨架，也不参与自动重跑。 */
export const RERUN_BOUNDARIES = {
  ready_for_category_match: { skeleton: 'category', capability: 'start_category_match' },
  ready_for_clarification: { skeleton: 'clarify', capability: 'start_clarification' },
  ready_for_taskbook: { skeleton: 'taskbook', capability: 'build_taskbook' },
  ready_for_style_direction: { skeleton: 'style', capability: 'prepare_style_direction' },
  ready_for_quality_inspection: { skeleton: 'quality', capability: 'start_quality_inspection' },
  ready_for_final_approval: { skeleton: null, capability: 'open_final_approval' },
};

/** 空负载即可启动的重启能力（其余能力需要答案/选择/操作人等负载，不能一键启动）。 */
export const EMPTY_PAYLOAD_CAPABILITIES = new Set([
  'retry', 'start_category_match', 'start_clarification', 'build_taskbook',
  'prepare_style_direction', 'render_candidates', 'choose_master',
  'start_quality_inspection', 'resume_quality_inspection', 'open_final_approval',
]);

/**
 * 当前视图是否停在重跑分支头边界。
 * 返回 { phase, skeleton, capability, processing, runnable } 或 null：
 * - processing：该工程有在途 job（骨架保持 busy，不显示启动按钮）；
 * - runnable：服务端能力清单包含该边界的重启动作（可一键/自动重跑）。
 */
export function rerunBoundary(view) {
  const phase = view?.snapshot?.phase;
  const info = RERUN_BOUNDARIES[phase];
  if (!info) return null;
  const capabilities = Array.isArray(view?.capabilities) ? view.capabilities : [];
  return {
    phase,
    skeleton: info.skeleton,
    capability: info.capability,
    processing: Boolean(view?.active_job),
    runnable: capabilities.includes(info.capability),
  };
}

/* T11（契约 §11）：未知状态不得把英文 state id 原样上屏，统一兜底中文。 */
export function stateLabel(id) { return STATE_LABELS[id] || (id ? '状态未知' : '未开始'); }

export function stepIndex(snapshot) {
  const idx = WORKFLOW_STATES.findIndex((s) => s.id === snapshot?.state);
  return idx < 0 ? 0 : idx;
}

/** 任务书确认是否仍有效（T32：编辑后确认失效立即可见）。 */
export function approvalValid(snapshot) {
  const approval = snapshot?.task_approval;
  const revision = snapshot?.task_revision;
  return Boolean(approval && approval.actor && approval.revision_hash && approval.revision_hash === revision?.revision_hash);
}

/**
 * 推导当前舞台。
 * 返回 { stage, reason?, actions, waiting }；stage 取值：
 * empty | category | clarify | taskbook | style_processing | skill_approval | gallery |
 * quality_pending | calibration | disposition | annotate | reinspection | resume_quality |
 * final | failed | terminated | completed
 */
export function deriveView(view) {
  const snapshot = view?.snapshot || {};
  const manifest = view?.manifest || {};
  const capabilities = Array.isArray(view?.capabilities) ? view.capabilities : [];
  const phase = snapshot.phase;
  const stateId = snapshot.state;

  // 1. 失败（含最终确认门禁的"等待式失败"）
  if (manifest.failed_step) {
    const failure = manifest.failed_step;
    const message = String(failure?.error?.message || '');
    if (failure.state === 'final_approval' && message.includes(FINAL_APPROVAL_HINT)) {
      return { stage: 'final', actions: capabilities, waiting: true, viaFailureGate: true };
    }
    if (failure?.error?.category === 'content_moderation' && snapshot.inspection) {
      return { stage: 'calibration', failure, moderationFailure: true,
        actions: capabilities, waiting: true };
    }
    return { stage: 'failed', failure, actions: capabilities, waiting: false };
  }

  // 2. 已交付
  if (snapshot.completed) return { stage: 'completed', actions: capabilities, waiting: false };

  // 3. 各等待阶段（与后端 next_state/capabilities 对齐）
  if (stateId === 'category_constraint') return { stage: 'category', actions: capabilities,
    waiting: phase === 'waiting_category_approval', processing: Boolean(view?.active_job) };
  if (phase === 'waiting_clarification') return { stage: 'clarify', actions: capabilities, waiting: true };
  if (phase === 'waiting_clarification_review') {
    return { stage: 'clarify', actions: capabilities, waiting: true, budgetReview: true };
  }
  if (phase === 'waiting_taskbook_revision') {
    return { stage: 'taskbook', actions: capabilities, waiting: true, taskbookRevision: true };
  }
  if (stateId === 'confirmation_build' && phase === 'waiting_human_approval') {
    return { stage: 'taskbook', actions: capabilities, waiting: true };
  }
  if (stateId === 'initial_candidate_generation' && phase === 'waiting_skill_approval') {
    return { stage: 'skill_approval', actions: capabilities, waiting: true };
  }
  if (stateId === 'initial_candidate_generation' && phase !== 'candidate_generation_completed') {
    return { stage: 'style_processing', actions: capabilities, waiting: false,
      processing: Boolean(view?.active_job) };
  }
  if (phase === 'waiting_master_selection') return { stage: 'gallery', actions: capabilities, waiting: true };
  if (stateId === 'master_candidate_selection' && phase === 'master_selected') {
    return { stage: 'quality_pending', actions: capabilities, waiting: false,
      processing: Boolean(view?.active_job) };
  }
  if (stateId === 'self_check_iteration' && phase === 'waiting_human_approval') {
    if (String(snapshot.termination_reason || '').includes('round_limit')
        && Array.isArray(snapshot.available_actions) && snapshot.available_actions.length) {
      return { stage: 'disposition', actions: capabilities, waiting: true };
    }
    return { stage: 'calibration', actions: capabilities, waiting: true };
  }
  if (phase === 'waiting_human_tune') return { stage: 'annotate', actions: capabilities, waiting: true };
  if (phase === 'waiting_reinspection') return { stage: 'reinspection', actions: capabilities, waiting: true };
  if (phase === 'additional_rounds_approved') return { stage: 'resume_quality', actions: capabilities, waiting: true };
  if (phase === 'terminated_without_delivery') return { stage: 'terminated', actions: capabilities, waiting: false };
  if (phase === 'calibration_completed' && snapshot.termination_satisfied) {
    // 质检已通过：直接呈现最终确认，避免在无 final_approved 的情况下撞上门禁失败。
    return { stage: 'final', actions: capabilities, waiting: true };
  }

  // 4. 每个可持久化状态都有明确舞台，不使用通用恢复状态。
  if (stateId === 'intake_clarify') return { stage: 'clarify', actions: capabilities, waiting: false };
  if (stateId === 'confirmation_build') return { stage: 'taskbook', actions: capabilities, waiting: false };
  if (stateId === 'initial_candidate_generation') return { stage: 'gallery', actions: capabilities, waiting: false };
  if (stateId === 'master_candidate_selection') return { stage: 'quality_pending', actions: capabilities, waiting: false };
  if (stateId === 'self_check_iteration') return { stage: 'calibration', actions: capabilities, waiting: false };
  if (stateId === 'human_prompt_iteration') return { stage: 'annotate', actions: capabilities, waiting: false };
  if (stateId === 'final_approval') return { stage: 'final', actions: capabilities, waiting: true };
  if (stateId) return { stage: 'failed', failure: { state: stateId,
    error: { message: '当前阶段无法识别，请从历史检查点创建分支。' } }, actions: capabilities, waiting: false };
  return { stage: 'empty', actions: [], waiting: false };
}

/** Human-gated skill actions share the same actor requirement as final approval. */
export function skillApprovalActorState(value) {
  const actor = String(value || '').trim();
  return {
    actor,
    ready: Boolean(actor),
    message: actor
      ? `本次确认或换版将记录操作人：${actor}`
      : '请先在状态页「工程信息」填写操作人身份，才能确认或换版。',
  };
}

/** 事件类型 → 时间线可读标签（未知类型有统一兜底，不伪造语义）。 */
export const EVENT_LABELS = {
  project_created: '创建工程',
  step_started: '开始处理',
  step_succeeded: '完成节点',
  step_failed: '处理失败',
  branch_created: '创建分支',
  retry_started: '开始重试',
  task_revision_created: '任务书修订',
  inspection_started: '开始画面质检',
  inspection_completed: '完成画面质检',
  inspection_presented: '质检结果已展示',
  inspection_reused: '复用质检结果',
  inspection_schema_failed: '质检输出校验失败',
  rework_started: '开始画面返工',
  rework_completed: '完成画面返工',
  round_checkpointed: '质检轮次已保存',
  waiting_human_approval: '等待人工处置',
  calibration_terminated_without_delivery: '终止且不交付',
  calibration_current_asset_accepted: '接受当前图',
  calibration_round_limit_reached: '自动质检达到上限',
  calibration_invalidated: '质检结论已失效',
  quality_disposition: '质检分流决定',
  human_annotation_rework: '圈画微调完成',
  human_tune_final_accepted: '人工确认终稿',
  delivery_frozen: '交付已冻结',
  delivery_exported: '交付说明已导出',
  delivery_note_retried: '交付说明已重新生成',
  resource_degraded: '资源已按策略降级',
  runtime_policy_revised: '运行策略已修订',
  skill_invocation_completed: '两库调用已完成',
  skill_invocation_retried: '已重新调用两库',
  skill_invocation_approved: '技能调用已人工放行',
  category_constraint_matched: '已匹配品类约束',
  category_constraint_approved: '品类约束已人工放行',
};

export function eventLabel(event) {
  const base = EVENT_LABELS[event?.type] || '记录进度';
  const suffix = event?.state ? ` · ${stateLabel(event.state)}` : '';
  return `${base}${suffix}`;
}
