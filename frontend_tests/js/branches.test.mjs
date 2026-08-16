/* 分支查看界面：branchListModel 纯函数回归。 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { branchListModel } from '../../frontend/static/js/branches.js';

const payload = {
  current_branch: 'main',
  current_checkpoint_id: 'checkpoint_main2',
  items: [
    {
      name: 'main', parent: null, mode: 'fork_after', created_at: '2026-08-15T06:00:00Z', current: true,
      checkpoints: [
        { checkpoint_id: 'checkpoint_main1', branch: 'main', sequence: 1, state: 'category_constraint' },
        { checkpoint_id: 'checkpoint_main2', branch: 'main', sequence: 2, state: 'intake_clarify' },
      ],
    },
    {
      name: '艺术风格-0816-132659', parent: 'main', mode: 'rerun_stage',
      created_at: '2026-08-16T05:26:59Z', current: false,
      checkpoints: [
        { checkpoint_id: 'checkpoint_branch1', branch: '艺术风格-0816-132659', sequence: 1, state: 'initial_candidate_generation' },
      ],
    },
    {
      name: '艺术风格-0816-120000', parent: 'main', mode: 'rerun_stage',
      created_at: '2026-08-16T04:00:00Z', current: false,
      checkpoints: [],
    },
  ],
};

test('当前分支置顶，其余按创建时间倒序', () => {
  const rows = branchListModel(payload);
  assert.deepEqual(rows.map((row) => row.name), ['main', '艺术风格-0816-132659', '艺术风格-0816-120000']);
  assert.equal(rows[0].current, true);
});

test('切换目标取分支头检查点（sequence 最大者）', () => {
  const rows = branchListModel(payload);
  assert.equal(rows[0].headCheckpointId, 'checkpoint_main2');
  assert.equal(rows[0].headState, 'intake_clarify');
  assert.equal(rows[0].headSequence, 2);
  assert.equal(rows[1].headCheckpointId, 'checkpoint_branch1');
  // 无检查点的分支不可切换。
  assert.equal(rows[2].headCheckpointId, null);
});

test('容错：空负载/缺字段不抛错', () => {
  assert.deepEqual(branchListModel(null), []);
  assert.deepEqual(branchListModel({}), []);
  const rows = branchListModel({ items: [{ name: 'x' }, { checkpoints: [] }] });
  assert.deepEqual(rows.map((row) => row.name), ['x']);
  assert.equal(rows[0].mode, 'fork_after');
});
