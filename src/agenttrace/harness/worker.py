"""Subprocess entrypoint: `python -m agenttrace.harness.worker`.

Protocol: one JSON object on stdin describing the child's role/objective/
budgets; the worker recreates its own RunContext (so it can itself spawn
further subagents, mixing runner types across depths) and prints exactly one
JSON object to stdout: {"result": "...", "error": null} or {"error": "..."}.

stdout is reserved for that single line. All logging goes to stderr, and
from Phase 2 on, telemetry never touches stdout either (D4).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from opentelemetry import context as otel_context

from agenttrace.harness.fake_model import FakeChatModel
from agenttrace.harness.graph import run_agent
from agenttrace.harness.run_context import RunContext
from agenttrace.harness.runners.http import HttpRunner
from agenttrace.harness.runners.inproc import InprocRunner
from agenttrace.harness.runners.subprocess import SubprocessRunner
from agenttrace.harness.scripts import default_script
from agenttrace.telemetry.bootstrap import init_telemetry, sdk_disabled
from agenttrace.telemetry.propagation import MissingTraceparentError, extract_context_from_env
from agenttrace.telemetry.semconv import RUNNER_HTTP, RUNNER_INPROC, RUNNER_SUBPROCESS


def _build_run_ctx(payload: dict[str, Any]) -> tuple[RunContext, FakeChatModel]:
    model = FakeChatModel(default_script)
    run_ctx = RunContext(
        run_id=payload["run_id"],
        max_depth=payload["max_depth"],
        max_fanout=payload["max_fanout"],
        max_agents=payload["max_agents"],
        recursion_limit=payload["recursion_limit"],
        per_child_timeout_s=payload["per_child_timeout_s"],
        runner_plan=payload["runner_plan"],
        http_base_url=payload.get("http_base_url", "http://127.0.0.1:8765"),
    )
    run_ctx._agents_spawned = payload["agents_spawned_so_far"]
    run_ctx.runners = {
        RUNNER_INPROC: InprocRunner(model),
        RUNNER_SUBPROCESS: SubprocessRunner(),
        RUNNER_HTTP: HttpRunner(base_url=run_ctx.http_base_url),
    }
    return run_ctx, model


async def _main() -> None:
    provider = init_telemetry("agenttrace-worker")
    ctx_token = None
    try:
        if not sdk_disabled():
            # Strict mode (D4): a worker that starts with no TRACEPARENT
            # raises rather than silently becoming a new trace root.
            ctx = extract_context_from_env(dict(os.environ), strict=True)
            ctx_token = otel_context.attach(ctx)

        raw = sys.stdin.readline()
        payload = json.loads(raw)
        try:
            run_ctx, model = _build_run_ctx(payload)
            result = await run_agent(
                role=payload["role"],
                objective=payload["objective"],
                depth=payload["depth"],
                run_ctx=run_ctx,
                model=model,
                agent_name=payload["role"],
                span_kind="INTERNAL",
            )
            sys.stdout.write(json.dumps({"result": result, "error": None}) + "\n")
        except Exception as exc:  # noqa: BLE001 - reported to parent as structured error
            sys.stdout.write(json.dumps({"result": None, "error": f"{type(exc).__name__}: {exc}"}) + "\n")
        sys.stdout.flush()
    except MissingTraceparentError as exc:
        sys.stdout.write(json.dumps({"result": None, "error": f"MissingTraceparentError: {exc}"}) + "\n")
        sys.stdout.flush()
    finally:
        if ctx_token is not None:
            otel_context.detach(ctx_token)
        if provider is not None:
            provider.shutdown()


if __name__ == "__main__":
    asyncio.run(_main())
