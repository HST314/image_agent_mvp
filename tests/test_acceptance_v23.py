"""T10（契约 §7/Q10-A）新建工程流程验收：

- defer_run 创建仅持久化工程与入站任务卡、立即返回，不做任何模型调用；
- POST /api/projects/{id}/jobs 在无检查点且存在入站任务卡时异步引导首个推进，
  操作名为「初始化工程」，timeline 的 step_started 承载真实进行状态；
- 首个推进失败（无检查点）后的真实前端恢复链：同键对账（去重命中终态=后端
  确认原尝试终结）→ 前端轮换新尝试键 → 引导路径重启，成功后 failed_step 被清除；
- 在途同键重发去重（M1 防重复语义）：返回同一在途记录，不发生第二次执行；
- 默认（defer_run 缺省）同步创建行为保持不变（契约 §12 既有契约不回退）。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main_front
from agent_core.jobs import JobRegistry
from agent_core.workflow_runner import WorkflowRunner

TASK = {
    "task_id": "task-v23",
    "project_id": "v23-project",
    "source_refs": [{"ref_id": "brief-v23", "ref_type": "brief", "excerpt": "新品海报", "source_hash": None}],
    "deliverable_goal": "新品海报",
    "usage_context": "内部审核",
    "category_ref": {"category_id": "generic", "version": "1"},
    "known_facts": {"audience": "审核人员"},
    "unknowns": {"output_spec": "待确认"},
    "asset_inputs": [],
    "status": "draft",
}

TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(main_front, "JOBS", JobRegistry(tmp_path / "jobs"))
    return TestClient(main_front.app, raise_server_exceptions=False)


def _wait_job(client: TestClient, job_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = client.get(f"/api/jobs/{job_id}").json()
        if record["status"] in TERMINAL:
            return record
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} 未在预期时间内结束")


def _create(client: TestClient, project_id: str, **extra) -> dict:
    payload = {"project_id": project_id, "task_card": TASK, "offline": True, **extra}
    response = client.post("/api/projects", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_defer_run_create_persists_without_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)
    view = _create(client, "v23-defer", defer_run=True)

    # 立即返回：无检查点、无工作流快照、无可用动作，首个推进尚未发生
    assert view["manifest"]["current_checkpoint"] is None
    assert view["snapshot"] == {}
    assert view["capabilities"] == []

    root = tmp_path / "projects" / "v23-defer"
    # 入站任务卡已持久化，供 jobs 引导路径使用
    intake = json.loads((root / "intake_task.json").read_text(encoding="utf-8"))
    assert intake["deliverable_goal"] == TASK["deliverable_goal"]
    assert intake["project_id"] == "v23-defer"
    # 未产生任何步骤事件（只有创建事件）——确实没有发生模型调用
    types = [event["type"] for event in view["history"]]
    assert "step_started" not in types and "project_created" in types


def test_default_create_still_runs_synchronously(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """契约 §12：既有同步创建契约不因 defer_run 新增而改变。"""
    client = _client(tmp_path, monkeypatch)
    view = _create(client, "v23-sync")
    assert view["manifest"]["current_checkpoint"] is not None
    assert view["snapshot"].get("phase") == "waiting_clarification"
    assert not (tmp_path / "projects" / "v23-sync" / "intake_task.json").exists()


def test_jobs_bootstrap_advances_fresh_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)
    _create(client, "v23-boot", defer_run=True)

    submitted = client.post("/api/projects/v23-boot/jobs", json={})
    assert submitted.status_code == 202, submitted.text
    job = submitted.json()
    assert job["operation"] == "初始化工程"

    record = _wait_job(client, job["job_id"])
    assert record["status"] == "succeeded", record

    view = client.get("/api/projects/v23-boot").json()
    assert view["manifest"]["current_checkpoint"] is not None
    assert view["snapshot"].get("phase") == "waiting_clarification"

    # 实时状态的真实来源：timeline 中存在 intake 步骤的 step_started 事件
    timeline = client.get("/api/projects/v23-boot/timeline").json()["items"]
    assert any(e["type"] == "step_started" and e.get("state") == "intake_clarify" for e in timeline)


def test_jobs_bootstrap_without_intake_task_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)
    _create(client, "v23-nointake", defer_run=True)
    (tmp_path / "projects" / "v23-nointake" / "intake_task.json").unlink()

    submitted = client.post("/api/projects/v23-nointake/jobs", json={})
    assert submitted.status_code == 202, submitted.text
    record = _wait_job(client, submitted.json()["job_id"])
    assert record["status"] == "failed"
    assert "可恢复节点" in record["error"]["message"]


def test_bootstrap_restart_after_first_step_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """首步未知失败后的真实前端恢复链（前后端串联回归）。

    「重新启动创作流程」点击后前端 jobrunner.start 的真实请求序列（T10 对账契约）：
    1) 携带持久化幂等键 POST /jobs——后端按「工程+键」去重，命中旧记录即对账
       应答：返回 200 + 终态记录 = 原尝试已被后端确认终结（新建记录必为
       queued），期间不产生任何新执行；
    2) 前端据此清除旧键、生成新尝试键再次 POST——引导路径真正重启首个推进。
    对账按「终态」判定而非错误分类，故本用例保留 transport_unknown 分类，
    与前端「未知结果保留键」策略不再矛盾（旧版手工换键绕开了前端真实行为）。
    """
    client = _client(tmp_path, monkeypatch)
    _create(client, "v23-fail", defer_run=True)

    original_run = WorkflowRunner.run

    def failing_run(self, snapshot, options, **kwargs):
        # 模拟首个模型调用失败的真实产物：failed_step 已落盘但无成功检查点；
        # 异常携带规范化分类（对齐 ModelCallError 经 jobs.py 透出的真实形态），
        # 前端对该分类保留幂等键——恢复必须走对账换键，而非旧版的手工换键假设。
        self.store.fail_step("intake_clarify", {
            "code": "RuntimeError", "message": "模拟首个模型调用失败",
            "category": "transport_unknown", "retryable": True, "recovery_actions": ["retry"],
        })
        error = RuntimeError("模拟首个模型调用失败")
        error.category = "transport_unknown"
        raise error

    monkeypatch.setattr(WorkflowRunner, "run", failing_run)
    first = client.post("/api/projects/v23-fail/jobs", json={"idempotency_key": "bootstrap-attempt-1"})
    assert first.status_code == 202, first.text
    failed_record = _wait_job(client, first.json()["job_id"])
    assert failed_record["status"] == "failed"
    assert failed_record["error"]["category"] == "transport_unknown"

    failed_view = client.get("/api/projects/v23-fail").json()
    assert failed_view["manifest"]["failed_step"] is not None
    assert failed_view["manifest"]["current_checkpoint"] is None

    # 步骤 1（对账）：前端重试点击先携带同一持久化键 POST——去重命中返回
    # 200 + 原失败终态记录（后端确认原尝试终结），且不得创建新 job
    reconcile = client.post("/api/projects/v23-fail/jobs", json={"idempotency_key": "bootstrap-attempt-1"})
    assert reconcile.status_code == 200, reconcile.text
    assert reconcile.json()["job_id"] == failed_record["job_id"]
    assert reconcile.json()["status"] == "failed"
    assert len(list((tmp_path / "jobs").glob("job_*.json"))) == 1, "对账应答不得产生新执行"

    # 步骤 2：前端确认终态后轮换新尝试键重发（M1 机制生成 bootstrap-attempt-2
    # 对应的新随机键）——引导路径从任务卡重新执行首个推进
    monkeypatch.setattr(WorkflowRunner, "run", original_run)
    second = client.post("/api/projects/v23-fail/jobs", json={"idempotency_key": "bootstrap-attempt-2"})
    assert second.status_code == 202, second.text
    assert second.json()["job_id"] != failed_record["job_id"]
    assert _wait_job(client, second.json()["job_id"])["status"] == "succeeded"

    recovered = client.get("/api/projects/v23-fail").json()
    assert recovered["manifest"]["failed_step"] is None
    assert recovered["manifest"]["current_checkpoint"] is not None
    assert recovered["snapshot"].get("phase") == "waiting_clarification"


def test_bootstrap_same_key_while_inflight_dedupes_without_second_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1 防重复语义保留：原尝试在途时同键重发（双击/响应丢失重试）去重——
    返回同一在途记录，同一工程同一键永不发生第二次执行。"""
    client = _client(tmp_path, monkeypatch)
    _create(client, "v23-inflight", defer_run=True)

    gate = threading.Event()
    calls: list[int] = []
    original_run = WorkflowRunner.run

    def slow_run(self, snapshot, options, **kwargs):
        calls.append(1)
        gate.wait(5)
        return original_run(self, snapshot, options, **kwargs)

    monkeypatch.setattr(WorkflowRunner, "run", slow_run)
    first = client.post("/api/projects/v23-inflight/jobs", json={"idempotency_key": "bootstrap-attempt-1"})
    assert first.status_code == 202, first.text

    # 等 job 真正进入执行（阻塞在 gate 上）再在途重发
    deadline = time.time() + 5
    while not calls and time.time() < deadline:
        time.sleep(0.02)
    assert calls, "job 未在预期时间内进入执行"

    duplicate = client.post("/api/projects/v23-inflight/jobs", json={"idempotency_key": "bootstrap-attempt-1"})
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["job_id"] == first.json()["job_id"]
    assert duplicate.json()["status"] in {"queued", "running"}
    assert len(list((tmp_path / "jobs").glob("job_*.json"))) == 1, "在途去重不得创建新 job"

    gate.set()
    assert _wait_job(client, first.json()["job_id"])["status"] == "succeeded"
    assert len(calls) == 1, "同一键在途期间不得发生第二次执行"
