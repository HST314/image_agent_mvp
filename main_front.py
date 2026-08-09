"""Image Agent 的 FastAPI Web 薄适配层。

本文件只负责 HTTP 输入校验、调用生产 WorkflowRunner / ProjectStore，以及把
文件型快照转换成前端可消费的视图；工作流判断和图片生成均由现有后端完成。
"""
from __future__ import annotations

import asyncio
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_core.models import ImageTaskCard
from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
from calibrator.calibration_loop import ManualAction
from storage.project_store import ProjectStore

from configs.env_loader import load_dotenv  # 引入 .env 加载器

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


class BranchRequest(StrictRequest):
    checkpoint: str = Field(min_length=1, max_length=256)
    name: str | None = Field(default=None, min_length=2, max_length=64)


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
    }


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
            store.create()
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


@app.get("/api/projects/{project_id}/assets/{artifact_id}")
async def get_asset(project_id: str, artifact_id: str) -> FileResponse:
    """只允许读取当前工程 artifacts/images 下的受支持图片。"""
    if not re.fullmatch(r"[a-zA-Z0-9._-]{1,160}", artifact_id):
        raise HTTPException(status_code=422, detail="资源标识无效。")
    project_root = _store(project_id).root.resolve()
    asset = (project_root / "artifacts" / "images" / artifact_id).resolve()
    allowed_root = (project_root / "artifacts" / "images").resolve()
    if allowed_root not in asset.parents or not asset.is_file():
        raise HTTPException(status_code=404, detail="图片资源不存在。")
    if asset.stat().st_size > MAX_ASSET_BYTES:
        raise HTTPException(status_code=413, detail="图片超过 25 MiB 下载限制。")
    media_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
    if media_type not in IMAGE_TYPES:
        raise HTTPException(status_code=415, detail="资源类型不受支持。")
    return FileResponse(asset, media_type=media_type, headers={"Cache-Control": "private, max-age=3600"})
