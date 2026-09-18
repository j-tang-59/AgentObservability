"""Real OTel-backed implementation of telemetry.hooks.HarnessHooks (D3, D6, D9, D11).

Span ownership split (D3): topology spans (workflow/agent/tool) are all
hand-rolled here with start_as_current_span; the leaf `chat` span is also
opened here (this project's own first-party callback, not an external
GenAI instrumentation package — see DEPENDENCIES.md for why).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import baggage, trace
from opentelemetry import context as otel_context
from opentelemetry.trace import SpanKind, Status, StatusCode

from agenttrace.telemetry import semconv
from agenttrace.telemetry.content_store import ContentStore, get_default_store, sha256_of
from agenttrace.telemetry.hooks import HarnessHooks

BAGGAGE_SPAWN_ID = semconv.AT_SPAWN_ID

_KIND_MAP = {"INTERNAL": SpanKind.INTERNAL, "CLIENT": SpanKind.CLIENT, "SERVER": SpanKind.SERVER}


class OtelHooks(HarnessHooks):
    def __init__(
        self,
        *,
        tracer: trace.Tracer,
        content_store: ContentStore | None = None,
        capture_content: bool = False,
    ) -> None:
        self._tracer = tracer
        self._store = content_store or get_default_store()
        self._capture = capture_content
        self._spawn_ctx_tokens: dict[str, object] = {}

    def _maybe_store(self, text: str) -> tuple[str | None, str]:
        digest = sha256_of(text)
        if self._capture:
            ref, _ = self._store.put(text)
            return ref, digest
        return None, digest

    @contextmanager
    def workflow_span(self, span_name: str) -> Iterator[None]:
        with self._tracer.start_as_current_span(span_name, kind=SpanKind.INTERNAL):
            yield

    @contextmanager
    def agent_span(
        self, span_name: str, *, agent_name: str, kind: str, role: str, depth: int
    ) -> Iterator[None]:
        with self._tracer.start_as_current_span(span_name, kind=_KIND_MAP[kind]) as span:
            span.set_attribute(semconv.GEN_AI_OPERATION_NAME, semconv.SPAN_INVOKE_AGENT)
            span.set_attribute(semconv.GEN_AI_AGENT_NAME, agent_name)
            span.set_attribute(semconv.AT_AGENT_ROLE, role)
            span.set_attribute(semconv.AT_AGENT_DEPTH, depth)
            spawn_id = baggage.get_baggage(BAGGAGE_SPAWN_ID)
            if spawn_id:
                span.set_attribute(semconv.AT_SPAWN_ID, str(spawn_id))
            yield

    @contextmanager
    def tool_span(self, span_name: str, *, tool_name: str) -> Iterator[None]:
        with self._tracer.start_as_current_span(span_name, kind=SpanKind.INTERNAL) as span:
            span.set_attribute(semconv.GEN_AI_OPERATION_NAME, semconv.SPAN_EXECUTE_TOOL)
            span.set_attribute(semconv.GEN_AI_TOOL_NAME, tool_name)
            yield

    @contextmanager
    def chat_span(self, span_name: str, *, model: str) -> Iterator[None]:
        with self._tracer.start_as_current_span(span_name, kind=SpanKind.CLIENT) as span:
            span.set_attribute(semconv.GEN_AI_OPERATION_NAME, semconv.SPAN_CHAT)
            span.set_attribute(semconv.GEN_AI_REQUEST_MODEL, model)
            span.set_attribute(semconv.GEN_AI_RESPONSE_MODEL, model)
            span.set_attribute(semconv.GEN_AI_PROVIDER_NAME, "agenttrace-fake")
            yield

    def spawn_intent(self, *, role: str, objective: str, depth: int, runner_name: str) -> str:
        spawn_id = uuid.uuid4().hex
        span = trace.get_current_span()
        ref, digest = self._maybe_store(objective)
        attrs: dict[str, Any] = {
            semconv.AT_SPAWN_ID: spawn_id,
            semconv.AT_SPAWN_DEPTH: depth,
            semconv.AT_SPAWN_RUNNER: runner_name,
            semconv.AT_SPAWN_OBJECTIVE_SHA256: digest,
        }
        if ref:
            attrs[semconv.AT_SPAWN_OBJECTIVE_REF] = ref
        span.add_event(semconv.EVENT_SPAWN_INTENT, attrs)

        ctx = baggage.set_baggage(BAGGAGE_SPAWN_ID, spawn_id)
        token = otel_context.attach(ctx)
        self._spawn_ctx_tokens[spawn_id] = token
        return spawn_id

    def spawn_outcome(self, spawn_id: str, outcome: str) -> None:
        span = trace.get_current_span()
        span.set_attribute(semconv.AT_SPAWN_ID, spawn_id)
        span.set_attribute(semconv.AT_SPAWN_OUTCOME, outcome)
        token = self._spawn_ctx_tokens.pop(spawn_id, None)
        if token is not None:
            otel_context.detach(token)  # type: ignore[arg-type]

    def set_tool_result(
        self,
        *,
        args: dict[str, Any],
        result: str,
        taint: str,
        error_type: str | None = None,
    ) -> None:
        span = trace.get_current_span()
        args_str = json.dumps(args, sort_keys=True)
        span.set_attribute(semconv.AT_TOOL_ARGS_SHA256, sha256_of(args_str))
        span.set_attribute(semconv.AT_TOOL_RESULT_BYTES, len(result.encode()))
        span.set_attribute(semconv.AT_TAINT, taint)
        args_ref, _ = self._maybe_store(args_str)
        result_ref, _ = self._maybe_store(result)
        if args_ref:
            span.set_attribute(semconv.AT_TOOL_ARGS_REF, args_ref)
        if result_ref:
            span.set_attribute(semconv.AT_TOOL_RESULT_REF, result_ref)
        if error_type:
            span.set_attribute("error.type", error_type)
            span.set_status(Status(StatusCode.ERROR, error_type))

    def set_chat_attrs(
        self, *, tool_definitions: list[dict[str, Any]], input_tokens: int, output_tokens: int
    ) -> None:
        span = trace.get_current_span()
        simplified = [{"name": d["name"], "description": d.get("description", "")} for d in tool_definitions]
        span.set_attribute(semconv.GEN_AI_TOOL_DEFINITIONS, json.dumps(simplified, sort_keys=True))
        span.set_attribute(semconv.GEN_AI_USAGE_INPUT_TOKENS, input_tokens)
        span.set_attribute(semconv.GEN_AI_USAGE_OUTPUT_TOKENS, output_tokens)

    def set_agent_result(self, result: str) -> None:
        span = trace.get_current_span()
        ref, digest = self._maybe_store(result)
        span.set_attribute(semconv.AT_CONTENT_SHA256, digest)
        if ref:
            span.set_attribute(semconv.AT_AGENT_RESULT_REF, ref)

    def current_trace_id(self) -> str | None:
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if not ctx or not ctx.is_valid:
            return None
        return format(ctx.trace_id, "032x")
