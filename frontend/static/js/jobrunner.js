/* 后台 job 运行器 + 导航（纯逻辑核心）：操作世代、跟踪调度、幂等键终态策略
 * 与视图导航世代守卫。DOM 与网络全部由调用方注入（project.js/app.js 负责
 * 接线），本模块不触碰 document/fetch，可在 Node 下直接做异步交错回归
 * 测试（H1/M1）。 */

import { state, intentIdempotencyKey, clearIntentIdempotencyKey } from './store.js';

/* ---------- 视图/操作世代注册表 ----------
 * 工程视图每次渲染、每次提交新操作都 begin() 一个新操作并中止旧操作的
 * controller；离开工程视图（回首页等）leave()。异步回调（延时刷新、POST
 * 返回、跟踪事件）只有在所属操作仍是当前操作时才允许改写界面，避免过期
 * 回调中止新 job 或覆盖用户切换后的视图（H1 竞态修复）。 */
export function createOperationRegistry() {
  let active = null;
  return {
    /** 新操作/新渲染接管：中止旧操作，返回本次操作句柄。 */
    begin() {
      active?.controller.abort();
      active = { controller: new AbortController() };
      return active;
    },
    /** 离开工程视图：中止当前操作（含 in-flight POST）并清空登记。 */
    leave() {
      active?.controller.abort();
      active = null;
    },
    /** 该操作仍是当前操作且未被中止？（异步回调的世代守卫） */
    isCurrent(op) {
      return !!op && op === active && !op.controller.signal.aborted;
    },
  };
}

/* 应用级单例：整个前端同一时刻只有一个当前视图操作。 */
export const viewOperations = createOperationRegistry();

/* ---------- 视图导航（侧栏/首页/刷新共用的真实入口）----------
 * 导航意图发生即 begin() 新世代：同步中止当前操作（含 in-flight 的 POST 提交
 * 与 job 跟踪循环），不等新工程数据返回；GET 本身绑定该世代 signal（被更新的
 * 导航取代时请求即中止），返回前再复核世代——慢 GET 或连续点击产生的迟到
 * 响应一律丢弃，不得覆盖用户正在浏览的视图（H1 竞态修复的导航侧闭合）。 */
export function createNavigator(deps, registry = viewOperations) {
  const { getProject, renderProject, afterOpen = () => {}, notify = () => {} } = deps;
  return {
    /** 打开工程的真实导航入口：app.js 以真实依赖接线后导出供侧栏/首页使用。 */
    async openProject(id) {
      const op = registry.begin();
      let view;
      try {
        view = await getProject(id, { signal: op.controller.signal });
      } catch (error) {
        // 拉取被中止/失败：仍是当前导航才报错（过期导航静默，不打扰新视图）。
        if (registry.isCurrent(op)) notify(error.message, 'error');
        return;
      }
      // 返回前复核世代：连续点击/切页后迟到的响应不得覆盖新视图。
      if (!registry.isCurrent(op)) return;
      renderProject(view);
      afterOpen();
    },
  };
}

/* ---------- 幂等键终态清除策略（M1）----------
 * 按终态与副作用确定性决定「工程+意图」幂等键的去留：
 * - succeeded：工作流成功、检查点已推进 → 清除（后续动作指纹变化亦会轮换）。
 * - failed 且错误为已知结果（鉴权/限额/审核/结构化输出/输入校验等）：外部
 *   调用结果确定（未发生或已完成并记录在案）→ 清除；同一动作入口的重试是
 *   知情新执行，应创建新 job 真正重执行。
 * - failed 且结果未知（超时/传输/提供方不可用等，可能已扣费）→ 保留原键，
 *   重试复用同一键供后端去重，并走既有 unknown/retry 人工处置恢复路径。
 * - cancelled / interrupted / unknown（轮询失败的合成记录）：执行结果不可知
 *   （running 期取消可能已提交副作用；重启中断同理）→ 保留。
 *
 * 分类对齐后端真实契约（agent_core/jobs.py 写入的 error 记录）：
 * ① error.category——异常携带的规范化分类（ModelCallError 等经 jobs.py 透出）。
 *   与 model_router/gateway.py 的 possible_charge 判定同约定：以 _unknown 结尾
 *   （timeout_unknown/transport_unknown/rate_limited_unknown/…）→ 结果未知。
 * ② error.code——异常类型名或显式错误码。TimeoutError 等超时类型的消息可
 *   能不含 TIMEOUT 字样（如 TimeoutError("x") → {code, message:"x"}），必须
 *   按 code 识别为结果未知。
 * ③ error.message 兜底——与后端工作流 classify_error 的 message 判定一致。 */
