/* 工作台视图（T32 信息架构）：原始任务 / 当前决策 / 实时进度 / 结果 / 历史。
 * 长任务一律经后台 job + SSE 序号续传，页面在长任务期间保持可操作（T35）。 */

import { $, el, toast, stateBlock, icons, formatDate } from './dom.js';
import { state, patch } from './store.js';
import * as api from './api.js';
import { viewOperations, createJobRunner, isTerminalJobStatus } from './jobrunner.js';
import { renderMarkdownInto } from './markdown.js';
import { deriveView, stepIndex, WORKFLOW_STATES, stateLabel } from './states.js';
import { renderClarify } from './clarify.js';
import { renderTaskbook } from './taskbook.js';
import { renderGalleryStage } from './gallery.js';
import { createAnnotator } from './annotate.js';
import { renderTimeline, renderJobProgress } from './history.js';
import { renderSettings } from './settings.js';

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

export function renderProject(view) {
  viewOperations.begin();
  patch({ current: view });
  const content = $('#content');
  content.textContent = '';
  const snapshot = view.snapshot || {};
  const manifest = view.manifest || {};
  const projectId = view.project_id;
  const derived = deriveView(view);

  $('#page-title').textContent = projectId;
  $('#context-label').textContent = snapshot.completed ? '已完成工程' : stateLabel(snapshot.state);

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
  const idx = stepIndex(snapshot);
  const stepper = el('div', { class: 'stepper', 'aria-label': '工作流进度' });
  WORKFLOW_STATES.forEach((s, i) => {
    const step = el('div', { class: `step ${i < idx ? 'is-done' : i === idx ? 'is-current' : ''}` });
    step.append(el('div', { class: 'step__bar' }), el('span', { text: s.label }));
    stepper.append(step);
  });
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

  /* ===== 原始任务 ===== */
  const taskCard = snapshot.task_card;
  if (taskCard) {
    const panel = sectionPanel('原始任务', '上游输入原样留存；澄清答案与任务书修订均有审计');
    const list = el('dl', { class: 'kv' });
    list.append(
      el('dt', { text: '交付目标' }), el('dd', { text: taskCard.deliverable_goal || '—' }),
      el('dt', { text: '使用场景' }), el('dd', { text: taskCard.usage_context || '—' }),
      el('dt', { text: '已知事实' }), el('dd', { text: summarizeFacts(taskCard.known_facts) }),
      el('dt', { text: '未知项' }), el('dd', { text: summarizeFacts(taskCard.unknowns) }),
    );
    panel.append(list);
    primary.append(panel);
  }

  /* ===== 任务书 ===== */
  if (snapshot.task_markdown) {
    const panel = sectionPanel('任务书', '');
    panel.querySelector('.section__head').remove();
    renderTaskbook(panel, view, { projectId, actor, onChanged: refresh, jobRunner });
    primary.append(panel);
  }

  /* ===== 结果 ===== */
  if (snapshot.completed && snapshot.final_asset) {
    primary.append(renderResult(view, { projectId, refresh }));
  }

  /* ===== 工程信息（含操作人） ===== */
  rail.append(renderInfo(view, derived, () => renderProject(state.current)));

  /* ===== 付费调用待处置 ===== */
  const unknowns = view.unknown_actions || [];
  if (unknowns.length) rail.append(renderUnknowns(unknowns, { projectId, refresh }));

  /* ===== 历史 ===== */
  const historyPanel = sectionPanel('最近活动', '真实事件审计记录');
  renderTimeline(historyPanel, view.history || []);
  rail.append(historyPanel);

  /* ===== 设置 ===== */
  if (view.runtime_policy) {
    const holder = el('div');
    renderSettings(holder, view, { projectId, onChanged: refresh });
    rail.append(holder.firstChild);
  }

  /* 恢复进行中的 job 展示（刷新后） */
  const activeJob = view.active_job || (state.job && state.job.project_id === projectId && !isTerminalJobStatus(state.job.status) ? state.job : null);
  if (activeJob) {
    patch({ job: activeJob });
    jobRunner.attach(activeJob);
  }
}

/* ---------- 舞台 ---------- */

