"""The seam between the harness (Phase 1) and instrumentation (Phase 2).

The harness never imports OpenTelemetry directly. It calls this Protocol,
which defaults to a no-op implementation so Phase 1 has zero telemetry
dependency and identical behavior with instrumentation on or off (a
requirement checked by tests/integration/test_otel_disabled_parity.py).

Content hashing/storage (D8) and spawn-id bookkeeping (D6) live behind this
seam too, not in the harness: the harness reports *what happened* (a tool
returned this text with this taint), and the telemetry layer decides how
that becomes span attributes and content-store refs.

setup.py installs the real OTel-backed implementation via set_hooks().
"""

from __future__ import annotations

import contextlib
import uuid
from contextlib import AbstractContextManager
from typing import Any, Protocol


class HarnessHooks(Protocol):
    def workflow_span(self, span_name: str) -> AbstractContextManager[None]: ...

    def agent_span(
        self, span_name: str, *, agent_name: str, kind: str, role: str, depth: int
    ) -> AbstractContextManager[None]: ...

    def tool_span(self, span_name: str, *, tool_name: str) -> AbstractContextManager[None]: ...

    def chat_span(self, span_name: str, *, model: str) -> AbstractContextManager[None]: ...

    def spawn_intent(
        self, *, role: str, objective: str, depth: int, runner_name: str
    ) -> str:
        """Record spawn_intent event + agenttrace.spawn.id; returns spawn_id."""
        ...

    def spawn_outcome(self, spawn_id: str, outcome: str) -> None: ...

    def set_tool_result(
        self,
        *,
        args: dict[str, Any],
        result: str,
        taint: str,
        error_type: str | None = None,
    ) -> None: ...

    def set_chat_attrs(
        self, *, tool_definitions: list[dict[str, Any]], input_tokens: int, output_tokens: int
    ) -> None: ...

    def set_agent_result(self, result: str) -> None: ...

    def current_trace_id(self) -> str | None:
        """Hex trace ID of the currently open span, if any — lets a caller
        (demo.py, verify.py) know which trace to fetch afterwards without
        the harness importing OTel directly."""
        ...


class NoOpHooks:
    def workflow_span(self, span_name: str) -> AbstractContextManager[None]:
        return contextlib.nullcontext()

    def agent_span(
        self, span_name: str, *, agent_name: str, kind: str, role: str, depth: int
    ) -> AbstractContextManager[None]:
        return contextlib.nullcontext()

    def tool_span(self, span_name: str, *, tool_name: str) -> AbstractContextManager[None]:
        return contextlib.nullcontext()

    def chat_span(self, span_name: str, *, model: str) -> AbstractContextManager[None]:
        return contextlib.nullcontext()

    def spawn_intent(
        self, *, role: str, objective: str, depth: int, runner_name: str
    ) -> str:
        return uuid.uuid4().hex

    def spawn_outcome(self, spawn_id: str, outcome: str) -> None:
        return None

    def set_tool_result(
        self,
        *,
        args: dict[str, Any],
        result: str,
        taint: str,
        error_type: str | None = None,
    ) -> None:
        return None

    def set_chat_attrs(
        self, *, tool_definitions: list[dict[str, Any]], input_tokens: int, output_tokens: int
    ) -> None:
        return None

    def set_agent_result(self, result: str) -> None:
        return None

    def current_trace_id(self) -> str | None:
        return None


_hooks: HarnessHooks = NoOpHooks()


def get_hooks() -> HarnessHooks:
    return _hooks


def set_hooks(hooks: HarnessHooks) -> None:
    global _hooks
    _hooks = hooks


def reset_hooks() -> None:
    global _hooks
    _hooks = NoOpHooks()
