/* T9：进度卡历史阶段只读快照与一键分支。
 * 历史快照来自当前分支谱系上的不可变 checkpoint；打开弹窗不触发任何写操作。
 */

import { el, toast } from './dom.js';
import * as api from './api.js';
import { assetUrl } from './api.js';
import { renderMarkdownInto } from './markdown.js';
import { WORKFLOW_STATES, stateLabel } from './states.js';
import { fieldLabel, taskbookDisplayMarkdown, terminationReasonLabel } from './copy.js';

const STAGE_INDEX = new Map(WORKFLOW_STATES.map((stage, index) => [stage.id, index]));

/** 每个已完成状态只保留沿当前谱系最后一次成功快照。 */
export function completedStageSnapshots(items = [], currentSnapshot = {}) {
  const currentIndex = STAGE_INDEX.get(currentSnapshot?.state) ?? 0;
  const completedThrough = currentSnapshot?.completed === true ? currentIndex : currentIndex - 1;
  const latest = new Map();
  for (const item of items || []) {
    const index = STAGE_INDEX.get(item?.state);
    if (index === undefined || index > completedThrough) continue;
    latest.set(item.state, item);
  }
  return WORKFLOW_STATES
    .filter((stage, index) => index <= completedThrough && latest.has(stage.id))
    .map((stage) => latest.get(stage.id));
}

