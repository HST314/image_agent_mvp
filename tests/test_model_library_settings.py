"""模型库 + 设置页模型绑定：能力匹配、原子改写与 HTTP 契约。"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import main_front
from model_router.library import (
    apply_bindings,
    load_config,
    load_library,
    settings_view,
    write_model_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
LIBRARY_PATH = REPO_ROOT / "configs" / "model_library.yaml"
CONFIG_PATH = REPO_ROOT / "configs" / "model_config.yaml"


def test_shipped_library_covers_every_required_role() -> None:
    """仓库自带的模型库：每个阶段所需角色在对应分组里至少有一个备选。"""
    library = load_library(LIBRARY_PATH)
    config = load_config(CONFIG_PATH)
    view = settings_view(library, config)
    assert {entry["id"] for entry in view["library"]["text_models"]}
    assert {entry["id"] for entry in view["library"]["vlm_models"]}
    assert {entry["id"] for entry in view["library"]["image_models"]}
    for state in view["states"]:
        assert state["binding"] is not None, state["state"]
        # 当前绑定必须能在所属分组中反查到（设置页下拉才能选中）。
        group = view["library"][state["group"]]
        assert any(
            entry["provider"] == state["binding"]["provider"] and entry["model"] == state["binding"]["model"]
            for entry in group
        ), state["state"]


def test_apply_bindings_enforces_capability_match(tmp_path: Path) -> None:
    library = load_library(LIBRARY_PATH)
    config = load_config(CONFIG_PATH)
    image_entry = library.image_models[0].id

    # 文生图模型不能绑到需求澄清（文本）阶段。
    with pytest.raises(ValueError, match="只能绑定"):
        apply_bindings(library, config, {"intake_clarify": image_entry})
    # 未知阶段与未知条目同样拒绝。
    with pytest.raises(ValueError, match="未知的工作流阶段"):
        apply_bindings(library, config, {"not_a_state": image_entry})
    with pytest.raises(ValueError, match="只能绑定"):
        apply_bindings(library, config, {"self_check_inspection": "no-such-model"})


def test_apply_bindings_switch_adopts_library_parameters(tmp_path: Path) -> None:
    library = load_library(LIBRARY_PATH)
    config = load_config(CONFIG_PATH)
    text_entry = library.text_models[0]

    updated = apply_bindings(library, config, {"self_check_rework": library.image_models[0].id})
    binding = next(b for b in updated.state_bindings if b.state == "self_check_rework")
    assert binding.model == library.image_models[0].model
    assert binding.model_role.value == "text_to_image_model"
    # 未改动的阶段保持原绑定。
    intact = next(b for b in updated.state_bindings if b.state == "intake_clarify")
    assert intact.model == text_entry.model

    # 原子改写后可重新加载，且阶段顺序完整。
    target = tmp_path / "model_config.yaml"
    write_model_config(target, updated)
    reloaded = load_config(target)
    assert {b.state for b in reloaded.state_bindings} == {b.state for b in config.state_bindings}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path / "projects")
    config_copy = tmp_path / "model_config.yaml"
    config_copy.write_text(CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(main_front, "MODEL_CONFIG", config_copy)
    monkeypatch.setattr(main_front, "MODEL_LIBRARY", LIBRARY_PATH)
    return TestClient(main_front.app, raise_server_exceptions=False)


def test_model_settings_get_returns_library_and_bindings(client: TestClient) -> None:
    data = client.get("/api/settings/models").json()
    assert data["library"]["text_models"]
    states = {state["state"]: state for state in data["states"]}
    assert states["intake_clarify"]["group"] == "text_models"
    assert states["initial_candidate_generation"]["group"] == "image_models"
    assert states["self_check_inspection"]["group"] == "vlm_models"


def test_model_settings_post_rewrites_config_with_capability_check(client: TestClient) -> None:
    library = load_library(LIBRARY_PATH)
    image_id = library.image_models[0].id

    # 能力不匹配：409（ValueError 契约，与策略修订端点一致）。
    bad = client.post("/api/settings/models", json={
        "bindings": {"confirmation_build": image_id}, "actor": "tester", "confirmed": True,
    })
    assert bad.status_code == 409

    # 合法绑定：保存后 GET 立即可见（热加载语义）。
    ok = client.post("/api/settings/models", json={
        "bindings": {"initial_candidate_generation": image_id}, "actor": "tester", "confirmed": True,
    })
    assert ok.status_code == 200
    binding = next(s for s in ok.json()["states"] if s["state"] == "initial_candidate_generation")
    assert binding["binding"]["model"] == library.image_models[0].model
    persisted = yaml.safe_load((main_front.MODEL_CONFIG).read_text(encoding="utf-8"))
    persisted_binding = next(b for b in persisted["state_bindings"] if b["state"] == "initial_candidate_generation")
    assert persisted_binding["model"] == library.image_models[0].model

    # 未确认拒绝（PermissionError 契约，与策略修订端点一致）。
    rejected = client.post("/api/settings/models", json={
        "bindings": {"initial_candidate_generation": image_id}, "actor": "tester", "confirmed": False,
    })
    assert rejected.status_code in {409, 503}


def test_managed_instance_rejects_model_config_writeback(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = main_front.MODEL_CONFIG.read_bytes()
    monkeypatch.setattr(main_front, "MANAGED_MODE", True)

    response = client.post(
        "/api/settings/models",
        json={
            "bindings": {"intake_clarify": "unused"},
            "actor": "tester",
            "confirmed": True,
        },
    )

    assert response.status_code == 403
    assert main_front.MODEL_CONFIG.read_bytes() == before
