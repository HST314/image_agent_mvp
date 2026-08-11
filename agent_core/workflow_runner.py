"""Production workflow runner and explicit state-handler registry."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent_core.batch import CandidateBatchGenerator
from agent_core.models import ImageTaskCard, ModelRole, TaskSpecification, VisualCheckResult
from agent_core.state_machine import RecoverableWorkflow
from agent_core.workflow import SelfCheckPolicy, validate_transition
from agent_core.unified_workflow import (DomainState, classify_error, freeze_delivery,
                                         recovery_actions, revise_task, TaskRevision)
from calibrator.calibration_loop import CalibrationLoop, ManualAction
from interaction.confirmation_builder import specification_from_task, specification_to_markdown, update_specification_from_markdown
from interaction.question_generator import generate_question_card
from model_router.clients import build_text_client, build_vlm_client
from model_router.gateway import RuntimeModelGateway
from model_router.router import ModelRoute, ModelRouter
from render_clients.ark_client import ArkImageRenderClient
from render_clients.payload_mapper import build_render_payload
from storage.project_store import ProjectStore, content_hash
from storage.assets import normalize_image_asset
from storage.provider_assets import ProviderImageAdapter
from interaction.presenter import Presenter
from configs.runtime_policy import RuntimePolicy
from model_router.executor import ModelExecutor
from skills.resource_loader import load_with_policy
from skills.errors import ResourceError
from calibrator.structured_inspection import parse_with_one_repair, InspectionOutputError
from agent_core.delivery import build_delivery, persist_delivery

Handler = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


@dataclass
class RunnerOptions:
    selected_id: str | None = None
    manual_action: ManualAction | None = None
    human_prompt: str | None = None
    edited_markdown: str | None = None
    final_approved: bool = False
    clarification_answers: dict[str, Any] | None = None
    task_approved: bool = False
    actor: str | None = None


class WorkflowRunner:
    """Run registered real handlers and checkpoint every successful boundary."""

    ORDER = ("intake_clarify", "confirmation_build", "initial_candidate_generation",
             "master_candidate_selection", "self_check_iteration", "human_prompt_iteration", "final_approval")
    DOMAIN_TARGET = {
        "intake_clarify": DomainState.CLARIFICATION,
        "confirmation_build": DomainState.TASK_APPROVAL,
        "initial_candidate_generation": DomainState.FIVE_RENDER,
        "master_candidate_selection": DomainState.MASTER_SELECTION,
        "self_check_iteration": DomainState.QUALITY_REWORK,
        "human_prompt_iteration": DomainState.HUMAN_EDIT,
        "final_approval": DomainState.DELIVERY_FROZEN,
    }

    def __init__(self, store: ProjectStore, config: Path, *, offline_mode: bool = False,
                 output: Callable[[str], None] | None = None) -> None:
        self.store = store
        policy_file = store.root / "runtime_policy.json"
        stored = json.loads(policy_file.read_text(encoding="utf-8")).get("policy", {}) if policy_file.exists() else {}
        self.policy = RuntimePolicy.model_validate(stored) if stored else RuntimePolicy(offline_mode=offline_mode)
        if stored and self.policy.offline_mode != offline_mode:
            raise ValueError("工程真实/离线模式已在创建时固化，运行中不可切换。")
        executor = ModelExecutor(max_attempts=self.policy.max_render_retries + 1,
                                 timeout=self.policy.model_timeout_seconds)
        self.gateway = RuntimeModelGateway(store, ModelRouter.from_file(config), executor, offline_mode=offline_mode)
        self.offline_mode = offline_mode
        self.output = output or (lambda _: None)
        self.presenter = Presenter()
        self.workflow = RecoverableWorkflow(store)
        self.provider_assets = ProviderImageAdapter(store)
        self.handlers: dict[str, Handler] = {
            "intake_clarify": self._clarify, "confirmation_build": self._confirmation,
            "initial_candidate_generation": self._candidates, "master_candidate_selection": self._selection,
            "self_check_iteration": self._self_check, "human_prompt_iteration": self._human_rework,
            "final_approval": self._final,
        }

    def next_state(self, snapshot: dict[str, Any] | None) -> str:
        if snapshot is None or not snapshot.get("state"): return self.ORDER[0]
        phase = snapshot.get("phase")
        if phase in {"waiting_human_approval", "waiting_clarification", "waiting_master_selection"}:
            return str(snapshot.get("state"))
        if phase in {"additional_rounds_approved", "waiting_reinspection"}:
            return "self_check_iteration"
        if phase == "waiting_human_tune":
            return "human_prompt_iteration"
        current = str(snapshot.get("state", ""))
        if current not in self.ORDER: raise ValueError("检查点中的流程状态无法识别。")
        index = self.ORDER.index(current)
        if index + 1 >= len(self.ORDER): raise ValueError("工程已经完成最终确认。")
        return self.ORDER[index + 1]

    def run(self, snapshot: dict[str, Any] | None, options: RunnerOptions, *, only_state: str | None = None) -> dict[str, Any]:
        with self.store.lock():
            return self._run_locked(snapshot, options, only_state=only_state)

    def _run_locked(self, snapshot: dict[str, Any] | None, options: RunnerOptions, *, only_state: str | None = None) -> dict[str, Any]:
        data = dict(snapshot or {}); target = only_state or self.next_state(snapshot)
        if "task_card" in data and "domain_state" not in data:
            data["domain_state"] = DomainState.TASK.value
        while True:
            current = str(data.get("state", ""))
            if current and current != target:
                validate_transition(current, target)
            handler = self.handlers[target]
            self.store.start_step(target, input_hash=content_hash(data))
            try:
                result = handler(data, options.__dict__)
                data = {**data, **result, "state": target}
                if "domain_state" in data:
                    self._advance_domain(data, self.DOMAIN_TARGET[target])
                # Waiting is a successful recoverable boundary, not a failed state.
                self.store.checkpoint(target, data)
            except Exception as exc:
                category = classify_error(exc)
                actions = recovery_actions(category)
                self.store.fail_step(target, {"code": type(exc).__name__, "message": str(exc),
                                               "category": category, "retryable": "retry" in actions,
                                               "recovery_actions": list(actions)})
                raise
            if only_state or data.get("waiting") or target == "final_approval": return data
            target = self.next_state(data)

    @staticmethod
    def _advance_domain(data: dict[str, Any], target: DomainState) -> None:
        """Advance every production handler through the canonical graph."""
        from agent_core.unified_workflow import FLOW, require_transition
        current = DomainState(data["domain_state"])
        if FLOW.index(target) < FLOW.index(current):
            return
        while current != target:
            following = FLOW[FLOW.index(current) + 1]
            require_transition(current, following, data)
            current = following
        data["domain_state"] = current.value

    def _clarify(self, data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        task = ImageTaskCard.model_validate(data["task_card"])
        fingerprints = set(data.get("previous_fingerprints", []))
        already_asked = int(data.get("clarification_asked_count", 0))
        if data.get("phase") == "waiting_clarification":
            answers = options.get("clarification_answers")
            if not answers: return {"waiting": True, "phase": "waiting_clarification"}
            facts = dict(task.known_facts); facts.update(answers)
            task = task.model_copy(update={"known_facts": facts, "unknowns": {k:v for k,v in task.unknowns.items() if k not in answers}})
            return {"task_card": task.model_dump(mode="json"), "clarification_answers": answers, "waiting": False, "phase": "clarification_completed",
                    "previous_fingerprints": sorted(fingerprints), "clarification_asked_count": already_asked,
                    "clarification_remaining_budget": max(0, self.policy.clarification_total_budget - already_asked)}
        if self.offline_mode:
            card = generate_question_card(task, previous_fingerprints=fingerprints, already_asked=already_asked,
                                          total_budget=self.policy.clarification_total_budget,
                                          max_auto_questions=self.policy.max_auto_questions)
        else:
            card = self.gateway.call("intake_clarify", ModelRole.REASONING_LLM,
                lambda route: generate_question_card(task, self._text(route), previous_fingerprints=fingerprints, already_asked=already_asked, error_recorder=lambda e: self.store.events.append(**{"event_type": "model_parse_failed", **e})),
                messages=[{"role":"user","content":"分析任务中真正阻塞的未知项"}], variables={"task":task.model_dump(mode="json")},
                template_id="clarification", template_version="2", input_refs=[r.ref_id for r in task.source_refs])
        if card.questions:
            fingerprints.update(q.semantic_fingerprint for q in card.questions)
            asked = already_asked + len(card.questions)
            return {"question_card": card.model_dump(mode="json"), "waiting": True, "phase": "waiting_clarification",
                    "previous_fingerprints": sorted(fingerprints), "clarification_asked_count": asked,
                    "clarification_remaining_budget": max(0, self.policy.clarification_total_budget - asked)}
        return {"question_card": card.model_dump(mode="json"), "waiting": False,
                "previous_fingerprints": sorted(fingerprints), "clarification_asked_count": already_asked,
                "clarification_remaining_budget": max(0, self.policy.clarification_total_budget - already_asked)}

    def _confirmation(self, data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        spec = TaskSpecification.model_validate(data["task_specification"]) if data.get("task_specification") else specification_from_task(ImageTaskCard.model_validate(data["task_card"]))
        if options.get("edited_markdown"):
            spec = update_specification_from_markdown(spec, options["edited_markdown"])
        markdown = specification_to_markdown(spec)
        history = list(data.get("task_revision_history", []))
        raw_task = json.dumps(data.get("task_card"), ensure_ascii=False, sort_keys=True)
        actor = options.get("actor") or "manual-user"
        revisions = [TaskRevision.model_validate(item) for item in history]
        candidate = revise_task(revisions, raw_task, markdown, actor)
        if history and history[-1]["raw_task"] == raw_task and history[-1]["task_markdown"] == markdown:
            revision = history[-1]
        else:
            revision = candidate.model_dump(mode="json")
            history.append(revision)
            self.store.events.append("task_revision_created", revision=revision)
        revision_hash = revision["revision_hash"]
        approved = bool(options.get("task_approved") and options.get("actor"))
        return {"task_specification": spec.model_dump(mode="json"), "task_markdown": markdown,
                "task_revision": revision,
                "task_revision_history": history,
                "task_approval": ({"revision_hash": revision_hash, "actor": options["actor"]} if approved else None),
                "waiting": not approved, "phase": "task_approved" if approved else "waiting_human_approval"}

    def _candidates(self, data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        spec = TaskSpecification.model_validate(data["task_specification"])
        task_card = ImageTaskCard.model_validate(data["task_card"])
        
        # 1. 匹配广告品类 Skill
        from skills.category_library_adapter import CategoryLibraryAdapter
        lib_path = Path(__file__).parent.parent / "skills/category_libraries/advertising_category_library_v2.json"
        def load_category():
            try:
                match = CategoryLibraryAdapter(lib_path).load_for_task(task_card)
            except (FileNotFoundError, UnicodeError, json.JSONDecodeError) as exc:
                from uuid import uuid4
                raise ResourceError("RESOURCE_MISSING" if isinstance(exc, FileNotFoundError) else "RESOURCE_CORRUPT", str(lib_path), f"trace_{uuid4().hex}") from exc
            if match:
                return match.skill

            # The advertising library is an optional specialization boundary.
            # A valid visual task that has no advertising keyword must keep using
            # the approved generic skill instead of being treated as a corrupt
            # runtime resource.
            from skills.category_loader import CategorySkillLoader
            generic_index = Path(__file__).parent.parent / "skills/category_skills/index.json"
            return CategorySkillLoader(generic_index).load_for_task(task_card)
        category_skill = load_with_policy(load_category, resource=str(lib_path),
            allow_degradation=self.policy.allow_skill_degradation, fallback=None,
            emit=lambda detail: self.store.events.append("resource_degraded", **detail))

        # 2. 唯一风格入口：图片只在 VLM 提取边界出现，渲染侧只收到文字。
        from agent_core.models import SignStatus, TaskConfirmationDoc
        from agent_core.style_pipeline import StyleRenderPlanner
        from skills.style_library import StyleExtractor, StyleLibrary, select_five

        if not data.get("task_approval") or data["task_approval"].get("revision_hash") != data.get("task_revision", {}).get("revision_hash"):
            raise ValueError("任务书修改后必须重新人工确认，才能进入付费步骤。")
        style_root = Path(self.policy.style_library_root)
        if not style_root.is_absolute():
            configured = Path.cwd() / style_root
            if configured.exists():
                style_root = configured
            else:
                from skills.builtin_style_library import ensure_builtin_style_library
                style_root = ensure_builtin_style_library(self.store.root / "runtime" / "style-library-v1")
        library = StyleLibrary(style_root)
        records = library.records()
        extractor = StyleExtractor(style_root, self._extract_style, model_id="offline-style-vlm" if self.offline_mode else "runtime-style-vlm")
        def extraction_for(record):
            try:
                return library.extraction(record)
            except Exception:
                return extractor.extract(record)
        selected_styles = select_five(records, extraction_for, specification_to_markdown(spec))
        doc = TaskConfirmationDoc(task_id=task_card.task_id, summary=specification_to_markdown(spec),
                                  confirmed_facts=[], default_handling_for_unknowns=[],
                                  markdown_body=specification_to_markdown(spec), sign_status=SignStatus.APPROVED,
                                  signed_by=data["task_approval"]["actor"])
        plans = StyleRenderPlanner().plan(confirmation=doc, category=category_skill, styles=selected_styles,
            deliverable_goal=task_card.deliverable_goal, usage_context=task_card.usage_context,
            task_revision_hash=data["task_revision"]["revision_hash"], config_hash=self.policy.sha256())

        # 3. 终端输出这 5 张文本卡片给用户
        self.output("\n=================== 🎨 筛选出 5 种艺术风格方向 ===================")
        for i, selected in enumerate(selected_styles, 1):
            self.output(f"\n【方向 {i}：{selected.style.title}】")
            self.output(f"  • 主导机制：{selected.mechanism}")
            self.output(f"  • 推荐理由：{selected.reason}")
            self.output(f"  • 主要风险：{selected.risk}")
        self.output("\n=================================================================")
        self.output("保持主体内容、品牌色彩与空间条件一致，正在按上述 5 种风格分别生图，请稍候...\n")

        # 4. 生图逻辑：固定内容与品牌色，只注入各自风格
        from render_clients.payload_mapper import validate_render_size
        image_binding = self.gateway.router.binding_for_state("initial_candidate_generation")
        validate_render_size(image_binding.model, self.policy.default_output_size)

        def render(index: int) -> dict[str, Any]:
            plan = plans[index]
            result = self._image_call("initial_candidate_generation", plan.prompt_text, [], index=index)
            return {**normalize_image_asset(result), "candidate_index": index, "id": f"candidate-{index + 1}",
                    "style_name": selected_styles[index].style.title, "style_id": plan.style_id,
                    "extraction_key": plan.extraction_key, "prompt_version_id": plan.prompt_version_id,
                    "provenance": plan.provenance}

        batch = CandidateBatchGenerator(self.store, render, attempts=1,
                                        max_workers=self.policy.candidate_concurrency).generate(spec.content_hash)
        if batch["failed"]:
            first = batch["failed"][0]
            if not first.get("retryable"):
                raise ValueError(f"候选图生成请求被拒绝且不可重试：{first['error']} 请修正配置或凭证后重新生成。")
            raise RuntimeError(f"候选图有 {len(batch['failed'])} 项生成失败；成功项已保存，可确认后重试。")
        return {"candidates": batch["succeeded"], "style_selections": [
            {"style_id": item.style.style_id, "extraction_key": item.extraction.extraction_key,
             "reason": item.reason, "task_fit": item.task_fit, "mechanism": item.mechanism, "risk": item.risk}
            for item in selected_styles]}

    def _extract_style(self, image_path: str, prompt: str) -> Any:
        if self.offline_mode:
            return {"composition":"稳定构图", "material":"可控材质", "lighting":"均衡光影",
                    "narrative":"清晰叙事", "graphic_language":"差异化图形语言", "color":"任务色彩",
                    "prompt_supplement":"使用抽象视觉机制，不复制参考作品。"}
        return self.gateway.call("self_check_inspection", ModelRole.VISION_LANGUAGE_MODEL,
            lambda route: self._vlm(route).inspect(image_path, prompt),
            messages=[{"role":"user","content":prompt},{"role":"image","content":"[LOCAL_STYLE_ASSET]"}],
            variables={"style_asset_sha256": content_hash(Path(image_path).read_bytes().hex())},
            template_id="style-extraction", template_version="1", input_refs=[], needs_images=1)

    def _selection(self, data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        selected = options.get("selected_id")
        if not selected: return {"waiting": True, "phase": "waiting_master_selection"}
        master = self.workflow.select_master(data["candidates"], selected)
        selection = {"candidate_id": selected, "artifact_id": master.get("artifact_id"),
                     "actor": options.get("actor") or "manual-user"}
        self.store.events.append("master_selected", selection=selection, asset=master)
        return {"master_asset": master, "selected_master": selection,
                "waiting": False, "phase": "master_selected"}

    @staticmethod
    def _fallback_style_cards():
        """Minimal policy-approved directions used only when explicit degradation is enabled."""
        from agent_core.models import SkillStatus, StyleCard, VisualLanguage
        compositions = ("centered", "diagonal", "grid", "asymmetric", "panoramic")
        return [StyleCard(style_id=f"degraded-{index}", version="1", style_name=f"安全方向 {index}",
                          composition=composition, visual_language=VisualLanguage(materiality=["neutral"]),
                          risk_notes=["外部风格资源不可用，需人工复核"], status=SkillStatus.APPROVED)
                for index, composition in enumerate(compositions, 1)]

    def _self_check(self, data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        spec = TaskSpecification.model_validate(data["task_specification"])
        policy_data = data.get("self_check_policy", self.policy.self_check.model_dump(mode="json"))
        action = options.get("manual_action")
        at_round_limit = (data.get("phase") == "waiting_human_approval" and
                          "round_limit" in str(data.get("termination_reason")))
        if at_round_limit and action:
            asset = data.get("best_asset") or data.get("asset") or data["master_asset"]
            round_number = int(data.get("round", 0))
            if action.action == "accept_current":
                checked = data.get("asset") or asset
                self.store.events.append("quality_disposition", action="accept_current", selected_asset=checked)
                self.store.events.append("calibration_current_asset_accepted", round=round_number,
                                         asset_hash=checked["sha256"], decision=(data.get("inspection") or {}).get("decision"),
                                         policy=policy_data)
                return {"waiting": False, "phase": "calibration_completed", "round": round_number,
                        "asset": checked, "current_asset": checked, "calibration_status": "human_accepted",
                        "termination_satisfied": True, "termination_reason": "human_accepted_current_asset",
                        "latest_checked_asset_hash": checked["sha256"], "selected_policy": policy_data}
            if action.action == "end":
                self.store.events.append("quality_disposition", action="end", selected_asset=asset)
                self.store.events.append("calibration_terminated_without_delivery", round=round_number,
                                         asset_hash=asset["sha256"], decision=(data.get("inspection") or {}).get("decision"))
                return {"waiting": True, "phase": "terminated_without_delivery", "round": round_number,
                        "asset": asset, "current_asset": asset, "calibration_status": "terminated_without_delivery",
                        "termination_satisfied": False, "termination_reason": "human_ended_without_delivery",
                        "latest_checked_asset_hash": data.get("latest_checked_asset_hash"), "selected_policy": policy_data}
            if action.action == "human_tune_best":
                prompt = options.get("human_prompt")
                if not prompt:
                    raise ValueError("human_tune_best 必须同时提供 --human-prompt。")
                self.store.events.append("quality_disposition", action="human_tune_best", selected_asset=asset)
                return self._human_rework({**data, "asset": asset, "current_asset": asset}, options)
            if action.action == "add_rounds":
                if not action.cost_confirmed or action.additional_rounds < 1:
                    raise ValueError("add_rounds 必须提供正整数 --additional-rounds 并使用 --confirm-cost。")
                policy_data = dict(policy_data)
                policy_data["termination"] = "solo"
                policy_data["max_rounds"] = round_number + action.additional_rounds
                policy_data["fixed_rounds"] = min(int(policy_data.get("fixed_rounds", 1)), policy_data["max_rounds"])
                self.store.events.append("quality_disposition", action="add_rounds",
                                         additional_rounds=action.additional_rounds, cost_confirmed=True,
                                         selected_asset=asset)
                data = {**data, "asset": asset, "current_asset": asset, "round": round_number + 1,
                        "phase": "additional_rounds_approved"}
            elif action.action not in {"execute", "edit_and_execute", "skip"}:
                raise ValueError(f"轮次上限不支持处置动作：{action.action}")
        loop = CalibrationLoop(self.store, SelfCheckPolicy(**policy_data), inspector=self._inspect,
            reworker=lambda assembled: self._image_call("self_check_rework", assembled["text"], [r["uri"] for r in assembled["references"]]),
            presenter=lambda number, result: self._present_inspection(number, result))
        result = loop.run(current_asset=data.get("asset") or data["master_asset"], stable_specification=specification_to_markdown(spec),
                          constraints=[], approve=(lambda _: action) if action else None,
                          start_round=int(data.get("round", 1)))
        return {**result, "current_asset": result.get("asset", data.get("master_asset"))}
    
    def _human_rework(self, data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        # Human tuning is an optional graph stage. Automatic inspection success
        # traverses it without creating a synthetic waiting_human_tune gate;
        # only an explicit human-tune disposition activates the interactive
        # branch below.
        if data.get("phase") == "calibration_completed" and not data.get("human_tune_mode"):
            return {"asset": data.get("current_asset") or data.get("asset"),
                    "waiting": False, "phase": "calibration_completed"}
        action = options.get("manual_action")
        if action and action.action == "accept_current":
            current = data.get("current_asset") or data["asset"]
            self.store.events.append("human_tune_final_accepted", asset_hash=current["sha256"])
            return {"asset": current, "current_asset": current, "waiting": False,
                    "phase": "calibration_completed", "calibration_status": "human_accepted",
                    "termination_satisfied": True, "termination_reason": "human_tune_final_accepted",
                    "latest_checked_asset_hash": current["sha256"],
                    "selected_policy": data.get("selected_policy") or data.get("self_check_policy") or {"release": "human"}}
        prompt = options.get("human_prompt")
        if not prompt: return {"asset": data.get("current_asset") or data.get("asset"), "waiting": True,
                               "phase": "waiting_human_tune", "human_tune_mode": True}
        current = data.get("current_asset") or data["asset"]
        result = self._image_call("human_prompt_rework", prompt, [str(current["uri"])])
        asset = normalize_image_asset(result)
        self.store.events.append("calibration_invalidated", reason="human_rework", previous_checked_asset_hash=data.get("latest_checked_asset_hash"), new_asset_hash=asset["sha256"])
        return {"asset": asset, "current_asset": asset, "waiting": True, "phase": "waiting_human_tune", "human_tune_mode": True,
                "calibration_status": "waiting_human_tune", "termination_satisfied": False, "termination_reason": "human_tune_in_progress",
                "latest_checked_asset_hash": None, "inspection": None}

    def _present_inspection(self, number: int, result: VisualCheckResult) -> None:
        self.store.events.append("inspection_presented", round=number, result=result.model_dump(mode="json"))
        self.output(f"第 {number} 轮画面质检\n{self.presenter.inspection(result)}")

    def _final(self, data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        asset = data.get("asset") or data.get("current_asset")
        checked_hash = data.get("latest_checked_asset_hash")
        satisfied = bool(data.get("termination_satisfied"))
        if data.get("calibration_status") not in {"completed", "human_accepted"}:
            satisfied = False
        if not checked_hash or checked_hash != asset.get("sha256"):
            satisfied = False
        if not data.get("selected_policy") or not data.get("termination_reason"):
            satisfied = False
        actor = options.get("actor") or (data.get("task_approval") or {}).get("actor")
        if self.offline_mode and asset.get("mock") is True and "domain_state" in data:
            if not options.get("final_approved") or not actor:
                raise ValueError("离线演练最终验收需要操作人人工确认。")
            if not satisfied:
                raise ValueError("离线演练最终验收前必须完成并通过质检处置。")
            self.store.events.append("offline_rehearsal_completed", actor=actor,
                                     asset_sha256=asset["sha256"])
            return {"rehearsal_asset": asset, "offline_rehearsal_completed": True,
                    "completed": True, "phase": "offline_rehearsal_completed"}
        if "domain_state" not in data:
            # Read-only compatibility for checkpoints created before the unified
            # graph. All newly created task projects use the frozen contract.
            self.workflow.validate_final_asset(asset, human_approved=bool(options.get("final_approved")), self_check_complete=satisfied)
            return {"final_asset": asset, "completed": True}
        self.workflow.validate_final_asset(asset, human_approved=bool(options.get("final_approved") and actor), self_check_complete=satisfied)
        frozen = freeze_delivery({**data, "quality_asset_sha256": checked_hash, "quality_passed": satisfied},
                                 asset=asset, quality_version=str(data.get("quality_version", "visual-check-v2")), actor=actor)
        delivery = frozen.model_dump(mode="json")
        trace_ref = f"project:{self.store.project_id}:asset:{asset['sha256']}"
        envelope = build_delivery(data, self.store.project_id, asset, trace_ref)
        delivery_files = persist_delivery(self.store.root, envelope)
        self.store.events.append("delivery_frozen", delivery=delivery)
        self.store.events.append("delivery_exported", envelope=envelope.model_dump(mode="json"), files=delivery_files)
        return {"final_asset": asset, "frozen_delivery": delivery, "delivery_envelope": envelope.model_dump(mode="json"),
                "delivery_files": delivery_files, "completed": True}

    def _text(self, route: ModelRoute):
        client = build_text_client(route.binding, timeout=self.policy.model_timeout_seconds)
        if client is None: raise RuntimeError("文本模型不可用。")
        return client

    def _inspect(self, image_uri: str, prompt: str) -> dict[str, Any]:
            if self.offline_mode:
                return {"passed": False, "decision": "continue", "rework_prompt_delta": "提高主体清晰度", "confidence": 0.8}
            
            # 👇 构造明确要求返回 JSON 的质检 Prompt
            inspection_prompt = (
                f"你是一个专业的视觉质量审查员。请对比任务书与图片，检查图片是否符合要求。\n"
                f"只能输出一个纯 JSON 对象，格式要求如下：\n"
                f"{{\n"
                f'  "passed": true或false,\n'
                f'  "decision": "pass"（符合）或 "continue"（需微调）或 "blocked"（严重不符）,\n'
                f'  "deviations": ["发现的问题或偏差描述"],\n'
                f'  "rework_prompt_delta": "如果不符合，给出具体的改图提示词建议",\n'
                f'  "confidence": 0.95\n'
                f"}}\n\n"
                f"设计任务书要求：\n{prompt}"
            )
            
            provider_image = self.provider_assets.resolve(image_uri)
            raw = self.gateway.call("self_check_inspection", ModelRole.VISION_LANGUAGE_MODEL,
                lambda route: self._vlm(route).inspect(provider_image, inspection_prompt),
                messages=[{"role":"user","content":inspection_prompt},{"role":"image","content":image_uri}],
                variables={"image":image_uri}, template_id="visual-check", template_version="2", input_refs=[image_uri], needs_images=1)
            def repair(response: str, errors: str):
                repair_prompt = f"只修复为指定 JSON Schema，不改变语义。校验错误：{errors}\n原响应：{response}"
                return self.gateway.call("self_check_inspection", ModelRole.VISION_LANGUAGE_MODEL,
                    lambda route: self._vlm(route).inspect(provider_image, repair_prompt), messages=[{"role":"user","content":repair_prompt}],
                    variables={"repair":True}, template_id="visual-check-repair", template_version="1", input_refs=[image_uri], needs_images=1)
            try:
                return parse_with_one_repair(raw, repair).model_dump(mode="json")
            except InspectionOutputError as exc:
                self.store.events.append("inspection_schema_failed", safe_raw=exc.safe_raw, errors=exc.errors, recoverable=True)
                raise

    def _vlm(self, route: ModelRoute):
        client = build_vlm_client(route.binding, timeout=self.policy.model_timeout_seconds)
        if client is None: raise RuntimeError("视觉检查模型不可用。")
        return client

    def _image_call(self, state: str, prompt: str, references: list[str], *, index: int | None = None) -> dict[str, Any]:
        if state == "initial_candidate_generation":
            from agent_core.style_pipeline import assert_reference_isolated
            assert_reference_isolated({"prompt": prompt, "candidate_index": index})
            if references:
                raise ValueError("STYLE_REFERENCE_LEAK:reference_images")
        if self.offline_mode:
            # A real decodable fixture crosses the same persistence boundary;
            # mock/provider URLs never enter successful events or checkpoints.
            fixture = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            result = self.gateway.call(state, ModelRole.TEXT_TO_IMAGE_MODEL,
                lambda route: {"content": fixture, "mock": True, "provider": "offline", "model": route.binding.model},
                messages=[{"role":"user","content":prompt}], variables={"candidate_index":index, "reference_images":references},
                template_id=state, template_version="2", input_refs=references, needs_images=len(references))
            from storage.image_ingest import persist_image_response
            saved = persist_image_response(self.store.artifacts, result,
                                           metadata={"state": state, "candidate_index": index, "mock": True})
            return normalize_image_asset({**saved, "mock": True})
        provider_references = self.provider_assets.resolve_all(references)
        result = self.gateway.call(state, ModelRole.TEXT_TO_IMAGE_MODEL,
            lambda route: ArkImageRenderClient(base_url=self.policy.image_api_base_url or "https://ark.cn-beijing.volces.com/api/v3",
                model=route.binding.model, timeout=self.policy.model_timeout_seconds, max_retries=0,
                idempotency_key=str(route.binding.parameters.get("_idempotency_key", ""))).render(build_render_payload(route.binding.model, prompt,
                self.policy.default_output_size, {"state":state}, response_format=self.policy.response_format,
                watermark=self.policy.watermark, reference_images=provider_references)),
            messages=[{"role":"user","content":prompt}], variables={"candidate_index":index, "reference_images":references},
            template_id=state, template_version="2", input_refs=references, needs_images=len(references))
        from storage.image_ingest import persist_image_response
        return persist_image_response(self.store.artifacts, result, metadata={"state": state, "candidate_index": index})
