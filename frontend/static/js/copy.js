/* T11 全局文案中文化：后端英文枚举 / 原因码 / 字段名 → 自然中文的唯一映射层。
 *
 * 契约 §11：界面任何位置不出现英文字段名（audience、solo_round_limit、
 * task_approved 等）；后端返回的英文枚举/原因码必须映射为自然中文后展示；
 * 异常/错误提示同样中文化。后端 API 与状态机契约不变（§12），映射全部在
 * 前端本模块完成：纯函数、无 DOM/网络依赖，可在 Node 下直接回归。
 *
 * 注意：映射表按后端 v1.7.7 真实输出枚举核对（calibrator/calibration_loop、
 * agent_core/workflow_runner、agent_core/unified_workflow、main_front、
 * skills/style_library、configs/runtime_policy）。后端新增英文输出时，只需
 * 在本模块补一行映射；未识别的英文一律走兜底文案，绝不原样上屏。 */

import { CAPABILITY_ACTIONS } from './states.js';

const CJK_RE = /[㐀-鿿豈-﫿]/;
/** 是否含中文字符（中文内容/键名原样保留，不属于"英文字段名"）。 */
export const hasCJK = (text) => CJK_RE.test(String(text ?? ''));

/* ---------- 工程阶段（snapshot.phase） ---------- */

const PHASE_LABELS = {
  waiting_category_approval: '等待品类约束人工确认',
  category_approved: '品类约束已确认',
  ready_for_category_match: '等待重新匹配品类',
  ready_for_clarification: '等待重新澄清',
  ready_for_taskbook: '等待重新生成任务书',
  ready_for_style_direction: '等待重新准备艺术风格',
  ready_for_quality_inspection: '等待重新质检',
  waiting_clarification: '等待澄清回答',
  waiting_clarification_review: '澄清预算用尽，等待人工复核',
  waiting_taskbook_revision: '任务书需要人工修订',
  waiting_human_approval: '等待人工确认',
  waiting_skill_approval: '等待技能调用人工确认',
  skill_approved_pending_render: '技能调用已放行，等待生成主图',
  waiting_master_selection: '等待选择主图',
  waiting_human_tune: '等待人工微调',
  waiting_reinspection: '等待重新质检',
  additional_rounds_approved: '已确认追加质检轮次',
  terminated_without_delivery: '已终止且不交付',
  calibration_completed: '质检已完成',
  task_approved: '任务书已确认',
  clarification_completed: '澄清已完成',
  master_selected: '主图已选定',
  round_checkpointed: '质检轮次已保存',
  offline_rehearsal_completed: '离线演练已完成',
};

export function phaseLabel(phase) {
  if (!phase) return '—';
  return PHASE_LABELS[phase] || '阶段未知';
}

/* ---------- 服务端能力（capabilities） ---------- */

export function capabilityLabel(id) {
  return CAPABILITY_ACTIONS[id]?.label || '其他动作';
}

/* ---------- 质检终止原因（termination_reason 枚举） ---------- */

const TERMINATION_REASON_LABELS = {
  pass: '质检通过',
  solo_round_limit: '自动质检达到轮次上限',
  fixed_round_limit: '固定自检轮次已完成',
  inspection_blocked: '质检受阻，需要人工处理',
  manual_release_required: '需要人工放行',
  human_ended_without_delivery: '人工终止且不交付',
  human_accepted_current_asset: '人工接受了当前图',
  human_tune_in_progress: '人工微调进行中',
  human_abandoned_after_limit: '达到轮次上限后人工放弃',
  human_tune_final_accepted: '人工微调后已确认终稿',
};

export function terminationReasonLabel(reason) {
  if (!reason) return '达到轮次上限';
  return TERMINATION_REASON_LABELS[reason] || '已满足终止条件';
}

/* ---------- 任务卡字段 / 表单字段名 ---------- */

