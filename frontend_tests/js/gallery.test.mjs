/* T33 验收核心：五个稳定槽位、部分失败补偿时成功槽身份不变（不闪烁/不替换）。 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildSlots, SLOT_COUNT } from '../../frontend/static/js/gallery.js';

const candidate = (index, sha = `sha-${index}`) => ({
  id: `candidate-${index + 1}`, candidate_index: index, sha256: sha,
  uri: `artifact://artifact_${sha}`, style_id: `style-${index}`, style_name: `风格${index}`,
});

test('五个候选完整时全部 ready，槽位顺序稳定', () => {
  const slots = buildSlots([0, 1, 2, 3, 4].map((i) => candidate(i)), []);
  assert.equal(slots.length, SLOT_COUNT);
  assert.ok(slots.every((s) => s.status === 'ready'));
  assert.deepEqual(slots.map((s) => s.index), [0, 1, 2, 3, 4]);
});

test('乱序候选仍落入正确槽位', () => {
  const slots = buildSlots([candidate(3), candidate(0), candidate(4)], []);
  assert.equal(slots[0].asset.id, 'candidate-1');
  assert.equal(slots[3].asset.id, 'candidate-4');
  assert.equal(slots[4].asset.id, 'candidate-5');
  assert.equal(slots[1].status, 'missing');
  assert.equal(slots[2].status, 'missing');
});

test('部分失败只留缺失槽位；补偿后成功槽 key 不变（不替换）', () => {
  const before = buildSlots([candidate(0), candidate(1), candidate(3)], []);
  const beforeKeys = before.map((s) => s.key);
  // 补偿返回：0/1/3 保持原资产（幂等），新增 2/4
  const after = buildSlots([candidate(0), candidate(1), candidate(2), candidate(3), candidate(4)], []);
  assert.equal(after[0].key, beforeKeys[0]);
  assert.equal(after[1].key, beforeKeys[1]);
  assert.equal(after[3].key, beforeKeys[3]);
  assert.equal(after[2].status, 'ready');
  assert.equal(after[4].status, 'ready');
});

test('兼容无 candidate_index 的旧检查点：按 candidate-N 推断槽位', () => {
  const legacy = { id: 'candidate-2', sha256: 'x', uri: 'artifact://artifact_x' };
  const slots = buildSlots([legacy], []);
  assert.equal(slots[1].status, 'ready');
  assert.equal(slots[1].asset.id, 'candidate-2');
});

test('风格选择信息按 style_id 关联到槽位', () => {
  const styles = [{ style_id: 'style-0', mechanism: '非对称网格', reason: '适配叙事', risk: '信息密度' }];
  const slots = buildSlots([candidate(0)], styles);
  assert.equal(slots[0].style.mechanism, '非对称网格');
  assert.equal(slots[0].styleName, '风格0');
});

test('空候选 → 五个 missing 槽位', () => {
  const slots = buildSlots([], []);
  assert.equal(slots.length, 5);
  assert.ok(slots.every((s) => s.status === 'missing'));
});
