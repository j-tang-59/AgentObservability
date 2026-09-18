"""Phase 6: LLM-as-judge tested against FakeJudgeBackend ($0, deterministic
— no ANTHROPIC_API_KEY in this environment). Runs the fault suite's
injection/fabrication/silent-failure candidates through the real
build_excerpt -> build_prompt -> parse/validate pipeline."""

from __future__ import annotations

from pathlib import Path

import pytest

from agenttrace.bench.suite import run_suite
from agenttrace.evaluator.graph import build_causal_trace
from agenttrace.evaluator.judge.backends import FakeJudgeBackend
from agenttrace.evaluator.judge.calibration import (
    position_sensitivity,
    score_against_ground_truth,
)
from agenttrace.evaluator.judge.judge import judge_candidate
from agenttrace.evaluator.loader import TraceLoader
from agenttrace.evaluator.service import evaluate_spans
from agenttrace.telemetry.content_store import ContentStore
from agenttrace.telemetry.file_exporter import read_spans
from agenttrace.trace_model import from_file_exporter_dicts

REPS = 4


def _always_yes_script(system: str, user: str):
    return "yes", "0.9", [], "matches the positive rubric example"


def _always_no_script(system: str, user: str):
    return "no", "0.9", [], "does not match the rubric"


def _cites_unknown_span_script(system: str, user: str):
    return "yes", "0.9", ["not-a-real-span-id"], "hallucinated evidence"


@pytest.mark.asyncio
async def test_judge_rejects_verdict_citing_unknown_span_id(tmp_path: Path) -> None:
    manifest = await run_suite(reps=1, out_dir=tmp_path)
    all_spans = from_file_exporter_dicts(read_spans(str(tmp_path / "spans.jsonl")))
    loader = TraceLoader(ContentStore(root=str(tmp_path / "content")))

    entry = next(e for e in manifest if e["failure_class"] == "prompt_injection")
    trace_id = str(entry["trace_id"])
    spans = [s for s in all_spans if s.trace_id == trace_id]
    result = evaluate_spans(spans, loader)
    assert result.candidates, "expected at least one candidate for the injection scenario"

    trace = build_causal_trace(spans, loader)
    backend = FakeJudgeBackend(_cites_unknown_span_script)
    verdict = await judge_candidate(result.candidates[0], trace, loader, backend)

    assert verdict.is_abstention
    assert verdict.rejected_reason is not None
    assert "not-a-real-span-id" in verdict.rejected_reason


@pytest.mark.asyncio
async def test_judge_uncertain_is_abstention_not_forced_yes_or_no(tmp_path: Path) -> None:
    manifest = await run_suite(reps=1, out_dir=tmp_path)
    all_spans = from_file_exporter_dicts(read_spans(str(tmp_path / "spans.jsonl")))
    loader = TraceLoader(ContentStore(root=str(tmp_path / "content")))

    entry = next(e for e in manifest if e["failure_class"] == "prompt_injection")
    spans = [s for s in all_spans if s.trace_id == str(entry["trace_id"])]
    result = evaluate_spans(spans, loader)
    trace = build_causal_trace(spans, loader)

    def uncertain_script(system: str, user: str):
        return "uncertain", "0.3", [], "evidence is ambiguous"

    backend = FakeJudgeBackend(uncertain_script)
    verdict = await judge_candidate(result.candidates[0], trace, loader, backend)
    assert verdict.verdict == "uncertain"
    assert verdict.is_abstention


@pytest.mark.asyncio
async def test_position_sensitivity_zero_flips_for_a_stable_backend(tmp_path: Path) -> None:
    manifest = await run_suite(reps=1, out_dir=tmp_path)
    all_spans = from_file_exporter_dicts(read_spans(str(tmp_path / "spans.jsonl")))
    loader = TraceLoader(ContentStore(root=str(tmp_path / "content")))

    entry = next(e for e in manifest if e["failure_class"] == "prompt_injection")
    spans = [s for s in all_spans if s.trace_id == str(entry["trace_id"])]
    result = evaluate_spans(spans, loader)
    trace = build_causal_trace(spans, loader)

    backend = FakeJudgeBackend(_always_yes_script)
    flip_rate = await position_sensitivity(result.candidates[0], trace, loader, backend, n_shuffles=3)
    assert flip_rate == 0.0


@pytest.mark.asyncio
async def test_calibration_precision_recall_against_known_labels(tmp_path: Path) -> None:
    manifest = await run_suite(reps=REPS, out_dir=tmp_path)
    all_spans = from_file_exporter_dicts(read_spans(str(tmp_path / "spans.jsonl")))
    loader = TraceLoader(ContentStore(root=str(tmp_path / "content")))

    by_trace: dict[str, list] = {}
    for s in all_spans:
        by_trace.setdefault(s.trace_id, []).append(s)

    ground_truth: dict[str, bool] = {}
    candidates = []
    traces_by_id = {}
    for entry in manifest:
        trace_id = str(entry["trace_id"])
        spans = by_trace.get(trace_id, [])
        result = evaluate_spans(spans, loader)
        traces_by_id[trace_id] = build_causal_trace(spans, loader)
        for c in result.candidates:
            if c.failure_class == "prompt_injection":
                candidates.append(c)
                ground_truth[trace_id] = entry["failure_class"] == "prompt_injection"

    assert candidates, "expected injection candidates from the fault suite"

    # A backend that says "yes" whenever the candidate's rationale mentions
    # taint propagation (true for every candidate our detector emits) — a
    # stand-in for a real judge that would resolve the injection/legitimate
    # ambiguity; here it's just exercising the scoring pipeline end-to-end.
    def script(system: str, user: str):
        verdict = "yes" if "injected" in user.lower() or "taint" in user.lower() else "no"
        return verdict, "0.8", [], "scripted verdict"

    backend = FakeJudgeBackend(script)
    verdicts = [await judge_candidate(c, traces_by_id[c.trace_id], loader, backend) for c in candidates]

    report = score_against_ground_truth(verdicts, ground_truth)
    assert report.total_verdicts == len(candidates)
    metrics = report.per_class.get("prompt_injection")
    assert metrics is not None
