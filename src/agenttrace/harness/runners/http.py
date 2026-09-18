"""HTTP runner: calls a (possibly remote) agenttrace.harness.server /spawn.

Per D5, the parent's call to this runner sits inside an invoke_agent CLIENT
span (added in Phase 2); the server opens its own invoke_agent INTERNAL span
for the work it does. Context crosses via the W3C traceparent header, and the
server only extracts it from authenticated callers.
"""

from __future__ import annotations

import os

import httpx

from agenttrace.harness.run_context import RunContext
from agenttrace.telemetry.hooks import get_hooks
from agenttrace.telemetry.propagation import headers_for_http
from agenttrace.telemetry.semconv import RUNNER_HTTP, agent_span_name

SHARED_SECRET = os.environ.get("AGENTTRACE_SHARED_SECRET", "dev-secret")


class HttpRunner:
    name = RUNNER_HTTP

    def __init__(self, base_url: str = "http://127.0.0.1:8765") -> None:
        self._base_url = base_url

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
        hooks = get_hooks()
        # Per D5: the parent wraps a remote agent call in a CLIENT-kind
        # invoke_agent span; the server opens its own INTERNAL invoke_agent
        # span for the work it does. W3C headers, injected from inside this
        # CLIENT span, carry the parent-child relationship across the wire.
        with hooks.agent_span(agent_span_name(role), agent_name=role, kind="CLIENT", role=role, depth=depth):
            headers = {"Authorization": f"Bearer {SHARED_SECRET}", **headers_for_http()}
            timeout = httpx.Timeout(run_ctx.per_child_timeout_s)
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(f"{self._base_url}/spawn", json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                raise TimeoutError(f"http spawn {spawn_id} timed out") from exc
            resp.raise_for_status()
            return str(resp.json()["result"])
