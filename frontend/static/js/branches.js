/* 分支查看/切换界面：顶栏「分支 xxx」按钮打开的对话框。
 * 列表/切头选择为纯函数（branchListModel），可在 Node 下直接回归；
 * 切换只移动当前分支指针（后端 switch 接口），不改任何历史。 */

import { el, toast, formatDate } from './dom.js';
import * as api from './api.js';
import { stateLabel } from './states.js';

/**
 * 分支列表视图模型：当前分支置顶，其余按创建时间倒序；
 * 每项附分支头检查点（sequence 最大者）作为切换目标。
 */
export function branchListModel(payload) {
  const items = Array.isArray(payload?.items) ? payload.items : [];
  const rows = items.map((item) => {
    const checkpoints = Array.isArray(item.checkpoints) ? item.checkpoints : [];
    const head = checkpoints.reduce(
      (latest, cp) => (!latest || Number(cp?.sequence) > Number(latest?.sequence) ? cp : latest),
      null,
    );
    return {
      name: String(item?.name || ''),
      current: Boolean(item?.current),
      parent: item?.parent || null,
      mode: item?.mode === 'rerun_stage' ? 'rerun_stage' : 'fork_after',
      createdAt: item?.created_at || null,
      headCheckpointId: head?.checkpoint_id || null,
      headState: head?.state || null,
      headSequence: Number.isInteger(head?.sequence) ? head.sequence : null,
    };
  }).filter((row) => row.name);
  return rows.sort((a, b) => {
    if (a.current !== b.current) return a.current ? -1 : 1;
    return String(b.createdAt || '').localeCompare(String(a.createdAt || ''));
  });
}

/** 打开分支界面：列出全部分支，可切换到非当前分支的分支头。 */
export async function openBranchDialog({ projectId, onSwitched }) {
  const dialog = el('dialog', { class: 'dialog branch-dialog', 'aria-labelledby': 'branch-dialog-title' });
  const body = el('div', { class: 'dialog__body' });
  body.append(el('p', { class: 'hint', text: '正在读取分支列表…' }));
  dialog.append(
    el('div', { class: 'dialog__head' }, [
      el('div', {}, [
        el('h2', { id: 'branch-dialog-title', text: '查看分支' }),
        el('p', { text: '切换分支只改变当前查看与继续的位置，各分支历史保持不变。' }),
      ]),
    ]),
    body,
  );
  const foot = el('div', { class: 'dialog__foot' });
  const close = el('button', { type: 'button', class: 'btn btn--secondary', text: '关闭' });
  close.addEventListener('click', () => dialog.close());
  foot.append(close);
  dialog.append(foot);
  dialog.addEventListener('close', () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();

  let rows;
  try {
    rows = branchListModel(await api.listBranches(projectId));
  } catch (error) {
    body.textContent = '';
    body.append(el('p', { class: 'field-error', role: 'alert', text: error.message }));
    return;
  }
  body.textContent = '';
  if (!rows.length) {
    body.append(el('p', { class: 'hint', text: '当前工程还没有分支。' }));
    return;
  }
  const list = el('div', { class: 'branch-list', role: 'list', 'aria-label': '全部分支' });
  for (const row of rows) {
    const item = el('div', { class: `branch-item${row.current ? ' is-current' : ''}`, role: 'listitem' });
    const head = el('div', { class: 'branch-item__head' });
    head.append(el('div', { class: 'branch-item__name' }, [
      el('span', { text: row.name }),
      row.current ? el('span', { class: 'badge badge--success', text: '当前分支' }) : null,
    ]));
    const meta = [
      row.createdAt ? `创建于 ${formatDate(row.createdAt)}` : null,
      row.parent ? `自 ${row.parent} ${row.mode === 'rerun_stage' ? '重跑创建' : '快照创建'}` : null,
      row.headState ? `当前节点 ${stateLabel(row.headState)}` : null,
      Number.isInteger(row.headSequence) ? `检查点 ${row.headSequence}` : null,
    ].filter(Boolean).join(' · ');
    head.append(el('div', { class: 'branch-item__meta', text: meta || '—' }));
    item.append(head);
    if (!row.current && row.headCheckpointId) {
      const switchBtn = el('button', { type: 'button', class: 'btn btn--primary branch-item__actions', text: '切换到此分支' });
      switchBtn.addEventListener('click', async () => {
        switchBtn.disabled = true;
        switchBtn.textContent = '正在切换…';
        try {
          await api.switchBranch(projectId, row.headCheckpointId);
          toast(`已切换到分支 ${row.name}。`);
          dialog.close();
          onSwitched?.();
        } catch (error) {
          switchBtn.disabled = false;
          switchBtn.textContent = '切换到此分支';
          toast(error.message, 'error');
        }
      });
      item.append(switchBtn);
    }
    list.append(item);
  }
  body.append(list);
}
