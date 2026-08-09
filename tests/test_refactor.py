from pathlib import Path
import time
import pytest
from agent_core.batch import CandidateBatchGenerator
from agent_core.context import AgentContext
from agent_core.models import DirectionSelection, ImageTaskCard, ModelRole, ReferenceImage, SourceRef
from agent_core.state_machine import RecoverableWorkflow
from agent_core.workflow import SelfCheckPolicy
from calibrator.calibration_loop import CalibrationLoop, ManualAction
from storage.assets import normalize_image_asset
from interaction.confirmation_builder import specification_from_task, specification_to_markdown, update_specification_from_markdown
from interaction.presenter import Presenter
from interaction.question_generator import generate_question_card
from model_router.executor import ModelCallError, ModelExecutor
from model_router.gateway import RuntimeModelGateway
from model_router.router import ModelRouter
from prompt_engine.context_assembler import CapabilityMismatchError, ContextAssembler, ContextPolicy
from render_clients.ark_client import ArkImageRenderClient
from render_clients.payload_mapper import build_render_payload
from storage.project_store import CorruptProjectError, ProjectExistsError, ProjectLockError, ProjectStore

def task(unknowns=None):
    return ImageTaskCard(task_id="t", project_id="p", source_refs=[SourceRef(ref_id="s", ref_type="text")], deliverable_goal="海报", usage_context="手机", known_facts={"主体":"产品"}, unknowns=unknowns or {})

def audit(**extra):
    return {"messages":[{"role":"user","content":"x"}], "template_id":"t", "template_version":"1", "template_hash":"h", "variables":{}, "input_refs":[], "model":{"name":"m"}, "parameters":{}, "config_hash":"c", "state":"s", "trace_id":"trace", **extra}

def test_01_five_select_one_and_presenter_hides_keys():
    assert DirectionSelection(task_id="t", selected_asset_ids=["one"], selected_by="u").selected_asset_ids == ["one"]
    assert RecoverableWorkflow.select_master([{"id":str(i)} for i in range(5)], "2")["id"] == "2"
    assert "asset_id" not in Presenter().progress("master_candidate_selection")

def test_02_immutable_project_snapshot_checkpoint_and_lock(tmp_path: Path):
    store=ProjectStore(tmp_path,"p"); store.create()
    with pytest.raises(ProjectExistsError): store.create()
    ctx=AgentContext(task_card=task()); snap=ctx.dump_snapshot(); assert AgentContext.load_snapshot(snap).task_card.task_id=="t"
    snap["context"]["task_card"]["task_id"]="bad"
    with pytest.raises(CorruptProjectError): AgentContext.load_snapshot(snap)
    with store.lock():
        with store.lock(): pass  # same transaction may safely enter helper locks

def test_03_prompt_contract_redaction_and_audited_gateway(tmp_path: Path):
    store=ProjectStore(tmp_path,"p"); store.create()
    with pytest.raises(ValueError): store.prompts.begin({"messages":[]})
    pid=store.prompts.begin(audit(api_key="secret")); assert store.prompts.get(pid)["api_key"]=="[REDACTED]"
    result=store.prompts.complete(pid, output_raw={"token":"secret", "ok":1}); assert store.prompts.get(result)["output_raw"]["token"]=="[REDACTED]"

def test_04_retry_rewind_resume_and_chinese_history(tmp_path: Path):
    store=ProjectStore(tmp_path,"p"); store.create(); store.start_step("intake_clarify"); cp=store.checkpoint("intake_clarify",{"ok":1}); store.fail_step("confirmation_build",{"message":"x"})
    called=[]; store.retry(lambda state,data: called.append((state,data)))
    assert called[0][0]=="confirmation_build" and store.resume()=={"ok":1}
    assert store.branch_from(cp).startswith("branch-")
    history=Presenter().history(store.history()); assert "创建新分支" in history and "step_" not in history

def test_05_clarification_zero_budget_dedup_over_ten_and_repair():
    assert generate_question_card(task()).questions==[]
    unknown={f"x{i}":{"impact":"影响","blocking":True,"has_safe_default":False} for i in range(12)}
    unknown["output_spec"]={"impact":"影响构图","blocking":True,"has_safe_default":False,"evidence":"未说明"}
    card=generate_question_card(task(unknown), max_auto_questions=20); assert len(card.questions)<=3
    seen={q.semantic_fingerprint for q in card.questions}; assert not generate_question_card(task(unknown), previous_fingerprints=seen).questions
    class Broken:
        n=0
        def complete(self,prompt): self.n+=1; return "bad" if self.n==1 else '{"questions":[]}'
    errors=[]; assert generate_question_card(task(unknown), Broken(), error_recorder=errors.append).questions==[] and len(errors)==1

def test_06_specific_mutually_exclusive_options():
    card=generate_question_card(task({"output_spec":{"impact":"改变构图","blocking":True,"has_safe_default":False}}))
    labels={o.label for o in card.questions[0].options}; assert labels=={"竖版手机","横版屏幕","方形信息流"}

