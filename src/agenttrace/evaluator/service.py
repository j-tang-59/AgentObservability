"""Runs every deterministic detector over one trace and returns findings +
judge candidates (E1's principle: deterministic first, semantic questions
become candidates for the judge, never resolved by guessing)."""

from __future__ import annotations

from dataclasses import dataclass, field

from agenttrace.evaluator.detectors.orphans import detect_orphans
from agenttrace.evaluator.detectors.prompt_injection import detect_injection_candidates
from agenttrace.evaluator.detectors.retry_storm import detect_retry_storms
from agenttrace.evaluator.detectors.silent_failure import detect_silent_failures
from agenttrace.evaluator.detectors.tool_hallucination import (
    detect_fabricated_result_candidates,
    detect_structural_hallucinations,
)
from agenttrace.evaluator.findings import Candidate, Finding
from agenttrace.evaluator.graph import CausalTrace, build_causal_trace
from agenttrace.evaluator.loader import TraceLoader
from agenttrace.trace_model import SpanRecord


@dataclass
class EvaluationResult:
    trace_id: str
    findings: list[Finding] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)


def evaluate_spans(spans: list[SpanRecord], loader: TraceLoader | None = None) -> EvaluationResult:
    loader = loader or TraceLoader()
    trace: CausalTrace = build_causal_trace(spans, loader)
    trace_id = spans[0].trace_id if spans else ""

    findings: list[Finding] = []
    candidates: list[Candidate] = []

    findings += detect_orphans(trace)
    findings += detect_retry_storms(trace)
    findings += detect_structural_hallucinations(trace)

    silent_findings, silent_candidates = detect_silent_failures(trace, loader.resolve_ref)
    findings += silent_findings
    candidates += silent_candidates

    candidates += detect_fabricated_result_candidates(trace, loader.resolve_ref)
    candidates += detect_injection_candidates(trace)

    return EvaluationResult(trace_id=trace_id, findings=findings, candidates=candidates)
