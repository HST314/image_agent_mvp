"""Image Agent 的 FastAPI Web 薄适配层。

本文件只负责 HTTP 输入校验、调用生产 WorkflowRunner / ProjectStore，以及把
文件型快照转换成前端可消费的视图；工作流判断和图片生成均由现有后端完成。
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import hashlib
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_core.models import ImageTaskCard
from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
from calibrator.calibration_loop import ManualAction
from storage.project_store import ProjectStore, atomic_json

from configs.env_loader import load_dotenv  # 引入 .env 加载器
from configs.runtime_policy import RuntimePolicy
from skills.errors import ResourceError
from agent_core.jobs import JobNotFoundError, JobRegistry
from agent_core.annotation import compose
from storage.project_store import CorruptProjectError
from storage.provider_assets import ArtifactCorruptError, ArtifactNotFoundError
from diagnostics import run_diagnostics

load_dotenv(".env")  # 在程序启动时自动读取当前目录下的 .env 文件

APP_ROOT = Path(__file__).resolve().parent
FRONTEND_ROOT = APP_ROOT / "frontend"
PROJECTS_ROOT = Path(os.getenv("IMAGE_AGENT_FRONT_PROJECTS_ROOT", FRONTEND_ROOT / "data" / "projects")).resolve()
MODEL_CONFIG = Path(os.getenv("IMAGE_AGENT_MODEL_CONFIG", APP_ROOT / "configs" / "model_config.yaml")).resolve()
MAX_REQUEST_BYTES = 512 * 1024
MAX_ASSET_BYTES = 25 * 1024 * 1024
PROJECT_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,63}$")
BRANCH_NAME_PATTERN = r"^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff._-]{1,63}$"
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
# T10：defer_run 创建时持久化的入站任务卡文件名；jobs 引导路径据此启动首个推进。
INTAKE_TASK_FILE = "intake_task.json"

app = FastAPI(
    title="Image Agent Studio",
    description="生产 Image Agent 的 Web 薄适配接口",
    version="1.0.0",
)
def _persist_recovered_job(record: dict[str, Any]) -> None:
    """Mirror restart interruption into the owning project's durable timeline."""
    project_id = str(record["project_id"])
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{1,63}", project_id):
        raise ValueError("PROJECT_ID_INVALID")
    store = ProjectStore(PROJECTS_ROOT, project_id)
    if not (store.root / "manifest.json").is_file():
        raise FileNotFoundError(f"工程不存在：{project_id}")
    store.events.append("job_status_changed", job_id=record["job_id"], operation=record.get("operation"),
                        status=record["status"], error=record.get("error"))


JOBS = JobRegistry(PROJECTS_ROOT / ".jobs", on_recover=_persist_recovered_job)


class ProjectNotFoundError(FileNotFoundError):
    """The requested project root is absent (not an arbitrary missing file)."""


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateProjectRequest(StrictRequest):
    project_id: str = Field(min_length=2, max_length=64)
    task_card: dict[str, Any]
    offline: bool = False
    # T10（契约 §7）：True 时仅持久化工程与入站任务卡并立即返回，首个工作流
    # 推进交由 POST /api/projects/{id}/jobs 以异步 job 执行，前端不再长时间等待。
    defer_run: bool = False


class AdvanceRequest(StrictRequest):
    selected_id: str | None = Field(default=None, max_length=128)
    clarification_answers: dict[str, Any] | None = None
    edited_markdown: str | None = Field(default=None, max_length=100_000)
    manual_action: Literal["execute", "edit_and_execute", "skip", "end", "accept_current"] | None = None
    edited_delta: str | None = Field(default=None, max_length=4_000)
    human_prompt: str | None = Field(default=None, max_length=8_000)
    final_approved: bool = False
    task_approved: bool = False
    skill_action: Literal["approve", "retry"] | None = None
    category_action: Literal["approve", "retry"] | None = None
    actor: str | None = Field(default=None, min_length=1, max_length=128)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)

class AnnotationRequest(StrictRequest):
    artifact_id: str = Field(pattern=r"^[a-zA-Z0-9._-]{1,160}$")
    marks: list[dict[str, Any]] = Field(min_length=1, max_length=5000)
    prompt: str = Field(min_length=1, max_length=8_000)

class QualityDispositionRequest(StrictRequest):
    action: Literal["add_rounds_with_cost_confirmation", "human_tune_best", "abandon"]
    additional_rounds: int = Field(default=0, ge=0, le=20)
    cost_confirmed: bool = False
    checkpoint: str | None = Field(default=None, pattern=r"^checkpoint_[0-9a-f]{24}$")


