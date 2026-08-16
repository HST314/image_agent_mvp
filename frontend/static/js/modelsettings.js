/* 设置页「模型」标签页：模型库备选池 + 各阶段运行时绑定。
 * 纯逻辑（选项过滤/绑定收集）不触碰 DOM，可在 Node 下回归；
 * 能力匹配：阶段下拉只列出其 model_role 对应分组的库模型（后端保存时二次校验）。 */

import { el, toast } from './dom.js';
import { modelStateLabel } from './copy.js';
import { getModelSettings, saveModelBindings } from './api.js';

/** 阶段当前绑定 → 匹配的库条目 id（按 provider+model 反查；库里没有则返回 ''）。 */
export function currentEntryId(library, group, binding) {
  if (!binding) return '';
  const hit = (library?.[group] || []).find(
    (entry) => entry.provider === binding.provider && entry.model === binding.model,
  );
  return hit ? hit.id : '';
}

/** 单阶段下拉选项：[{ value, label, selected }]；当前绑定不在库中时追加占位项提示。 */
export function buildModelOptions(library, stateEntry) {
  const entries = library?.[stateEntry?.group] || [];
  const selected = currentEntryId(library, stateEntry?.group, stateEntry?.binding);
  const options = entries.map((entry) => ({
    value: entry.id,
    label: entry.description ? `${entry.label}｜${entry.description}` : entry.label,
    selected: entry.id === selected,
  }));
  if (!selected && stateEntry?.binding) {
    options.unshift({
      value: '',
      label: `当前绑定不在模型库中（${stateEntry.binding.model}），请选择`,
      selected: true,
    });
  }
  return options;
}

/** 读取面板中所有阶段下拉的选中值 → { state: entryId }（仅占位项未动的阶段不提交）。 */
export function collectBindings(panel, states) {
  const bindings = {};
  for (const item of states || []) {
    const select = panel.querySelector(`select[data-model-state="${item.state}"]`);
    if (select && select.value) bindings[item.state] = select.value;
  }
  return bindings;
}

/**
 * 渲染「模型」标签页内容。deps.getActor() 提供确认人身份（与运行策略共用）。
 */
export function renderModelSettings(container, { getActor, signal } = {}) {
  const holder = el('div');
  holder.append(el('p', { class: 'hint', text: '正在读取模型库与当前绑定…' }));
  container.append(holder);

  getModelSettings({ signal })
    .then((data) => {
      holder.textContent = '';
      holder.append(el('p', {
        class: 'hint',
        text: '各阶段只能选用与其能力匹配的模型；保存后到下一个阶段边界自动生效，对全部工程生效。',
      }));
      const grid = el('div', { class: 'form-grid' });
      for (const stateEntry of data.states || []) {
        const wrap = el('div', { class: 'field' });
        const id = `model-${stateEntry.state}`;
        wrap.append(el('label', { for: id, text: modelStateLabel(stateEntry.state) }));
        const select = el('select', { class: 'input', id, dataset: { modelState: stateEntry.state } });
        for (const option of buildModelOptions(data.library, stateEntry)) {
          const node = el('option', { value: option.value, text: option.label });
          if (option.selected) node.setAttribute('selected', 'selected');
          select.append(node);
        }
        wrap.append(select);
        grid.append(wrap);
      }
      holder.append(grid);

      const error = el('div', { class: 'field-error', role: 'alert' });
      const saveBtn = el('button', { type: 'button', class: 'btn btn--primary', text: '保存模型设置' });
      saveBtn.addEventListener('click', async () => {
        error.textContent = '';
        const actor = String(getActor?.() || '').trim();
        if (!actor) {
          error.textContent = '请先在下方「保存」区填写确认人身份。';
          return;
        }
        const bindings = collectBindings(holder, data.states);
        if (!Object.keys(bindings).length) {
          error.textContent = '模型库为空或未选择任何模型。';
          return;
        }
        saveBtn.disabled = true;
        try {
          const updated = await saveModelBindings({ bindings, actor, confirmed: true });
          holder.querySelectorAll('select[data-model-state]').forEach((select) => {
            const stateEntry = (updated.states || []).find((item) => item.state === select.dataset.modelState);
            if (!stateEntry) return;
            const selected = currentEntryId(updated.library, stateEntry.group, stateEntry.binding);
            select.value = selected;
          });
          toast('模型设置已保存，到下一个阶段边界自动生效。');
        } catch (err) {
          error.textContent = err.message;
        } finally {
          saveBtn.disabled = false;
        }
      });
      holder.append(error, saveBtn);
    })
    .catch((err) => {
      holder.textContent = '';
      holder.append(el('p', { class: 'field-error', role: 'alert', text: `模型设置读取失败：${err.message}` }));
    });

  return holder;
}
