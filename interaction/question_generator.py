"""Minimal clarification gate with semantic deduplication and one repair pass."""

from __future__ import annotations
import hashlib
import json
from collections.abc import Callable
from typing import Any, Literal
from agent_core.models import ImageTaskCard, QuestionCard, QuestionItem, QuestionOption
from model_router.clients import TextModelClient
from interaction.presenter import label_for

QuestionMode = Literal["fixed", "auto"]
FIELD_DECISIONS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "output_spec": (
        "主要投放媒介和画面比例是什么？",
        [
            ("竖版手机", "按 9:16 竖版构图"),
            ("横版屏幕", "按 16:9 横版构图"),
            ("方形信息流", "按 1:1 方形构图"),
        ],
    ),
    "asset_rules": (
        "现有素材允许怎样用于成图？",
        [
            ("仅作参考", "学习视觉信息但不直接复刻"),
            ("允许入画", "可将提供素材作为画面元素"),
            ("暂不使用", "本轮不引用现有素材"),
        ],
    ),
    "content_boundaries": (
        "画面内容应遵循哪种边界？",
        [
            ("仅呈现明确内容", "不增加未提供的人物、标识或文字"),
            ("允许中性补充", "可补充不改变含义的环境元素"),
        ],
    ),
}


def _fingerprint(field: str, question: str) -> str:
    normalized = "".join((field + question).casefold().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:20]


def _candidate(
    task: ImageTaskCard, field: str, value: Any, index: int
) -> QuestionItem | None:
    missing = str(value).strip().casefold() in {
        "",
        "unknown",
        "pending",
        "待确认",
    } or isinstance(value, dict)
    details = value if isinstance(value, dict) else {}
    impact = str(details.get("impact", "会改变核心构图、用途或合规边界。"))
    safe = bool(details.get("has_safe_default", field not in FIELD_DECISIONS))
    blocking = bool(details.get("blocking", field in FIELD_DECISIONS))
    if not missing or not impact or safe or not blocking:
        return None
    decision = FIELD_DECISIONS.get(field)
    safe_label = label_for(field)
    if decision is None:
        # Unknown blocking fields must remain visible and recoverable.  Use the
        # supplied structured options; never silently discard the field.
        supplied = details.get("options")
        if not isinstance(supplied, list) or len(supplied) < 2:
            supplied = [
                {
                    "label": "现在补充明确要求",
                    "description": f"提供{safe_label}的可执行内容后继续",
                },
                {
                    "label": "保持阻塞并暂停",
                    "description": "不猜测该信息，保存工程并稍后恢复",
                },
            ]
        supplied_question = details.get("question")
        question = (
            str(supplied_question)
            if supplied_question and field not in str(supplied_question)
            else f"请确认{safe_label}。"
        )
        choices = [
            (str(x["label"]), str(x.get("description", x["label"]))) for x in supplied
        ]
    else:
        question, choices = decision
    fingerprint_field = field if field in FIELD_DECISIONS else safe_label
    fp = str(
        details.get("semantic_fingerprint") or _fingerprint(fingerprint_field, question)
    )
    return QuestionItem(
        question_id=f"{task.task_id}_q{index}",
        field=field,
        question=question,
        options=[
            QuestionOption(option_id=chr(65 + i), label=label, description=desc)
            for i, (label, desc) in enumerate(choices)
        ],
        recommended_option_id="A",
        impact=impact,
        evidence=str(details.get("evidence", "来源材料未明确说明。")),
        missing=True,
        has_safe_default=False,
        blocking=True,
        semantic_fingerprint=fp,
    )


def select_clarification_fields(task: ImageTaskCard, count: int = 3) -> list[str]:
    return [
        field
        for field, value in task.unknowns.items()
        if _candidate(task, field, value, 1) is not None
    ][: min(3, count)]


def generate_question_card(
    task: ImageTaskCard,
    client: TextModelClient | None = None,
    *,
    question_count: int | None = 3,
    mode: QuestionMode = "auto",
    max_auto_questions: int = 3,
    previous_context: str | None = None,
    stream_handler: Callable[[str], None] | None = None,
    previous_fingerprints: set[str] | None = None,
    total_budget: int = 10,
    already_asked: int = 0,
    error_recorder: Callable[[dict[str, Any]], None] | None = None,
) -> QuestionCard:
    per_round = min(
        3, max(0, max_auto_questions if mode == "auto" else int(question_count or 0))
    )
    remaining = max(0, total_budget - already_asked)
    seen = set(previous_fingerprints or ())
    if previous_context:
        seen.update(token for token in previous_context.split() if len(token) == 20)
    if client is not None:
        prompt = _prompt(task, min(per_round, remaining), seen)
        raw = client.complete(prompt)
        for attempt in range(2):
            try:
                payload = json.loads(_extract(raw))
                items = payload.get("questions", [])
                questions = [
                    _normalize(task, item, i) for i, item in enumerate(items, 1)
                ]
                eligible = _eligible_unique(questions, seen)
                return QuestionCard(
                    task_id=task.task_id,
                    questions=eligible[: min(per_round, remaining)],
                )
            except Exception as exc:
                if error_recorder:
                    error_recorder(
                        {
                            "type": "clarification_parse_failed",
                            "attempt": attempt + 1,
                            "error": str(exc),
                        }
                    )
                if attempt == 0:
                    raw = client.complete(
                        "请仅修复下列输出为符合原契约的 JSON，不新增问题：\n" + raw
                    )
                    continue
                raise ValueError(
                    "澄清模型输出连续两次无法解析；错误已记录，可使用 retry 恢复。"
                ) from exc
    deterministic = [
        _candidate(task, f, v, i) for i, (f, v) in enumerate(task.unknowns.items(), 1)
    ]
    eligible = _eligible_unique([q for q in deterministic if q], seen)
    return QuestionCard(
        task_id=task.task_id, questions=eligible[: min(per_round, remaining)]
    )


