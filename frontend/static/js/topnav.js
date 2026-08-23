/* 顶部导航（T1 布局总框架）：页签激活态 + 「工程名 · 分支」上下文标识。
 * 纯 DOM 小模块，不触碰网络与 store，供 app.js / project.js 共同使用。 */

import { $, $$ } from './dom.js';

export const VIEWS = ['workspace', 'status'];

/** 高亮当前视图页签（aria-current=page），其余移除。 */
export function markActiveTab(view) {
  $$('.topnav__tab').forEach((tab) => {
    if (tab.dataset.view === view) tab.setAttribute('aria-current', 'page');
    else tab.removeAttribute('aria-current');
  });
}

/** 更新顶栏右侧的当前工程标识；projectId 为空时隐藏。 */
export function setTopContext({ projectId = null, branch = null } = {}) {
  const wrap = $('#topnav-project');
  if (!wrap) return;
  wrap.hidden = !projectId;
  if (!projectId) return;
  $('#topnav-project-name').textContent = projectId;
  $('#topnav-branch').textContent = `分支 ${branch || 'main'}`;
}
