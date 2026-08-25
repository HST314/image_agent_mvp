/* T2 状态页（契约 §4，Q2-A）：集中呈现 Agent 运行状态、工程信息、原始任务、
 * 最近活动，以及事件日志——实时自动滚动刷新、当前正在执行的动作高亮、可暂停/
 * 继续。数据源：工程视图（工作区最近加载的 state.current）+ GET /timeline/events
 * SSE 快照流（游标 after=seq 重连续传，契约 §4/§12，不引入 WebSocket）。
 * 日志跟随与「当前动作」推导的纯逻辑在 eventlog.js（Node 可回归），实时状态
 * 文案推导在 stepstatus.js（T10 同一事实来源）；本模块只做 DOM 接线。
 * renderStatusPage 返回 { dispose }：切页/离开时由 app.js 调用，停止跟随并
 * 中止在途流（仅停前端订阅，不影响后台 job 本身）。 */

import { el, toast, stateBlock, sectionPanel } from './dom.js';
import { renderTimeline } from './history.js';
import { deriveView, eventLabel, stateLabel } from './states.js';
import { phaseLabel, capabilityLabel, jobStatusLabel, fieldLabel } from './copy.js';
import { liveStepText } from './stepstatus.js';
import { createEventLogFollower, currentActionSeq } from './eventlog.js';
import { getActor, setActor } from './store.js';

/* 日志初始展示的最大条数（长历史工程只取尾部；游标仍按全量推进，不丢新事件）。 */
const INITIAL_LOG_ITEMS = 100;
/* 日志窗口上限：运行期最多保留的条目数（超出从顶部裁减）。 */
const MAX_LOG_ITEMS = 500;

/**
 * 渲染状态页。view 为空（未打开工程）时渲染空态并返回 null。
 * deps.openStream(after, { signal }) → 异步可迭代事件序列（app.js 注入
 * api.streamTimelineEvents 绑定工程后的形态）。
 */