class BranchRequest(StrictRequest):
    checkpoint: str = Field(min_length=1, max_length=256)
    name: str | None = Field(default=None, min_length=2, max_length=64, pattern=BRANCH_NAME_PATTERN)
    mode: Literal["fork_after", "rerun_stage"] = "rerun_stage"

class BranchSwitchRequest(StrictRequest):
    checkpoint_id: str = Field(pattern=r"^checkpoint_[0-9a-f]{24}$")

class PolicyRevisionRequest(StrictRequest):
    policy: dict[str, Any]
    actor: str = Field(min_length=1, max_length=128)
    confirmed: bool

class UnknownResolutionRequest(StrictRequest):
    action: Literal["retry_after_confirmation", "abandon"]
    actor: str = Field(min_length=1, max_length=128)


@app.middleware("http")
async def enforce_request_size(request: Request, call_next):
    """在 JSON 解析前拒绝超大请求，避免内存型拒绝服务。"""
    raw_length = request.headers.get("content-length")
    if raw_length:
        try:
            if int(raw_length) > MAX_REQUEST_BYTES:
                return JSONResponse(status_code=413, content={"detail": "请求内容超过 512 KiB 限制。"})
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Content-Length 无效。"})

    # Content-Length is optional (notably for Transfer-Encoding: chunked) and
    # must not be treated as the source of truth.  Consume the ASGI body with a
    # hard bound, then replay the bounded bytes to the downstream parser.
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_REQUEST_BYTES:
            return JSONResponse(status_code=413, content={"detail": "请求内容超过 512 KiB 限制。"})
        chunks.append(chunk)

    body = b"".join(chunks)
    # BaseHTTPMiddleware's cached request replays `_body` to the route.  Setting
    # this after the bounded stream read avoids a second read from the now
    # exhausted client receive channel.
    request._body = body
    return await call_next(request)


def _safe_project_id(value: str) -> str:
    value = value.strip()
    if not PROJECT_ID.fullmatch(value):
        raise HTTPException(status_code=422, detail="工程 ID 仅允许 2–64 位字母、数字、下划线和连字符。")
    return value


def _store(project_id: str) -> ProjectStore:
    return ProjectStore(PROJECTS_ROOT, _safe_project_id(project_id))


def _existing_store(project_id: str) -> ProjectStore:
    store = _store(project_id)
    if not (store.root / "manifest.json").is_file():
        raise ProjectNotFoundError(f"工程不存在：{store.project_id}")
    return store


def _runner(store: ProjectStore, offline: bool) -> WorkflowRunner:
    if not MODEL_CONFIG.is_file():
        raise RuntimeError("模型配置文件不存在，请设置 IMAGE_AGENT_MODEL_CONFIG。")
    return WorkflowRunner(store, MODEL_CONFIG, offline_mode=offline)


def _project_policy(store: ProjectStore) -> RuntimePolicy:
    """Load the immutable project runtime policy; requests never choose its mode."""
    policy_file = store.root / "runtime_policy.json"
    if not policy_file.is_file():
        raise FileNotFoundError(f"工程运行策略不存在：{store.project_id}")
    payload = json.loads(policy_file.read_text(encoding="utf-8"))
    return RuntimePolicy.model_validate(payload["policy"])


def _project_runner(store: ProjectStore) -> WorkflowRunner:
    return _runner(store, _project_policy(store).offline_mode)


def _options(body: AdvanceRequest) -> RunnerOptions:
    action = None
    if body.manual_action:
        action = ManualAction(action=body.manual_action, edited_delta=body.edited_delta)
    return RunnerOptions(
        selected_id=body.selected_id,
        manual_action=action,
        human_prompt=body.human_prompt,
        edited_markdown=body.edited_markdown,
        final_approved=body.final_approved,
        clarification_answers=body.clarification_answers,
        task_approved=body.task_approved,
        actor=body.actor,
        skill_action=body.skill_action,
        category_action=body.category_action,
    )


