"""Orphaned subagents (E5).

Distinguishes a runtime orphan (spawn.outcome is timeout/killed/error — a
real agent failure) from an instrumentation miss (outcome=returned but no
child span ever arrived — a coverage bug, not a run failure). Keeping these
separate is what keeps the coverage metric (D6/verify.py) and the failure
metric from contaminating each other.
"""

from __future__ import annotations

from agenttrace.evaluator.findings import Finding
from agenttrace.evaluator.graph import CausalTrace
from agenttrace.telemetry import semconv

RUNTIME_ORPHAN_OUTCOMES = {"timeout", "killed", "error"}


def detect_orphans(trace: CausalTrace) -> list[Finding]:
    findings: list[Finding] = []
    trace_id = trace.spans[0].trace_id if trace.spans else ""
    tool_spans = [s for s in trace.spans if s.name == semconv.tool_span_name("spawn_subagent")]
    agent_spans = trace.agent_spans()

    for tool_span in tool_spans:
        for event in tool_span.events:
            if event["name"] != semconv.EVENT_SPAWN_INTENT:
                continue
            spawn_id = event["attributes"].get(semconv.AT_SPAWN_ID)
            outcome = tool_span.attributes.get(semconv.AT_SPAWN_OUTCOME)
            matched = [a for a in agent_spans if a.attributes.get(semconv.AT_SPAWN_ID) == spawn_id]

            if outcome == "returned" and not matched:
                findings.append(
                    Finding(
                        failure_class="instrumentation_miss",
                        trace_id=trace_id,
                        evidence_span_ids=[tool_span.span_id],
                        explanation=(
                            f"spawn {spawn_id} outcome=returned but no matching child span "
                            "arrived — coverage bug, not a runtime failure"
                        ),
                    )
                )
            elif outcome in RUNTIME_ORPHAN_OUTCOMES:
                findings.append(
                    Finding(
                        failure_class="orphaned_subagent",
                        trace_id=trace_id,
                        evidence_span_ids=[tool_span.span_id, *[m.span_id for m in matched]],
                        explanation=f"spawn {spawn_id} outcome={outcome}",
                        extra={"spawn_outcome": outcome, "matched_children": len(matched)},
                    )
                )
    return findings