export function renderStatusPage(container, view, {
  openStream,
  loadRuntimeStatus,
  loadManagedSettings,
  onManagedSettings,
  health,
} = {}) {
  if (!view?.project_id) {
    container.append(stateBlock('empty', '尚未打开工程',
      '从左侧目录打开一个工程后，这里会集中显示它的运行状态、工程信息、原始任务、最近活动与实时事件日志。'));
    return null;
  }
  const snapshot = view.snapshot || {};
  const manifest = view.manifest || {};
  const derived = deriveView(view);
  let disposed = false;
  let follower = null;
  let controller = null;
  let cursor = 0;
  let paused = false;
  const logNodes = new Map(); // sequence → 日志条目节点（当前动作高亮切换用）
  const logEvents = []; // 已上屏事件（与 logNodes 同序，供当前动作推导）
  let currentSeq = null;

  /* ===== Agent 运行状态 ===== */
  const statusPanel = sectionPanel('Agent 运行状态', '实时反映后端真实状态（来自事件流与后台任务记录）');
  statusPanel.append(agentStatusRow(view, derived));
  const liveLine = el('p', { class: 'agent-status__live', hidden: true });
  const facts = el('dl', { class: 'kv', style: 'margin-top:12px' });
  const activeJob = view.active_job;
  facts.append(
    el('dt', { text: '后台任务' }),
    el('dd', { text: activeJob ? `${activeJob.operation || '后端任务'} · ${jobStatusLabel(activeJob.status)}` : '无进行中的后台任务' }),
    el('dt', { text: '当前检查点' }),
    el('dd', { text: `分支 ${manifest.current_branch || 'main'} · 检查点 ${manifest.current_checkpoint?.sequence || 0}` }),
  );
  statusPanel.append(facts, liveLine);

  /* ===== 服务与配置状态：只读取结构化投影，不解析日志文本 ===== */
  const runtimePanel = sectionPanel('服务与配置', '显示进程健康、活动修订、待应用设置与结构化异常。');
  const runtimeFacts = el('dl', { class: 'kv' });
  runtimeFacts.append(
    el('dt', { text: '服务健康' }),
    el('dd', { text: health?.status === 'ok' ? '正常' : '降级或暂不可用' }),
    el('dt', { text: '当前配置修订' }),
    el('dd', { text: '正在读取…' }),
    el('dt', { text: '待应用设置' }),
    el('dd', { text: '正在读取…' }),
    el('dt', { text: '最近异常' }),
    el('dd', { text: '正在读取…' }),
  );
  runtimePanel.append(runtimeFacts);

  /* ===== 事件日志（Q2-A：实时滚动 / 当前动作高亮 / 可暂停） ===== */
  const logPanel = sectionPanel('事件日志', '实时自动滚动刷新；当前正在执行的动作高亮');
  const pauseBtn = el('button', { type: 'button', class: 'btn btn--secondary', text: '暂停', 'aria-pressed': 'false' });
  logPanel.querySelector('.section__head').append(pauseBtn);
  const logBox = el('div', { class: 'eventlog', role: 'log', 'aria-label': '事件日志' });
  logPanel.append(logBox);

  /* ===== 工程信息 + 原始任务（自工作台迁入，契约 §3/§4） ===== */
  const side = el('div', { class: 'status-side' });
  side.append(renderInfoPanel(view, derived));
  const taskCard = snapshot.task_card;
  if (taskCard) {
    const panel = sectionPanel('原始任务', '上游输入原样留存；澄清答案与任务书修订均有审计');
    const list = el('dl', { class: 'kv' });
    list.append(
      el('dt', { text: '交付目标' }), el('dd', { text: taskCard.deliverable_goal || '—' }),
      el('dt', { text: '使用场景' }), el('dd', { text: taskCard.usage_context || '—' }),
      el('dt', { text: '已知事实' }), el('dd', { text: summarizeFacts(taskCard.known_facts) }),
      el('dt', { text: '未知项' }), el('dd', { text: summarizeFacts(taskCard.unknowns) }),
    );
    panel.append(list);
    side.append(panel);
  }

  const grid = el('div', { class: 'status-grid' });
  grid.append(logPanel, side);

  /* ===== 最近活动（自工作台迁入） ===== */
  const activityPanel = sectionPanel('最近活动', '真实事件审计记录');
  renderTimeline(activityPanel, view.history || []);

  container.append(statusPanel, runtimePanel, grid, activityPanel);

  const runtimeController = new AbortController();
  if (loadRuntimeStatus) {
    void loadRuntimeStatus({ signal: runtimeController.signal }).then((runtime) => {
      if (disposed) return;
      const values = runtimeFacts.querySelectorAll('dd');
      const config = runtime.configuration || {};
      values[0].textContent = runtime.process_health === 'ok' ? '正常' : '降级或暂不可用';
      values[1].textContent = config.revision_id
        ? `${config.revision_id} · 分支 ${config.branch_id || 'main'} · 校验 ${String(config.config_hash || '').slice(0, 12)}…`
        : '未绑定配置修订';
      values[2].textContent = config.pending_revision_id || '无';
      values[3].textContent = runtime.recent_exceptions?.length
        ? `${runtime.recent_exceptions.length} 项需要处理`
        : '无';
    }).catch(() => {
      if (!disposed) runtimeFacts.querySelectorAll('dd')[0].textContent = '状态投影暂不可用';
    });
  }
  if (loadManagedSettings) {
    void loadManagedSettings().then((settings) => {
      if (disposed) return;
      const pending = settings.pending_application;
      onManagedSettings?.(settings);
      runtimeFacts.querySelectorAll('dd')[2].textContent = pending
        ? `${pending.revision_id || settings.revision?.pending_revision_id || '待应用修订'} · ${pending.status === 'WAITING_SAFE_POINT' ? '等待安全检查点' : '正在应用'}`
        : settings.revision?.pending_revision_id || '无';
    }).catch(() => {
      if (!disposed) runtimeFacts.querySelectorAll('dd')[2].textContent = '主系统状态暂不可用';
    });
  }

  /* ---- 事件日志数据与跟随 ---- */
  const initial = (Array.isArray(view.history) ? view.history : []).filter((e) => Number.isFinite(Number(e?.sequence)));
  for (const event of initial.slice(-INITIAL_LOG_ITEMS)) appendLogItem(event);
  cursor = initial.reduce((max, e) => Math.max(max, Number(e.sequence)), 0);
  if (!logNodes.size) logBox.append(el('p', { class: 'eventlog__empty', text: '暂无事件。' }));
  refreshHighlight();
  scrollToBottom();
  startFollower();

  pauseBtn.addEventListener('click', () => {
    if (disposed) return;
    if (paused) {
      paused = false;
      pauseBtn.textContent = '暂停';
      pauseBtn.setAttribute('aria-pressed', 'false');
      startFollower(); // 从已推进游标恢复：暂停期间的事件会按序补齐
    } else {
      paused = true;
      pauseBtn.textContent = '继续';
      pauseBtn.setAttribute('aria-pressed', 'true');
      stopFollower();
    }
  });

  function startFollower() {
    if (!openStream || disposed || paused) return;
    controller = new AbortController();
    follower = createEventLogFollower({
      openStream,
      signal: controller.signal,
      initialAfter: cursor,
      onBatch(batch, nextCursor) {
        if (disposed || paused) return;
        cursor = nextCursor;
        logBox.querySelector('.eventlog__empty')?.remove();
        for (const event of batch) appendLogItem(event);
        refreshHighlight();
        // 新建工程等场景的实时状态文案（「正在理解任务书…」）也在状态区呈现（§4）。
        const text = liveStepText(batch);
        if (text) {
          liveLine.hidden = false;
          liveLine.textContent = text;
        }
        scrollToBottom();
      },
    });
  }

  function stopFollower() {
    follower?.stop();
    follower = null;
    controller?.abort();
    controller = null;
  }

  function appendLogItem(event) {
    const seq = Number(event.sequence);
    if (logNodes.has(seq)) return;
    const item = el('div', { class: 'eventlog__item', dataset: { seq: String(seq) } }, [
      el('span', { class: 'eventlog__seq', text: `#${seq}` }),
      el('span', { class: 'eventlog__label', text: eventLabel(event) }),
      el('time', { text: formatTime(event.timestamp) }),
    ]);
    logNodes.set(seq, item);
    logEvents.push(event);
    logBox.append(item);
    /* 日志窗口上限：超长时从顶部裁减（跟随态本就在底部，视觉无感）。 */
    while (logEvents.length > MAX_LOG_ITEMS) {
      const oldest = logEvents.shift();
      logNodes.get(Number(oldest.sequence))?.remove();
      logNodes.delete(Number(oldest.sequence));
    }
  }

  /* 当前正在执行的动作高亮：最新的「已开始但未闭环」步骤（逻辑见 eventlog.js）。 */
  function refreshHighlight() {
    const next = currentActionSeq(logEvents);
    if (next === currentSeq) return;
    if (currentSeq !== null) logNodes.get(currentSeq)?.classList.remove('is-current');
    currentSeq = next;
    if (currentSeq !== null) logNodes.get(currentSeq)?.classList.add('is-current');
  }

  function scrollToBottom() { logBox.scrollTop = logBox.scrollHeight; }

  return {
    dispose() {
      disposed = true;
      runtimeController.abort();
      stopFollower();
    },
  };
}

