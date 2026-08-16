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
    """Convert pre-existing unknowns into strategy-aware handling records."""

    records: list[UnknownHandling] = []
    for field, value in task.unknowns.items():
        strategy = value.get("handling_strategy") if isinstance(value, dict) else None
        if strategy == "safe_default" and isinstance(value, dict):
            default = str(value.get("default_value") or "").strip()
            if default:
                records.append(
                    UnknownHandling(
                        field=field,
                        handling=f"采用明确默认值作为执行基线：{default}（任务书确认前仍可修改）。",
                        risk_level=RiskLevel.LOW,
                    )
                )
                continue
        if strategy == "out_of_scope" and isinstance(value, dict):
            records.append(
                UnknownHandling(
                    field=field,
                    handling=str(value.get("scope_note") or "本轮交付不包含该项，按范围边界处理，不进入生成假设。"),
                    risk_level=RiskLevel.LOW,
                )
            )
            continue
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
    allow_fallback: bool = True,
) -> TaskConfirmationDoc:
    """Build a pending-sign confirmation document from intake artifacts.

    When a reasoning client is supplied, the model can draft the structured
    confirmation document. The result is still validated against the Pydantic
    contract and falls back to the deterministic builder if malformed.
    """

    if client is not None:
        try:
            return _build_with_client(task, question_card, answer_record, client,
                                      stream_handler=stream_handler,
                                      require_markdown=not allow_fallback)
        except (KeyError, TypeError, ValueError):
            if not allow_fallback:
                raise

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
    require_markdown: bool = False,
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
    if require_markdown and not doc.markdown_body.strip():
        raise ValueError("任务书模型没有返回其综合撰写的 markdown_body。")
    if not doc.markdown_body:
        doc.markdown_body = confirmation_doc_to_markdown(doc)
    return doc


def _unknown_strategy_lines(task: ImageTaskCard) -> list[str]:
    """Deterministic per-field handling instructions injected into the prompt."""

    lines: list[str] = []
    for field, value in task.unknowns.items():
        if not isinstance(value, dict):
            continue
        label = str(value.get("label") or value.get("question") or field)
        strategy = value.get("handling_strategy")
        if strategy == "safe_default" and str(value.get("default_value") or "").strip():
            lines.append(
                f"- {label}：采用明确默认值“{str(value['default_value']).strip()}”作为执行基线写入任务书，标注为默认处理；不得写成待决事项。"
            )
        elif strategy == "out_of_scope":
            lines.append(
                f"- {label}：本轮交付范围不包含该项，任务书写为范围边界（例如“本轮不包含{label}”）；不得写成待决事项，不进入生成假设。"
            )
        elif bool(value.get("blocking")) and not bool(value.get("has_safe_default")):
            lines.append(f"- {label}：阻塞项，保持人工处理，不得自行补全。")
    return lines


def _build_confirmation_prompt(
    task: ImageTaskCard,
    question_card: QuestionCard,
    answer_record: QuestionAnswerRecord,
) -> str:
    """Build a generic JSON-only prompt for confirmation document drafting."""

    strategy_lines = _unknown_strategy_lines(task)
    strategy_block = (
        "未知项处理策略（必须逐项遵守）：\n" + "\n".join(strategy_lines) + "\n"
        if strategy_lines
        else ""
    )
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
        '"human_annotations":["string"],'
        '"markdown_body":"由你综合理解后撰写的完整中文创作任务书，不得逐字段照抄"'
        "}\n"
        "规则：sign_status 保持省略或 pending_sign；confirmed_facts 必须能追溯到 source_ref；"
        "用户跳过的问题必须采用保守的未确认处理；summary、handling、human_annotations、markdown_body 必须使用中文；"
        "markdown_body 必须综合目标、受众、核心内容、视觉方向、交付规格、硬约束和禁止项重新撰写，不得把输入字段逐项拼接；"
        "markdown_body 与 default_handling_for_unknowns 中严禁出现“待确认、待补充、未提供、需后续、尚未明确”等未闭环表述；"
        "非阻塞未知项只能按下方策略写为执行基线或本轮范围边界，不得写成待决事项；"
        "不得把任何非阻塞未知项的 risk_level 标为 blocking。\n"
        f"{strategy_block}"
        f"任务卡 JSON: {json.dumps(task.model_dump(mode='json'), ensure_ascii=False)}\n"
        f"问题卡 JSON: {json.dumps(question_card.model_dump(mode='json'), ensure_ascii=False)}\n"
        f"回答记录 JSON: {json.dumps(answer_record.model_dump(mode='json'), ensure_ascii=False)}"
    )


