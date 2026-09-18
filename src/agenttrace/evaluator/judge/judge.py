"""E6: LLM-as-judge, called only on deterministic candidates (E1) — never on
raw spans or a whole trace. Backend is pluggable exactly like the harness's
FakeChatModel (D-rules ground rule: $0, deterministic CI; real-model runs
are opt-in with hard token caps).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

from agenttrace.evaluator.findings import Candidate
from agenttrace.evaluator.graph import CausalTrace
from agenttrace.evaluator.judge.rubrics import RUBRICS, Rubric
from agenttrace.evaluator.loader import TraceLoader
from agenttrace.telemetry import semconv

VERDICT_YES = "yes"
VERDICT_NO = "no"
VERDICT_UNCERTAIN = "uncertain"
VALID_VERDICTS = {VERDICT_YES, VERDICT_NO, VERDICT_UNCERTAIN}

MAX_EXCERPT_SPANS = 12
MAX_CONTENT_CHARS = 800


@dataclass
class JudgeVerdict:
    candidate: Candidate
    verdict: str  # yes | no | uncertain
    confidence: float
    evidence_span_ids: list[str]
    explanation: str
    input_tokens: int = 0
    output_tokens: int = 0
    rejected_reason: str | None = None

    @property
    def is_abstention(self) -> bool:
        return self.verdict == VERDICT_UNCERTAIN or self.rejected_reason is not None


class JudgeBackend(Protocol):
    model_name: str

    async def complete(self, *, system: str, user: str) -> tuple[str, int, int]:
        """Returns (raw_text_response, input_tokens, output_tokens)."""
        ...


def _truncate(text: str) -> str:
    return text if len(text) <= MAX_CONTENT_CHARS else text[:MAX_CONTENT_CHARS] + "...[truncated]"


def build_excerpt(candidate: Candidate, trace: CausalTrace, loader: TraceLoader) -> str:
    """Build the excerpt from exactly the spans connected to the candidate
    via the causal graph (E6) — never the whole trace."""
    span_ids = list(dict.fromkeys(candidate.evidence_span_ids))[:MAX_EXCERPT_SPANS]
    lines = [f"Candidate failure class: {candidate.failure_class}", f"Rationale: {candidate.rationale}", ""]

    for span_id in span_ids:
        span = trace.by_id.get(span_id)
        if span is None:
            continue
        lines.append(f"--- span {span_id} ({span.name}) ---")
        tool_name = span.attributes.get(semconv.GEN_AI_TOOL_NAME)
        if tool_name:
            lines.append(f"tool: {tool_name}")
        for ref_key, label in (
            (semconv.AT_TOOL_ARGS_REF, "args"),
            (semconv.AT_TOOL_RESULT_REF, "result"),
            (semconv.AT_AGENT_RESULT_REF, "agent_result"),
            (semconv.AT_SPAWN_OBJECTIVE_REF, "spawn_objective"),
        ):
            ref = span.attributes.get(ref_key)
            if isinstance(ref, str):
                content = loader.resolve_ref(ref)
                if content:
                    lines.append(f"{label}: {_truncate(content)}")
        taint = span.attributes.get(semconv.AT_TAINT)
        if taint:
            lines.append(f"taint: {taint}")

    return "\n".join(lines)


def build_prompt(rubric: Rubric, excerpt: str) -> tuple[str, str]:
    system = (
        "You are a strict, evidence-bound evaluator of AI agent traces. "
        "You are given a candidate failure and the exact spans connected to it. "
        "Respond with ONLY a JSON object, no other text, matching this schema:\n"
        '{"verdict": "yes"|"no"|"uncertain", "confidence": <0.0-1.0>, '
        '"evidence_span_ids": [<span ids you relied on>], "explanation": "<one or two sentences>"}\n'
        "Use \"uncertain\" when the evidence genuinely does not support a confident yes or no — "
        "do not guess. Every span ID you cite in evidence_span_ids MUST be one of the span IDs "
        "shown to you below; never invent one."
    )
    user = (
        f"Failure class definition:\n{rubric.definition}\n\n"
        f"Positive example:\n{rubric.positive_example}\n\n"
        f"Negative example:\n{rubric.negative_example}\n\n"
        f"Evidence excerpt:\n{excerpt}\n\n"
        "Did this failure actually occur? Respond with only the JSON object."
    )
    return system, user


def _extract_json(text: str) -> dict[str, object]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON object found in judge response: {text!r}")
    return dict(json.loads(match.group(0)))


async def judge_candidate(
    candidate: Candidate, trace: CausalTrace, loader: TraceLoader, backend: JudgeBackend
) -> JudgeVerdict:
    rubric = RUBRICS.get(candidate.failure_class)
    if rubric is None:
        return JudgeVerdict(
            candidate=candidate,
            verdict=VERDICT_UNCERTAIN,
            confidence=0.0,
            evidence_span_ids=[],
            explanation=f"no rubric registered for {candidate.failure_class}",
            rejected_reason="no_rubric",
        )

    excerpt = build_excerpt(candidate, trace, loader)
    system, user = build_prompt(rubric, excerpt)
    valid_span_ids = set(candidate.evidence_span_ids)

    try:
        raw, input_tokens, output_tokens = await backend.complete(system=system, user=user)
        parsed = _extract_json(raw)
        verdict = str(parsed.get("verdict", "")).lower()
        if verdict not in VALID_VERDICTS:
            raise ValueError(f"invalid verdict {verdict!r}")
        raw_evidence_ids = parsed.get("evidence_span_ids", [])
        if not isinstance(raw_evidence_ids, list):
            raise TypeError(f"evidence_span_ids must be a list, got {raw_evidence_ids!r}")
        evidence_ids = [str(x) for x in raw_evidence_ids]
        cited_unknown = [e for e in evidence_ids if e not in valid_span_ids]
        if cited_unknown:
            return JudgeVerdict(
                candidate=candidate,
                verdict=VERDICT_UNCERTAIN,
                confidence=0.0,
                evidence_span_ids=evidence_ids,
                explanation=str(parsed.get("explanation", "")),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                rejected_reason=f"cited span IDs not in trace: {cited_unknown}",
            )
        return JudgeVerdict(
            candidate=candidate,
            verdict=verdict,
            confidence=float(parsed.get("confidence", 0.0) or 0.0),  # type: ignore[arg-type]
            evidence_span_ids=evidence_ids,
            explanation=str(parsed.get("explanation", "")),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    except (ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return JudgeVerdict(
            candidate=candidate,
            verdict=VERDICT_UNCERTAIN,
            confidence=0.0,
            evidence_span_ids=[],
            explanation=f"judge response could not be parsed/validated: {exc}",
            rejected_reason="parse_error",
        )
