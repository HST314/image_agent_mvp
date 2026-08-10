/* 状态/动作映射（T35 验收：与后端状态表一一对应）。
 *
 * 后端事实来源（v1.7.3）：
 * - agent_core.workflow_runner.ORDER：七个生产状态；
 * - WorkflowRunner.next_state / calibrator.calibration_loop：phase 语义；
 * - main_front._capabilities：服务端能力清单，前端只把这些能力映射为动作，
 *   不自行发明后端没有的入口。
 *
 * deriveView 为纯函数：输入 project view（snapshot/manifest/capabilities），
 * 输出工作台"当前决策"区的舞台标识与可用动作，供视图层渲染。
 */

export const WORKFLOW_STATES = [
  { id: 'intake_clarify', label: '需求澄清' },
  { id: 'confirmation_build', label: '任务书' },
  { id: 'initial_candidate_generation', label: '候选生成' },
  { id: 'master_candidate_selection', label: '主图选择' },
  { id: 'self_check_iteration', label: '画面质检' },
  { id: 'human_prompt_iteration', label: '人工修改' },
  { id: 'final_approval', label: '最终交付' },
];

export const STATE_LABELS = Object.fromEntries(WORKFLOW_STATES.map((s) => [s.id, s.label]));

/** 服务端能力 → 前端动作（一一对应，不增不减）。 */
export const CAPABILITY_ACTIONS = {
  retry: { id: 'retry', label: '从上一成功点重试', kind: 'job' },
  answer_clarification: { id: 'answer_clarification', label: '提交答案并继续', kind: 'sync' },
  select_master: { id: 'select_master', label: '确认当前主图', kind: 'job' },
  review_calibration: { id: 'review_calibration', label: '人工处置', kind: 'ui' },
  resume_quality_inspection: { id: 'resume_quality_inspection', label: '开始重新质检', kind: 'job' },
  submit_human_tune: { id: 'submit_human_tune', label: '提交微调', kind: 'ui' },
  resume: { id: 'resume', label: '继续工作流', kind: 'job' },
  branch: { id: 'branch', label: '查看分支', kind: 'ui' },
  inspect: { id: 'inspect', label: '查看交付', kind: 'ui' },
};

const FINAL_APPROVAL_HINT = '人工确认';

export function stateLabel(id) { return STATE_LABELS[id] || id || '未开始'; }

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
 * empty | clarify | taskbook | gallery | calibration | disposition | annotate |
 * reinspection | resume_quality | final | failed | terminated | completed | resume
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
    return { stage: 'failed', failure, actions: capabilities, waiting: false };
  }

  // 2. 已交付
  if (snapshot.completed) return { stage: 'completed', actions: capabilities, waiting: false };

  // 3. 各等待阶段（与后端 next_state/capabilities 对齐）
  if (phase === 'waiting_clarification') return { stage: 'clarify', actions: capabilities, waiting: true };
  if (stateId === 'confirmation_build' && phase === 'waiting_human_approval') {
    return { stage: 'taskbook', actions: capabilities, waiting: true };
  }
  if (phase === 'waiting_master_selection') return { stage: 'gallery', actions: capabilities, waiting: true };
  if (stateId === 'self_check_iteration' && phase === 'waiting_human_approval') {
    if (Array.isArray(snapshot.available_actions) && snapshot.available_actions.length) {
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

  // 4. 有快照但无明确等待：可继续
  if (stateId) return { stage: 'resume', actions: capabilities, waiting: false };
  return { stage: 'empty', actions: [], waiting: false };
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
  model_call_unknown: '付费调用结果未知',
  runtime_policy_revised: '运行策略已修订',
};

export function eventLabel(event) {
  const base = EVENT_LABELS[event?.type] || '记录进度';
  const suffix = event?.state ? ` · ${stateLabel(event.state)}` : '';
  return `${base}${suffix}`;
}
