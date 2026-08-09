"""Build task confirmation documents with optional reasoning LLM support."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from agent_core.models import (
    ConfirmedFact,
    ImageTaskCard,
    QuestionAnswerRecord,
    QuestionCard,
    RiskLevel,
    TaskConfirmationDoc,
    UnknownHandling,
    TaskSpecification,
    SpecificationFact,
    new_id,
)
from model_router.clients import TextModelClient


def _source_ref_id(task: ImageTaskCard) -> str:
    """Return the primary source reference id for generated facts."""

    return task.source_refs[0].ref_id


def _risk_for_answer(selected_option_id: str | None) -> RiskLevel:
    """Map a generic answer choice to unresolved-field risk."""

    if selected_option_id is None:
        return RiskLevel.HIGH
    if selected_option_id.endswith("_c"):
        return RiskLevel.BLOCKING
    if selected_option_id.endswith("_b"):
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def _handling_for_answer(selected_option_id: str | None) -> str:
    """Describe how an unresolved field should be handled."""

    if selected_option_id is None:
        return "用户未提供答案；该字段保持未确认，避免在生成中引入依赖该字段的假设。"
    if selected_option_id.upper() == "C" or selected_option_id.endswith("_c"):
        return "暂停下游生成，直到人工补充明确内容。"
    if selected_option_id.upper() == "B" or selected_option_id.endswith("_b"):
        return "采用通用保守默认值，并标记为默认处理而非已确认事实。"
    return "仅使用任务卡或来源资料中已经明确给出的约束。"


def _known_fact_items(task: ImageTaskCard) -> list[ConfirmedFact]:
    """Convert task fields and known facts to locked confirmation facts."""

    source_ref = _source_ref_id(task)
    facts: list[ConfirmedFact] = [
        ConfirmedFact(field="deliverable_goal", value=task.deliverable_goal, source_ref=source_ref),
        ConfirmedFact(field="usage_context", value=task.usage_context, source_ref=source_ref),
    ]
    for field, value in task.known_facts.items():
        facts.append(ConfirmedFact(field=field, value=value, source_ref=source_ref))
    return facts


def _question_handling(question_card: QuestionCard, answer_record: QuestionAnswerRecord) -> list[UnknownHandling]:
    """Convert clarification results into unknown handling records."""

    answer_by_id = {answer.question_id: answer for answer in answer_record.answers}
    handling: list[UnknownHandling] = []
    for question in question_card.questions:
        answer = answer_by_id.get(question.question_id)
        selected = answer.selected_option_id if answer else None
        handling.append(
            UnknownHandling(
                field=question.field,
                handling=_handling_for_answer(selected),
                risk_level=_risk_for_answer(selected),
            )
        )
    return handling


def _unknown_handling(task: ImageTaskCard) -> list[UnknownHandling]:
    """Convert pre-existing unknowns into generic handling records."""

    records: list[UnknownHandling] = []
    for field, value in task.unknowns.items():
        risk_value = value.get("risk_level") if isinstance(value, dict) else None
        try:
            risk_level = RiskLevel(str(risk_value).lower()) if risk_value else RiskLevel.MEDIUM
        except ValueError:
            risk_level = RiskLevel.MEDIUM
        records.append(
            UnknownHandling(
                field=field,
                handling="除非人工回答或来源资料已经明确，否则保持未确认状态。",
                risk_level=risk_level,
            )
        )
    return records


def _forbidden_items(task: ImageTaskCard) -> list[str]:
    """Read generic forbidden items from the task card when supplied."""

    raw: Any = task.known_facts.get("forbidden_items", [])
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if raw:
        return [str(raw)]
    return []


def build_confirmation_doc(
    task: ImageTaskCard,
    question_card: QuestionCard,
    answer_record: QuestionAnswerRecord,
    client: TextModelClient | None = None,
    stream_handler: Callable[[str], None] | None = None,
) -> TaskConfirmationDoc:
    """Build a pending-sign confirmation document from intake artifacts.

    When a reasoning client is supplied, the model can draft the structured
    confirmation document. The result is still validated against the Pydantic
    contract and falls back to the deterministic builder if malformed.
    """

    if client is not None:
        try:
            return _build_with_client(task, question_card, answer_record, client, stream_handler=stream_handler)
        except (KeyError, TypeError, ValueError):
            pass

    unknowns = _unknown_handling(task) + _question_handling(question_card, answer_record)
    doc = TaskConfirmationDoc(
        task_id=task.task_id,
        summary=(
            "创作任务书已根据来源事实和澄清回答生成。"
            "进入下游生成前必须经过人工确认。"
        ),
        confirmed_facts=_known_fact_items(task),
        default_handling_for_unknowns=unknowns,
        forbidden_items=_forbidden_items(task),
    )
    doc.markdown_body = confirmation_doc_to_markdown(doc)
    return doc


def _build_with_client(
    task: ImageTaskCard,
    question_card: QuestionCard,
    answer_record: QuestionAnswerRecord,
    client: TextModelClient,
    stream_handler: Callable[[str], None] | None = None,
) -> TaskConfirmationDoc:
    """Build and validate a confirmation document through a reasoning LLM."""

    prompt = _build_confirmation_prompt(task, question_card, answer_record)
    response_text = (
        client.complete(prompt, stream_handler=stream_handler)
        if stream_handler is not None
        else client.complete(prompt)
    )
    payload = json.loads(_extract_json_object(response_text))
    payload["task_id"] = task.task_id
    doc = TaskConfirmationDoc.model_validate(payload)
    if not doc.markdown_body:
        doc.markdown_body = confirmation_doc_to_markdown(doc)
    return doc


def _build_confirmation_prompt(
    task: ImageTaskCard,
    question_card: QuestionCard,
    answer_record: QuestionAnswerRecord,
) -> str:
    """Build a generic JSON-only prompt for confirmation document drafting."""

    return (
        "你正在为通用图片任务引擎起草 TaskConfirmationDoc。\n"
        "只能使用给定任务卡、澄清问题和用户回答，不得增加任何具体业务品类假设。\n"
        "模型对话输出必须使用中文。只返回一个 JSON 对象，结构如下：\n"
        "{"
        '"task_id":"string",'
        '"summary":"string",'
        '"confirmed_facts":[{"field":"string","value":any,"source_ref":"string","locked":true}],'
        '"default_handling_for_unknowns":[{"field":"string","handling":"string","risk_level":"low|medium|high|blocking"}],'
        '"forbidden_items":["string"],'
        '"human_annotations":["string"]'
        "}\n"
        "规则：sign_status 保持省略或 pending_sign；confirmed_facts 必须能追溯到 source_ref；"
        "用户跳过的问题必须采用保守的未确认处理；summary、handling、human_annotations 必须使用中文。\n"
        f"任务卡 JSON: {json.dumps(task.model_dump(mode='json'), ensure_ascii=False)}\n"
        f"问题卡 JSON: {json.dumps(question_card.model_dump(mode='json'), ensure_ascii=False)}\n"
        f"回答记录 JSON: {json.dumps(answer_record.model_dump(mode='json'), ensure_ascii=False)}"
    )


def _extract_json_object(text: str) -> str:
    """Extract the first JSON object from a model response."""

    stripped = text.strip()
    if stripped.startswith("```"):
        lines = [line for line in stripped.splitlines() if not line.strip().startswith("```")]
        stripped = "\n".join(lines).strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型响应中未找到 JSON 对象。")
    return stripped[start : end + 1]


def confirmation_doc_to_markdown(doc: TaskConfirmationDoc) -> str:
    """Render a confirmation document as editable Markdown."""
    from interaction.presenter import confirmation_markdown
    return confirmation_markdown(doc)


def update_confirmation_doc_from_markdown(doc: TaskConfirmationDoc, markdown: str) -> TaskConfirmationDoc:
    """Legacy document editing is forbidden because it created two fact sources."""
    raise ValueError("旧版确认书不能直接更新；请迁移为 TaskSpecification 后生成结构化新版本。")


def specification_from_task(task: ImageTaskCard) -> TaskSpecification:
    facts = [SpecificationFact(label=field, value=str(value), provenance=_source_ref_id(task), status="extracted") for field, value in task.known_facts.items()]
    facts.extend(SpecificationFact(label=field, value=str(value), provenance="系统安全默认", status="tentative") for field, value in task.unknowns.items())
    return TaskSpecification(task_id=task.task_id, facts=facts).finalized()


def specification_to_markdown(spec: TaskSpecification) -> str:
    groups = {"confirmed": "已确认", "extracted": "根据材料提取", "tentative": "暂定处理", "blocking": "仍需你决定"}
    sections = ["# 创作任务书"]
    for status, heading in groups.items():
        items = [fact for fact in spec.facts if fact.status == status]
        if items:
            sections.extend([f"## {heading}", *[f"- {fact.label}：{fact.value}" for fact in items]])
    if not any(f.status == "blocking" for f in spec.facts): sections.extend(["## 仍需你决定", "- 当前没有阻塞项"])
    sections.extend(["## 修改方式", "可直接编辑以上条目；保存后会生成新的结构化版本。"])
    return "\n\n".join(sections) + "\n"


def update_specification_from_markdown(spec: TaskSpecification, markdown: str) -> TaskSpecification:
    """Parse edited list items and create a new structured fact version."""
    status_by_heading = {"已确认": "confirmed", "根据材料提取": "extracted", "暂定处理": "tentative", "仍需你决定": "blocking"}
    status = "tentative"; parsed: list[SpecificationFact] = []
    for raw in markdown.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            status = status_by_heading.get(line[3:].strip(), status); continue
        if not line.startswith("- ") or "：" not in line: continue
        label, value = line[2:].split("：", 1)
        if value.strip() == "当前没有阻塞项": continue
        old = next((f for f in spec.facts if f.label == label.strip()), None)
        parsed.append(SpecificationFact(fact_id=old.fact_id if old else new_id("fact"), label=label.strip(), value=value.strip(), provenance=old.provenance if old else "人工编辑", status=status))
    if not parsed: raise ValueError("未能从 Markdown 解析出任何任务事实。")
    return TaskSpecification(task_id=spec.task_id, version=spec.version + 1, facts=parsed, parent_hash=spec.content_hash).finalized()


def _first_body_paragraph(markdown: str) -> str:
    """Extract a compact summary candidate from edited Markdown."""

    for line in markdown.splitlines():
        text = line.strip()
        if not text or text.startswith("#") or text.startswith("-"):
            continue
        return text[:240]
    return ""