const FIELD_LABELS = {
  audience: '目标受众',
  tone: '语气风格',
  output_spec: '输出规格',
  asset_rules: '资产规则',
  content_boundaries: '内容边界',
  forbidden_items: '禁忌元素',
  deliverable_goal: '交付目标',
  usage_context: '使用场景',
  task_id: '任务标识',
  project_id: '工程标识',
  source_refs: '参考来源',
  ref_id: '参考编号',
  ref_type: '参考类型',
  excerpt: '参考摘要',
  source_hash: '来源校验值',
  category_ref: '交付类别',
  category_id: '类别标识',
  version: '版本',
  known_facts: '已知事实',
  unknowns: '未知项',
  asset_inputs: '资产输入',
  status: '状态',
  self_check: '自检',
  brand: '品牌',
  style: '风格',
  colors: '品牌色',
  color_palette: '色彩规范',
  size: '尺寸规格',
  format: '格式要求',
  channel: '投放渠道',
  campaign: '活动主题',
  topic: '主题',
  subject: '主体',
  style_refs: '风格参考',
  reference_images: '参考图片',
  task_card: '任务卡',
  clarification_answers: '澄清答案',
  edited_markdown: '任务书内容',
  policy: '运行策略',
  actor: '操作人',
  confirmed: '确认标记',
  manual_action: '处置动作',
  selected_id: '主图选择',
  final_approved: '最终确认',
  human_prompt: '微调说明',
  additional_rounds: '追加轮次',
  cost_confirmed: '费用确认',
  action: '动作',
  skill_action: '技能调用处置',
  /* 运行策略字段（T3 设置页 422 校验路径 loc 中文化，契约 §5/§8/§11） */
  max_auto_questions: '自动提问上限',
  clarification_total_budget: '澄清问题总预算',
  question_preference: '提问偏好',
  termination: '终止方式',
  fixed_rounds: '固定自检轮次',
  max_rounds: '最大自检轮次',
  stop_early_on_pass: '通过后提前停止',
  release: '放行方式',
  candidate_concurrency: '候选图并发数',
  default_output_size: '默认出图尺寸',
  watermark: '水印',
  offline_mode: '离线模式',
  model_timeout_seconds: '模型超时时间',
  image_api_base_url: '图像接口地址',
  response_format: '图片返回格式',
  max_render_retries: '渲染重试次数',
  allow_skill_degradation: '允许技能降级',
  style_library_root: '风格库路径',
  stream_model_output: '流式输出',
};

/**
 * 字段名 → 中文名。中文字段名（用户自定义键）原样保留；
 * 未收录的英文字段名返回兜底文案，不原样上屏（§11）。
 */
export function fieldLabel(key, fallback = '其他信息') {
  if (!key) return fallback;
  const text = String(key);
  if (FIELD_LABELS[text]) return FIELD_LABELS[text];
  return hasCJK(text) ? text : fallback;
}

/* ---------- 运行策略（runtime_policy 键值，契约 §8 中文化基线） ---------- */

const POLICY_KEY_LABELS = {
  max_auto_questions: '自动提问上限',
  clarification_total_budget: '澄清问题总预算',
  question_preference: '提问偏好',
  'self_check.termination': '自检终止方式',
  'self_check.fixed_rounds': '固定自检轮次',
  'self_check.max_rounds': '最大自检轮次',
  'self_check.stop_early_on_pass': '通过后提前停止',
  'self_check.release': '放行方式',
  'category_constraint.release': '品类约束放行方式',
  'style_direction.release': '艺术风格放行方式',
  'skill_invocation.release': '技能调用放行方式',
  candidate_concurrency: '候选图并发数',
  default_output_size: '默认出图尺寸',
  watermark: '水印',
  offline_mode: '离线模式',
  model_timeout_seconds: '模型超时时间（秒）',
  image_api_base_url: '图像接口地址',
  response_format: '图片返回格式',
  max_render_retries: '渲染重试次数',
  allow_skill_degradation: '允许技能降级',
  style_library_root: '风格库路径',
  stream_model_output: '流式输出',
};

const POLICY_ENUM_LABELS = {
  question_preference: { proactive: '全程积极全面追问', blocking_only: '只问阻断交付的关键问题' },
  'self_check.termination': { fix: '固定轮次', solo: '按质量判定' },
  'self_check.release': { auto: '自动放行', manual: '人工确认放行' },
  'category_constraint.release': { auto: '自动放行', manual: '人工确认后继续', off: '不使用数据库' },
  'style_direction.release': { auto: '自动放行', manual: '人工确认后继续', off: '不使用数据库' },
  'skill_invocation.release': { auto: '后台自动继续', manual: '人工确认后继续' },
  response_format: { url: 'URL 链接', b64_json: 'Base64 数据' },
};

