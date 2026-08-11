/* 顶部导航视图切换决策（T1 布局总框架）：纯逻辑核心。DOM 与网络全部由
 * app.js 注入，本模块不触碰 document/fetch，可在 Node 下直接做
 * 「占位页 → 慢工程 GET → 重点击当前页签」交错回归测试（H1 竞态修复的
 * 视图侧闭合：最新页签意图必须能中止在途工程导航）。 */

import { VIEWS } from './topnav.js';

export function createViewSwitcher(deps) {
  const {
    getState, patch, markActiveTab,
    stopJobTracking, renderPlaceholder, openProject, goHome,
  } = deps;

  return {
    setView(view) {
      if (!VIEWS.includes(view)) return;
      if (view === getState().view) {
        /* 占位页上重点击当前页签 = 留在本页的最新意图：中止在途的工程导航
         * （侧栏慢 GET 尚未返回），其迟到响应不得把界面切回工作区（H1）。
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
      renderPlaceholder(view);
    },
  };
}
