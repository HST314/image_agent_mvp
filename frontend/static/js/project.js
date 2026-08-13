/* 工作台视图（契约 §3）：创作进度卡 + 当前阶段工作区。
 * T2/T3 起，工程信息/最近活动/原始任务迁入状态页（statuspage.js），运行策略
 * 迁入设置页（settings.js），工作台不再展示这四张卡片。
 * 长任务一律经后台 job + SSE 序号续传，页面在长任务期间保持可操作（T35）。 */

import { $, el, toast, stateBlock, icons, sectionPanel } from './dom.js';
import { state, patch, getActor } from './store.js';
import * as api from './api.js';
import { viewOperations, createJobRunner, isTerminalJobStatus } from './jobrunner.js';
import { renderMarkdownInto } from './markdown.js';
import { deriveView, skillApprovalActorState, stateLabel } from './states.js';
import { createTimelineFollower } from './stepstatus.js';
import { renderClarify } from './clarify.js';
import { renderTaskbook } from './taskbook.js';
import { renderGalleryStage } from './gallery.js';
import { createAnnotator } from './annotate.js';
import { renderJobProgress } from './history.js';
import { markActiveTab, setTopContext } from './topnav.js';
import { errorText, terminationReasonLabel } from './copy.js';
import { renderProgressSteps, renderSkillInvocations } from './snapshots.js';

const MANUAL_ACTIONS = [
  { id: 'execute', label: '执行建议', primary: true },
  { id: 'edit_and_execute', label: '修改后执行', needsDelta: true },
  { id: 'accept_current', label: '接受当前图' },
  { id: 'skip', label: '跳过本轮' },
  { id: 'end', label: '终止且不交付', danger: true },
];

/* 当前活动的视图操作（全局唯一，见 jobrunner.js）。视图切换/重渲染/回首页
 * 时中止旧操作，避免旧 job 的回调覆盖用户正在浏览的页面（H1 竞态修复）。 */

/** 离开工程视图（回首页等）时调用：中止仍在进行中的操作与 job 跟踪循环。 */
export function stopJobTracking() { viewOperations.leave(); }

export function renderProject(view, { autostartBootstrap = false } = {}) {
  viewOperations.begin();
  /* 渲染工程即回到工作区视图，并同步顶栏「工程名 · 分支」标识（T1）。 */
  patch({ current: view, view: 'workspace' });
  markActiveTab('workspace');
  const content = $('#content');
  content.textContent = '';
  const snapshot = view.snapshot || {};
  const manifest = view.manifest || {};
  const projectId = view.project_id;
  const derived = deriveView(view);

  setTopContext({ projectId, branch: manifest.current_branch });

  const actor = getActor();

  const projectOffline = view.runtime_policy?.offline_mode === true;
  if (projectOffline) {
    const banner = el('div', { class: 'offline-banner', role: 'status' });
    banner.append(el('span', { 'aria-hidden': 'true' }), el('span', {}, [el('strong', { text: '离线测试模式：' }), '生成结果为模拟资产，不可用于最终交付。']));
    banner.firstChild.innerHTML = icons.info;
    content.append(banner);
  }

  /* ===== 实时进度 ===== */
  const progressSection = sectionPanel('创作进度', `当前分支 ${manifest.current_branch || 'main'} · 检查点 ${manifest.current_checkpoint?.sequence || 0}`);
  const stepper = el('div', { class: 'stepper', 'aria-label': '工作流进度' });
  renderProgressSteps(stepper, view, { onBranchCreated: renderProject });
  progressSection.append(stepper);
  const jobBox = el('div', { style: 'margin-top:14px' });
  progressSection.append(jobBox);
  content.append(progressSection);

  const workspace = el('div', { class: 'workspace' });
  const primary = el('div', { class: 'workspace__primary' });
  const rail = el('div', { class: 'workspace__rail' });
  workspace.append(primary, rail);
  content.append(workspace);

  /* ===== 当前决策（舞台） ===== */
  const stagePanel = sectionPanel(stageTitle(derived), stageSubtitle(derived));
  stagePanel.classList.add('stage');
  primary.append(stagePanel);
  const refresh = (next) => { if (next) renderProject(next); else openProject(projectId); };
  const jobRunner = makeJobRunner(jobBox, projectId, refresh, content);

  renderStage(stagePanel, view, derived, { projectId, actor, refresh, jobRunner });

  /* ===== 付费调用待处置（契约 §3：工作台仅保留进度卡与阶段工作区；
   * 工程信息/最近活动/原始任务迁入状态页，运行策略迁入设置页） ===== */
  const unknowns = view.unknown_actions || [];
  if (unknowns.length) rail.append(renderUnknowns(unknowns, { projectId, refresh }));
  if (!rail.children.length) workspace.classList.add('workspace--solo');

  /* 恢复进行中的 job 展示（刷新后） */
  const activeJob = view.active_job || (state.job && state.job.project_id === projectId && !isTerminalJobStatus(state.job.status) ? state.job : null);
  if (activeJob) {
    patch({ job: activeJob });
    jobRunner.attach(activeJob);
  } else if (autostartBootstrap && derived.stage === 'empty' && !manifest.failed_step) {
    /* T10（契约 §7/Q10-A）：创建后立即跳工作台，首个推进经 jobs 异步启动；
     * 幂等键走 M1 机制（intent=bootstrap，指纹=空检查点+空负载），重复点击
     * 或刷新重进不会重复执行（后端 JOBS.submit 按工程+键去重）。 */
    jobRunner.start({}, { intent: 'bootstrap', operation: '初始化工程' });
  }
}

