import base64
import json
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_core.contracts import DesignDeliveryEnvelopeV1, DesignTaskEnvelopeV1
from configs.runtime_policy import RuntimePolicy
from model_router.executor import ModelCallError, ModelExecutor
from storage.project_store import ArtifactStore, ProjectStore


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


def test_t01_contract_fixtures():
    root = Path("examples/contracts")
    DesignTaskEnvelopeV1.model_validate_json((root / "design_task_v1.valid.json").read_text())
    DesignDeliveryEnvelopeV1.model_validate_json((root / "design_delivery_v1.valid.json").read_text())
    with pytest.raises(ValidationError):
        DesignTaskEnvelopeV1.model_validate_json((root / "design_task_v1.invalid.json").read_text())
    with pytest.raises(ValidationError):
        DesignDeliveryEnvelopeV1.model_validate_json((root / "design_delivery_v1.invalid.json").read_text())


def test_t02_runtime_policy_is_strict_and_snapshotted(tmp_path: Path):
    policy = RuntimePolicy.from_file("configs/runtime.yaml")
    with pytest.raises(ValidationError):
        RuntimePolicy.model_validate({**policy.snapshot(), "not_wired": True})
    store = ProjectStore(tmp_path, "p")
    store.create(policy.snapshot())
    saved = json.loads((store.root / "runtime_policy.json").read_text())
    assert saved["policy"]["watermark"] is False and len(saved["sha256"]) == 64


def test_t05_content_addressing_dedup_decode_and_isolation(tmp_path: Path):
    one = ProjectStore(tmp_path, "one"); one.create()
    two = ProjectStore(tmp_path, "two"); two.create()
    first = one.artifacts.save_bytes(PNG, metadata={"source": "test"})
    again = one.artifacts.save_bytes(PNG, metadata={"source": "test"})
    assert first["artifact_id"] == again["artifact_id"]
    path, record = one.artifacts.resolve(first["artifact_id"])
    assert path.is_file() and record["uri"].startswith("artifact://")
    with pytest.raises(FileNotFoundError):
        two.artifacts.resolve(first["artifact_id"])
    with pytest.raises(ValueError):
        one.artifacts.save_bytes(b"\x89PNG\r\n\x1a\ntruncated")


def test_t07_timeout_is_unknown_and_never_auto_retries():
    calls = 0
    def slow():
        nonlocal calls
        calls += 1
        raise TimeoutError("provider SDK timeout")
    with pytest.raises(ModelCallError) as error:
        ModelExecutor(max_attempts=5, timeout=.001).run(slow)
    assert error.value.category == "timeout_unknown" and calls == 1


def test_t09_event_sequences_are_unique_under_concurrency(tmp_path: Path):
    store = ProjectStore(tmp_path, "p"); store.create()
    threads = [threading.Thread(target=lambda n=n: store.events.append("stress", n=n)) for n in range(100)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    events = store.history()
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert len({event["event_id"] for event in events}) == len(events)


def test_t10_t12_checkpoint_id_and_pending_recovery(tmp_path: Path):
    store = ProjectStore(tmp_path, "p"); store.create()
    checkpoint_id = store.checkpoint("safe", {"ok": True})
    assert checkpoint_id.startswith("checkpoint_") and store.resume() == {"ok": True}
    for attack in ("../other/checkpoint.json", "/etc/passwd", "checkpoint_forged"):
        with pytest.raises((ValueError, FileNotFoundError)):
            store.branch_from(attack)
    pending = store.root / "transactions/pending.json"
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text(json.dumps({"format_version": 1, "status": "intent", "branch": "main", "sequence": 2, "state": "next", "data": {}}))
    assert store.resume() == {"ok": True} and not pending.exists()