def _project_view(store: ProjectStore, *, include_progress_snapshots: bool = True) -> dict[str, Any]:
    manifest = store.manifest()
    snapshot = store.resume() or {}
    delivery_status = None
    delivery_status_path = store.root / "delivery" / "finalized.json"
    if delivery_status_path.is_file():
        try:
            delivery_status = json.loads(delivery_status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            delivery_status = None
    view = {
        "project_id": store.project_id,
        "manifest": manifest,
        "snapshot": snapshot,
        "history": store.history(),
        "capabilities": _capabilities(manifest, snapshot),
        "resource_events": [e for e in store.history() if e.get("type") == "resource_degraded"],
        "unknown_actions": _gateway_for_store(store).unknown_actions(),
        "runtime_policy": json.loads((store.root / "runtime_policy.json").read_text(encoding="utf-8"))["policy"],
        "active_job": JOBS.active_for_project(store.project_id),
        "delivery_status": delivery_status,
    }
    if include_progress_snapshots:
        # T9：只返回当前分支谱系上的不可变检查点；前端据此展示已完成阶段的
        # 只读快照，回看本身不修改 manifest/current_checkpoint。工程列表无需
        # 这些大字段，避免为每张工程卡重复载入完整历史快照。
        view["progress_snapshots"] = store.progress_snapshots()
    return view


def _job_operation(body: AdvanceRequest) -> str:
    if body.category_action == "retry":
        return "重新匹配品类约束"
    if body.category_action == "approve":
        return "确认品类约束"
    if body.skill_action == "retry":
        return "重新调用两库"
    if body.skill_action == "approve":
        return "确认技能调用并生成五张主图"
    if body.manual_action in {"execute", "edit_and_execute"}:
        return "执行质检建议"
    if body.human_prompt:
        return "模型微调图像"
    if body.selected_id:
        return "确认主图并开始质检"
    if body.task_approved:
        return "生成候选图像"
    if body.final_approved or body.manual_action == "accept_current":
        return "确认最终图像"
    return "推进工作流"

def _gateway_for_store(store: ProjectStore):
    return _project_runner(store).gateway


def _capabilities(manifest: dict[str, Any], snapshot: dict[str, Any]) -> list[str]:
    """仅把生产快照已有等待原因映射为 UI 动作，不执行或替代状态迁移。"""
    if manifest.get("failed_step"):
        error = manifest["failed_step"].get("error", {})
        if error.get("category") == "content_moderation":
            return ["edit_rework", "abandon"]
        return ["retry"] if error.get("retryable", False) else []
    if snapshot.get("completed"):
        return ["inspect", "branch"]
    phase = snapshot.get("phase")
    if phase == "waiting_category_approval":
        return ["approve_category_constraint", "retry_category_constraint"]
    if phase == "waiting_clarification":
        return ["answer_clarification"]
    if phase == "waiting_skill_approval":
        return ["approve_skill_invocations", "retry_skill_invocations"]
    if phase == "waiting_master_selection":
        return ["select_master"]
    if phase == "waiting_human_approval":
        return ["review_calibration", "enter_human_tune"]
    if phase == "additional_rounds_approved":
        return ["resume_quality_inspection"]
    if phase == "waiting_human_tune":
        return ["submit_human_tune"]
    if phase == "waiting_reinspection":
        return ["resume_quality_inspection"]
    recovery = {
        "category_approved": "start_clarification",
        "ready_to_draft": "build_taskbook",
        "task_approved": "prepare_style_direction",
        "skill_approved_pending_render": "render_candidates",
        "candidate_generation_completed": "choose_master",
        "master_selected": "start_quality_inspection",
        "ready_for_quality_inspection": "start_quality_inspection",
        "calibration_completed": "open_final_approval",
    }
    if snapshot and phase in recovery:
        return [recovery[phase], "branch"]
    if snapshot:
        return ["branch"]
    return []


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, ValidationError):
        return HTTPException(status_code=422, detail=exc.errors(include_url=False))
    if isinstance(exc, ResourceError):
        return HTTPException(status_code=503, detail=exc.as_dict())
    if isinstance(exc, ArtifactNotFoundError):
        return HTTPException(status_code=404, detail={"code":"ARTIFACT_NOT_FOUND","message":str(exc)})
    if isinstance(exc, ArtifactCorruptError):
        return HTTPException(status_code=409, detail={"code":"ARTIFACT_CORRUPT","message":str(exc)})
    if isinstance(exc, JobNotFoundError):
        return HTTPException(status_code=404, detail={"code":"JOB_NOT_FOUND","message":str(exc)})
    if isinstance(exc, CorruptProjectError):
        return HTTPException(status_code=409, detail={"code":"PROJECT_CORRUPT","message":str(exc)})
    if isinstance(exc, ProjectNotFoundError):
        return HTTPException(status_code=404, detail="PROJECT_NOT_FOUND: 工程不存在。")
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=409, detail={"code":"PROJECT_FILE_MISSING","message":str(exc)})
    if isinstance(exc, NotADirectoryError):
        return HTTPException(status_code=409, detail={"code":"PROJECT_PATH_INVALID","message":str(exc)})
    if isinstance(exc, FileExistsError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=409, detail=str(exc))
    if "正在由另一个进程处理" in str(exc):
        return HTTPException(status_code=423, detail=str(exc))
    return HTTPException(
        status_code=503,
        detail=f"后端能力暂不可用：{exc}。已有进度已保留，可修正配置后恢复或重试。",
    )