/* ---------- 舞台 ---------- */

function stageTitle(derived) {
  return {
    clarify: '需求澄清', taskbook: '创作任务书', skill_approval: '技能调用人工把关',
    gallery: '选择主图', calibration: '画面自检与人工放行',
    disposition: '自动质检已达上限', annotate: '圈画微调', reinspection: '等待重新质检',
    resume_quality: '追加质检已确认', final: '最终确认', failed: '流程已暂停', terminated: '已终止且不交付',
    completed: '最终交付', resume: '继续工作流', empty: '工程已保存',
  }[derived.stage] || '当前决策';
}

function stageSubtitle(derived) {
  if (derived.stage === 'taskbook') return '请通读任务目标与约束；确认后开始生成候选图';
  if (derived.stage === 'skill_approval') return '确认两库结果后才会生成五张主图；不合适可换一版';
  if (derived.stage === 'gallery') return '对比五个视觉方向，放大查看细节后选择一个作为主图';
  if (derived.stage === 'disposition') return '自动质检达到配置上限；最高分不会冒充通过，请选择分流方式';
  if (derived.stage === 'final') return '确认后冻结交付并生成说明；此后任何修改都将创建新修订';
  if (derived.stage === 'completed') return '最终图片与设计说明已冻结；确认完成后保存到工程交付目录';
  return '工作流在需要你决策时暂停；每个动作都会进入审计事件';
}

function renderStage(panel, view, derived, ctx) {
  const snapshot = view.snapshot || {};
  const { projectId, actor, refresh, jobRunner } = ctx;
  const stage = derived.stage;

  if (stage === 'clarify') {
    renderClarify(panel, view, { projectId, onSubmitted: refresh });
    return;
  }
  if (stage === 'taskbook') {
    renderTaskbook(panel, view, { projectId, actor, onChanged: refresh, jobRunner });
    return;
  }
  if (stage === 'skill_approval') {
    renderSkillApproval(panel, view, ctx);
    return;
  }
  if (stage === 'gallery') {
    let selectedId = null;
    const { confirmButton } = renderGalleryStage(panel, view, {
      projectId, selectedId,
      onSelect(slot) {
        selectedId = slot.asset.id || `candidate-${slot.index + 1}`;
        confirmButton.disabled = false;
        confirmButton.textContent = `确认方向 ${slot.index + 1} 为主图`;
      },
      onCompensate() { jobRunner.start({}, { intent: 'candidate-compensation' }); },
    });
    confirmButton.addEventListener('click', () => {
      if (selectedId) jobRunner.start({ selected_id: selectedId }, { intent: 'select' });
    });
    return;
  }
  if (stage === 'calibration') {
    renderCalibration(panel, view, ctx);
    return;
  }
  if (stage === 'disposition') {
    renderDisposition(panel, view, ctx);
    return;
  }
  if (stage === 'annotate') {
    renderAnnotateStage(panel, view, ctx);
    return;
  }
  if (stage === 'reinspection') {
    const btn = el('button', { type: 'button', class: 'btn btn--primary', text: '开始重新质检' });
    btn.addEventListener('click', () => jobRunner.start({}));
    panel.append(stateBlock('empty', '修改已保存，等待重新质检', '将使用真实视觉模型检查最新资产，不会复用旧质检结论。', btn));
    return;
  }
  if (stage === 'resume_quality') {
    const btn = el('button', { type: 'button', class: 'btn btn--primary', text: '继续追加的质检轮次' });
    btn.addEventListener('click', () => jobRunner.start({}));
    panel.append(stateBlock('empty', '追加轮次已确认', '继续后将执行已确认费用的追加质检。', btn));
    return;
  }
  if (stage === 'final') {
    renderFinal(panel, view, ctx);
    return;
  }
  if (stage === 'failed') {
    const failure = derived.failure || {};
    let btn;
    if (!view.manifest?.current_checkpoint) {
      /* T10：首个推进失败且尚无成功检查点时，同步 retry 契约不可用（后端要求
       * 已有检查点）；改为重新引导——jobs 引导路径从持久化任务卡重启首个推进，
       * 成功后 checkpoint 自动清除失败标记。 */
      btn = el('button', { type: 'button', class: 'btn btn--primary', text: '重新启动创作流程' });
      btn.addEventListener('click', () => jobRunner.start({}, { intent: 'bootstrap', operation: '初始化工程' }));
    } else if (derived.actions.includes('retry')) {
      btn = el('button', { type: 'button', class: 'btn btn--primary', text: '从上一成功点重试' });
      btn.addEventListener('click', () => jobRunner.retry({}));
    }
    panel.append(stateBlock('error', `流程在 ${stateLabel(failure.state)} 暂停`, failure?.error?.message ? errorText(failure.error.message) : '后端能力暂不可用。修正模型、密钥或依赖后可安全重试。', btn));
    return;
  }
  if (stage === 'terminated') {
    panel.append(stateBlock('empty', '已终止且不交付', '该工程已按人工决定终止；可从历史分支重新开始。'));
    return;
  }
  if (stage === 'completed') {
    renderDeliveryStage(panel, view, ctx);
    return;
  }
  // empty（T10：defer_run 创建后未启动/启动失败）与 resume（有检查点可继续）
  if (stage === 'empty') {
    const btn = el('button', { type: 'button', class: 'btn btn--primary', text: '启动创作流程' });
    btn.addEventListener('click', () => jobRunner.start({}, { intent: 'bootstrap', operation: '初始化工程' }));
    panel.append(stateBlock('empty', '工程已创建', '启动后系统开始理解任务书并生成澄清问题，进度在上方状态区实时显示。', btn));
    return;
  }
  const btn = el('button', { type: 'button', class: 'btn btn--primary', text: '继续工作流' });
  btn.addEventListener('click', () => jobRunner.start({}));
  panel.append(stateBlock('empty', stateLabel(snapshot.state) || '工程已保存', '从当前检查点继续推进。若外部模型或密钥不可用，系统会保存真实错误供恢复。', btn));
}

