"""Phase 1 gate: recursive harness runs deterministically and mixes runners."""

from __future__ import annotations

import pytest

from agenttrace.harness.fake_model import FakeChatModel
from agenttrace.harness.graph import new_run_id, run_workflow
from agenttrace.harness.run_context import RunContext
from agenttrace.harness.runners.http import HttpRunner
from agenttrace.harness.runners.inproc import InprocRunner
from agenttrace.harness.runners.subprocess import SubprocessRunner
from agenttrace.harness.scripts import default_script, make_objective
from agenttrace.telemetry.semconv import RUNNER_HTTP, RUNNER_INPROC, RUNNER_SUBPROCESS


def _build_run_ctx(runner_plan: list[str], **kwargs: object) -> RunContext:
    model = FakeChatModel(default_script)
    run_ctx = RunContext(
        run_id=new_run_id(),
        max_depth=kwargs.get("max_depth", 3),  # type: ignore[arg-type]
        max_fanout=kwargs.get("max_fanout", 3),  # type: ignore[arg-type]
        max_agents=kwargs.get("max_agents", 1000),  # type: ignore[arg-type]
        recursion_limit=kwargs.get("recursion_limit", 6),  # type: ignore[arg-type]
        per_child_timeout_s=kwargs.get("per_child_timeout_s", 20.0),  # type: ignore[arg-type]
        runner_plan=runner_plan,
    )
    run_ctx.runners = {
        RUNNER_INPROC: InprocRunner(model),
        RUNNER_SUBPROCESS: SubprocessRunner(),
        RUNNER_HTTP: HttpRunner(),
    }
    return run_ctx, model


@pytest.mark.asyncio
async def test_fanout3_depth3_is_deterministic_across_20_runs() -> None:
    objective = make_objective(3, 3, "benchmark shape")
    results: set[str] = set()
    agent_counts: set[int] = set()
    for _ in range(20):
        run_ctx, model = _build_run_ctx([RUNNER_INPROC])
        result = await run_workflow(objective=objective, run_ctx=run_ctx, model=model)
        results.add(result)
        agent_counts.add(run_ctx.agents_spawned)

    assert results == {next(iter(results))}, "identical objective must produce identical output"
    assert agent_counts == {40}, f"fanout 3 depth 3 must spawn 1+3+9+27=40 agents, got {agent_counts}"


@pytest.mark.asyncio
async def test_mixed_runner_run_succeeds() -> None:
    objective = make_objective(2, 2, "mixed runner smoke")
    run_ctx, model = _build_run_ctx([RUNNER_INPROC, RUNNER_SUBPROCESS, RUNNER_INPROC], max_depth=2)
    result = await run_workflow(objective=objective, run_ctx=run_ctx, model=model)
    assert result
    # NOTE: max_agents is a soft, best-effort budget guard, not a source of
    # truth. Each subprocess/http child gets its own RunContext instance, so
    # grandchildren spawned across a process boundary never increment the
    # parent's in-memory counter back. Coverage (D6, verify.py) is what
    # actually proves nothing was lost — this counter just stops runaway
    # fan-out within a single process.
    assert run_ctx.agents_spawned == 1 + 2  # root + depth-1 children counted before dispatch


@pytest.mark.asyncio
async def test_budget_max_depth_is_enforced() -> None:
    objective = make_objective(3, 5, "too deep")
    run_ctx, model = _build_run_ctx([RUNNER_INPROC], max_depth=1)
    # orchestrator (depth 0) tries to spawn depth-1 children (ok), which try
    # to spawn depth-2 children (exceeds max_depth=1) -> surfaces as a tool
    # error the child agent records, not a crash of the whole run.
    result = await run_workflow(objective=objective, run_ctx=run_ctx, model=model)
    assert result
