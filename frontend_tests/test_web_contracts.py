"""T32 跨端契约测试：任务书确认/失效必须可经 Web API 完成（advance 透传
task_approved 与 actor 到生产 RunnerOptions）。"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main_front

pytestmark = pytest.mark.usefixtures("offline_frontend_runtime")

# 任务文本需命中品类库记录名（适配器按精确出现计分），沿用验收测试的"广告/海报"语料。
TASK = {
    "task_id": "task-web-contract",
    "project_id": "web-contract",
    "source_refs": [{"ref_id": "brief-1", "ref_type": "brief", "excerpt": "广告海报", "source_hash": None}],
    "deliverable_goal": "广告 海报",
    "usage_context": "内部审核",
    "category_ref": {"category_id": "generic", "version": "1"},
    "known_facts": {"audience": "审核人员"},
    "unknowns": {},
    "asset_inputs": [],
    "status": "draft",
}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path / "projects")
    return TestClient(main_front.app, raise_server_exceptions=False)


def _create_with_fake_provider(client: TestClient) -> dict:
    response = client.post(
        "/api/projects", json={"project_id": "web-contract", "task_card": TASK}
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_taskbook_approval_roundtrip_via_web(client: TestClient) -> None:
    view = _create_with_fake_provider(client)
    snapshot = view["snapshot"]
    # 离线无澄清问题，直接停在任务书确认门。
    assert snapshot["state"] == "confirmation_build"
    assert snapshot["phase"] == "waiting_human_approval"
    assert not snapshot.get("task_approval")

    approved = client.post(
        "/api/projects/web-contract/advance",
        json={"task_approved": True, "actor": "web-user"},
    )
    assert approved.status_code == 200, approved.text
    snap = approved.json()["snapshot"]
    # 确认后已推进过付费门禁状态：候选生成完成并等待主图选择。
    assert snap["phase"] == "waiting_master_selection", snap.get("phase")
    assert snap["task_approval"]["actor"] == "web-user"
    assert snap["task_approval"]["revision_hash"] == snap["task_revision"]["revision_hash"]
    assert len(snap["candidates"]) == 5


def test_edited_markdown_at_confirmation_stage_invalidates_approval(client: TestClient) -> None:
    view = _create_with_fake_provider(client)
    markdown = view["snapshot"]["task_markdown"]
    edited = client.post(
        "/api/projects/web-contract/advance",
        json={"edited_markdown": markdown + "\n- 补充事实：主色为品牌紫\n"},
    )
    assert edited.status_code == 200, edited.text
    snap = edited.json()["snapshot"]
    # 仍在确认门：新修订产生、确认状态为空（编辑后确认失效）。
    assert snap["state"] == "confirmation_build"
    assert snap["phase"] == "waiting_human_approval"
    assert snap.get("task_approval") in (None, {})
    assert snap["task_revision"]["revision_hash"] != view["snapshot"]["task_revision"]["revision_hash"]
    assert len(snap["task_revision_history"]) >= 2


def test_markdown_edit_routing_boundary_after_approval(client: TestClient) -> None:
    """记录 v1.7.3 路由边界：确认门之后 advance 不会回流任务书状态，编辑被忽略。
    若验收要求"确认后仍可修订任务书"，需要后端在 T13/T14 域补充回灌路由。"""
    view = _create_with_fake_provider(client)
    approved = client.post(
        "/api/projects/web-contract/advance",
        json={"task_approved": True, "actor": "web-user"},
    )
    assert approved.status_code == 200, approved.text
    rev = approved.json()["snapshot"]["task_revision"]["revision_hash"]
    edited = client.post(
        "/api/projects/web-contract/advance",
        json={"edited_markdown": approved.json()["snapshot"]["task_markdown"] + "\n- 补充事实：x\n"},
    )
    assert edited.status_code == 200, edited.text
    snap = edited.json()["snapshot"]
    assert snap["state"] == "master_candidate_selection"
    assert snap["task_revision"]["revision_hash"] == rev


def test_task_approval_requires_actor(client: TestClient) -> None:
    _create_with_fake_provider(client)
    response = client.post(
        "/api/projects/web-contract/advance",
        json={"task_approved": True},
    )
    assert response.status_code == 200, response.text
    snap = response.json()["snapshot"]
    # 无 actor 的确认不生效：仍停在确认门。
    assert snap["phase"] == "waiting_human_approval"
    assert not snap.get("task_approval")
