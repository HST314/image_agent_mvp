/* 应用入口：导航、工程列表、新建工程对话框与全局接线。 */

import { $, $$, el, toast, escapeHtml } from './dom.js';
import { state, patch } from './store.js';
import * as api from './api.js';
import { STATE_LABELS } from './states.js';
import { renderHome } from './home.js';
import { renderProject, stopJobTracking } from './project.js';
import { createNavigator } from './jobrunner.js';

const SAMPLE_TASK = {
  task_id: 'task_new',
  project_id: 'campaign-visual-01',
  source_refs: [{ ref_id: 'brief-001', ref_type: 'brief', excerpt: '请描述已确认的创作输入。', source_hash: null }],
  deliverable_goal: '描述需要生成的视觉内容、主体、风格和画面重点。',
  usage_context: '内部审核与决策',
  category_ref: { category_id: 'generic_visual_delivery', version: '1.0' },
  known_facts: { audience: '内部审核人员', tone: '清晰、精致' },
  unknowns: { output_spec: '待确认' },
  asset_inputs: [],
  status: 'draft',
};

async function boot() {
  patch({ offline: safeGet('studio-offline') === 'true' });
  bindChrome();
  await loadProjects();
  goHome();
}

function safeGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
function safeSet(key, value) { try { localStorage.setItem(key, value); } catch { /* ignore */ } }

async function loadProjects() {
  try {
    const [health, data] = await Promise.all([api.health(), api.listProjects()]);
    patch({ projects: data.items });
    const ready = health.status === 'ok';
    $('#health-text').textContent = ready ? '服务已就绪' : '服务部分降级';
    $('#health-dot').style.background = ready ? 'var(--success)' : 'var(--warning)';
  } catch (error) {
    $('#health-text').textContent = '服务未连接';
    $('#health-dot').style.background = 'var(--danger)';
    toast(error.message, 'error');
  }
  renderNav();
}

function renderNav() {
  const nav = $('#project-list');
  nav.textContent = '';
  if (!state.projects.length) {
    nav.append(el('div', { class: 'sidebar__empty', text: '还没有工程。创建第一个视觉任务开始工作。' }));
    return;
  }
  for (const p of state.projects) {
    const item = el('button', { class: 'project-item', type: 'button', 'aria-current': String(state.current?.project_id === p.project_id) });
    const retryable = p.failed_step?.error?.retryable === true;
    const status = p.failed_step ? (retryable ? '处理失败 · 可重试' : '处理失败 · 请修正配置') : p.completed ? '已完成' : STATE_LABELS[p.state] || '等待开始';
    item.append(el('strong', { text: p.project_id }), el('span', { text: status }));
    item.addEventListener('click', () => openProject(p.project_id));
    nav.append(item);
  }
}

function goHome() {
  stopJobTracking();
  patch({ current: null });
  $('#page-title').textContent = '开始一项新的视觉创作';
  $('#context-label').textContent = '创作工作台';
  renderHome($('#content'), { onNew: showCreate, onOpen: openProject });
  renderNav();
}

/* 侧栏/首页/刷新共用的真实导航入口（逻辑在 jobrunner.js createNavigator）：
 * 导航意图发生即中止当前操作（含 in-flight POST 与跟踪循环），GET 绑定导航
 * 世代并在返回前复核——慢 GET/连续点击的迟到响应不会覆盖新视图（H1）。 */
const navigation = createNavigator({
  getProject: (id, opts) => api.getProject(id, opts),
  renderProject,
  afterOpen: () => { renderNav(); closeSidebar(); },
  notify: toast,
});
export const openProject = navigation.openProject;

/* ---- 新建工程对话框 ---- */

function showCreate() {
  const dialog = $('#project-dialog');
  $('#task-json').value = JSON.stringify({ ...SAMPLE_TASK, project_id: 'campaign-visual-01' }, null, 2);
  $('#project-id').value = '';
  $('#offline').checked = state.offline;
  $('#project-error').textContent = '';
  $('#task-error').textContent = '';
  dialog.showModal();
  setTimeout(() => $('#project-id').focus(), 0);
}

async function createProject(event) {
  event.preventDefault();
  const id = $('#project-id').value.trim();
  $('#project-error').textContent = '';
  $('#task-error').textContent = '';
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$/.test(id)) {
    $('#project-error').textContent = '请输入 2–64 位合法工程 ID。';
    $('#project-id').focus();
    return;
  }
  let task;
  try { task = JSON.parse($('#task-json').value); } catch {
    $('#task-error').textContent = 'JSON 格式无效，请检查逗号、引号和括号。';
    $('#task-json').focus();
    return;
  }
  patch({ offline: $('#offline').checked });
  safeSet('studio-offline', String(state.offline));
  const button = $('#create-button');
  button.disabled = true;
  try {
    const view = await api.createProject({ project_id: id, task_card: task, offline: state.offline });
    $('#project-dialog').close();
    await loadProjects();
    renderProject(view);
    toast('工程已创建并保存首个检查点。');
  } catch (error) {
    $('#task-error').textContent = error.message;
    toast(error.message, 'error');
  } finally {
    button.disabled = false;
  }
}

/* ---- 全局接线 ---- */

function closeSidebar() {
  $('#sidebar').classList.remove('is-open');
  $('#menu-button').setAttribute('aria-expanded', 'false');
}

function bindChrome() {
  $('#new-button').addEventListener('click', showCreate);
  $('#refresh-button').addEventListener('click', () => {
    if (state.current) openProject(state.current.project_id);
    else loadProjects().then(goHome);
  });
  $('#project-form').addEventListener('submit', createProject);
  $$('#project-dialog [data-close]').forEach((b) => b.addEventListener('click', () => $('#project-dialog').close()));
  $('#project-dialog').addEventListener('cancel', (event) => { event.preventDefault(); $('#project-dialog').close(); });
  $('#menu-button').addEventListener('click', () => {
    const open = $('#sidebar').classList.toggle('is-open');
    $('#menu-button').setAttribute('aria-expanded', String(open));
  });
}

boot();