SECRET_FIELDS = ("api_key", "apikey", "authorization", "token", "secret")


def _redact(value: Any) -> Any:
    """Recursively remove credentials before an audit record crosses HTTP."""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if any(secret in key.lower() for secret in SECRET_FIELDS) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _page(items: list[dict[str, Any]], after: int, limit: int, *, cursor: str) -> dict[str, Any]:
    selected = [item for item in items if int(item.get(cursor, 0)) > after][:limit]
    return {"items": selected, "next_cursor": int(selected[-1][cursor]) if selected else after,
            "has_more": any(int(item.get(cursor, 0)) > (int(selected[-1][cursor]) if selected else after) for item in items)}


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(FRONTEND_ROOT / "index.html", media_type="text/html")


# 前端模块化静态资源（T35）：仅暴露 frontend/static 下的 js/css，Starlette 内部拒绝路径穿越。
app.mount("/static", StaticFiles(directory=FRONTEND_ROOT / "static"), name="static")


@app.get("/api/health")
async def health(response: Response) -> dict[str, Any]:
    result = await asyncio.to_thread(
        run_diagnostics, projects_root=PROJECTS_ROOT, model_config=MODEL_CONFIG,
        app_root=APP_ROOT, job_registry=JOBS,
    )
    if result["status"] != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result