function renderSkillApproval(panel, view, { projectId, actor, jobRunner }) {
  const snapshot = view.snapshot || {};
  const current = snapshot.skill_invocation_current || {};
  const history = Array.isArray(snapshot.skill_invocation_history) ? snapshot.skill_invocation_history : [];
  panel.append(el('div', { class: 'skill-gate__head' }, [
    el('div', {}, [
      el('span', { class: 'badge badge--warning', text: '等待人工放行' }),
      el('h3', { text: `技能调用版本 ${current.version || history.length || 1}` }),
      el('p', { text: `已保留 ${history.length} 个版本供审计；当前结果确认前不会发起五图生成。` }),
    ]),
  ]));
  renderSkillInvocations(panel, projectId, snapshot);
  if (history.length) {
    const audit = el('details', { class: 'skill-gate__history' });
    audit.append(el('summary', { text: `查看 ${history.length} 个技能调用审计版本` }));
    const versions = el('div', { class: 'skill-gate__versions' });
    const decisionLabel = { pending: '等待确认', rejected: '已否决', approved: '已批准', auto_approved: '自动放行' };
    history.forEach((version) => {
      const item = el('details', { class: 'skill-gate__version' });
      item.append(el('summary', {
        text: `版本 ${version.version || '—'} · ${decisionLabel[version.decision] || '已记录'}`,
      }));
      const body = el('div', { class: 'skill-gate__version-body' });
      renderSkillInvocations(body, projectId, version);
      item.append(body);
      versions.append(item);
    });
    audit.append(versions);
    panel.append(audit);
  }

  const actorState = skillApprovalActorState(actor);
  const actorHelpId = 'skill-gate-actor-help';
  const approve = el('button', { type: 'button', class: 'btn btn--primary', text: '确认结果并生成 5 张主图', 'aria-describedby': actorHelpId });
  const retry = el('button', { type: 'button', class: 'btn btn--secondary', text: 'Retry · 换一版结果', 'aria-describedby': actorHelpId });
  approve.disabled = !actorState.ready;
  retry.disabled = !actorState.ready;
  approve.addEventListener('click', () => jobRunner.start(
    { skill_action: 'approve', actor: actorState.actor },
    { intent: 'skill-approve', operation: '确认技能调用并生成五张主图' },
  ));
  retry.addEventListener('click', () => {
    if (!window.confirm('确定否决当前两库结果并重新调用？上一版会保留在历史记录中，新结果仍需人工确认。')) return;
    jobRunner.start(
      { skill_action: 'retry', actor: actorState.actor },
      { intent: 'skill-retry', operation: '重新调用两库' },
    );
  });
  panel.append(el('div', { class: 'skill-gate__actions' }, [
    el('div', {}, [
      el('strong', { text: '人工门禁' }),
      el('p', { text: 'Retry 会携带上一版的品类与五张风格卡作为排除上下文。' }),
      el('p', {
        id: actorHelpId,
        class: actorState.ready ? 'skill-gate__actor' : 'field-error',
        role: actorState.ready ? 'status' : 'alert',
        text: actorState.message,
      }),
    ]),
    el('div', { class: 'button-row' }, [retry, approve]),
  ]));
}

