"""Context propagation across every boundary in the harness (D2, D4, D5).

Three carriers:
  - contextvars, for same-process asyncio tasks (automatic; run_in_context
    exists only to make the guarantee explicit and testable).
  - environment variables, for subprocess children (D4): TRACEPARENT,
    TRACESTATE, BAGGAGE, per the OTel spec's "Environment Variables as
    Context Propagation Carriers".
  - W3C traceparent/tracestate HTTP headers, for the HTTP runner (D5).

Baggage carries only agenttrace.run_id and agenttrace.depth (never payload
content — env vars and headers can be visible to other processes/services).
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable
from typing import ParamSpec, TypeVar

from opentelemetry import baggage
from opentelemetry import context as otel_context
from opentelemetry.propagate import get_global_textmap

from agenttrace.telemetry.semconv import AT_BAGGAGE_DEPTH, AT_BAGGAGE_RUN_ID

_P = ParamSpec("_P")
_T = TypeVar("_T")

ENV_TRACEPARENT = "TRACEPARENT"
ENV_TRACESTATE = "TRACESTATE"
ENV_BAGGAGE = "BAGGAGE"

BAGGAGE_RUN_ID = AT_BAGGAGE_RUN_ID
BAGGAGE_DEPTH = AT_BAGGAGE_DEPTH


class MissingTraceparentError(RuntimeError):
    """Raised in strict mode when a child starts with no parent context."""


def run_in_context(fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> _T:  # noqa: UP047
    """Run fn in a copy of the current context. asyncio.create_task /
    asyncio.to_thread already do this automatically; this helper exists for
    the one path that doesn't: a raw thread or executor submission. Prefer
    asyncio.create_task directly — reach for this only when you must use
    Thread/ThreadPoolExecutor/run_in_executor."""
    ctx = contextvars.copy_context()
    return ctx.run(fn, *args, **kwargs)


def with_run_baggage(run_id: str, depth: int) -> otel_context.Context:
    ctx = otel_context.get_current()
    ctx = baggage.set_baggage(BAGGAGE_RUN_ID, run_id, context=ctx)
    ctx = baggage.set_baggage(BAGGAGE_DEPTH, str(depth), context=ctx)
    return ctx


def inject_to_carrier() -> dict[str, str]:
    """W3C traceparent/tracestate + baggage from the CURRENT context."""
    carrier: dict[str, str] = {}
    get_global_textmap().inject(carrier)
    return carrier


def extract_from_carrier(carrier: dict[str, str]) -> otel_context.Context:
    return get_global_textmap().extract(carrier)


def env_updates_for_subprocess(run_id: str, depth: int) -> dict[str, str]:
    """Env-var additions for a subprocess child, injected from *inside* the
    execute_tool spawn_subagent span so the child's parent is that span."""
    token = otel_context.attach(with_run_baggage(run_id, depth))
    try:
        carrier = inject_to_carrier()
    finally:
        otel_context.detach(token)
    updates: dict[str, str] = {}
    if "traceparent" in carrier:
        updates[ENV_TRACEPARENT] = carrier["traceparent"]
    if "tracestate" in carrier:
        updates[ENV_TRACESTATE] = carrier["tracestate"]
    if "baggage" in carrier:
        updates[ENV_BAGGAGE] = carrier["baggage"]
    return updates


def extract_context_from_env(env: dict[str, str], *, strict: bool = True) -> otel_context.Context:
    if strict and ENV_TRACEPARENT not in env:
        raise MissingTraceparentError(
            "child process started without TRACEPARENT in strict mode; "
            "refusing to silently become a new root"
        )
    carrier = {
        "traceparent": env.get(ENV_TRACEPARENT, ""),
        "tracestate": env.get(ENV_TRACESTATE, ""),
        "baggage": env.get(ENV_BAGGAGE, ""),
    }
    carrier = {k: v for k, v in carrier.items() if v}
    return extract_from_carrier(carrier)


def headers_for_http() -> dict[str, str]:
    """W3C headers injected from inside the CLIENT-side spawn span."""
    return inject_to_carrier()


def extract_context_from_headers(headers: dict[str, str]) -> otel_context.Context:
    return extract_from_carrier(headers)
