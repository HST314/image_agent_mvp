/* T1 视图切换的 H1 交错回归：状态页 → 慢工程 GET → 重点击当前页签。
 * 真实 createNavigator（app.js 侧栏接线同一实现）+ 真实 createViewSwitcher
 * （app.js 顶栏接线同一实现），GET 手工 settle，精确编排返回顺序。 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createOperationRegistry, createNavigator } from '../../frontend/static/js/jobrunner.js';
import { createAuxPageRefresher, createViewSwitcher } from '../../frontend/static/js/viewswitch.js';

/* 可控应用假依赖：镜像 app.js 的接线（stopJobTracking 即 registry.leave；
 * renderProject 挂载即进入工作区视图）。 */
function fakeApp(registry, initialView = 'status') {
  const state = { view: initialView, current: null };
  const calls = {
    gets: [], rendered: [], pages: [], tabs: [], notified: [],
    leaves: 0, home: 0, cached: [], captured: [],
  };
  const nav = createNavigator({
    getProject: (id, opts) => new Promise((resolve, reject) => {
      calls.gets.push({ id, signal: opts?.signal, resolve, reject });
    }),
    renderProject: (view) => { state.view = 'workspace'; state.current = view; calls.rendered.push(view.project_id); },
    notify: (message, kind) => calls.notified.push({ message, kind }),
  }, registry);
  const switcher = createViewSwitcher({
    getState: () => state,
    patch: (partial) => Object.assign(state, partial),
    markActiveTab: (view) => calls.tabs.push(view),
    stopJobTracking: () => { calls.leaves += 1; registry.leave(); },
    renderPage: (view) => calls.pages.push(view),
    openProject: nav.openProject,
    goHome: () => { calls.home += 1; },
    captureWorkspace: (view) => calls.captured.push(view.project_id),
    renderCachedWorkspace: (view) => calls.cached.push(view.project_id),
  });
  return {
    state, calls, nav, switcher,
    settleGet(index, value, isError = false) {
      const call = calls.gets[index];
      if (isError) call.reject(value); else call.resolve(value);
    },
  };
}

test('状态页→慢工程 GET→重点击当前页签：中止在途导航，迟到 GET 不得切回工作区', async () => {
  const registry = createOperationRegistry();
  const app = fakeApp(registry, 'status');

  // 侧栏点击某工程 → 慢 GET in-flight（视图仍停留在状态页）
  const opening = app.nav.openProject('p1');
  assert.equal(app.calls.gets.length, 1);
  assert.equal(app.calls.gets[0].signal.aborted, false);

  // GET 返回前再次点击「状态」页签 = 留在本页的最新意图
  app.switcher.setView('status');
  assert.equal(app.calls.leaves, 1, '同状态页签重点击必须中止在途工程导航');
  assert.equal(app.calls.gets[0].signal.aborted, true, '在途 GET 应立即中止');
  assert.equal(app.state.view, 'status');
  assert.deepEqual(app.calls.tabs, [], '同页签点击不重复置激活态');

  // 慢 GET 迟到返回：世代守卫丢弃——不渲染、不报错、不切视图
  app.settleGet(0, { project_id: 'p1' });
  await opening;
  assert.deepEqual(app.calls.rendered, [], '迟到 GET 不得执行 renderProject');
  assert.equal(app.state.view, 'status', '界面必须停留在状态页');
  assert.equal(app.calls.notified.length, 0, '被中止的导航不得 toast 打扰');
});

test('对照：状态页打开工程后未再点击页签，GET 返回正常进入工作区', async () => {
  const registry = createOperationRegistry();
  const app = fakeApp(registry, 'status');

  const opening = app.nav.openProject('p1');
  app.settleGet(0, { project_id: 'p1' });
  await opening;
  assert.deepEqual(app.calls.rendered, ['p1']);
  assert.equal(app.state.view, 'workspace');
  assert.equal(app.calls.leaves, 0);
});

test('工作区同页签点击为忽略操作：不得中止在途工程导航（保护 job 跟踪语义）', async () => {
  const registry = createOperationRegistry();
  const app = fakeApp(registry, 'workspace');

  const opening = app.nav.openProject('p1'); // 工作区内刷新/重开工程的在途 GET
  app.switcher.setView('workspace');
  assert.equal(app.calls.leaves, 0, '工作区同页签点击不得触发 leave');
  assert.equal(app.calls.gets[0].signal.aborted, false, '在途 GET 不受影响');

  app.settleGet(0, { project_id: 'p1' });
  await opening;
  assert.deepEqual(app.calls.rendered, ['p1']);
});