@app.get("/api/projects")
async def list_projects() -> dict[str, Any]:
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    projects: list[dict[str, Any]] = []
    for child in sorted(PROJECTS_ROOT.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
        if not child.is_dir() or not PROJECT_ID.fullmatch(child.name) or not (child / "manifest.json").is_file():
            continue
        try:
            view = _project_view(ProjectStore(PROJECTS_ROOT, child.name), include_progress_snapshots=False)
            projects.append({
                "project_id": child.name,
                "state": view["snapshot"].get("state"),
                "phase": view["snapshot"].get("phase"),
                "completed": bool(view["snapshot"].get("completed")),
                "failed_step": view["manifest"].get("failed_step"),
                "updated_at": view["manifest"].get("updated_at"),
            })
        except (OSError, ValueError):
            continue
    return {"items": projects}


@app.post("/api/projects", status_code=status.HTTP_201_CREATED)
async def create_project(body: CreateProjectRequest) -> dict[str, Any]:
    project_id = _safe_project_id(body.project_id)
    try:
        task = ImageTaskCard.model_validate(body.task_card)
        if task.project_id != project_id:
            task = task.model_copy(update={"project_id": project_id})

        def execute() -> dict[str, Any]:
            store = _store(project_id)
            policy = RuntimePolicy.from_file(APP_ROOT / "configs/runtime.yaml").model_copy(update={"offline_mode": body.offline})
            store.create(policy.snapshot())
            if body.defer_run:
                # T10（契约 §7）：仅持久化工程与入站任务卡，立即返回视图（不做任何
                # 模型调用）；首个工作流推进由 POST /api/projects/{id}/jobs 异步执行。
                atomic_json(store.root / INTAKE_TASK_FILE, task.model_dump(mode="json"))
                return _project_view(store)
            _runner(store, body.offline).run({"task_card": task.model_dump(mode="json")}, RunnerOptions())
            return _project_view(store)

        return await asyncio.to_thread(execute)
    except Exception as exc:
        raise _translate_error(exc) from exc


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(_project_view, _existing_store(project_id))
    except Exception as exc:
        raise _translate_error(exc) from exc


@app.post("/api/projects/{project_id}/advance")
async def advance_project(project_id: str, body: AdvanceRequest) -> dict[str, Any]:
    try:
        def execute() -> dict[str, Any]:
            store = _existing_store(project_id)
            snapshot = store.resume()
            if snapshot is None:
                raise ValueError("工程还没有可恢复节点。")
            _project_runner(store).run(snapshot, _options(body))
            return _project_view(store)

        return await asyncio.to_thread(execute)
    except Exception as exc:
        raise _translate_error(exc) from exc

@app.post("/api/projects/{project_id}/jobs", status_code=status.HTTP_202_ACCEPTED)
async def create_advance_job(project_id: str, body: AdvanceRequest) -> JSONResponse:
    """Queue an advance without holding the HTTP request open."""
    project_id = _safe_project_id(project_id)
    key = body.idempotency_key or hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    def execute() -> dict[str, Any]:
        store = _existing_store(project_id); snapshot = store.resume()
        if snapshot is None:
            # T10 引导路径（契约 §7）：工程已创建但尚无检查点（defer_run 创建或首个
            # 推进失败）——从持久化的入站任务卡启动首个工作流推进；无任务卡则维持
            # 原拒绝语义。成功后 checkpoint 自动清除 failed_step，支持失败后重启。
            intake = store.root / INTAKE_TASK_FILE
            if not intake.is_file():
                raise ValueError("工程还没有可恢复节点。")
            task = json.loads(intake.read_text(encoding="utf-8"))
            _project_runner(store).run({"task_card": task}, _options(body))
            return {"project_id":project_id}
        _project_runner(store).run(snapshot, _options(body))
        return {"project_id":project_id}
    try:
        store = _existing_store(project_id)
        bootstrapping = store.resume() is None and (store.root / INTAKE_TASK_FILE).is_file()
        def persist_job(record: dict[str, Any]) -> None:
            store = _existing_store(project_id)
            store.events.append("job_status_changed", job_id=record["job_id"], operation=record["operation"],
                                status=record["status"], error=record.get("error"))
        operation = "初始化工程" if bootstrapping else _job_operation(body)
        job, created = JOBS.submit(project_id, key, operation, execute, on_event=persist_job)
        return JSONResponse(status_code=202 if created else 200, content=job)
    except Exception as exc:
        raise _translate_error(exc) from exc

@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    try: return await asyncio.to_thread(JOBS.get, job_id)
    except Exception as exc: raise _translate_error(exc) from exc

@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict[str, Any]:
    try: return await asyncio.to_thread(JOBS.cancel, job_id)
    except Exception as exc: raise _translate_error(exc) from exc

@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, after: int = 0) -> StreamingResponse:
    """Finite SSE snapshot; clients reconnect using the last sequence number."""
    try: events = await asyncio.to_thread(JOBS.events, job_id, after)
    except Exception as exc: raise _translate_error(exc) from exc
    async def stream():
        for event in events:
            yield f"id: {event['seq']}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control":"no-cache"})

@app.post("/api/projects/{project_id}/annotations")
async def annotate_and_rework(project_id: str, body: AnnotationRequest) -> dict[str, Any]:
    """Create an immutable guide asset, then create a child edited asset."""
    try:
        def execute():
            store=_store(project_id)
            with store.lock():
                snapshot=store.resume()
                if snapshot is None:
                    raise ValueError("工程还没有可恢复节点。")
                if snapshot.get("phase") != "waiting_human_tune" or not snapshot.get("human_tune_mode"):
                    raise ValueError("HUMAN_TUNE_NOT_ACTIVE")
                source, source_record=store.artifacts.resolve(body.artifact_id)
                guide_bytes=compose(source.read_bytes(), body.marks)
                guide=store.artifacts.save_bytes(guide_bytes, metadata={"kind":"annotation_guide","parent_artifact_id":body.artifact_id,"marks":body.marks})
                child=_project_runner(store)._image_call("human_prompt_rework", body.prompt, [guide["uri"]])
                updated={**snapshot, "state":"human_prompt_iteration", "asset":child, "current_asset":child,
                         "annotation_parent_asset":source_record, "annotation_guide_asset":guide,
                         "phase":"waiting_human_tune", "waiting":True, "human_tune_mode":True,
                         "calibration_status":"waiting_human_tune", "termination_satisfied":False,
                         "termination_reason":"human_tune_in_progress",
                         "latest_checked_asset_hash":None, "inspection":None}
                store.events.append("human_annotation_rework", parent_asset=source_record, guide_asset=guide, asset=child, prompt=body.prompt)
                checkpoint_id=store.checkpoint("human_prompt_iteration",updated)
                return {"parent_asset":source_record,"guide_asset":guide,"asset":child,
                        "checkpoint_id":checkpoint_id,"requires_reinspection":False,"phase":"waiting_human_tune"}
        return await asyncio.to_thread(execute)
    except Exception as exc: raise _translate_error(exc) from exc