def test_07_task_spec_markdown_round_trip_version_and_hash():
    spec=specification_from_task(task()); md=specification_to_markdown(spec).replace("产品","产品与人物")
    updated=update_specification_from_markdown(spec,md); assert updated.version==2 and updated.parent_hash==spec.content_hash and any(f.value=="产品与人物" for f in updated.facts)

@pytest.mark.parametrize("termination,release",[(a,b) for a in ("fix","solo") for b in ("manual","auto")])
def test_08_self_check_four_modes_and_wait_recovery(tmp_path: Path, termination, release):
    store=ProjectStore(tmp_path,f"p-{termination}-{release}"); store.create(); calls=[]
    policy=SelfCheckPolicy(termination,release,fixed_rounds=1,max_rounds=1)
    loop=CalibrationLoop(store,policy,inspector=lambda u,p:{"passed":False,"decision":"continue","rework_prompt_delta":"改","confidence":.8},reworker=lambda p:calls.append(p) or {"uri":"new","sha256":"n"})
    result=loop.run(current_asset={"uri":"cur","sha256":"c"},stable_specification="稳定事实",constraints=[])
    assert result["waiting"] and not result["termination_satisfied"]
    assert result["asset"]["sha256"] == result["latest_checked_asset_hash"] == "c"
    assert calls == []
    assert any(e["type"]=="inspection_completed" for e in store.history())

def test_09_i2i_current_first_and_capability():
    payload=build_render_payload("m","p","2K",{},reference_images=["current","base"]); assert payload["extra_body"]["image"][0]=="current"
    refs=[ReferenceImage(uri=x,role="current" if i==0 else "base",source=x,sha256=str(i),order=i,reason="needed") for i,x in enumerate(["a","b"])]
    with pytest.raises(CapabilityMismatchError): ContextAssembler(ContextPolicy("image",supports_multiple_images=False)).assemble(objective="o",specification="s",constraints=[],current_input="i",references=refs)

def test_10_router_hot_reload_role_capability_and_gateway_audit(tmp_path: Path):
    store=ProjectStore(tmp_path,"p"); store.create(); router=ModelRouter.from_file(Path("configs/model_config.yaml"))
    with pytest.raises(ValueError): router.validate_capability("self_check_inspection",role=ModelRole.TEXT_TO_IMAGE_MODEL)
    gateway=RuntimeModelGateway(store,router,ModelExecutor(max_attempts=1),offline_mode=True)
    result=gateway.call("initial_candidate_generation",ModelRole.TEXT_TO_IMAGE_MODEL,lambda route:{"ok":True},messages=[{"role":"user","content":"x"}],variables={},template_id="x",template_version="1",input_refs=[])
    assert result["ok"] and any(e["type"]=="model_config_loaded" for e in store.history())

def test_11_batch_partial_success_retry_and_idempotency(tmp_path: Path):
    store=ProjectStore(tmp_path,"p"); store.create(); calls={}
    def render(i):
        calls[i]=calls.get(i,0)+1
        if i==2: raise RuntimeError("bad")
        return {"uri":str(i),"sha256":str(i)}
    first=CandidateBatchGenerator(store,render,attempts=2).generate("h"); assert len(first["succeeded"])==4 and len(first["failed"])==1
    second=CandidateBatchGenerator(store,render,attempts=1).generate("h"); assert len(second["succeeded"])==4 and all(calls[i]==1 for i in (0,1,3,4))

def test_12_executor_timeout_classification_backoff_ids():
    delays=[]; executor=ModelExecutor(max_attempts=2,timeout=.01,base_delay=.001,sleeper=delays.append,randomizer=lambda a,b:0)
    with pytest.raises(ModelCallError) as info: executor.run(lambda:(time.sleep(.05),1)[1])
    assert info.value.category=="timeout" and info.value.request_id.startswith("req_") and len(delays)==1
    with pytest.raises(ModelCallError) as invalid: executor.run(lambda:(_ for _ in ()).throw(ValueError("bad")))
    assert not invalid.value.retryable

def test_13_mock_never_implicit_final_asset(monkeypatch):
    monkeypatch.delenv("ARK_API_KEY",raising=False)
    with pytest.raises(RuntimeError): ArkImageRenderClient().render({"prompt":"x"})
    with pytest.raises(ValueError): RecoverableWorkflow.validate_final_asset({"uri":"mock://x","mock":True},human_approved=True,self_check_complete=True)

def test_14_filter_before_limit_and_unknown_field_is_safe():
    unknowns = {f"safe_{i}": {"impact":"低影响", "blocking":True, "has_safe_default":True} for i in range(3)}
    unknowns["internal_secret_key"] = {"impact":"会改变核心画面", "blocking":True, "has_safe_default":False}
    card = generate_question_card(task(unknowns))
    assert len(card.questions) == 1
    rendered = Presenter().questions(card)
    assert "internal_secret_key" not in rendered

def test_15_asset_normalization_is_stable_and_complete():
    one = normalize_image_asset({"url":"https://images.example/a.png", "provider":"ark", "model":"seedream"})
    two = normalize_image_asset({"uri":"https://images.example/a.png", "provider":"ark", "model":"seedream"})
    assert one["sha256"] == two["sha256"] == one["reference_hash"]
    assert {"uri", "reference_hash", "sha256", "provider", "model", "mock"} <= one.keys()
