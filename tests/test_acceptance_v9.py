from __future__ import annotations
import json
import threading
from pathlib import Path
from PIL import Image
import io
import pytest

from agent_core.annotation import compose
from agent_core.delivery import build_delivery, persist_delivery
from agent_core.jobs import JobRegistry
from calibrator.structured_inspection import InspectionOutputError, parse_with_one_repair


def png(size=(100, 50)):
    out=io.BytesIO(); Image.new("RGB",size,"white").save(out,"PNG"); return out.getvalue()


def test_t23_job_is_idempotent_and_events_resume(tmp_path: Path):
    registry=JobRegistry(tmp_path); gate=threading.Event(); calls=[]
    def work(): calls.append(1); gate.wait(2); return {"ok":True}
    first,created=registry.submit("p","idem-key-123","advance",work)
    second,created_again=registry.submit("p","idem-key-123","advance",work)
    assert created and not created_again and first["job_id"]==second["job_id"]
    gate.set()
    for _ in range(200):
        if registry.get(first["job_id"])["status"]=="succeeded": break
        threading.Event().wait(.01)
    item=registry.get(first["job_id"])
    assert item["status"]=="succeeded" and calls==[1]
    assert [e["seq"] for e in registry.events(first["job_id"])]==list(range(1,len(item["events"])+1))
    assert registry.events(first["job_id"],after=1)[0]["seq"]==2


def test_t23_restart_marks_orphan_interrupted_without_execution(tmp_path: Path):
    path=tmp_path/"job_dead.json"; tmp_path.mkdir(exist_ok=True)
    path.write_text(json.dumps({"job_id":"job_dead","project_id":"p","status":"running","events":[]}),encoding="utf-8")
    registry=JobRegistry(tmp_path)
    assert registry.get("job_dead")["status"]=="interrupted"
    assert registry.get("job_dead")["error"]["code"]=="PROCESS_RESTARTED"


def test_t23_cancel_is_terminal_and_does_not_publish_result(tmp_path: Path):
    registry=JobRegistry(tmp_path,workers=1); gate=threading.Event()
    job,_=registry.submit("p","cancel-key-1","advance",lambda:(gate.wait(2) or {"should":"not publish"}))
    registry.cancel(job["job_id"]); gate.set()
    for _ in range(200):
        item=registry.get(job["job_id"])
        if item["status"]=="cancelled": break
        threading.Event().wait(.01)
    assert item["status"]=="cancelled" and "result" not in item


def test_t24_schema_repair_once_and_redacted_failure():
    calls=[]
    result=parse_with_one_repair("```json\n{bad}\n```",lambda raw,error:(calls.append(error) or {"passed":True,"decision":"pass","confidence":.9}))
    assert result.passed and len(calls)==1
    with pytest.raises(InspectionOutputError) as info:
        parse_with_one_repair("token=secret invalid",lambda *_:"authorization: bearer-secret still invalid")
    assert "secret" not in info.value.safe_raw and "[REDACTED]" in info.value.safe_raw


def test_t26_annotation_composition_coordinates_and_validation():
    result=compose(png(),[{"kind":"rectangle","x":.1,"y":.1,"w":.5,"h":.5,"color":"#ff0000","width":3},
                          {"kind":"stroke","points":[[0,0],[1,1]],"color":"blue","width":2}])
    assert Image.open(io.BytesIO(result)).size==(100,50) and result!=png()
    with pytest.raises(ValueError): compose(png(),[{"kind":"rectangle","x":.8,"y":.1,"w":.5,"h":.5}])


def test_t27_delivery_formats_are_consistent_and_stable(tmp_path: Path):
    asset={"artifact_id":"artifact_"+"a"*24,"uri":"artifact://artifact_"+"a"*24,"sha256":"b"*64}
    snapshot={"task_card":{"task_id":"t1","deliverable_goal":"海报"},"style_selections":[{"mechanism":"网格构图","reason":"适配品牌","task_fit":"移动端"}]}
    envelope=build_delivery(snapshot,"p1",asset,"trace-1"); files=persist_delivery(tmp_path,envelope)
    payload=json.loads((tmp_path/files["json"]).read_text(encoding="utf-8"))
    markdown=(tmp_path/files["markdown"]).read_text(encoding="utf-8")
    assert payload["final_image"]==asset and payload["design_note"]["concept"] in markdown
    assert payload["trace_ref"] in markdown
