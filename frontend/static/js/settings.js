/* T3 设置页（契约 §5/§8，Q3-B/Q4-A）：运行策略全部字段的中文化 UI 表单，
 * 按功能分标签页（提问与澄清/数据库与放行/候选与出图/质量自检/模型/系统与高级）；
 * 字段清单/类型/约束取自后端 GET /settings/schema，前端不硬编码
 * （表单模型逻辑在 policyform.js，Node 可回归）。
 * 保存契约：保存即更新全局 runtime.yaml；当前打开工程存在时，同时为其创建
 * 策略修订分支并立即应用。设置页不再依赖先打开某个工程。 */

import { el, toast, stateBlock, sectionPanel } from './dom.js';
import { getGlobalSettingsSchema, reviseGlobalPolicy } from './api.js';
import { buildPolicyFormModel, buildPolicyPayload } from './policyform.js';
import { renderModelSettings } from './modelsettings.js';
import { getActor, setActor } from './store.js';

/**
 * deps.onSaved(projectView)：当前工程存在时，保存成功并创建新分支后回调；
 * 未打开工程时 projectView 为 null，但全局默认仍已保存。
 * 返回 { dispose }：切页/离开时中止在途的 schema 拉取，保存响应迟到时不再
 * 改写界面。
 */
export function renderSettingsPage(container, view, { onSaved } = {}) {
  const projectId = view?.project_id || null;
  let disposed = false;
  const controller = new AbortController();

  const holder = el('div');
  holder.append(stateBlock('loading', '正在读取运行策略…', '字段清单与当前值来自后端设置接口。'));
  container.append(holder);

  getGlobalSettingsSchema({ signal: controller.signal })
    .then((schema) => {
      if (disposed) return;
      holder.textContent = '';
      renderForm(holder, buildPolicyFormModel(schema));
    })
    .catch((error) => {
      if (disposed) return;
      holder.textContent = '';
      holder.append(stateBlock('error', '运行策略读取失败', error.message));
    });

  function renderForm(root, model) {
    /* 生效时机说明（Q4-A） */
    root.append(el('p', { class: 'hint', text: projectId
      ? '保存即更新全局默认，并为当前工程创建策略修订分支；旧分支与历史保持不变。'
      : '保存即更新全局默认，随后新建的工程会直接使用这些参数。' }));

    const form = el('form', { class: 'settings-form', novalidate: 'novalidate' });

    /* 功能标签页：策略字段页来自 schema 表单模型；「模型」页由模型库接口驱动。 */
    const tabs = [...model.tabs.map((tab) => ({ id: tab.id, title: tab.title })), { id: 'models', title: '模型' }];
    const tabBar = el('div', { class: 'settings-tabs', role: 'tablist', 'aria-label': '设置分组' });
    const panels = el('div', { class: 'settings-panels' });
    const panelById = {};
    for (const tab of model.tabs) {
      const panel = sectionPanel(tab.title, '');
      panel.querySelector('.section__head')?.remove();
      const grid = el('div', { class: 'form-grid' });
      for (const field of tab.fields) grid.append(policyField(field));
      panel.append(grid);
      panelById[tab.id] = panel;
      panels.append(panel);
    }
    const modelsPanel = sectionPanel('模型', '');
    modelsPanel.querySelector('.section__head')?.remove();
    renderModelSettings(modelsPanel, {
      signal: controller.signal,
      getActor: () => actorInput.value,
    });
    panelById.models = modelsPanel;
    panels.append(modelsPanel);

    const activate = (id) => {
      tabBar.querySelectorAll('.settings-tab').forEach((btn) => {
        const on = btn.dataset.tab === id;
        btn.classList.toggle('is-active', on);
        btn.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      for (const [panelId, panel] of Object.entries(panelById)) {
        panel.hidden = panelId !== id;
      }
    };
    tabs.forEach((tab, index) => {
      const btn = el('button', {
        type: 'button', class: 'settings-tab', role: 'tab',
        text: tab.title, dataset: { tab: tab.id },
        'aria-selected': index === 0 ? 'true' : 'false',
      });
      btn.addEventListener('click', () => activate(tab.id));
      tabBar.append(btn);
    });
    activate(tabs[0].id);
    form.append(tabBar, panels);

    /* 保存区：确认人身份（修订契约必填）+ 错误提示 + 保存按钮 */
    const savePanel = sectionPanel('保存', '');
    savePanel.querySelector('.section__head')?.remove();
    const actorField = el('div', { class: 'field' });
    const actorInput = el('input', { class: 'input', id: 'settings-actor', required: 'required', placeholder: '例如：zhangsan', value: getActor() });
    actorField.append(
      el('label', { for: 'settings-actor', text: '确认人身份' }),
      actorInput,
      el('small', { text: '策略修订会记入审计事件，需填写操作人身份。' }),
    );
    const error = el('div', { class: 'field-error', role: 'alert' });
    const saveBtn = el('button', { type: 'submit', class: 'btn btn--primary', text: '保存运行策略' });
    savePanel.append(actorField, error, saveBtn);
    form.append(savePanel);

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      error.textContent = '';
      let policy;
      try {
        policy = buildPolicyPayload(model.all, readValues(form, model.all));
      } catch (err) {
        error.textContent = err.message; // 表单级校验文案已是中文（policyform.js）
        return;
      }
      const actor = actorInput.value.trim();
      if (!actor) {
        error.textContent = '请填写确认人身份。';
        actorInput.focus();
        return;
      }
      saveBtn.disabled = true;
      try {
        const result = await reviseGlobalPolicy({ policy, actor, confirmed: true, project_id: projectId });
        if (disposed) return; // 已切页：不再改写界面（后端分支已创建，属正常完成）
        setActor(actor);
        toast(result.branch ? `全局设置已生效，并创建当前工程分支 ${result.branch}。` : '全局设置已保存，新工程将立即使用。');
        onSaved?.(result.project || null);
      } catch (err) {
        if (disposed) return;
        error.textContent = err.message; // 422 等后端错误已经 api.formatError 中文化
        saveBtn.disabled = false;
      }
    });

    root.append(form);
  }

  return {
    dispose() {
      disposed = true;
      controller.abort();
    },
  };
}