function renderCalibration(panel, view, { projectId, refresh, jobRunner }) {
  const snapshot = view.snapshot || {};
  const inspection = snapshot.inspection || {};
  const asset = snapshot.asset || snapshot.current_asset || snapshot.master_asset;
  const layout = el('div', { class: 'inspection-layout' });
  const visual = el('div', { class: 'inspection-visual' });
  const details = el('div', { class: 'inspection-details' });
  const badge = inspection.passed
    ? el('span', { class: 'badge badge--success', text: '建议通过' })
    : el('span', { class: 'badge badge--warning', text: '建议修改' });
  const summary = el('div', { class: 'inspection-summary' }, [
    el('div', {}, [
      el('span', { class: 'inspection-round', text: `第 ${snapshot.round || 1} 轮自检` }),
      el('h3', { text: '本轮自检结论' }),
    ]),
    badge,
  ]);
  details.append(summary, el('p', { class: 'inspection-recommendation', text: inspection.rework_prompt_delta || '请审阅当前图像与自检结果。' }));
  if (Array.isArray(inspection.deviations) && inspection.deviations.length) {
    details.append(el('h4', { text: '发现的问题' }));
    const ul = el('ul', { class: 'inspection-findings' });
    inspection.deviations.forEach((d) => ul.append(el('li', { text: d })));
    details.append(ul);
  } else {
    details.append(el('p', { class: 'inspection-empty', text: '本轮未发现需要单独列出的偏差。' }));
  }
  if (asset) {
    const url = api.assetUrl(projectId, asset);
    if (url) visual.append(el('img', { src: url, alt: '当前待审图像', loading: 'lazy', decoding: 'async' }));
  }
  if (!visual.children.length) visual.append(stateBlock('empty', '当前图像不可预览', '可依据右侧自检结果继续处置。'));
  layout.append(visual, details);
  panel.append(layout);

  const actionSection = el('div', { class: 'decision-section' }, [
    el('div', {}, [el('h3', { text: '选择下一步' }), el('p', { text: '建议优先执行自检建议；所有决定都会写入审计记录。' })]),
  ]);
  const row = el('div', { class: 'decision-actions' });
  for (const action of MANUAL_ACTIONS) {
    const btn = el('button', {
      type: 'button',
      class: `btn ${action.primary ? 'btn--primary' : action.danger ? 'btn--danger' : 'btn--secondary'}`,
      text: action.label,
    });
    btn.addEventListener('click', () => {
      if (action.needsDelta) {
        openTextActionDialog({
          title: '修改自检建议后执行',
          description: '写明需要调整的内容；提交后将据此生成下一版图像。',
          label: '修改建议',
          placeholder: inspection.rework_prompt_delta || '例如：保留构图，将标题层级拉开并降低背景干扰',
          submitLabel: '提交并执行',
          onSubmit: (delta) => jobRunner.start({ manual_action: action.id, edited_delta: delta }, { intent: 'manual' }),
        });
        return;
      }
      if (action.danger && !window.confirm('确定终止本工程且不交付？该决定会进入审计事件。')) return;
      jobRunner.start({ manual_action: action.id }, { intent: 'manual' });
    });
    row.append(btn);
  }
  actionSection.append(row);
  panel.append(actionSection);
}