/** 契约 §6/Q7-C：来源阶段 + 本地时间，秒级避免连续建分支重名。 */
export function automaticBranchName(state, date = new Date()) {
  const pad = (value) => String(value).padStart(2, '0');
  const label = stateLabel(state).replace(/[^\p{L}\p{N}]+/gu, '-').replace(/^-|-$/g, '') || '历史阶段';
  return `${label}-${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
}

function addAsset(container, projectId, asset, alt) {
  const url = assetUrl(projectId, asset);
  if (!url) return false;
  const img = el('img', { src: url, alt, loading: 'lazy', decoding: 'async' });
  if (/^https?:/.test(url)) img.setAttribute('referrerpolicy', 'no-referrer');
  container.append(img);
  return true;
}

function readableValue(value) {
  if (Array.isArray(value)) return value.filter((item) => ['string', 'number'].includes(typeof item)).join('、');
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (['string', 'number'].includes(typeof value)) return String(value);
  return '';
}

function renderTaskSummary(container, snapshot) {
  const task = snapshot.task_card || snapshot.context?.task_card;
  if (!task) return false;
  const dl = el('dl', { class: 'snapshot-kv' });
  const entries = [
    ['交付目标', task.deliverable_goal],
    ['使用场景', task.usage_context],
    ...Object.entries(task.known_facts || {}).map(([key, value]) => [fieldLabel(key), readableValue(value)]),
  ].filter(([, value]) => value);
  for (const [label, value] of entries) dl.append(el('dt', { text: label }), el('dd', { text: value }));
  if (entries.length) container.append(dl);

  const questions = snapshot.question_card?.questions || [];
  if (questions.length) {
    const list = el('ol', { class: 'snapshot-list' });
    for (const question of questions) {
      const text = question.prompt || question.question || '待确认问题';
      const options = (question.options || []).map((option) => option.label).filter(Boolean);
      list.append(el('li', {}, [el('strong', { text }), options.length ? el('span', { text: options.join(' / ') }) : null]));
    }
    container.append(el('h3', { text: '当时的澄清问题' }), list);
  }
  return entries.length > 0 || questions.length > 0;
}

function renderCandidates(container, projectId, snapshot) {
  const candidates = snapshot.candidates || [];
  if (!candidates.length) return false;
  const grid = el('div', { class: 'snapshot-gallery' });
  candidates.forEach((asset, index) => {
    const figure = el('figure');
    addAsset(figure, projectId, asset, `候选方向 ${index + 1}`);
    figure.append(el('figcaption', { text: asset.style_name ? `方向 ${index + 1} · ${asset.style_name}` : `方向 ${index + 1}` }));
    grid.append(figure);
  });
  container.append(grid);
  return true;
}

function renderInspection(container, projectId, snapshot) {
  const inspection = snapshot.inspection;
  const asset = snapshot.best_asset || snapshot.current_asset || snapshot.asset || snapshot.master_asset;
  if (!inspection && !asset) return false;
  const layout = el('div', { class: 'snapshot-inspection' });
  const visual = el('div', { class: 'snapshot-asset' });
  if (asset) addAsset(visual, projectId, asset, '该阶段保存的图像');
  const detail = el('div');
  detail.append(el('span', { class: `badge ${inspection?.passed ? 'badge--success' : 'badge--warning'}`, text: inspection?.passed ? '本轮质检通过' : terminationReasonLabel(snapshot.termination_reason) }));
  if (snapshot.round) detail.append(el('h3', { text: `第 ${snapshot.round} 轮自检` }));
  const deviations = inspection?.deviations || [];
  if (deviations.length) {
    const list = el('ul', { class: 'snapshot-list' });
    deviations.forEach((item) => list.append(el('li', { text: item })));
    detail.append(el('h4', { text: '当时记录的问题' }), list);
  }
  layout.append(visual, detail);
  container.append(layout);
  return true;
}

function renderFinal(container, projectId, snapshot) {
  const asset = snapshot.final_asset || snapshot.current_asset;
  const envelope = snapshot.delivery_envelope;
  if (!asset && !envelope) return false;
  const visual = el('div', { class: 'snapshot-asset snapshot-asset--final' });
  if (asset) addAsset(visual, projectId, asset, '该阶段的最终图像');
  container.append(visual);
  const markdown = envelope?.design_note_markdown;
  if (markdown) {
    const note = el('div', { class: 'markdown-body snapshot-note' });
    renderMarkdownInto(note, markdown);
    container.append(note);
  }
  return true;
}

function renderSnapshotContent(container, projectId, item) {
  const snapshot = item.snapshot || {};
  let rendered = false;
  if (item.state === 'intake_clarify') rendered = renderTaskSummary(container, snapshot);
  if (item.state === 'confirmation_build' && snapshot.task_markdown) {
    const document = el('div', { class: 'markdown-body snapshot-note' });
    renderMarkdownInto(document, taskbookDisplayMarkdown(snapshot.task_markdown));
    container.append(document);
    rendered = true;
  }
  if (['initial_candidate_generation', 'master_candidate_selection'].includes(item.state)) {
    rendered = renderCandidates(container, projectId, snapshot) || rendered;
  }
  if (item.state === 'self_check_iteration') rendered = renderInspection(container, projectId, snapshot) || rendered;
  if (item.state === 'human_prompt_iteration') {
    rendered = renderInspection(container, projectId, snapshot) || rendered;
    const prompt = snapshot.human_prompt || snapshot.last_human_prompt;
    if (prompt) { container.append(el('h3', { text: '当时的修改说明' }), el('p', { text: prompt })); rendered = true; }
  }
  if (item.state === 'final_approval') rendered = renderFinal(container, projectId, snapshot) || rendered;
  if (!rendered) container.append(el('p', { class: 'snapshot-empty', text: '该阶段已完成，但当时没有额外的可视内容。' }));
}

export function renderProgressSteps(container, view, { onBranchCreated }) {
  const current = view.snapshot || {};
  const currentIndex = STAGE_INDEX.get(current.state) ?? 0;
  const completed = new Map(completedStageSnapshots(view.progress_snapshots, current).map((item) => [item.state, item]));
  WORKFLOW_STATES.forEach((stage, index) => {
    const item = completed.get(stage.id);
    const className = `step ${item ? 'is-done step--interactive' : index === currentIndex ? 'is-current' : ''}`;
    const node = item
      ? el('button', { type: 'button', class: className, 'aria-label': `查看${stage.label}阶段只读快照` })
      : el('div', { class: className, 'aria-current': index === currentIndex ? 'step' : null });
    node.append(el('div', { class: 'step__bar' }), el('span', { text: stage.label }));
    if (item) node.addEventListener('click', () => openSnapshotDialog({ projectId: view.project_id, item, onBranchCreated }));
    container.append(node);
  });
}

export function openSnapshotDialog({ projectId, item, onBranchCreated }) {
  const label = stateLabel(item.state);
  const dialog = el('dialog', { class: 'dialog snapshot-dialog', 'aria-labelledby': 'snapshot-dialog-title' });
  const close = el('button', { type: 'button', class: 'btn btn--secondary', text: '关闭' });
  const branch = el('button', { type: 'button', class: 'btn btn--primary', text: '从此处创建分支' });
  const body = el('div', { class: 'dialog__body snapshot-dialog__body' });
  body.append(el('div', { class: 'snapshot-meta' }, [
    el('span', { class: 'badge badge--info', text: '只读快照' }),
    el('span', { text: `保存于${label}阶段 · 第 ${item.sequence} 个检查点` }),
  ]));
  renderSnapshotContent(body, projectId, item);
  dialog.append(
    el('div', { class: 'dialog__head' }, [el('div', {}, [el('h2', { id: 'snapshot-dialog-title', text: `${label} · 历史快照` }), el('p', { text: '回看不会改变当前工程进度。' })])]),
    body,
    el('div', { class: 'dialog__foot snapshot-dialog__foot' }, [el('small', { text: '创建后会自动切换到新分支，可从这里继续创作。' }), close, branch]),
  );
  close.addEventListener('click', () => dialog.close());
  branch.addEventListener('click', async () => {
    branch.disabled = true;
    branch.textContent = '正在创建分支…';
    try {
      const created = await api.branchFrom(projectId, { checkpoint: item.checkpoint_id, name: automaticBranchName(item.state) });
      const checkpointId = created.manifest?.current_checkpoint?.checkpoint_id;
      if (!checkpointId) throw new Error('新分支缺少可切换的检查点。');
      await api.switchBranch(projectId, checkpointId);
      const next = await api.getProject(projectId);
      dialog.close();
      toast(`已从${label}阶段创建并切换到新分支。`);
      onBranchCreated?.(next);
    } catch (error) {
      branch.disabled = false;
      branch.textContent = '从此处创建分支';
      toast(error.message, 'error');
    }
  });
  dialog.addEventListener('close', () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
}