/* T3 设置页表单说明小字（契约 §8 中文化映射基线）：名称不够清楚时附解释。 */
const POLICY_KEY_HELP = {
  max_auto_questions: 'Agent 自动向你追问澄清问题的最多次数',
  clarification_total_budget: '整个澄清阶段允许的问题总量',
  question_preference: '积极追问会主动补全对出图有价值的信息并写入任务书；关键问题模式只在不问就无法交付时提问',
  'self_check.termination': '固定轮次：固定执行指定轮数；按质量判定：达标即停',
  'self_check.fixed_rounds': '终止方式为「固定轮次」时生效',
  'self_check.max_rounds': '自检最多进行的轮数',
  'self_check.stop_early_on_pass': '自检达标即提前结束',
  'self_check.release': '自检通过后自动放行，或需人工确认后放行',
  'category_constraint.release': '控制广告品类库的使用方式；不使用数据库时跳过品类匹配与内容注入，由模型按需求自行提问',
  'style_direction.release': '控制艺术风格库的使用方式；不使用数据库时按任务书直接生成候选图，数量为候选图并发数',
  'skill_invocation.release': '旧工程兼容字段；新工程使用品类约束与艺术风格两个独立开关',
  candidate_concurrency: '同时生成的候选图数量（1–5）',
  default_output_size: '如 1K / 2K / 4K，或具体像素（如 2560x1440）',
  watermark: '生成图是否带水印',
  offline_mode: '不调用真实模型接口，用于演示/调试',
  model_timeout_seconds: '单次模型调用的最长等待时间',
  image_api_base_url: '出图服务的接口地址',
  response_format: '图片以 URL 链接或 Base64 数据返回',
  max_render_retries: '出图失败后的自动重试次数（当前固定为 0）',
  allow_skill_degradation: '技能不可用时是否降级处理',
  style_library_root: '本地风格库所在目录',
  stream_model_output: '当前固定关闭',
};

/** 策略字段路径 → 中文名；未收录的英文键兜底为「其他策略项」，不原样上屏（§11）。 */
export function policyKeyLabel(path) {
  const text = String(path ?? '');
  if (!text) return '其他策略项';
  return POLICY_KEY_LABELS[text] || (hasCJK(text) ? text : '其他策略项');
}

/** 策略字段路径 → 解释性小字（无则空串）。 */
export function policyKeyHelp(path) {
  return POLICY_KEY_HELP[String(path ?? '')] || '';
}

/** 策略枚举值 → 中文选项名；未收录的英文枚举值兜底，不原样上屏（§11）。 */
export function policyOptionLabel(path, value) {
  const text = String(value ?? '');
  const label = POLICY_ENUM_LABELS[String(path ?? '')]?.[text];
  if (label) return label;
  return hasCJK(text) ? text : '未设置';
}

/* ---------- 模型设置（设置页「模型」标签页） ---------- */

const MODEL_STATE_LABELS = {
  intake_clarify: '需求澄清',
  confirmation_build: '任务书生成',
  initial_candidate_generation: '候选图生成',
  self_check_inspection: '质检审查',
  self_check_rework: '质检重绘',
  human_prompt_rework: '人工重绘',
};

/** 工作流阶段 → 中文名；未收录的英文阶段名兜底，不原样上屏（§11）。 */
export function modelStateLabel(state) {
  const text = String(state ?? '');
  return MODEL_STATE_LABELS[text] || (hasCJK(text) ? text : '其他阶段');
}

function policyValueText(key, value) {
  if (typeof value === 'boolean') return value ? '开启' : '关闭';
  const enums = POLICY_ENUM_LABELS[key];
  if (enums) return enums[String(value)] || '未设置';
  if (value === null || value === undefined || value === '') return '未设置';
  return String(value);
}