function renderDisposition(panel, view, { projectId, refresh, jobRunner }) {
  const snapshot = view.snapshot || {};
  const asset = snapshot.best_asset || snapshot.asset;
  const inspection = snapshot.inspection || {};
  const layout = el('div', { class: 'inspection-layout' });
  const visual = el('div', { class: 'inspection-visual' });
  const details = el('div', { class: 'inspection-details' });
  details.append(
    el('span', { class: 'badge badge--warning', text: terminationReasonLabel(snapshot.termination_reason) }),
    el('h3', { text: `已完成第 ${snapshot.round || 1} 轮自检` }),
    el('p', { class: 'inspection-recommendation', text: '自动自检已按策略停止，请明确选择继续投入、人工微调、接受当前图或放弃交付。' }),
  );
  if (Array.isArray(inspection.deviations) && inspection.deviations.length) {
    details.append(el('h4', { text: '仍需关注' }));
    const ul = el('ul', { class: 'inspection-findings' });
    inspection.deviations.forEach((d) => ul.append(el('li', { text: d })));
    details.append(ul);
  }
  if (asset) {
    const url = api.assetUrl(projectId, asset);
    if (url) visual.append(el('img', { src: url, alt: '本轮最高分图像', loading: 'lazy', decoding: 'async' }));
  }
  if (!visual.children.length) visual.append(stateBlock('empty', '当前图像不可预览', '仍可依据自检结论选择后续处置。'));
  layout.append(visual, details);
  panel.append(layout);
  const row = el('div', { class: 'decision-actions decision-actions--limit' });

  const addBtn = el('button', { type: 'button', class: 'btn btn--primary', text: '追加自检轮次' });
  addBtn.addEventListener('click', () => openAdditionalRoundsDialog(async (rounds) => {
    await api.qualityDisposition(projectId, { action: 'add_rounds_with_cost_confirmation', additional_rounds: rounds, cost_confirmed: true });
    toast('已确认追加轮次。');
    refresh();
  }));

  const tuneBtn = el('button', { type: 'button', class: 'btn btn--secondary', text: '以本轮最高分进入人工微调' });
  tuneBtn.addEventListener('click', async () => {
    try {
      await api.qualityDisposition(projectId, { action: 'human_tune_best' });
      toast('已进入人工微调。');
      refresh();
    } catch (error) { toast(error.message, 'error'); }
  });

  const acceptBtn = el('button', { type: 'button', class: 'btn btn--secondary', text: '接受当前图' });
  acceptBtn.addEventListener('click', () => {
    jobRunner.start({ manual_action: 'accept_current' }, { intent: 'accept-current' });
  });

  const abandonBtn = el('button', { type: 'button', class: 'btn btn--danger', text: '放弃且不交付' });
  abandonBtn.addEventListener('click', async () => {
    if (!window.confirm('确定放弃本工程？该决定会进入审计事件。')) return;
    try {
      await api.qualityDisposition(projectId, { action: 'abandon' });
      toast('已放弃交付。');
      refresh();
    } catch (error) { toast(error.message, 'error'); }
  });

  row.append(addBtn, tuneBtn, acceptBtn, abandonBtn);
  panel.append(el('div', { class: 'decision-section' }, [
    el('div', {}, [el('h3', { text: '选择后续处置' }), el('p', { text: '继续投入会产生新的模型调用；接受或放弃将结束自动自检。' })]),
    row,
  ]));
}

