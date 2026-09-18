"""Retry storms (E5): repeated identical tool calls, repeated spawn
objectives, or LangGraph's own recursion-limit error.

False-positive trap: pagination/polling. Keying on args_sha256 (not tool
name alone) already excludes pagination, whose args differ page to page;
POLL_TOOL_ALLOWLIST additionally exempts tools expected to be called
repeatedly with identical args by design.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from agenttrace.evaluator.findings import Finding
from agenttrace.evaluator.graph import CausalTrace
from agenttrace.telemetry import semconv

REPEAT_THRESHOLD = 3
POLL_TOOL_ALLOWLIST: set[str] = set()


def detect_retry_storms(trace: CausalTrace) -> list[Finding]:
    findings: list[Finding] = []
    trace_id = trace.spans[0].trace_id if trace.spans else ""

    # 1. Repeated (enclosing agent, tool name, args hash) within an agent.
    groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for s in trace.tool_spans():
        tool_name = s.attributes.get(semconv.GEN_AI_TOOL_NAME)
        if tool_name in POLL_TOOL_ALLOWLIST or tool_name in ("spawn_subagent", "submit_result"):
            continue
        agent = trace.enclosing_agent(s.span_id)
        args_hash = s.attributes.get(semconv.AT_TOOL_ARGS_SHA256)
        if agent is None or tool_name is None or args_hash is None:
            continue
        groups[(agent.span_id, str(tool_name), str(args_hash))].append(s.span_id)

    for (agent_id, tool_name, _args_hash), span_ids in groups.items():
        if len(span_ids) >= REPEAT_THRESHOLD:
            findings.append(
                Finding(
                    failure_class="retry_storm",
                    trace_id=trace_id,
                    evidence_span_ids=[agent_id, *span_ids],
                    explanation=f"tool {tool_name!r} called {len(span_ids)} times with identical args in one agent",
                    extra={"repeat_count": len(span_ids), "tool_name": tool_name},
                )
            )

    # 2. Repeated spawn objectives from the same parent tool span context.
    objective_groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for s in trace.tool_spans():
        if s.name != semconv.tool_span_name("spawn_subagent"):
            continue
        agent = trace.enclosing_agent(s.span_id)
        for event in s.events:
            if event["name"] != semconv.EVENT_SPAWN_INTENT:
                continue
            obj_hash = event["attributes"].get(semconv.AT_SPAWN_OBJECTIVE_SHA256)
            if agent is not None and obj_hash is not None:
                objective_groups[(agent.span_id, str(obj_hash))].append(s.span_id)

    for (agent_id, _obj_hash), span_ids in objective_groups.items():
        if len(span_ids) >= REPEAT_THRESHOLD:
            findings.append(
                Finding(
                    failure_class="retry_storm",
                    trace_id=trace_id,
                    evidence_span_ids=[agent_id, *span_ids],
                    explanation=f"same spawn objective re-spawned {len(span_ids)} times",
                    extra={"repeat_count": len(span_ids)},
                )
            )

    # 3. GraphRecursionError surfaced as error.type anywhere in the trace.
    error_counts = Counter(
        s.attributes.get("error.type") for s in trace.spans if s.attributes.get("error.type")
    )
    for error_type, count in error_counts.items():
        if error_type and "GraphRecursionError" in str(error_type):
            offending = [s.span_id for s in trace.spans if s.attributes.get("error.type") == error_type]
            findings.append(
                Finding(
                    failure_class="retry_storm",
                    trace_id=trace_id,
                    evidence_span_ids=offending,
                    explanation=f"{error_type} raised ({count}x) — agent exceeded its recursion budget",
                    extra={"error_type": error_type, "count": count},
                )
            )

    return findings
