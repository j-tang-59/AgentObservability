"""Unit tests per detector on minimal hand-built traces (Phase 5 gate)."""

from __future__ import annotations

from agenttrace.evaluator.detectors.orphans import detect_orphans
from agenttrace.evaluator.detectors.retry_storm import detect_retry_storms
from agenttrace.evaluator.detectors.tool_hallucination import detect_structural_hallucinations
from agenttrace.evaluator.graph import CausalTrace, build_causal_trace
from agenttrace.evaluator.loader import TraceLoader
from agenttrace.telemetry import semconv
from agenttrace.telemetry.content_store import ContentStore
from agenttrace.trace_model import SpanRecord


def _span(
    span_id: str,
    parent: str | None,
    name: str,
    attrs: dict | None = None,
    events: list | None = None,
    start: int = 0,
    end: int = 1,
) -> SpanRecord:
    return SpanRecord(
        trace_id="t1",
        span_id=span_id,
        parent_span_id=parent,
        name=name,
        kind="INTERNAL",
        attributes=attrs or {},
        events=events or [],
        start_time_unix_nano=start,
        end_time_unix_nano=end,
    )


def _trace(spans: list[SpanRecord], tmp_path) -> CausalTrace:
    return build_causal_trace(spans, TraceLoader(ContentStore(root=str(tmp_path))))


def test_orphans_flags_killed_not_returned(tmp_path) -> None:
    spans = [
        _span("root", None, "invoke_workflow demo"),
        _span("agent", "root", "invoke_agent orchestrator"),
        _span(
            "tool",
            "agent",
            semconv.tool_span_name("spawn_subagent"),
            attrs={semconv.AT_SPAWN_ID: "s1", semconv.AT_SPAWN_OUTCOME: "killed"},
            events=[{"name": semconv.EVENT_SPAWN_INTENT, "attributes": {semconv.AT_SPAWN_ID: "s1"}}],
        ),
    ]
    findings = detect_orphans(_trace(spans, tmp_path))
    assert len(findings) == 1
    assert findings[0].failure_class == "orphaned_subagent"


def test_orphans_ignores_returned_with_matching_child(tmp_path) -> None:
    spans = [
        _span("root", None, "invoke_workflow demo"),
        _span("agent", "root", "invoke_agent orchestrator"),
        _span(
            "tool",
            "agent",
            semconv.tool_span_name("spawn_subagent"),
            attrs={semconv.AT_SPAWN_ID: "s1", semconv.AT_SPAWN_OUTCOME: "returned"},
            events=[{"name": semconv.EVENT_SPAWN_INTENT, "attributes": {semconv.AT_SPAWN_ID: "s1"}}],
        ),
        _span("child", "tool", "invoke_agent researcher", attrs={semconv.AT_SPAWN_ID: "s1"}),
    ]
    findings = detect_orphans(_trace(spans, tmp_path))
    assert findings == []


def test_retry_storm_flags_repeated_identical_calls(tmp_path) -> None:
    spans = [
        _span("root", None, "invoke_workflow demo"),
        _span("agent", "root", "invoke_agent researcher"),
    ]
    for i in range(4):
        spans.append(
            _span(
                f"tool{i}",
                "agent",
                semconv.tool_span_name("search_corpus"),
                attrs={
                    semconv.GEN_AI_TOOL_NAME: "search_corpus",
                    semconv.AT_TOOL_ARGS_SHA256: "sameHash",
                },
            )
        )
    findings = detect_retry_storms(_trace(spans, tmp_path))
    assert any(f.failure_class == "retry_storm" for f in findings)


def test_retry_storm_ignores_different_args(tmp_path) -> None:
    spans = [
        _span("root", None, "invoke_workflow demo"),
        _span("agent", "root", "invoke_agent researcher"),
    ]
    for i in range(4):
        spans.append(
            _span(
                f"tool{i}",
                "agent",
                semconv.tool_span_name("search_corpus"),
                attrs={
                    semconv.GEN_AI_TOOL_NAME: "search_corpus",
                    semconv.AT_TOOL_ARGS_SHA256: f"hash{i}",
                },
            )
        )
    findings = detect_retry_storms(_trace(spans, tmp_path))
    assert findings == []


def test_structural_hallucination_flags_undeclared_tool(tmp_path) -> None:
    import json

    tool_defs = json.dumps([{"name": "search_corpus", "description": ""}])
    spans = [
        _span("root", None, "invoke_workflow demo"),
        _span("agent", "root", "invoke_agent orchestrator"),
        _span(
            "chat",
            "agent",
            "chat fake",
            attrs={semconv.GEN_AI_TOOL_DEFINITIONS: tool_defs},
            start=0,
            end=1,
        ),
        _span(
            "tool",
            "agent",
            semconv.tool_span_name("delete_database"),
            attrs={semconv.GEN_AI_TOOL_NAME: "delete_database"},
            start=2,
            end=3,
        ),
    ]
    findings = detect_structural_hallucinations(_trace(spans, tmp_path))
    assert len(findings) == 1
    assert findings[0].failure_class == "tool_hallucination_structural"


def test_structural_hallucination_ignores_declared_tool(tmp_path) -> None:
    spans = [
        _span("root", None, "invoke_workflow demo"),
        _span("agent", "root", "invoke_agent orchestrator"),
        _span(
            "tool",
            "agent",
            semconv.tool_span_name("search_corpus"),
            attrs={semconv.GEN_AI_TOOL_NAME: "search_corpus"},
        ),
    ]
    findings = detect_structural_hallucinations(_trace(spans, tmp_path))
    assert findings == []
