"""Telemetry and evaluation must never fail or change a run: identical
outputs with OTEL_SDK_DISABLED=true vs false (ground rule)."""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased

from agenttrace.harness.fake_model import FakeChatModel
from agenttrace.harness.graph import new_run_id, run_workflow
from agenttrace.harness.run_context import RunContext
from agenttrace.harness.runners.inproc import InprocRunner
from agenttrace.harness.scripts import default_script, make_objective
from agenttrace.telemetry.content_store import ContentStore
from agenttrace.telemetry.hooks import reset_hooks, set_hooks
from agenttrace.telemetry.otel_hooks import OtelHooks


async def _run_once(objective: str) -> tuple[str, int]:
    model = FakeChatModel(default_script)
    run_ctx = RunContext(run_id=new_run_id(), max_depth=2, max_fanout=2, runner_plan=["inproc"])
    run_ctx.runners = {"inproc": InprocRunner(model)}
    result = await run_workflow(objective=objective, run_ctx=run_ctx, model=model)
    return result, run_ctx.agents_spawned


@pytest.mark.asyncio
async def test_identical_output_with_telemetry_on_or_off(tmp_path: object) -> None:
    objective = make_objective(2, 2, "parity check")

    reset_hooks()
    result_off, agents_off = await _run_once(objective)

    provider = TracerProvider(sampler=ParentBased(ALWAYS_ON))
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    store = ContentStore(root=tmp_path)  # type: ignore[arg-type]
    set_hooks(OtelHooks(tracer=provider.get_tracer("t"), content_store=store, capture_content=True))
    try:
        result_on, agents_on = await _run_once(objective)
    finally:
        reset_hooks()
        provider.shutdown()

    assert result_off == result_on
    assert agents_off == agents_on
    assert len(exporter.get_finished_spans()) > 0
