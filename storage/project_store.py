"""Versioned, atomic, file-backed project workspace."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

FORMAT_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def content_hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temporary.open("wb") as stream:
        stream.write(_canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class CorruptProjectError(ValueError):
    pass


class ProjectLockError(RuntimeError):
    pass

class ProjectExistsError(FileExistsError):
    pass

class ImmutableRecordError(FileExistsError):
    pass


class EventStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, event_type: str, **payload: Any) -> dict[str, Any]:
        event = {"format_version": FORMAT_VERSION, "event_id": uuid4().hex, "timestamp": _now(), "type": event_type, **payload}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return event

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]


class _LegacyPromptStore:
    SECRET_WORDS = ("api_key", "apikey", "authorization", "token", "secret")

    def __init__(self, root: Path) -> None:
        self.root = root

    REQUIRED = {"messages", "template_id", "template_version", "template_hash", "variables", "input_refs", "model", "parameters", "config_hash", "state", "trace_id"}

    def begin(self, record: dict[str, Any]) -> str:
        missing = self.REQUIRED - record.keys()
        if missing:
            raise ValueError(f"Prompt 审计记录缺少必填项：{', '.join(sorted(missing))}")
        prompt_id = str(record.get("prompt_id") or f"prompt_{uuid4().hex}")
        sanitized = self._redact(record)
        data = {"format_version": FORMAT_VERSION, "prompt_id": prompt_id, "created_at": _now(), "status": "started", **sanitized}
        data["record_hash"] = content_hash(data)
        path = self.root / f"{prompt_id}.json"
        if path.exists():
            raise ImmutableRecordError("Prompt 记录不可覆盖。")
        atomic_json(path, data)
        return prompt_id

    def complete(self, prompt_id: str, *, output_raw: Any, output_parsed: Any = None, output_ref: str | None = None) -> str:
        original = self.get(prompt_id)
        record = {**original, "parent_record_hash": original["record_hash"], "status": "completed", "completed_at": _now(), "output_raw": self._redact(output_raw), "output_parsed": self._redact(output_parsed), "output_ref": output_ref}
        record.pop("record_hash", None)
        record["record_hash"] = content_hash(record)
        result_id = f"{prompt_id}.result"
        path = self.root / f"{result_id}.json"
        if path.exists():
            raise ImmutableRecordError("Prompt 输出审计记录不可覆盖。")
        atomic_json(path, record)
        return result_id

    def save(self, record: dict[str, Any]) -> str:
        """Compatibility entry point; still enforces the strong contract."""
        return self.begin(record)

    def get(self, prompt_id: str) -> dict[str, Any]:
        data = json.loads((self.root / f"{prompt_id}.json").read_text(encoding="utf-8"))
        checksum = data.pop("record_hash", None)
        if data.get("format_version") != FORMAT_VERSION or checksum != content_hash(data):
            raise CorruptProjectError("Prompt 记录版本或完整性校验失败。")
        data["record_hash"] = checksum
        return data

    @classmethod
    def _redact(cls, value: Any) -> Any:
        # 新加这 2 行：如果是 Pydantic 对象，自动转为字典
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")

        if isinstance(value, dict):
            return {k: "[REDACTED]" if any(word in k.lower() for word in cls.SECRET_WORDS) else cls._redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [cls._redact(item) for item in value]
        return value


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.metadata = root / "metadata.jsonl"

    def save_bytes(self, content: bytes, *, suffix: str, metadata: dict[str, Any]) -> dict[str, Any]:
        digest = hashlib.sha256(content).hexdigest()
        path = self.root / "images" / f"{digest}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(content)
        record = {"format_version": FORMAT_VERSION, "artifact_id": f"artifact_{digest[:24]}", "uri": str(path), "sha256": digest, **metadata}
        EventStore(self.metadata).append("artifact_saved", **record)
        return record


class CheckpointStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, branch: str, sequence: int, state: str, data: dict[str, Any]) -> tuple[str, str]:
        envelope = {"format_version": FORMAT_VERSION, "branch": branch, "sequence": sequence, "state": state, "data": data}
        envelope["checksum"] = content_hash(envelope)
        relative = f"checkpoints/{branch}/{sequence:06d}-{state}.json"
        path = self.root / relative
        if path.exists():
            raise ImmutableRecordError("成功检查点不可覆盖。")
        atomic_json(path, envelope)
        return relative, envelope["checksum"]

    def load(self, relative: str) -> dict[str, Any]:
        envelope = json.loads((self.root / relative).read_text(encoding="utf-8"))
        checksum = envelope.pop("checksum", None)
        if envelope.get("format_version") != FORMAT_VERSION or checksum != content_hash(envelope):
            raise CorruptProjectError("检查点版本或完整性校验失败。")
        envelope["checksum"] = checksum
        return envelope


class ProjectStore:
    """Own project manifest, branches, prompts, events, artifacts and checkpoints."""

    def __init__(self, projects_root: str | Path, project_id: str) -> None:
        self.root = Path(projects_root) / project_id
        self.project_id = project_id
        self.events = EventStore(self.root / "events/events.jsonl")
        from storage.prompt_store import PromptStore
        self.prompts = PromptStore(self.root / "runtime/prompts.jsonl")
        self.artifacts = ArtifactStore(self.root / "artifacts")
        self.checkpoints = CheckpointStore(self.root)
        self._lock_depth = 0

    def create(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.root.exists() and any(self.root.iterdir()):
            raise ProjectExistsError("工程已存在；请使用 resume、retry 或 rewind，禁止重复 new。")
        self.root.mkdir(parents=True, exist_ok=False)
        manifest = {"format_version": FORMAT_VERSION, "project_id": self.project_id, "current_branch": "main", "current_checkpoint": None, "failed_step": None, "created_at": _now(), "updated_at": _now()}
        atomic_json(self.root / "manifest.json", manifest)
        atomic_json(self.root / "project.yaml", config or {})
        atomic_json(self.root / "branches.json", {"format_version": FORMAT_VERSION, "branches": {"main": {"parent": None, "from_checkpoint": None, "created_at": _now()}}})
        self.events.append("project_created", branch="main")
        return manifest

    def manifest(self) -> dict[str, Any]:
        data = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        if data.get("format_version") != FORMAT_VERSION:
            raise CorruptProjectError("工程版本不受支持。")
        return data

    def checkpoint_context(self, state: str, context: Any, *, branch: str | None = None) -> str:
        return self.checkpoint(state, context.dump_snapshot(), branch=branch)

    def checkpoint(self, state: str, data: dict[str, Any], *, branch: str | None = None) -> str:
        manifest = self.manifest()
        active = branch or manifest["current_branch"]
        previous = manifest.get("current_checkpoint")
        sequence = 1 if not previous or previous.get("branch") != active else int(previous["sequence"]) + 1
        relative, checksum = self.checkpoints.save(active, sequence, state, data)
        pointer = {"path": relative, "checksum": checksum, "branch": active, "sequence": sequence, "state": state}
        manifest.update(current_branch=active, current_checkpoint=pointer, failed_step=None, updated_at=_now())
        atomic_json(self.root / "manifest.json", manifest)
        self.events.append("step_succeeded", branch=active, state=state, checkpoint=relative)
        return relative

    def start_step(self, state: str, **details: Any) -> None:
        self.events.append("step_started", branch=self.manifest()["current_branch"], state=state, **details)

    def fail_step(self, state: str, error: dict[str, Any]) -> None:
        manifest = self.manifest()
        manifest["failed_step"] = {"state": state, "error": error, "at": _now()}
        manifest["updated_at"] = _now()
        atomic_json(self.root / "manifest.json", manifest)
        self.events.append("step_failed", branch=manifest["current_branch"], state=state, error=error)

    def resume(self) -> dict[str, Any] | None:
        pointer = self.manifest().get("current_checkpoint")
        return self.checkpoints.load(pointer["path"])["data"] if pointer else None

    def retry(self, execute: Any, *, name: str | None = None) -> Any:
        manifest = self.manifest()
        failure = manifest.get("failed_step")
        pointer = manifest.get("current_checkpoint")
        if not failure:
            raise ValueError("当前没有失败步骤需要重试。")
        if not pointer:
            raise ValueError("失败步骤之前没有成功检查点，无法安全重试。")
        branch = self.branch_from(pointer["path"], name=name or f"retry-{uuid4().hex[:8]}")
        self.events.append("retry_started", branch=branch, state=failure["state"], from_checkpoint=pointer["path"])
        return execute(failure["state"], self.resume())

    def branch_from(self, checkpoint: str, *, name: str | None = None) -> str:
        source = self.checkpoints.load(checkpoint)
        branches_path = self.root / "branches.json"
        branches = json.loads(branches_path.read_text(encoding="utf-8"))
        branch = name or f"branch-{uuid4().hex[:8]}"
        if branch in branches["branches"]:
            raise ValueError("分支名称已存在。")
        branches["branches"][branch] = {"parent": source["branch"], "from_checkpoint": checkpoint, "created_at": _now()}
        atomic_json(branches_path, branches)
        manifest = self.manifest()
        relative, checksum = self.checkpoints.save(branch, 1, source["state"], source["data"])
        manifest.update(current_branch=branch, current_checkpoint={"path": relative, "checksum": checksum, "branch": branch, "sequence": 1, "state": source["state"]}, failed_step=None, updated_at=_now())
        atomic_json(self.root / "manifest.json", manifest)
        self.events.append("branch_created", branch=branch, parent=source["branch"], from_checkpoint=checkpoint)
        return branch

    @contextmanager
    def lock(self) -> Iterator[None]:
        # The runner owns the project transaction. Helpers such as the candidate
        # batch may enter it again on the same store instance without deadlock.
        if self._lock_depth:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        lock_path = self.root / ".lock"
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(descriptor, str(os.getpid()).encode())
            os.close(descriptor)
        except FileExistsError as exc:
            raise ProjectLockError("该工程正在由另一个进程处理，请稍后重试。") from exc
        try:
            self._lock_depth = 1
            yield
        finally:
            self._lock_depth = 0
            lock_path.unlink(missing_ok=True)

    def idempotency_key(self, state: str, checkpoint_hash: str, prompt_hash: str, model_hash: str, reference_hash: str = "") -> str:
        return content_hash([state, checkpoint_hash, prompt_hash, model_hash, reference_hash])

    def history(self) -> list[dict[str, Any]]:
        return self.events.read_all()