@app.post("/api/projects/{project_id}/quality-disposition")
async def quality_disposition(project_id: str, body: QualityDispositionRequest) -> dict[str, Any]:
    """Record the explicit human route after automatic inspection is exhausted."""
    try:
        def execute():
            store=_store(project_id)
            with store.lock():
                if body.checkpoint:
                    source = store.checkpoints.load(body.checkpoint).get("data") or {}
                    if body.action != "human_tune_best" or source.get("state") != "self_check_iteration" or not (source.get("asset") or source.get("current_asset")):
                        raise ValueError("QUALITY_TUNE_NOT_AVAILABLE")
                    store.branch_from(body.checkpoint)
                snapshot=store.resume() or {}
                at_limit = (snapshot.get("phase") == "waiting_human_approval" and
                            "round_limit" in str(snapshot.get("termination_reason")))
                can_tune = (snapshot.get("state") == "self_check_iteration" and
                            bool(snapshot.get("asset") or snapshot.get("current_asset")))
                if body.action == "human_tune_best" and not can_tune:
                    raise ValueError("QUALITY_TUNE_NOT_AVAILABLE")
                if body.action != "human_tune_best" and not at_limit:
                    raise ValueError("QUALITY_LIMIT_NOT_REACHED")
                if body.action=="add_rounds_with_cost_confirmation" and (not body.cost_confirmed or body.additional_rounds<1):
                    raise ValueError("COST_CONFIRMATION_REQUIRED")
                asset=(snapshot.get("best_asset") if at_limit else None) or snapshot.get("inspection_asset") or snapshot.get("asset") or snapshot.get("current_asset")
                store.events.append("quality_disposition", action=body.action, additional_rounds=body.additional_rounds,
                                    cost_confirmed=body.cost_confirmed, selected_asset=asset)
                updated=dict(snapshot)
                if body.action=="abandon":
                    updated.update(phase="terminated_without_delivery",calibration_status="terminated_without_delivery",
                                   termination_satisfied=False,termination_reason="human_abandoned_after_limit")
                    status_value="abandoned"
                elif body.action=="human_tune_best":
                    updated.update(asset=asset,current_asset=asset,phase="waiting_human_tune",calibration_status="waiting_human_tune",
                                   human_tune_mode=True,termination_satisfied=False,termination_reason="human_tune_in_progress",
                                   latest_checked_asset_hash=None,inspection=None,available_actions=[],best_asset=None)
                    status_value="waiting_human_tune"
                else:
                    policy=dict(updated.get("self_check_policy") or updated.get("selected_policy") or {})
                    policy["termination"]="solo"; policy["max_rounds"]=int(snapshot.get("round",0))+body.additional_rounds
                    policy["fixed_rounds"]=min(int(policy.get("fixed_rounds",1)),policy["max_rounds"])
                    updated.update(asset=asset,current_asset=asset,phase="additional_rounds_approved",calibration_status="pending",
                                   self_check_policy=policy,round=int(snapshot.get("round",0))+1,
                                   available_actions=[],best_asset=None,inspection=None,termination_reason=None,
                                   termination_satisfied=False)
                    status_value="additional_rounds_approved"
                store.checkpoint("self_check_iteration",updated)
            return {"status":status_value,"additional_rounds":body.additional_rounds,"asset":asset}
        return await asyncio.to_thread(execute)
    except Exception as exc: raise _translate_error(exc) from exc

@app.post("/api/projects/{project_id}/delivery/retry")
async def retry_delivery_note(project_id: str) -> dict[str, Any]:
    """Regenerate note files without mutating the already frozen image."""
    try:
        def execute():
            from agent_core.delivery import build_delivery, persist_delivery
            store=_store(project_id); snapshot=store.resume() or {}
            asset=snapshot.get("final_asset")
            frozen=snapshot.get("frozen_delivery")
            if not asset or not frozen or frozen.get("asset_sha256")!=asset.get("sha256"):
                raise ValueError("DELIVERY_NOT_FROZEN")
            envelope=build_delivery(snapshot,store.project_id,asset,f"project:{store.project_id}:asset:{asset['sha256']}")
            files=persist_delivery(store.root,envelope)
            store.events.append("delivery_note_retried",asset_sha256=asset["sha256"],files=files)
            return {"delivery_envelope":envelope.model_dump(mode="json"),"delivery_files":files}
        return await asyncio.to_thread(execute)
    except Exception as exc: raise _translate_error(exc) from exc


