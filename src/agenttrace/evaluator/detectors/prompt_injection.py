"""Prompt injection candidates (E5): an agent consumes tainted content
(taint.py) and then does something out of scope — calls a tool outside its
role's declared set, spawns a subagent with a diverging objective, or
passes tainted chunks into a later tool call's args. All three are
mechanically detectable; whether the agent was *obeying* the injection or
legitimately *using* retrieved information is a judgment call left to the
LLM judge (E6) — this detector only ever produces Candidates.
"""

from __future__ import annotations

from agenttrace.evaluator.findings import Candidate
from agenttrace.evaluator.graph import CausalTrace
from agenttrace.evaluator.taint import compute_tainted_span_ids
from agenttrace.telemetry import semconv


def detect_injection_candidates(trace: CausalTrace) -> list[Candidate]:
    tainted_ids = compute_tainted_span_ids(trace)
    trace_id = trace.spans[0].trace_id if trace.spans else ""
    candidates: list[Candidate] = []

    for span in trace.tool_spans():
        # Note: this span may ALSO be independently tainted at its own
        # source (e.g. web_fetch always taints its own result) — that does
        # NOT disqualify it. The signal here is specifically "did tainted
        # content flow INTO this span's args from an earlier span",
        # regardless of what this span's own output taint is.
        producers = [
            p for p in trace.data_producers(span.span_id) if p.span_id in tainted_ids and p.span_id != span.span_id
        ]
        if not producers:
            continue
        candidates.append(
            Candidate(
                failure_class="prompt_injection",
                trace_id=trace_id,
                evidence_span_ids=[p.span_id for p in producers] + [span.span_id],
                rationale=(
                    f"{span.name} (tool={span.attributes.get(semconv.GEN_AI_TOOL_NAME)}) "
                    f"consumed content originating from tainted span(s) "
                    f"{[p.span_id for p in producers]}"
                ),
            )
        )

    return candidates