/**
 * 运行策略 → 展示条目 [{ key, label, valueText }]。
 * 嵌套对象（如 self_check）按点路径拍平一层；键名一律中文化，
 * 未收录的英文键不原样展示（更深层的嵌套值同理不直接 JSON 上屏）。
 */
export function policyEntries(policy) {
  const entries = [];
  for (const [key, value] of Object.entries(policy || {})) {
    if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
      for (const [sub, subValue] of Object.entries(value)) {
        const path = `${key}.${sub}`;
        entries.push({ key: path, label: policyKeyLabel(path), valueText: policyValueText(path, subValue) });
      }
    } else {
      entries.push({ key, label: policyKeyLabel(key), valueText: policyValueText(key, value) });
    }
  }
  return entries;
}

/* ---------- 风格机制（"dimension: value" 前缀） ---------- */

const DIMENSION_LABELS = {
  composition: '构图',
  material: '材质',
  lighting: '光影',
  narrative: '叙事',
  graphic_language: '图形语言',
};

/**
 * 风格机制 → 展示文案。"dimension: value" 的已知英文维度映射为中文前缀；
 * 未知英文维度时，值含中文则保留该中文自由文本，全英文机制不原样上屏，
 * 兜底为「其他机制」（§11）；无维度前缀的中文自由文本原样保留。
 */
export function mechanismLabel(mechanism) {
  const text = String(mechanism || '');
  if (!text) return text;
  const match = /^([A-Za-z][A-Za-z0-9_]*)[:：]\s*(.+)$/.exec(text);
  if (match) {
    const [, dimension, value] = match;
    if (DIMENSION_LABELS[dimension]) return `${DIMENSION_LABELS[dimension]}：${value}`;
    return hasCJK(value) ? value : '其他机制';
  }
  return hasCJK(text) ? text : '其他机制';
}

/* ---------- 候选 / 任务状态标识 ---------- */

/** candidate-3 → 方向 3；其余标识符（artifact id 等）原样保留。 */
export function candidateLabel(id) {
  const match = /^candidate-(\d+)$/.exec(String(id || ''));
  return match ? `方向 ${match[1]}` : String(id || '');
}

const JOB_STATUS_LABELS = {
  submitting: '提交中', queued: '排队中', running: '进行中', cancelling: '取消中',
  succeeded: '已完成', failed: '失败', cancelled: '已取消', interrupted: '已中断',
  stalled: '排队超时', unknown: '未知',
};

export function jobStatusLabel(status) {
  return JOB_STATUS_LABELS[status] || '未知';
}

/* ---------- 错误文案中文化 ---------- */

/* 后端英文错误码 → 中文提示。匹配方式为"包含"（兼容 "CODE:detail" 形态），
 * 按从具体到通用的顺序声明。 */
