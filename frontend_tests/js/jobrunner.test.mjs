/* H1/M1 修正轮验收核心：操作世代闭合延时刷新与 POST in-flight 生命周期窗口；
 * 幂等键按终态与副作用确定性清除（对齐真实 error.category/error.code 契约）；
 * 侧栏导航经真实 openProject 入口在意图发生时中止旧操作、GET 绑定导航世代。
 * 三类异步交错回归：① job A 完成后同工程启动 job B；② 切页发生在 POST
 * in-flight（含真实导航入口的慢 GET/连续点击）；③ job 接受后失败/未知再重试。 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  createOperationRegistry, createJobRunner, createNavigator, shouldClearIntentKey, isTerminalJobStatus,
} from '../../frontend/static/js/jobrunner.js';
import { loadDraft } from '../../frontend/static/js/store.js';

/* 可控假依赖：手工调度器 + 可编程 POST/track，精确编排异步交错顺序。 */
function fakeDeps(projectId = 'p1') {
  const calls = { post: [], track: [], refresh: [], notify: [], records: [], timers: [] };
  let checkpoint = '';
  let currentProjectId = projectId;
  const deps = {
    projectId,
    renderProgress: (job) => ({ job, updates: [], dones: [], update(r) { this.updates.push(r); }, done(r) { this.dones.push(r); } }),
    clearProgress: () => {},
    setBusy: () => {},
    notify: (message, kind) => calls.notify.push({ message, kind }),
    refresh: (op) => calls.refresh.push(op),
    getProjectId: () => currentProjectId,
    getCheckpoint: () => checkpoint,
    postJob: (body, opts) => new Promise((resolve, reject) => {
      calls.post.push({ body, signal: opts?.signal, resolve, reject });
    }),
    track: (jobId, { signal, onEvent, onDone }) => { calls.track.push({ jobId, signal, onEvent, onDone }); },
    cancelJob: async () => {},
    onJobRecord: (record) => calls.records.push(record),
    schedule: (fn) => { calls.timers.push(fn); },
  };
  return {
    calls, deps,
    setCheckpoint: (v) => { checkpoint = v; },
    setProject: (v) => { currentProjectId = v; },
    settlePost(index, value, isError = false) {
      const call = calls.post[index];
      if (isError) call.reject(value); else call.resolve(value);
    },
  };
}

/* 可控导航假依赖：真实 createNavigator 入口（与 app.js 侧栏接线同一实现），
 * GET 手工 settle，精确编排慢 GET/连续点击/POST 交错的返回顺序。 */
function fakeNav(registry) {
  const calls = { gets: [], rendered: [], notified: [], afterOpen: 0 };
  const nav = createNavigator({
    getProject: (id, opts) => new Promise((resolve, reject) => {
      calls.gets.push({ id, signal: opts?.signal, resolve, reject });
    }),
    renderProject: (view) => calls.rendered.push(view.project_id),
    afterOpen: () => { calls.afterOpen += 1; },
    notify: (message, kind) => calls.notified.push({ message, kind }),
  }, registry);
  return {
    nav, calls,
    settleGet(index, value, isError = false) {
      const call = calls.gets[index];
      if (isError) call.reject(value); else call.resolve(value);
    },
  };
}

test('交错①：job A 完成后同工程启动 job B，A 的延时刷新被丢弃且 B 的跟踪存活', async () => {
  const registry = createOperationRegistry();
  const { calls, deps, settlePost } = fakeDeps();
  const runner = createJobRunner(deps, registry);

  const first = runner.start({ selected_id: 'a' }, { intent: 'select' });
  settlePost(0, { job_id: 'job_A', project_id: 'p1', status: 'queued' });
  await first;
  assert.equal(calls.track.length, 1);

  // A 到达终态 → 安排 300ms 延时刷新，busy 释放
  calls.track[0].onDone({ job_id: 'job_A', status: 'succeeded' });
  assert.equal(calls.timers.length, 1);

  // 用户在 A 的定时器触发前于同一工程启动 job B
  const second = runner.start({ selected_id: 'b' }, { intent: 'select' });
  settlePost(1, { job_id: 'job_B', project_id: 'p1', status: 'queued' });
  await second;
  assert.equal(calls.track.length, 2);

  // A 的定时器触发：不得执行过期刷新，不得中止仍在运行的 B
  calls.timers.shift()();
  assert.equal(calls.refresh.length, 0);
  assert.equal(calls.track[1].signal.aborted, false);

  // B 正常完成后，只有 B 的刷新生效
  calls.track[1].onDone({ job_id: 'job_B', status: 'succeeded' });
  assert.equal(calls.timers.length, 1);
  calls.timers.shift()();
  assert.equal(calls.refresh.length, 1);
});

