import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createParentBridge, trustedParentOrigin } from '../../frontend/static/js/parentbridge.js';

class EventTargetFake {
  constructor() { this.listeners = new Set(); }
  addEventListener(name, listener) { if (name === 'message') this.listeners.add(listener); }
  removeEventListener(name, listener) { if (name === 'message') this.listeners.delete(listener); }
  emit(event) { for (const listener of this.listeners) listener(event); }
}

test('父页面来源只接受浏览器 referrer 的精确 http(s) origin', () => {
  assert.equal(trustedParentOrigin('https://control.example/tasks/t1'), 'https://control.example');
  assert.equal(trustedParentOrigin('javascript:alert(1)'), null);
  assert.equal(trustedParentOrigin('not a url'), null);
});

test('桥接校验来源、实例与一次性 nonce，并在每次响应后轮换', async () => {
  const sent = [];
  const parent = { postMessage: (message, origin) => sent.push({ message, origin }) };
  const target = new EventTargetFake();
  const bridge = createParentBridge({
    instanceId: 'instance_1',
    protocolVersion: '1.0',
    parentWindow: parent,
    eventTarget: target,
    referrer: 'https://control.example/tasks/t1',
    timeoutMs: 500,
  });
  assert.equal(bridge.supported, true);
  assert.equal(sent[0].message.type, 'bridge.hello');
  target.emit({
    source: parent,
    origin: 'https://attacker.example',
    data: { protocol: 'image-agent-runtime-settings', version: '1.0', type: 'bridge.init', instance_id: 'instance_1', nonce: 'wrong-origin-nonce-123' },
  });
  target.emit({
    source: parent,
    origin: 'https://control.example',
    data: { protocol: 'image-agent-runtime-settings', version: '1.0', type: 'bridge.init', instance_id: 'instance_1', nonce: 'nonce-first-request-123' },
  });
  const first = bridge.getSettings();
  await new Promise((resolve) => setTimeout(resolve, 0));
  const request = sent.at(-1).message;
  assert.equal(request.action, 'runtime_settings.get');
  assert.equal(request.nonce, 'nonce-first-request-123');
  target.emit({
    source: parent,
    origin: 'https://control.example',
    data: {
      protocol: 'image-agent-runtime-settings', version: '1.0', type: 'bridge.response',
      instance_id: 'instance_1', request_id: request.request_id, nonce: request.nonce,
      next_nonce: 'nonce-second-request-456', ok: true, payload: { revision: { current: 2 } },
    },
  });
  assert.equal((await first).revision.current, 2);
  const second = bridge.proposeSettings({ overrides: { watermark: true } });
  await new Promise((resolve) => setTimeout(resolve, 0));
  const nextRequest = sent.at(-1).message;
  assert.equal(nextRequest.nonce, 'nonce-second-request-456');
  target.emit({
    source: parent,
    origin: 'https://control.example',
    data: {
      protocol: 'image-agent-runtime-settings', version: '1.0', type: 'bridge.response',
      instance_id: 'instance_1', request_id: nextRequest.request_id, nonce: nextRequest.nonce,
      next_nonce: 'nonce-third-request-789', ok: true, payload: { proposal_id: 'proposal_1' },
    },
  });
  assert.equal((await second).proposal_id, 'proposal_1');
  bridge.dispose();
});