/* 单个字段控件：enum→下拉框，boolean→开关行，number→数字输入，text→文本输入，
 * fixed（后端常量）→只读展示。中文名 + 解释性小字（契约 §8），英文键名不上屏。 */
function policyField(field) {
  const wrap = el('div', { class: 'field', dataset: { path: field.path } });
  const id = `policy-${field.path.replace(/[^A-Za-z0-9_-]/g, '-')}`;

  if (field.kind === 'boolean') {
    const input = el('input', { type: 'checkbox', id, name: field.path });
    if (field.value === true) input.setAttribute('checked', 'checked');
    const row = el('label', { class: 'switch-row', for: id }, [
      input,
      el('span', {}, [el('strong', { text: field.label }), el('span', { text: field.help || '' })]),
    ]);
    wrap.append(row);
    return wrap;
  }

  wrap.append(el('label', { for: id, text: field.label }));
  if (field.kind === 'enum') {
    const select = el('select', { class: 'input', id, name: field.path });
    for (const option of field.options || []) {
      const node = el('option', { value: String(option.value), text: option.label });
      if (option.value === field.value) node.setAttribute('selected', 'selected');
      select.append(node);
    }
    wrap.append(select);
  } else if (field.kind === 'fixed') {
    /* 后端锁定的常量字段（如「流式输出：当前固定关闭」）：只读展示，不参与编辑。 */
    wrap.append(el('input', { class: 'input', id, name: field.path, value: fixedText(field), disabled: 'disabled', 'aria-readonly': 'true' }));
  } else {
    const attrs = { class: 'input', id, name: field.path, value: String(field.value ?? '') };
    if (field.kind === 'number') {
      attrs.type = 'number';
      if (field.min !== undefined) attrs.min = String(field.min);
      if (field.max !== undefined) attrs.max = String(field.max);
      attrs.step = field.integer ? '1' : 'any';
    } else {
      attrs.type = 'text';
      if (field.pattern) attrs.pattern = field.pattern;
    }
    wrap.append(el('input', attrs));
  }
  if (field.help) wrap.append(el('small', { text: field.help }));
  return wrap;
}

/* fixed 常量的中文展示：布尔常量显示 开启/关闭，其余显示原值（纯数字/英文常量值
 * 属「当前固定」类技术约束，附说明小字解释）。 */
function fixedText(field) {
  if (typeof field.value === 'boolean') return field.value ? '开启（当前固定）' : '关闭（当前固定）';
  return `${String(field.value)}（当前固定）`;
}

/* 从表单控件收集 path → 原始输入值（boolean 取 checked，fixed 不读控件）。 */
function readValues(form, fields) {
  const values = {};
  for (const field of fields) {
    if (field.kind === 'fixed') continue;
    const control = form.elements[field.path];
    if (!control) continue;
    values[field.path] = field.kind === 'boolean' ? control.checked : control.value;
  }
  return values;
}