test('当前任务设置页可接管在途导航并阻止迟到工程响应', async () => {
  const registry = createOperationRegistry();
  const app = fakeApp(registry, 'status');

  const opening = app.nav.openProject('p1');
  app.switcher.setView('settings');
  assert.equal(app.calls.leaves, 1);
  assert.equal(app.calls.gets[0].signal.aborted, true);
  assert.equal(app.state.view, 'settings');
  assert.deepEqual(app.calls.tabs, ['settings']);
  assert.deepEqual(app.calls.pages, ['settings']);

  app.settleGet(0, { project_id: 'p1' });
  await opening;
  assert.deepEqual(app.calls.rendered, []);
  assert.equal(app.state.view, 'settings');
});

test('切回工作区：有当前工程则重新打开，无当前工程则回首页', async () => {
  const registry = createOperationRegistry();
  const app = fakeApp(registry, 'status');

  // 无当前工程 → goHome
  app.switcher.setView('workspace');
  assert.equal(app.calls.home, 1);
  assert.equal(app.calls.gets.length, 0);

  // 有当前工程 → patch 视图 + 激活页签 + 经真实导航入口重开
  app.state.view = 'status';
  app.state.current = { project_id: 'p9' };
  app.switcher.setView('workspace');
  assert.equal(app.calls.home, 1);
  assert.equal(app.state.view, 'workspace');
  assert.deepEqual(app.calls.tabs, ['workspace']);
  assert.equal(app.calls.gets.length, 1);
  assert.equal(app.calls.gets[0].id, 'p9');
  assert.deepEqual(app.calls.cached, ['p9'], '应先同步恢复缓存工作区，再后台请求权威视图');
  app.settleGet(0, { project_id: 'p9' });
  await Promise.resolve();
});

test('离开工作区前捕获当前工程 UI 状态，同状态页点击不重复捕获', () => {
  const registry = createOperationRegistry();
  const app = fakeApp(registry, 'workspace');
  app.state.current = { project_id: 'p1' };

  app.switcher.setView('status');
  assert.deepEqual(app.calls.captured, ['p1']);
  app.switcher.setView('status');
  assert.deepEqual(app.calls.captured, ['p1']);
});

test('非法视图名直接忽略', () => {
  const registry = createOperationRegistry();
  const app = fakeApp(registry, 'status');
  app.switcher.setView('bogus');
  assert.equal(app.state.view, 'status');
  assert.equal(app.calls.leaves, 0);
  assert.deepEqual(app.calls.pages, []);
});

function fakeAuxPage(initialProject = 'p1', initialView = 'status') {
  const registry = createOperationRegistry();
  const state = { view: initialView, current: initialProject ? { project_id: initialProject } : null };
  const calls = { gets: [], pages: [], notified: [], lists: 0 };
  const refresher = createAuxPageRefresher({
    getState: () => state,
    getProject: (id, opts) => new Promise((resolve, reject) => {
      calls.gets.push({ id, signal: opts.signal, resolve, reject });
    }),
    loadProjects: () => { calls.lists += 1; },
    patch: (partial) => Object.assign(state, partial),
    renderPage: (view) => calls.pages.push({ view, project: state.current?.project_id }),
    notify: (message, kind) => calls.notified.push({ message, kind }),
  }, registry);
  return { registry, state, calls, refresher };
}

test('辅助页刷新：A 慢响应不得在切到 B 并返回同页签后覆盖 B', async () => {
  const app = fakeAuxPage('A', 'status');
  const refreshing = app.refresher.refresh();
  assert.equal(app.calls.gets[0].id, 'A');

  // 模拟真实侧栏导航：新操作世代接管，随后 B 工程进入状态页。
  app.registry.begin();
  app.state.current = { project_id: 'B' };
  app.state.view = 'status';
  assert.equal(app.calls.gets[0].signal.aborted, true);
  app.calls.gets[0].resolve({ project_id: 'A', snapshot: { state: 'late' } });
  await refreshing;

  assert.equal(app.state.current.project_id, 'B');
  assert.deepEqual(app.calls.pages, []);
  assert.deepEqual(app.calls.notified, []);
});

test('辅助页刷新：连续刷新只接受最新世代，且拒绝响应工程 ID 不匹配', async () => {
  const app = fakeAuxPage('A', 'status');
  const first = app.refresher.refresh();
  const second = app.refresher.refresh();
  assert.equal(app.calls.gets[0].signal.aborted, true);
  assert.equal(app.calls.gets[1].signal.aborted, false);

  app.calls.gets[0].resolve({ project_id: 'A', marker: 'old' });
  await first;
  app.calls.gets[1].resolve({ project_id: 'wrong', marker: 'bad' });
  await second;
  assert.deepEqual(app.state.current, { project_id: 'A' });
  assert.deepEqual(app.calls.pages, []);

  const third = app.refresher.refresh();
  app.calls.gets[2].resolve({ project_id: 'A', marker: 'fresh' });
  await third;
  assert.equal(app.state.current.marker, 'fresh');
  assert.deepEqual(app.calls.pages, [{ view: 'status', project: 'A' }]);
  assert.equal(app.calls.lists, 3);
});
