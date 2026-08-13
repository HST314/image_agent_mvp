/* T11 全局文案中文化验收：英文枚举/原因码/字段名一律映射为自然中文，
 * 未识别的英文不原样上屏（契约 §11）；后端契约不变（§12）。 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  hasCJK, phaseLabel, capabilityLabel, terminationReasonLabel, fieldLabel,
  policyEntries, mechanismLabel, candidateLabel, jobStatusLabel,
  errorText, validationText, localizeFactLabelsInMarkdown, taskbookDisplayMarkdown,
} from '../../frontend/static/js/copy.js';
import { formatError } from '../../frontend/static/js/api.js';

/* ---- phase / capability / termination_reason ---- */

test('phase 全量映射为中文，未知 phase 不泄露英文', () => {
  assert.equal(phaseLabel('waiting_clarification'), '等待澄清回答');
  assert.equal(phaseLabel('waiting_human_approval'), '等待人工确认');
  assert.equal(phaseLabel('waiting_skill_approval'), '等待技能调用人工确认');
  assert.equal(phaseLabel('skill_approved_pending_render'), '技能调用已放行，等待生成主图');
  assert.equal(phaseLabel('terminated_without_delivery'), '已终止且不交付');
  assert.equal(phaseLabel('offline_rehearsal_completed'), '离线演练已完成');
  assert.equal(phaseLabel('some_future_phase'), '阶段未知');
  assert.equal(phaseLabel(''), '—');
  assert.equal(phaseLabel(null), '—');
});

test('capability 映射为动作中文名，未知能力不泄露英文 id', () => {
  assert.equal(capabilityLabel('select_master'), '确认当前主图');
  assert.equal(capabilityLabel('submit_human_tune'), '提交微调');
  assert.equal(capabilityLabel('retry_skill_invocations'), '换一版技能调用结果');
  assert.equal(capabilityLabel('future_capability'), '其他动作');
});

