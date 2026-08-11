/* API client（T35 独立模块）：统一错误格式、120s 超时、资产 URL 协议转换、
 * 后台 job 提交/轮询/SSE 序号续传（对齐 T23 契约）。
 * T11：所有 HTTP 错误文案在此统一中文化（契约 §11），英文错误码/字段名不上屏。 */

import { errorText, validationText, fieldLabel, hasCJK } from './copy.js';

const TERMINAL_JOB_STATUS = new Set(['succeeded', 'failed', 'cancelled', 'interrupted']);
const TERMINAL_EVENTS = new Set(['succeeded', 'failed', 'cancelled']);

export class ApiError extends Error {}

/* 422 校验错误的字段路径（body.policy.self_check…）→ 中文路径；含未收录英文段时整体兜底。 */
function locLabel(loc) {
  const segments = (loc || []).slice(1);
  if (!segments.length) return '输入';
  const labels = segments.map((seg) => {
    const text = String(seg);
    if (/^\d+$/.test(text)) return null; // 数组下标不参与展示
    return hasCJK(text) ? text : fieldLabel(text, null);
  }).filter(Boolean);
  return labels.length ? labels.join(' · ') : '输入';
}

export function formatError(detail) {
  if (typeof detail === 'string') return errorText(detail);
  if (Array.isArray(detail)) return detail.map((x) => `${locLabel(x.loc)}：${validationText(x.msg)}`).join('；');
  if (detail && typeof detail === 'object') return errorText(detail.message || detail.code || '');
  return '请求未完成，请检查输入后重试。';
}

export async function api(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeoutMs || 120000);
  // 外部 signal（如视图操作 controller）与 120s 超时组合：任一触发都中止请求，
  // 避免传入 signal 后丢失超时保护（M1 的 120s 超时重试路径依赖该保护）。
  const external = options.signal;
  const onExternalAbort = () => controller.abort();
  if (external) {
    if (external.aborted) controller.abort();
    else external.addEventListener('abort', onExternalAbort, { once: true });
  }
  try {
    const res = await fetch(path, {
      ...options,
      signal: controller.signal,
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    });
    let data;
    try { data = await res.json(); } catch { data = { detail: '服务返回了无法解析的响应。' }; }
    if (!res.ok) throw new ApiError(formatError(data.detail));
    return data;
  } catch (error) {
    if (error.name === 'AbortError') {
      if (external?.aborted) throw new ApiError('操作已随视图切换取消。');
      throw new ApiError('请求超时。后端可能仍在处理，请稍后刷新工程状态。');
    }
    if (error instanceof TypeError) throw new ApiError('无法连接服务。请检查 FastAPI 是否运行，然后重试。');
    throw error;
  } finally {
    clearTimeout(timer);
    external?.removeEventListener('abort', onExternalAbort);
  }
}

