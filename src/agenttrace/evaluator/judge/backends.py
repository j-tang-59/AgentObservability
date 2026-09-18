"""Judge backends: a deterministic FakeJudgeBackend for $0 CI (mirrors
harness.fake_model.FakeChatModel), and a real Anthropic-backed judge for
production use, opt-in and budget-capped.

The Anthropic backend's calls run under their own TracerProvider
(service.name=agenttrace-evaluator), never the harness's — per D3/E6, the
judge's own model calls must never be captured into the run being judged.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from opentelemetry.trace import SpanKind, Tracer

from agenttrace.telemetry.setup import tracer

ScriptFn = Callable[[str, str], tuple[str, str, list[str], str]]
"""(system, user) -> (verdict, confidence_str, evidence_span_ids, explanation)"""


class FakeJudgeBackend:
    """Deterministic: script(system, user) -> verdict tuple. No network."""

    model_name = "fake-judge-v1"

    def __init__(self, script: ScriptFn) -> None:
        self._script = script

    async def complete(self, *, system: str, user: str) -> tuple[str, int, int]:
        verdict, confidence, evidence_span_ids, explanation = self._script(system, user)
        body = {
            "verdict": verdict,
            "confidence": confidence,
            "evidence_span_ids": evidence_span_ids,
            "explanation": explanation,
        }
        text = json.dumps(body)
        return text, len(user) // 4, len(text) // 4


class BudgetExceededError(Exception):
    pass


class AnthropicJudgeBackend:
    """Real judge backend. Requires ANTHROPIC_API_KEY. Enforces a hard
    global token budget (E6) — exceeding it raises rather than silently
    spending more. A per-run budget is the caller's responsibility (e.g.
    the evaluator service tracking cumulative tokens per trace_id and
    stopping early) since "one run" is a concept this stateless-per-process
    backend doesn't otherwise know about."""

    def __init__(
        self,
        *,
        model: str = "claude-sonnet-5",
        max_tokens: int = 512,
        global_token_budget: int = 200_000,
        judge_tracer: Tracer | None = None,
    ) -> None:
        import anthropic

        self.model_name = model
        self._client = anthropic.AsyncAnthropic()
        self._max_tokens = max_tokens
        self._global_token_budget = global_token_budget
        self._tokens_used = 0
        # Separate TracerProvider/tracer for the judge's own model calls —
        # deliberately never wired into telemetry.hooks, so these calls
        # never attach to a harness run's trace. In production the
        # evaluator is its own process/service (E1) with its own
        # setup_tracing(service_name="agenttrace-evaluator") call, so the
        # process-global tracer() lookup is correct there; tests/calibration
        # running in-process with the harness pass judge_tracer explicitly
        # to avoid the "TracerProvider can only be set once per process"
        # collision (same reasoning as tests/property's local-provider
        # pattern).
        self._tracer = judge_tracer or tracer("agenttrace-evaluator")

    def tokens_used(self) -> int:
        return self._tokens_used

    async def complete(self, *, system: str, user: str) -> tuple[str, int, int]:
        if self._tokens_used >= self._global_token_budget:
            raise BudgetExceededError(f"judge global token budget of {self._global_token_budget} exhausted")

        with self._tracer.start_as_current_span("judge.chat", kind=SpanKind.CLIENT):
            resp = await self._client.messages.create(
                model=self.model_name,
                max_tokens=self._max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        text = "".join(block.text for block in resp.content if hasattr(block, "text"))
        input_tokens = resp.usage.input_tokens
        output_tokens = resp.usage.output_tokens
        self._tokens_used += input_tokens + output_tokens
        return text, input_tokens, output_tokens
