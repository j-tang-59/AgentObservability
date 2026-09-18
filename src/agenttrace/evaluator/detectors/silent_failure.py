"""Silent tool failures (E5): OK-status span whose result is failure-shaped,
consumed downstream as if it were valid, with no retry and no
acknowledgment. Both conditions are required — a failure the agent noticed
and handled (a retry, or the result never propagating further) is not
silent, which is what keeps this a causal detector rather than a regex.

Ambiguous cases (e.g. a legitimately empty search result) are intentionally
under-flagged here and left as judge candidates (E6) rather than guessed at
deterministically.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from agenttrace.evaluator.findings import Candidate, Finding
from agenttrace.evaluator.graph import CausalTrace
from agenttrace.telemetry import semconv

FAILURE_PATTERNS = re.compile(
    r"\b(error|exception|traceback|failed|failure|404|500|502|503|timeout)\b", re.IGNORECASE
)
EMPTY_RESULT_MAX_BYTES = 20


def detect_silent_failures(
    trace: CausalTrace, resolve_ref: Callable[[str | None], str | None]
) -> tuple[list[Finding], list[Candidate]]:
    findings: list[Finding] = []
    candidates: list[Candidate] = []
    trace_id = trace.spans[0].trace_id if trace.spans else ""

    for s in trace.tool_spans():
        if s.status == "STATUS_CODE_ERROR" or s.attributes.get("error.type"):
            continue  # an acknowledged failure, not a silent one
        if s.name in (semconv.tool_span_name("spawn_subagent"), semconv.tool_span_name("submit_result")):
            continue

        result_bytes = int(s.attributes.get(semconv.AT_TOOL_RESULT_BYTES, 999))
        result_ref = s.attributes.get(semconv.AT_TOOL_RESULT_REF)
        resolved = resolve_ref(result_ref) if isinstance(result_ref, str) else None

        explicit_failure = bool(resolved and FAILURE_PATTERNS.search(resolved))
        # An empty/near-empty OK result (e.g. a legitimately-empty search)
        # is only ever ambiguous, never a confident Finding on its own —
        # confidence comes from an explicit failure pattern in the content.
        empty_shaped = result_bytes <= EMPTY_RESULT_MAX_BYTES and not explicit_failure
        if not explicit_failure and not empty_shaped:
            continue

        consumers = trace.data_consumers(s.span_id)
        if not consumers:
            continue  # never flowed anywhere — not "consumed as valid"

        agent = trace.enclosing_agent(s.span_id)
        tool_name = s.attributes.get(semconv.GEN_AI_TOOL_NAME)
        same_tool_calls = [
            t
            for t in trace.tool_spans()
            if t.attributes.get(semconv.GEN_AI_TOOL_NAME) == tool_name
            and trace.enclosing_agent(t.span_id) == agent
        ]
        was_retried = len(same_tool_calls) > 1
        if was_retried:
            continue  # the agent noticed and tried again — not silent

        evidence = [s.span_id, *[c.span_id for c in consumers]]
        if explicit_failure:
            findings.append(
                Finding(
                    failure_class="silent_tool_failure",
                    trace_id=trace_id,
                    evidence_span_ids=evidence,
                    explanation=f"tool {tool_name!r} returned a failure-shaped OK result, consumed downstream with no retry",
                    extra={"result_bytes": result_bytes},
                )
            )
        else:
            candidates.append(
                Candidate(
                    failure_class="silent_tool_failure",
                    trace_id=trace_id,
                    evidence_span_ids=evidence,
                    rationale="empty/near-empty OK result consumed downstream; ambiguous whether this was a real failure",
                )
            )

    return findings, candidates
