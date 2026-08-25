import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  collectSettingsPatch,
  localSettingsDiff,
  nextSettingsTabIndex,
  optionsWithHistoricalValue,
  SETTINGS_TAB_LAYOUT,
} from '../../frontend/static/js/settingspage.js';

function wrapper(path, { current, initial, inherited, initiallyOverridden, kind = 'text' }) {
  const input = {
    value: String(current), checked: Boolean(current),
    dataset: {
      kind,
      initialEffective: JSON.stringify(initial),
      inheritedValue: JSON.stringify(inherited),
      initialOverridden: String(initiallyOverridden),
    },
  };
  return {
    dataset: { path },
    querySelector() { return input; },
  };
}

test('设置补丁只包含实际变化，改回继承值时恢复任务基线', () => {
  const controls = [
    wrapper('category_constraint.release', { current: 'manual', initial: 'off', inherited: 'off', initiallyOverridden: false }),
    wrapper('style_direction.release', { current: 'auto', initial: 'manual', inherited: 'off', initiallyOverridden: true }),
    wrapper('candidate_concurrency', { current: 5, initial: 3, inherited: 5, initiallyOverridden: true, kind: 'number' }),
    wrapper('watermark', { current: true, initial: false, inherited: false, initiallyOverridden: false, kind: 'checkbox' }),
    wrapper('self_check.max_rounds', { current: 6, initial: 4, inherited: 4, initiallyOverridden: true, kind: 'number' }),
  ];
  const root = { querySelectorAll: () => controls };
  assert.deepEqual(collectSettingsPatch(root), {
    category_constraint: { release: 'manual' },
    style_direction: { release: 'auto' },
    candidate_concurrency: null,
    watermark: true,
    self_check: { max_rounds: 6 },
  });
});

test('六个设置选项卡保持既定顺序', () => {
  assert.deepEqual(
    SETTINGS_TAB_LAYOUT.map(({ id, title }) => [id, title]),
    [
      ['clarify', '提问与澄清'],
      ['libraries', '数据库与放行'],
      ['render', '候选与出图'],
      ['selfcheck', '质量自检'],
      ['system', '系统与高级'],
      ['models', '模型'],
    ],
  );
});

test('设置选项卡支持方向键与首尾键循环导航', () => {
  assert.equal(nextSettingsTabIndex(0, 'ArrowRight', 6), 1);
  assert.equal(nextSettingsTabIndex(0, 'ArrowLeft', 6), 5);
  assert.equal(nextSettingsTabIndex(3, 'Home', 6), 0);
  assert.equal(nextSettingsTabIndex(2, 'End', 6), 5);
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

test('数据库放行设置保留两个独立策略字段及三种放行方式', () => {
  const libraryTab = SETTINGS_TAB_LAYOUT.find(({ id }) => id === 'libraries');
  assert.deepEqual(
    libraryTab.fields.map(([path, label, _kind, options]) => ({
      path,
      label,
      values: options.map(([value]) => value),
    })),
    [
      {
        path: 'category_constraint.release',
        label: '品类约束放行方式',
        values: ['auto', 'manual', 'off'],
      },
      {
        path: 'style_direction.release',
        label: '艺术风格放行方式',
        values: ['auto', 'manual', 'off'],
      },
    ],
  );
});

test('独立模式预览把恢复基线显示为继承后的有效值', () => {
  const settings = {
    values: {
      candidate_concurrency: {
        inherited: 5,
        effective: 3,
        overridden: true,
        explicit: 3,
      },
    },
  };
  assert.deepEqual(
    localSettingsDiff(settings, { candidate_concurrency: null }),
    [{ field: 'candidate_concurrency', before: 3, after: 5 }],
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
