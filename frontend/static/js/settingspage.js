/* Current-task runtime settings. Safe fields only; inherited values stay visible,
 * and every write creates a future-only immutable revision. */

import { el, sectionPanel, stateBlock } from './dom.js';
import {
  clearIntentIdempotencyKey,
  getActor,
  intentIdempotencyKey,
  setActor,
} from './store.js';

const TOP_LEVEL_FIELDS = [
  ['question_preference', '提问偏好', 'select', [['proactive', '积极追问'], ['blocking_only', '仅阻断时追问']]],
  ['max_auto_questions', '自动提问上限', 'number', { min: 0, max: 10 }],
  ['clarification_total_budget', '澄清问题总预算', 'number', { min: 0, max: 100 }],
  ['candidate_concurrency', '候选图并发数', 'number', { min: 1, max: 5 }],
  ['default_output_size', '默认出图尺寸', 'text', { pattern: '(?:[1-9][0-9]{1,4}x[1-9][0-9]{1,4}|[124]K)' }],
  ['response_format', '图片返回格式', 'select', [['url', '受控链接'], ['b64_json', '内嵌图片数据']]],
  ['watermark', '生成水印', 'checkbox'],
];

const SELF_CHECK_FIELDS = [
  ['termination', '自检终止方式', 'select', [['fix', '固定轮次'], ['solo', '达到质量门槛']]],
  ['fixed_rounds', '固定自检轮次', 'number', { min: 1, max: 20 }],
  ['max_rounds', '最大自检轮次', 'number', { min: 1, max: 50 }],
  ['stop_early_on_pass', '通过后提前停止', 'checkbox'],
];

const MODEL_FIELDS = [
  ['intake_clarify', '需求澄清'],
  ['confirmation_build', '任务书生成'],
  ['initial_candidate_generation', '候选图生成'],
  ['self_check_inspection', '质量检查'],
  ['self_check_rework', '自动返修'],
  ['human_prompt_rework', '人工微调'],
];

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

export function optionsWithHistoricalValue(options, effective) {
  if (effective == null || options.some(([value]) => value === effective)) return options;
  return [
    ['', '请选择当前批准的模型', true],
    [effective, `${effective}（历史配置，不可再次选择）`, true],
    ...options,
  ];
}

function settingControl(settings, group, definition, modelOptions = [], editable = true) {
  const [key, label, kind = 'select', options = []] = definition;
  const entry = settingEntry(settings, group, key);
  const path = group ? `${group}.${key}` : key;
  const wrapper = el('div', { class: 'settings-field', dataset: { path } });
  const overrideId = `override-${path.replaceAll('.', '-')}`;
  const inputId = `setting-${path.replaceAll('.', '-')}`;
  const override = el('input', {
    id: overrideId,
    type: 'checkbox',
    checked: entry.overridden ? '' : null,
  });
  override.checked = entry.overridden === true;
  override.disabled = !editable;
  const toggle = el('label', { class: 'settings-field__override', for: overrideId }, [
    override,
    el('span', { text: '覆盖当前任务' }),
  ]);
  let input;
  const resolvedOptions = group === 'advanced_model_overrides'
    ? modelOptions.map((value) => (
      typeof value === 'string' ? [value, value] : [value.id, value.label || value.id]
    ))
    : options;
  if (kind === 'select') {
    input = el('select', { id: inputId, class: 'input' });
    const historicalValue = entry.effective != null
      && !resolvedOptions.some(([value]) => value === entry.effective);
    for (const [value, text, unavailable] of optionsWithHistoricalValue(resolvedOptions, entry.effective)) {
      const option = el('option', { value, text, disabled: unavailable ? '' : null });
      if (value === entry.effective) option.selected = true;
      input.append(option);
    }
    if (historicalValue) input.dataset.historicalValue = String(entry.effective);
  } else {
    input = el('input', { id: inputId, class: kind === 'checkbox' ? '' : 'input', type: kind });
    if (kind === 'checkbox') input.checked = Boolean(entry.effective);
    else input.value = valueText(entry.effective) === '未设置' ? '' : String(entry.effective);
    if (options && !Array.isArray(options)) {
      for (const [name, value] of Object.entries(options)) input.setAttribute(name, value);
    }
  }
  input.disabled = !editable || !override.checked;
  input.dataset.kind = kind;
  input.dataset.initialValue = JSON.stringify(entry.explicit);
  input.dataset.initialOverridden = String(entry.overridden === true);
  if (override.checked && input.dataset.historicalValue) {
    input.value = '';
    input.required = true;
  }
  override.addEventListener('change', () => {
    input.disabled = !editable || !override.checked;
    if (!input.dataset.historicalValue) return;
    input.required = override.checked;
    input.value = override.checked ? '' : input.dataset.historicalValue;
  });
  const inherited = el('small', {
    text: `继承值：${valueText(entry.inherited)} · 当前生效：${valueText(entry.effective)} · 来源：${entry.overridden ? '当前任务覆盖' : '任务基线'}`,
  });
  wrapper.append(
    el('div', { class: 'settings-field__heading' }, [el('label', { for: inputId, text: label }), toggle]),
    input,
    inherited,
  );
  return wrapper;
}

