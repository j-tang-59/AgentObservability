"""Phase 5 gate: deterministic detectors score >=0.95 precision and recall
on the synthetic labeled suite (E8), with zero confirmed false positives on
clean runs.

Runs bench/suite.py's full scenario set (a handful of reps each — enough to
be a meaningful check without slowing down the default test run; `make
bench` / bench/suite.py --reps N reproduces the full-size suite) and scores
each detector's CONFIRMED FINDINGS (never Candidates, which are deliberately
over-inclusive and deferred to the judge in Phase 6) against ground truth.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agenttrace.bench.suite import run_suite
from agenttrace.evaluator.loader import TraceLoader
from agenttrace.evaluator.service import evaluate_spans
from agenttrace.faults.scenarios import SCENARIOS
from agenttrace.telemetry.content_store import ContentStore
from agenttrace.telemetry.file_exporter import read_spans
from agenttrace.trace_model import from_file_exporter_dicts

REPS = 6


@pytest.mark.asyncio
async def test_deterministic_detector_precision_recall(tmp_path: Path) -> None:
    manifest = await run_suite(reps=REPS, out_dir=tmp_path)

    all_spans = from_file_exporter_dicts(read_spans(str(tmp_path / "spans.jsonl")))
    by_trace: dict[str, list] = {}
    for s in all_spans:
        by_trace.setdefault(s.trace_id, []).append(s)
    loader = TraceLoader(ContentStore(root=str(tmp_path / "content")))

    # failure classes a deterministic detector can CONFIRM outright (not
    # just candidate) — matches what service.py's `findings` list can ever
    # contain, per E5/E1.
    deterministic_classes = {
        "retry_storm",
        "silent_tool_failure",
        "tool_hallucination_structural",
        "orphaned_subagent",
    }

    per_class_tp: dict[str, int] = dict.fromkeys(deterministic_classes, 0)
    per_class_fn: dict[str, int] = dict.fromkeys(deterministic_classes, 0)
    per_class_fp: dict[str, int] = dict.fromkeys(deterministic_classes, 0)
    clean_false_positives = 0
    clean_total = 0

    for entry in manifest:
        trace_id = str(entry["trace_id"])
        label = str(entry["failure_class"])
        spans = by_trace.get(trace_id, [])
        result = evaluate_spans(spans, loader)
        detected = {f.failure_class for f in result.findings}

        if label == "clean":
            clean_total += 1
            if detected:
                clean_false_positives += 1
            continue

        if label in deterministic_classes:
            if label in detected:
                per_class_tp[label] += 1
            else:
                per_class_fn[label] += 1

        # any deterministic class detected on a run labeled as a DIFFERENT
        # class is a false positive for that class
        for cls in detected & deterministic_classes:
            if cls != label:
                per_class_fp[cls] += 1

    assert clean_false_positives == 0, f"{clean_false_positives}/{clean_total} clean runs produced a false finding"

    for cls in deterministic_classes:
        tp, fn, fp = per_class_tp[cls], per_class_fn[cls], per_class_fp[cls]
        total_labeled = tp + fn
        if total_labeled == 0:
            continue
        recall = tp / total_labeled
        precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
        assert recall >= 0.95, f"{cls}: recall {recall:.2f} ({tp}/{total_labeled}) below 0.95 gate"
        assert precision >= 0.95, f"{cls}: precision {precision:.2f} ({tp}/{tp + fp}) below 0.95 gate"

    assert len(SCENARIOS) >= 6  # sanity: the suite still covers every class
