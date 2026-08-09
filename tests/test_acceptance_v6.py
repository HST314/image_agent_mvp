import base64
import json
from pathlib import Path

import pytest

from agent_core.models import (AppliesWhen, CategorySkill, ConfirmedFact, PromptInjection,
                               SignStatus, TaskConfirmationDoc)
from agent_core.style_pipeline import StyleRenderPlanner, assert_reference_isolated
from agent_core.unified_workflow import (DomainState, freeze_delivery, recovery_actions,
                                         require_transition, revise_task)
from skills.style_library import (StyleExtraction, StyleExtractor, StyleLibrary, StyleLibraryError,
                                  StyleRecord, safe_render_supplement, select_five)
from skills.style_library_cli import build_index

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


def make_library(root: Path, count=5):
    (root / "styles").mkdir(parents=True)
    for n in range(count):
        folder = root / "styles" / f"CW-HA-{n+1:03}"
        folder.mkdir()
        (folder / "image.png").write_bytes(PNG + bytes([n]))  # distinct hashes; decoder ignores trailing byte
        (folder / "style.json").write_text(json.dumps({"style_id":f"CW-HA-{n+1:03}","image":"image.png",
            "title":f"style {n}","describe":"brand history","tags":["brand"],"task_fit":["history"]}), "utf-8")
    (root / "library.json").write_text(json.dumps({"schema_version":"1.0","library_id":"x","version":"1","style_count":0}), "utf-8")
    return build_index(root)


def extraction(style, n=0):
    return StyleExtraction(extraction_key=f"key{n}", style_id=style.style_id, image_sha256=style.sha256,
        model_id="vlm", prompt_version="v1", status="success", composition=f"comp {n}", material=f"mat {n}",
        lighting=f"light {n}", narrative=f"story {n}", graphic_language=f"graphic {n}", color=f"color {n}",
        prompt_supplement=f"abstract {n}")


def test_t18_t19_schema_import_validation_and_legacy_isolation(tmp_path):
    rows = make_library(tmp_path)
    assert len(StyleLibrary(tmp_path).records()) == 5
    (tmp_path / "legacy").mkdir(); (tmp_path / "legacy" / "bad.json").write_text("not-json")
    assert len(StyleLibrary(tmp_path).records()) == 5
    assert build_index(tmp_path)[0].style_id == rows[0].style_id  # idempotent
    (tmp_path / "index.jsonl").write_text((tmp_path / "index.jsonl").read_text() + (tmp_path / "index.jsonl").read_text().splitlines()[0] + "\n")
    with pytest.raises(StyleLibraryError): StyleLibrary(tmp_path).records()


def test_t20_cache_repair_and_version_key(tmp_path):
    style = make_library(tmp_path, 1)[0]
    calls=[]
    good={k:k for k in StyleExtractor.FIELDS}
    def inspect(image,prompt): calls.append((image,prompt)); return "bad" if len(calls)==1 else good
    first=StyleExtractor(tmp_path,inspect,model_id="m1").extract(style)
    second=StyleExtractor(tmp_path,inspect,model_id="m1").extract(style)
    assert first == second and len(calls)==2
    third=StyleExtractor(tmp_path,lambda *_:good,model_id="m2").extract(style)
    assert third.extraction_key != first.extraction_key


def test_t21_five_unique_or_explicit_shortage(tmp_path):
    rows=make_library(tmp_path)
    mapping={r.style_id:extraction(r,n) for n,r in enumerate(rows)}
    selected=select_five(rows,lambda r:mapping[r.style_id],"brand history")
    assert len({x.style.style_id for x in selected})==5
    with pytest.raises(StyleLibraryError,match="INSUFFICIENT"): select_five(rows[:4],lambda r:mapping[r.style_id],"x")


def test_t22_text_only_render_boundary(tmp_path):
    rows=make_library(tmp_path); selected=[]
    for n,r in enumerate(rows):
        e=extraction(r,n); selected.append(type("S",(),{"style":r,"extraction":e,"reason":"fit","risk":"risk","task_fit":"fit"})())
        assert "image.png" not in safe_render_supplement(selected[-1])
    with pytest.raises(ValueError,match="STYLE_REFERENCE_LEAK"): assert_reference_isolated({"reference_images":["x.png"]})


def test_t13_t14_state_gate_audit_and_t16_freeze():
    first=revise_task([],"raw","# task","alice")
    second=revise_task([first],"changed","# task 2","bob")
    assert second.previous_hash==first.revision_hash and second.diff and second.actor=="bob"
    snapshot={"task_revision":second.model_dump(mode="json"),"task_approval":{"revision_hash":second.revision_hash,"actor":"owner"}}
    require_transition(DomainState.TASK_APPROVAL,DomainState.CATEGORY_ANALYSIS,snapshot)
    bad={**snapshot,"task_approval":{"revision_hash":first.revision_hash,"actor":"owner"}}
    with pytest.raises(ValueError,match="APPROVAL"): require_transition(DomainState.CATEGORY_ANALYSIS,DomainState.STYLE_SELECTION,bad)
    asset={"artifact_id":"artifact_"+"a"*24,"sha256":"a"*64}
    snapshot.update(quality_asset_sha256=asset["sha256"],quality_passed=True)
    frozen=freeze_delivery(snapshot,asset=asset,quality_version="q1",actor="owner")
    assert frozen.asset_sha256==asset["sha256"]
    with pytest.raises(Exception): frozen.asset_sha256="b"*64


def test_t17_error_matrix_disallows_contract_and_auth_retry():
    assert recovery_actions("timeout_unknown")==('retry_after_confirmation','abandon')
    assert recovery_actions("authentication")==()
    assert recovery_actions("invalid_input")==()