const ERROR_CODE_MAP = [
  ['QUEUE_START_TIMEOUT', '任务尚未开始便排队超时，可安全重试。'],
  ['PROJECT_FILE_MISSING', '工程数据不完整，请运行工程健康检查并修复后重试。'],
  ['PROJECT_CORRUPT', '工程数据校验失败，请运行工程健康检查并修复后重试。'],
  ['BACKEND_UNAVAILABLE', '后端能力暂不可用，已有进度已保留，请稍后重试。'],
  ['INPUTTEXTSENSITIVECONTENTDETECTED', '输入文案触发模型内容审核，请修改质检建议或任务文案后再执行。'],
  ['HUMAN_TUNE_NOT_ACTIVE', '当前不在人工微调阶段，请刷新后重试。'],
  ['QUALITY_LIMIT_NOT_REACHED', '尚未达到质检轮次上限，暂不能执行该操作。'],
  ['QUALITY_TUNE_NOT_AVAILABLE', '该质检检查点不完整，无法进入人工微调。'],
  ['COST_CONFIRMATION_REQUIRED', '追加质检轮次前需要先确认费用。'],
  ['DELIVERY_NOT_FROZEN', '交付尚未冻结，暂不能重新生成说明。'],
  ['PROJECT_ID_INVALID', '工程标识无效。'],
  ['JOB_ID_INVALID', '任务标识无效。'],
  ['TASK_APPROVAL_REQUIRED', '请先确认任务书。'],
  ['LATEST_ASSET_REINSPECTION_REQUIRED', '最新图像需要先重新质检。'],
  ['QUALITY_GATE_NOT_PASSED', '质检未通过，暂不能进入下一步。'],
  ['UNKNOWN_ERROR_CATEGORY', '出现未分类的错误，请稍后重试。'],
  ['ANNOTATION_OUT_OF_BOUNDS', '圈画范围超出图像边界，请重新圈画。'],
  ['STYLE_REFERENCE_LEAK', '风格参考图不能直接作为交付内容。'],
  ['RESOURCE_SCHEMA_INVALID', '资源格式不符合要求，请联系管理员。'],
  ['RESOURCE_MISSING', '所需资源缺失，请联系管理员检查资源库。'],
  ['RESOURCE_CORRUPT', '所需资源已损坏，请联系管理员检查资源库。'],
  ['STYLE_LIBRARY_INSUFFICIENT_DISTINCT_STYLES', '风格库中可用的不同风格数量不足，请联系管理员。'],
  ['STYLE_INDEX_COUNT_OR_ID_INVALID', '风格库索引与风格数量不匹配，请联系管理员。'],
  ['STYLE_IMAGE_MISSING_OR_HASH_MISMATCH', '风格图片缺失或校验不一致，请联系管理员。'],
  ['STYLE_IMAGE_DECODE_FAILED', '风格图片无法读取，请联系管理员。'],
  ['STYLE_IMAGE_DUPLICATE', '风格库中存在重复图片，请联系管理员。'],
  ['STYLE_EXTRACTION_RECOVERABLE', '风格提取结果异常，请重试。'],
  ['STYLE_EXTRACTION_MISSING', '风格提取结果缺失，请联系管理员。'],
  ['STYLE_EXTRACTION_INVALID', '风格提取结果无效，请联系管理员。'],
  ['STYLE_EXTRACTION_STALE', '风格提取结果已过期，请联系管理员。'],
  ['STYLE_PATH_TRAVERSAL', '风格库路径不合法，请联系管理员。'],
  ['STYLE_LIBRARY_INVALID', '风格库配置无效，请联系管理员。'],
  ['STYLE_INDEX_INVALID', '风格库索引无效，请联系管理员。'],
  ['INSPECTION_SCHEMA_INVALID_AFTER_REPAIR', '质检结果格式异常，请重试。'],
];

/* 后端英文短语（raise 消息 / pydantic / 校验器）→ 中文提示。 */
const ERROR_PHRASE_MAP = [
  ['actor required', '请先填写操作人身份。'],
  ['illegal transition', '当前状态不允许该操作，请刷新后重试。'],
  ['exactly five distinct styles required', '需要恰好五个不同的风格方向。'],
  ['hard task constraints differ between slots', '各候选方向的硬约束不一致。'],
  ['source_refs must contain at least one reference', '任务卡需要至少一条参考来源。'],
  ['recommended_option_id must match one option_id', '推荐选项与可选项不匹配。'],
  ['delivery assets require artifact://', '交付资产格式不正确。'],
  ['fixed_rounds cannot exceed max_rounds', '固定自检轮次不能超过最大自检轮次。'],
  ['runtime policy must be a mapping', '运行策略必须是键值对结构。'],
  ['openai sdk is required', '服务缺少模型调用组件，请联系管理员。'],
  ['vision language model returned an empty response', '视觉模型未返回内容，请重试。'],
  ['style index does not contain', '风格库中可用的风格数量不足，请联系管理员。'],
  ['category skill', '类别技能不可用，请联系管理员。'],
  ['json object not found', '模型返回内容格式异常，请重试。'],
  ['inspection must be an object', '质检结果格式异常，请重试。'],
  ['field required', '缺少必填字段'],
  ['input should be a valid dictionary', '输入应为键值对结构'],
  ['extra inputs are not permitted', '包含不允许的字段'],
  ['input should be', '输入值不符合要求'],
  ['value error', '输入值无效'],
];

