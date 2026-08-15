/* T11 新建工程表单：普通界面只收集自然中文信息，领域契约的英文键仅在
 * 提交边界组装，不把原始任务 JSON 暴露给用户。 */

function clean(value) {
  return String(value || '').trim();
}

export function buildNewProjectTask({
  projectId,
  goal,
  usageScene,
  targetGroup = '',
  styleTone = '',
  deliverySpec = '',
}) {
  const project = clean(projectId);
  const deliverableGoal = clean(goal);
  const usageContext = clean(usageScene);
  if (!project || !deliverableGoal || !usageContext) {
    throw new TypeError('工程 ID、创作目标和使用场景均不能为空。');
  }

  const knownFacts = {};
  const audience = clean(targetGroup);
  const tone = clean(styleTone);
  const outputSpec = clean(deliverySpec);
  if (audience) knownFacts.audience = audience;
  if (tone) knownFacts.tone = tone;
  if (outputSpec) knownFacts.output_spec = outputSpec;

  return {
    task_id: `task-${project}`,
    project_id: project,
    source_refs: [{
      ref_id: `brief-${project}`,
      ref_type: 'brief',
      excerpt: deliverableGoal,
      source_hash: null,
    }],
    deliverable_goal: deliverableGoal,
    usage_context: usageContext,
    known_facts: knownFacts,
    unknowns: outputSpec ? {} : { output_spec: '待确认' },
    asset_inputs: [],
    status: 'draft',
  };
}