def revise_confirmation_markdown(
    task: ImageTaskCard,
    markdown: str,
    client: TextModelClient,
) -> str:
    """One directed repair pass rewriting open-ended task-book prose."""

    strategy_lines = _unknown_strategy_lines(task)
    strategy_block = (
        "未知项处理策略（必须逐项遵守）：\n" + "\n".join(strategy_lines) + "\n"
        if strategy_lines
        else ""
    )
    prompt = (
        "下面的创作任务书正文含有未闭环的开放性表述，请重写为一份完整、可立即执行的中文创作任务书。\n"
        "要求：\n"
        "- 严禁出现“待确认、待补充、未提供、需后续、尚未明确”等未闭环表述；\n"
        "- 非阻塞未知项按给定策略写成明确执行基线（给出采用的默认值）或本轮范围边界（写明“本轮不包含……”）；\n"
        "- 保留原任务书的结构与已确认事实，不得新增任务卡中不存在的事实；\n"
        "- 只输出重写后的 Markdown 正文，不要输出任何解释。\n"
        f"{strategy_block}"
        f"任务卡 JSON: {json.dumps(task.model_dump(mode='json'), ensure_ascii=False)}\n"
        f"原任务书正文：\n{markdown}"
    )
    text = client.complete(prompt).strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    if not text:
        raise ValueError("任务书修复模型没有返回正文。")
    return text


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
    """Create a complete, human-reviewable specification from every user input."""

    source_ref = _source_ref_id(task)
    facts = [
        SpecificationFact(label="deliverable_goal", value=task.deliverable_goal,
                          provenance=source_ref, status="confirmed"),
        SpecificationFact(label="usage_context", value=task.usage_context,
                          provenance=source_ref, status="confirmed"),
    ]
    facts.extend(
        SpecificationFact(label=field, value=_human_value(value),
                          provenance=source_ref, status="extracted")
        for field, value in task.known_facts.items()
        if value not in (None, "", [], {})
    )
    for field, value in task.unknowns.items():
        blocking = bool(isinstance(value, dict) and value.get("blocking") and not value.get("has_safe_default"))
        strategy = value.get("handling_strategy") if isinstance(value, dict) else None
        if not blocking and strategy == "safe_default":
            default = str(value.get("default_value") or "").strip()
            if default:
                facts.append(SpecificationFact(
                    label=field,
                    value=f"采用明确默认值作为执行基线：{default}（确认前仍可修改）",
                    provenance="需求澄清",
                    status="tentative",
                ))
                continue
        if not blocking and strategy == "out_of_scope":
            note = str(value.get("scope_note") or "").strip() or "本轮交付不包含该项，按范围边界处理，不进入生成假设。"
            facts.append(SpecificationFact(
                label=field,
                value=note,
                provenance="需求澄清",
                status="tentative",
            ))
            continue
        facts.append(SpecificationFact(
            label=field,
            value=_human_value(value),
            provenance="需求澄清",
            status="blocking" if blocking else "tentative",
        ))

    excerpts = [ref.excerpt.strip() for ref in task.source_refs if ref.excerpt and ref.excerpt.strip()]
    if excerpts:
        facts.append(SpecificationFact(label="需求来源", value="；".join(dict.fromkeys(excerpts)),
                                       provenance=source_ref, status="extracted"))
    for index, asset in enumerate(task.asset_inputs, 1):
        verified = "已核验" if asset.verified else "待核验"
        facts.append(SpecificationFact(
            label=f"输入素材 {index}",
            value=f"{asset.asset_type}；使用规则：{asset.usage_rule}；{verified}",
            provenance=asset.asset_id,
            status="extracted" if asset.verified else "tentative",
        ))
    return TaskSpecification(task_id=task.task_id, facts=facts).finalized()


_FIELD_LABELS = {
    "deliverable_goal": "交付目标",
    "usage_context": "使用场景",
    "audience": "目标受众",
    "tone": "语气与风格",
    "output_spec": "输出规格",
    "asset_rules": "素材使用规则",
    "content_boundaries": "内容边界",
    "forbidden_items": "禁止元素",
    "brand": "品牌",
    "style": "视觉风格",
    "colors": "色彩要求",
    "color_palette": "色彩规范",
    "size": "尺寸规格",
    "format": "文件格式",
    "channel": "投放渠道",
    "campaign": "活动主题",
    "topic": "主题",
    "subject": "画面主体",
    "style_refs": "风格参考",
    "reference_images": "参考图片",
}
_DISPLAY_TO_FIELD = {value: key for key, value in _FIELD_LABELS.items()}


def _human_value(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, list):
        return "；".join(_human_value(item) for item in value) or "未提供"
    if isinstance(value, dict):
        preferred = value.get("value") or value.get("answer") or value.get("evidence")
        if preferred not in (None, ""):
            return _human_value(preferred)
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _display_label(label: str) -> str:
    return _FIELD_LABELS.get(label, label)


