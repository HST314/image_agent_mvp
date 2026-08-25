import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  collectSettingsPatch,
  localSettingsDiff,
  optionsWithHistoricalValue,
} from '../../frontend/static/js/settingspage.js';

function wrapper(path, { current, initial, initiallyOverridden, override, kind = 'text' }) {
  const input = {
    value: String(current), checked: Boolean(current),
    dataset: { kind, initialValue: JSON.stringify(initial), initialOverridden: String(initiallyOverridden) },
  };
  const toggle = { checked: override };
  return {
    dataset: { path },
    querySelector(selector) { return selector.startsWith('.input') ? input : toggle; },
  };
}

test('设置补丁只包含变化，并用 null 清除当前任务覆盖', () => {
  const controls = [
    wrapper('candidate_concurrency', { current: 3, initial: 3, initiallyOverridden: true, override: false, kind: 'number' }),
    wrapper('watermark', { current: true, initial: null, initiallyOverridden: false, override: true, kind: 'checkbox' }),
    wrapper('self_check.max_rounds', { current: 6, initial: 4, initiallyOverridden: true, override: true, kind: 'number' }),
  ];
  const root = { querySelectorAll: () => controls };
  assert.deepEqual(collectSettingsPatch(root), {
    candidate_concurrency: null,
    watermark: true,
    self_check: { max_rounds: 6 },
  });
});

test('独立模式预览把嵌套设置展开为字段级未来影响', () => {
  const settings = {
    values: {
      self_check: {
        inherited: { max_rounds: 4 },
        effective: { max_rounds: 4 },
        explicit: {},
      },
    },
  };
  assert.deepEqual(
    localSettingsDiff(settings, { self_check: { max_rounds: 6 } }),
    [{ field: 'self_check.max_rounds', before: 4, after: 6 }],
  );
});

test('历史模型保持当前显示但不能作为新的可选值', () => {
  assert.deepEqual(
    optionsWithHistoricalValue([['approved-model', 'Approved model']], 'retired-model'),
    [
      ['', '请选择当前批准的模型', true],
      ['retired-model', 'retired-model（历史配置，不可再次选择）', true],
      ['approved-model', 'Approved model'],
    ],
  );
  assert.deepEqual(
    optionsWithHistoricalValue([['approved-model', 'Approved model']], 'approved-model'),
    [['approved-model', 'Approved model']],
  );
});
