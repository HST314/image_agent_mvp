"""Chinese user views kept separate from internal contracts."""
from __future__ import annotations
import json
from typing import Any
from agent_core.models import QuestionCard, TaskConfirmationDoc, VisualCheckResult

LABELS = {"deliverable_goal": "创作目标", "usage_context": "使用场景", "forbidden_items": "禁止出现", "output_spec": "交付要求", "asset_rules": "素材使用边界", "content_boundaries": "内容边界"}

def label_for(field: str) -> str:
    return LABELS.get(field, "相关要求")

class Presenter:
    def __init__(self, debug: bool = False) -> None:
        self.debug = debug

    def progress(self, state: str, round_number: int | None = None) -> str:
        labels = {"intake_clarify": "正在理解创作需求", "confirmation_build": "正在生成创作任务书", "initial_candidate_generation": "正在生成 5 个候选方向", "master_candidate_selection": "请选择当前主图", "self_check_iteration": "画面质检", "human_prompt_iteration": "正在按你的要求修改", "final_approval": "请确认最终交付"}
        value = labels.get(state, "正在处理")
        return f"第 {round_number} 轮{value}" if round_number else value

    def questions(self, card: QuestionCard) -> str:
        blocks = []
        for index, item in enumerate(card.questions, 1):
            choices = "\n".join(f"  {choice.option_id}. {choice.label} — {choice.description}" for choice in item.options)
            blocks.append(f"{index}. {item.question}\n为什么现在需要决定：{item.impact}\n{choices}\n可直接输入自己的答案。")
        return "\n\n".join(blocks) or "当前信息足够，无需补充。"

    def inspection(self, result: VisualCheckResult) -> str:
        issues = "\n".join(f"- {item.get('evidence', str(item))}" for item in result.issues) or "- 未发现明显问题"
        return f"质检结论：{'可以通过' if result.passed else '建议修改'}\n{issues}\n修改建议：{result.rework_prompt_delta or '无需修改'}\n置信度：{result.confidence:.0%}"

    def candidates(self, assets: list[dict[str, Any]]) -> str:
        rows = ["请选择一张作为当前主图："]
        for index, asset in enumerate(assets, 1):
            rows.append(f"  {index}. 候选方向 {index} — {asset.get('uri', '图片已保存')}")
        return "\n".join(rows)

    def technical(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str) if self.debug else "技术详情仅在 --debug 模式下显示。"

    def history(self, events: list[dict[str, Any]]) -> str:
        labels = {"project_created": "创建工程", "step_started": "开始处理", "step_succeeded": "完成节点", "step_failed": "处理失败", "branch_created": "创建新分支", "retry_started": "重试失败步骤", "inspection_completed": "完成画面质检", "rework_completed": "完成画面返工", "round_checkpointed": "保存本轮进度"}
        state_labels = {"intake_clarify": "理解创作需求", "confirmation_build": "生成创作任务书", "initial_candidate_generation": "生成五个候选方向", "master_candidate_selection": "选择当前主图", "self_check_iteration": "画面质检与返工", "human_prompt_iteration": "人工要求修改", "final_approval": "最终确认"}
        rows = []
        for event in events:
            action = labels.get(str(event.get("type")), "记录进度")
            state = state_labels.get(str(event.get("state")), "")
            branch = f"，分支 {event['branch']}" if event.get("branch") else ""
            rows.append(f"{event.get('timestamp', '')}  {action}{'：' + state if state else ''}{branch}")
        return "\n".join(rows) or "当前还没有历史记录。"

def confirmation_markdown(doc: TaskConfirmationDoc) -> str:
    facts = "\n".join(f"- {label_for(f.field)}：{f.value}" for f in doc.confirmed_facts)
    tentative = "\n".join(f"- 当前暂按{u.handling}处理；如需修改请直接改写。" for u in doc.default_handling_for_unknowns if u.risk_level.value != "blocking") or "- 当前没有暂定项"
    blocking = "\n".join(f"- {label_for(u.field)}：{u.handling}" for u in doc.default_handling_for_unknowns if u.risk_level.value == "blocking") or "- 当前没有阻塞项"
    forbidden = "\n".join(f"- {item}" for item in doc.forbidden_items) or "- 未提供额外禁止项"
    return f"# 创作任务书\n\n> {doc.summary or '请确认本次创作目标和交付要求。'}\n\n## 本次目标\n{facts}\n\n## 画面重点\n- 以已确认的主体、场景和视觉重点为准\n\n## 交付要求\n- 生成 5 个差异化候选方向，并从中选择 1 张继续完善\n\n## 必须遵守\n{forbidden}\n\n## 暂定处理（可直接编辑）\n{tentative}\n\n## 仍需你决定\n{blocking}\n\n## 修改方式\n可直接编辑任意段落，或用自然语言说明需要变更的内容。\n"