def specification_value(spec: TaskSpecification, label: str, fallback: str = "") -> str:
    """Read a canonical fact while accepting its localized display label."""

    display = _display_label(label)
    fact = next((item for item in spec.facts if item.label in {label, display}), None)
    return fact.value if fact and fact.value else fallback


def specification_to_markdown(spec: TaskSpecification) -> str:
    if spec.source_markdown:
        return spec.source_markdown

    used: set[str] = set()
    sections = ["# 创作任务书"]

    def add_section(heading: str, labels: set[str], *, statuses: set[str] | None = None) -> None:
        items = [
            fact for fact in spec.facts
            if fact.label not in used and fact.label in labels and (statuses is None or fact.status in statuses)
        ]
        if not items:
            return
        sections.extend([f"## {heading}", *[f"- {_display_label(fact.label)}：{fact.value}" for fact in items]])
        used.update(fact.label for fact in items)

    settled = {"confirmed", "extracted"}
    add_section("任务目标与使用场景", {"deliverable_goal", "usage_context"}, statuses=settled)
    add_section("受众与视觉方向", {"audience", "tone", "style", "colors", "color_palette",
                                  "brand", "campaign", "topic", "subject", "style_refs", "reference_images"},
                statuses=settled)
    add_section("交付规格", {"output_spec", "size", "format", "channel"}, statuses=settled)
    add_section("约束与禁止项", {"asset_rules", "content_boundaries", "forbidden_items"}, statuses=settled)
    add_section("参考资料", {"需求来源"}, statuses=settled)

    confirmed = [fact for fact in spec.facts if fact.label not in used and fact.status in {"confirmed", "extracted"}]
    if confirmed:
        sections.extend(["## 已确认信息", *[f"- {_display_label(fact.label)}：{fact.value}" for fact in confirmed]])
        used.update(fact.label for fact in confirmed)

    tentative = [fact for fact in spec.facts if fact.label not in used and fact.status == "tentative"]
    if tentative:
        sections.extend(["## 暂定处理（请核对）", *[f"- {_display_label(fact.label)}：{fact.value}" for fact in tentative]])
        used.update(fact.label for fact in tentative)

    blocking = [fact for fact in spec.facts if fact.label not in used and fact.status == "blocking"]
    sections.append("## 仍需你决定")
    sections.extend([f"- {_display_label(fact.label)}：{fact.value}" for fact in blocking] or ["- 当前没有阻塞项"])
    sections.extend(["## 修改方式", "可直接编辑以上条目；保存后会生成新的结构化版本。"])
    return "\n\n".join(sections) + "\n"


def update_specification_from_markdown(spec: TaskSpecification, markdown: str) -> TaskSpecification:
    """Parse searchable facts while preserving the user's Markdown byte-for-byte."""
    if not markdown.strip():
        raise ValueError("任务书不能为空。")
    status_by_heading = {
        "任务目标与使用场景": "confirmed",
        "受众与视觉方向": "extracted",
        "交付规格": "extracted",
        "约束与禁止项": "extracted",
        "参考资料": "extracted",
        "已确认": "confirmed",
        "已确认信息": "extracted",
        "根据材料提取": "extracted",
        "暂定处理": "tentative",
        "暂定处理（请核对）": "tentative",
        "仍需你决定": "blocking",
        # 操作说明区之后的追加内容视为人工敲定的结论，而非阻塞待决项。
        "修改方式": "confirmed",
    }
    status = "confirmed"; parsed: list[SpecificationFact] = []
    for raw in markdown.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            status = status_by_heading.get(line[3:].strip(), status); continue
        if not line.startswith("- "): continue
        separator = "：" if "：" in line else ":" if ":" in line else None
        if separator is None: continue
        label, value = line[2:].split(separator, 1)
        if value.strip() == "当前没有阻塞项": continue
        entered_label = label.strip()
        canonical_label = _DISPLAY_TO_FIELD.get(entered_label, entered_label)
        old = next((f for f in spec.facts if f.label in {canonical_label, entered_label}
                    or _display_label(f.label) == entered_label), None)
        parsed.append(SpecificationFact(
            fact_id=old.fact_id if old else new_id("fact"),
            label=old.label if old else canonical_label,
            value=value.strip(),
            provenance=old.provenance if old else "人工编辑",
            status=status,
        ))
    if not parsed:
        parsed = [SpecificationFact(label="任务书正文", value=markdown.strip(),
                                    provenance="人工编辑", status="confirmed")]
    return TaskSpecification(
        task_id=spec.task_id,
        version=spec.version + 1,
        facts=parsed,
        source_markdown=markdown,
        parent_hash=spec.content_hash,
    ).finalized()


def _first_body_paragraph(markdown: str) -> str:
    """Extract a compact summary candidate from edited Markdown."""

    for line in markdown.splitlines():
        text = line.strip()
        if not text or text.startswith("#") or text.startswith("-"):
            continue
        return text[:240]
    return ""
