"""Local live-demo backend for AgentTrace.

Serves index.html and a streaming API that runs the *real* harness — real
OpenTelemetry spans, real cross-process/HTTP propagation, real evaluator
detectors — against a deterministic FakeChatModel (no external API calls,
no docker/Collector/Tempo required), and pushes each span to the browser as
it is actually created, over Server-Sent Events, so the agent tree animates
in as the run genuinely progresses rather than being revealed all at once.

Same offline file-exporter technique used by
tests/property/test_propagation_coverage.py and faults/injectors.py.

Run with: uv run python demo_server.py
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from opentelemetry.sdk.trace import SpanProcessor

REPO_ROOT = Path(__file__).parent
DEMO_DIR = REPO_ROOT / ".agenttrace_demo"
SPAN_FILE = DEMO_DIR / "spans.jsonl"
CONTENT_ROOT = DEMO_DIR / "content"

# Must be set before any agenttrace.telemetry module reads them at import
# time (ContentStore's default is captured at first use, the span exporter
# choice is read per setup_tracing() call).
os.environ["AGENTTRACE_TEST_SPAN_FILE"] = str(SPAN_FILE)
os.environ["AGENTTRACE_CONTENT_STORE"] = str(CONTENT_ROOT)

from agenttrace.evaluator.loader import TraceLoader
from agenttrace.evaluator.service import evaluate_spans
from agenttrace.faults.injectors import _default_overrides
from agenttrace.faults.scenarios import SCENARIOS, SCENARIOS_BY_NAME
from agenttrace.harness.fake_model import FakeChatModel
from agenttrace.harness.graph import new_run_id, run_workflow
from agenttrace.harness.run_context import RunContext
from agenttrace.harness.runners.http import HttpRunner
from agenttrace.harness.runners.inproc import InprocRunner
from agenttrace.harness.runners.subprocess import SubprocessRunner
from agenttrace.harness.scripts import default_script, make_objective
from agenttrace.telemetry import semconv
from agenttrace.telemetry.content_store import ContentStore
from agenttrace.telemetry.file_exporter import read_spans, span_to_dict
from agenttrace.telemetry.hooks import reset_hooks, set_hooks
from agenttrace.telemetry.otel_hooks import OtelHooks
from agenttrace.telemetry.setup import setup_tracing
from agenttrace.trace_model import SpanRecord, from_file_exporter_dicts

DESCRIPTIONS: dict[str, str] = {
    "clean": "No injected fault — the false-positive baseline.",
    "retry_storm": "Same (agent, tool, args) repeated back to back — deterministic detector.",
    "silent_tool_failure": "A failure-shaped tool result flows into a later call with no retry, and no error status — deterministic detector.",
    "structural_tool_hallucination": "Agent calls a tool name absent from its own declared tool definitions — deterministic detector.",
    "fabricated_tool_result": "Result text cites a tool with no matching span in the subtree — candidate, escalated to the LLM judge.",
    "orphaned_subagent": "Child spawn recorded, but killed for missing its timeout budget — deterministic detector.",
    "prompt_injection": "Untrusted fetched content flows into a later tool call's arguments — candidate, escalated to the LLM judge.",
    "mixed_boundary": "One objective, three transports: in-process, subprocess, HTTP — reconstructed as one trace, one root.",
}

MIXED_BOUNDARY_NAME = "mixed_boundary"
TOOL_SPAWN_NAME = semconv.tool_span_name("spawn_subagent")

# Artificial pacing so a run that completes in native milliseconds (the fake
# model is near-instant) is still watchable as an animation. These sleeps
# run synchronously inside the real span lifecycle callbacks below, so they
# genuinely pace the run itself rather than faking a delay after the fact.
PACE_ON_START_S = 0.10
PACE_ON_END_S = 0.05
CASCADE_REVEAL_S = 0.05
CASCADE_SETTLE_S = 0.06

_run_lock = asyncio.Lock()
_http_proc: subprocess.Popen[bytes] | None = None
_http_base_url: str | None = None


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    if DEMO_DIR.exists():
        shutil.rmtree(DEMO_DIR)
    DEMO_DIR.mkdir(parents=True)
    CONTENT_ROOT.mkdir(parents=True, exist_ok=True)
    yield
    global _http_proc
    if _http_proc is not None:
        _http_proc.terminate()
        try:
            _http_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _http_proc.kill()
        _http_proc = None


app = FastAPI(title="agenttrace-demo", lifespan=_lifespan)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _ensure_http_worker() -> str:
    global _http_proc, _http_base_url
    if _http_proc is not None and _http_base_url is not None:
        return _http_base_url

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env["AGENTTRACE_TEST_SPAN_FILE"] = str(SPAN_FILE)
    env["AGENTTRACE_CONTENT_STORE"] = str(CONTENT_ROOT)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agenttrace.harness.server:app",
         "--port", str(port), "--log-level", "warning"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    async with httpx.AsyncClient() as client:
        for _ in range(50):
            try:
                r = await client.get(f"{base_url}/health", timeout=0.5)
                if r.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
        else:
            proc.kill()
            raise RuntimeError("agenttrace HTTP worker did not start in time")

    _http_proc = proc
    _http_base_url = base_url
    return base_url


class StreamingSpanProcessor(SpanProcessor):
    """Bridges real OTel span lifecycle callbacks (called synchronously,
    in true execution order, from inside the harness's own async code) to
    an asyncio.Queue the SSE endpoint drains — plus a small sleep on each
    callback so the resulting animation is watchable rather than instant."""

    def __init__(self, on_event: Any) -> None:
        self._on_event = on_event

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        time.sleep(PACE_ON_START_S)
        ctx = span.get_span_context()
        parent = span.parent
        self._on_event({
            "phase": "pending",
            "span_id": format(ctx.span_id, "016x"),
            "parent_span_id": format(parent.span_id, "016x") if parent else None,
            "name": span.name,
            "kind": span.kind.name,
        })

    def on_end(self, span: Any) -> None:
        time.sleep(PACE_ON_END_S)
        raw = span_to_dict(span)
        self._on_event({
            "phase": "settled",
            "span_id": raw["span_id"],
            "status": raw["status"],
            "start": raw["start_time_unix_nano"],
            "end": raw["end_time_unix_nano"],
            "raw": raw,
        })

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def _spawn_runner_of(span: SpanRecord) -> str | None:
    """The runner recorded on an execute_tool spawn_subagent span's own
    spawn_intent event (D5's agenttrace.spawn.runner is an event attribute,
    not a span attribute — see OtelHooks.spawn_intent)."""
    for event in span.events:
        if event.get("name") == semconv.EVENT_SPAWN_INTENT:
            runner = event.get("attributes", {}).get(semconv.AT_SPAWN_RUNNER)
            if runner:
                return str(runner)
    return None


def _runner_lane_for(span: SpanRecord, by_id: dict[str, SpanRecord]) -> str:
    cur: SpanRecord | None = span
    seen = 0
    while cur is not None and seen < 64:
        runner = _spawn_runner_of(cur)
        if runner:
            return runner
        cur = by_id.get(cur.parent_span_id) if cur.parent_span_id else None
        seen += 1
    return semconv.RUNNER_INPROC


def _serialize_span(span: SpanRecord, by_id: dict[str, SpanRecord]) -> dict[str, Any]:
    attrs = {
        k: v for k, v in span.attributes.items()
        if isinstance(v, (str, int, float, bool)) or v is None
    }
    return {
        "span_id": span.span_id,
        "parent_span_id": span.parent_span_id,
        "name": span.name,
        "kind": span.kind,
        "status": span.status,
        "start": span.start_time_unix_nano,
        "end": span.end_time_unix_nano,
        "lane": _runner_lane_for(span, by_id),
        "attributes": attrs,
    }


async def _run_scenario_core(name: str, on_span_event: Any, on_root_span_started: Any) -> str:
    scenario = SCENARIOS_BY_NAME.get(name)
    if scenario is None:
        raise ValueError(f"unknown scenario {name!r}")
    if scenario.setup:
        scenario.setup()

    provider, _processor = setup_tracing(service_name="agenttrace-demo")
    provider.add_span_processor(StreamingSpanProcessor(on_span_event))
    local_tracer = provider.get_tracer("agenttrace-demo", schema_url=semconv.SCHEMA_URL)
    store = ContentStore(root=CONTENT_ROOT)
    set_hooks(OtelHooks(tracer=local_tracer, content_store=store, capture_content=True))

    try:
        model = FakeChatModel(scenario.script_factory())
        overrides = {**_default_overrides(), **scenario.run_ctx_overrides}
        run_ctx = RunContext(run_id=new_run_id(), **overrides)
        run_ctx.runners = {
            semconv.RUNNER_INPROC: InprocRunner(model),
            semconv.RUNNER_SUBPROCESS: SubprocessRunner(),
            semconv.RUNNER_HTTP: HttpRunner(base_url=run_ctx.http_base_url),
        }
        result = await run_workflow(
            objective=scenario.objective,
            run_ctx=run_ctx,
            model=model,
            on_root_span_started=on_root_span_started,
            role=scenario.role,
        )
    finally:
        provider.force_flush()
        provider.shutdown()
        reset_hooks()
    return result


async def _run_mixed_boundary_core(on_span_event: Any, on_root_span_started: Any) -> str:
    http_base_url = await _ensure_http_worker()

    provider, _processor = setup_tracing(service_name="agenttrace-demo")
    provider.add_span_processor(StreamingSpanProcessor(on_span_event))
    local_tracer = provider.get_tracer("agenttrace-demo", schema_url=semconv.SCHEMA_URL)
    store = ContentStore(root=CONTENT_ROOT)
    set_hooks(OtelHooks(tracer=local_tracer, content_store=store, capture_content=True))

    try:
        model = FakeChatModel(default_script)
        run_ctx = RunContext(
            run_id=new_run_id(),
            max_depth=3,
            max_fanout=1,
            max_agents=10,
            recursion_limit=8,
            per_child_timeout_s=15.0,
            runner_plan=[semconv.RUNNER_INPROC, semconv.RUNNER_SUBPROCESS, semconv.RUNNER_HTTP],
            http_base_url=http_base_url,
        )
        run_ctx.runners = {
            semconv.RUNNER_INPROC: InprocRunner(model),
            semconv.RUNNER_SUBPROCESS: SubprocessRunner(),
            semconv.RUNNER_HTTP: HttpRunner(base_url=http_base_url),
        }
        objective = make_objective(1, 3, "mixed boundary demo")
        result = await run_workflow(
            objective=objective,
            run_ctx=run_ctx,
            model=model,
            on_root_span_started=on_root_span_started,
        )
    finally:
        provider.force_flush()
        provider.shutdown()
        reset_hooks()
    return result


async def _reveal_new_spans(
    queue: asyncio.Queue[dict[str, Any]],
    seen: set[str],
    lane_map: dict[str, str],
    trace_id: str,
    known: dict[str, SpanRecord],
) -> None:
    """Called when a spawn_subagent tool span (in-process or crossing a
    process boundary) has ended. Its child's spans may already be sitting
    in the shared span file — flushed by the subprocess worker at exit, or
    by the HTTP worker's own batch processor — even though our own
    in-process StreamingSpanProcessor never saw them directly. Reveal
    whichever ones are new, in start-time order, paced like a cascade.

    ``known`` carries every span this process has already settled live
    (StreamingSpanProcessor's on_end fires well before our own
    BatchSpanProcessor gets around to flushing that same span to the
    shared file, on its own 2s schedule) — the ancestor lookup below needs
    it merged in, or a spawning tool span's own runner attribute can be
    briefly invisible and mislabel its child's lane as 'inproc'."""
    all_spans = from_file_exporter_dicts(read_spans(str(SPAN_FILE)))
    my_spans = [s for s in all_spans if s.trace_id == trace_id]
    by_id: dict[str, SpanRecord] = dict(known)
    by_id.update({s.span_id: s for s in my_spans})
    new_spans = sorted(
        (s for s in my_spans if s.span_id not in seen), key=lambda s: s.start_time_unix_nano
    )
    for s in new_spans:
        seen.add(s.span_id)
        lane = _runner_lane_for(s, by_id)
        lane_map[s.span_id] = lane
        queue.put_nowait({
            "type": "pending", "span_id": s.span_id, "parent_span_id": s.parent_span_id,
            "name": s.name, "kind": s.kind, "lane": lane,
        })
        await asyncio.sleep(CASCADE_REVEAL_S)
        queue.put_nowait({
            "type": "settled", "span_id": s.span_id, "status": s.status,
            "start": s.start_time_unix_nano, "end": s.end_time_unix_nano,
        })
        await asyncio.sleep(CASCADE_SETTLE_S)


async def _execute_and_stream(name: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
    seen: set[str] = set()
    lane_map: dict[str, str] = {}
    known: dict[str, SpanRecord] = {}
    trace_id_holder: dict[str, str | None] = {"trace_id": None}
    tail_tasks: list[asyncio.Task[None]] = []

    def on_root_span_started(trace_id: str) -> None:
        trace_id_holder["trace_id"] = trace_id

    def on_span_event(ev: dict[str, Any]) -> None:
        if ev["phase"] == "pending":
            parent = ev["parent_span_id"]
            lane = lane_map.get(parent, semconv.RUNNER_INPROC) if parent else semconv.RUNNER_INPROC
            lane_map[ev["span_id"]] = lane
            seen.add(ev["span_id"])
            queue.put_nowait({
                "type": "pending", "span_id": ev["span_id"], "parent_span_id": parent,
                "name": ev["name"], "kind": ev["kind"], "lane": lane,
            })
        else:
            queue.put_nowait({
                "type": "settled", "span_id": ev["span_id"], "status": ev["status"],
                "start": ev["start"], "end": ev["end"],
            })
            known[ev["span_id"]] = from_file_exporter_dicts([ev["raw"]])[0]
            if ev["raw"]["name"] == TOOL_SPAWN_NAME:
                trace_id = trace_id_holder["trace_id"]
                if trace_id:
                    tail_tasks.append(
                        asyncio.create_task(
                            _reveal_new_spans(queue, seen, lane_map, trace_id, known)
                        )
                    )

    try:
        if name == MIXED_BOUNDARY_NAME:
            result = await _run_mixed_boundary_core(on_span_event, on_root_span_started)
        else:
            result = await _run_scenario_core(name, on_span_event, on_root_span_started)

        if tail_tasks:
            await asyncio.gather(*tail_tasks)
        await asyncio.sleep(0.3)  # let any trailing batch flush land in the shared file

        trace_id = str(trace_id_holder["trace_id"])
        all_spans = from_file_exporter_dicts(read_spans(str(SPAN_FILE)))
        my_spans = [s for s in all_spans if s.trace_id == trace_id]
        by_id = {s.span_id: s for s in my_spans}
        evaluation = evaluate_spans(my_spans, TraceLoader(ContentStore(root=CONTENT_ROOT)))
        queue.put_nowait({
            "type": "final",
            "trace_id": trace_id,
            "result": result,
            "span_count": len(my_spans),
            "spans": [_serialize_span(s, by_id) for s in sorted(my_spans, key=lambda s: s.start_time_unix_nano)],
            "findings": [dataclasses.asdict(f) for f in evaluation.findings],
            "candidates": [dataclasses.asdict(c) for c in evaluation.candidates],
        })
    except Exception as e:  # noqa: BLE001 - surfaced to the client, not swallowed
        queue.put_nowait({"type": "run_error", "message": str(e)})
    finally:
        queue.put_nowait({"type": "__done__"})


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(REPO_ROOT / "index.html")


@app.get("/api/scenarios")
async def scenarios() -> list[dict[str, str]]:
    infos = [
        {"name": s.name, "failure_class": s.failure_class, "description": DESCRIPTIONS[s.name]}
        for s in SCENARIOS
    ]
    infos.append({
        "name": MIXED_BOUNDARY_NAME,
        "failure_class": "clean",
        "description": DESCRIPTIONS[MIXED_BOUNDARY_NAME],
    })
    return infos


@app.get("/api/run_stream")
async def run_stream(name: str) -> StreamingResponse:
    valid = {s.name for s in SCENARIOS} | {MIXED_BOUNDARY_NAME}

    async def gen():
        if name not in valid:
            yield f"data: {json.dumps({'type': 'run_error', 'message': f'unknown scenario {name!r}'})}\n\n"
            yield f"data: {json.dumps({'type': '__done__'})}\n\n"
            return
        async with _run_lock:
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            task = asyncio.create_task(_execute_and_stream(name, queue))
            try:
                while True:
                    item = await queue.get()
                    if item["type"] == "__done__":
                        break
                    yield f"data: {json.dumps(item)}\n\n"
            finally:
                await task

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="info")
