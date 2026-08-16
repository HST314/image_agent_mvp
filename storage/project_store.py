"""Versioned, atomic, file-backed project workspace."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import portalocker
from io import BytesIO
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

FORMAT_VERSION = 1
BRANCH_NAME = re.compile(r"^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff._-]{1,63}$")
STATE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


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
    _guards: dict[str, threading.RLock] = {}
    _guards_lock = threading.Lock()

    def __init__(self, path: Path) -> None:
        self.path = path
        key = str(path.resolve())
        with self._guards_lock:
            self._guard = self._guards.setdefault(key, threading.RLock())

    def append(self, event_type: str, **payload: Any) -> dict[str, Any]:
        with self._guard:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a+", encoding="utf-8") as stream:
                portalocker.lock(stream, portalocker.LOCK_EX)
                try:
                    stream.seek(0)
                    existing = [json.loads(line) for line in stream if line.strip()]
                    event = {"format_version": FORMAT_VERSION, "event_id": uuid4().hex,
                             "sequence": (existing[-1].get("sequence", len(existing)) + 1) if existing else 1,
                             "timestamp": _now(), "type": event_type, **payload}
                    stream.seek(0, os.SEEK_END)
                    stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                finally:
                    portalocker.unlock(stream)
        return event

    def read_all(self) -> list[dict[str, Any]]:
        with self._guard:
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

    MAGIC = {b"\x89PNG\r\n\x1a\n": (".png", "image/png"), b"\xff\xd8\xff": (".jpg", "image/jpeg"), b"GIF8": (".gif", "image/gif"), b"RIFF": (".webp", "image/webp")}

    def save_bytes(self, content: bytes, *, suffix: str = "", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        if not content or len(content) > 25 * 1024 * 1024:
            raise ValueError("图片为空或超过 25 MiB。")
        detected = next((value for magic, value in self.MAGIC.items() if content.startswith(magic)), None)
        if not detected or (detected[0] == ".webp" and content[8:12] != b"WEBP"):
            raise ValueError("资产不是可识别的 PNG/JPEG/GIF/WebP 图片。")
        self._validate_image(content)
        canonical_suffix, mime = detected
        if suffix and suffix.lower() not in {canonical_suffix, ".jpeg" if canonical_suffix == ".jpg" else canonical_suffix}:
            raise ValueError("图片后缀与内容不匹配。")
        digest = hashlib.sha256(content).hexdigest()
        path = self.root / "images" / f"{digest}{canonical_suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(content)
        record = {"format_version": FORMAT_VERSION, "artifact_id": f"artifact_{digest[:24]}",
                  "uri": f"artifact://artifact_{digest[:24]}", "sha256": digest,
                  "mime_type": mime, "size_bytes": len(content), "filename": path.name, **(metadata or {})}
        EventStore(self.metadata).append("artifact_saved", **record)
        return record

    @staticmethod
    def _validate_image(content: bytes) -> None:
        """Fully decode untrusted bytes before persisting a success state."""
        try:
            from PIL import Image
            with Image.open(BytesIO(content)) as image:
                image.verify()
                if image.width <= 0 or image.height <= 0:
                    raise ValueError("图片尺寸无效。")
        except Exception as exc:
            raise ValueError("图片内容截断或无法解码。") from exc

    def resolve(self, artifact_id: str) -> tuple[Path, dict[str, Any]]:
        if not artifact_id.startswith("artifact_") or len(artifact_id) != 33:
            raise FileNotFoundError("资产不存在。")
        records = [e for e in EventStore(self.metadata).read_all() if e.get("artifact_id") == artifact_id]
        if not records:
            raise FileNotFoundError("资产不存在。")
        record = records[-1]
        path = (self.root / "images" / record["filename"]).resolve()
        allowed = (self.root / "images").resolve()
        if allowed not in path.parents or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
            raise CorruptProjectError("资产完整性校验失败。")
        return path, record


class CheckpointStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def index_path(self) -> Path:
        return self.root / "checkpoints/index.json"

    def _index(self) -> dict[str, Any]:
        # 写侧 atomic_json 永远按 UTF-8 落盘；读侧必须显式指定同一编码，
        # 否则在中文 Windows（默认 GBK）上含中文分支名的索引会被读成乱码。
        return json.loads(self.index_path.read_text(encoding="utf-8")) if self.index_path.exists() else {"format_version": FORMAT_VERSION, "items": {}}

    def prepare(self, branch: str, sequence: int, state: str, data: dict[str, Any]) -> dict[str, Any]:
        if not BRANCH_NAME.fullmatch(branch):
            raise ValueError("分支名称包含不安全字符。")
        if not STATE_NAME.fullmatch(state):
            raise ValueError("状态名称包含不安全字符。")
        envelope = {"format_version": FORMAT_VERSION, "branch": branch, "sequence": sequence, "state": state, "data": data}
        envelope["checksum"] = content_hash(envelope)
        relative = f"checkpoints/{branch}/{sequence:06d}-{state}.json"
        return {"envelope": envelope, "checkpoint_id": f"checkpoint_{envelope['checksum'][:24]}",
                "path": relative, "checksum": envelope["checksum"]}

    def save(self, branch: str, sequence: int, state: str, data: dict[str, Any], *, prepared: dict[str, Any] | None = None) -> tuple[str, str, str]:
        record = prepared or self.prepare(branch, sequence, state, data)
        envelope, relative = record["envelope"], record["path"]
        path = self.root / relative
        if path.exists():
            raise ImmutableRecordError("成功检查点不可覆盖。")
        atomic_json(path, envelope)
        checkpoint_id = record["checkpoint_id"]
        index = self._index()
        index["items"][checkpoint_id] = {"path": relative, "checksum": envelope["checksum"], "branch": branch, "sequence": sequence, "state": state}
        atomic_json(self.index_path, index)
        return checkpoint_id, relative, envelope["checksum"]

    def load(self, checkpoint_id: str) -> dict[str, Any]:
        if not checkpoint_id.startswith("checkpoint_"):
            raise ValueError("只接受本工程 checkpoint_id。")
        item = self._index().get("items", {}).get(checkpoint_id)
        if not item:
            raise ValueError("checkpoint_id 不属于本工程。")
        path = (self.root / item["path"]).resolve()
        if self.root.resolve() not in path.parents:
            raise CorruptProjectError("检查点路径越界。")
        envelope = json.loads(path.read_text(encoding="utf-8"))
        checksum = envelope.pop("checksum", None)
        if envelope.get("format_version") != FORMAT_VERSION or checksum != content_hash(envelope):
            raise CorruptProjectError("检查点版本或完整性校验失败。")
        envelope["checksum"] = checksum
        envelope["checkpoint_id"] = checkpoint_id
        for field in ("branch", "sequence", "state"):
            if item.get(field) != envelope.get(field):
                raise CorruptProjectError("检查点索引与文件内容不一致。")
        if item.get("checksum") != checksum:
            raise CorruptProjectError("检查点索引与文件校验值不一致。")
        return envelope

    def validate(self, checkpoint_id: str, *, expected: dict[str, Any] | None = None) -> dict[str, Any]:
        """Validate index, path, envelope metadata and checksum as one unit."""
        index = self._index()
        item = index.get("items", {}).get(checkpoint_id)
        if not item:
            raise CorruptProjectError("检查点索引记录缺失。")
        envelope = self.load(checkpoint_id)
        if expected:
            expected_fields = {
                "checkpoint_id": checkpoint_id,
                "path": item.get("path"),
                "checksum": envelope.get("checksum"),
                "branch": envelope.get("branch"),
                "sequence": envelope.get("sequence"),
                "state": envelope.get("state"),
            }
            for field, actual in expected_fields.items():
                value = expected.get(field)
                if value is not None and value != actual:
                    raise CorruptProjectError("检查点事务记录与落盘结果不一致。")
        return envelope

    def list(self) -> list[dict[str, Any]]:
        """Return safe checkpoint metadata without exposing filesystem paths."""
        items = self._index().get("items", {})
        return sorted(
            ({"checkpoint_id": checkpoint_id, "branch": item["branch"],
              "sequence": item["sequence"], "state": item["state"]}
             for checkpoint_id, item in items.items()),
            key=lambda item: (item["branch"], item["sequence"]),
        )


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
        self._lock_state = threading.local()
        self._lock_guard = threading.RLock()

    def create(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.root.exists() and any(self.root.iterdir()):
            raise ProjectExistsError("工程已存在；请使用 resume、retry 或 rewind，禁止重复 new。")
        self.root.mkdir(parents=True, exist_ok=False)
        manifest = {"format_version": FORMAT_VERSION, "project_id": self.project_id, "current_branch": "main", "current_checkpoint": None, "failed_step": None, "created_at": _now(), "updated_at": _now()}
        atomic_json(self.root / "manifest.json", manifest)
        snapshot = config or {}
        atomic_json(self.root / "project.yaml", snapshot)
        atomic_json(self.root / "runtime_policy.json", {"policy": snapshot, "sha256": content_hash(snapshot)})
        atomic_json(self.root / "branches.json", {"format_version": FORMAT_VERSION, "branches": {"main": {
            "parent": None, "from_checkpoint": None, "created_at": _now(),
            "runtime_policy": snapshot, "runtime_policy_hash": content_hash(snapshot),
        }}})
        self.events.append("project_created", branch="main")
        return manifest

    def manifest(self) -> dict[str, Any]:
        data = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        if data.get("format_version") != FORMAT_VERSION:
            raise CorruptProjectError("工程版本不受支持。")
        return data

    def pending_transaction(self) -> dict[str, Any] | None:
        """Read a pending intent without recovering or mutating project data.

        Ordinary reads must never decide the fate of a writer's transaction.  A
        reader instead projects the last complete manifest recorded in the
        intent while the writer owns the project lock.
        """
        pending = self.root / "transactions/pending.json"
        if not pending.is_file():
            return None
        try:
            intent = json.loads(pending.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CorruptProjectError("工程事务记录无法读取。") from exc
        if not isinstance(intent, dict) or intent.get("format_version") != FORMAT_VERSION:
            raise CorruptProjectError("工程事务记录版本无效。")
        return intent

    def read_manifest(self) -> dict[str, Any]:
        """Return a stable, read-only manifest projection.

        During a checkpoint/branch commit the intent contains the previously
        committed manifest.  Serving that version keeps GET paths readable and,
        crucially, leaves recovery/rollback exclusively to lock-owning writers.
        """
        intent = self.pending_transaction()
        previous = intent.get("previous_manifest") if intent else None
        if isinstance(previous, dict) and previous.get("format_version") == FORMAT_VERSION:
            return dict(previous)
        return self.manifest()

    def corruption_context(self, operation: str) -> dict[str, Any]:
        """Build a sanitized server-log context for PROJECT_CORRUPT failures."""
        context: dict[str, Any] = {
            "operation": operation,
            "project_id": self.project_id,
            "lock_owned_by_current": bool(getattr(self._lock_state, "depth", 0)),
            "lock_file_present": (self.root / ".lock").is_file(),
        }
        try:
            intent = self.pending_transaction()
        except CorruptProjectError:
            context["transaction"] = "unreadable"
            return context
        if intent:
            context.update(
                transaction_kind=intent.get("kind"),
                transaction_phase=intent.get("status"),
                source_checkpoint=intent.get("from_checkpoint"),
                target_checkpoint=intent.get("checkpoint_id"),
            )
        return context

    def checkpoint_context(self, state: str, context: Any, *, branch: str | None = None) -> str:
        return self.checkpoint(state, context.dump_snapshot(), branch=branch)

    def checkpoint(self, state: str, data: dict[str, Any], *, branch: str | None = None) -> str:
        self._recover_transaction()
        manifest = self.manifest()
        previous_manifest = dict(manifest)
        active = branch or manifest["current_branch"]
        previous = manifest.get("current_checkpoint")
        sequence = 1 if not previous or previous.get("branch") != active else int(previous["sequence"]) + 1
        prepared = self.checkpoints.prepare(active, sequence, state, data)
        transaction = {"format_version": FORMAT_VERSION, "kind": "checkpoint", "status": "intent",
                       "branch": active, "sequence": sequence, "state": state, "data": data,
                       "checkpoint_id": prepared["checkpoint_id"], "path": prepared["path"],
                       "checksum": prepared["checksum"], "previous_manifest": previous_manifest}
        atomic_json(self.root / "transactions/pending.json", transaction)
        checkpoint_id, relative, checksum = self.checkpoints.save(active, sequence, state, data, prepared=prepared)
        transaction.update(status="prepared", checkpoint_id=checkpoint_id, path=relative, checksum=checksum)
        atomic_json(self.root / "transactions/pending.json", transaction)
        pointer = {"checkpoint_id": checkpoint_id, "checksum": checksum, "branch": active, "sequence": sequence, "state": state}
        manifest.update(current_branch=active, current_checkpoint=pointer, failed_step=None, updated_at=_now())
        atomic_json(self.root / "manifest.json", manifest)
        self.events.append("step_succeeded", branch=active, state=state, checkpoint_id=checkpoint_id)
        (self.root / "transactions/pending.json").unlink(missing_ok=True)
        return checkpoint_id

    def _transaction_complete(self, intent: dict[str, Any], manifest: dict[str, Any]) -> bool:
        pointer = manifest.get("current_checkpoint") or {}
        expected_pointer = {
            "checkpoint_id": intent.get("checkpoint_id"),
            "checksum": intent.get("checksum"),
            "branch": intent.get("branch"),
            "sequence": intent.get("sequence"),
            "state": intent.get("state"),
        }
        if any(pointer.get(key) != value for key, value in expected_pointer.items()):
            return False
        if manifest.get("current_branch") != intent.get("branch"):
            return False
        try:
            self.checkpoints.validate(str(intent.get("checkpoint_id") or ""), expected=intent)
            if intent.get("kind") == "branch":
                branches = json.loads((self.root / "branches.json").read_text(encoding="utf-8"))["branches"]
                details = branches.get(intent["branch"])
                if not details or details.get("from_checkpoint") != intent.get("from_checkpoint"):
                    return False
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return False
        return True

    def _restore_manifest_after_rollback(self, intent: dict[str, Any], manifest: dict[str, Any]) -> None:
        previous = intent.get("previous_manifest")
        if isinstance(previous, dict) and previous.get("format_version") == FORMAT_VERSION:
            atomic_json(self.root / "manifest.json", previous)
            return
        pointer = manifest.get("current_checkpoint") or {}
        if pointer.get("branch") != intent.get("branch") or pointer.get("sequence") != intent.get("sequence"):
            return
        # 回退指针计算依赖检查点索引；索引不可读（如乱码索引）时不得中断回滚，
        # 退回到无指针的安全初始态，由工程健康检查/修复兜底。
        try:
            candidates = [
                item for item in self.checkpoints.list()
                if item["branch"] == intent.get("branch") and item["sequence"] < int(intent.get("sequence", 0))
            ]
            if intent.get("kind") == "branch" and intent.get("from_checkpoint"):
                try:
                    source = self.checkpoints.validate(intent["from_checkpoint"])
                    candidates = [{
                        "checkpoint_id": intent["from_checkpoint"], "checksum": source["checksum"],
                        "branch": source["branch"], "sequence": source["sequence"], "state": source["state"],
                    }]
                except (OSError, ValueError, json.JSONDecodeError):
                    candidates = []
            if candidates:
                target = max(candidates, key=lambda item: item["sequence"])
                envelope = self.checkpoints.validate(target["checkpoint_id"])
                manifest.update(
                    current_branch=envelope["branch"],
                    current_checkpoint={
                        "checkpoint_id": target["checkpoint_id"], "checksum": envelope["checksum"],
                        "branch": envelope["branch"], "sequence": envelope["sequence"], "state": envelope["state"],
                    },
                    updated_at=_now(),
                )
            else:
                manifest.update(current_branch="main", current_checkpoint=None, updated_at=_now())
        except (OSError, ValueError, json.JSONDecodeError):
            manifest.update(current_branch="main", current_checkpoint=None, updated_at=_now())
        atomic_json(self.root / "manifest.json", manifest)

    def _rollback_transaction(self, intent: dict[str, Any], manifest: dict[str, Any]) -> None:
        # Roll back every observable part, including a file written before its
        # index update. The intent contains the deterministic target up front.
        # 回滚不得被控制文件的读取失败中断：索引不可读时仅跳过依赖它的清理，
        # 事务意图中已记录的路径/ID 仍做尽力清理，且 pending.json 一定被清除，
        # 避免半回滚死锁态（残留的乱码索引可再由工程健康检查定向修复）。
        try:
            index = self.checkpoints._index()
        except (OSError, ValueError, json.JSONDecodeError):
            index = None
        try:
            expected_id = intent.get("checkpoint_id")
            expected_path = intent.get("path")
            if expected_path:
                target = (self.root / expected_path).resolve()
                if self.root.resolve() in target.parents:
                    target.unlink(missing_ok=True)
                    # 回滚后不保留误导性的空分支目录；仅删除已确认为空的目标父目录。
                    if target.parent != self.root and target.parent.exists():
                        try:
                            target.parent.rmdir()
                        except OSError:
                            pass
            if index is not None:
                if expected_id:
                    index["items"].pop(expected_id, None)
                for checkpoint_id, item in list(index["items"].items()):
                    if item["branch"] == intent["branch"] and item["sequence"] == intent["sequence"]:
                        target = (self.root / item["path"]).resolve()
                        if self.root.resolve() in target.parents:
                            target.unlink(missing_ok=True)
                        del index["items"][checkpoint_id]
                atomic_json(self.checkpoints.index_path, index)
            if intent.get("kind") == "branch":
                branches_path = self.root / "branches.json"
                branches = json.loads(branches_path.read_text(encoding="utf-8"))
                branches["branches"].pop(intent["branch"], None)
                atomic_json(branches_path, branches)
            self._restore_manifest_after_rollback(intent, manifest)
        finally:
            (self.root / "transactions/pending.json").unlink(missing_ok=True)

    def _recover_transaction(self) -> None:
        # Recovery is a write operation.  If a legacy/direct caller reaches a
        # write helper without already owning the lock, acquire it before
        # inspecting or changing the intent.  Read paths never call this method.
        if not getattr(self._lock_state, "depth", 0):
            with self.lock():
                self._recover_transaction()
            return
        if getattr(self._lock_state, "validating_transaction", False):
            return
        pending = self.root / "transactions/pending.json"
        if not pending.exists():
            return
        try:
            intent = json.loads(pending.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CorruptProjectError("工程事务记录无法读取。") from exc
        manifest = self.manifest()
        if self._transaction_complete(intent, manifest):
            checkpoint_id = intent.get("checkpoint_id")
            if intent.get("kind") == "branch":
                if not any(e.get("type") == "branch_created" and e.get("branch") == intent["branch"] for e in self.events.read_all()):
                    source = self.checkpoints.load(intent["from_checkpoint"])
                    self.events.append("branch_created", branch=intent["branch"], parent=source["branch"],
                                       from_checkpoint=intent["from_checkpoint"], recovered=True)
            elif not any(e.get("type") == "step_succeeded" and e.get("checkpoint_id") == checkpoint_id for e in self.events.read_all()):
                self.events.append("step_succeeded", branch=intent["branch"], state=intent["state"], checkpoint_id=checkpoint_id, recovered=True)
            pending.unlink(missing_ok=True)
            return
        self._rollback_transaction(intent, manifest)

    def start_step(self, state: str, **details: Any) -> None:
        self.events.append("step_started", branch=self.manifest()["current_branch"], state=state, **details)

    def fail_step(self, state: str, error: dict[str, Any]) -> None:
        manifest = self.manifest()
        manifest["failed_step"] = {"state": state, "error": error, "at": _now()}
        manifest["updated_at"] = _now()
        atomic_json(self.root / "manifest.json", manifest)
        self.events.append("step_failed", branch=manifest["current_branch"], state=state, error=error)

    def recover_pending_transaction(self) -> None:
        """Explicitly recover an interrupted write while holding the project lock."""
        with self.lock():
            self._recover_transaction()

    def resume(self, *, manifest: dict[str, Any] | None = None) -> dict[str, Any] | None:
        pointer = (manifest or self.read_manifest()).get("current_checkpoint")
        return self.checkpoints.load(pointer["checkpoint_id"])["data"] if pointer else None

    def retry(self, execute: Any, *, name: str | None = None) -> Any:
        manifest = self.manifest()
        failure = manifest.get("failed_step")
        pointer = manifest.get("current_checkpoint")
        if not failure:
            raise ValueError("当前没有失败步骤需要重试。")
        if not pointer:
            raise ValueError("失败步骤之前没有成功检查点，无法安全重试。")
        branch = self.branch_from(pointer["checkpoint_id"], name=name or f"retry-{uuid4().hex[:8]}")
        self.events.append("retry_started", branch=branch, state=failure["state"], from_checkpoint=pointer["checkpoint_id"])
        return execute(failure["state"], self.resume())

    def _rewind_stage(self, state: str, source: dict[str, Any]) -> dict[str, Any]:
        """Return the stage input boundary for an explicit rerun branch."""
        data = dict(source)
        if state in {"category_constraint", "intake_clarify"}:
            original = self.root / "intake_task.json"
            if original.is_file():
                data["task_card"] = json.loads(original.read_text(encoding="utf-8"))
        downstream = {
            "category_constraint": {
                "category_constraint_current", "category_constraint_history", "category_constraint_approval",
                "question_card", "clarification_transcript", "previous_fingerprints",
                "clarification_asked_fields", "clarification_asked_count", "clarification_remaining_budget",
                "clarification_blocking_fields", "clarification_safe_default_fields",
                "clarification_recovery_actions", "clarification_review_reason",
            },
            "intake_clarify": {
                "question_card", "clarification_transcript", "previous_fingerprints",
                "clarification_asked_fields", "clarification_asked_count", "clarification_remaining_budget",
                "clarification_blocking_fields", "clarification_safe_default_fields",
                "clarification_recovery_actions", "clarification_review_reason",
            },
            "confirmation_build": {"task_specification", "task_markdown", "task_revision",
                                   "task_revision_history", "task_approval", "readiness"},
            "initial_candidate_generation": {"skill_invocations", "style_selections", "render_plans",
                                             "skill_invocation_current", "skill_invocation_history",
                                             "skill_invocation_approval", "candidates"},
            "master_candidate_selection": {"master_asset", "selected_master"},
        }
        order = ["category_constraint", "intake_clarify", "confirmation_build",
                 "initial_candidate_generation", "master_candidate_selection"]
        common_after_master = {"asset", "current_asset", "inspection_asset", "inspection", "round",
                               "best_asset", "available_actions", "calibration_status", "termination_satisfied",
                               "termination_reason", "latest_checked_asset_hash", "selected_policy",
                               "human_tune_mode", "final_asset", "frozen_delivery", "delivery_envelope",
                               "delivery_files", "completed"}
        if state not in order and state not in {"self_check_iteration", "human_prompt_iteration", "final_approval"}:
            raise ValueError("该历史阶段暂不支持重跑。")
        start = order.index(state) if state in order else len(order)
        for stage in order[start:]:
            for key in downstream.get(stage, set()):
                data.pop(key, None)
        if state in order and order.index(state) <= order.index("master_candidate_selection"):
            for key in common_after_master:
                data.pop(key, None)
        data.update(state=state, waiting=False)
        if state == "category_constraint":
            data["phase"] = "ready_for_category_match"
        elif state == "intake_clarify":
            data["phase"] = "ready_for_clarification"
        elif state == "confirmation_build":
            data["phase"] = "ready_for_taskbook"
        elif state == "initial_candidate_generation":
            data["phase"] = "ready_for_style_direction"
        elif state == "master_candidate_selection":
            # 风格库模式固定 5 张；「不使用数据库」模式候选数等于渲染方案数（candidate_concurrency）。
            expected = len(data.get("render_plans") or []) or 5
            if len(data.get("candidates") or []) != expected:
                raise ValueError(f"主图选择重跑需要保留完整的 {expected} 张候选图。")
            data.update(phase="waiting_master_selection", waiting=True)
        elif state == "self_check_iteration":
            if not (data.get("master_asset") or data.get("asset")):
                raise ValueError("画面质检重跑缺少可检查主图。")
            for key in common_after_master:
                if key not in {"asset", "current_asset"}:
                    data.pop(key, None)
            data.update(phase="ready_for_quality_inspection", waiting=False)
        elif state == "human_prompt_iteration":
            data.update(phase="waiting_human_tune", waiting=True, human_tune_mode=True)
        else:
            data.update(phase="ready_for_final_approval", waiting=False)
        data.pop("domain_state", None)
        return data

    def branch_from(self, checkpoint_id: str, *, name: str | None = None,
                    mode: str = "fork_after", verify: Callable[[], Any] | None = None) -> str:
        self._recover_transaction()
        source = self.checkpoints.load(checkpoint_id)
        branches_path = self.root / "branches.json"
        branches = json.loads(branches_path.read_text(encoding="utf-8"))
        branch = name or f"branch-{uuid4().hex[:8]}"
        if branch in branches["branches"]:
            raise ValueError("分支名称已存在。")
        if mode not in {"fork_after", "rerun_stage"}:
            raise ValueError("分支模式无效。")
        branch_data = (self._rewind_stage(source["state"], source["data"])
                       if mode == "rerun_stage" else source["data"])
        prepared = self.checkpoints.prepare(branch, 1, source["state"], branch_data)
        previous_manifest = self.manifest()
        transaction = {"format_version": FORMAT_VERSION, "kind": "branch", "status": "intent",
                       "branch": branch, "sequence": 1, "state": source["state"], "data": branch_data,
                       "checkpoint_id": prepared["checkpoint_id"], "path": prepared["path"],
                       "checksum": prepared["checksum"], "from_checkpoint": checkpoint_id,
                       "previous_manifest": previous_manifest}
        atomic_json(self.root / "transactions/pending.json", transaction)
        parent_details = branches["branches"][source["branch"]]
        policy = parent_details.get("runtime_policy")
        if policy is None:
            policy = json.loads((self.root / "runtime_policy.json").read_text(encoding="utf-8"))["policy"]
            parent_details.update(runtime_policy=policy, runtime_policy_hash=content_hash(policy))
        branches["branches"][branch] = {
            "parent": source["branch"], "from_checkpoint": checkpoint_id, "created_at": _now(),
            "mode": mode,
            "runtime_policy": policy, "runtime_policy_hash": content_hash(policy),
        }
        atomic_json(branches_path, branches)
        manifest = dict(previous_manifest)
        new_id, relative, checksum = self.checkpoints.save(branch, 1, source["state"], branch_data, prepared=prepared)
        manifest.update(current_branch=branch, current_checkpoint={"checkpoint_id": new_id, "checksum": checksum, "branch": branch, "sequence": 1, "state": source["state"]}, failed_step=None, updated_at=_now())
        atomic_json(self.root / "manifest.json", manifest)
        try:
            self.checkpoints.validate(new_id, expected=transaction)
            if not self._transaction_complete(transaction, manifest):
                raise CorruptProjectError("新分支持久化校验失败。")
            if verify:
                self._lock_state.validating_transaction = True
                verify()
        except Exception:
            self._rollback_transaction(transaction, manifest)
            raise
        finally:
            self._lock_state.validating_transaction = False
        self.events.append("branch_created", branch=branch, parent=source["branch"],
                           from_checkpoint=checkpoint_id, mode=mode)
        (self.root / "transactions/pending.json").unlink(missing_ok=True)
        return branch

    def branches(self) -> dict[str, Any]:
        """Expose branch lineage from one stable, read-only commit projection.

        A branch transaction writes ``branches.json`` and the checkpoint index
        before swapping the manifest.  While its pending intent exists, hide
        those target records and serve the previous manifest just like the
        project-detail GET; readers must never observe a half-committed branch.
        """
        # Writers hold the exclusive project lock for the whole transaction.
        # Taking a shared lock makes the four reads below one commit projection:
        # a reader either runs before the writer creates pending.json or after
        # it removes it, never across either boundary.  The shared lock is
        # deliberately read-only; unlike ``lock()`` it does not rewrite .lock
        # and it never invokes transaction recovery.
        with self.read_lock():
            intent = self.pending_transaction()
            manifest = self.read_manifest()
            branches = json.loads((self.root / "branches.json").read_text(encoding="utf-8"))["branches"]
            checkpoints = self.checkpoints.list()
        if intent:
            pending_checkpoint = intent.get("checkpoint_id")
            checkpoints = [item for item in checkpoints if item["checkpoint_id"] != pending_checkpoint]
            if intent.get("kind") == "branch":
                branches.pop(str(intent.get("branch") or ""), None)
        checkpoints = [item for item in checkpoints if item["branch"] in branches]
        return {
            "current_branch": manifest["current_branch"],
            "current_checkpoint_id": (manifest.get("current_checkpoint") or {}).get("checkpoint_id"),
            "items": [
                {"name": name, **details,
                 "current": name == manifest["current_branch"],
                 "checkpoints": [item for item in checkpoints if item["branch"] == name]}
                for name, details in branches.items()
            ],
        }

    @contextmanager
    def read_lock(self) -> Iterator[None]:
        """Wait for writers and hold a non-mutating shared project lock."""
        if getattr(self._lock_state, "depth", 0):
            yield
            return
        lock_path = self.root / ".lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        stream = lock_path.open("a+b", buffering=0)
        try:
            portalocker.lock(stream, portalocker.LOCK_SH)
            yield
        finally:
            portalocker.unlock(stream)
            stream.close()

    def check_health(self, *, repair: bool = False) -> dict[str, Any]:
        """Inspect project references and optionally repair unambiguous index drift.

        Repairs are checksum-driven: an index record is changed only when exactly
        one valid checkpoint file has the same checksum and names an existing
        branch. The original control files are backed up before any write.
        """
        manifest = self.manifest()
        branches_path = self.root / "branches.json"
        branches_document = json.loads(branches_path.read_text(encoding="utf-8"))
        branch_defs = branches_document.get("branches", {})
        index = self.checkpoints._index()
        issues: list[dict[str, Any]] = []
        repairs: list[dict[str, Any]] = []
        physical: dict[str, list[dict[str, Any]]] = {}

        for path in (self.root / "checkpoints").glob("**/*.json"):
            if path == self.checkpoints.index_path:
                continue
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                checksum = document.pop("checksum", None)
                if document.get("format_version") != FORMAT_VERSION or checksum != content_hash(document):
                    continue
                relative = path.relative_to(self.root).as_posix()
                physical.setdefault(str(checksum), []).append({**document, "path": relative, "checksum": checksum})
            except (OSError, ValueError, json.JSONDecodeError):
                continue

        for checkpoint_id, item in list(index.get("items", {}).items()):
            try:
                self.checkpoints.validate(checkpoint_id)
                continue
            except (OSError, ValueError, json.JSONDecodeError):
                candidates = [
                    candidate for candidate in physical.get(str(item.get("checksum")), [])
                    if candidate.get("branch") in branch_defs
                    and checkpoint_id == f"checkpoint_{str(candidate.get('checksum'))[:24]}"
                ]
                issue = {
                    "code": "CHECKPOINT_REFERENCE_INVALID",
                    "checkpoint_id": checkpoint_id,
                    "message": "检查点索引无法读取对应文件或与文件内容不一致。",
                    "repairable": len(candidates) == 1,
                }
                if len(candidates) == 1:
                    candidate = candidates[0]
                    replacement = {
                        "path": candidate["path"], "checksum": candidate["checksum"],
                        "branch": candidate["branch"], "sequence": candidate["sequence"],
                        "state": candidate["state"],
                    }
                    repairs.append({"checkpoint_id": checkpoint_id, "replacement": replacement})
                issues.append(issue)

        pointer = manifest.get("current_checkpoint") or {}
        if pointer:
            target_repair = next((item for item in repairs if item["checkpoint_id"] == pointer.get("checkpoint_id")), None)
            target = target_repair["replacement"] if target_repair else index.get("items", {}).get(pointer.get("checkpoint_id"), {})
            for field in ("checksum", "branch", "sequence", "state"):
                if pointer.get(field) != target.get(field):
                    issues.append({
                        "code": "MANIFEST_POINTER_INVALID", "checkpoint_id": pointer.get("checkpoint_id"),
                        "message": "工程当前指针与检查点记录不一致。", "repairable": False,
                    })
                    break

        backup = None
        if repair and repairs:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_dir = self.root / "backups" / f"health-{stamp}-{uuid4().hex[:8]}"
            backup_dir.mkdir(parents=True, exist_ok=False)
            for source in (self.checkpoints.index_path, branches_path, self.root / "manifest.json"):
                if source.is_file():
                    shutil.copy2(source, backup_dir / source.name)
            for item in repairs:
                index["items"][item["checkpoint_id"]] = item["replacement"]
            atomic_json(self.checkpoints.index_path, index)
            for item in repairs:
                self.checkpoints.validate(item["checkpoint_id"])
            backup = backup_dir.relative_to(self.root).as_posix()
            self.events.append(
                "project_index_repaired", repaired_checkpoints=[item["checkpoint_id"] for item in repairs],
                backup=backup,
            )

        healthy = not issues or (repair and len(repairs) == len(issues))
        return {
            "project_id": self.project_id,
            "mode": "repair" if repair else "dry-run",
            "healthy": healthy,
            "issues": issues,
            "repairs": [{"checkpoint_id": item["checkpoint_id"]} for item in repairs],
            "applied": len(repairs) if repair else 0,
            "backup": backup,
        }

    def progress_snapshots(self, *, manifest: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Return immutable snapshots along the active branch lineage.

        A branch stores only its fork checkpoint and subsequent work.  Walking
        through ``parent``/``from_checkpoint`` preserves the earlier completed
        stages in the progress card after a historical fork without exposing
        unrelated branch contents.
        """
        manifest = manifest or self.read_manifest()
        branch_defs = json.loads((self.root / "branches.json").read_text(encoding="utf-8"))["branches"]
        checkpoints = self.checkpoints.list()
        by_branch: dict[str, list[dict[str, Any]]] = {}
        for item in checkpoints:
            by_branch.setdefault(item["branch"], []).append(item)

        def lineage(branch: str, head_id: str | None, visiting: set[str]) -> list[dict[str, Any]]:
            if branch in visiting or branch not in branch_defs:
                raise CorruptProjectError("分支谱系损坏。")
            visiting = {*visiting, branch}
            details = branch_defs[branch]
            result: list[dict[str, Any]] = []
            parent = details.get("parent")
            fork_id = details.get("from_checkpoint")
            if parent and fork_id:
                result.extend(lineage(parent, fork_id, visiting))

            items = by_branch.get(branch, [])
            if head_id:
                target = next((item for item in items if item["checkpoint_id"] == head_id), None)
                if target is None:
                    raise CorruptProjectError("分支头检查点不存在。")
                items = [item for item in items if item["sequence"] <= target["sequence"]]
            result.extend(items)
            return result

        pointer = manifest.get("current_checkpoint") or {}
        if not pointer:
            return []
        ordered = lineage(manifest["current_branch"], pointer.get("checkpoint_id"), set())
        seen: set[str] = set()
        response: list[dict[str, Any]] = []
        for item in ordered:
            checkpoint_id = item["checkpoint_id"]
            if checkpoint_id in seen:
                continue
            seen.add(checkpoint_id)
            envelope = self.checkpoints.load(checkpoint_id)
            response.append({**item, "snapshot": envelope["data"]})
        return response

    def switch_branch(self, checkpoint_id: str) -> dict[str, Any]:
        """Move the active read pointer to an indexed checkpoint without changing history."""
        self._recover_transaction()
        target = self.checkpoints.load(checkpoint_id)
        branches = json.loads((self.root / "branches.json").read_text(encoding="utf-8"))["branches"]
        if target["branch"] not in branches:
            raise CorruptProjectError("检查点引用了不存在的分支。")
        branch_checkpoints = [item for item in self.checkpoints.list() if item["branch"] == target["branch"]]
        head = max(branch_checkpoints, key=lambda item: item["sequence"])
        if head["checkpoint_id"] != checkpoint_id:
            raise ValueError("历史节点只读；如需继续，请从该节点创建新分支。")
        manifest = self.manifest()
        manifest.update(
            current_branch=target["branch"],
            current_checkpoint={
                "checkpoint_id": checkpoint_id, "checksum": target["checksum"],
                "branch": target["branch"], "sequence": target["sequence"], "state": target["state"],
            },
            failed_step=None,
            updated_at=_now(),
        )
        atomic_json(self.root / "manifest.json", manifest)
        policy = branches[target["branch"]].get("runtime_policy")
        if policy is not None:
            atomic_json(self.root / "runtime_policy.json", {"policy": policy, "sha256": content_hash(policy)})
        self.events.append("branch_switched", branch=target["branch"], checkpoint_id=checkpoint_id)
        return self.branches()

    @contextmanager
    def lock(self) -> Iterator[None]:
        # The runner owns the project transaction. Helpers such as the candidate
        # batch may enter it again on the same store instance without deadlock.
        depth = getattr(self._lock_state, "depth", 0)
        if depth:
            self._lock_state.depth = depth + 1
            try:
                yield
            finally:
                self._lock_state.depth -= 1
            return
        lock_path = self.root / ".lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.touch(mode=0o600, exist_ok=True)
        stream = lock_path.open("r+b", buffering=0)
        try:
            portalocker.lock(stream, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except portalocker.exceptions.LockException as exc:
            stream.close()
            raise ProjectLockError("该工程正在由另一个进程处理，请稍后重试。") from exc
        stream.seek(0)
        stream.truncate()
        stream.write(json.dumps({"pid": os.getpid(), "started_at": _now(), "thread": threading.get_ident()}).encode())
        stream.flush()
        os.fsync(stream.fileno())
        try:
            self._lock_state.depth = 1
            yield
        finally:
            self._lock_state.depth = 0
            portalocker.unlock(stream)
            stream.close()

    def idempotency_key(self, state: str, checkpoint_hash: str, prompt_hash: str, model_hash: str, reference_hash: str = "") -> str:
        return content_hash([state, checkpoint_hash, prompt_hash, model_hash, reference_hash])

    def history(self) -> list[dict[str, Any]]:
        return self.events.read_all()

    def revise_policy(self, policy: dict[str, Any], *, confirmed: bool, actor: str) -> str:
        """Configuration changes are auditable branches, never in-place edits."""
        if not confirmed or not actor:
            raise PermissionError("配置修订需要人工确认和操作者身份。")
        pointer = self.manifest().get("current_checkpoint")
        if not pointer:
            raise ValueError("配置修订前必须存在安全检查点。")
        branches_path = self.root / "branches.json"
        branches = json.loads(branches_path.read_text(encoding="utf-8"))
        active = self.manifest()["current_branch"]
        current = json.loads((self.root / "runtime_policy.json").read_text(encoding="utf-8"))["policy"]
        branches["branches"][active].update(runtime_policy=current, runtime_policy_hash=content_hash(current))
        atomic_json(branches_path, branches)
        branch = self.branch_from(pointer["checkpoint_id"], name=f"policy-{content_hash(policy)[:8]}")
        branches = json.loads(branches_path.read_text(encoding="utf-8"))
        branches["branches"][branch].update(runtime_policy=policy, runtime_policy_hash=content_hash(policy))
        atomic_json(branches_path, branches)
        atomic_json(self.root / "runtime_policy.json", {"policy": policy, "sha256": content_hash(policy)})
        self.events.append("runtime_policy_revised", branch=branch, actor=actor,
                           policy_hash=content_hash(policy), confirmed=True)
        return branch
