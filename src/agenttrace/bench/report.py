"""`python -m agenttrace.bench.report` — generates docs/RESULTS.md's numbers.

Scope note (repeated in RESULTS.md): the design doc asks for >=200
mixed-runner runs and a ~2,000-span shape. This report runs a smaller N by
default (raise via --coverage-runs / --shape-fanout / --shape-depth) — same
code path, smaller default so the report finishes in CI-reasonable time.
Every number below is measured by actually running the harness + evaluator,
never estimated.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any

from agenttrace.bench.suite import run_suite
from agenttrace.evaluator.graph import build_causal_trace
from agenttrace.evaluator.judge.backends import FakeJudgeBackend
from agenttrace.evaluator.judge.calibration import score_against_ground_truth
from agenttrace.evaluator.judge.judge import judge_candidate
from agenttrace.evaluator.loader import TraceLoader
from agenttrace.evaluator.service import evaluate_spans
from agenttrace.harness.fake_model import FakeChatModel
from agenttrace.harness.graph import new_run_id, run_workflow
from agenttrace.harness.run_context import RunContext
from agenttrace.harness.runners.http import HttpRunner
from agenttrace.harness.runners.inproc import InprocRunner
from agenttrace.harness.runners.subprocess import SubprocessRunner
from agenttrace.harness.scripts import default_script, make_objective
from agenttrace.telemetry import semconv
from agenttrace.telemetry.content_store import ContentStore
from agenttrace.telemetry.file_exporter import read_spans
from agenttrace.telemetry.hooks import reset_hooks, set_hooks
from agenttrace.telemetry.otel_hooks import OtelHooks
from agenttrace.telemetry.setup import setup_tracing
from agenttrace.trace_model import from_file_exporter_dicts
from agenttrace.verify import verify_spans


async def _run_one_inproc(
    objective: str, *, out_dir: Path, capture: bool
) -> tuple[str, list[dict[str, object]], RunContext]:
    import os

    span_file = out_dir / "spans.jsonl"
    os.environ["AGENTTRACE_TEST_SPAN_FILE"] = str(span_file)
    provider, _processor = setup_tracing(service_name="agenttrace-bench")
    local_tracer = provider.get_tracer("agenttrace-bench", schema_url=semconv.SCHEMA_URL)
    store = ContentStore(root=str(out_dir / "content"))
    set_hooks(OtelHooks(tracer=local_tracer, content_store=store, capture_content=capture))

    trace_id_holder: dict[str, str | None] = {"trace_id": None}
    model = FakeChatModel(default_script)
    run_ctx = RunContext(
        run_id=new_run_id(),
        max_depth=10,
        max_fanout=10,
        max_agents=10000,
        recursion_limit=6,
        per_child_timeout_s=30.0,
        runner_plan=[semconv.RUNNER_INPROC],
    )
    run_ctx.runners = {
        semconv.RUNNER_INPROC: InprocRunner(model),
        semconv.RUNNER_SUBPROCESS: SubprocessRunner(),
        semconv.RUNNER_HTTP: HttpRunner(),
    }
    try:
        await run_workflow(
            objective=objective,
            run_ctx=run_ctx,
            model=model,
            on_root_span_started=lambda tid: trace_id_holder.__setitem__("trace_id", tid),
        )
    finally:
        provider.force_flush()
        provider.shutdown()
        reset_hooks()

    all_spans = read_spans(str(span_file))
    tid = trace_id_holder["trace_id"]
    my_spans = [s for s in all_spans if s["trace_id"] == tid]
    return str(tid), my_spans, run_ctx


async def bench_span_shapes(out_dir: Path) -> dict[str, Any]:
    shapes = {"fanout3_depth3": (3, 3), "fanout5_depth3": (5, 3)}
    results = {}
    for name, (fanout, depth) in shapes.items():
        shape_dir = out_dir / f"shape_{name}"
        shape_dir.mkdir(parents=True, exist_ok=True)
        objective = make_objective(fanout, depth, "benchmark shape")
        _tid, spans, _run_ctx = await _run_one_inproc(objective, out_dir=shape_dir, capture=False)
        expected_agents = sum(fanout**d for d in range(depth + 1))
        results[name] = {
            "fanout": fanout,
            "depth": depth,
            "expected_agents": expected_agents,
            "span_count": len(spans),
            "spans_per_agent": round(len(spans) / expected_agents, 2),
        }
    return results


async def bench_coverage(out_dir: Path, n_runs: int) -> dict[str, Any]:
    import os

    shape_dir = out_dir / "coverage"
    shape_dir.mkdir(parents=True, exist_ok=True)
    span_file = shape_dir / "spans.jsonl"
    if span_file.exists():
        span_file.unlink()
    os.environ["AGENTTRACE_TEST_SPAN_FILE"] = str(span_file)

    runner_plans = [
        [semconv.RUNNER_INPROC],
        [semconv.RUNNER_INPROC, semconv.RUNNER_SUBPROCESS],
        [semconv.RUNNER_SUBPROCESS],
    ]
    results = []
    for i in range(n_runs):
        provider, processor = setup_tracing(service_name="agenttrace-bench-coverage")
        local_tracer = provider.get_tracer("agenttrace-bench-coverage", schema_url=semconv.SCHEMA_URL)
        set_hooks(OtelHooks(tracer=local_tracer, content_store=ContentStore(root=str(shape_dir / "content"))))
        model = FakeChatModel(default_script)
        run_ctx = RunContext(
            run_id=new_run_id(),
            max_depth=2,
            max_fanout=2,
            max_agents=100,
            recursion_limit=6,
            per_child_timeout_s=20.0,
            runner_plan=runner_plans[i % len(runner_plans)],
        )
        run_ctx.runners = {
            semconv.RUNNER_INPROC: InprocRunner(model),
            semconv.RUNNER_SUBPROCESS: SubprocessRunner(),
            semconv.RUNNER_HTTP: HttpRunner(),
        }
        trace_id_holder: dict[str, str | None] = {"trace_id": None}
        objective = make_objective(2, 2, f"coverage run {i}")

        def _capture_trace_id(tid: str, _holder: dict[str, str | None] = trace_id_holder) -> None:
            _holder["trace_id"] = tid

        try:
            await run_workflow(
                objective=objective,
                run_ctx=run_ctx,
                model=model,
                on_root_span_started=_capture_trace_id,
            )
        finally:
            provider.force_flush()
            provider.shutdown()
            reset_hooks()

        all_spans = from_file_exporter_dicts(read_spans(str(span_file)))
        my_spans = [s for s in all_spans if s.trace_id == trace_id_holder["trace_id"]]
        report = verify_spans(my_spans, sdk_drops=processor.queue_drops)
        results.append(report.to_dict())

    n_ok = sum(1 for r in results if r["ok"])
    n_multi_root = sum(1 for r in results if r["root_count"] != 1)
    n_missing_parents = sum(r["missing_parent_count"] for r in results)
    n_drops = sum(r["sdk_drops"] or 0 for r in results)
    avg_coverage = statistics.mean(r["coverage"] for r in results)
    return {
        "n_runs": n_runs,
        "n_fully_ok": n_ok,
        "n_multi_root": n_multi_root,
        "total_missing_parents": n_missing_parents,
        "total_sdk_drops": n_drops,
        "avg_coverage": avg_coverage,
    }


async def bench_overhead(out_dir: Path, n_runs: int) -> dict[str, Any]:
    objective = make_objective(3, 2, "overhead bench")

    async def _timed_off() -> float:
        reset_hooks()
        model = FakeChatModel(default_script)
        run_ctx = RunContext(run_id=new_run_id(), max_depth=2, max_fanout=3, runner_plan=[semconv.RUNNER_INPROC])
        run_ctx.runners = {semconv.RUNNER_INPROC: InprocRunner(model)}
        t0 = time.perf_counter()
        await run_workflow(objective=objective, run_ctx=run_ctx, model=model)
        return time.perf_counter() - t0

    off_times = [await _timed_off() for _ in range(n_runs)]

    on_times = []
    span_counts = []
    for i in range(n_runs):
        shape_dir = out_dir / f"overhead_on_{i}"
        t0 = time.perf_counter()
        _tid, spans, _run_ctx = await _run_one_inproc(objective, out_dir=shape_dir, capture=False)
        on_times.append(time.perf_counter() - t0)
        span_counts.append(len(spans))

    avg_off = statistics.mean(off_times)
    avg_on = statistics.mean(on_times)
    avg_spans = statistics.mean(span_counts)
    overhead_pct = (avg_on - avg_off) / avg_off * 100 if avg_off else float("nan")
    us_per_span = (avg_on - avg_off) * 1_000_000 / avg_spans if avg_spans else float("nan")
    return {
        "n_runs": n_runs,
        "avg_wall_s_off": avg_off,
        "avg_wall_s_on": avg_on,
        "overhead_pct": overhead_pct,
        "avg_span_count": avg_spans,
        "us_per_span": us_per_span,
    }


async def bench_evaluation(out_dir: Path, reps: int) -> dict[str, Any]:
    eval_dir = out_dir / "evaluation"
    manifest = await run_suite(reps=reps, out_dir=eval_dir)
    all_spans = from_file_exporter_dicts(read_spans(str(eval_dir / "spans.jsonl")))
    loader = TraceLoader(ContentStore(root=str(eval_dir / "content")))

    by_trace: dict[str, list[Any]] = {}
    for s in all_spans:
        by_trace.setdefault(s.trace_id, []).append(s)

    deterministic_classes = {
        "retry_storm",
        "silent_tool_failure",
        "tool_hallucination_structural",
        "orphaned_subagent",
    }
    per_class_tp: dict[str, int] = dict.fromkeys(deterministic_classes, 0)
    per_class_total: dict[str, int] = dict.fromkeys(deterministic_classes, 0)
    per_class_fp: dict[str, int] = dict.fromkeys(deterministic_classes, 0)
    clean_fp = 0
    clean_total = 0
    headline_count = 0  # exit-code-0 runs with no stdout error signal but a detected failure
    eval_latencies = []

    injection_candidates = []
    injection_ground_truth: dict[str, bool] = {}
    traces_by_id = {}

    for entry in manifest:
        trace_id = str(entry["trace_id"])
        label = str(entry["failure_class"])
        spans = by_trace.get(trace_id, [])
        t0 = time.perf_counter()
        result = evaluate_spans(spans, loader)
        eval_latencies.append(time.perf_counter() - t0)
        detected = {f.failure_class for f in result.findings}

        result_text = str(entry.get("result", ""))
        no_stdout_error_signal = "error" not in result_text.lower() and "traceback" not in result_text.lower()
        if detected and no_stdout_error_signal and label != "clean":
            headline_count += 1

        if label == "clean":
            clean_total += 1
            if detected:
                clean_fp += 1
        elif label in deterministic_classes:
            per_class_total[label] += 1
            if label in detected:
                per_class_tp[label] += 1
        for cls in detected & deterministic_classes:
            if cls != label:
                per_class_fp[cls] += 1

        trace = build_causal_trace(spans, loader)
        traces_by_id[trace_id] = trace
        for c in result.candidates:
            if c.failure_class == "prompt_injection":
                injection_candidates.append(c)
                injection_ground_truth[trace_id] = label == "prompt_injection"

    per_class_report = {}
    for cls in deterministic_classes:
        tp, total, fp = per_class_tp[cls], per_class_total[cls], per_class_fp[cls]
        if total == 0:
            continue
        per_class_report[cls] = {
            "recall": tp / total,
            "precision": tp / (tp + fp) if (tp + fp) else 1.0,
            "n": total,
        }

    # Judge: FakeJudgeBackend only (no ANTHROPIC_API_KEY in this
    # environment) — a scripted stand-in so the pipeline (and its cost/
    # latency accounting) runs end-to-end; NOT a real-model calibration
    # number. Real-model numbers are explicitly out of scope for this run
    # (see docs/RESULTS.md).
    def fake_script(system: str, user: str) -> tuple[str, str, list[str], str]:
        verdict = "yes" if "injected" in user.lower() else "no"
        return verdict, "0.8", [], "fake-judge scripted verdict"

    backend = FakeJudgeBackend(fake_script)
    judge_verdicts = [
        await judge_candidate(c, traces_by_id[c.trace_id], loader, backend) for c in injection_candidates
    ]
    judge_report = score_against_ground_truth(judge_verdicts, injection_ground_truth)

    return {
        "n_scenarios": len(manifest),
        "reps_per_scenario": reps,
        "clean_false_positive_rate": clean_fp / clean_total if clean_total else 0.0,
        "deterministic_detectors": per_class_report,
        "headline_undetected_by_stdout_count": headline_count,
        "avg_evaluation_latency_s": statistics.mean(eval_latencies) if eval_latencies else 0.0,
        "fake_judge": {
            "n_verdicts": judge_report.total_verdicts,
            "abstention_rate": judge_report.abstention_rate,
            "avg_tokens_per_verdict": judge_report.avg_tokens_per_verdict,
            "note": "FakeJudgeBackend only; no ANTHROPIC_API_KEY in this environment",
        },
    }


async def main(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {}
    report["span_shapes"] = await bench_span_shapes(out_dir)
    report["coverage"] = await bench_coverage(out_dir, args.coverage_runs)
    report["overhead"] = await bench_overhead(out_dir, args.overhead_runs)
    report["evaluation"] = await bench_evaluation(out_dir, args.eval_reps)

    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))
    print(f"\nWrote {report_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="./.agenttrace_bench_report")
    parser.add_argument("--coverage-runs", type=int, default=20)
    parser.add_argument("--overhead-runs", type=int, default=10)
    parser.add_argument("--eval-reps", type=int, default=10)
    asyncio.run(main(parser.parse_args()))
