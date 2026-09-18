# Dependencies

Pinned via `uv add`; exact resolved versions live in `uv.lock`. Key packages
as of this build (2026-09-11):

| Package | Version | Maintainer | Why |
|---|---|---|---|
| langgraph | 1.2.11 | LangChain Inc. | recursive agent graph (StateGraph) |
| langchain-core | 1.6.2 | LangChain Inc. | message/graph primitives |
| opentelemetry-api / -sdk | 1.44.0 | OpenTelemetry Authors (CNCF) | tracing |
| opentelemetry-exporter-otlp-proto-grpc | 1.44.0 | OpenTelemetry Authors | OTLP export to the Collector |
| opentelemetry-semantic-conventions | 0.65b0 | OpenTelemetry Authors | semconv constants (not used directly — see below) |
| fastapi / uvicorn | latest at add-time | Sebastián Ramírez / Encode | HTTP agent runner + server |
| httpx | latest at add-time | Encode | HTTP client for the HTTP runner |
| networkx | latest at add-time | NetworkX developers | evaluator causal graph (Part B) |
| anthropic | latest at add-time | Anthropic | LLM-as-judge (Part B) |
| hypothesis | latest at add-time | Hypothesis Works | property-based propagation tests |

## D3 deviation: no external GenAI callback instrumentor

The design doc (D3) asks us to evaluate `opentelemetry-instrumentation-genai-langchain`
(from `open-telemetry/opentelemetry-python-genai`) against Traceloop's
OpenLLMetry (`opentelemetry-instrumentation-langchain`, which added GenAI
agent-span naming in PR #3673), and use exactly one for leaf `chat` spans.

We deviated: **`chat` spans are hand-rolled in `telemetry/otel_hooks.py`**,
not emitted by an external callback instrumentor. Reasoning:

- The project targets GenAI semconv **v1.41.0**, dated April 28, 2026 — very
  recent relative to this build. Neither external package's compatibility
  with that exact schema version could be verified against a live PyPI
  release at build time (`opentelemetry-instrumentation-langchain` alone has
  190+ published versions from multiple near-identically-named packages;
  picking the wrong one silently breaks span naming).
- The harness uses a `FakeChatModel`, not LangChain's real `BaseChatModel`
  callback path, for deterministic $0 CI runs (a ground rule). Wiring a real
  LangChain callback handler to a fake model synthetic API surface would
  itself be a bespoke integration, undermining the reason to depend on the
  external package in the first place.
- Owning `chat` spans directly keeps every span name/kind/attribute pinned
  to `telemetry/semconv.py` and covered by the golden tests
  (`tests/golden/test_no_stray_semconv_literals.py`), with zero risk of a
  transitive dependency bump silently renaming a span.

This is a scope reduction from the original design, not a functional gap:
`chat` spans still carry `gen_ai.request.model`, `gen_ai.response.model`,
`gen_ai.provider.name`, `gen_ai.usage.{input,output}_tokens`, and
`gen_ai.tool.definitions`, matching what the callback instrumentors would
have produced. **No provider-SDK instrumentor is used anywhere** (per D3),
including for the evaluator's judge calls (E6): those run under their own
`TracerProvider` and are never captured into a harness run's trace.
