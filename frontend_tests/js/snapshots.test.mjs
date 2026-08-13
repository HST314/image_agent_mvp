import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  automaticBranchName,
  completedStageSnapshots,
  createSnapshotBranch,
  isCreatedBranchView,
  skillInvocationDomIds,
  skillInvocationView,
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

test('技能调用快照分别归一广告品类约束与五张风格参考', () => {
  const snapshot = {
    skill_invocations: {
      category_library: {
        category_name: '饮料海报', description: '用于新品传播',
        production_constraints: ['保留安全边距'], visual_rules: ['主体清晰'],
        forbidden_elements: ['虚构认证'], review_checks: ['检查品牌信息'],
      },
      style_library: {
        selections: Array.from({ length: 5 }, (_, index) => ({
          style_id: `STYLE-${index + 1}`, style_name: `风格 ${index + 1}`,
          reference_asset: { uri: `artifact://artifact_${String(index).padStart(24, '0')}` },
          artistic_interpretation: `艺术理解 ${index + 1}`,
        })),
      },
    },
  };
  const model = skillInvocationView(snapshot);
  assert.equal(model.category.name, '饮料海报');
  assert.deepEqual(model.category.productionConstraints, ['保留安全边距']);
  assert.equal(model.styles.length, 5);
  assert.equal(model.styles[0].interpretation, '艺术理解 1');
  assert.equal(model.hasPersistedStyleDetails, true);
});

test('旧快照只回退为风格文字，不把五张候选主图当作风格参考图', () => {
  const model = skillInvocationView({
    candidates: [{ style_id: 'STYLE-1', style_name: '旧风格名', uri: 'artifact://artifact_candidate' }],
    style_selections: [{ style_id: 'STYLE-1', reason: '适配任务' }],
  });
  assert.equal(model.category.available, false);
  assert.equal(model.styles[0].styleName, '旧风格名');
  assert.equal(model.styles[0].interpretation, '适配任务');
  assert.equal(model.styles[0].reference_asset, undefined);
  assert.equal(model.hasPersistedStyleDetails, false);
});

test('当前技能结果与历史版本生成唯一 DOM id，aria-labelledby 可一一对应', () => {
  const current = skillInvocationDomIds({ skill_invocation_current: { version_id: 'skill-invocation-v2' } });
  const duplicateInHistory = skillInvocationDomIds({ version_id: 'skill-invocation-v2' });
  const oldVersion = skillInvocationDomIds({ version_id: 'skill invocation/v1' });
  assert.equal(new Set([
    current.category, current.style,
    duplicateInHistory.category, duplicateInHistory.style,
    oldVersion.category, oldVersion.style,
  ]).size, 6);
  assert.match(oldVersion.category, /^category-skill-title-skill-invocation-v1-\d+$/);
});
