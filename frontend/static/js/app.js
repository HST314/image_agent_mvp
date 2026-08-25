/* 应用入口：顶部导航视图切换、可折叠工程目录、新建工程对话框与全局接线。 */

import { $, $$, el, toast, sectionPanel } from './dom.js';
import { state, patch } from './store.js';
import * as api from './api.js';
import { STATE_LABELS } from './states.js';
import { renderHome } from './home.js';
import { renderProject, stopJobTracking } from './project.js';
import { renderStatusPage } from './statuspage.js';
import { renderRuntimeSettingsPage } from './settingspage.js';
import { createParentBridge } from './parentbridge.js';
import { createNavigator, viewOperations } from './jobrunner.js';
import { createImmediateProjectFlow } from './createflow.js';
import { buildNewProjectTask } from './createform.js';
import { renderJobProgress } from './history.js';
import { markActiveTab, setStatusAlert, setTopContext } from './topnav.js';
import { createAuxPageRefresher, createViewSwitcher } from './viewswitch.js';
import { captureWorkspaceState } from './workspace_state.js';

let parentBridge = null;

async function boot() {
  try {
    const context = await api.runtimeContext();
    patch({
      managedByHarness: context.managed_by_harness === true,
      managedProjectId: context.project_id || null,
      taskId: context.task_id || null,
      instanceId: context.instance_id || null,
      capabilities: context.capabilities || {},
      bridgeProtocolVersion: context.bridge_protocol_version || null,
    });
    if (context.managed_by_harness) {
      $('#app').classList.add('app--managed');
      parentBridge = createParentBridge({
        instanceId: context.instance_id,
        protocolVersion: context.bridge_protocol_version,
      });
    }
  } catch (error) {
    toast(error.message, 'error');
  }
  bindChrome();
  if (state.managedByHarness && state.managedProjectId) {
    await loadHealth();
    await openProject(state.managedProjectId);
    void refreshManagedStatusAlert();
  } else {
    await loadProjects();
    goHome();
  }
}

async function loadHealth() {
  try {
    const health = await api.health();
    patch({ health });
    const ready = health.status === 'ok';
    if ($('#health-text')) $('#health-text').textContent = ready ? '服务已就绪' : '服务部分降级';
    if ($('#health-dot')) $('#health-dot').style.background = ready ? 'var(--success)' : 'var(--warning)';
  } catch (error) {
    patch({ health: { status: 'degraded' } });
    if ($('#health-text')) $('#health-text').textContent = '服务未连接';
    if ($('#health-dot')) $('#health-dot').style.background = 'var(--danger)';
    toast(error.message, 'error');
  }
  updateStatusAlert();
}

async function loadProjects() {
  if (state.managedByHarness) {
    await loadHealth();
    return;
  }
  try {
    const [health, data] = await Promise.all([api.health(), api.listProjects()]);
    patch({ projects: data.items, health });
    const ready = health.status === 'ok';
    $('#health-text').textContent = ready ? '服务已就绪' : '服务部分降级';
    $('#health-dot').style.background = ready ? 'var(--success)' : 'var(--warning)';
  } catch (error) {
    patch({ health: { status: 'degraded' } });
    $('#health-text').textContent = '服务未连接';
    $('#health-dot').style.background = 'var(--danger)';
    toast(error.message, 'error');
  }
  renderNav();
}