@app.post("/api/projects/{project_id}/delivery/finalize")
async def finalize_project_delivery(project_id: str) -> dict[str, Any]:
    """Idempotently save the final image and Markdown note to the project delivery folder."""
    try:
        def execute():
            from datetime import datetime, timezone
            from agent_core.contracts import DesignDeliveryEnvelopeV1
            from agent_core.delivery import build_delivery, finalize_delivery

            store = _store(project_id)
            with store.lock():
                snapshot = store.resume() or {}
                asset = snapshot.get("final_asset")
                frozen = snapshot.get("frozen_delivery")
                if not snapshot.get("completed") or not asset or not frozen or frozen.get("asset_sha256") != asset.get("sha256"):
                    raise ValueError("DELIVERY_NOT_FROZEN")
                source, record = store.artifacts.resolve(str(asset.get("artifact_id", "")))
                if record.get("sha256") != asset.get("sha256"):
                    raise ValueError("最终图片与冻结交付记录不一致。")
                raw_envelope = snapshot.get("delivery_envelope")
                envelope = (DesignDeliveryEnvelopeV1.model_validate(raw_envelope) if raw_envelope else
                            build_delivery(snapshot, store.project_id, asset,
                                           f"project:{store.project_id}:asset:{asset['sha256']}"))
                files = finalize_delivery(store.root, envelope, source)
                marker_path = store.root / "delivery" / "finalized.json"
                previous = None
                if marker_path.is_file():
                    try: previous = json.loads(marker_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError): previous = None
                marker = {
                    "finalized": True,
                    "asset_sha256": asset["sha256"],
                    "files": files,
                    "finalized_at": (previous.get("finalized_at") if previous and previous.get("asset_sha256") == asset["sha256"]
                                     else datetime.now(timezone.utc).isoformat()),
                }
                atomic_json(marker_path, marker)
                if not previous or previous.get("asset_sha256") != asset["sha256"]:
                    store.events.append("delivery_finalized", asset_sha256=asset["sha256"], files=files)
                return marker
        return await asyncio.to_thread(execute)
    except Exception as exc:
        raise _translate_error(exc) from exc


@app.post("/api/projects/{project_id}/retry")
async def retry_project(project_id: str, body: AdvanceRequest) -> dict[str, Any]:
    try:
        def execute() -> dict[str, Any]:
            store = _store(project_id)
            runner = _project_runner(store)
            store.retry(lambda state_name, snapshot: runner.run(snapshot, _options(body), only_state=state_name))
            return _project_view(store)

        return await asyncio.to_thread(execute)
    except Exception as exc:
        raise _translate_error(exc) from exc


@app.post("/api/projects/{project_id}/branches")
async def create_branch(project_id: str, body: BranchRequest) -> dict[str, Any]:
    try:
        def execute() -> dict[str, Any]:
            store = _store(project_id)
            with store.lock():
                branch = store.branch_from(body.checkpoint, name=body.name, mode=body.mode)
                pointer = store.manifest()["current_checkpoint"]
            return {"created": True, "project_id": store.project_id, "branch": branch,
                    "checkpoint_id": pointer["checkpoint_id"], "mode": body.mode}

        return await asyncio.to_thread(execute)
    except Exception as exc:
        raise _translate_error(exc) from exc


@app.get("/api/projects/{project_id}/branches")
async def list_project_branches(project_id: str) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(lambda: _store(project_id).branches())
    except Exception as exc:
        raise _translate_error(exc) from exc


@app.post("/api/projects/{project_id}/branches/switch")
async def switch_project_branch(project_id: str, body: BranchSwitchRequest) -> dict[str, Any]:
    try:
        def execute() -> dict[str, Any]:
            store = _store(project_id)
            with store.lock():
                return store.switch_branch(body.checkpoint_id)
        return await asyncio.to_thread(execute)
    except Exception as exc:
        raise _translate_error(exc) from exc


@app.get("/api/projects/{project_id}/timeline")
async def project_timeline(project_id: str, after: int = 0, limit: int = 100) -> dict[str, Any]:
    if after < 0 or not 1 <= limit <= 500:
        raise HTTPException(status_code=422, detail="游标或分页大小无效。")
    try:
        events = await asyncio.to_thread(lambda: _store(project_id).history())
        return _page(_redact(events), after, limit, cursor="sequence")
    except Exception as exc:
        raise _translate_error(exc) from exc