export function collectSettingsPatch(root) {
  const patch = {};
  for (const wrapper of root.querySelectorAll('.settings-field')) {
    const input = wrapper.querySelector('.input, input:not([id^="override-"])');
    const override = wrapper.querySelector('input[id^="override-"]');
    const [group, nested] = wrapper.dataset.path.includes('.')
      ? wrapper.dataset.path.split('.')
      : [null, wrapper.dataset.path];
    const initiallyOverridden = input.dataset.initialOverridden === 'true';
    const current = inputValue(input, input.dataset.kind);
    const initial = JSON.parse(input.dataset.initialValue || 'null');
    let changed = false;
    let value;
    if (!override.checked && initiallyOverridden) {
      changed = true;
      value = null;
    } else if (override.checked && (!initiallyOverridden || JSON.stringify(current) !== JSON.stringify(initial))) {
      changed = true;
      value = current;
    }
    if (!changed) continue;
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
    const label = [...TOP_LEVEL_FIELDS, ...SELF_CHECK_FIELDS, ...MODEL_FIELDS]
      .find(([key]) => key === fieldKey)?.[1] || '运行设置';
    list.append(el('li', {}, [
      el('strong', { text: label }),
      el('span', { text: `${valueText(item.before)} → ${valueText(item.after)}` }),
      el('small', { text: '只影响后续步骤或以后显式重跑；不会改写已完成历史。' }),
    ]));
  }
  container.append(list);
}

export function localSettingsDiff(settings, patch) {
  const diff = [];
  for (const [field, after] of Object.entries(patch)) {
    if (after && typeof after === 'object' && !Array.isArray(after)) {
      for (const [nested, nestedAfter] of Object.entries(after)) {
        diff.push({
          field: `${field}.${nested}`,
          before: settingEntry(settings, field, nested).effective,
          after: nestedAfter,
        });
      }
    } else {
      diff.push({ field, before: settingEntry(settings, null, field).effective, after });
    }
  }
  return diff;
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
      '该实例会继续使用启动时锁定的配置；新协议实例才可在安全检查点创建设置修订。',
    ));
    return { dispose: () => controller.abort() };
  }
  const loading = stateBlock('loading', '正在读取当前任务设置', '设置仅作用于当前任务，不会修改系统默认值。');
  container.append(loading);

  void deps.load({ signal: controller.signal }).then((settings) => {
    if (disposed) return;
    container.textContent = '';
    const heading = sectionPanel('当前任务设置', '当前任务可显式产生新的未来，已经发生的历史保持不变。');
    const revision = settings.revision || {};
    heading.querySelector('.section__head').append(el('span', {
      class: 'badge badge--info',
      text: `修订 ${revision.current || '—'} · ${revision.revision_id || '未绑定'}`,
    }));
    const form = el('form', { class: 'settings-form' });
    const editable = settings.editable !== false;
    if (!editable) {
      heading.querySelector('.section__head').append(el('span', {
        class: 'badge badge--warning',
        text: '暂时只读',
      }));
    }
    const interaction = el('fieldset', { class: 'settings-group' });
    interaction.append(el('legend', { text: '交互与生成' }));
    for (const field of TOP_LEVEL_FIELDS) {
      interaction.append(settingControl(settings, null, field, [], editable));
    }
    const selfCheck = el('fieldset', { class: 'settings-group' });
    selfCheck.append(el('legend', { text: '质量检查' }));
    for (const field of SELF_CHECK_FIELDS) {
      selfCheck.append(settingControl(settings, 'self_check', field, [], editable));
    }
    const models = el('fieldset', { class: 'settings-group' });
    models.append(el('legend', { text: '各阶段模型' }));
    for (const [key, label] of MODEL_FIELDS) {
      models.append(settingControl(
        settings,
        'advanced_model_overrides',
        [key, label, 'select'],
        settings.model_options?.[key] || [],
        editable,
      ));
    }
    form.append(interaction, selfCheck, models);

    let actorInput = null;
    if (!context.managed) {
      const actor = el('div', { class: 'field settings-actor' });
      actorInput = el('input', { id: 'settings-actor', class: 'input', required: '', maxlength: '128', value: getActor() || 'studio_operator' });
      actor.append(el('label', { for: 'settings-actor', text: '操作人' }), actorInput, el('small', { text: '用于记录本次设置修订的审计来源。' }));
      form.append(actor);
    }

    const syncCandidates = settings.sync_candidates || [];
    const sync = el('label', { class: 'switch-row settings-sync' });
    const syncInput = el('input', { type: 'checkbox' });
    sync.append(syncInput, el('span', {}, [
      el('strong', { text: '同步到本主任务内其他尚未启动的 Image 工作项' }),
      el('span', { text: syncCandidates.length ? `本次预览范围为 ${syncCandidates.length} 个实例；确认时会再次严格校验。` : '当前没有可同步的未启动 Image 工作项。' }),
    ]));
    syncInput.disabled = !editable || !context.managed || !syncCandidates.length;
    if (context.managed) form.append(sync);

    const notice = el('p', { class: 'settings-notice', role: 'status', 'aria-live': 'polite' });
    const error = el('p', { class: 'field-error settings-error', role: 'alert' });
    const previewButton = el('button', { type: 'submit', class: 'btn btn--primary', text: '预览设置变更' });
    if (!editable) {
      previewButton.disabled = true;
      notice.textContent = '已有设置正在等待应用或需要主系统处理；当前值保持可见，完成处理后可继续修改。';
    }
    form.append(el('div', { class: 'settings-actions' }, [previewButton]), error, notice);
    heading.append(form);

    const preview = sectionPanel('变更预览', '确认后生成不可变修订；运行中的调用不会被取消或换模。');
    preview.hidden = true;
    const previewBody = el('div', { class: 'settings-preview' });
    const confirmButton = el('button', { type: 'button', class: 'btn btn--primary', text: '确认创建设置修订' });
    preview.append(previewBody, el('div', { class: 'settings-actions' }, [confirmButton]));
    container.append(heading, preview);

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      error.textContent = '';
      notice.textContent = '';
      proposal = null;
      const patch = collectSettingsPatch(form);
      if (!Object.keys(patch).length) {
        error.textContent = '请先修改至少一项当前任务设置。';
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
          if (!actor) throw new Error('请填写操作人。');
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