def _passes(item: QuestionItem, seen: set[str]) -> bool:
    return (
        item.missing
        and bool(item.impact)
        and not item.has_safe_default
        and item.blocking
        and item.semantic_fingerprint not in seen
    )


def _eligible_unique(items: list[QuestionItem], seen: set[str]) -> list[QuestionItem]:
    accepted: list[QuestionItem] = []
    fingerprints = set(seen)
    for item in items:
        if _passes(item, fingerprints):
            accepted.append(item)
            fingerprints.add(item.semantic_fingerprint)
    return accepted


def _normalize(task: ImageTaskCard, item: dict[str, Any], index: int) -> QuestionItem:
    item = dict(item)

    # 1. 规范化 options 列表及其内部字段
    options_list = item.get("options")
    norm_options = []
    if isinstance(options_list, list):
        for i, opt in enumerate(options_list):
            if isinstance(opt, dict):
                o = dict(opt)
                # 兼容 id / option_id，并强制转为字符串 (如 1 -> "1", 或自动转 A, B)
                raw_id = (
                    o.get("option_id")
                    if o.get("option_id") is not None
                    else o.get("id")
                )
                opt_id = str(raw_id) if raw_id is not None else chr(65 + i)
                o["option_id"] = opt_id
                o.pop("id", None)
                o["label"] = str(o.get("label") or f"选项 {opt_id}")
                o["description"] = str(o.get("description") or o["label"])
                norm_options.append(o)
            elif isinstance(opt, (str, int)):
                norm_options.append(
                    {
                        "option_id": chr(65 + i),
                        "label": str(opt),
                        "description": str(opt),
                    }
                )

    # 保底：如果 options 为空，补齐默认选项
    if not norm_options:
        norm_options = [
            {"option_id": "A", "label": "确认并继续", "description": "按默认标准处理"},
            {"option_id": "B", "label": "暂不处理", "description": "保持未确认状态"},
        ]
    item["options"] = norm_options

    # 2. 规范化 recommended_option_id（强制为字符串，且必须在 options 范围里）
    rec_id = item.get("recommended_option_id")
    rec_str = str(rec_id) if rec_id is not None else str(norm_options[0]["option_id"])
    valid_ids = {o["option_id"] for o in norm_options}
    item["recommended_option_id"] = (
        rec_str if rec_str in valid_ids else str(norm_options[0]["option_id"])
    )

    # 3. 规范化布尔值字段：如果大模型把 missing 填成了中文句子，强行转为 bool 值 (True)
    for bool_field, default_val in [
        ("missing", True),
        ("has_safe_default", False),
        ("blocking", True),
    ]:
        val = item.get(bool_field)
        if not isinstance(val, bool):
            item[bool_field] = default_val

    # 4. 规范化基础文本字段
    item["question_id"] = str(item.get("question_id") or f"{task.task_id}_q{index}")
    item["field"] = str(item.get("field") or "unknown_field")
    item["question"] = str(item.get("question") or "请确认该项要求：")
    item["impact"] = str(item.get("impact") or "会影响画面最终效果。")
    item["evidence"] = str(item.get("evidence") or "")

    # 5. 生成指纹
    item["semantic_fingerprint"] = str(
        item.get("semantic_fingerprint")
        or _fingerprint(item["field"], item["question"])
    )

    return QuestionItem.model_validate(item)


def _prompt(task: ImageTaskCard, limit: int, seen: set[str]) -> str:
    return (
        '只输出一个 JSON 对象，结构为 {"questions": [...]}\n'
        "严禁包含任何前言、Markdown 外的解释。"
        "允许 0 问，最多 %d 问。\n"
        "每个问题必须包含以下字段（注意类型）：\n"
        "- field: 字符串，缺失的字段名\n"
        "- question: 字符串，向用户提问的中文问题\n"
        '- options: 列表，每个选项格式为 {"option_id": "A", "label": "标题", "description": "说明"}，option_id 必须是字母字符串如 "A", "B"\n'
        '- recommended_option_id: 字符串，推荐的 option_id，如 "A"\n'
        "- impact: 字符串，影响说明\n"
        "- evidence: 字符串，依据材料\n"
        "- missing: 布尔值(true/false)，是否缺失\n"
        "- has_safe_default: 布尔值(true/false)\n"
        "- blocking: 布尔值(true/false)\n"
        "- semantic_fingerprint: 字符串\n"
        "已问指纹：%s\n任务卡：%s"
    ) % (
        limit,
        sorted(seen),
        json.dumps(task.model_dump(mode="json"), ensure_ascii=False),
    )


def _extract(text: str) -> str:
    text = text.strip()
    # 过滤 Markdown 代码块标记（```json ... ```）
    if text.startswith("```"):
        lines = [
            line for line in text.splitlines() if not line.strip().startswith("```")
        ]
        text = "\n".join(lines).strip()

    start = text.find("{")
    if start < 0:
        raise ValueError("响应中没有 JSON 对象")

    try:
        # 使用 raw_decode：只解析第一个完整的 JSON 对象，自动忽略大模型后面附带的废话/多余字符
        decoder = json.JSONDecoder()
        obj, _ = decoder.raw_decode(text[start:])
        return json.dumps(obj, ensure_ascii=False)
    except Exception as exc:
        raise ValueError(f"无法解析 JSON 对象: {exc}") from exc
