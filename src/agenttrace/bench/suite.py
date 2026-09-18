"""`python -m agenttrace.bench.suite` — runs the synthetic labeled suite
(E8): N repetitions of every fault scenario (plus clean runs), each with
known ground truth, and writes a manifest the evaluator/detectors can be
scored against.

Scope note: the design doc's target is >=300 synthetic labeled runs. This
default (REPS_PER_SCENARIO=10, 7 scenarios = 70 runs) is a scoped-down
stand-in that exercises every failure class and the clean/false-positive
baseline; raising REPS_PER_SCENARIO (or --reps) reproduces the full-size
suite with the same code path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from agenttrace.faults.injectors import run_scenario
from agenttrace.faults.scenarios import SCENARIOS

DEFAULT_REPS = 10


async def run_suite(*, reps: int, out_dir: Path) -> list[dict[str, object]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    span_file = out_dir / "spans.jsonl"
    content_root = out_dir / "content"
    if span_file.exists():
        span_file.unlink()

    manifest: list[dict[str, object]] = []
    for scenario in SCENARIOS:
        for i in range(reps):
            run = await run_scenario(scenario, span_file=str(span_file), content_store_root=str(content_root))
            manifest.append(
                {
                    "scenario_name": run.scenario_name,
                    "failure_class": run.failure_class,
                    "trace_id": run.trace_id,
                    "span_count": len(run.spans),
                    "result": run.result,
                    "rep": i,
                }
            )
            print(f"[{scenario.name}] rep {i + 1}/{reps}: trace={run.trace_id} spans={len(run.spans)}")

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"\nWrote {len(manifest)} labeled runs to {manifest_path}")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reps", type=int, default=DEFAULT_REPS)
    parser.add_argument("--out-dir", type=str, default="./.agenttrace_bench")
    args = parser.parse_args()
    asyncio.run(run_suite(reps=args.reps, out_dir=Path(args.out_dir)))


if __name__ == "__main__":
    main()
