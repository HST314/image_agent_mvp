/* T32：草稿保存/恢复/清除 + T33 资产 URL 协议转换。 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { saveDraft, loadDraft, clearDraft } from '../../frontend/static/js/store.js';
import { assetUrl, assetIdOf } from '../../frontend/static/js/api.js';

function memoryStorage() {
  const map = new Map();
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    map,
  };
}

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
