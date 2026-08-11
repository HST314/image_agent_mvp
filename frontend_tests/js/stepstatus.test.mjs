/* T10 实时状态纯逻辑回归：liveStepText 只认真实 timeline 的 step_started 事件
 * （不做前端假状态，未知英文 id 不上屏）；createTimelineFollower 的游标推进、
 * stop/abort 退出与拉取失败容忍。 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { liveStepText, maxSequence, createTimelineFollower } from '../../frontend/static/js/stepstatus.js';

const flush = () => new Promise((resolve) => setImmediate(resolve));

test('liveStepText：空输入与非数组返回 null', () => {
  assert.equal(liveStepText(null), null);
  assert.equal(liveStepText([]), null);
  assert.equal(liveStepText('x'), null);
});

test('liveStepText：step_started 映射为中文进行中文案，最新一条生效', () => {
  assert.equal(
    liveStepText([{ type: 'step_started', state: 'intake_clarify', sequence: 2 }]),
    '正在理解任务书，生成澄清问题…',
  );
  const text = liveStepText([
    { type: 'project_created', sequence: 1 },
    { type: 'step_started', state: 'intake_clarify', sequence: 2 },
    { type: 'step_succeeded', state: 'intake_clarify', sequence: 3 },
    { type: 'step_started', state: 'confirmation_build', sequence: 4 },
  ]);
  assert.equal(text, '正在生成任务书…');
});

test('liveStepText：非 step_started 与未知状态不产生文案（英文 id 不上屏）', () => {
  assert.equal(liveStepText([{ type: 'step_succeeded', state: 'intake_clarify' }]), null);
  assert.equal(liveStepText([{ type: 'step_started', state: 'mystery_state' }]), null);
  assert.equal(liveStepText([{ type: 'step_started' }]), null);
});

test('maxSequence：推进到最大有效序号，忽略缺失/非法值', () => {
  assert.equal(maxSequence([{ sequence: 3 }, { sequence: 7 }, {}, { sequence: 'x' }], 2), 7);
  assert.equal(maxSequence([], 5), 5);
  assert.equal(maxSequence(null, 4), 4);
});

/* 可控假依赖：fetchPage 依次返回编程页，schedule 手工泵送。 */
function fakeDeps(pages) {
  const calls = { fetches: [], timers: [], texts: [] };
  return {
    calls,
    fetchPage: (after, opts) => {
      calls.fetches.push({ after, signal: opts?.signal });
      const next = pages.length ? pages.shift() : { items: [] };
      return next instanceof Error ? Promise.reject(next) : Promise.resolve(next);
    },
    schedule: (fn) => calls.timers.push(fn),
    onText: (text) => calls.texts.push(text),
  };
}

test('follower：推送真实步骤文案并按序号推进游标', async () => {
  const deps = fakeDeps([
    { items: [{ type: 'project_created', sequence: 1 }, { type: 'step_started', state: 'intake_clarify', sequence: 2 }] },
    { items: [{ type: 'step_started', state: 'confirmation_build', sequence: 4 }] },
  ]);
  const follower = createTimelineFollower({ ...deps, onText: deps.onText, intervalMs: 5 });
  await flush(); // 第一拍：拉到 intake_clarify
  assert.deepEqual(deps.calls.texts, ['正在理解任务书，生成澄清问题…']);
  assert.equal(deps.calls.fetches[0].after, 0);
  deps.calls.timers.shift()(); // 放行第二拍
  await flush();
  assert.equal(deps.calls.fetches[1].after, 2); // 游标推进到已见最大序号
  assert.deepEqual(deps.calls.texts, ['正在理解任务书，生成澄清问题…', '正在生成任务书…']);
  follower.stop();
  deps.calls.timers.shift()?.();
  await follower.done;
});

test('follower：拉取失败容忍并继续，不产生假状态', async () => {
  const deps = fakeDeps([
    new Error('网络抖动'),
    { items: [{ type: 'step_started', state: 'self_check_iteration', sequence: 9 }] },
  ]);
  const follower = createTimelineFollower({ ...deps, onText: deps.onText, intervalMs: 5 });
  await flush(); // 第一拍失败：无文案
  assert.deepEqual(deps.calls.texts, []);
  deps.calls.timers.shift()();
  await flush();
  assert.deepEqual(deps.calls.texts, ['正在质检画面…']);
  follower.stop();
  deps.calls.timers.shift()?.();
  await follower.done;
});

test('follower：stop() 与 signal 中止都能退出循环', async () => {
  const a = fakeDeps([{ items: [] }]);
  const f1 = createTimelineFollower({ ...a, onText: a.onText, intervalMs: 5 });
  await flush();
  f1.stop();
  a.calls.timers.shift()?.();
  await f1.done; // 正常退出即通过

  const b = fakeDeps([{ items: [] }]);
  const controller = new AbortController();
  const f2 = createTimelineFollower({ ...b, onText: b.onText, signal: controller.signal, intervalMs: 5 });
  await flush();
  controller.abort();
  b.calls.timers.shift()?.();
  await f2.done;
});

test('follower：initialAfter 尊重调用方给定的历史游标', async () => {
  const deps = fakeDeps([{ items: [{ type: 'step_started', state: 'final_approval', sequence: 42 }] }]);
  const follower = createTimelineFollower({ ...deps, onText: deps.onText, intervalMs: 5, initialAfter: 40 });
  await flush();
  assert.equal(deps.calls.fetches[0].after, 40);
  assert.deepEqual(deps.calls.texts, ['正在完成最终确认…']);
  follower.stop();
  deps.calls.timers.shift()?.();
  await follower.done;
});
