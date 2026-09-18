"""Runs one Scenario end-to-end with real telemetry and returns its trace.

Uses the same file-exporter test infrastructure as the Phase 2 property test
(telemetry/file_exporter.py) so the synthetic labeled suite doesn't require
a running Collector/Tempo — bench/suite.py can be run offline in CI.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any

from agenttrace.faults.scenarios import Scenario
from agenttrace.harness.fake_model import FakeChatModel
from agenttrace.harness.graph import run_workflow
from agenttrace.harness.run_context import RunContext
from agenttrace.harness.runners.http import HttpRunner
from agenttrace.harness.runners.inproc import InprocRunner
from agenttrace.harness.runners.subprocess import SubprocessRunner
from agenttrace.telemetry import semconv
from agenttrace.telemetry.content_store import ContentStore
from agenttrace.telemetry.file_exporter import read_spans
from agenttrace.telemetry.hooks import reset_hooks, set_hooks
from agenttrace.telemetry.otel_hooks import OtelHooks
from agenttrace.telemetry.setup import setup_tracing
from agenttrace.trace_model import SpanRecord, from_file_exporter_dicts


@dataclass
class ScenarioRun:
    scenario_name: str
    failure_class: str
    trace_id: str
    spans: list[SpanRecord]
    result: str


def _default_overrides() -> dict[str, Any]:
    return {
        "max_depth": 2,
        "max_fanout": 2,
        "max_agents": 50,
        "recursion_limit": 8,
        "per_child_timeout_s": 20.0,
        "runner_plan": [semconv.RUNNER_INPROC],
        "http_base_url": "http://127.0.0.1:8765",
    }


async def run_scenario(scenario: Scenario, *, span_file: str, content_store_root: str) -> ScenarioRun:
    if scenario.setup:
        scenario.setup()

    os.environ["AGENTTRACE_TEST_SPAN_FILE"] = span_file
    provider, _processor = setup_tracing(service_name="agenttrace-faults")
    local_tracer = provider.get_tracer("agenttrace-faults", schema_url=semconv.SCHEMA_URL)
    store = ContentStore(root=content_store_root)
    set_hooks(OtelHooks(tracer=local_tracer, content_store=store, capture_content=True))

    trace_id_holder: dict[str, str | None] = {"trace_id": None}
    try:
        model = FakeChatModel(scenario.script_factory())
        overrides = {**_default_overrides(), **scenario.run_ctx_overrides}
        run_ctx = RunContext(run_id=uuid.uuid4().hex, **overrides)
        run_ctx.runners = {
            semconv.RUNNER_INPROC: InprocRunner(model),
            semconv.RUNNER_SUBPROCESS: SubprocessRunner(),
            semconv.RUNNER_HTTP: HttpRunner(base_url=run_ctx.http_base_url),
        }
        result = await run_workflow(
            objective=scenario.objective,
            run_ctx=run_ctx,
            model=model,
            on_root_span_started=lambda tid: trace_id_holder.__setitem__("trace_id", tid),
            role=scenario.role,
        )
    finally:
        provider.force_flush()
        provider.shutdown()
        reset_hooks()

    all_spans = from_file_exporter_dicts(read_spans(span_file))
    trace_id = trace_id_holder["trace_id"]
    my_spans = [s for s in all_spans if s.trace_id == trace_id]
    return ScenarioRun(
        scenario_name=scenario.name,
        failure_class=scenario.failure_class,
        trace_id=str(trace_id),
        spans=my_spans,
        result=result,
    )
