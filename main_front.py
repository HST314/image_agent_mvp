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

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_core.models import ImageTaskCard
from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
from calibrator.calibration_loop import ManualAction
from storage.project_store import ProjectStore

from configs.env_loader import load_dotenv  # 引入 .env 加载器
from configs.runtime_policy import RuntimePolicy
from skills.errors import ResourceError
from agent_core.jobs import JobRegistry
from agent_core.annotation import compose

load_dotenv(".env")  # 在程序启动时自动读取当前目录下的 .env 文件

APP_ROOT = Path(__file__).resolve().parent
FRONTEND_ROOT = APP_ROOT / "frontend"
PROJECTS_ROOT = Path(os.getenv("IMAGE_AGENT_FRONT_PROJECTS_ROOT", FRONTEND_ROOT / "data" / "projects")).resolve()
MODEL_CONFIG = Path(os.getenv("IMAGE_AGENT_MODEL_CONFIG", APP_ROOT / "configs" / "model_config.yaml")).resolve()
MAX_REQUEST_BYTES = 512 * 1024
MAX_ASSET_BYTES = 25 * 1024 * 1024
PROJECT_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,63}$")
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}

app = FastAPI(
    title="Image Agent Studio",
    description="生产 Image Agent 的 Web 薄适配接口",
    version="1.0.0",
)
JOBS = JobRegistry(PROJECTS_ROOT / ".jobs")


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateProjectRequest(StrictRequest):
    project_id: str = Field(min_length=2, max_length=64)
    task_card: dict[str, Any]
    offline: bool = False


class AdvanceRequest(StrictRequest):
    selected_id: str | None = Field(default=None, max_length=128)
    clarification_answers: dict[str, Any] | None = None
    edited_markdown: str | None = Field(default=None, max_length=100_000)
    manual_action: Literal["execute", "edit_and_execute", "skip", "end", "accept_current"] | None = None
    edited_delta: str | None = Field(default=None, max_length=4_000)
    human_prompt: str | None = Field(default=None, max_length=8_000)
    final_approved: bool = False
    offline: bool = False
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)

class AnnotationRequest(StrictRequest):
    artifact_id: str = Field(pattern=r"^[a-zA-Z0-9._-]{1,160}$")
    marks: list[dict[str, Any]] = Field(min_length=1, max_length=5000)
    prompt: str = Field(min_length=1, max_length=8_000)
    offline: bool = False

class QualityDispositionRequest(StrictRequest):
    action: Literal["add_rounds_with_cost_confirmation", "human_tune_best", "abandon"]
    additional_rounds: int = Field(default=0, ge=0, le=20)
    cost_confirmed: bool = False


class BranchRequest(StrictRequest):
    checkpoint: str = Field(min_length=1, max_length=256)
    name: str | None = Field(default=None, min_length=2, max_length=64)

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
    return await call_next(request)


def _safe_project_id(value: str) -> str:
    value = value.strip()
    if not PROJECT_ID.fullmatch(value):
        raise HTTPException(status_code=422, detail="工程 ID 仅允许 2–64 位字母、数字、下划线和连字符。")
    return value


def _store(project_id: str) -> ProjectStore:
    return ProjectStore(PROJECTS_ROOT, _safe_project_id(project_id))


def _runner(store: ProjectStore, offline: bool) -> WorkflowRunner:
    if not MODEL_CONFIG.is_file():
        raise RuntimeError("模型配置文件不存在，请设置 IMAGE_AGENT_MODEL_CONFIG。")
    return WorkflowRunner(store, MODEL_CONFIG, offline_mode=offline)


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
    )


def _project_view(store: ProjectStore) -> dict[str, Any]:
    manifest = store.manifest()
    snapshot = store.resume() or {}
    return {
        "project_id": store.project_id,
        "manifest": manifest,
        "snapshot": snapshot,
        "history": store.history(),
        "capabilities": _capabilities(manifest, snapshot),
        "resource_events": [e for e in store.history() if e.get("type") == "resource_degraded"],
        "unknown_actions": _gateway_for_store(store).unknown_actions(),
        "runtime_policy": json.loads((store.root / "runtime_policy.json").read_text(encoding="utf-8"))["policy"],
    }

def _gateway_for_store(store: ProjectStore):
    policy = RuntimePolicy.model_validate(json.loads((store.root / "runtime_policy.json").read_text(encoding="utf-8"))["policy"])
    return _runner(store, policy.offline_mode).gateway


def _capabilities(manifest: dict[str, Any], snapshot: dict[str, Any]) -> list[str]:
    """仅把生产快照已有等待原因映射为 UI 动作，不执行或替代状态迁移。"""
    if manifest.get("failed_step"):
        return ["retry"]
    if snapshot.get("completed"):
        return ["inspect", "branch"]
    phase = snapshot.get("phase")
    if phase == "waiting_clarification":
        return ["answer_clarification"]
    if phase == "waiting_master_selection":
        return ["select_master"]
    if phase == "waiting_human_approval":
        return ["review_calibration"]
    if phase == "waiting_reinspection":
        return ["resume"]
    if snapshot:
        return ["resume", "branch"]
    return []


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, ValidationError):
        return HTTPException(status_code=422, detail=exc.errors(include_url=False))
    if isinstance(exc, ResourceError):
        return HTTPException(status_code=503, detail=exc.as_dict())
    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return HTTPException(status_code=404, detail="工程或资源不存在。")
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


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(FRONTEND_ROOT / "index.html", media_type="text/html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "model_config_available": MODEL_CONFIG.is_file(),
        "projects_root": str(PROJECTS_ROOT),
    }


