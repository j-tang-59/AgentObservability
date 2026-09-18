"""`python -m agenttrace.demo` — run one mixed-runner recursive workflow with
real telemetry on, print the run_id/trace context, and exit. Used by `make
demo` and by verify.py's smoke check."""

from __future__ import annotations

import argparse
import asyncio
import json

from agenttrace.harness.fake_model import FakeChatModel
from agenttrace.harness.graph import new_run_id, run_workflow
from agenttrace.harness.run_context import RunContext
from agenttrace.harness.runners.http import HttpRunner
from agenttrace.harness.runners.inproc import InprocRunner
from agenttrace.harness.runners.subprocess import SubprocessRunner
from agenttrace.harness.scripts import default_script, make_objective
from agenttrace.telemetry.bootstrap import init_telemetry
from agenttrace.telemetry.semconv import RUNNER_HTTP, RUNNER_INPROC, RUNNER_SUBPROCESS


async def _main(fanout: int, depth: int, runner_plan: list[str], http_base_url: str) -> None:
    provider = init_telemetry("agenttrace-demo")

    model = FakeChatModel(default_script)
    run_ctx = RunContext(
        run_id=new_run_id(),
        max_depth=depth,
        max_fanout=fanout,
        max_agents=1000,
        recursion_limit=8,
        per_child_timeout_s=30.0,
        runner_plan=runner_plan,
        http_base_url=http_base_url,
    )
    run_ctx.runners = {
        RUNNER_INPROC: InprocRunner(model),
        RUNNER_SUBPROCESS: SubprocessRunner(),
        RUNNER_HTTP: HttpRunner(base_url=http_base_url),
    }
    objective = make_objective(fanout, depth, "demo run")

    trace_id_holder: dict[str, str | None] = {"trace_id": None}
    result = await run_workflow(
        objective=objective,
        run_ctx=run_ctx,
        model=model,
        on_root_span_started=lambda tid: trace_id_holder.__setitem__("trace_id", tid),
    )

    print(json.dumps({"run_id": run_ctx.run_id, "trace_id": trace_id_holder["trace_id"], "result": result}))

    if provider is not None:
        provider.force_flush()
        provider.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fanout", type=int, default=3)
    parser.add_argument("--depth", type=int, default=2)
    parser.add_argument("--runner-plan", type=str, default="inproc,subprocess,http")
    parser.add_argument("--http-base-url", type=str, default="http://127.0.0.1:8765")
    args = parser.parse_args()
    asyncio.run(
        _main(args.fanout, args.depth, args.runner_plan.split(","), args.http_base_url)
    )