function renderNav() {
  const nav = $('#project-list');
  if (!nav) return;
  nav.textContent = '';
  if (!state.projects.length) {
    nav.append(el('div', { class: 'sidebar__empty', text: state.managedByHarness ? '等待主系统启动当前实例。' : '还没有工程。创建第一个视觉任务开始工作。' }));
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

/* ---- 顶部导航视图切换 ---- */

/* 当前挂载的非工作区状态页，含实时订阅；切页/导航/回首页时 dispose。 */
let activePage = null;
function leavePage() {
  activePage?.dispose?.();
  activePage = null;
}

/* 渲染状态页：基于当前工程视图；未打开工程时由页面自身渲染空态。 */
function renderPage(view) {
  leavePage();
  const content = $('#content');
  content.textContent = '';
  if (view === 'settings') {
    const current = state.current;
    activePage = renderRuntimeSettingsPage(content, current, {
      managed: state.managedByHarness,
      editable: state.capabilities.edit_runtime_settings === true,
    }, {
      load: (options) => state.managedByHarness
        ? requireParentBridge().getSettings()
        : api.getRuntimeSettings(current.project_id, options),
      propose: (payload) => requireParentBridge().proposeSettings(payload),
      confirm: (payload) => requireParentBridge().confirmSettings(payload),
      revise: (payload, options) => api.reviseRuntimeSettings(current.project_id, payload, options),
      onApplied: async () => {
        const fresh = await api.getProject(current.project_id, { cache: 'no-store' });
        patch({ current: fresh });
        setTopContext({ projectId: fresh.project_id, branch: fresh.manifest?.current_branch });
        updateStatusAlert(fresh);
        renderNav();
        toast('当前任务设置修订已保存。');
        if (state.view === 'settings' && state.current?.project_id === fresh.project_id) {
          renderPage('settings');
        }
      },
    });
  } else if (view === 'status') {
    const current = state.current;
    activePage = renderStatusPage(content, current, {
      openStream: current
        ? (after, { signal } = {}) => api.streamTimelineEvents(current.project_id, { after, signal })
        : null,
      loadRuntimeStatus: current
        ? ({ signal } = {}) => api.runtimeStatus(current.project_id, { signal })
        : null,
      loadManagedSettings: state.managedByHarness && parentBridge?.supported
        ? () => parentBridge.getSettings()
        : null,
      onManagedSettings: (settings) => updateStatusAlert(
        current,
        Boolean(settings.pending_application?.last_error),
      ),
      health: state.health,
    });
  }
}

function requireParentBridge() {
  if (!parentBridge?.supported) throw new Error('当前页面未建立可信的主系统设置连接。');
  return parentBridge;
}

async function refreshManagedStatusAlert() {
  if (!parentBridge?.supported) return;
  try {
    const settings = await parentBridge.getSettings();
    updateStatusAlert(state.current, Boolean(settings.pending_application?.last_error));
  } catch {
    updateStatusAlert(state.current, true);
  }
}

function updateStatusAlert(view = state.current, externalAbnormal) {
  if (typeof externalAbnormal === 'boolean') patch({ externalStatusAbnormal: externalAbnormal });
  const failed = Boolean(view?.manifest?.failed_step);
  const unresolved = Array.isArray(view?.unknown_actions) && view.unknown_actions.length > 0;
  setStatusAlert(state.health?.status !== 'ok' || failed || unresolved || state.externalStatusAbnormal);
}

function renderCurrentProject(view, options) {
  renderProject(view, options);
  updateStatusAlert(view);
}

const auxPageRefresher = createAuxPageRefresher({
  getState: () => state,
  getProject: (id, opts) => api.getProject(id, opts),
  loadProjects,
  patch,
  renderPage,
  notify: toast,
}, viewOperations);
const refreshAuxPage = auxPageRefresher.refresh;

let workspaceReconcileController = null;
function cancelWorkspaceReconcile() {
  workspaceReconcileController?.abort();
  workspaceReconcileController = null;
}

function workspaceRevision(view) {
  const manifest = view?.manifest || {};
  const job = view?.active_job || {};
  return [
    manifest.current_branch,
    manifest.current_checkpoint?.checkpoint_id,
    manifest.updated_at,
    job.job_id,
    job.status,
  ].map((value) => String(value || '')).join(':');
}

async function reconcileWorkspace(cached) {
  cancelWorkspaceReconcile();
  const controller = new AbortController();
  workspaceReconcileController = controller;
  try {
    const fresh = await api.getProject(cached.project_id, { signal: controller.signal, cache: 'no-store' });
    if (controller.signal.aborted || state.view !== 'workspace'
        || state.current?.project_id !== cached.project_id) return;
    if (workspaceRevision(fresh) === workspaceRevision(cached)) {
      // 同一提交只更新内存权威视图，不重建 DOM，保留用户刚恢复的 UI 状态。
      patch({ current: fresh });
      return;
    }
    renderCurrentProject(fresh);
    renderNav();
  } catch (error) {
    if (!controller.signal.aborted) toast(error.message, 'error');
  } finally {
    if (workspaceReconcileController === controller) workspaceReconcileController = null;
  }
}

function goHome() {
  cancelWorkspaceReconcile();
  leavePage();
  stopJobTracking();

  patch({ current: null, view: 'workspace' });
  markActiveTab('workspace');
  setTopContext({});
  if (state.managedByHarness) {
    $('#content').append(sectionPanel('等待当前任务', '请从主系统任务面板重新打开此 Image 工作项。'));
  } else {
    renderHome($('#content'), { onNew: showCreate, onOpen: openProject });
  }
  renderNav();
}

/* 侧栏/首页/刷新共用的真实导航入口（逻辑在 jobrunner.js createNavigator）：
 * 导航意图发生即中止当前操作（含 in-flight POST 与跟踪循环），GET 绑定导航
 * 世代并在返回前复核——慢 GET/连续点击的迟到响应不会覆盖新视图（H1）。
 * T2/T3：导航意图同时卸载状态/设置页的实时订阅（leavePage）。 */
const navigation = createNavigator({
  getProject: (id, opts) => api.getProject(id, opts),
  renderProject: renderCurrentProject,
  afterOpen: () => { renderNav(); collapseSidebar(); },
  notify: toast,
});
export const openProject = (id) => {
  cancelWorkspaceReconcile();
  leavePage();
  return navigation.openProject(id);
};

/* 视图切换决策（逻辑在 viewswitch.js，可 Node 侧回归）：真实依赖在此接线。
 * 重点击当前状态/设置页签会中止在途工程导航（H1），迟到 GET 不得切回工作区。 */
const viewSwitcher = createViewSwitcher({
  getState: () => state,
  patch,
  markActiveTab,
  stopJobTracking,
  renderPage,
  openProject,
  goHome,
  captureWorkspace: (view) => {
    cancelWorkspaceReconcile();
    captureWorkspaceState($('#content'), view);
  },
  renderCachedWorkspace: (view) => {
    leavePage();
    renderCurrentProject(view);
  },
  reconcileWorkspace,
});
const setView = viewSwitcher.setView;

/* ---- 新建工程对话框 ---- */

function showCreate() {
  if (state.managedByHarness) {
    toast('受管实例的任务卡请在主系统中审阅和启动。', 'error');
    return;
  }
  const dialog = $('#project-dialog');
  $('#project-form').reset();
  $('#project-error').textContent = '';
  $('#task-error').textContent = '';
  $('#create-button').disabled = false;
  dialog.showModal();
  setTimeout(() => $('#project-id').focus(), 0);
}

function renderCreatePending(projectId) {
  leavePage();
  patch({ current: null, view: 'workspace' });
  markActiveTab('workspace');
  setTopContext({ projectId, branch: 'main' });
  const content = $('#content');
  content.textContent = '';
  const section = sectionPanel('创作进度', `工程 ${projectId}`);
  section.setAttribute('aria-busy', 'true');
  const progress = el('div', { style: 'margin-top:14px' });
  const progressHandle = renderJobProgress(progress, {
    project_id: projectId,
    operation: '创建工程',
    status: 'submitting',
  });
  section.append(progress);
  content.append(section);
  renderNav();
  collapseSidebar();
  return { busyRegion: section, progressHandle };
}

const projectCreator = createImmediateProjectFlow({
  createProject: (payload, opts) => api.createProject(payload, opts),
  showPending(payload) {
    $('#create-button').disabled = true;
    $('#project-dialog').close();
    return renderCreatePending(payload.project_id);
  },
  showCreated(view, pending) {
    pending?.busyRegion?.removeAttribute('aria-busy');
    $('#create-button').disabled = false;
    renderCurrentProject(view, { autostartBootstrap: true });
    renderNav();
    collapseSidebar();
    void loadProjects();
    toast('工程已创建，正在启动创作流程。');
  },
  showError(error, pending) {
    pending?.busyRegion?.removeAttribute('aria-busy');
    pending?.progressHandle?.done({ status: 'failed', error: { message: error.message } });
    $('#create-button').disabled = false;
    $('#task-error').textContent = error.message;
    const dialog = $('#project-dialog');
    if (!dialog.open) dialog.showModal();
    $('#project-id').focus();
    toast(error.message, 'error');
  },
}, viewOperations);

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
  const goal = $('#creative-goal').value.trim();
  const usageScene = $('#usage-scene').value.trim();
  if (!goal) {
    $('#task-error').textContent = '请填写创作目标。';
    $('#creative-goal').focus();
    return;
  }
  if (!usageScene) {
    $('#task-error').textContent = '请填写使用场景。';
    $('#usage-scene').focus();
    return;
  }
  const task = buildNewProjectTask({
    projectId: id,
    goal,
    usageScene,
    targetGroup: $('#target-group').value,
    styleTone: $('#style-tone').value,
    deliverySpec: $('#delivery-spec').value,
  });
  /* T10（契约 §7/Q10-A）：等待创建接口前已经关闭弹窗并切至工作台等待态；
   * 创建成功后再用权威工程视图接管并异步启动首个 job。 */
  await projectCreator.start({ project_id: id, task_card: task, defer_run: true });
}

/* ---- 全局接线 ---- */

/* 左侧目录栏折叠（Q1-A）：默认窄条只留图标；悬停临时展开，点击图钉按钮固定展开。 */
let sidebarPinned = false;

function applySidebar(hover) {
  if (!$('#sidebar')) return;
  const expanded = sidebarPinned || hover;
  $('#app').classList.toggle('sidebar-expanded', expanded);
  $('#sidebar-toggle').setAttribute('aria-expanded', String(expanded));
}

function collapseSidebar() {
  sidebarPinned = false;
  applySidebar(false);
}

function bindChrome() {
  $('#new-button')?.addEventListener('click', showCreate);
  $('#refresh-button').addEventListener('click', () => {
    if (state.view !== 'workspace') { refreshAuxPage(); return; }
    if (state.current) openProject(state.current.project_id);
    else loadProjects().then(goHome);
  });
  $('#project-form')?.addEventListener('submit', createProject);
  $$('#project-dialog [data-close]').forEach((b) => b.addEventListener('click', () => $('#project-dialog').close()));
  $('#project-dialog')?.addEventListener('cancel', (event) => { event.preventDefault(); $('#project-dialog').close(); });
  $$('.topnav__tab').forEach((tab) => tab.addEventListener('click', () => setView(tab.dataset.view)));
  const sidebar = $('#sidebar');
  sidebar?.addEventListener('mouseenter', () => applySidebar(true));
  sidebar?.addEventListener('mouseleave', () => applySidebar(false));
  $('#sidebar-toggle')?.addEventListener('click', () => { sidebarPinned = !sidebarPinned; applySidebar(false); });
}

boot();