const ERROR_PATTERN_MAP = [
  [/timed?[ _-]?out/i, '模型调用超时，请稍后重试。'],
  [/rate[ _-]?limit|\b429\b/i, '模型服务繁忙（触发限流），请稍后重试。'],
  [/\b401\b|\b403\b|unauthorized|forbidden|invalid api key/i, '模型服务鉴权失败，请联系管理员检查密钥配置。'],
  [/econn|connection|connect|network|socket|dns/i, '无法连接模型服务，请稍后重试。'],
  [/content[ _-]?filter|moderation|safety/i, '内容未通过审核，请调整后重试。'],
  [/\b5\d{2}\b|unavailable|overloaded/i, '服务暂不可用；当前操作的已提交结果请刷新后核对。'],
];

const ERROR_FALLBACK = '服务暂时无法完成该操作，请稍后重试。';

/**
 * 任意后端错误文本/错误码 → 中文提示（§11：异常/错误提示中文化）。
 * 已含中文的消息原样保留；可识别的英文错误码/短语/常见模式映射为中文；
 * 无法识别的英文不原样上屏，返回通用兜底（原始错误仍可从历史事件追溯）。
 */
export function errorText(raw) {
  const text = String(raw ?? '').trim();
  if (!text) return ERROR_FALLBACK;
  if (hasCJK(text)) return text;
  const upper = text.toUpperCase();
  for (const [code, label] of ERROR_CODE_MAP) if (upper.includes(code)) return label;
  const lower = text.toLowerCase();
  for (const [phrase, label] of ERROR_PHRASE_MAP) if (lower.includes(phrase)) return label;
  for (const [pattern, label] of ERROR_PATTERN_MAP) if (pattern.test(text)) return label;
  return ERROR_FALLBACK;
}

/** 表单校验消息（pydantic msg 等）→ 中文；兜底为字段级提示而非服务级。 */
export function validationText(raw) {
  const text = String(raw ?? '').trim();
  if (!text) return '输入不符合要求';
  if (hasCJK(text)) return text;
  const translated = errorText(text);
  return translated === ERROR_FALLBACK ? '输入不符合要求' : translated;
}

/**
 * 任务书 Markdown 预览的中文化：后端 confirmation_builder 以任务卡字段名
 * 作为事实标签（如 "- audience：内部审核"），英文字段标签一律中文化——
 * 已收录的映射为中文名，未收录的经 fieldLabel 兜底为「其他信息」，不原样
 * 上屏（§11）；中文自由文本标签不匹配本正则，原样保留。编辑区仍持有原始
 * Markdown，回写后端的数据不受影响。
 * （任务书阶段的整体重构属 T4，本函数只保证预览不出现英文字段名。）
 */
export function localizeFactLabelsInMarkdown(markdown) {
  return String(markdown ?? '').replace(
    /^(-\s*)([A-Za-z][A-Za-z0-9_]*)(：)/gm,
    (full, prefix, key, colon) => `${prefix}${fieldLabel(key)}${colon}`,
  );
}

/**
 * 任务书工作区展示稿：保留可审阅事实，移除与页面标题和编辑控件重复的模板说明。
 * 原始 Markdown 仍在编辑器中完整保留，提交数据不受影响。
 */
export function taskbookDisplayMarkdown(markdown) {
  return localizeFactLabelsInMarkdown(markdown)
    .replace(/^#\s+创作任务书\s*/u, '')
    .replace(/^>\s*本任务书汇总原始需求、澄清结果与交付约束。请在确认前逐项核对；保存后的文本将作为后续创作依据。\s*/mu, '')
    // 只移除系统生成过的两种完整说明块。不能从「修改方式」标题截到文末：
    // 用户可能在模板说明后继续追加任意 Markdown，预览必须与保存内容一致。
    .replace(/(^|\n)##[ \t]+修改方式[ \t]*\r?\n(?:[ \t]*\r?\n)*可直接编辑以上条目；保存后会生成新的结构化版本。[ \t]*(?=\r?\n|$)/gu, '$1')
    .replace(/(^|\n)##[ \t]+修改方式[ \t]*\r?\n(?:[ \t]*\r?\n)*可直接编辑任意段落，或用自然语言说明需要变更的内容。[ \t]*(?=\r?\n|$)/gu, '$1')
    .trim();
}
