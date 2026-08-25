/* Current-task runtime settings. The page keeps the established tabbed settings
 * layout while all writes remain future-only immutable task revisions. */

import { el, sectionPanel, stateBlock } from './dom.js';
import {
  clearIntentIdempotencyKey,
  getActor,
  intentIdempotencyKey,
  setActor,
} from './store.js';

const CLARIFY_FIELDS = [
  ['question_preference', '提问偏好', 'select', [['proactive', '积极追问'], ['blocking_only', '仅阻断时追问']], '控制系统补充问题的方式。'],
  ['max_auto_questions', '自动提问上限', 'number', { min: 0, max: 10 }, '单次澄清阶段允许自动提出的问题数量。'],
  ['clarification_total_budget', '澄清问题总预算', 'number', { min: 0, max: 100 }, '当前任务后续流程可使用的澄清问题总量。'],
];

const RENDER_FIELDS = [
  ['candidate_concurrency', '候选图并发数', 'number', { min: 1, max: 5 }, '同一轮并行生成的候选图数量。'],
  ['default_output_size', '默认出图尺寸', 'text', { pattern: '(?:[1-9][0-9]{1,4}x[1-9][0-9]{1,4}|[124]K)' }, '支持“宽x高”或 1K、2K、4K。'],
  ['response_format', '图片返回格式', 'select', [['url', '受控链接'], ['b64_json', '内嵌图片数据']], '控制生成图片的返回载体。'],
  ['watermark', '生成水印', 'checkbox', null, '开启后，后续生成请求会携带水印设置。'],
];

const SELF_CHECK_FIELDS = [
  ['termination', '自检终止方式', 'select', [['fix', '固定轮次'], ['solo', '达到质量门槛']], '控制质量检查循环的停止条件。'],
  ['fixed_rounds', '固定自检轮次', 'number', { min: 1, max: 20 }, '选择固定轮次时执行的检查次数。'],
  ['max_rounds', '最大自检轮次', 'number', { min: 1, max: 50 }, '质量检查允许执行的轮次上限。'],
  ['stop_early_on_pass', '通过后提前停止', 'checkbox', null, '达到质量门槛后结束后续自检轮次。'],
];

const MODEL_FIELDS = [
  ['intake_clarify', '需求澄清'],
  ['confirmation_build', '任务书生成'],
  ['initial_candidate_generation', '候选图生成'],
  ['self_check_inspection', '质量检查'],
  ['self_check_rework', '自动返修'],
  ['human_prompt_rework', '人工微调'],
];

export const SETTINGS_TAB_LAYOUT = Object.freeze([
  { id: 'clarify', title: '提问与澄清', group: null, fields: CLARIFY_FIELDS },
  {
    id: 'libraries',
    title: '数据库与放行',
    fields: [],
    note: '品类与艺术风格的放行遵循当前任务审批流程，并在工作区对应阶段完成。',
  },
  { id: 'render', title: '候选与出图', group: null, fields: RENDER_FIELDS },
  { id: 'selfcheck', title: '质量自检', group: 'self_check', fields: SELF_CHECK_FIELDS },
  {
    id: 'system',
    title: '系统与高级',
    fields: [],
    note: '系统运行与安全参数由当前任务配置基线统一管理；当前修订信息可在状态页查看。',
  },
  { id: 'models', title: '模型', group: 'advanced_model_overrides', fields: MODEL_FIELDS },
]);

const ALL_FIELD_DEFINITIONS = [...CLARIFY_FIELDS, ...RENDER_FIELDS, ...SELF_CHECK_FIELDS, ...MODEL_FIELDS];

function hasOwn(value, key) {
  return value && Object.prototype.hasOwnProperty.call(value, key);
}

function settingEntry(settings, group, key) {
  if (!group) return settings.values?.[key] || {};
  if (group === 'advanced_model_overrides' && settings.model_bindings?.[key]) {
    return settings.model_bindings[key];
  }
  const parent = settings.values?.[group] || {};
  return {
    inherited: parent.inherited?.[key],
    effective: parent.effective?.[key],
    overridden: hasOwn(parent.explicit, key),
    explicit: parent.explicit?.[key],
    source: hasOwn(parent.explicit, key) ? 'instance' : parent.source,
  };
}

function valueText(value) {
  if (value === true) return '开启';
  if (value === false) return '关闭';
  if (value === null || value === undefined || value === '') return '未设置';
  return String(value);
}

function inputValue(input, kind) {
  if (kind === 'checkbox') return input.checked;
  if (kind === 'number') return Number(input.value);
  return input.value;
}