export function shouldClearIntentKey(record) {
  if (record?.status === 'succeeded') return true;
  if (record?.status !== 'failed') return false;
  const error = record?.error || {};
  const category = String(error.category || '').toLowerCase();
  if (category) return !category.endsWith('_unknown');
  const code = String(error.code || '').toUpperCase();
  if (code.includes('TIMEOUT') || code.includes('UNKNOWN')) return false;
  const message = String(error.message || '').toUpperCase();
  return !(message.includes('TIMEOUT') || message.includes('UNKNOWN'));
}

const TERMINAL_JOB_STATUS = new Set(['succeeded', 'failed', 'cancelled', 'interrupted']);
export function isTerminalJobStatus(status) { return TERMINAL_JOB_STATUS.has(status); }

/** 进度展示用的操作名（由 payload 推断）。 */
export function jobOperation(payload) {
  if (['execute', 'edit_and_execute'].includes(payload.manual_action)) return '执行质检建议';
  if (payload.human_prompt) return '模型微调图像';
  if (payload.selected_id) return '确认主图并开始质检';
  if (payload.task_approved) return '生成候选图像';
  if (payload.final_approved || payload.manual_action === 'accept_current') return '确认最终图像';
  return '推进工作流';
}

/**
 * 创建 job 运行器。注入依赖：
 * - projectId, renderProgress(job, {onCancel}), clearProgress(), setBusy(flag)
 * - notify(message, kind?), refresh(op)（世代守卫后的工程刷新）, getProjectId()
 * - postJob(body, {signal}), track(jobId, {signal,onEvent,onDone}), cancelJob(jobId)
 * - onJobRecord(record), getCheckpoint(), schedule(fn, ms)（默认 setTimeout）, refreshDelayMs
 */
export function createJobRunner(deps, registry = viewOperations) {
  const {
    projectId, renderProgress, clearProgress, setBusy, notify = () => {}, refresh,
    postJob, track, cancelJob, onJobRecord, getProjectId, getCheckpoint,
    schedule = setTimeout, refreshDelayMs = 300,
  } = deps;
  let progress = null;
  let busy = false;
  const applyBusy = (flag) => { busy = flag; setBusy?.(flag); };

  const attach = (job, op = registry.begin(), intent = null) => {
    applyBusy(!isTerminalJobStatus(job.status));
    clearProgress?.();
    progress = renderProgress(job, {
      onCancel: async () => {
        try { await cancelJob(job.job_id); notify('已请求取消。'); } catch (error) { notify(error.message, 'error'); }
      },
    });
    track(job.job_id, {
      signal: op.controller.signal,
      onEvent: (event) => { if (registry.isCurrent(op)) progress?.update({ status: event.type }); },
      onDone: (record) => {
        if (!registry.isCurrent(op)) return;
        progress?.done(record);
        applyBusy(false);
        onJobRecord?.(record);
        if (record.status === 'succeeded') notify('已保存到新的工作流检查点。');
        else if (record.status === 'failed') notify(record.error?.message || '任务失败。', 'error');
        // M1：仅在工作流成功或确认无副作用的终态清除幂等键；未知结果保留。
        if (intent && shouldClearIntentKey(record)) clearIntentIdempotencyKey(projectId, intent);
        schedule(() => {
          // 世代守卫：确认没有更新的操作/渲染接管（如 job A 完成后用户已在同
          // 工程启动 job B），且仍在同一工程，再刷新，避免误中止新 tracker（H1）。
          if (!registry.isCurrent(op)) return;
          if (getProjectId?.() !== projectId) return;
          refresh(op);
        }, refreshDelayMs);
      },
    });
  };

  return {
    attach,
    async start(payload, { intent } = {}) {
      if (busy) return state.job;
      // POST in-flight 窗口：发请求前先建立本次操作的 controller/世代——离开
      // 视图或新操作接管时这次提交被中止，其迟到的返回不得 attach 或影响新视图。
      const op = registry.begin();
      applyBusy(true);
      clearProgress?.();
      progress = renderProgress({ project_id: projectId, operation: jobOperation(payload), status: 'submitting' }, {});
      // M1：幂等键按「工程+意图」持久化、以「检查点+负载」为指纹——同一意图
      // 重试（如 120s 超时响应丢失）复用同一键供后端去重；键的去留见 onDone。
      const body = { ...payload };
      if (intent) {
        body.idempotency_key = intentIdempotencyKey(projectId, intent, `${getCheckpoint?.() ?? ''}:${JSON.stringify(payload)}`);
      }
      try {
        const job = await postJob(body, { signal: op.controller.signal });
        // 返回后确认本操作仍当前（未切页/未被新操作取代）再登记与 attach；
        // 否则静默丢弃——后端已创建的 job 会在下次进入工程时由恢复逻辑接管。
        if (!registry.isCurrent(op)) return null;
        onJobRecord?.(job);
        attach(job, op, intent);
        return job;
      } catch (error) {
        if (!registry.isCurrent(op)) return null; // 已离开/被取代：静默
        applyBusy(false);
        progress?.done({ status: 'failed', error: { message: error.message } });
        notify(error.message, 'error');
        return null;
      }
    },
  };
}
