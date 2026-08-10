"""Translate private project artifacts at the provider boundary."""
from __future__ import annotations

import base64
from typing import Any

from storage.project_store import CorruptProjectError, ProjectStore


class ArtifactNotFoundError(FileNotFoundError):
    code = "ARTIFACT_NOT_FOUND"


class ArtifactCorruptError(ValueError):
    code = "ARTIFACT_CORRUPT"


class ProviderImageAdapter:
    """Resolve internal URIs into provider-consumable, self-contained inputs."""

    def __init__(self, store: ProjectStore) -> None:
        self.store = store

    def resolve(self, value: str | dict[str, Any]) -> str:
        uri = value.get("uri") if isinstance(value, dict) else value
        if not isinstance(uri, str) or not uri:
            raise ArtifactNotFoundError("图片资源标识为空。")
        if not uri.startswith("artifact://"):
            return uri
        artifact_id = uri.removeprefix("artifact://")
        try:
            path, record = self.store.artifacts.resolve(artifact_id)
        except FileNotFoundError as exc:
            raise ArtifactNotFoundError(f"图片资源不存在：{artifact_id}") from exc
        except CorruptProjectError as exc:
            raise ArtifactCorruptError(f"图片资源完整性校验失败：{artifact_id}") from exc
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{record['mime_type']};base64,{encoded}"

    def resolve_all(self, values: list[str]) -> list[str]:
        return [self.resolve(value) for value in values]
