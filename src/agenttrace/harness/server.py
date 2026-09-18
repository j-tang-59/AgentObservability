"""FastAPI server exposing the agent graph as an HTTP agent (D5).

The server only accepts spawn requests carrying a bearer token matching
AGENTTRACE_SHARED_SECRET — Phase 2 adds W3C traceparent extraction, and that
extraction must only happen for authenticated callers, never for arbitrary
inbound headers (D5's "attacker-controlled headers must never graft spans
into a run").
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Header, HTTPException, Request
from opentelemetry import context as otel_context
from pydantic import BaseModel

from agenttrace.harness.fake_model import FakeChatModel
from agenttrace.harness.graph import run_agent
from agenttrace.harness.run_context import RunContext
from agenttrace.harness.runners.http import HttpRunner
from agenttrace.harness.runners.inproc import InprocRunner
from agenttrace.harness.runners.subprocess import SubprocessRunner
from agenttrace.harness.scripts import default_script
from agenttrace.telemetry.bootstrap import init_telemetry
from agenttrace.telemetry.propagation import extract_context_from_headers
from agenttrace.telemetry.semconv import RUNNER_HTTP, RUNNER_INPROC, RUNNER_SUBPROCESS

app = FastAPI(title="agenttrace-worker")
_TRACER_PROVIDER = init_telemetry("agenttrace-http-worker")

SHARED_SECRET = os.environ.get("AGENTTRACE_SHARED_SECRET", "dev-secret")


class SpawnRequest(BaseModel):
    role: str
    objective: str
    depth: int
    run_id: str
    max_depth: int
    max_fanout: int
    max_agents: int
    recursion_limit: int
    per_child_timeout_s: float
    runner_plan: list[str]
    http_base_url: str = "http://127.0.0.1:8765"
    agents_spawned_so_far: int = 1


class SpawnResponse(BaseModel):
    result: str


def _authenticated(authorization: str | None) -> bool:
    return authorization == f"Bearer {SHARED_SECRET}"


@app.post("/spawn", response_model=SpawnResponse)
async def spawn(
    req: SpawnRequest, request: Request, authorization: str | None = Header(default=None)
) -> SpawnResponse:
    if not _authenticated(authorization):
        raise HTTPException(status_code=403, detail="unauthenticated caller")

    # Context (and therefore parentage) is only ever extracted from an
    # authenticated caller's headers (D5) — an unauthenticated request would
    # be rejected above before this line runs, so an attacker's traceparent
    # can never graft a span into a real run.
    ctx = extract_context_from_headers(dict(request.headers))
    token = otel_context.attach(ctx)
    try:
        model = FakeChatModel(default_script)
        run_ctx = RunContext(
            run_id=req.run_id,
            max_depth=req.max_depth,
            max_fanout=req.max_fanout,
            max_agents=req.max_agents,
            recursion_limit=req.recursion_limit,
            per_child_timeout_s=req.per_child_timeout_s,
            runner_plan=req.runner_plan,
            http_base_url=req.http_base_url,
        )
        run_ctx._agents_spawned = req.agents_spawned_so_far
        run_ctx.runners = {
            RUNNER_INPROC: InprocRunner(model),
            RUNNER_SUBPROCESS: SubprocessRunner(),
            RUNNER_HTTP: HttpRunner(base_url=req.http_base_url),
        }
        result = await run_agent(
            role=req.role,
            objective=req.objective,
            depth=req.depth,
            run_ctx=run_ctx,
            model=model,
            agent_name=req.role,
            span_kind="INTERNAL",
        )
        return SpawnResponse(result=result)
    finally:
        otel_context.detach(token)
        # This is a synchronous request/response HTTP agent: the caller is
        # about to query this trace (directly, or via the evaluator) as
        # soon as it gets a response back. Flushing here — rather than
        # relying on this long-lived process's BatchSpanProcessor to get to
        # it on its own multi-second schedule — keeps "the server responded"
        # and "the server's spans are queryable" from racing each other.
        if _TRACER_PROVIDER is not None:
            _TRACER_PROVIDER.force_flush()


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}