/** artifact://artifact_x → 受控资产 API；http(s) 旧资产按历史原值展示（T06）。 */
export function assetUrl(projectId, asset) {
  if (!asset) return null;
  const uri = String(asset.uri || '');
  if (uri.startsWith('artifact://')) {
    return `/api/projects/${encodeURIComponent(projectId)}/assets/${encodeURIComponent(uri.slice('artifact://'.length))}`;
  }
  if (/^https?:\/\//.test(uri)) return uri;
  return null;
}

export function assetIdOf(asset) {
  if (!asset) return null;
  if (asset.artifact_id) return asset.artifact_id;
  const uri = String(asset.uri || '');
  return uri.startsWith('artifact://') ? uri.slice('artifact://'.length) : null;
}

/* ---- 工程 ---- */
export const listProjects = () => api('/api/projects');
export const health = () => api('/api/health');
export const getProject = (id, { signal } = {}) => api(`/api/projects/${encodeURIComponent(id)}`, { signal });
export const createProject = (payload, { signal } = {}) =>
  api('/api/projects', { method: 'POST', body: JSON.stringify(payload), signal });
/* T10：时间线增量拉取（契约 §7——实时状态只来自后端真实事件，不做前端假状态）。 */
export const getTimeline = (id, { after = 0, limit = 100, signal } = {}) =>
  api(`/api/projects/${encodeURIComponent(id)}/timeline?after=${after}&limit=${limit}`, { signal });

/* ---- 推进（同步，仅用于不触发付费模型调用的动作） ---- */
export const advance = (id, payload) => api(`/api/projects/${encodeURIComponent(id)}/advance`, { method: 'POST', body: JSON.stringify(payload) });
export const retryProject = (id, payload) => api(`/api/projects/${encodeURIComponent(id)}/retry`, { method: 'POST', body: JSON.stringify(payload) });

/* ---- 后台 job（T23：会触发付费/长耗时调用的推进一律走 job） ---- */
export async function startAdvanceJob(id, payload, { signal } = {}) {
  const res = await api(`/api/projects/${encodeURIComponent(id)}/jobs`, { method: 'POST', body: JSON.stringify(payload), signal });
  return res;
}
export const getJob = (jobId) => api(`/api/jobs/${encodeURIComponent(jobId)}`);
export const cancelJob = (jobId) => api(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST', body: '{}' });

/**
 * 消费 job 事件：优先 fetch 读取 SSE 快照并按 seq 续传；流不可用或断线时降级为
 * 轮询 GET /api/jobs/{id}。onEvent(event) 按序触发；终态时 onDone(record) 恰好一次。
 */
export async function trackJob(jobId, { onEvent, onDone, signal, pollMs = 1500 } = {}) {
  let lastSeq = 0;
  let done = false;
  const seen = (event) => {
    if (typeof event.seq === 'number' && event.seq > lastSeq) {
      lastSeq = event.seq;
      onEvent?.(event);
    }
  };
  const finish = (record) => {
    if (done) return;
    done = true;
    onDone?.(record);
  };
  const emitFromRecord = (record) => {
    for (const event of record.events || []) seen(event);
    if (TERMINAL_JOB_STATUS.has(record.status)) { finish(record); return true; }
    return false;
  };

  while (!done) {
    if (signal?.aborted) return;
    try {
      const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/events?after=${lastSeq}`, {
        signal, headers: { Accept: 'text/event-stream' },
      });
      if (!res.ok || !res.body) throw new Error('SSE_UNAVAILABLE');
      const text = await res.text();
      for (const block of text.split('\n\n')) {
        const dataLine = block.split('\n').find((line) => line.startsWith('data: '));
        if (!dataLine) continue;
        let event;
        try { event = JSON.parse(dataLine.slice(6)); } catch { continue; }
        seen(event);
        if (TERMINAL_EVENTS.has(event.type)) { finish(await getJob(jobId)); return; }
      }
      // 有限快照读完仍未终结：轮询补齐（服务可能正在执行长调用）。
      const record = await getJob(jobId);
      if (!emitFromRecord(record)) await new Promise((resolve) => setTimeout(resolve, pollMs));
    } catch (error) {
      if (signal?.aborted) return;
      // SSE 不可用/网络抖动：轮询兜底。
      try {
        const record = await getJob(jobId);
        if (!emitFromRecord(record)) await new Promise((resolve) => setTimeout(resolve, pollMs));
      } catch (pollError) {
        finish({ job_id: jobId, status: 'unknown', error: { message: pollError.message } });
      }
    }
  }
}

/* ---- 圈画微调（T26 后端管线） ---- */
export const submitAnnotation = (id, payload) =>
  api(`/api/projects/${encodeURIComponent(id)}/annotations`, { method: 'POST', body: JSON.stringify(payload) });

/* ---- 质检分流 / 交付 / 策略 / 未知调用 ---- */
export const qualityDisposition = (id, payload) =>
  api(`/api/projects/${encodeURIComponent(id)}/quality-disposition`, { method: 'POST', body: JSON.stringify(payload) });
export const retryDeliveryNote = (id) =>
  api(`/api/projects/${encodeURIComponent(id)}/delivery/retry`, { method: 'POST', body: '{}' });
export const revisePolicy = (id, payload) =>
  api(`/api/projects/${encodeURIComponent(id)}/policy`, { method: 'POST', body: JSON.stringify(payload) });
export const resolveUnknown = (id, key, payload) =>
  api(`/api/projects/${encodeURIComponent(id)}/unknown-actions/${encodeURIComponent(key)}`, { method: 'POST', body: JSON.stringify(payload) });
export const branchFrom = (id, payload) =>
  api(`/api/projects/${encodeURIComponent(id)}/branches`, { method: 'POST', body: JSON.stringify(payload) });
