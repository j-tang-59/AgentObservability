"""E6 calibration: precision/recall/F1 against ground truth, abstention
rate, judge cost, and position-sensitivity (does shuffling evidence order
flip the verdict?).

Two things this module deliberately does NOT implement against a live
service in this build: second-judge-model Cohen's kappa and human-label
agreement on a real-model tier. Both require either a second paid model or
manually labeled real-model runs, neither of which this offline build
exercises (see docs/RESULTS.md) — the functions are written to accept
whatever labels/verdicts are supplied, so wiring in real data later is a
call-site change, not a rewrite.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from agenttrace.evaluator.findings import Candidate
from agenttrace.evaluator.graph import CausalTrace
from agenttrace.evaluator.judge.judge import JudgeBackend, JudgeVerdict, judge_candidate
from agenttrace.evaluator.loader import TraceLoader


@dataclass
class ClassMetrics:
    failure_class: str
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    abstentions: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


@dataclass
class CalibrationReport:
    per_class: dict[str, ClassMetrics] = field(default_factory=dict)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_verdicts: int = 0
    total_abstentions: int = 0

    @property
    def abstention_rate(self) -> float:
        return self.total_abstentions / self.total_verdicts if self.total_verdicts else 0.0

    @property
    def avg_tokens_per_verdict(self) -> float:
        if not self.total_verdicts:
            return 0.0
        return (self.total_input_tokens + self.total_output_tokens) / self.total_verdicts


async def judge_candidates(
    candidates: list[Candidate],
    traces_by_trace_id: dict[str, CausalTrace],
    loader: TraceLoader,
    backend: JudgeBackend,
) -> list[JudgeVerdict]:
    verdicts: list[JudgeVerdict] = []
    for c in candidates:
        trace = traces_by_trace_id.get(c.trace_id)
        if trace is None:
            continue
        verdicts.append(await judge_candidate(c, trace, loader, backend))
    return verdicts


def score_against_ground_truth(
    verdicts: list[JudgeVerdict], ground_truth: dict[str, bool]
) -> CalibrationReport:
    """ground_truth maps trace_id -> "was this candidate's failure class a
    real positive in this trace" (from the fault suite's known labels)."""
    report = CalibrationReport()
    for v in verdicts:
        cls = v.candidate.failure_class
        metrics = report.per_class.setdefault(cls, ClassMetrics(failure_class=cls))
        report.total_verdicts += 1
        report.total_input_tokens += v.input_tokens
        report.total_output_tokens += v.output_tokens

        if v.is_abstention:
            metrics.abstentions += 1
            report.total_abstentions += 1
            continue

        truth = ground_truth.get(v.candidate.trace_id)
        if truth is None:
            continue
        predicted_positive = v.verdict == "yes"
        if predicted_positive and truth:
            metrics.tp += 1
        elif predicted_positive and not truth:
            metrics.fp += 1
        elif not predicted_positive and truth:
            metrics.fn += 1
        else:
            metrics.tn += 1

    return report


async def position_sensitivity(
    candidate: Candidate,
    trace: CausalTrace,
    loader: TraceLoader,
    backend: JudgeBackend,
    *,
    n_shuffles: int = 3,
    seed: int = 0,
) -> float:
    """Shuffles candidate.evidence_span_ids order n_shuffles times, re-runs
    the judge each time, and returns the fraction of shuffles whose verdict
    differs from the first (unshuffled) run — the position-sensitivity
    check E6 calls for."""
    rng = random.Random(seed)
    baseline = await judge_candidate(candidate, trace, loader, backend)
    flips = 0
    for _ in range(n_shuffles):
        shuffled_ids = list(candidate.evidence_span_ids)
        rng.shuffle(shuffled_ids)
        shuffled_candidate = Candidate(
            failure_class=candidate.failure_class,
            trace_id=candidate.trace_id,
            evidence_span_ids=shuffled_ids,
            rationale=candidate.rationale,
        )
        result = await judge_candidate(shuffled_candidate, trace, loader, backend)
        if result.verdict != baseline.verdict:
            flips += 1
    return flips / n_shuffles if n_shuffles else 0.0
