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

/** 创建接口的成功契约：返回的就是已经切换到新分支的完整工程视图。 */
export function isCreatedBranchView(view, { projectId, branchName }) {
  return view?.project_id === projectId
    && view.manifest?.current_branch === branchName
    && view.manifest?.current_checkpoint?.branch === branchName;
}

/**
 * “创建即切换”的单请求事务。
 *
 * POST 可能已在服务端完成、但响应在客户端解析前丢失。此时只能 GET 当前工程
 * 对账，绝不能自动补发第二次创建，否则会撞上同名分支并把真实成功误报为失败。
 */
export async function createSnapshotBranch(
  { projectId, checkpoint, branchName },
  { branchFrom = api.branchFrom, getProject = api.getProject } = {},
) {
  let createError;
  try {
    const created = await branchFrom(projectId, { checkpoint, name: branchName });
    if (isCreatedBranchView(created, { projectId, branchName })) {
      return { view: created, reconciled: false };
    }
    createError = new Error('创建接口未返回完整的新分支工程视图。');
  } catch (error) {
    createError = error;
  }

  try {
    const current = await getProject(projectId);
    if (isCreatedBranchView(current, { projectId, branchName })) {
      return { view: current, reconciled: true };
    }
  } catch {
    // 保留创建请求的原始错误；GET 仅用于确认结果，不覆盖首要故障信息。
  }
  throw createError;
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

const asTextList = (value) => Array.isArray(value)
  ? value.map((item) => readableValue(item)).filter(Boolean)
  : [];

/**
 * 将新旧检查点归一为“广告品类库 + 艺术风格库”两张卡片的数据。
 * 旧检查点没有保存参考图和品类详情时只展示明确的缺失提示，不再回退为候选主图。
 */
export function skillInvocationView(snapshot = {}) {
  const invocation = snapshot.skill_invocations || {};
  const category = invocation.category_library || {};
  const persistedStyles = invocation.style_library?.selections;
  const legacyStyles = Array.isArray(snapshot.style_selections) ? snapshot.style_selections : [];
  const candidateNames = new Map((snapshot.candidates || []).map((item) => [item.style_id, item.style_name]));
  const styles = (Array.isArray(persistedStyles) ? persistedStyles : legacyStyles).slice(0, 5).map((style, index) => ({
    ...style,
    styleName: style.style_name || candidateNames.get(style.style_id) || `风格方向 ${index + 1}`,
    interpretation: readableValue(style.artistic_interpretation)
      || readableValue(style.reason)
      || readableValue(style.mechanism),
  }));
  return {
    category: {
      name: readableValue(category.category_name),
      description: readableValue(category.description),
      productionConstraints: asTextList(category.production_constraints),
      visualRules: asTextList(category.visual_rules),
      forbiddenElements: asTextList(category.forbidden_elements),
      reviewChecks: asTextList(category.review_checks),
      available: Object.keys(category).length > 0,
    },
    styles,
    hasPersistedStyleDetails: Array.isArray(persistedStyles),
  };
}

function appendRuleGroup(container, title, items) {
  if (!items.length) return false;
  const list = el('ul', { class: 'skill-call-card__list' });
  items.forEach((item) => list.append(el('li', { text: item })));
  container.append(el('section', { class: 'skill-call-card__group' }, [el('h4', { text: title }), list]));
  return true;
}

let skillInvocationRenderSequence = 0;

/** Produce collision-free accessible-name targets for current + audit versions. */
export function skillInvocationDomIds(snapshot = {}) {
  const rawVersion = snapshot?.skill_invocation_current?.version_id || snapshot?.version_id || 'legacy';
  const version = String(rawVersion).replace(/[^A-Za-z0-9_-]/g, '-') || 'legacy';
  const instance = ++skillInvocationRenderSequence;
  return {
    category: `category-skill-title-${version}-${instance}`,
    style: `style-skill-title-${version}-${instance}`,
  };
}

export function renderSkillInvocations(container, projectId, snapshot) {
  const model = skillInvocationView(snapshot);
  const titleIds = skillInvocationDomIds(snapshot);
  const layout = el('div', { class: 'skill-call-grid' });

  const categoryCard = el('section', { class: 'skill-call-card', 'aria-labelledby': titleIds.category });
  categoryCard.append(el('div', { class: 'skill-call-card__head' }, [
    el('span', { class: 'skill-call-card__index', text: '01', 'aria-hidden': 'true' }),
    el('div', {}, [
      el('h3', { id: titleIds.category, text: '广告品类库' }),
      el('p', { text: model.category.name ? `已匹配：${model.category.name}` : '本次调用获得的品类设计约束' }),
    ]),
  ]));
  if (model.category.description) categoryCard.append(el('p', { class: 'skill-call-card__summary', text: model.category.description }));
  let hasCategoryContent = Boolean(model.category.description);
  hasCategoryContent = appendRuleGroup(categoryCard, '制作约束', model.category.productionConstraints) || hasCategoryContent;
  hasCategoryContent = appendRuleGroup(categoryCard, '视觉规则', model.category.visualRules) || hasCategoryContent;
  hasCategoryContent = appendRuleGroup(categoryCard, '禁用元素', model.category.forbiddenElements) || hasCategoryContent;
  hasCategoryContent = appendRuleGroup(categoryCard, '验收检查', model.category.reviewChecks) || hasCategoryContent;
  if (!hasCategoryContent) categoryCard.append(el('p', { class: 'skill-call-card__empty', text: '该旧快照未保存广告品类库调用详情；新生成的检查点会完整记录。' }));

  const styleCard = el('section', { class: 'skill-call-card skill-call-card--styles', 'aria-labelledby': titleIds.style });
  styleCard.append(el('div', { class: 'skill-call-card__head' }, [
    el('span', { class: 'skill-call-card__index', text: '02', 'aria-hidden': 'true' }),
    el('div', {}, [
      el('h3', { id: titleIds.style, text: '艺术风格库' }),
      el('p', { text: `已选择 ${model.styles.length}/5 张风格参考图，并提取可迁移的视觉机制` }),
    ]),
  ]));
  if (model.styles.length) {
    const gallery = el('div', { class: 'skill-reference-grid', role: 'list', 'aria-label': '五张已选择的艺术风格参考图' });
    model.styles.forEach((style, index) => {
      const figure = el('figure', { class: 'skill-reference', role: 'listitem' });
      const visual = el('div', { class: 'skill-reference__visual' });
      if (!addAsset(visual, projectId, style.reference_asset, `风格参考 ${index + 1}：${style.styleName}`)) {
        visual.append(el('span', { text: model.hasPersistedStyleDetails ? '参考图暂不可用' : '旧快照未保存参考图' }));
      }
      figure.append(visual, el('figcaption', {}, [
        el('strong', { text: `${index + 1}. ${style.styleName}` }),
        el('p', { text: style.interpretation || '该旧快照未保存模型生成的艺术理解。' }),
      ]));
      gallery.append(figure);
    });
    styleCard.append(gallery);
  } else {
    styleCard.append(el('p', { class: 'skill-call-card__empty', text: '该快照没有可展示的艺术风格库调用结果。' }));
  }
  layout.append(categoryCard, styleCard);
  container.append(layout);
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
  if (item.state === 'initial_candidate_generation') rendered = renderSkillInvocations(container, projectId, snapshot) || rendered;
  if (item.state === 'master_candidate_selection') rendered = renderCandidates(container, projectId, snapshot) || rendered;
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
      const branchName = automaticBranchName(item.state);
      const result = await createSnapshotBranch({
        projectId,
        checkpoint: item.checkpoint_id,
        branchName,
      });
      dialog.close();
      toast(result.reconciled
        ? `已核对并切换到从${label}阶段创建的新分支。`
        : `已从${label}阶段创建并切换到新分支。`);
      onBranchCreated?.(result.view);
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
