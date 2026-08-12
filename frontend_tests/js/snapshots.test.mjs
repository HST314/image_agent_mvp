import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  automaticBranchName,
  completedStageSnapshots,
  createSnapshotBranch,
  isCreatedBranchView,
} from '../../frontend/static/js/snapshots.js';

const checkpoint = (state, sequence, branch = 'main') => ({
  checkpoint_id: `checkpoint_${String(sequence).padStart(24, '0')}`,
  state, sequence, branch, snapshot: { state },
});

test('T9 仅返回当前阶段之前的已完成阶段，并保留每个阶段最后一份快照', () => {
  const items = [
    checkpoint('intake_clarify', 1),
    checkpoint('confirmation_build', 2),
    checkpoint('confirmation_build', 3),
    checkpoint('initial_candidate_generation', 4),
    checkpoint('master_candidate_selection', 5),
  ];
  const completed = completedStageSnapshots(items, { state: 'master_candidate_selection' });
  assert.deepEqual(completed.map((item) => item.state), [
    'intake_clarify', 'confirmation_build', 'initial_candidate_generation',
  ]);
  assert.equal(completed[1].sequence, 3);
});

test('T9 已完成工程允许回看最终交付快照', () => {
  const items = [checkpoint('final_approval', 9)];
  assert.deepEqual(
    completedStageSnapshots(items, { state: 'final_approval', completed: true }).map((item) => item.state),
    ['final_approval'],
  );
});

test('T9 自动分支名由中文来源阶段与本地时间组成', () => {
  const local = new Date(2026, 7, 12, 9, 7, 5);
  assert.equal(automaticBranchName('confirmation_build', local), '任务书-0812-090705');
  assert.match(automaticBranchName('unknown_state', local), /^状态未知-0812-090705$/);
});

const projectView = (branch = '任务书-0812-090705') => ({
  project_id: 'demo',
  manifest: {
    current_branch: branch,
    current_checkpoint: { branch, checkpoint_id: `checkpoint_${'1'.padStart(24, '0')}` },
  },
  snapshot: { state: 'confirmation_build' },
  progress_snapshots: [],
});

test('T9 创建即切换：直接消费创建接口返回的完整工程视图，不再二次 switch 或 GET', async () => {
  const calls = { create: 0, get: 0 };
  const created = projectView();
  const result = await createSnapshotBranch({
    projectId: 'demo', checkpoint: 'checkpoint_source', branchName: '任务书-0812-090705',
  }, {
    branchFrom: async (projectId, payload) => {
      calls.create += 1;
      assert.equal(projectId, 'demo');
      assert.deepEqual(payload, { checkpoint: 'checkpoint_source', name: '任务书-0812-090705' });
      return created;
    },
    getProject: async () => { calls.get += 1; return projectView('main'); },
  });

  assert.equal(result.view, created);
  assert.equal(result.reconciled, false);
  assert.deepEqual(calls, { create: 1, get: 0 });
});

test('T9 创建响应异常：只重新拉取工程对账，确认已创建后不重复 POST', async () => {
  const calls = { create: 0, get: 0 };
  const reconciled = projectView();
  const result = await createSnapshotBranch({
    projectId: 'demo', checkpoint: 'checkpoint_source', branchName: '任务书-0812-090705',
  }, {
    branchFrom: async () => { calls.create += 1; throw new Error('响应解析失败'); },
    getProject: async () => { calls.get += 1; return reconciled; },
  });

  assert.equal(result.view, reconciled);
  assert.equal(result.reconciled, true);
  assert.deepEqual(calls, { create: 1, get: 1 }, '故障恢复只能对账，不得补发创建导致同名冲突');
});

test('T9 创建返回异常视图：重新拉取可恢复；对账不匹配则保留原始错误', async () => {
  const calls = { create: 0, get: 0 };
  const deps = {
    branchFrom: async () => { calls.create += 1; return { project_id: 'demo' }; },
    getProject: async () => { calls.get += 1; return projectView('main'); },
  };

  await assert.rejects(
    createSnapshotBranch({
      projectId: 'demo', checkpoint: 'checkpoint_source', branchName: '任务书-0812-090705',
    }, deps),
    /未返回完整的新分支工程视图/,
  );
  assert.deepEqual(calls, { create: 1, get: 1 });
  assert.equal(isCreatedBranchView(projectView('main'), {
    projectId: 'demo', branchName: '任务书-0812-090705',
  }), false);
});
