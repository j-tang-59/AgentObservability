"""E2: deciding a trace is safe to evaluate.

A trace is complete when: the root span has ended, every spawn_intent has a
matching spawn.outcome (or the spawn is still legitimately in flight), and
no new spans have arrived for a quiescence window. The first two are
structural and checked here directly; the quiescence window is a property
of the *fetch* loop (only meaningful against a live backend that can be
polled repeatedly), implemented as `poll_until_complete` for a real Tempo
deployment — bench/suite.py's offline runs are complete by construction
(the harness run has already finished before spans are read back).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from agenttrace.telemetry import semconv
from agenttrace.trace_model import SpanRecord


@dataclass
class CompletenessResult:
    complete: bool
    root_ended: bool
    unresolved_spawn_ids: list[str]


def check_completeness(spans: list[SpanRecord]) -> CompletenessResult:
    roots = [s for s in spans if not s.parent_span_id]
    root_ended = any(r.end_time_unix_nano > 0 for r in roots) if roots else False

    tool_spans = [s for s in spans if s.name == semconv.tool_span_name("spawn_subagent")]
    unresolved: list[str] = []
    for tool_span in tool_spans:
        for event in tool_span.events:
            if event["name"] != semconv.EVENT_SPAWN_INTENT:
                continue
            spawn_id = event["attributes"].get(semconv.AT_SPAWN_ID)
            if semconv.AT_SPAWN_OUTCOME not in tool_span.attributes:
                unresolved.append(str(spawn_id))

    return CompletenessResult(
        complete=root_ended and not unresolved,
        root_ended=root_ended,
        unresolved_spawn_ids=unresolved,
    )


def poll_until_complete(
    fetch: Callable[[], list[SpanRecord]],
    *,
    quiescence_s: float = 5.0,
    timeout_s: float = 60.0,
    poll_interval_s: float = 1.0,
) -> list[SpanRecord]:
    """Poll `fetch` (e.g. Tempo query-by-ID) until span count stops growing
    for `quiescence_s` AND the structural check passes, or `timeout_s`
    elapses — whichever comes first. Returns whatever was last fetched.

    Tradeoff (E2): a longer quiescence window means fewer false orphans but
    slower verdicts; callers needing a different tradeoff pass their own
    quiescence_s."""
    deadline = time.monotonic() + timeout_s
    last_count = -1
    stable_since: float | None = None
    spans: list[SpanRecord] = []

    while time.monotonic() < deadline:
        spans = fetch()
        result = check_completeness(spans)
        if len(spans) == last_count:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= quiescence_s and result.complete:
                return spans
        else:
            stable_since = None
        last_count = len(spans)
        time.sleep(poll_interval_s)

    return spans
