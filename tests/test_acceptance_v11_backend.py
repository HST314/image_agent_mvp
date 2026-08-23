from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

import main_front
from configs.runtime_policy import RuntimePolicy
from storage.project_store import ProjectStore


def _project(root: Path, project_id: str = "history_demo") -> tuple[ProjectStore, str]:
    store = ProjectStore(root, project_id)
    store.create(RuntimePolicy(offline_mode=True).snapshot())
    first = store.checkpoint("confirmation_build", {"state": "confirmation_build", "phase": "waiting"})
    store.events.append("model_call_started", trace_id="trace_safe", state="render", token="must-not-leak")
    store.prompts.begin({
        "messages": [{"role": "user", "content": "draw"}], "template_id": "render",
        "template_version": "1", "template_hash": "h", "variables": {"api_key": "secret"},
        "input_refs": [], "model": {"name": "offline"}, "parameters": {}, "config_hash": "c",
        "state": "render", "trace_id": "trace_safe",
    })
    return store, first


def test_t28_branch_list_switch_and_reopen_are_checkpoint_id_only(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path)
    store, first = _project(tmp_path)
    client = TestClient(main_front.app)

    reopened = client.post(f"/api/projects/{store.project_id}/branches", json={"checkpoint": first, "name": "revision-1"})
    assert reopened.status_code == 200
    listing = client.get(f"/api/projects/{store.project_id}/branches").json()
    assert listing["current_branch"] == "revision-1"
    assert {item["name"] for item in listing["items"]} == {"main", "revision-1"}
    assert all("checkpoints" in item for item in listing["items"])

    switched = client.post(f"/api/projects/{store.project_id}/branches/switch", json={"checkpoint_id": first})
    assert switched.status_code == 200
    assert switched.json()["current_branch"] == "main"
    assert client.post(f"/api/projects/{store.project_id}/branches/switch", json={"checkpoint_id": "../x"}).status_code == 422

    other, _ = _project(tmp_path, "other_project")
    foreign = other.checkpoint("foreign", {"state": "foreign", "project": "other_project"})
    assert other.project_id
    assert client.post(f"/api/projects/{store.project_id}/branches/switch", json={"checkpoint_id": foreign}).status_code == 409

    store.switch_branch(first)
    older = first
    newer = store.checkpoint("newer", {"state": "newer"})
    assert newer != older
    blocked = client.post(f"/api/projects/{store.project_id}/branches/switch", json={"checkpoint_id": older})
    assert blocked.status_code == 409 and "历史节点只读" in blocked.text


def test_t9_progress_snapshots_follow_active_lineage_and_accept_auto_chinese_name(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path)
    store = ProjectStore(tmp_path, "snapshot_demo")
    store.create(RuntimePolicy(offline_mode=True).snapshot())
    intake = store.checkpoint("intake_clarify", {"state": "intake_clarify", "task_card": {"deliverable_goal": "海报"}})
    taskbook = store.checkpoint("confirmation_build", {"state": "confirmation_build", "task_markdown": "# 创作任务书"})
    store.checkpoint("initial_candidate_generation", {"state": "initial_candidate_generation", "candidates": []})
    client = TestClient(main_front.app)

    created = client.post(
        f"/api/projects/{store.project_id}/branches",
        json={"checkpoint": taskbook, "name": "任务书-0812-090705", "mode": "fork_after"},
    )
    assert created.status_code == 200
    assert created.json()["manifest"]["current_branch"] == "任务书-0812-090705"
    assert created.json()["manifest"]["current_checkpoint"]["branch"] == "任务书-0812-090705"
    assert created.json()["snapshot"]["state"] == "confirmation_build"
    refreshed = client.get(f"/api/projects/{store.project_id}").json()
    assert refreshed["manifest"]["current_branch"] == "任务书-0812-090705"
    snapshots = refreshed["progress_snapshots"]
    assert [item["state"] for item in snapshots] == [
        "intake_clarify", "confirmation_build", "confirmation_build",
    ]
    assert snapshots[0]["checkpoint_id"] == intake
    assert snapshots[-1]["branch"] == "任务书-0812-090705"
    assert snapshots[-1]["snapshot"]["task_markdown"] == "# 创作任务书"

    invalid = client.post(
        f"/api/projects/{store.project_id}/branches",
        json={"checkpoint": taskbook, "name": "../逃逸"},
    )
    assert invalid.status_code == 422


def test_t29_t30_timeline_cursor_sse_and_trace_redaction(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path)
    store, _ = _project(tmp_path)
    client = TestClient(main_front.app)

    first = client.get(f"/api/projects/{store.project_id}/timeline", params={"after": 0, "limit": 2}).json()
    assert len(first["items"]) == 2 and first["next_cursor"] == first["items"][-1]["sequence"]
    second = client.get(f"/api/projects/{store.project_id}/timeline", params={"after": first["next_cursor"], "limit": 100}).json()
    assert all(item["sequence"] > first["next_cursor"] for item in second["items"])

    sse = client.get(f"/api/projects/{store.project_id}/timeline/events", params={"after": first["next_cursor"], "limit": 100})
    assert sse.status_code == 200 and "text/event-stream" in sse.headers["content-type"]
    assert [json.loads(line[6:]) for line in sse.text.splitlines() if line.startswith("data: ")] == second["items"]

    traces = client.get(f"/api/projects/{store.project_id}/traces", params={"after": 0, "limit": 100})
    assert traces.status_code == 200
    raw = json.dumps(traces.json())
    assert "secret" not in raw and "must-not-leak" not in raw
    assert "trace_safe" in raw


def test_usage_observation_api_is_paginated_and_secret_free(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path)
    store, _ = _project(tmp_path, "usage-api")
    store.events.append(
        "model_usage_recorded", usage_id="usage_one", request_id="local-one",
        provider_request_id="provider-one", provider="ark", model="reasoner",
        call_type="reasoning_llm", usage_basis="tokens",
        token_usage={"input_tokens": 7, "output_tokens": 3, "cached_input_tokens": 1,
                     "reasoning_tokens": 2, "total_tokens": 10},
        billing_units=[], raw_usage={
            "prompt_tokens": 7,
            "completion_tokens": 3,
            "total_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 1, "private_key": "hidden"},
            "password": "plain-password-value",
            "token": "plain-token-value",
        },
        api_key="must-not-cross",
    )
    client = TestClient(main_front.app)

    first = client.get(f"/api/projects/{store.project_id}/usage", params={"after": 0, "limit": 1})
    assert first.status_code == 200
    body = first.json()
    assert len(body["items"]) == 1 and body["has_more"] is False
    assert body["items"][0]["token_usage"]["total_tokens"] == 10
    assert body["items"][0]["raw_usage"] == {
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "total_tokens": 10,
        "prompt_tokens_details": {"cached_tokens": 1},
    }
    assert "must-not-cross" not in json.dumps(body)
    assert "plain-password-value" not in json.dumps(body)
    assert "plain-token-value" not in json.dumps(body)


def test_private_configuration_routes_are_removed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path)
    store, _ = _project(tmp_path, "immutable-policy")
    before = (store.root / "runtime_policy.json").read_bytes()
    client = TestClient(main_front.app)

    assert client.get("/api/settings/schema").status_code == 404
    assert client.get("/api/settings/models").status_code == 404
    assert client.post("/api/settings/policy", json={}).status_code == 404
    assert client.get(f"/api/projects/{store.project_id}/settings/schema").status_code == 404
    assert client.post(f"/api/projects/{store.project_id}/policy", json={}).status_code == 404
    assert (store.root / "runtime_policy.json").read_bytes() == before
