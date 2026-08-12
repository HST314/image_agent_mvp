/* T2 状态页事件日志（契约 §4/Q2-A）：实时自动滚动刷新、当前正在执行的动作
 * 高亮、可暂停。数据源为 GET /api/projects/{id}/timeline/events——SSE 快照
 * 结束后按游标 after=seq 重连续传（契约 §4/§12「有限 SSE（游标重连）」）。
 * 本模块为纯逻辑核心：不触碰 document/fetch，流与调度全部由调用方注入，
 * 可在 Node 下直接回归。 */

/**
 * 当前正在执行的动作（高亮目标）：最新的「已开始但未闭环」步骤事件序号。
 * step_started / retry_started 记为开始；同 state 的 step_succeeded /
 * step_failed 记为闭环。返回未闭环开始事件里最大的 sequence；无则 null。
 */
export function currentActionSeq(events) {
  if (!Array.isArray(events)) return null;
  const started = new Map(); // state → 最新开始序号
  const closed = new Map(); // state → 最新闭环序号
  for (const event of events) {
    const seq = Number(event?.sequence);
    const stateId = String(event?.state || '');
    if (!Number.isFinite(seq) || !stateId) continue;
    if (event.type === 'step_started' || event.type === 'retry_started') started.set(stateId, seq);
    else if (event.type === 'step_succeeded' || event.type === 'step_failed') closed.set(stateId, seq);
  }
  let current = null;
  for (const [stateId, seq] of started) {
    if (seq > (closed.get(stateId) ?? -1) && (current === null || seq > current)) current = seq;
  }
  return current;
}

/**
 * 事件日志跟随器：反复打开 SSE 快照流，读到流结束后按已推进的游标重连。
 * - openStream(after, { signal }) → 异步可迭代的事件序列（由 api.js 接线）；
 * - 每轮收到的有效新事件（sequence 严格大于游标）汇总为一批推给 onBatch；
 * - 流断开/读取失败不致命：等待 intervalMs 后以最新游标重连续传；
 * - signal 中止或 stop() 后退出循环（done 兑现），暂停语义由调用方组合
 *   stop() + 以 cursor() 重新创建实现。
 */
export function createEventLogFollower({
  openStream, onBatch, signal, intervalMs = 1500, schedule = setTimeout, initialAfter = 0,
}) {
  let stopped = false;
  let after = initialAfter;
  const sleep = () => new Promise((resolve) => schedule(resolve, intervalMs));
  const done = (async () => {
    while (!stopped && !signal?.aborted) {
      const batch = [];
      try {
        for await (const event of openStream(after, { signal })) {
          if (stopped || signal?.aborted) return;
          const seq = Number(event?.sequence);
          if (Number.isFinite(seq) && seq > after) {
            after = seq;
            batch.push(event);
          }
        }
      } catch { /* 流不可用/网络抖动：下一拍按最新游标重连 */ }
      if (stopped || signal?.aborted) return;
      if (batch.length) onBatch?.(batch, after);
      await sleep();
    }
  })();
  return {
    stop() { stopped = true; },
    done,
    /** 当前游标（暂停后继续时作为 initialAfter 重新创建跟随器）。 */
    cursor: () => after,
  };
}
