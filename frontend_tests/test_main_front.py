"""FastAPI 薄适配层的契约与安全测试。"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import main_front
from agent_core.delivery import build_delivery


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path / "projects")
    return TestClient(main_front.app, raise_server_exceptions=False)


def test_health_and_frontend_are_served(client: TestClient) -> None:
    assert client.get("/api/health").json()["status"] == "ok"
    page = client.get("/")
    assert page.status_code == 200
    # T11 后首屏品牌与标题已中文化（契约 §11）。
    assert "Image Agent 创作工作台" in page.text
    # 样式已模块化到静态资源（T35）；可访问性媒体查询随样式表一起提供。
    assert '/static/css/main.css' in page.text
    css = client.get("/static/css/main.css")
    assert css.status_code == 200
    assert "prefers-reduced-motion" in css.text


def test_managed_mode_removes_duplicate_creation_and_rejects_direct_posts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(main_front, "MANAGED_MODE", True)
    monkeypatch.setattr(main_front, "MANAGED_PROJECT_ID", "managed-project")
    monkeypatch.setattr(main_front, "MANAGED_ADAPTER_KEY", "managed-adapter-key-for-tests-12345")
    managed_client = TestClient(main_front.app, raise_server_exceptions=False)
    page = managed_client.get("/")
    assert page.status_code == 200
    assert 'id="new-button"' not in page.text
    assert 'id="project-form"' not in page.text
    context = managed_client.get("/api/runtime-context").json()
    assert context == {"managed_by_harness": True, "project_id": "managed-project"}

    direct = managed_client.post(
        "/api/projects",
        json={"project_id": "managed-project", "task_card": {}, "offline": True},
    )
    assert direct.status_code == 409
    assert direct.json()["detail"]["code"] == "MANAGED_BY_HARNESS"


def test_managed_creation_requires_adapter_header_loopback_and_exact_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(main_front, "MANAGED_MODE", True)
    monkeypatch.setattr(main_front, "MANAGED_PROJECT_ID", "managed-project")
    monkeypatch.setattr(main_front, "MANAGED_ADAPTER_KEY", "managed-adapter-key-for-tests-12345")
    real_ip_address = main_front.ipaddress.ip_address
    monkeypatch.setattr(
        main_front.ipaddress,
        "ip_address",
        lambda _value: real_ip_address("127.0.0.1"),
    )
    managed_client = TestClient(main_front.app, raise_server_exceptions=False)
    task = {
        "task_id": "managed-project",
        "project_id": "managed-project",
        "source_refs": [{"ref_id": "brief-1", "ref_type": "brief", "excerpt": "测试输入", "source_hash": None}],
        "deliverable_goal": "生成受管视觉图",
        "usage_context": "内部审核",
        "category_ref": {"category_id": "generic", "version": "1"},
        "known_facts": {},
        "unknowns": {},
        "asset_inputs": [],
        "status": "draft",
    }
    rejected = managed_client.post(
        "/api/managed/projects",
        json={"project_id": "managed-project", "task_card": task, "offline": True},
    )
    assert rejected.status_code == 403
    assert rejected.json()["detail"]["code"] == "MANAGED_BY_HARNESS"

    created = managed_client.post(
        "/api/managed/projects",
        headers={main_front.MANAGED_ADAPTER_HEADER: "managed-adapter-key-for-tests-12345"},
        json={
            "project_id": "managed-project",
            "task_card": task,
            "offline": True,
            "defer_run": True,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["project_id"] == "managed-project"


@pytest.mark.parametrize("project_id", ["../escape", "a", "含中文", "bad/id"])
def test_project_id_rejects_path_traversal(client: TestClient, project_id: str) -> None:
    response = client.get(f"/api/projects/{project_id}")
    assert response.status_code in {404, 422}


def test_oversized_request_is_rejected_before_json_parse(client: TestClient) -> None:
    response = client.post(
        "/api/projects",
        content=b"x" * (main_front.MAX_REQUEST_BYTES + 1),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413


def test_unknown_project_returns_real_not_found(client: TestClient) -> None:
    response = client.get("/api/projects/not-created")
    assert response.status_code == 404
    assert "不存在" in response.json()["detail"]


def test_asset_endpoint_hides_unindexed_file(client: TestClient) -> None:
    store = main_front.ProjectStore(main_front.PROJECTS_ROOT, "safe-project")
    store.create()
    asset_dir = store.root / "artifacts" / "images"
    asset_dir.mkdir(parents=True)
    (asset_dir / "note.txt").write_text("not an image", encoding="utf-8")
    response = client.get("/api/projects/safe-project/assets/note.txt")
    # Files that were not admitted to the content-addressed asset index are
    # deliberately indistinguishable from missing assets.
    assert response.status_code == 404
    assert response.json()["detail"] == "图片资源不存在。"


def test_offline_project_stops_at_a_real_waiting_checkpoint(client: TestClient) -> None:
    task = {
        "task_id": "task-web-test",
        "project_id": "web-test",
        "source_refs": [{"ref_id": "brief-1", "ref_type": "brief", "excerpt": "测试创作输入", "source_hash": None}],
        "deliverable_goal": "生成一张用于内部审核的极简产品视觉图",
        "usage_context": "内部审核",
        "category_ref": {"category_id": "generic_visual_delivery", "version": "1.0"},
        "known_facts": {"audience": "审核人员"},
        "unknowns": {"output_spec": "待确认"},
        "asset_inputs": [],
        "status": "draft",
    }
    response = client.post("/api/projects", json={"project_id": "web-test", "task_card": task, "offline": True})
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["snapshot"]["state"] == "intake_clarify"
    assert data["manifest"]["current_checkpoint"]["sequence"] == 2
    assert data["snapshot"].get("completed") is not True


def test_finalize_delivery_persists_image_and_markdown_idempotently(client: TestClient, tmp_path: Path) -> None:
    store = main_front.ProjectStore(main_front.PROJECTS_ROOT, "delivery-project")
    store.create()
    image_bytes = io.BytesIO()
    Image.new("RGB", (32, 24), "purple").save(image_bytes, "PNG")
    asset = store.artifacts.save_bytes(image_bytes.getvalue(), metadata={"kind": "final"})
    snapshot = {
        "completed": True,
        "task_card": {"task_id": "task-delivery", "deliverable_goal": "活动主视觉"},
        "style_selections": [{"mechanism": "清晰的中心构图", "reason": "突出主题", "task_fit": "线上活动"}],
        "final_asset": asset,
        "frozen_delivery": {"asset_sha256": asset["sha256"]},
    }
    envelope = build_delivery(snapshot, "delivery-project", asset, f"project:delivery-project:asset:{asset['sha256']}")
    snapshot["delivery_envelope"] = envelope.model_dump(mode="json")
    store.checkpoint("final_approval", snapshot)

    first = client.post("/api/projects/delivery-project/delivery/finalize")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["finalized"] is True
    assert body["asset_sha256"] == asset["sha256"]
    assert set(body["files"]) == {"image", "markdown", "json"}
    for relative in body["files"].values():
        assert (store.root / relative).is_file()
    assert (store.root / body["files"]["image"]).read_bytes() == image_bytes.getvalue()
    assert "最终设计说明" in (store.root / body["files"]["markdown"]).read_text(encoding="utf-8")

    second = client.post("/api/projects/delivery-project/delivery/finalize")
    assert second.status_code == 200
    assert second.json()["files"] == body["files"]
    assert second.json()["finalized_at"] == body["finalized_at"]
    events = [event for event in store.history() if event.get("type") == "delivery_finalized"]
    assert len(events) == 1

    view = client.get("/api/projects/delivery-project")
    assert view.status_code == 200
    assert view.json()["delivery_status"]["asset_sha256"] == asset["sha256"]
