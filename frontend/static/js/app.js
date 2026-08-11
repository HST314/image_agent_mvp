/* 应用入口：顶部导航视图切换、可折叠工程目录、新建工程对话框与全局接线（T1）。 */

import { $, $$, el, toast, escapeHtml, stateBlock } from './dom.js';
import { state, patch } from './store.js';
import * as api from './api.js';
import { STATE_LABELS } from './states.js';
import { renderHome } from './home.js';
import { renderProject, stopJobTracking } from './project.js';
import { createNavigator } from './jobrunner.js';
import { VIEWS, markActiveTab, setTopContext } from './topnav.js';

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
    const avatar = el('span', { class: 'project-item__avatar', 'aria-hidden': 'true', text: (p.project_id || '?').slice(0, 1).toUpperCase() });
    const text = el('span', { class: 'project-item__text' }, [el('strong', { text: p.project_id }), el('span', { text: status })]);
    item.append(avatar, text);
    item.addEventListener('click', () => openProject(p.project_id));
    nav.append(item);
  }
}

/* ---- 顶部导航视图切换（T1：工作区默认；状态/设置页由 T2/T3 填充） ---- */

const PLACEHOLDERS = {
  status: ['状态页（建设中）', 'T2 任务将在此集中呈现：Agent 运行状态、工程信息、最近活动、原始任务，以及实时滚动刷新、当前动作高亮、可暂停的事件日志。'],
  settings: ['设置页（建设中）', 'T3 任务将把运行策略迁移到此页：全部字段中文化表单，分「常用 / 高级」两组；保存即生效并自动创建新分支。'],
};

function renderPlaceholder(view) {
  const content = $('#content');
  content.textContent = '';
  const [title, detail] = PLACEHOLDERS[view];
  content.append(stateBlock('empty', title, detail));
}

function setView(view) {
  if (!VIEWS.includes(view) || view === state.view) return;
  if (view === 'workspace') {
    if (state.current) {
      patch({ view });
      markActiveTab(view);
      openProject(state.current.project_id);
    } else {
      goHome();
    }
    return;
  }
  /* 离开工作区：中止进行中的操作与跟踪循环（后台 job 仍继续，
   * 回到工作区重新打开工程时会按既有逻辑恢复挂载）。 */
  stopJobTracking();
  patch({ view });
  markActiveTab(view);
  renderPlaceholder(view);
}

function goHome() {
  stopJobTracking();
  patch({ current: null, view: 'workspace' });
  markActiveTab('workspace');
  setTopContext({});
  renderHome($('#content'), { onNew: showCreate, onOpen: openProject });
  renderNav();
}

/* 侧栏/首页/刷新共用的真实导航入口（逻辑在 jobrunner.js createNavigator）：
 * 导航意图发生即中止当前操作（含 in-flight POST 与跟踪循环），GET 绑定导航
 * 世代并在返回前复核——慢 GET/连续点击的迟到响应不会覆盖新视图（H1）。 */
const navigation = createNavigator({
  getProject: (id, opts) => api.getProject(id, opts),
  renderProject,
  afterOpen: () => { renderNav(); collapseSidebar(); },
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

/* 左侧目录栏折叠（Q1-A）：默认窄条只留图标；悬停临时展开，点击图钉按钮固定展开。 */
let sidebarPinned = false;

function applySidebar(hover) {
  const expanded = sidebarPinned || hover;
  $('#app').classList.toggle('sidebar-expanded', expanded);
  $('#sidebar-toggle').setAttribute('aria-expanded', String(expanded));
}

function collapseSidebar() {
  sidebarPinned = false;
  applySidebar(false);
}

function bindChrome() {
  $('#new-button').addEventListener('click', showCreate);
  $('#refresh-button').addEventListener('click', () => {
    if (state.view !== 'workspace') { loadProjects(); return; }
    if (state.current) openProject(state.current.project_id);
    else loadProjects().then(goHome);
  });
  $('#project-form').addEventListener('submit', createProject);
  $$('#project-dialog [data-close]').forEach((b) => b.addEventListener('click', () => $('#project-dialog').close()));
  $('#project-dialog').addEventListener('cancel', (event) => { event.preventDefault(); $('#project-dialog').close(); });
  $$('.topnav__tab').forEach((tab) => tab.addEventListener('click', () => setView(tab.dataset.view)));
  const sidebar = $('#sidebar');
  sidebar.addEventListener('mouseenter', () => applySidebar(true));
  sidebar.addEventListener('mouseleave', () => applySidebar(false));
  $('#sidebar-toggle').addEventListener('click', () => { sidebarPinned = !sidebarPinned; applySidebar(false); });
}

boot();
