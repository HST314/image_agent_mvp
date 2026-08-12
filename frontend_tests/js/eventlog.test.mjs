/* T2 状态页事件日志纯逻辑回归（契约 §4/Q2-A）：
 * currentActionSeq（当前动作推导）与 createEventLogFollower（SSE 快照流
 * 游标重连续传）。全部用注入的可控流与手工调度，不触碰 DOM/网络。 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { currentActionSeq, createEventLogFollower } from '../../frontend/static/js/eventlog.js';

/* ---------- currentActionSeq ---------- */

test('currentActionSeq：无事件或非数组返回 null', () => {
  assert.equal(currentActionSeq([]), null);
  assert.equal(currentActionSeq(null), null);
  assert.equal(currentActionSeq(undefined), null);
});

test('currentActionSeq：已开始未闭环的步骤即当前动作', () => {
  const events = [
    { sequence: 1, type: 'project_created' },
    { sequence: 2, type: 'step_started', state: 'intake_clarify' },
  ];
  assert.equal(currentActionSeq(events), 2);
});

test('currentActionSeq：同状态闭环后不再是当前动作', () => {
  const events = [
    { sequence: 2, type: 'step_started', state: 'intake_clarify' },
    { sequence: 3, type: 'step_succeeded', state: 'intake_clarify' },
  ];
  assert.equal(currentActionSeq(events), null);
});

test('currentActionSeq：多状态时取最新未闭环开始；失败也算闭环', () => {
  const events = [
    { sequence: 2, type: 'step_started', state: 'intake_clarify' },
    { sequence: 3, type: 'step_succeeded', state: 'intake_clarify' },
    { sequence: 4, type: 'step_started', state: 'confirmation_build' },
    { sequence: 5, type: 'step_failed', state: 'confirmation_build' },
    { sequence: 6, type: 'retry_started', state: 'confirmation_build' },
  ];
  assert.equal(currentActionSeq(events), 6, '重试开始未闭环即当前动作');
});

test('currentActionSeq：缺 sequence/state 的事件安全跳过', () => {
  const events = [
    { type: 'step_started', state: 'intake_clarify' },
    { sequence: 'x', type: 'step_started', state: 'confirmation_build' },
    { sequence: 7, type: 'step_started' },
    null,
  ];
  assert.equal(currentActionSeq(events), null);
});

/* ---------- createEventLogFollower ---------- */

/* 手工调度器：记录定时回调，tick() 逐一触发，避免真实等待。 */
function manualSchedule() {
  const queue = [];
  return {
    schedule: (fn) => queue.push(fn),
    async tick(times = 1) {
      for (let i = 0; i < times; i += 1) {
        const fn = queue.shift();
        if (!fn) return;
        fn();
        await Promise.resolve();
      }
    },
    pending: () => queue.length,
  };
}

/* 可控流工厂：每次 openStream(after) 从脚本队列取一段事件（或抛错）。 */
function scriptedStreams(script) {
  const calls = [];
  return {
    calls,
    openStream(after) {
      const step = script[calls.length] ?? [];
      calls.push(after);
      if (step instanceof Error) return (async function* () { throw step; })();
      return (async function* () { for (const event of step) yield event; })();
    },
  };
}

test('跟随器：按游标推送新事件批次并推进游标', async () => {
  const timer = manualSchedule();
  const streams = scriptedStreams([
    [{ sequence: 3, type: 'step_started', state: 'intake_clarify' }, { sequence: 4, type: 'step_succeeded', state: 'intake_clarify' }],
    [{ sequence: 5, type: 'step_started', state: 'confirmation_build' }],
  ]);
  const batches = [];
  const follower = createEventLogFollower({
    openStream: streams.openStream,
    onBatch: (batch, cursor) => batches.push({ batch: batch.map((e) => e.sequence), cursor }),
    initialAfter: 2,
    schedule: timer.schedule,
  });
  await flush();
  assert.deepEqual(batches, [{ batch: [3, 4], cursor: 4 }], '首批按序上屏并推进游标');
  await timer.tick();
  await flush();
  assert.deepEqual(batches[1], { batch: [5], cursor: 5 });
  follower.stop();
  await timer.tick();
  await follower.done;
});

test('跟随器：流结束后按最新游标重连续传；非递增序号不重复推送', async () => {
  const timer = manualSchedule();
  const streams = scriptedStreams([
    [{ sequence: 3, type: 'a' }, { sequence: 4, type: 'b' }],
    [{ sequence: 4, type: 'b' }, { sequence: 5, type: 'c' }], // 重发 4 + 新增 5
    [{ sequence: 6, type: 'd' }],
  ]);
  const batches = [];
  const follower = createEventLogFollower({
    openStream: streams.openStream,
    onBatch: (batch, cursor) => batches.push({ batch: batch.map((e) => e.sequence), cursor }),
    initialAfter: 2,
    schedule: timer.schedule,
  });
  await flush();
  assert.deepEqual(batches, [{ batch: [3, 4], cursor: 4 }]);
  assert.deepEqual(streams.calls, [2]);

  await timer.tick(); // 第二次开流
  await flush();
  assert.deepEqual(batches[1], { batch: [5], cursor: 5 }, '游标去重：4 不重复上屏');
  assert.deepEqual(streams.calls, [2, 4], '按已推进游标重连');

  await timer.tick();
  await flush();
  assert.deepEqual(batches[2], { batch: [6], cursor: 6 });
  follower.stop();
  await timer.tick();
  await follower.done;
});

test('跟随器：流读取失败不致命，等待下一拍按最新游标重试', async () => {
  const timer = manualSchedule();
  const streams = scriptedStreams([
    new Error('SSE_UNAVAILABLE'),
    [{ sequence: 9, type: 'step_started', state: 'self_check_iteration' }],
  ]);
  const batches = [];
  const follower = createEventLogFollower({
    openStream: streams.openStream,
    onBatch: (batch, cursor) => batches.push({ batch: batch.map((e) => e.sequence), cursor }),
    initialAfter: 8,
    schedule: timer.schedule,
  });
  await flush();
  assert.deepEqual(batches, [], '首轮流失败不产生批次');
  await timer.tick();
  await flush();
  assert.deepEqual(batches, [{ batch: [9], cursor: 9 }], '失败重连后续传不丢事件');
  follower.stop();
  await timer.tick();
  await follower.done;
});

test('跟随器：stop() 后退出循环且不再开流；cursor() 供暂停后恢复', async () => {
  const timer = manualSchedule();
  const streams = scriptedStreams([[{ sequence: 2, type: 'a' }]]);
  const follower = createEventLogFollower({
    openStream: streams.openStream,
    onBatch: () => {},
    initialAfter: 1,
    schedule: timer.schedule,
  });
  await flush();
  assert.equal(follower.cursor(), 2);
  follower.stop();
  await timer.tick();
  await follower.done;
  const opened = streams.calls.length;
  await timer.tick(3);
  assert.equal(streams.calls.length, opened, '停止后不得再开流');
});

test('跟随器：signal 中止即退出', async () => {
  const timer = manualSchedule();
  const controller = new AbortController();
  const follower = createEventLogFollower({
    openStream: scriptedStreams([[]]).openStream,
    onBatch: () => {},
    signal: controller.signal,
    schedule: timer.schedule,
  });
  await flush();
  controller.abort();
  await timer.tick();
  await follower.done;
  assert.ok(true, 'done 正常兑现');
});

async function flush(times = 8) {
  for (let i = 0; i < times; i += 1) await Promise.resolve();
}
