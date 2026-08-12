/* 顶部导航视图切换决策（T1 布局总框架）：纯逻辑核心。DOM 与网络全部由
 * app.js 注入，本模块不触碰 document/fetch，可在 Node 下直接做
 * 「状态/设置页 → 慢工程 GET → 重点击当前页签」交错回归测试（H1 竞态修复的
 * 视图侧闭合：最新页签意图必须能中止在途工程导航）。
 * T2/T3 起 renderPage 渲染真实状态页/设置页（原为占位页）。 */

import { VIEWS } from './topnav.js';

/* 状态/设置页刷新控制器：刷新 GET 与应用级操作世代绑定，同时锁定发起时的
 * 页签和工程。仅检查页签不足以防止 A 工程慢响应在用户打开 B、再回到同一
 * 页签后覆盖 B；三重守卫确保迟到响应只能更新原工程的原页面。 */
export function createAuxPageRefresher(deps, registry) {
  const {
    getState, getProject, loadProjects = () => {}, patch,
    renderPage, notify = () => {},
  } = deps;

  return {
    async refresh() {
      const op = registry.begin();
      const viewAtCall = getState().view;
      const projectAtCall = getState().current?.project_id || null;
      // 工程目录独立刷新，不阻塞当前工程视图返回和渲染。
      void loadProjects();
      if (!projectAtCall) {
        if (registry.isCurrent(op) && getState().view === viewAtCall && !getState().current) {
          renderPage(viewAtCall);
        }
        return;
      }
      try {
        const view = await getProject(projectAtCall, { signal: op.controller.signal });
        const current = getState();
        if (!registry.isCurrent(op)) return;
        if (current.view !== viewAtCall || current.current?.project_id !== projectAtCall) return;
        if (view?.project_id !== projectAtCall) return;
        patch({ current: view });
        renderPage(viewAtCall);
      } catch (error) {
        if (!registry.isCurrent(op)) return;
        const current = getState();
        if (current.view !== viewAtCall || current.current?.project_id !== projectAtCall) return;
        notify(error.message, 'error');
      }
    },
  };
}

export function createViewSwitcher(deps) {
  const {
    getState, patch, markActiveTab,
    stopJobTracking, renderPage, openProject, goHome,
  } = deps;

  return {
    setView(view) {
      if (!VIEWS.includes(view)) return;
      if (view === getState().view) {
        /* 状态/设置页上重点击当前页签 = 留在本页的最新意图：中止在途的工程
         * 导航（侧栏慢 GET 尚未返回），其迟到响应不得把界面切回工作区（H1）。
         * 工作区视图可能挂着进行中的 job 跟踪循环，同页签点击不得中止，
         * 直接忽略。 */
        if (view !== 'workspace') stopJobTracking();
        return;
      }
      if (view === 'workspace') {
        const current = getState().current;
        if (current) {
          patch({ view });
          markActiveTab(view);
          openProject(current.project_id);
        } else {
          goHome();
        }
        return;
      }
      /* 离开工作区：中止进行中的操作与跟踪循环（后台 job 仍继续，
       * 回到工作区重新打开工程时会按既有逻辑恢复挂载）。 */
      stopJobTracking();
      patch({ view });
      markActiveTab(view);
      renderPage(view);
    },
  };
}
