"""In-process runner: same asyncio event loop, same OS process.

Context (both contextvars and, once Phase 2 wires it up, the OTel current
span) crosses for free as long as the child is launched through
asyncio.create_task / asyncio.gather rather than a raw thread pool (D2).
graph.py's fan-out already does this; this runner just calls back into
run_agent directly.
"""

from __future__ import annotations

from agenttrace.harness.fake_model import FakeChatModel
from agenttrace.harness.run_context import RunContext
from agenttrace.telemetry.semconv import RUNNER_INPROC


class InprocRunner:
    name = RUNNER_INPROC

    def __init__(self, model: FakeChatModel) -> None:
        self._model = model

    async def spawn(
        self,
        *,
        role: str,
        objective: str,
        depth: int,
        run_ctx: RunContext,
        spawn_id: str,
    ) -> str:
        from agenttrace.harness.graph import run_agent  # local import: avoid cycle

        return await run_agent(
            role=role,
            objective=objective,
            depth=depth,
            run_ctx=run_ctx,
            model=self._model,
            agent_name=role,
            span_kind="INTERNAL",
        )
