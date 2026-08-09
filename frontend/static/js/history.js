/* 时间线模块（T29 前端侧）：真实事件、增量分页展示；进度只来自真实事件，不用假计时。
 * 注：游标分页 API 属后端 T29；当前消费工程视图中的完整 history，前端做分页展示。 */

import { el } from './dom.js';
import { eventLabel } from './states.js';

const PAGE_SIZE = 20;

export function renderTimeline(container, events = [], { initial = 6, expandable = true } = {}) {
  const sorted = events.slice().reverse(); // 最新在前
  if (!sorted.length) {
    container.append(el('p', { text: '暂无活动记录。' }));
    return;
  }
  let shown = Math.min(initial, sorted.length);
  const list = el('div', { class: 'timeline' });
  const more = el('button', { type: 'button', class: 'btn btn--secondary timeline-more', text: '加载更多' });

  const draw = () => {
    list.textContent = '';
    for (const event of sorted.slice(0, shown)) {
      const item = el('div', { class: 'timeline__item' });
      item.append(el('strong', { text: eventLabel(event) }), el('time', { text: formatTime(event.timestamp) }));
      list.append(item);
    }
    more.style.display = shown >= sorted.length ? 'none' : '';
  };
  more.addEventListener('click', () => { shown = Math.min(shown + PAGE_SIZE, sorted.length); draw(); });
  container.append(list);
  if (expandable && sorted.length > initial) container.append(more);
  draw();
}

function formatTime(value) {
  if (!value) return '—';
  try {
    return new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(value));
  } catch { return String(value); }
}

/** 实时进度条：仅展示 job 真实事件流（queued/running/succeeded/failed）。 */
export function renderJobProgress(container, job, { onCancel } = {}) {
  const box = el('div', { class: 'job-progress', role: 'status' });
  const spinner = el('span', { class: 'spinner', 'aria-hidden': 'true' });
  const text = el('span', { text: describeJob(job) });
  box.append(spinner, text);
  if (job && (job.status === 'queued' || job.status === 'running')) {
    const cancel = el('button', { type: 'button', class: 'btn btn--secondary', text: '取消任务' });
    cancel.addEventListener('click', () => onCancel?.());
    box.append(cancel);
  }
  container.append(box);
  return {
    update(next) { text.textContent = describeJob(next); },
    done(record) {
      spinner.remove();
      text.textContent = describeJob(record);
    },
  };
}

function describeJob(job) {
  if (!job) return '正在提交任务…';
  const map = {
    queued: '任务已排队…', running: '后端正在处理，页面可继续操作…', cancelling: '正在取消…',
    succeeded: '任务完成。', failed: `任务失败：${job.error?.message || '未知错误'}`,
    cancelled: '任务已取消。', interrupted: '服务重启，任务中断且不会自动补调用。',
  };
  return map[job.status] || `任务状态：${job.status}`;
}