test('交错①变体：A 完成后离开工程视图，A 的延时刷新不得覆盖首页', async () => {
  const registry = createOperationRegistry();
  const { calls, deps, settlePost, setProject } = fakeDeps();
  const runner = createJobRunner(deps, registry);

  const pending = runner.start({ selected_id: 'a' }, { intent: 'select' });
  settlePost(0, { job_id: 'job_A', project_id: 'p1', status: 'queued' });
  await pending;
  calls.track[0].onDone({ job_id: 'job_A', status: 'succeeded' });
  assert.equal(calls.timers.length, 1);

  // 用户回首页（stopJobTracking → leave），随后侧栏显示列表
  registry.leave();
  setProject(undefined);
  calls.timers.shift()();
  assert.equal(calls.refresh.length, 0);
});

test('交错②：POST in-flight 时经真实 openProject 入口切页——意图即中止提交，GET 绑定导航世代', async () => {
  const registry = createOperationRegistry();
  const { calls, deps, settlePost } = fakeDeps();
  const runner = createJobRunner(deps, registry);
  const sidebar = fakeNav(registry);

  const pending = runner.start({ task_approved: true, actor: 'u1' }, { intent: 'task' });
  assert.equal(calls.post.length, 1);
  assert.equal(calls.post[0].signal.aborted, false);

  // 真实导航入口（侧栏点击另一工程）：意图发生即同步中止 in-flight POST，
  // 不等新工程 GET 返回；GET 自身绑定导航世代 signal。
  const opening = sidebar.nav.openProject('p2');
  assert.equal(calls.post[0].signal.aborted, true, '导航意图发生即中止 in-flight POST');
  assert.equal(sidebar.calls.gets.length, 1);
  assert.ok(sidebar.calls.gets[0].signal, 'GET 必须绑定导航世代 signal');
  assert.equal(sidebar.calls.gets[0].signal.aborted, false);

  // 新工程 GET 返回：渲染 p2；旧 POST 迟到的返回（后端已创建 job）静默丢弃，
  // 不 attach、不中止新视图恢复出的 tracker（由既有 active_job 恢复逻辑接管）。
  sidebar.settleGet(0, { project_id: 'p2' });
  await opening;
  assert.deepEqual(sidebar.calls.rendered, ['p2']);
  assert.equal(sidebar.calls.afterOpen, 1);
  const resumed = registry.begin(); // 新视图渲染后 attach 的 tracker 世代
  settlePost(0, { job_id: 'job_late', project_id: 'p1', status: 'queued' });
  assert.equal(await pending, null);
  assert.equal(calls.track.filter((t) => t.jobId === 'job_late').length, 0);
  assert.equal(resumed.controller.signal.aborted, false);
  // 幂等键保留（POST 中止、结果未知）：同指纹重试复用同一键供后端去重
  const reentered = createJobRunner(deps, registry);
  const retry = reentered.start({ task_approved: true, actor: 'u1' }, { intent: 'task' });
  settlePost(1, { job_id: 'job_new', project_id: 'p1', status: 'queued' });
  await retry;
  assert.equal(calls.post[1].body.idempotency_key, calls.post[0].body.idempotency_key);
});

test('交错②连续点击：慢 GET 交错——意图即中止旧 GET，先发 GET 迟到不得覆盖新视图', async () => {
  const registry = createOperationRegistry();
  const sidebar = fakeNav(registry);

  const first = sidebar.nav.openProject('p1'); // GET1 in-flight（慢）
  const second = sidebar.nav.openProject('p2'); // 连续点击：意图发生即中止 GET1
  assert.equal(sidebar.calls.gets.length, 2);
  assert.equal(sidebar.calls.gets[0].signal.aborted, true, '第二次导航意图立即中止第一次的 GET');
  assert.equal(sidebar.calls.gets[1].signal.aborted, false);

  // GET2 先返回：渲染 p2
  sidebar.settleGet(1, { project_id: 'p2' });
  await second;
  assert.deepEqual(sidebar.calls.rendered, ['p2']);

  // GET1 在中止竞态下仍 resolve（迟到响应）：世代守卫丢弃，不得覆盖 p2
  sidebar.settleGet(0, { project_id: 'p1' });
  await first;
  assert.deepEqual(sidebar.calls.rendered, ['p2'], '先发 GET 迟到返回不得覆盖新视图');
  assert.equal(sidebar.calls.afterOpen, 1);
  assert.equal(sidebar.calls.notified.length, 0);
});