function stageTitle(derived) {
  return {
    clarify: '需求澄清', taskbook: '确认任务书', gallery: '选择主图', calibration: '画面质检与人工放行',
    disposition: '自动质检已达上限', annotate: '圈画微调', reinspection: '等待重新质检',
    resume_quality: '追加质检已确认', final: '最终确认', failed: '流程已暂停', terminated: '已终止且不交付',
    completed: '交付完成', resume: '继续工作流', empty: '工程已保存',
  }[derived.stage] || '当前决策';
}

function stageSubtitle(derived) {
  if (derived.stage === 'disposition') return '自动质检达到配置上限；最高分不会冒充通过，请选择分流方式';
  if (derived.stage === 'final') return '确认后冻结交付并生成说明；此后任何修改都将创建新修订';
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
    panel.append(stateBlock('empty', '请先确认任务书', '任务书在下方"任务书"区预览或编辑；确认后进入候选生成（付费步骤）。'));
    return;
  }
  if (stage === 'gallery') {
    let selectedId = null;
    const { confirmButton } = renderGalleryStage(panel, view, {
      projectId, selectedId,
      onSelect(slot) {
        selectedId = slot.asset.id || `candidate-${slot.index + 1}`;
        panel.querySelectorAll('.slot').forEach((node) => node.classList.remove('is-selected'));
        confirmButton.disabled = false;
        confirmButton.textContent = `确认方向 ${slot.index + 1} 为主图`;
        const grid = panel.querySelector('.gallery-grid');
        if (grid) {
          // 仅更新选择态
          [...grid.children].forEach((child, i) => {
            const pressed = child.dataset.key === slot.key;
            child.classList.toggle('is-selected', pressed);
            child.setAttribute('aria-selected', String(pressed));
          });
        }
      },
      onCompensate() { jobRunner.start({}); },
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
    if (derived.actions.includes('retry')) {
      btn = el('button', { type: 'button', class: 'btn btn--primary', text: '从上一成功点重试' });
      btn.addEventListener('click', () => jobRunner.retry({}));
    }
    panel.append(stateBlock('error', `流程在 ${stateLabel(failure.state)} 暂停`, failure?.error?.message || '后端能力暂不可用。修正模型、密钥或依赖后可安全重试。', btn));
    return;
  }
  if (stage === 'terminated') {
    panel.append(stateBlock('empty', '已终止且不交付', '该工程已按人工决定终止；可从历史分支重新开始。'));
    return;
  }
  if (stage === 'completed') {
    const rehearsal = snapshot.offline_rehearsal_completed === true;
    panel.append(stateBlock('empty', rehearsal ? '离线演练已完成最终验收' : '视觉资产已完成最终审批',
      rehearsal ? '模拟资产仅用于流程验收，未冻结为真实交付。' : '最终资产已由生产工作流校验并冻结，见下方"结果"区。'));
    return;
  }
  // resume / empty
  const btn = el('button', { type: 'button', class: 'btn btn--primary', text: '继续工作流' });
  btn.addEventListener('click', () => jobRunner.start({}));
  panel.append(stateBlock('empty', stateLabel(snapshot.state) || '工程已保存', '从当前检查点继续推进。若外部模型或密钥不可用，系统会保存真实错误供恢复。', btn));
}

function renderCalibration(panel, view, { projectId, refresh, jobRunner }) {
  const snapshot = view.snapshot || {};
  const inspection = snapshot.inspection || {};
  const asset = snapshot.asset || snapshot.current_asset || snapshot.master_asset;
  const summary = el('div', { class: 'section__head' });
  const badge = inspection.passed
    ? el('span', { class: 'badge badge--success', text: '建议通过' })
    : el('span', { class: 'badge badge--warning', text: '建议修改' });
  summary.append(el('div', {}, [el('h3', { text: '本轮质检结论' }), el('p', { text: inspection.rework_prompt_delta || '请审阅当前图像与质检结果。' })]), badge);
  panel.append(summary);
  if (Array.isArray(inspection.deviations) && inspection.deviations.length) {
    const ul = el('ul');
    inspection.deviations.forEach((d) => ul.append(el('li', { text: d })));
    panel.append(ul);
  }
  if (asset) {
    const url = api.assetUrl(projectId, asset);
    if (url) panel.append(el('img', { src: url, alt: '当前待审图像', style: 'max-width:360px;width:100%;border-radius:12px', loading: 'lazy' }));
  }
  const row = el('div', { class: 'button-row', style: 'margin-top:14px' });
  for (const action of MANUAL_ACTIONS) {
    const btn = el('button', {
      type: 'button',
      class: `btn ${action.primary ? 'btn--primary' : action.danger ? 'btn--danger' : 'btn--secondary'}`,
      text: action.label,
    });
    btn.addEventListener('click', () => {
      if (action.needsDelta) {
        const delta = window.prompt('请输入修改建议（将指导下一轮返工）');
        if (!delta) return;
        jobRunner.start({ manual_action: action.id, edited_delta: delta }, { intent: 'manual' });
        return;
      }
      if (action.danger && !window.confirm('确定终止本工程且不交付？该决定会进入审计事件。')) return;
      jobRunner.start({ manual_action: action.id }, { intent: 'manual' });
    });
    row.append(btn);
  }
  panel.append(row);
}

function renderDisposition(panel, view, { projectId, refresh, jobRunner }) {
  const snapshot = view.snapshot || {};
  const asset = snapshot.best_asset || snapshot.asset;
  const inspection = snapshot.inspection || {};
  panel.append(el('p', { class: 'hint', text: `终止原因：${snapshot.termination_reason || '达到轮次上限'}；当前为第 ${snapshot.round || 1} 轮。` }));
  if (Array.isArray(inspection.deviations) && inspection.deviations.length) {
    const ul = el('ul');
    inspection.deviations.forEach((d) => ul.append(el('li', { text: d })));
    panel.append(ul);
  }
  if (asset) {
    const url = api.assetUrl(projectId, asset);
    if (url) panel.append(el('img', { src: url, alt: '本轮最高分图像', style: 'max-width:360px;width:100%;border-radius:12px', loading: 'lazy' }));
  }
  const row = el('div', { class: 'button-row', style: 'margin-top:14px' });

  const addBtn = el('button', { type: 'button', class: 'btn btn--primary', text: '追加 N 轮（需确认费用）' });
  addBtn.addEventListener('click', async () => {
    const raw = window.prompt('追加质检轮数（1-20）：', '2');
    if (!raw) return;
    const rounds = Number(raw);
    if (!Number.isInteger(rounds) || rounds < 1 || rounds > 20) { toast('轮数需为 1-20 的整数。', 'error'); return; }
    if (!window.confirm(`追加 ${rounds} 轮将产生真实模型调用费用，确认继续？`)) return;
    try {
      await api.qualityDisposition(projectId, { action: 'add_rounds_with_cost_confirmation', additional_rounds: rounds, cost_confirmed: true });
      toast('已确认追加轮次。');
      refresh();
    } catch (error) { toast(error.message, 'error'); }
  });

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
  panel.append(row);
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
  createAnnotator(panel, {
    projectId,
    asset,
    history: view.history || [],
    onSubmitted: refresh,
    onBusy: setTuneBusy,
  });
  const divider = el('hr', { style: 'border:none;border-top:1px solid var(--border);margin:18px 0' });
  panel.append(divider);
  panel.append(el('h3', { text: '或仅用文字微调' }));
  const textArea = el('textarea', { class: 'input', 'aria-label': '文字微调说明', placeholder: '不圈画，直接描述整体修改方向' });
  const btn = el('button', { type: 'button', class: 'btn btn--secondary', text: '提交文字微调' , style: 'margin-top:10px' });
  btn.addEventListener('click', async () => {
    const prompt = textArea.value.trim();
    if (!prompt) { toast('请填写微调说明。', 'error'); return; }
    setTuneBusy(true);
    const job = await jobRunner.start({ human_prompt: prompt }, { intent: 'tune' });
    if (!job) setTuneBusy(false);
  });
  panel.append(textArea, btn);

  const finalBtn = el('button', { type: 'button', class: 'btn btn--primary', text: '确定终稿', style: 'margin-top:10px;margin-left:10px' });
  finalBtn.addEventListener('click', async () => {
    if (!window.confirm('确定将当前图片作为终稿？确认后将冻结交付。')) return;
    setTuneBusy(true);
    const job = await jobRunner.start({ manual_action: 'accept_current', final_approved: true }, { intent: 'human-final' });
    if (!job) setTuneBusy(false);
  });
  panel.append(finalBtn);
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
  if (!actor) panel.append(el('small', { text: '请先在右侧"工程信息"填写操作人身份。' }));
}

function renderResult(view, { projectId, refresh }) {
  const snapshot = view.snapshot || {};
  const panel = sectionPanel('结果', '冻结交付与最终说明');
  const asset = snapshot.final_asset;
  const url = api.assetUrl(projectId, asset);
  if (url) panel.append(el('img', { src: url, alt: '最终交付资产', style: 'max-width:480px;width:100%;border-radius:12px', loading: 'lazy' }));
  const meta = el('dl', { class: 'kv', style: 'margin-top:12px' });
  meta.append(
    el('dt', { text: '资产 ID' }), el('dd', { text: asset?.artifact_id || '—' }),
    el('dt', { text: '内容哈希' }), el('dd', { text: asset?.sha256 ? `${asset.sha256.slice(0, 16)}…` : '—' }),
  );
  panel.append(meta);
  const envelope = snapshot.delivery_envelope;
  if (envelope) {
    const note = el('div', { class: 'markdown-body' });
    renderMarkdownInto(note, envelope.design_note_markdown || '');
    panel.append(el('h3', { text: '最终设计说明' }), note);
    const row = el('div', { class: 'button-row', style: 'margin-top:10px' });
    const copy = el('button', { type: 'button', class: 'btn btn--secondary', text: '复制交付 JSON' });
    copy.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(JSON.stringify(envelope, null, 2)); toast('交付 JSON 已复制。'); }
      catch { toast('复制失败，请检查浏览器剪贴板权限。', 'error'); }
    });
    const retry = el('button', { type: 'button', class: 'btn btn--secondary', text: '重新生成说明' });
    retry.addEventListener('click', async () => {
      retry.disabled = true;
      try { await api.retryDeliveryNote(projectId); toast('说明已重新生成（冻结图片不变）。'); refresh(); }
      catch (error) { toast(error.message, 'error'); retry.disabled = false; }
    });
    row.append(copy, retry);
    panel.append(row);
  }
  return panel;
}

