/* Cross-origin runtime-settings bridge used only inside a Harness-owned iframe.
 * The child learns the exact parent origin from the browser-provided referrer,
 * never from a message payload. Every post-handshake request consumes one nonce. */

export const BRIDGE_PROTOCOL = 'image-agent-runtime-settings';
export const BRIDGE_VERSION = '1.0';
export const BRIDGE_ACTIONS = new Set([
  'runtime_settings.get',
  'runtime_settings.propose',
  'runtime_settings.confirm',
  'runtime_settings.sync_toggle',
  'delivery.complete',
  'delivery.status',
]);

export function trustedParentOrigin(referrer) {
  try {
    const url = new URL(referrer);
    return ['http:', 'https:'].includes(url.protocol) ? url.origin : null;
  } catch {
    return null;
  }
}

function randomId() {
  const value = globalThis.crypto?.randomUUID
    ? globalThis.crypto.randomUUID()
    : `${Date.now()}-${Math.random()}`;
  return String(value).replace(/[^A-Za-z0-9_-]/g, '').slice(0, 96);
}

function protocolMessage(value, instanceId) {
  return value && typeof value === 'object'
    && value.protocol === BRIDGE_PROTOCOL
    && value.version === BRIDGE_VERSION
    && value.instance_id === instanceId;
}

export function createParentBridge({
  instanceId,
  protocolVersion,
  parentWindow = globalThis.window?.parent,
  eventTarget = globalThis.window,
  referrer = globalThis.document?.referrer || '',
  timeoutMs = 30_000,
} = {}) {
  const parentOrigin = trustedParentOrigin(referrer);
  const supported = Boolean(
    parentOrigin
      && parentWindow
      && eventTarget
      && parentWindow !== eventTarget
      && instanceId
      && protocolVersion === BRIDGE_VERSION,
  );
  let disposed = false;
  let nonce = null;
  let handshakeWaiter = null;
  let serial = Promise.resolve();
  const pending = new Map();

  const bridgeError = (message, code) => {
    const error = new Error(message);
    error.code = code;
    return error;
  };

  const postHello = () => parentWindow.postMessage({
    protocol: BRIDGE_PROTOCOL,
    version: BRIDGE_VERSION,
    type: 'bridge.hello',
    instance_id: instanceId,
  }, parentOrigin);

  const beginHandshake = () => {
    if (!handshakeWaiter) {
      let resolve;
      const promise = new Promise((done) => { resolve = done; });
      handshakeWaiter = { promise, resolve };
    }
    nonce = null;
    postHello();
    return handshakeWaiter.promise;
  };

  const onMessage = (event) => {
    if (disposed || event.source !== parentWindow || event.origin !== parentOrigin) return;
    const message = event.data;
    if (!protocolMessage(message, instanceId)) return;
    if (message.type === 'bridge.init' && typeof message.nonce === 'string' && message.nonce.length >= 16) {
      nonce = message.nonce;
      handshakeWaiter?.resolve();
      handshakeWaiter = null;
      return;
    }
    if (message.type !== 'bridge.response' || typeof message.request_id !== 'string') return;
    const request = pending.get(message.request_id);
    if (!request || message.nonce !== request.nonce) return;
    pending.delete(message.request_id);
    if (typeof message.next_nonce !== 'string' || message.next_nonce.length < 16) {
      beginHandshake();
      request.reject(bridgeError('主系统返回了无效的桥接凭据，正在重新连接。', 'BRIDGE_INVALID_CREDENTIAL'));
      return;
    }
    nonce = message.next_nonce;
    if (message.ok) request.resolve(message.payload);
    else {
      const error = new Error(message.error?.message || '主系统未完成设置请求。');
      error.code = message.error?.code || 'BRIDGE_REQUEST_FAILED';
      request.reject(error);
    }
  };

  if (supported) {
    eventTarget.addEventListener('message', onMessage);
    beginHandshake();
  }

  async function waitForHandshake() {
    if (disposed) throw bridgeError('主系统设置连接已关闭。', 'BRIDGE_CLOSED');
    if (nonce) return;
    // Every wait re-announces the child. A previous hello can be lost while
    // the parent effect is being replaced, so retaining only its unresolved
    // promise would otherwise make later clicks wait on a dead session.
    const handshake = beginHandshake();
    let timer;
    try {
      await Promise.race([
        handshake,
        new Promise((_, reject) => {
          timer = setTimeout(
            () => reject(bridgeError('等待主系统设置连接超时。', 'BRIDGE_HANDSHAKE_TIMEOUT')),
            timeoutMs,
          );
        }),
      ]);
    } finally {
      clearTimeout(timer);
    }
    if (disposed || !nonce) throw bridgeError('主系统设置连接尚未恢复。', 'BRIDGE_DISCONNECTED');
  }

  async function transmit(action, payload) {
    await waitForHandshake();
    const requestId = `bridge_${randomId()}`;
    const requestNonce = nonce;
    nonce = null;
    const result = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(requestId);
        beginHandshake();
        reject(bridgeError('主系统设置请求超时，正在重新连接。', 'BRIDGE_REQUEST_TIMEOUT'));
      }, timeoutMs);
      pending.set(requestId, {
        nonce: requestNonce,
        resolve: (value) => { clearTimeout(timer); resolve(value); },
        reject: (error) => { clearTimeout(timer); reject(error); },
      });
    });
    parentWindow.postMessage({
      protocol: BRIDGE_PROTOCOL,
      version: BRIDGE_VERSION,
      type: 'bridge.request',
      instance_id: instanceId,
      request_id: requestId,
      nonce: requestNonce,
      action,
      payload,
    }, parentOrigin);
    return result;
  }

  async function send(action, payload = {}) {
    if (!BRIDGE_ACTIONS.has(action)) throw new Error('不支持的设置桥接操作。');
    if (!supported) throw new Error('当前页面未建立可信的主系统连接。');
    const run = async () => {
      try {
        return await transmit(action, payload);
      } catch (error) {
        if (action !== 'delivery.complete' || error?.code !== 'BRIDGE_REQUEST_TIMEOUT') throw error;
        await waitForHandshake();
        const status = await transmit('delivery.status', payload);
        if (status?.status === 'PUBLISHED') return status;
        return transmit(action, payload);
      }
    };
    serial = serial.then(run, run);
    return serial;
  }

  return {
    supported,
    parentOrigin,
    getSettings: () => send('runtime_settings.get'),
    proposeSettings: (payload) => send('runtime_settings.propose', payload),
    confirmSettings: (payload) => send('runtime_settings.confirm', payload),
    syncToggle: (payload) => send('runtime_settings.sync_toggle', payload),
    completeDelivery: (payload) => send('delivery.complete', payload),
    deliveryStatus: (payload) => send('delivery.status', payload),
    dispose() {
      disposed = true;
      nonce = null;
      eventTarget?.removeEventListener?.('message', onMessage);
      for (const request of pending.values()) request.reject(new Error('主系统设置连接已关闭。'));
      pending.clear();
    },
  };
}
