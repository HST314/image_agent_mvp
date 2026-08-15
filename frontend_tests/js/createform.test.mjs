import { test } from 'node:test';
import assert from 'node:assert/strict';

import { buildNewProjectTask } from '../../frontend/static/js/createform.js';

test('T11 中文新建表单在提交边界组装任务卡，不依赖原始 JSON 编辑器', () => {
  const task = buildNewProjectTask({
    projectId: 'campaign-01',
    goal: '  广告 海报  ',
    usageScene: '内部审核',
    targetGroup: '审核人员',
    styleTone: '清晰、精致',
  });

  assert.equal(task.project_id, 'campaign-01');
  assert.equal(task.deliverable_goal, '广告 海报');
  assert.equal(task.category_ref, undefined, '未显式选择品类时必须由广告品类库匹配');
  assert.deepEqual(task.known_facts, { audience: '审核人员', tone: '清晰、精致' });
  assert.deepEqual(task.unknowns, { output_spec: '待确认' });
  assert.equal(task.source_refs[0].excerpt, '广告 海报');
});

test('T11 已填写交付规格时作为已知事实提交，不再制造澄清项', () => {
  const task = buildNewProjectTask({
    projectId: 'campaign-02',
    goal: '产品主视觉',
    usageScene: '电商详情页',
    deliverySpec: '正方形图片',
  });

  assert.equal(task.known_facts.output_spec, '正方形图片');
  assert.deepEqual(task.unknowns, {});
});

test('T11 必填自然语言信息不可仅为空白', () => {
  assert.throws(
    () => buildNewProjectTask({ projectId: 'campaign-03', goal: ' ', usageScene: '内部审核' }),
    /不能为空/,
  );
});
