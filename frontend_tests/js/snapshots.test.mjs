import { test } from 'node:test';
import assert from 'node:assert/strict';
import { automaticBranchName, completedStageSnapshots } from '../../frontend/static/js/snapshots.js';

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
