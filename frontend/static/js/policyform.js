/* T3 设置页表单模型（契约 §5/§8，Q3-B）：后端 GET /settings/schema 返回的
 * JSON Schema → 中文化表单模型。纯函数、不触碰 DOM/网络，可在 Node 下回归。
 *
 * 三条不可偏离（Q3-B）：全字段、中文化、分「常用 / 高级」两组。
 * - 字段清单/类型/约束全部来自后端 schema，前端不硬编码字段清单；
 * - 中文名与说明小字经 copy.js 唯一映射层（契约 §8 基线 + §11 兜底）；
 * - 分组按契约 §8 基线顺序；schema 新增字段自动落入高级组末尾，标签走
 *   中文兜底（「其他策略项」），英文键名绝不上屏。 */

import { policyKeyLabel, policyKeyHelp, policyOptionLabel } from './copy.js';

/* 契约 §8 分组基线：常用组 / 高级组的字段路径与排列顺序。 */
const COMMON_PATHS = [
  'max_auto_questions', 'clarification_total_budget',
  'skill_invocation.release',
  'self_check.termination', 'self_check.fixed_rounds', 'self_check.max_rounds',
  'self_check.stop_early_on_pass', 'self_check.release',
  'candidate_concurrency', 'default_output_size', 'watermark', 'offline_mode',
];
const ADVANCED_PATHS = [
  'model_timeout_seconds', 'image_api_base_url', 'response_format',
  'max_render_retries', 'allow_skill_degradation', 'style_library_root',
  'stream_model_output',
];

/** 解析 JSON Schema 的 $ref（#/$defs/X 一层），非 $ref 原样返回。 */
function resolveRef(node, defs) {
  const ref = node?.$ref;
  if (!ref) return node || {};
  const name = String(ref).split('/').pop();
  return defs?.[name] || {};
}

/**
 * schema properties 拍平为「点路径 → 字段 schema」：嵌套对象（如 self_check）
 * 展开一层为 self_check.termination 等子字段；其余字段按原路径保留。
 */
export function flattenSchemaProperties(properties = {}, defs = {}) {
  const flat = {};
  for (const [key, raw] of Object.entries(properties || {})) {
    const node = resolveRef(raw, defs);
    if (node?.type === 'object' && node.properties && typeof node.properties === 'object') {
      for (const [sub, subRaw] of Object.entries(node.properties)) {
        flat[`${key}.${sub}`] = resolveRef(subRaw, defs);
      }
    } else {
      flat[key] = raw?.$ref ? node : raw;
    }
  }
  return flat;
}

/** 字段控件类型：fixed（常量只读）/ enum / boolean / number / text。 */
function kindOf(node) {
  if (node && typeof node === 'object' && 'const' in node) return 'fixed';
  if (Array.isArray(node?.enum)) return 'enum';
  if (node?.type === 'boolean') return 'boolean';
  if (node?.type === 'integer' || node?.type === 'number') return 'number';
  return 'text';
}

/** 当前值取值：current 嵌套值 → schema default → 按类型的安全空值。 */
function currentValue(current, path, node, kind) {
  const [head, sub] = String(path).split('.');
  const fromCurrent = sub ? current?.[head]?.[sub] : current?.[head];
  if (fromCurrent !== undefined && fromCurrent !== null) return fromCurrent;
  if (node && 'const' in node) return node.const;
  if (node && node.default !== undefined) return node.default;
  if (kind === 'boolean') return false;
  if (kind === 'number') return 0;
  return '';
}

/**
 * 后端 settings/schema 响应 → 表单模型：
 * { common: [field], advanced: [field], all: [field] }，field =
 * { path, label, help, kind, value, disabled, options?, min?, max?, integer?, pattern? }。
 * options: [{ value, label(中文) }]；fixed 字段 disabled 且 value 恒为 const。
 */
export function buildPolicyFormModel(schema = {}) {
  const { properties = {}, $defs = {}, current = {} } = schema || {};
  const flat = flattenSchemaProperties(properties, $defs);
  const fields = [];
  for (const [path, node] of Object.entries(flat)) {
    const kind = kindOf(node);
    const field = {
      path,
      label: policyKeyLabel(path),
      help: policyKeyHelp(path),
      kind,
      value: currentValue(current, path, node, kind),
      disabled: kind === 'fixed',
    };
    if (kind === 'enum') {
      field.options = node.enum.map((value) => ({ value, label: policyOptionLabel(path, value) }));
    }
    if (kind === 'number') {
      field.min = node.minimum ?? node.exclusiveMinimum;
      field.max = node.maximum ?? node.exclusiveMaximum;
      field.integer = node.type === 'integer';
    }
    if (kind === 'text' && node?.pattern) field.pattern = node.pattern;
    fields.push(field);
  }
  const orderOf = (list) => (field) => {
    const idx = list.indexOf(field.path);
    return idx < 0 ? list.length : idx;
  };
  const bySchemaOrder = (a, b) => fields.indexOf(a) - fields.indexOf(b);
  const common = fields.filter((f) => COMMON_PATHS.includes(f.path)).sort((a, b) => orderOf(COMMON_PATHS)(a) - orderOf(COMMON_PATHS)(b));
  const advanced = fields
    .filter((f) => !COMMON_PATHS.includes(f.path))
    .sort((a, b) => (orderOf(ADVANCED_PATHS)(a) - orderOf(ADVANCED_PATHS)(b)) || bySchemaOrder(a, b));
  return { common, advanced, all: [...common, ...advanced] };
}

/**
 * 表单值（path → 原始输入值）→ POST /policy 的策略体：按字段类型收敛
 * （数字字符串→Number、布尔→Boolean、fixed 恒为常量），点路径还原为嵌套
 * 对象（self_check.x → self_check: { x }）。非法数字抛错（调用方中文化提示）。
 */
export function buildPolicyPayload(fields, values = {}) {
  const policy = {};
  for (const field of fields || []) {
    const raw = values[field.path];
    let value;
    if (field.kind === 'fixed') value = field.value;
    else if (field.kind === 'boolean') value = raw === true;
    else if (field.kind === 'number') {
      value = Number(raw);
      if (!Number.isFinite(value)) throw new Error(`「${field.label}」需要填写数字`);
      if (field.integer) value = Math.trunc(value);
    } else {
      value = String(raw ?? '');
    }
    const [head, sub] = String(field.path).split('.');
    if (sub) {
      policy[head] = policy[head] || {};
      policy[head][sub] = value;
    } else {
      policy[head] = value;
    }
  }
  return policy;
}