test('交错②导航失败：当前导航 GET 抛错→报错；被取代后迟到抛错→静默', async () => {
  const registry = createOperationRegistry();
  const sidebar = fakeNav(registry);

  // 当前导航的 GET 失败：提示错误，不渲染
  const failing = sidebar.nav.openProject('p1');
  sidebar.settleGet(0, new Error('无法连接服务。'), true);
  await failing;
  assert.equal(sidebar.calls.notified.length, 1);
  assert.equal(sidebar.calls.notified[0].kind, 'error');
  assert.deepEqual(sidebar.calls.rendered, []);

  // 被更新导航取代后旧 GET 才失败（中止竞态）：静默，不打扰新视图
  const stale = sidebar.nav.openProject('p2');
  const current = sidebar.nav.openProject('p3');
  sidebar.settleGet(1, new Error('操作已随视图切换取消。'), true);
  await stale;
  assert.equal(sidebar.calls.notified.length, 1, '过期导航的失败不得再 toast');
  sidebar.settleGet(2, { project_id: 'p3' });
  await current;
  assert.deepEqual(sidebar.calls.rendered, ['p3']);
});

test('交错②变体：POST 因视图切换中止而抛错时静默，不报超时错误', async () => {
  const registry = createOperationRegistry();
  const { calls, deps, settlePost } = fakeDeps();
  const runner = createJobRunner(deps, registry);

  const pending = runner.start({ manual_action: 'execute' }, { intent: 'manual' });
  registry.leave();
  settlePost(0, new Error('操作已随视图切换取消。'), true);
  assert.equal(await pending, null);
  assert.equal(calls.notify.length, 0); // 已离开视图：不再 toast 打扰
});

test('交错③：终态键策略——succeeded/已知失败清除，未知失败/cancelled/interrupted 保留', () => {
  assert.equal(shouldClearIntentKey({ status: 'succeeded' }), true);
  // 已知结果的失败（鉴权/限额/审核/输入）：清除，重试为知情新执行
  assert.equal(shouldClearIntentKey({ status: 'failed', error: { message: 'AUTH failed: invalid API key' } }), true);
  assert.equal(shouldClearIntentKey({ status: 'failed', error: { message: 'RATE LIMIT exceeded' } }), true);
  assert.equal(shouldClearIntentKey({ status: 'failed', error: { message: '内容未通过审核' } }), true);
  // 未知结果的失败（message 兜底，与后端 classify_error 的判定一致）：保留
  assert.equal(shouldClearIntentKey({ status: 'failed', error: { message: 'Model request TIMEOUT after 60s' } }), false);
  assert.equal(shouldClearIntentKey({ status: 'failed', error: { message: '调用结果 UNKNOWN' } }), false);
  // 执行结果不可知的终态：保留
  assert.equal(shouldClearIntentKey({ status: 'cancelled' }), false);
  assert.equal(shouldClearIntentKey({ status: 'interrupted' }), false);
  assert.equal(shouldClearIntentKey({ status: 'unknown', error: { message: 'poll failed' } }), false);
});

test('交错③契约：按真实 error.category/error.code 记录分类（agent_core/jobs.py 写入形态）', () => {
  // 真实记录形态：jobs.py 写入 {code: 异常类型名或 exc.code, message: str(exc)}，
  // 异常携带规范化 category 时一并透出（ModelCallError 等）。
  // TimeoutError("x")：消息不含 TIMEOUT 字样，必须按 code 判为结果未知 → 保留键
  assert.equal(shouldClearIntentKey({ status: 'failed', error: { code: 'TimeoutError', message: 'x' } }), false);
  assert.equal(shouldClearIntentKey({ status: 'failed', error: { code: 'ReadTimeoutError', message: 'HTTPSConnectionPool read timed out' } }), false);
  // 规范化 category：以 _unknown 结尾（可能已扣费，对齐 gateway possible_charge）→ 保留
  assert.equal(shouldClearIntentKey({ status: 'failed', error: { code: 'ModelCallError', message: 'x', category: 'timeout_unknown' } }), false);
  assert.equal(shouldClearIntentKey({ status: 'failed', error: { code: 'ModelCallError', message: 'x', category: 'transport_unknown' } }), false);
  assert.equal(shouldClearIntentKey({ status: 'failed', error: { code: 'ModelCallError', message: '429', category: 'rate_limited_unknown' } }), false);
  // 已知分类（请求被拒绝/输入校验，未发生付费副作用）→ 清除，重试为知情新执行
  assert.equal(shouldClearIntentKey({ status: 'failed', error: { code: 'ModelCallError', message: 'bad key', category: 'validation_or_refusal' } }), true);
  assert.equal(shouldClearIntentKey({ status: 'failed', error: { code: 'ModelCallError', message: 'HTTP 401', category: 'request_rejected' } }), true);
  // category 为权威分类：存在时优先于 message 兜底扫描
  assert.equal(shouldClearIntentKey({ status: 'failed', error: { code: 'ModelCallError', message: 'read TIMEOUT', category: 'request_rejected' } }), true);
  // 无 category 的已知失败（审批门禁/输入校验）：清除
  assert.equal(shouldClearIntentKey({ status: 'failed', error: { code: 'ValueError', message: 'TASK_APPROVAL_REQUIRED' } }), true);
});

