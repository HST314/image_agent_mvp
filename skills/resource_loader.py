"""Policy-aware resource loading boundary."""
from __future__ import annotations
from typing import Callable, TypeVar
from uuid import uuid4
from skills.errors import ResourceError

T = TypeVar("T")

def load_with_policy(load: Callable[[], T], *, resource: str,
                     allow_degradation: bool, fallback: T | None = None,
                     emit: Callable[[dict[str, str]], None] | None = None) -> T:
    try:
        return load()
    except ResourceError as exc:
        if not allow_degradation or fallback is None:
            raise
        degraded = ResourceError(exc.code, resource, exc.trace_id or f"trace_{uuid4().hex}",
                                 degradation="fallback", detail=exc.detail)
        if emit:
            emit(degraded.as_dict())
        return fallback