function renderAnnotateStage(panel, view, { projectId, refresh, jobRunner }) {
  const snapshot = view.snapshot || {};
  const asset = snapshot.current_asset || snapshot.asset || snapshot.best_asset;
  panel.append(el('p', { class: 'hint', text: '当前已进入人工微调；修改后仍停留在本界面，可继续多轮微调或确定终稿，不再返回自动质检。' }));
  if (!asset) {
    panel.append(stateBlock('error', '没有可微调的资产', '当前快照缺少可标注图像。'));
    return;
  }
  const busy = el('div', { class: 'job-progress', role: 'status', style: 'display:none;margin-bottom:14px' }, [
    el('span', { class: 'spinner', 'aria-hidden': 'true' }),
    el('span', { text: '模型正在修改…' }),
  ]);
  panel.append(busy);
  const setTuneBusy = (flag) => {
    busy.style.display = flag ? '' : 'none';
    panel.querySelectorAll('button, textarea, input').forEach((node) => { node.disabled = flag; });
  };
  const tabList = el('div', { class: 'tune-tabs', role: 'tablist', 'aria-label': '人工修改方式' });
  const drawTab = el('button', { type: 'button', class: 'tune-tab', role: 'tab', id: 'tune-tab-draw', 'aria-controls': 'tune-panel-draw', 'aria-selected': 'true', tabindex: '0', text: '圈画微调' });
  const textTab = el('button', { type: 'button', class: 'tune-tab', role: 'tab', id: 'tune-tab-text', 'aria-controls': 'tune-panel-text', 'aria-selected': 'false', tabindex: '-1', text: '纯文字微调' });
  const drawPanel = el('div', { class: 'tune-panel', role: 'tabpanel', id: 'tune-panel-draw', 'aria-labelledby': 'tune-tab-draw' });
  const textPanel = el('div', { class: 'tune-panel', role: 'tabpanel', id: 'tune-panel-text', 'aria-labelledby': 'tune-tab-text', hidden: 'hidden' });
  const tabs = [drawTab, textTab];
  const panels = [drawPanel, textPanel];
  const activateTab = (index, focus = false) => {
    tabs.forEach((tab, i) => {
      const active = i === index;
      tab.setAttribute('aria-selected', String(active));
      tab.setAttribute('tabindex', active ? '0' : '-1');
      if (active) panels[i].removeAttribute('hidden');
      else panels[i].setAttribute('hidden', 'hidden');
    });
    if (focus) tabs[index].focus();
  };
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => activateTab(index));
    tab.addEventListener('keydown', (event) => {
      let next = null;
      if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
      if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = tabs.length - 1;
      if (next !== null) { event.preventDefault(); activateTab(next, true); }
    });
  });
  tabList.append(...tabs);
  panel.append(tabList, drawPanel, textPanel);

  createAnnotator(drawPanel, {
    projectId,
    asset,
    history: view.history || [],
    onSubmitted: refresh,
    onBusy: setTuneBusy,
  });
  const textArea = el('textarea', { class: 'input', id: 'text-tune-prompt', 'aria-describedby': 'text-tune-help', placeholder: '例如：整体提高明度，标题向上移动，并保持主体比例不变' });
  const btn = el('button', { type: 'button', class: 'btn btn--secondary', text: '提交文字微调' , style: 'margin-top:10px' });
  btn.addEventListener('click', async () => {
    const prompt = textArea.value.trim();
    if (!prompt) { toast('请填写微调说明。', 'error'); return; }
    setTuneBusy(true);
    const job = await jobRunner.start({ human_prompt: prompt }, { intent: 'tune' });
    if (!job) setTuneBusy(false);
  });
  textPanel.append(
    el('div', { class: 'tune-text-intro' }, [el('h3', { text: '用文字描述整体修改方向' }), el('p', { text: '适合不需要精确圈选区域的构图、色彩与文案调整。' })]),
    el('div', { class: 'field' }, [
      el('label', { for: 'text-tune-prompt', text: '修改说明' }),
      textArea,
      el('small', { id: 'text-tune-help', text: '说明越具体，模型越容易保持无需改变的部分。' }),
    ]),
    btn,
  );

  const finalBtn = el('button', { type: 'button', class: 'btn btn--primary', text: '确定当前图片为终稿' });
  finalBtn.addEventListener('click', async () => {
    if (!window.confirm('确定将当前图片作为终稿？确认后将冻结交付。')) return;
    setTuneBusy(true);
    const job = await jobRunner.start({ manual_action: 'accept_current', final_approved: true }, { intent: 'human-final' });
    if (!job) setTuneBusy(false);
  });
  panel.append(el('div', { class: 'tune-final-bar' }, [
    el('div', {}, [el('strong', { text: '当前图片已符合要求？' }), el('small', { text: '确定后将进入最终交付确认。' })]),
    finalBtn,
  ]));
}

