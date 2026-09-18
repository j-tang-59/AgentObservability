"""Phase 2 gate: propagation coverage holds across mixed runner plans,
fan-out, and depth — one trace ID, one root, every spawn_intent matched by
one correctly parented child, zero missing parents, zero SDK drops.

This exercises the real cross-process paths (D4 env-var carrier, D5 HTTP
headers), not just in-process contextvars, using a shared file exporter
(telemetry/file_exporter.py) so spans from the parent, subprocess children,
and the HTTP server all land in one place without a real Collector.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from agenttrace.harness.fake_model import FakeChatModel
from agenttrace.harness.graph import new_run_id, run_workflow
from agenttrace.harness.run_context import RunContext
from agenttrace.harness.runners.http import HttpRunner
from agenttrace.harness.runners.inproc import InprocRunner
from agenttrace.harness.runners.subprocess import SubprocessRunner
from agenttrace.harness.scripts import default_script, make_objective
from agenttrace.telemetry import semconv
from agenttrace.telemetry.content_store import get_default_store
from agenttrace.telemetry.file_exporter import read_spans
from agenttrace.telemetry.hooks import reset_hooks, set_hooks
from agenttrace.telemetry.otel_hooks import OtelHooks
from agenttrace.telemetry.setup import setup_tracing

RUNNER_NAMES = [semconv.RUNNER_INPROC, semconv.RUNNER_SUBPROCESS, semconv.RUNNER_HTTP]


def _verify_coverage(spans: list[dict[str, object]]) -> None:
    trace_ids = {s["trace_id"] for s in spans}
    assert len(trace_ids) == 1, f"expected exactly one trace ID, got {trace_ids}"

    by_span_id = {s["span_id"]: s for s in spans}
    roots = [s for s in spans if not s["parent_span_id"]]
    assert len(roots) == 1, f"expected exactly one root span, got {[r['name'] for r in roots]}"

    missing_parents = [
        s for s in spans if s["parent_span_id"] and s["parent_span_id"] not in by_span_id
    ]
    assert not missing_parents, f"spans referencing an absent parent: {missing_parents}"

    tool_spans = [s for s in spans if s["name"] == semconv.tool_span_name("spawn_subagent")]
    agent_spans = [s for s in spans if s["name"].startswith("invoke_agent")]

    for tool_span in tool_spans:
        intent_events = [
            e for e in tool_span["events"] if e["name"] == semconv.EVENT_SPAWN_INTENT  # type: ignore[index]
        ]
        assert len(intent_events) == 1, f"expected one spawn_intent event, got {len(intent_events)}"
        spawn_id = intent_events[0]["attributes"][semconv.AT_SPAWN_ID]
        outcome = tool_span["attributes"].get(semconv.AT_SPAWN_OUTCOME)  # type: ignore[union-attr]
        assert outcome == "returned", f"spawn {spawn_id} outcome was {outcome!r}, not 'returned'"

        # A spawn_id can legitimately match TWO agent spans for an
        # HTTP-routed child (D5): a CLIENT-kind wrapper opened by the
        # caller's HttpRunner, and the server's own INTERNAL span for the
        # actual work — both carry the same spawn_id by design. verify.py's
        # real coverage math already only requires >=1 correctly-parented
        # match (not exactly 1); mirror that here rather than a stricter,
        # HTTP-incompatible check.
        matches = [a for a in agent_spans if a["attributes"].get(semconv.AT_SPAWN_ID) == spawn_id]  # type: ignore[union-attr]
        assert matches, f"spawn {spawn_id} matched by 0 agent spans, want at least 1"
        correctly_parented = [a for a in matches if a["parent_span_id"] == tool_span["span_id"]]
        assert correctly_parented, (
            f"spawn {spawn_id}: none of {len(matches)} matching agent span(s) are parented to "
            f"their own execute_tool spawn_subagent span {tool_span['span_id']}"
        )


async def _run_one(*, fanout: int, depth: int, runner_plan: list[str], http_base_url: str) -> RunContext:
    model = FakeChatModel(default_script)
    run_ctx = RunContext(
        run_id=new_run_id(),
        max_depth=depth,
        max_fanout=fanout,
        max_agents=200,
        recursion_limit=6,
        per_child_timeout_s=20.0,
        runner_plan=runner_plan,
        http_base_url=http_base_url,
    )
    run_ctx.runners = {
        semconv.RUNNER_INPROC: InprocRunner(model),
        semconv.RUNNER_SUBPROCESS: SubprocessRunner(),
        semconv.RUNNER_HTTP: HttpRunner(base_url=http_base_url),
    }
    objective = make_objective(fanout, depth, "coverage property test")
    await run_workflow(objective=objective, run_ctx=run_ctx, model=model)
    return run_ctx


@pytest.mark.asyncio
@settings(max_examples=6, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    fanout=st.integers(min_value=1, max_value=2),
    depth=st.integers(min_value=0, max_value=2),
    runner_plan=st.lists(st.sampled_from(RUNNER_NAMES), min_size=1, max_size=3),
)
async def test_propagation_coverage_across_runner_mixes(
    fanout: int, depth: int, runner_plan: list[str], span_file: Path, http_server: str
) -> None:
    if span_file.exists():
        span_file.unlink()
    os.environ["AGENTTRACE_TEST_SPAN_FILE"] = str(span_file)
    provider, processor = setup_tracing(service_name="agenttrace-property-test")
    # Bypass the process-global tracer registry: OTel only allows
    # set_tracer_provider() to take effect once per process, but this test
    # (unlike a real process) constructs a fresh provider per hypothesis
    # example within the same interpreter. Getting the tracer directly from
    # the local `provider` object sidesteps that entirely.
    local_tracer = provider.get_tracer("agenttrace-property-test", schema_url=semconv.SCHEMA_URL)
    set_hooks(OtelHooks(tracer=local_tracer, content_store=get_default_store()))
    try:
        run_ctx = await _run_one(fanout=fanout, depth=depth, runner_plan=runner_plan, http_base_url=http_server)
    finally:
        provider.force_flush()
        provider.shutdown()
        reset_hooks()

    spans = read_spans(str(span_file))
    assert spans, "expected at least one exported span"
    _verify_coverage(spans)
    assert processor.queue_drops == 0

    del run_ctx  # budgets aren't the coverage source of truth; spans are
