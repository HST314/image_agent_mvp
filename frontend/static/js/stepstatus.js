/* T10：job 运行期间的实时状态文案（契约 §4/§7）——只来自后端真实 timeline
 * 事件（step_started），不做前端假状态。本模块不触碰 document/fetch，
 * 网络与调度全部由调用方注入，可在 Node 下直接回归。 */

import { STATE_LABELS } from './states.js';

/* 各工作流状态「正在进行」的中文文案（step_started.state → 展示文本）。
 * 键与后端 WorkflowRunner.ORDER 的七个生产状态一一对应。 */
const LIVE_STEP_TEXT = {
  intake_clarify: '正在理解任务书，生成澄清问题…',
  confirmation_build: '正在生成任务书…',
  initial_candidate_generation: '正在调用品类与艺术风格技能并生成候选图…',
  master_candidate_selection: '正在整理候选结果…',
  self_check_iteration: '正在质检画面…',
  human_prompt_iteration: '正在按修改意见调整画面…',
  final_approval: '正在完成最终确认…',
};

/**
 * 从一批 timeline 事件提取最新的「正在进行」文案。
 * 只认 step_started 事件；未知状态兜底为「正在{状态中文名}…」，仍未知返回 null
 * （调用方保留上一条文案，不把英文 id 上屏——契约 §11）。
 */
export function liveStepText(events) {
  if (!Array.isArray(events)) return null;
  let text = null;
  for (const event of events) {
    if (event?.type !== 'step_started') continue;
    const stateId = String(event.state || '');
    if (LIVE_STEP_TEXT[stateId]) text = LIVE_STEP_TEXT[stateId];
    else if (STATE_LABELS[stateId]) text = `正在${STATE_LABELS[stateId]}…`;
  }
  return text;
}

/** 这批事件里最大的 sequence（游标推进用；无有效序号时返回原值）。 */
export function maxSequence(events, after = 0) {
  if (!Array.isArray(events)) return after;
  let cursor = after;
  for (const event of events) {
    const seq = Number(event?.sequence);
    if (Number.isFinite(seq) && seq > cursor) cursor = seq;
  }
  return cursor;
}

/**
 * 跟随工程 timeline，把最新的真实步骤文案推给 onText。
 * 注入依赖：fetchPage(after, {signal}) → {items}；signal 中止或 stop() 后退出；
 * 单次拉取失败不致命，等待下一拍重试（网络抖动不产生假状态，只是暂停更新）。
 * 返回 { stop, done }：done 在循环真正退出后兑现，便于测试编排。
 */
export function createTimelineFollower({
  fetchPage, onText, signal, intervalMs = 2000, schedule = setTimeout, initialAfter = 0,
}) {
  let stopped = false;
  let after = initialAfter;
  const sleep = () => new Promise((resolve) => schedule(resolve, intervalMs));
  const done = (async () => {
    while (!stopped && !signal?.aborted) {
      let page = null;
      try { page = await fetchPage(after, { signal }); } catch { page = null; }
      if (stopped || signal?.aborted) return;
      const items = page?.items;
      if (Array.isArray(items) && items.length) {
        after = maxSequence(items, after);
        const text = liveStepText(items);
        if (text) onText?.(text);
      }
      await sleep();
    }
  })();
  return { stop() { stopped = true; }, done };
}