function sameValue(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

export function optionsWithHistoricalValue(options, effective) {
  if (effective == null || options.some(([value]) => value === effective)) return options;
  return [
    ['', '请选择当前批准的模型', true],
    [effective, `${effective}（历史配置，不可再次选择）`, true],
    ...options,
  ];
}

function settingControl(settings, group, definition, modelOptions = [], editable = true) {
  const [key, label, kind = 'select', options = [], help = ''] = definition;
  const entry = settingEntry(settings, group, key);
  const path = group ? `${group}.${key}` : key;
  const wrapper = el('div', { class: `field settings-field${kind === 'checkbox' ? ' settings-field--boolean' : ''}`, dataset: { path } });
  const inputId = `setting-${path.replaceAll('.', '-')}`;
  let input;
  const resolvedOptions = group === 'advanced_model_overrides'
    ? modelOptions.map((value) => (
      typeof value === 'string' ? [value, value] : [value.id, value.label || value.id]
    ))
    : options;
  if (kind === 'select') {
    input = el('select', { id: inputId, class: 'input settings-input', required: '' });
    for (const [value, text, unavailable] of optionsWithHistoricalValue(resolvedOptions, entry.effective)) {
      const option = el('option', { value, text, disabled: unavailable ? '' : null });
      if (value === entry.effective) option.selected = true;
      input.append(option);
    }
  } else {
    input = el('input', {
      id: inputId,
      class: kind === 'checkbox' ? 'settings-input' : 'input settings-input',
      type: kind,
      required: kind === 'checkbox' ? null : '',
    });
    if (kind === 'checkbox') input.checked = Boolean(entry.effective);
    else input.value = valueText(entry.effective) === '未设置' ? '' : String(entry.effective);
    if (options && !Array.isArray(options)) {
      for (const [name, value] of Object.entries(options)) input.setAttribute(name, value);
    }
  }
  input.disabled = !editable;
  input.dataset.kind = kind;
  input.dataset.initialEffective = JSON.stringify(entry.effective ?? null);
  input.dataset.inheritedValue = JSON.stringify(entry.inherited ?? null);
  input.dataset.initialOverridden = String(entry.overridden === true);

  const source = entry.overridden ? '当前实例' : '任务基线';
  const inheritance = el('small', {
    class: 'settings-field__meta',
    text: `继承值：${valueText(entry.inherited)} · 当前生效：${valueText(entry.effective)} · 来源：${source}`,
  });
  if (kind === 'checkbox') {
    wrapper.append(el('label', { class: 'switch-row', for: inputId }, [
      input,
      el('span', {}, [el('strong', { text: label }), el('span', { text: help })]),
    ]), inheritance);
  } else {
    wrapper.append(el('label', { for: inputId, text: label }), input);
    if (help) wrapper.append(el('small', { text: help }));
    wrapper.append(inheritance);
  }
  return wrapper;
}

export function collectSettingsPatch(root) {
  const patch = {};
  for (const wrapper of root.querySelectorAll('.settings-field')) {
    const input = wrapper.querySelector('.settings-input');
    const [group, nested] = wrapper.dataset.path.includes('.')
      ? wrapper.dataset.path.split('.')
      : [null, wrapper.dataset.path];
    const current = inputValue(input, input.dataset.kind);
    const initial = JSON.parse(input.dataset.initialEffective || 'null');
    if (sameValue(current, initial)) continue;
    const inherited = JSON.parse(input.dataset.inheritedValue || 'null');
    const value = input.dataset.initialOverridden === 'true' && sameValue(current, inherited)
      ? null
      : current;
    if (group) {
      patch[group] ||= {};
      patch[group][nested] = value;
    } else {
      patch[nested] = value;
    }
  }
  return patch;
}

function renderDiff(container, diff) {
  container.textContent = '';
  if (!diff.length) {
    container.append(el('p', { class: 'settings-preview__empty', text: '没有需要保存的变化。' }));
    return;
  }
  const list = el('ul', { class: 'settings-diff' });
  for (const item of diff) {
    const fieldKey = String(item.field || '').split('.').at(-1);
    const label = ALL_FIELD_DEFINITIONS.find(([key]) => key === fieldKey)?.[1] || '运行设置';
    list.append(el('li', {}, [
      el('strong', { text: label }),
      el('span', { text: `${valueText(item.before)} → ${valueText(item.after)}` }),
      el('small', { text: '仅影响后续步骤或以后显式重跑；已完成历史保持不变。' }),
    ]));
  }
  container.append(list);
}

export function localSettingsDiff(settings, patch) {
  const diff = [];
  for (const [field, after] of Object.entries(patch)) {
    if (after && typeof after === 'object' && !Array.isArray(after)) {
      for (const [nested, nestedAfter] of Object.entries(after)) {
        const entry = settingEntry(settings, field, nested);
        diff.push({
          field: `${field}.${nested}`,
          before: entry.effective,
          after: nestedAfter === null ? entry.inherited : nestedAfter,
        });
      }
    } else {
      const entry = settingEntry(settings, null, field);
      diff.push({ field, before: entry.effective, after: after === null ? entry.inherited : after });
    }
  }
  return diff;
}

export function nextSettingsTabIndex(current, key, total) {
  if (!total) return 0;
  if (key === 'Home') return 0;
  if (key === 'End') return total - 1;
  if (key === 'ArrowRight' || key === 'ArrowDown') return (current + 1) % total;
  if (key === 'ArrowLeft' || key === 'ArrowUp') return (current - 1 + total) % total;
  return current;
}

function createSettingsTabs(form, settings, editable) {
  const tabBar = el('div', { class: 'settings-tabs', role: 'tablist', 'aria-label': '设置分组' });
  const panels = el('div', { class: 'settings-panels' });
  const buttons = [];
  const panelById = {};

  for (const tab of SETTINGS_TAB_LAYOUT) {
    const panel = sectionPanel(tab.title, '');
    panel.querySelector('.section__head')?.remove();
    panel.classList.add('settings-panel');
    panel.id = `settings-panel-${tab.id}`;
    panel.setAttribute('role', 'tabpanel');
    panel.setAttribute('aria-labelledby', `settings-tab-${tab.id}`);
    panel.tabIndex = 0;
    if (tab.fields.length) {
      const grid = el('div', { class: 'form-grid' });
      for (const field of tab.fields) {
        const modelOptions = tab.group === 'advanced_model_overrides'
          ? settings.model_options?.[field[0]] || []
          : [];
        grid.append(settingControl(settings, tab.group, field, modelOptions, editable));
      }
      panel.append(grid);
    } else {
      panel.append(el('p', { class: 'settings-boundary', text: tab.note }));
    }
    panelById[tab.id] = panel;
    panels.append(panel);
  }

  const activate = (id, focus = false) => {
    for (const button of buttons) {
      const selected = button.dataset.tab === id;
      button.classList.toggle('is-active', selected);
      button.setAttribute('aria-selected', String(selected));
      button.tabIndex = selected ? 0 : -1;
      if (selected && focus) button.focus();
    }
    for (const [panelId, panel] of Object.entries(panelById)) panel.hidden = panelId !== id;
  };

  SETTINGS_TAB_LAYOUT.forEach((tab, index) => {
    const button = el('button', {
      id: `settings-tab-${tab.id}`,
      type: 'button',
      class: 'settings-tab',
      role: 'tab',
      text: tab.title,
      dataset: { tab: tab.id },
      'aria-controls': `settings-panel-${tab.id}`,
      'aria-selected': index === 0 ? 'true' : 'false',
    });
    button.addEventListener('click', () => activate(tab.id));
    button.addEventListener('keydown', (event) => {
      const current = buttons.indexOf(button);
      const next = nextSettingsTabIndex(current, event.key, buttons.length);
      if (next === current && !['Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      activate(buttons[next].dataset.tab, true);
    });
    buttons.push(button);
    tabBar.append(button);
  });
  activate(SETTINGS_TAB_LAYOUT[0].id);
  form.append(tabBar, panels);
}

export function renderRuntimeSettingsPage(container, view, context, deps) {
  let disposed = false;
  let proposal = null;
  const controller = new AbortController();
  container.textContent = '';
  if (!view?.project_id) {
    container.append(stateBlock('empty', '尚未打开任务', '打开当前任务后可查看它独立的运行设置。'));
    return { dispose: () => controller.abort() };
  }
  if (context.managed && !context.editable) {
    container.append(stateBlock(
      'empty',
      '当前实例使用只读设置协议',
      '该实例会继续使用启动时锁定的配置；支持任务设置修订的实例可在安全检查点应用新配置。',
    ));
    return { dispose: () => controller.abort() };
  }
  const loading = stateBlock('loading', '正在读取当前任务设置', '设置仅作用于当前任务，不会修改系统默认值。');
  container.append(loading);

  void deps.load({ signal: controller.signal }).then((settings) => {
    if (disposed) return;
    container.textContent = '';
    const revision = settings.revision || {};
    const editable = settings.editable !== false;
    container.append(el('p', {
      class: 'hint settings-summary',
      text: `当前设置仅作用于这个 Image 实例的后续步骤，已完成历史保持不变。当前修订：${revision.current || '—'} · ${revision.revision_id || '未绑定'}`,
    }));
    const form = el('form', { class: 'settings-form' });
    createSettingsTabs(form, settings, editable);

    const savePanel = sectionPanel('保存', '');
    savePanel.querySelector('.section__head')?.remove();
    savePanel.classList.add('settings-save');
    let actorInput = null;
    if (!context.managed) {
      const actor = el('div', { class: 'field settings-actor' });
      actorInput = el('input', { id: 'settings-actor', class: 'input', required: '', maxlength: '128', value: getActor() || 'studio_operator' });
      actor.append(
        el('label', { for: 'settings-actor', text: '确认人身份' }),
        actorInput,
        el('small', { text: '本次设置修订会记录该身份作为审计来源。' }),
      );
      savePanel.append(actor);
    }

    const syncCandidates = settings.sync_candidates || [];
    const sync = el('label', { class: 'switch-row settings-sync' });
    const syncInput = el('input', { type: 'checkbox', id: 'settings-sync' });
    sync.append(syncInput, el('span', {}, [
      el('strong', { text: '同步到本任务其他 Image Agent' }),
      el('span', {
        text: syncCandidates.length
          ? `可同步 ${syncCandidates.length} 个尚未启动的实例；确认时会再次校验。`
          : '当前没有可同步的尚未启动实例。',
      }),
    ]));
    syncInput.disabled = !editable || !context.managed || !syncCandidates.length;
    if (context.managed) savePanel.append(sync);

    const notice = el('p', { class: 'settings-notice', role: 'status', 'aria-live': 'polite' });
    const error = el('p', { class: 'field-error settings-error', role: 'alert' });
    const previewButton = el('button', { type: 'submit', class: 'btn btn--primary', text: '预览设置变更' });
    if (!editable) {
      previewButton.disabled = true;
      notice.textContent = '已有设置修订正在处理中；当前值保持可见，处理完成后可继续修改。';
    }
    savePanel.append(el('div', { class: 'settings-actions' }, [previewButton]), error, notice);

    const preview = el('div', { class: 'settings-preview' });
    preview.hidden = true;
    preview.append(el('h3', { text: '变更预览' }));
    const previewBody = el('div', { class: 'settings-preview__body' });
    const confirmButton = el('button', { type: 'button', class: 'btn btn--primary', text: '确认创建设置修订' });
    preview.append(previewBody, el('div', { class: 'settings-actions' }, [confirmButton]));
    savePanel.append(preview);
    form.append(savePanel);
    container.append(form);

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      error.textContent = '';
      notice.textContent = '';
      proposal = null;
      const patch = collectSettingsPatch(form);
      if (!Object.keys(patch).length) {
        error.textContent = '请先调整至少一项当前任务设置。';
        return;
      }
      previewButton.disabled = true;
      previewButton.textContent = '正在生成预览…';
      try {
        if (context.managed) {
          proposal = await deps.propose({
            base_revision: Number(revision.current),
            overrides: patch,
            sync_unstarted_image_work_items: syncInput.checked,
            expected_sync_instance_ids: syncInput.checked
              ? syncCandidates.map((item) => item.instance_id)
              : [],
          });
          renderDiff(previewBody, proposal.diff || []);
        } else {
          proposal = { patch, diff: localSettingsDiff(settings, patch) };
          renderDiff(previewBody, proposal.diff);
        }
        preview.hidden = false;
        confirmButton.focus();
      } catch (requestError) {
        error.textContent = requestError.message;
      } finally {
        previewButton.disabled = false;
        previewButton.textContent = '预览设置变更';
      }
    });

    confirmButton.addEventListener('click', async () => {
      if (!proposal) return;
      error.textContent = '';
      confirmButton.disabled = true;
      confirmButton.textContent = '正在确认修订…';
      try {
        let result;
        if (context.managed) {
          result = await deps.confirm({ proposal_id: proposal.proposal_id });
        } else {
          const actor = actorInput.value.trim();
          if (!actor) throw new Error('请填写确认人身份。');
          setActor(actor);
          const fingerprint = JSON.stringify([revision.revision_id, proposal.patch]);
          const key = intentIdempotencyKey(view.project_id, 'runtime-settings', fingerprint);
          result = await deps.revise({
            base_revision_id: revision.revision_id,
            overrides: proposal.patch,
            actor,
            confirmed: true,
            idempotency_key: key,
          }, { signal: controller.signal });
          clearIntentIdempotencyKey(view.project_id, 'runtime-settings');
        }
        const waiting = result.status === 'WAITING_SAFE_POINT';
        notice.textContent = waiting
          ? '设置修订已确认，当前调用完成后将在最近安全检查点自动应用。'
          : '设置修订已应用；后续步骤将使用新配置。';
        preview.hidden = true;
        await deps.onApplied?.();
      } catch (requestError) {
        error.textContent = requestError.message;
      } finally {
        confirmButton.disabled = false;
        confirmButton.textContent = '确认创建设置修订';
      }
    });
  }).catch((error) => {
    if (disposed || controller.signal.aborted) return;
    container.textContent = '';
    container.append(stateBlock('error', '无法读取当前任务设置', error.message));
  });

  return {
    dispose() {
      disposed = true;
      controller.abort();
    },
  };
}