/* ---------- 侧栏 ---------- */

function renderInfo(view, derived, onActorChanged) {
  const snapshot = view.snapshot || {};
  const manifest = view.manifest || {};
  const panel = sectionPanel('工程信息', '');
  panel.querySelector('.section__head p')?.remove();
  const list = el('dl', { class: 'kv' });
  list.append(
    el('dt', { text: '当前状态' }), el('dd', { text: stateLabel(snapshot.state) }),
    el('dt', { text: '当前阶段' }), el('dd', { text: snapshot.phase || '—' }),
    el('dt', { text: '当前分支' }), el('dd', { text: manifest.current_branch || 'main' }),
    el('dt', { text: '更新时间' }), el('dd', { text: formatDate(manifest.updated_at) }),
    el('dt', { text: '可用动作' }), el('dd', { text: (view.capabilities || []).join('、') || '无' }),
  );
  panel.append(list);
  const actorField = el('div', { class: 'field', style: 'margin-top:12px' });
  const actorInput = el('input', { class: 'input', id: 'actor-input', placeholder: '确认/审批时使用的身份', value: getActor() });
  actorInput.addEventListener('change', () => {
    setActor(actorInput.value.trim());
    toast('操作人身份已保存。');
    onActorChanged?.();
  });
  actorField.append(el('label', { for: 'actor-input', text: '操作人身份' }), actorInput);
  panel.append(actorField);
  return panel;
}

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
        if (!actor) { toast('请先在"工程信息"填写操作人身份。', 'error'); return; }
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

function sectionPanel(title, subtitle) {
  const panel = el('section', { class: 'panel section ia-section' });
  const head = el('div', { class: 'section__head' });
  const text = el('div');
  text.append(el('h2', { text: title }));
  if (subtitle) text.append(el('p', { text: subtitle }));
  head.append(text);
  panel.append(head);
  return panel;
}

function summarizeFacts(facts) {
  if (!facts || typeof facts !== 'object') return '—';
  const entries = Object.entries(facts);
  if (!entries.length) return '—';
  return entries.map(([k, v]) => `${k}：${typeof v === 'object' ? JSON.stringify(v) : v}`).join('；');
}

function getActor() { try { return localStorage.getItem('studio-actor') || ''; } catch { return ''; } }
function setActor(value) { try { localStorage.setItem('studio-actor', value); } catch { /* ignore */ } }

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
