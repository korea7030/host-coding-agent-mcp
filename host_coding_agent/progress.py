from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator


ProgressEmitter = Callable[[str, str, dict[str, Any] | None], None]
_emitter: ContextVar[ProgressEmitter | None] = ContextVar(
    "host_coding_agent_progress_emitter",
    default=None,
)


@contextmanager
def progress_events(emitter: ProgressEmitter) -> Iterator[None]:
    token = _emitter.set(emitter)
    try:
        yield
    finally:
        _emitter.reset(token)


def emit_progress(
    stage: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    emitter = _emitter.get()
    if emitter is not None:
        emitter(stage, message, details)