/* Agent 运行状态徽标：进行中 / 等待人工处理 / 已暂停（失败）/ 已完成 / 已终止 / 待启动。 */
function agentStatusRow(view, derived) {
  const activeJob = view.active_job;
  let cls = 'badge badge--info';
  let text = '待启动';
  if (activeJob && ['queued', 'running', 'cancelling'].includes(activeJob.status)) { cls = 'badge badge--warning'; text = '运行中'; }
  else if (derived.stage === 'failed') { cls = 'badge badge--danger'; text = '已暂停（处理失败）'; }
  else if (derived.stage === 'completed') { cls = 'badge badge--success'; text = '已完成'; }
  else if (derived.stage === 'terminated') { cls = 'badge badge--danger'; text = '已终止且不交付'; }
  else if (derived.waiting) { text = '等待人工处理'; }
  else if (derived.stage !== 'empty') { text = '可继续推进'; }
  return el('div', { class: 'agent-status' }, [
    el('span', { class: cls, role: 'status', text }),
    el('span', { class: 'agent-status__state', text: `${stateLabel(view.snapshot?.state)} · ${phaseLabel(view.snapshot?.phase)}` }),
  ]);
}

/* 工程信息卡片（含操作人身份；自工作台迁入，契约 §3→§4）。 */
function renderInfoPanel(view, derived) {
  const snapshot = view.snapshot || {};
  const manifest = view.manifest || {};
  const panel = sectionPanel('工程信息', '');
  panel.querySelector('.section__head')?.remove();
  const list = el('dl', { class: 'kv' });
  list.append(
    el('dt', { text: '当前状态' }), el('dd', { text: stateLabel(snapshot.state) }),
    el('dt', { text: '当前阶段' }), el('dd', { text: phaseLabel(snapshot.phase) }),
    el('dt', { text: '当前分支' }), el('dd', { text: manifest.current_branch || 'main' }),
    el('dt', { text: '更新时间' }), el('dd', { text: formatDateTime(manifest.updated_at) }),
    el('dt', { text: '可用动作' }), el('dd', { text: (view.capabilities || []).map(capabilityLabel).join('、') || '无' }),
  );
  panel.append(list);
  const actorField = el('div', { class: 'field', style: 'margin-top:12px' });
  const actorInput = el('input', { class: 'input', id: 'actor-input', placeholder: '确认/审批时使用的身份', value: getActor() });
  actorInput.addEventListener('change', () => {
    setActor(actorInput.value.trim());
    toast('操作人身份已保存。');
  });
  actorField.append(el('label', { for: 'actor-input', text: '操作人身份' }), actorInput);
  panel.append(actorField);
  return panel;
}

/* 任务卡事实展示：键名一律中文化（契约 §11），嵌套对象/数组的值拍平为可读文本。 */
function formatFactValue(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (Array.isArray(value)) return value.map(formatFactValue).join('、');
  if (typeof value === 'object') {
    return Object.entries(value).map(([k, v]) => `${fieldLabel(k)} ${formatFactValue(v)}`).join('，');
  }
  return String(value);
}

function summarizeFacts(facts) {
  if (!facts || typeof facts !== 'object') return '—';
  const entries = Object.entries(facts);
  if (!entries.length) return '—';
  return entries.map(([k, v]) => `${fieldLabel(k)}：${formatFactValue(v)}`).join('；');
}

function formatTime(value) {
  if (!value) return '—';
  try {
    return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(value));
  } catch { return String(value); }
}

function formatDateTime(value) {
  if (!value) return '—';
  try {
    return new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(value));
  } catch { return String(value); }
}
