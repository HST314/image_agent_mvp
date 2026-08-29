from __future__ import annotations
import json
import threading
from pathlib import Path
from PIL import Image
import io
import pytest

from agent_core.annotation import compose
from agent_core.delivery import build_delivery, delivery_note_prompt, persist_delivery
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
    registry=JobRegistry(tmp_path,workers=1); worker_gate=threading.Event(); cancelled_calls=[]
    # Occupy the sole worker so cancellation is deterministically accepted
    # before the target's execute callback starts.  Running cancellation has a
    # separate fault-injection regression because it cannot roll back effects.
    blocker,_=registry.submit("blocker","blocker-key-1","advance",lambda:worker_gate.wait(2))
    job,_=registry.submit("p","cancel-key-1","advance",lambda:(cancelled_calls.append(1) or {"should":"not publish"}))
    registry.cancel(job["job_id"]); worker_gate.set()
    for _ in range(200):
        item=registry.get(job["job_id"])
        if item["status"]=="cancelled": break
        threading.Event().wait(.01)
    assert item["status"]=="cancelled" and "result" not in item and cancelled_calls==[]


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


def test_t27_delivery_note_prompt_has_five_shots_and_frozen_evidence():
    asset={"artifact_id":"artifact_"+"a"*24,"uri":"artifact://artifact_"+"a"*24,
           "sha256":"b"*64,"style_id":"style-2"}
    snapshot={"task_card":{"task_id":"t1","deliverable_goal":"品牌海报"},
              "style_selections":[{"style_id":"style-2","reason":"强化品牌识别"}],
              "render_plans":[{"style_id":"style-2","prompt_text":"中心构图，蓝金配色"}],
              "inspection":{"passed":True,"overall_score":93}}
    prompt=delivery_note_prompt(snapshot,asset)
    assert all(f"【示例{number}" in prompt for number in "一二三四五")
    assert "中心构图，蓝金配色" in prompt and "overall_score" in prompt
    assert "只输出 Markdown 正文" in prompt


def test_t27_generated_delivery_note_replaces_template():
    asset={"artifact_id":"artifact_"+"a"*24,"uri":"artifact://artifact_"+"a"*24,"sha256":"b"*64}
    markdown=("# 图：品牌主视觉\n\n## 视觉描述\n完整的段落化说明。\n\n"
              "### 整体风格\n现代。\n\n### 构图分析\n居中。\n\n### 色彩体系\n蓝金。\n\n"
              "### 象征意义\n成长。\n\n### 工艺特征\n细腻材质。")
    envelope=build_delivery({"task_card":{"task_id":"t1"}},"p1",asset,"trace-1",
                            generated_markdown=markdown)
    assert envelope.design_note_markdown == markdown

    free_form=build_delivery({"task_card":{"task_id":"t1"}},"p1",asset,"trace-1",
                             generated_markdown="# 简洁说明\n\n这是一段正常 Markdown 文本。")
    assert free_form.design_note_markdown == "# 简洁说明\n\n这是一段正常 Markdown 文本。"

    fallback=build_delivery({"task_card":{"task_id":"t1"}},"p1",asset,"trace-1",
                            generated_markdown="   ")
    assert fallback.design_note_markdown.startswith("# 最终设计说明")