test('交错③端到端：job 接受后成功→键清除轮换；超时失败→键保留复用；已知失败→键清除轮换', async () => {
  const registry = createOperationRegistry();
  const { calls, deps, settlePost, setCheckpoint } = fakeDeps('p-terminal');
  const runner = createJobRunner(deps, registry);
  const payload = { selected_id: 'a' };
  const keyAt = (i) => calls.post[i].body.idempotency_key;

  // 1) 成功：HTTP 接受后、终态前键必须仍在（v1 在此过早清除——本断言锁定该回归）；
  //    终态成功后清除，下一次生成新键。
  let pending = runner.start(payload, { intent: 'select' });
  settlePost(0, { job_id: 'j1', project_id: 'p-terminal', status: 'queued' });
  await pending;
  assert.ok(loadDraft('p-terminal', 'idem:select'), 'HTTP 接受后、终态前幂等键不得清除');
  calls.track[0].onDone({ job_id: 'j1', status: 'succeeded' });
  assert.equal(loadDraft('p-terminal', 'idem:select'), null, '终态成功后幂等键应清除');
  setCheckpoint('2'); // 成功推进检查点
  pending = runner.start(payload, { intent: 'select' });
  settlePost(1, { job_id: 'j2', project_id: 'p-terminal', status: 'queued' });
  await pending;
  assert.notEqual(keyAt(1), keyAt(0));

  // 2) 超时失败（结果未知）：键保留，同指纹重试复用同一键去重
  calls.track[1].onDone({ job_id: 'j2', status: 'failed', error: { message: 'Model request TIMEOUT after 60s' } });
  assert.ok(loadDraft('p-terminal', 'idem:select'), '未知失败应保留幂等键');
  pending = runner.start(payload, { intent: 'select' });
  settlePost(2, { job_id: 'j3', project_id: 'p-terminal', status: 'queued' });
  await pending;
  assert.equal(keyAt(2), keyAt(1));

  // 3) 已知失败（鉴权）：键清除，重试轮换新键真正重执行
  calls.track[2].onDone({ job_id: 'j3', status: 'failed', error: { message: 'AUTH failed' } });
  assert.equal(loadDraft('p-terminal', 'idem:select'), null, '已知失败应清除幂等键');
  pending = runner.start(payload, { intent: 'select' });
  settlePost(3, { job_id: 'j4', project_id: 'p-terminal', status: 'queued' });
  await pending;
  assert.notEqual(keyAt(3), keyAt(2));
});

test('M1 主路径回归：POST 抛错（120s 超时响应丢失）保留键，重试复用同一键', async () => {
  const registry = createOperationRegistry();
  const { calls, deps, settlePost } = fakeDeps('p-retry');
  const runner = createJobRunner(deps, registry);

  const first = runner.start({ selected_id: 'a' }, { intent: 'select' });
  settlePost(0, new Error('请求超时。后端可能仍在处理，请稍后刷新工程状态。'), true);
  assert.equal(await first, null);

  const retry = runner.start({ selected_id: 'a' }, { intent: 'select' });
  settlePost(1, { job_id: 'job_1', project_id: 'p-retry', status: 'queued' });
  await retry;
  assert.equal(calls.post[1].body.idempotency_key, calls.post[0].body.idempotency_key);
});

test('isTerminalJobStatus 终态集合', () => {
  for (const s of ['succeeded', 'failed', 'cancelled', 'interrupted']) assert.equal(isTerminalJobStatus(s), true);
  for (const s of ['queued', 'running', 'cancelling', 'submitting', 'unknown']) assert.equal(isTerminalJobStatus(s), false);
});