test('termination_reason 全量映射（对齐 calibration_loop/workflow_runner/main_front）', () => {
  const cases = {
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
  for (const [reason, label] of Object.entries(cases)) {
    assert.equal(terminationReasonLabel(reason), label);
  }
  assert.equal(terminationReasonLabel('future_reason'), '已满足终止条件');
  assert.equal(terminationReasonLabel(''), '达到轮次上限');
});

/* ---- 字段名 ---- */

test('字段名映射：已知中文化、中文键原样、未知英文键兜底', () => {
  assert.equal(fieldLabel('audience'), '目标受众');
  assert.equal(fieldLabel('output_spec'), '输出规格');
  assert.equal(fieldLabel('主体'), '主体'); // 用户自定义中文键原样保留
  assert.equal(fieldLabel('brand_color'), '其他信息'); // 未收录英文键不上屏
  assert.equal(fieldLabel('brand_color', '补充信息'), '补充信息');
  assert.equal(fieldLabel(''), '其他信息');
});

/* ---- 运行策略 ---- */

test('运行策略键值中文化：嵌套拍平、枚举与布尔值映射（契约 §8 基线）', () => {
  const entries = policyEntries({
    max_auto_questions: 3,
    self_check: { termination: 'fix', fixed_rounds: 2, max_rounds: 4, stop_early_on_pass: false, release: 'auto' },
    response_format: 'b64_json',
    watermark: true,
    offline_mode: false,
    future_switch: true,
  });
  const byKey = Object.fromEntries(entries.map((e) => [e.key, e]));
  assert.equal(byKey.max_auto_questions.label, '自动提问上限');
  assert.equal(byKey.max_auto_questions.valueText, '3');
  assert.equal(byKey['self_check.termination'].label, '自检终止方式');
  assert.equal(byKey['self_check.termination'].valueText, '固定轮次');
  assert.equal(byKey['self_check.fixed_rounds'].label, '固定自检轮次');
  assert.equal(byKey['self_check.stop_early_on_pass'].valueText, '关闭');
  assert.equal(byKey['self_check.release'].valueText, '自动放行');
  assert.equal(byKey.response_format.valueText, 'Base64 数据');
  assert.equal(byKey.watermark.valueText, '开启');
  assert.equal(byKey.future_switch.label, '其他策略项'); // 未收录英文键不上屏
  assert.equal(byKey.future_switch.valueText, '开启');
  // 契约 §8 常用组/高级组基线键全部有中文名
  const contractKeys = [
    'max_auto_questions', 'clarification_total_budget', 'self_check.termination',
    'self_check.fixed_rounds', 'self_check.max_rounds', 'self_check.stop_early_on_pass',
    'self_check.release', 'candidate_concurrency', 'default_output_size', 'watermark',
    'offline_mode', 'model_timeout_seconds', 'image_api_base_url', 'response_format',
    'max_render_retries', 'allow_skill_degradation', 'style_library_root', 'stream_model_output',
  ];
  const full = Object.fromEntries(policyEntries({
    max_auto_questions: 1, clarification_total_budget: 1, candidate_concurrency: 1,
    default_output_size: '2K', watermark: false, offline_mode: false, model_timeout_seconds: 60,
    image_api_base_url: 'https://example', response_format: 'url', max_render_retries: 0,
    allow_skill_degradation: true, style_library_root: '/x', stream_model_output: false,
    self_check: { termination: 'solo', fixed_rounds: 1, max_rounds: 1, stop_early_on_pass: true, release: 'manual' },
  }).map((e) => [e.key, e.label]));
  for (const key of contractKeys) assert.ok(hasCJK(full[key] || ''), `契约键缺中文名：${key}`);
});

/* ---- 风格机制 / 候选 / 任务状态 ---- */

test('mechanism 维度前缀中文化，中文内容原样保留', () => {
  assert.equal(mechanismLabel('composition: 非对称网格'), '构图：非对称网格');
  assert.equal(mechanismLabel('graphic_language: 扁平插画'), '图形语言：扁平插画');
  assert.equal(mechanismLabel('lighting: 顶光'), '光影：顶光');
  assert.equal(mechanismLabel('非对称网格'), '非对称网格'); // 无前缀中文内容原样
  assert.equal(mechanismLabel(''), '');
});

test('mechanism 负向：未知英文维度 / 全英文机制不原样上屏', () => {
  assert.equal(mechanismLabel('camera: close-up'), '其他机制'); // 未知维度 + 全英文值
  assert.equal(mechanismLabel('lens_effect: bokeh blur'), '其他机制');
  assert.equal(mechanismLabel('close-up portrait'), '其他机制'); // 无前缀全英文
  assert.equal(mechanismLabel('camera: 近景特写'), '近景特写'); // 未知维度 + 中文值，保留中文自由文本
  assert.equal(mechanismLabel('视角：俯视'), '视角：俯视'); // 中文维度前缀不匹配英文正则，整体原样
});

test('candidate-N 映射为方向 N，其他标识符保留', () => {
  assert.equal(candidateLabel('candidate-1'), '方向 1');
  assert.equal(candidateLabel('artifact_ab12'), 'artifact_ab12');
});

test('job 状态映射，未知状态不泄露英文', () => {
  assert.equal(jobStatusLabel('running'), '进行中');
  assert.equal(jobStatusLabel('unknown'), '未知');
  assert.equal(jobStatusLabel('some_future_status'), '未知');
});

/* ---- 错误文案 ---- */

test('错误码映射（含 CODE:detail 形态），对齐后端真实 raise', () => {
  assert.equal(errorText('HUMAN_TUNE_NOT_ACTIVE'), '当前不在人工微调阶段，请刷新后重试。');
  assert.equal(errorText('QUALITY_LIMIT_NOT_REACHED'), '尚未达到质检轮次上限，暂不能执行该操作。');
  assert.equal(errorText('COST_CONFIRMATION_REQUIRED'), '追加质检轮次前需要先确认费用。');
  assert.equal(errorText('DELIVERY_NOT_FROZEN'), '交付尚未冻结，暂不能重新生成说明。');
  assert.equal(errorText('ANNOTATION_OUT_OF_BOUNDS'), '圈画范围超出图像边界，请重新圈画。');
  assert.equal(errorText('STYLE_REFERENCE_LEAK:reference_images'), '风格参考图不能直接作为交付内容。');
  assert.equal(errorText('RESOURCE_MISSING'), '所需资源缺失，请联系管理员检查资源库。');
  assert.equal(errorText('TASK_APPROVAL_REQUIRED'), '请先确认任务书。');
});

test('英文 raise 短语映射（pydantic / 校验器 / 状态机门禁）', () => {
  assert.equal(errorText('Value error, fixed_rounds cannot exceed max_rounds'), '固定自检轮次不能超过最大自检轮次。');
  assert.equal(errorText('runtime policy must be a mapping'), '运行策略必须是键值对结构。');
  assert.equal(errorText('actor required'), '请先填写操作人身份。');
  assert.equal(errorText('illegal transition: intake_clarify->final_approval'), '当前状态不允许该操作，请刷新后重试。');
  assert.equal(errorText('exactly five distinct styles required'), '需要恰好五个不同的风格方向。');
});

test('SDK/网络类英文异常按模式归类，原文不上屏', () => {
  assert.equal(errorText('Request timed out.'), '模型调用超时，请稍后重试。');
  assert.equal(errorText('Connection error.'), '无法连接模型服务，请稍后重试。');
  assert.equal(errorText('Error code: 429 - rate limit reached'), '模型服务繁忙（触发限流），请稍后重试。');
  assert.equal(errorText('HTTP 401 Unauthorized'), '模型服务鉴权失败，请联系管理员检查密钥配置。');
  assert.equal(errorText('503 Service Unavailable'), '模型服务暂不可用，请稍后重试。');
});

test('中文消息原样保留；未识别英文走通用兜底', () => {
  assert.equal(errorText('最终交付必须经过人工确认。'), '最终交付必须经过人工确认。');
  assert.equal(errorText('内容未通过审核'), '内容未通过审核');
  assert.equal(errorText('some completely novel english error'), '服务暂时无法完成该操作，请稍后重试。');
  assert.equal(errorText(''), '服务暂时无法完成该操作，请稍后重试。');
  assert.equal(errorText(null), '服务暂时无法完成该操作，请稍后重试。');
});

test('validationText：pydantic 消息中文化，兜底为字段级提示', () => {
  assert.equal(validationText('Field required'), '缺少必填字段');
  assert.equal(validationText('Input should be a valid dictionary'), '输入应为键值对结构');
  assert.equal(validationText("Input should be 'execute', 'edit_and_execute'"), '输入值不符合要求');
  assert.equal(validationText('任务书不能为空'), '任务书不能为空');
});

/* ---- api.formatError（HTTP 错误唯一出口） ---- */

test('formatError：字符串 detail 中文化', () => {
  assert.equal(formatError('HUMAN_TUNE_NOT_ACTIVE'), '当前不在人工微调阶段，请刷新后重试。');
  assert.equal(formatError('已保存。'), '已保存。');
});

test('formatError：422 数组 detail 的字段路径与消息均中文化', () => {
  const detail = [
    { loc: ['body', 'task_card', 'known_facts', 'audience'], msg: 'Field required' },
    { loc: ['body', 'policy'], msg: 'Input should be a valid dictionary' },
  ];
  assert.equal(formatError(detail), '任务卡 · 已知事实 · 目标受众：缺少必填字段；运行策略：输入应为键值对结构');
});

test('formatError：对象 detail 的英文 code 不直接上屏', () => {
  assert.equal(formatError({ code: 'RESOURCE_CORRUPT' }), '所需资源已损坏，请联系管理员检查资源库。');
  assert.equal(formatError({ message: 'Connection error.' }), '无法连接模型服务，请稍后重试。');
});

/* ---- 任务书预览字段标签 ---- */

test('任务书 markdown 预览：已知字段标签中文化，中文内容不动', () => {
  const md = '# 创作任务书\n\n## 根据材料提取\n\n- audience：内部审核人员\n- tone：清晰、精致\n- 主体：产品\n';
  const out = localizeFactLabelsInMarkdown(md);
  assert.ok(out.includes('- 目标受众：内部审核人员'));
  assert.ok(out.includes('- 语气风格：清晰、精致'));
  assert.ok(out.includes('- 主体：产品')); // 中文标签不动
  assert.ok(out.includes('## 根据材料提取'));
});

test('任务书 markdown 预览负向：未收录英文字段标签兜底中文，不原样上屏', () => {
  const md = '- future_field：x\n- market_segment：一二线城市\n';
  const out = localizeFactLabelsInMarkdown(md);
  assert.ok(out.includes('- 其他信息：x')); // 未收录英文标签 → 中文兜底
  assert.ok(out.includes('- 其他信息：一二线城市')); // 英文标签即使值是中文也兜底
  assert.ok(!/[A-Za-z_]+：/.test(out)); // 预览不出现英文字段名（§11）
});

test('任务书工作区展示稿移除重复标题与模板编辑说明，原始事实保留并中文化', () => {
  const md = '# 创作任务书\n\n> 本任务书汇总原始需求、澄清结果与交付约束。请在确认前逐项核对；保存后的文本将作为后续创作依据。\n\n## 根据材料提取\n\n- audience：内部审核人员\n\n## 修改方式\n\n可直接编辑以上条目；保存后会生成新的结构化版本。\n';
  const out = taskbookDisplayMarkdown(md);
  assert.ok(out.startsWith('## 根据材料提取'));
  assert.ok(out.includes('- 目标受众：内部审核人员'));
  assert.ok(!out.includes('# 创作任务书'));
  assert.ok(!out.includes('本任务书汇总原始需求'));
  assert.ok(!out.includes('修改方式'));
});

test('任务书展示稿仅移除已知模板说明块，保留其后的全部用户正文', () => {
  const md = [
    '# 创作任务书',
    '',
    '## 已确认信息',
    '',
    '- audience：内部审核人员',
    '',
    '## 修改方式',
    '',
    '可直接编辑以上条目；保存后会生成新的结构化版本。',
    '',
    '浏览器持久化验收-01cd0d91',
    '',
    '## 用户追加章节',
    '',
    '这里的任意正文、标题与列表都必须保留。',
  ].join('\n');

  const out = taskbookDisplayMarkdown(md);

  assert.ok(!out.includes('可直接编辑以上条目；保存后会生成新的结构化版本。'));
  assert.ok(out.includes('浏览器持久化验收-01cd0d91'));
  assert.ok(out.includes('## 用户追加章节'));
  assert.ok(out.includes('这里的任意正文、标题与列表都必须保留。'));
});

test('任务书展示稿不删除用户自定义的修改方式章节', () => {
  const md = '## 修改方式\n\n这是用户自己的正文，不是系统模板说明。';
  assert.equal(taskbookDisplayMarkdown(md), md);
});
