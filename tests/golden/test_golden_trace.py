"""Phase 2 golden tests: exact span names/kinds/parentage/attrs (D9), and the
D3 guard that a manually opened span nests under the right agent span."""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from agenttrace.harness.fake_model import FakeChatModel
from agenttrace.harness.graph import new_run_id, run_workflow
from agenttrace.harness.run_context import RunContext
from agenttrace.harness.runners.inproc import InprocRunner
from agenttrace.harness.scripts import default_script, make_objective
from agenttrace.telemetry import semconv


def _by_id(spans: list[ReadableSpan]) -> dict[int, ReadableSpan]:
    return {s.context.span_id: s for s in spans}  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_single_agent_no_spawn_has_correct_topology(memory_exporter: InMemorySpanExporter) -> None:
    model = FakeChatModel(default_script)
    run_ctx = RunContext(run_id=new_run_id(), max_depth=0, runner_plan=["inproc"])
    run_ctx.runners = {"inproc": InprocRunner(model)}

    objective = "leaf objective, no spawning"
    await run_workflow(objective=objective, run_ctx=run_ctx, model=model)

    spans = memory_exporter.get_finished_spans()
    names = [s.name for s in spans]
    assert "invoke_workflow agenttrace-demo" in names
    assert "invoke_agent orchestrator" in names
    assert any(n.startswith("chat ") for n in names)
    assert any(n.startswith("execute_tool ") for n in names)

    workflow_span = next(s for s in spans if s.name == "invoke_workflow agenttrace-demo")
    agent_span = next(s for s in spans if s.name == "invoke_agent orchestrator")
    assert agent_span.parent is not None
    assert agent_span.parent.span_id == workflow_span.context.span_id  # type: ignore[union-attr]
    assert agent_span.kind == SpanKind.INTERNAL

    trace_ids = {s.context.trace_id for s in spans}  # type: ignore[union-attr]
    assert len(trace_ids) == 1, "every span in one run must share one trace ID"

    roots = [s for s in spans if s.parent is None]
    assert len(roots) == 1
    assert roots[0].name == "invoke_workflow agenttrace-demo"


@pytest.mark.asyncio
async def test_recursive_spawn_parentage_is_exact(memory_exporter: InMemorySpanExporter) -> None:
    model = FakeChatModel(default_script)
    run_ctx = RunContext(run_id=new_run_id(), max_depth=1, max_fanout=2, runner_plan=["inproc"])
    run_ctx.runners = {"inproc": InprocRunner(model)}

    objective = make_objective(2, 1, "spawn two children")
    await run_workflow(objective=objective, run_ctx=run_ctx, model=model)

    spans = memory_exporter.get_finished_spans()

    agent_spans = [s for s in spans if s.name.startswith("invoke_agent")]
    assert len(agent_spans) == 3  # orchestrator + 2 children

    orchestrator = next(s for s in agent_spans if semconv.AT_AGENT_DEPTH in s.attributes and s.attributes[semconv.AT_AGENT_DEPTH] == 0)  # type: ignore[operator]
    children = [s for s in agent_spans if s is not orchestrator]
    assert len(children) == 2

    tool_spans = [
        s
        for s in spans
        if s.name == semconv.tool_span_name("spawn_subagent")
    ]
    assert len(tool_spans) == 2

    # Each child's root agent span must be parented to the SPECIFIC
    # execute_tool spawn_subagent span that spawned it, not to some ancestor.
    tool_span_ids = {s.context.span_id for s in tool_spans}  # type: ignore[union-attr]
    for child in children:
        assert child.parent is not None
        assert child.parent.span_id in tool_span_ids
        assert semconv.AT_SPAWN_ID in child.attributes

    # Every spawn_id on a child's agent span must match a spawn_id recorded
    # by a spawn_intent event AND a spawn_outcome attribute on its tool span.
    for tool_span in tool_spans:
        assert tool_span.attributes.get(semconv.AT_SPAWN_OUTCOME) == "returned"
        intent_events = [e for e in tool_span.events if e.name == semconv.EVENT_SPAWN_INTENT]
        assert len(intent_events) == 1
        intent_spawn_id = intent_events[0].attributes[semconv.AT_SPAWN_ID]  # type: ignore[index]
        assert tool_span.attributes[semconv.AT_SPAWN_ID] == intent_spawn_id
        matching_child = next(c for c in children if c.attributes[semconv.AT_SPAWN_ID] == intent_spawn_id)
        assert matching_child.parent.span_id == tool_span.context.span_id  # type: ignore[union-attr,union-attr]


@pytest.mark.asyncio
async def test_manual_span_nests_under_its_agent_span(memory_exporter: InMemorySpanExporter) -> None:
    """D3 guard: a hand-opened span inside a node must nest under that
    node's agent span, proving OTel's current-context is exactly the agent
    span while the node runs (no parallel/inconsistent parent chain)."""
    test_tracer = memory_exporter.tracer  # type: ignore[attr-defined]

    model = FakeChatModel(default_script)
    run_ctx = RunContext(run_id=new_run_id(), max_depth=0, runner_plan=["inproc"])
    run_ctx.runners = {"inproc": InprocRunner(model)}

    manual_span_id_holder: dict[str, int] = {}

    original_invoke = model.invoke

    def spying_invoke(role: str, messages: list, step: int):  # type: ignore[no-untyped-def]
        with test_tracer.start_as_current_span("manual-probe-span") as span:
            manual_span_id_holder["id"] = span.get_span_context().span_id
        return original_invoke(role, messages, step)

    model.invoke = spying_invoke  # type: ignore[method-assign]

    await run_workflow(objective="probe", run_ctx=run_ctx, model=model)

    spans = memory_exporter.get_finished_spans()
    manual_span = next(s for s in spans if s.name == "manual-probe-span")
    agent_span = next(s for s in spans if s.name == "invoke_agent orchestrator")
    by_span_id = _by_id(spans)

    # "nests under" means somewhere in the ancestor chain, not necessarily a
    # direct child — model.invoke() runs inside the chat span, which itself
    # is a direct child of the agent span.
    node = manual_span
    seen_agent = False
    for _ in range(10):
        if node.parent is None:
            break
        parent = by_span_id.get(node.parent.span_id)
        if parent is None:
            break
        if parent.context.span_id == agent_span.context.span_id:  # type: ignore[union-attr]
            seen_agent = True
            break
        node = parent
    assert seen_agent, "manual span must nest under its node's agent span, not attach elsewhere"
