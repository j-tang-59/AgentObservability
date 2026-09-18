"""Subprocess runner: launches `python -m agenttrace.harness.worker`.

Context crosses via environment variables (TRACEPARENT/TRACESTATE/BAGGAGE),
wired up in Phase 2 (telemetry.propagation). This module only owns process
lifecycle: launch, feed stdin, read exactly one stdout line, and guarantee a
SIGKILL (reported as spawn outcome "killed", not "timeout") if the child
outlives its budget.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys

from agenttrace.harness.run_context import ChildKilledError, RunContext
from agenttrace.telemetry.propagation import env_updates_for_subprocess
from agenttrace.telemetry.semconv import RUNNER_SUBPROCESS

_KILL_GRACE_S = 2.0


class SubprocessRunner:
    name = RUNNER_SUBPROCESS

    async def spawn(
        self,
        *,
        role: str,
        objective: str,
        depth: int,
        run_ctx: RunContext,
        spawn_id: str,
    ) -> str:
        payload = run_ctx.to_spawn_payload(role=role, objective=objective, depth=depth)
        env = dict(os.environ)
        # Inject from inside the execute_tool spawn_subagent span (already
        # current here), so the child's parent is that span, not whatever
        # ancestor happened to be current when the harness started (D4).
        env.update(env_updates_for_subprocess(run_ctx.run_id, depth))

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "agenttrace.harness.worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write((json.dumps(payload) + "\n").encode())
        await proc.stdin.drain()
        proc.stdin.close()

        try:
            stdout_data, _stderr_data = await asyncio.wait_for(
                proc.communicate(), timeout=run_ctx.per_child_timeout_s
            )
        except TimeoutError:
            proc.kill()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_S)
            raise ChildKilledError(
                f"subprocess for spawn {spawn_id} missed its {run_ctx.per_child_timeout_s}s budget and was SIGKILLed"
            ) from None
        except asyncio.CancelledError:
            proc.kill()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_S)
            raise ChildKilledError(f"subprocess for spawn {spawn_id} killed after cancellation") from None

        if proc.returncode != 0 and not stdout_data:
            raise RuntimeError(f"worker subprocess exited {proc.returncode} with no output")

        line = stdout_data.decode().strip().splitlines()[-1] if stdout_data.strip() else "{}"
        parsed = json.loads(line)
        if parsed.get("error"):
            raise RuntimeError(f"child agent error: {parsed['error']}")
        return str(parsed.get("result", ""))
