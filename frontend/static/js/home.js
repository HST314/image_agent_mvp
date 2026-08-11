/* 首页视图：工程总览与新建入口（一期手工入口已标明调试性质）。 */

import { el, icons, stateBlock } from './dom.js';
import { state } from './store.js';
import { STATE_LABELS } from './states.js';

export function renderHome(content, { onNew, onOpen }) {
  content.textContent = '';
  const hero = el('section', { class: 'hero' });
  const mainPanel = el('div', { class: 'panel hero__main' });
  mainPanel.append(
    el('span', { class: 'badge', text: '生产工作流' }),
    el('h2', { text: '从清晰需求到可审计视觉交付' }),
    el('p', { text: '在一个可恢复的工作台中完成需求澄清、任务确认、五图选择、逐轮质检和最终审批。每一步都保存为后端检查点。' }),
  );
  const newBtn = el('button', { class: 'btn btn--primary', type: 'button' });
  newBtn.innerHTML = icons.image;
  newBtn.append('新建视觉工程');
  newBtn.addEventListener('click', onNew);
  mainPanel.append(newBtn);
  const aside = el('aside', { class: 'panel hero__aside' });
  const stat = el('div');
  stat.append(el('div', { class: 'stat-label', text: '现有工程' }), el('div', { class: 'stat-value', text: String(state.projects.length) }));
  aside.append(stat, el('div', { class: 'hint', text: '工作流会在需要你决策时暂停。断线或关闭页面不会丢失已经完成的节点。' }));
  hero.append(mainPanel, aside);
  content.append(hero);

  const recent = el('section', { class: 'panel section' });
  const head = el('div', { class: 'section__head' });
  const headText = el('div');
  headText.append(el('h2', { text: '最近工程' }), el('p', { text: '选择工程可继续上一次的检查点' }));
  head.append(headText);
  recent.append(head);
  if (state.projects.length) {
    const grid = el('div', { class: 'candidate-grid' });
    for (const p of state.projects.slice(0, 4)) {
      const card = el('button', { class: 'candidate', type: 'button' });
      const image = el('span', { class: 'candidate__image' });
      image.innerHTML = icons.image;
      card.append(image, el('span', { class: 'candidate__label', text: `${p.project_id} · ${STATE_LABELS[p.state] || '待处理'}` }));
      card.addEventListener('click', () => onOpen(p.project_id));
      grid.append(card);
    }
    recent.append(grid);
  } else {
    const emptyBtn = el('button', { class: 'btn btn--secondary', type: 'button', text: '创建第一个工程' });
    emptyBtn.addEventListener('click', onNew);
    recent.append(stateBlock('empty', '尚无创作工程', '准备一份任务卡 JSON，系统会从真实后端工作流开始推进。', emptyBtn));
  }
  content.append(recent);
}