function openTextActionDialog({ title, description, label, placeholder, submitLabel, onSubmit }) {
  const dialog = el('dialog', { class: 'dialog', 'aria-labelledby': 'text-action-title' });
  const input = el('textarea', { class: 'input', id: 'text-action-input', placeholder, 'aria-describedby': 'text-action-help' });
  const error = el('div', { class: 'field-error', role: 'alert' });
  dialog.append(
    el('div', { class: 'dialog__head' }, [el('h2', { id: 'text-action-title', text: title })]),
    el('div', { class: 'dialog__body' }, [
      el('p', { id: 'text-action-help', text: description }),
      el('div', { class: 'field' }, [el('label', { for: 'text-action-input', text: label }), input, error]),
    ]),
  );
  const foot = el('div', { class: 'dialog__foot' });
  const cancel = el('button', { type: 'button', class: 'btn btn--secondary', text: '取消' });
  const submit = el('button', { type: 'button', class: 'btn btn--primary', text: submitLabel });
  cancel.addEventListener('click', () => dialog.close());
  submit.addEventListener('click', () => {
    const value = input.value.trim();
    if (!value) { error.textContent = `请填写${label}。`; input.focus(); return; }
    dialog.close();
    onSubmit(value);
  });
  foot.append(cancel, submit);
  dialog.append(foot);
  dialog.addEventListener('close', () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
  input.focus();
}

function openAdditionalRoundsDialog(onSubmit) {
  const dialog = el('dialog', { class: 'dialog', 'aria-labelledby': 'rounds-dialog-title' });
  const input = el('input', { class: 'input', id: 'additional-rounds', type: 'number', min: '1', max: '20', step: '1', value: '2' });
  const confirm = el('input', { type: 'checkbox', id: 'rounds-cost-confirm' });
  const error = el('div', { class: 'field-error', role: 'alert' });
  dialog.append(
    el('div', { class: 'dialog__head' }, [el('h2', { id: 'rounds-dialog-title', text: '追加自检轮次' })]),
    el('div', { class: 'dialog__body' }, [
      el('p', { text: '追加后会继续调用模型进行生成与自检，请先核对轮数和费用影响。' }),
      el('div', { class: 'field' }, [el('label', { for: 'additional-rounds', text: '追加轮数（1–20）' }), input]),
      el('label', { class: 'switch-row' }, [confirm, el('span', {}, [el('strong', { text: '我已知晓会产生真实模型调用费用' }), el('span', { text: '确认后立即进入追加轮次。' })])]),
      error,
    ]),
  );
  const foot = el('div', { class: 'dialog__foot' });
  const cancel = el('button', { type: 'button', class: 'btn btn--secondary', text: '取消' });
  const submit = el('button', { type: 'button', class: 'btn btn--primary', text: '确认追加' });
  cancel.addEventListener('click', () => dialog.close());
  submit.addEventListener('click', async () => {
    const rounds = Number(input.value);
    if (!Number.isInteger(rounds) || rounds < 1 || rounds > 20) { error.textContent = '轮数需为 1–20 的整数。'; input.focus(); return; }
    if (!confirm.checked) { error.textContent = '请先确认费用影响。'; confirm.focus(); return; }
    submit.disabled = true;
    try { await onSubmit(rounds); dialog.close(); }
    catch (cause) { error.textContent = cause.message; submit.disabled = false; }
  });
  foot.append(cancel, submit);
  dialog.append(foot);
  dialog.addEventListener('close', () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
  input.focus();
}

function renderFinal(panel, view, { projectId, actor, refresh, jobRunner }) {
  const snapshot = view.snapshot || {};
  const asset = snapshot.final_asset || snapshot.current_asset || snapshot.asset;
  panel.append(el('p', { class: 'hint', text: '确认后将冻结交付并生成最终说明；冻结后的资产不可覆盖。' }));
  if (asset) {
    const url = api.assetUrl(projectId, asset);
    if (url) panel.append(el('img', { src: url, alt: '待最终确认图像', style: 'max-width:420px;width:100%;border-radius:12px', loading: 'lazy' }));
  }
  const approve = el('button', { type: 'button', class: 'btn btn--primary', text: '确认最终交付', style: 'margin-top:14px' });
  approve.disabled = !actor;
  approve.addEventListener('click', async () => {
    if (!window.confirm(`以操作人 ${actor} 确认最终交付？确认后冻结资产与质检版本。`)) return;
    approve.disabled = true;
    if (view.manifest?.failed_step) {
      await jobRunner.retry({ final_approved: true });
      return;
    }
    const job = await jobRunner.start({ final_approved: true }, { intent: 'final' });
    if (!job) approve.disabled = false;
  });
  panel.append(approve);
  if (!actor) panel.append(el('small', { text: '请先在状态页「工程信息」填写操作人身份。' }));
}

function renderDeliveryStage(panel, view, { projectId }) {
  const snapshot = view.snapshot || {};
  const rehearsal = snapshot.offline_rehearsal_completed === true;
  const asset = snapshot.final_asset;
  if (rehearsal || !asset) {
    panel.append(stateBlock('empty', '离线演练已完成最终验收', '模拟资产只用于流程验收，不会保存为正式交付文件。'));
    return;
  }
  const url = api.assetUrl(projectId, asset);
  const layout = el('div', { class: 'delivery-layout' });
  const visual = el('figure', { class: 'delivery-visual' });
  if (url) visual.append(el('img', { src: url, alt: '最终交付图片', decoding: 'async' }));
  else visual.append(stateBlock('error', '最终图片暂不可预览', '请检查工程图片资源后重试。'));

  const notePanel = el('div', { class: 'delivery-note' });
  const envelope = snapshot.delivery_envelope;
  const note = envelope?.design_note || {};
  notePanel.append(el('h3', { text: '设计说明' }));
  const sections = [
    ['设计理念', note.concept],
    ['选择理由', note.selection_reason],
    ['任务适配', note.task_fit],
  ].filter(([, value]) => value);
  if (sections.length) {
    for (const [title, value] of sections) {
      notePanel.append(el('section', { class: 'delivery-note__section' }, [el('h4', { text: title }), el('p', { text: value })]));
    }
  } else {
    const markdown = el('div', { class: 'markdown-body' });
    renderMarkdownInto(markdown, envelope?.design_note_markdown || '最终图片已经过候选筛选、自检与人工确认。');
    notePanel.append(markdown);
  }
  layout.append(visual, notePanel);
  panel.append(layout);

  const finalized = view.delivery_status?.finalized === true
    && view.delivery_status?.asset_sha256 === asset.sha256;
  const status = el('div', {
    class: `delivery-complete__status ${finalized ? 'is-complete' : ''}`,
    role: 'status',
    text: finalized ? '最终图片与设计说明已保存到工程交付目录。' : '点击完成，将最终图片和设计说明保存到工程交付目录。',
  });
  const complete = el('button', {
    type: 'button', class: 'btn btn--primary',
    text: finalized ? '已完成并保存' : '完成并保存到本地',
    disabled: finalized ? 'disabled' : null,
  });
  complete.addEventListener('click', async () => {
    complete.disabled = true;
    complete.textContent = '正在保存…';
    try {
      await api.finalizeDelivery(projectId);
      complete.textContent = '已完成并保存';
      status.textContent = '最终图片与设计说明已保存到工程交付目录。';
      status.classList.add('is-complete');
      toast('交付文件已保存。');
    } catch (error) {
      complete.disabled = false;
      complete.textContent = '完成并保存到本地';
      status.textContent = error.message;
      status.classList.remove('is-complete');
      toast(error.message, 'error');
    }
  });
  panel.append(el('div', { class: 'delivery-complete' }, [status, complete]));
}

/* ---------- 侧栏 ---------- */

function renderUnknowns(items, { projectId, refresh }) {
  const panel = sectionPanel('付费调用待处置', '结果未知时不会自动重试');
  const tpl = document.getElementById('tpl-unknown-action');
  for (const item of items) {
    const node = tpl.content.firstElementChild.cloneNode(true);
    node.querySelector('.unknown-action__trace').textContent = item.trace_id || item.idempotency_key;
    for (const btn of node.querySelectorAll('[data-unknown]')) {
      btn.dataset.key = item.idempotency_key;
      btn.addEventListener('click', async () => {
        const actor = getActor();
        if (!actor) { toast('请先在状态页「工程信息」填写操作人身份。', 'error'); return; }
        btn.disabled = true;
        try {
          await api.resolveUnknown(projectId, item.idempotency_key, { action: btn.dataset.unknown, actor });
          toast('人工处置已记录。');
          refresh();
        } catch (error) { toast(error.message, 'error'); btn.disabled = false; }
      });
    }
    panel.append(node);
  }
  return panel;
}

/* ---------- job 运行器 ---------- */

function makeJobRunner(box, projectId, refresh, actionRoot) {
  const runner = createJobRunner({
    projectId,
    renderProgress: (job, handlers) => renderJobProgress(box, job, handlers),
    clearProgress: () => { box.textContent = ''; },
    setBusy: (flag) => {
      actionRoot.setAttribute('aria-busy', String(flag));
      actionRoot.querySelectorAll('button, textarea, input, select').forEach((node) => { node.disabled = flag; });
    },
    notify: toast,
    // 延时刷新的世代守卫在 jobrunner 内完成；此处再校验拉取期间世代未变。
    refresh: (op) => openProject(projectId, op),
    getProjectId: () => state.current?.project_id,
    getCheckpoint: () => state.current?.manifest?.current_checkpoint?.sequence ?? '',
    postJob: (body, opts) => api.startAdvanceJob(projectId, body, opts),
    track: api.trackJob,
    cancelJob: api.cancelJob,
    onJobRecord: (record) => patch({ job: record }),
    /* T10（契约 §7）：job 运行期间跟随工程 timeline，把真实步骤文案推到进度行。
     * 游标从视图已载历史尾部起（向前回退 30 条兜底在途 step_started），只读新增
     * 事件；signal 随操作世代中止，job 终态由 jobrunner 调 stop。 */
    startLiveStatus: (job, { signal, onText }) => {
      const historyLen = Array.isArray(state.current?.history) ? state.current.history.length : 0;
      const follower = createTimelineFollower({
        fetchPage: (after, { signal: pageSignal } = {}) => api.getTimeline(projectId, { after, signal: pageSignal }),
        signal,
        onText,
        initialAfter: Math.max(0, historyLen - 30),
      });
      return follower.stop;
    },
  });
  return {
    attach: runner.attach,
    start: runner.start,
    async retry(payload) {
      // 失败重试沿用同步 retry（后端在失败点恢复；付费动作仍经生产锁与幂等键保护）。
      try {
        const updated = await api.retryProject(projectId, payload);
        toast('已从上一成功点恢复。');
        refresh(updated);
      } catch (error) { toast(error.message, 'error'); }
    },
  };
}

/* ---------- 小工具 ---------- */

async function openProject(id, op) {
  // 视图拉取即一次操作：无调用方世代时登记新世代（意图发生即中止 in-flight
  // 的旧拉取/跟踪）；GET 绑定该世代 signal，返回前复核——拉取期间接管的
  // 新操作（如用户启动 job B 或侧栏切页）使本响应过期丢弃，不得覆盖新视图（H1）。
  const fetchOp = op ?? viewOperations.begin();
  try {
    const view = await api.getProject(id, { signal: fetchOp.controller.signal });
    if (!viewOperations.isCurrent(fetchOp)) return;
    renderProject(view);
  } catch (error) {
    if (!viewOperations.isCurrent(fetchOp)) return;
    toast(error.message, 'error');
  }
}