@app.get("/api/projects/{project_id}/timeline/events")
async def project_timeline_events(project_id: str, after: int = 0, limit: int = 100) -> StreamingResponse:
    page = await project_timeline(project_id, after, limit)
    async def stream():
        for event in page["items"]:
            yield f"id: {event['sequence']}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/projects/{project_id}/traces")
async def project_traces(project_id: str, after: int = 0, limit: int = 100) -> dict[str, Any]:
    if after < 0 or not 1 <= limit <= 500:
        raise HTTPException(status_code=422, detail="游标或分页大小无效。")
    try:
        def read() -> list[dict[str, Any]]:
            store = _store(project_id)
            records: list[dict[str, Any]] = []
            if store.prompts.path.exists():
                for sequence, line in enumerate(store.prompts.path.read_text(encoding="utf-8").splitlines(), 1):
                    if line.strip():
                        records.append({"sequence": sequence, **json.loads(line)})
            return _redact(records)
        return _page(await asyncio.to_thread(read), after, limit, cursor="sequence")
    except Exception as exc:
        raise _translate_error(exc) from exc


@app.get("/api/projects/{project_id}/settings/schema")
async def project_settings_schema(project_id: str) -> dict[str, Any]:
    try:
        store = _store(project_id)
        current = json.loads((store.root / "runtime_policy.json").read_text(encoding="utf-8"))["policy"]
        schema = RuntimePolicy.model_json_schema()
        properties = {name: schema["properties"][name] for name in RuntimePolicy.CONSUMERS
                      if name != "skill_invocation"}
        for name, consumer in RuntimePolicy.CONSUMERS.items():
            if name == "skill_invocation":
                continue
            properties[name]["consumer"] = consumer
            properties[name]["effect"] = "new_project_or_confirmed_revision"
        return {"schema_version": "1", "scope": "new_project_or_confirmed_revision",
                "risk": "修改现有工程会创建审计分支并使后续结果重新确认。",
                "properties": properties, "$defs": schema.get("$defs", {}), "current": current}
    except Exception as exc:
        raise _translate_error(exc) from exc

@app.post("/api/projects/{project_id}/policy")
async def revise_project_policy(project_id: str, body: PolicyRevisionRequest) -> dict[str, Any]:
    try:
        def execute():
            store = _store(project_id)
            policy = RuntimePolicy.model_validate(body.policy)
            with store.lock():
                branch = store.revise_policy(policy.snapshot(), confirmed=body.confirmed, actor=body.actor)
            return {"branch": branch, "project": _project_view(store)}
        return await asyncio.to_thread(execute)
    except Exception as exc:
        raise _translate_error(exc) from exc

@app.get("/api/projects/{project_id}/unknown-actions")
async def get_unknown_actions(project_id: str) -> dict[str, Any]:
    try:
        return {"items": await asyncio.to_thread(lambda: _gateway_for_store(_store(project_id)).unknown_actions())}
    except Exception as exc:
        raise _translate_error(exc) from exc

@app.post("/api/projects/{project_id}/unknown-actions/{idempotency_key}")
async def resolve_unknown_action(project_id: str, idempotency_key: str, body: UnknownResolutionRequest) -> dict[str, Any]:
    try:
        def execute():
            gateway = _gateway_for_store(_store(project_id))
            gateway.resolve_unknown(idempotency_key, body.action, body.actor)
            return {"items": gateway.unknown_actions()}
        return await asyncio.to_thread(execute)
    except Exception as exc:
        raise _translate_error(exc) from exc


@app.get("/api/projects/{project_id}/assets/{artifact_id}")
async def get_asset(project_id: str, artifact_id: str) -> FileResponse:
    """只允许读取当前工程 artifacts/images 下的受支持图片。"""
    if not re.fullmatch(r"[a-zA-Z0-9._-]{1,160}", artifact_id):
        raise HTTPException(status_code=422, detail="资源标识无效。")
    try:
        asset, record = _store(project_id).artifacts.resolve(artifact_id)
    except (FileNotFoundError, ValueError):
        raise HTTPException(status_code=404, detail="图片资源不存在。")
    if asset.stat().st_size > MAX_ASSET_BYTES:
        raise HTTPException(status_code=413, detail="图片超过 25 MiB 下载限制。")
    media_type = record["mime_type"]
    if media_type not in IMAGE_TYPES:
        raise HTTPException(status_code=415, detail="资源类型不受支持。")
    return FileResponse(asset, media_type=media_type, headers={"Cache-Control": "private, max-age=3600"})