@app.get("/api/projects")
async def list_projects() -> dict[str, Any]:
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    projects: list[dict[str, Any]] = []
    for child in sorted(PROJECTS_ROOT.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
        if not child.is_dir() or not PROJECT_ID.fullmatch(child.name) or not (child / "manifest.json").is_file():
            continue
        try:
            view = _project_view(ProjectStore(PROJECTS_ROOT, child.name))
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
            _runner(store, body.offline).run({"task_card": task.model_dump(mode="json")}, RunnerOptions())
            return _project_view(store)

        return await asyncio.to_thread(execute)
    except Exception as exc:
        raise _translate_error(exc) from exc


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(_project_view, _store(project_id))
    except Exception as exc:
        raise _translate_error(exc) from exc


@app.post("/api/projects/{project_id}/advance")
async def advance_project(project_id: str, body: AdvanceRequest) -> dict[str, Any]:
    try:
        def execute() -> dict[str, Any]:
            store = _store(project_id)
            snapshot = store.resume()
            if snapshot is None:
                raise ValueError("工程还没有可恢复节点。")
            _runner(store, body.offline).run(snapshot, _options(body))
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
        store = _store(project_id); snapshot = store.resume()
        if snapshot is None: raise ValueError("工程还没有可恢复节点。")
        _runner(store, body.offline).run(snapshot, _options(body))
        return {"project_id":project_id}
    try:
        job, created = JOBS.submit(project_id, key, "advance", execute)
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
            store=_store(project_id); source, source_record=store.artifacts.resolve(body.artifact_id)
            guide_bytes=compose(source.read_bytes(), body.marks)
            guide=store.artifacts.save_bytes(guide_bytes, metadata={"kind":"annotation_guide","parent_artifact_id":body.artifact_id,"marks":body.marks})
            result=_runner(store, body.offline)._image_call("human_prompt_rework", body.prompt, [guide["uri"]])
            store.events.append("human_annotation_rework", parent_asset=source_record, guide_asset=guide, asset=result, prompt=body.prompt)
            return {"parent_asset":source_record,"guide_asset":guide,"asset":result,"requires_reinspection":True}
        return await asyncio.to_thread(execute)
    except Exception as exc: raise _translate_error(exc) from exc

@app.post("/api/projects/{project_id}/quality-disposition")
async def quality_disposition(project_id: str, body: QualityDispositionRequest) -> dict[str, Any]:
    """Record the explicit human route after automatic inspection is exhausted."""
    try:
        def execute():
            store=_store(project_id); snapshot=store.resume() or {}
            if snapshot.get("phase") != "waiting_human_approval" or "round_limit" not in str(snapshot.get("termination_reason")):
                raise ValueError("QUALITY_LIMIT_NOT_REACHED")
            if body.action=="add_rounds_with_cost_confirmation" and (not body.cost_confirmed or body.additional_rounds<1):
                raise ValueError("COST_CONFIRMATION_REQUIRED")
            asset=snapshot.get("best_asset") or snapshot.get("asset")
            store.events.append("quality_disposition", action=body.action, additional_rounds=body.additional_rounds,
                                cost_confirmed=body.cost_confirmed, selected_asset=asset)
            updated=dict(snapshot)
            if body.action=="abandon":
                updated.update(phase="terminated_without_delivery",calibration_status="terminated_without_delivery",
                               termination_satisfied=False,termination_reason="human_abandoned_after_limit")
                status_value="abandoned"
            elif body.action=="human_tune_best":
                updated.update(asset=asset,current_asset=asset,phase="waiting_human_tune",calibration_status="waiting_human_tune",
                               termination_satisfied=False,latest_checked_asset_hash=None,inspection=None)
                status_value="waiting_human_tune"
            else:
                policy=dict(updated.get("self_check_policy") or updated.get("selected_policy") or {})
                policy["termination"]="solo"; policy["max_rounds"]=int(snapshot.get("round",0))+body.additional_rounds
                policy["fixed_rounds"]=min(int(policy.get("fixed_rounds",1)),policy["max_rounds"])
                updated.update(asset=asset,current_asset=asset,phase="additional_rounds_approved",calibration_status="pending",
                               self_check_policy=policy,round=int(snapshot.get("round",0))+1)
                status_value="additional_rounds_approved"
            with store.lock(): store.checkpoint("self_check_iteration",updated)
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


@app.post("/api/projects/{project_id}/retry")
async def retry_project(project_id: str, body: AdvanceRequest) -> dict[str, Any]:
    try:
        def execute() -> dict[str, Any]:
            store = _store(project_id)
            runner = _runner(store, body.offline)
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
            if body.name:
                _safe_project_id(body.name)
            with store.lock():
                store.branch_from(body.checkpoint, name=body.name)
            return _project_view(store)

        return await asyncio.to_thread(execute)
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
