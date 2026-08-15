/* T32：草稿保存/恢复/清除 + T33 资产 URL 协议转换。 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { saveDraft, loadDraft, clearDraft, intentIdempotencyKey, clearIntentIdempotencyKey } from '../../frontend/static/js/store.js';
import { assetUrl, assetIdOf } from '../../frontend/static/js/api.js';
import {
  annotationDraftKey, clearAnnotationDraft, loadAnnotationDraft, saveAnnotationDraft,
  workspaceStateKey, saveWorkspaceState, loadWorkspaceState,
} from '../../frontend/static/js/workspace_state.js';

function memoryStorage() {
  const map = new Map();
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    map,
  };
}

test('工作区 UI 状态按工程、分支和检查点隔离', () => {
  const storage = memoryStorage();
  const view = {
    project_id: 'p1',
    manifest: { current_branch: 'main', current_checkpoint: { checkpoint_id: 'checkpoint_1' } },
  };
  assert.equal(workspaceStateKey(view), 'studio-workspace:p1:main:checkpoint_1');
  saveWorkspaceState(view, { scrollY: 320, details: [true] }, storage);
  assert.deepEqual(loadWorkspaceState(view, storage), { scrollY: 320, details: [true] });
  assert.equal(loadWorkspaceState({ ...view, manifest: { ...view.manifest, current_checkpoint: { checkpoint_id: 'checkpoint_2' } } }, storage), null);
});

test('标注草稿按工程、分支、检查点和资产四层隔离并可清除', () => {
  const storage = memoryStorage();
  const scope = { projectId: 'p/1', branch: '修订 A', checkpointId: 'checkpoint_1', assetId: 'artifact_1' };
  assert.equal(
    annotationDraftKey(scope),
    'studio-annotation:p%2F1:%E4%BF%AE%E8%AE%A2%20A:checkpoint_1:artifact_1',
  );
  const draft = { marks: [{ kind: 'rectangle' }], tool: 'stroke', color: '#00ff00', width: 12 };
  saveAnnotationDraft(scope, draft, storage);
  assert.deepEqual(loadAnnotationDraft(scope, storage), draft);
  assert.equal(loadAnnotationDraft({ ...scope, assetId: 'artifact_2' }, storage), null);
  assert.equal(loadAnnotationDraft({ ...scope, checkpointId: 'checkpoint_2' }, storage), null);
  assert.equal(loadAnnotationDraft({ ...scope, branch: '修订 B' }, storage), null);
  assert.equal(loadAnnotationDraft({ ...scope, projectId: 'p2' }, storage), null);
  clearAnnotationDraft(scope, storage);
  assert.equal(loadAnnotationDraft(scope, storage), null);
});

test('草稿往返：保存后可恢复，清除后为空', () => {
  const storage = memoryStorage();
  saveDraft('p1', 'taskbook-markdown', '# 草稿', storage);
  const hit = loadDraft('p1', 'taskbook-markdown', storage);
  assert.equal(hit.value, '# 草稿');
  assert.ok(hit.savedAt);
  clearDraft('p1', 'taskbook-markdown', storage);
  assert.equal(loadDraft('p1', 'taskbook-markdown', storage), null);
});

test('草稿按工程与表单隔离', () => {
  const storage = memoryStorage();
  saveDraft('p1', 'a', '1', storage);
  saveDraft('p2', 'a', '2', storage);
  saveDraft('p1', 'b', '3', storage);
  assert.equal(loadDraft('p1', 'a', storage).value, '1');
  assert.equal(loadDraft('p2', 'a', storage).value, '2');
  assert.equal(loadDraft('p1', 'b', storage).value, '3');
});

test('损坏的草稿 JSON 安全降级为 null', () => {
  const storage = memoryStorage();
  storage.map.set('studio-draft:p1:a', '{broken');
  assert.equal(loadDraft('p1', 'a', storage), null);
});

test('幂等键：同一意图同一指纹复用同一键（重试去重）', () => {
  const storage = memoryStorage();
  const first = intentIdempotencyKey('p1', 'select', '3:{"selected_id":"a"}', storage);
  const retry = intentIdempotencyKey('p1', 'select', '3:{"selected_id":"a"}', storage);
  assert.equal(first, retry);
  assert.ok(first.startsWith('select-'));
  assert.ok(first.length <= 128);
});

test('幂等键：指纹变化（输入或检查点不同）时轮换新键', () => {
  const storage = memoryStorage();
  const base = intentIdempotencyKey('p1', 'manual', '3:{"manual_action":"execute"}', storage);
  const otherPayload = intentIdempotencyKey('p1', 'manual', '3:{"manual_action":"skip"}', storage);
  assert.notEqual(base, otherPayload);
  const nextCheckpoint = intentIdempotencyKey('p1', 'manual', '4:{"manual_action":"execute"}', storage);
  assert.notEqual(otherPayload, nextCheckpoint);
});

test('幂等键：按工程与意图隔离，清除后重新生成', () => {
  const storage = memoryStorage();
  const a = intentIdempotencyKey('p1', 'task', 'fp', storage);
  const b = intentIdempotencyKey('p2', 'task', 'fp', storage);
  const c = intentIdempotencyKey('p1', 'final', 'fp', storage);
  assert.notEqual(a, b);
  assert.notEqual(a, c);
  clearIntentIdempotencyKey('p1', 'task', storage);
  const regenerated = intentIdempotencyKey('p1', 'task', 'fp', storage);
  assert.notEqual(a, regenerated);
  // 其他工程/意图的键不受影响
  assert.equal(intentIdempotencyKey('p2', 'task', 'fp', storage), b);
  assert.equal(intentIdempotencyKey('p1', 'final', 'fp', storage), c);
});

test('artifact:// 转换为受控资产 API 路径', () => {
  const url = assetUrl('proj-1', { uri: 'artifact://artifact_abc123', artifact_id: 'artifact_abc123' });
  assert.equal(url, '/api/projects/proj-1/assets/artifact_abc123');
});

test('旧 http(s) 资产按历史原值展示；其他协议不展示', () => {
  assert.equal(assetUrl('p', { uri: 'https://cdn.example.com/x.png' }), 'https://cdn.example.com/x.png');
  assert.equal(assetUrl('p', { uri: 'file:///etc/passwd' }), null);
  assert.equal(assetUrl('p', {}), null);
  assert.equal(assetUrl('p', null), null);
});

test('assetIdOf 优先 artifact_id 字段', () => {
  assert.equal(assetIdOf({ artifact_id: 'artifact_x', uri: 'artifact://artifact_y' }), 'artifact_x');
  assert.equal(assetIdOf({ uri: 'artifact://artifact_y' }), 'artifact_y');
  assert.equal(assetIdOf({ uri: 'https://x/y.png' }), null);
});
