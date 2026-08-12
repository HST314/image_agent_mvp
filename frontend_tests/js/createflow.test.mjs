import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createImmediateProjectFlow } from '../../frontend/static/js/createflow.js';
import { createOperationRegistry } from '../../frontend/static/js/jobrunner.js';

function deferredCreate() {
  const calls = [];
  const createProject = (payload, opts) => new Promise((resolve, reject) => {
    calls.push({ payload, signal: opts.signal, resolve, reject });
  });
  return { calls, createProject };
}

test('T10：接口未返回时已立即关闭弹窗并跳转工作台等待态', async () => {
  const registry = createOperationRegistry();
  const api = deferredCreate();
  const events = [];
  const flow = createImmediateProjectFlow({
    createProject: api.createProject,
    showPending: (payload) => { events.push(`pending:${payload.project_id}`); return { marker: 'pending' }; },
    showCreated: (view, pending) => events.push(`created:${view.project_id}:${pending.marker}`),
  }, registry);

  const pending = flow.start({ project_id: 'p1' });
  assert.deepEqual(events, ['pending:p1'], '等待网络前必须已执行关闭弹窗/跳工作台回调');
  assert.equal(api.calls.length, 1);
  assert.equal(api.calls[0].signal.aborted, false);

  api.calls[0].resolve({ project_id: 'p1' });
  assert.deepEqual(await pending, { project_id: 'p1' });
  assert.deepEqual(events, ['pending:p1', 'created:p1:pending']);
});

test('T10：当前创建失败恢复可修正错误；导航后的迟到结果静默丢弃', async () => {
  const registry = createOperationRegistry();
  const api = deferredCreate();
  const events = [];
  const flow = createImmediateProjectFlow({
    createProject: api.createProject,
    showPending: (payload) => events.push(`pending:${payload.project_id}`),
    showCreated: (view) => events.push(`created:${view.project_id}`),
    showError: (error) => events.push(`error:${error.message}`),
  }, registry);

  let request = flow.start({ project_id: 'failed' });
  api.calls[0].reject(new Error('工程 ID 已存在'));
  assert.equal(await request, null);
  assert.deepEqual(events, ['pending:failed', 'error:工程 ID 已存在']);

  request = flow.start({ project_id: 'late' });
  registry.begin(); // 用户已打开另一工程
  assert.equal(api.calls[1].signal.aborted, true);
  api.calls[1].resolve({ project_id: 'late' });
  assert.equal(await request, null);
  assert.deepEqual(events, ['pending:failed', 'error:工程 ID 已存在', 'pending:late']);
});
