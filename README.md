# AgentTrace

Recursive multi-agent harness observability: every run — across in-process,
subprocess, and HTTP subagents — reconstructs into exactly **one trace**,
with propagation coverage proven by code (Part A), plus an out-of-band
evaluator that detects agent failure classes via causal trace analysis
(Part B).

See `docs/RESULTS.md` for real measured numbers, `docs/DEPENDENCIES.md` for
a deliberate deviation from the design spec (no external GenAI callback
instrumentor), and `docs/DESIGN.md` for the D1–D11/E1–E8 decision log as
built.

## Architecture

```
 LangGraph harness (parent process)
 ┌──────────────────────────────────────────────────────────────────┐
 │ invoke_workflow harness                                          │
 │  └ invoke_agent orchestrator/researcher/verifier                 │
 │     ├ chat {model}                                               │
 │     └ execute_tool spawn_subagent ──┬─ InprocRunner   (contextvars)
 │                                     ├─ SubprocessRunner (TRACEPARENT env)
 │                                     └─ HttpRunner     (traceparent header)
 │ TracerProvider(ParentBased, schema v1.41.0) · ContentStore (hash → blob)
 │ BatchSpanProcessor → OTLP/gRPC → Collector                       │
 └──────────────────────────────────────────────────────────────────┘
        │ children export to the same Collector
        ▼
 OTel Collector ── traces ──► Tempo ◄──── fetch by trace ID ──── Evaluator (Part B)
      │          ── logs ────► Loki ◄──── gen_ai.evaluation.result ──┤
      │          ── spanmetrics ─► Prometheus ◄── findings counters ─┘
      ▼
 Grafana: trace waterfall · trace→logs (verdicts on spans) · dashboards
```

## Quickstart

```bash
uv sync
uv run pytest -q                 # full test suite, $0, deterministic
make up                          # docker-compose: Collector, Tempo, Loki, Prometheus, Grafana
make demo                        # one mixed-runner run + verify.py against real Tempo
python -m agenttrace.bench.suite --reps 10     # labeled fault-injection suite
python -m agenttrace.bench.report              # full benchmark report -> docs/RESULTS.md's numbers
make down
```

Grafana: http://localhost:3001 · Tempo: http://localhost:3200 · Prometheus: http://localhost:9090

## The boundary table (where unification breaks, and what catches it)

| Boundary | How context crosses | How it silently breaks | Test that catches it |
|---|---|---|---|
| Same task/coroutine | `contextvars`, automatic | N/A as long as spans use `start_as_current_span` | Golden trace tests |
| Threads/executors | Must be copied explicitly | Raw `ThreadPoolExecutor`/`Thread`/`run_in_executor` don't copy context | `tests/golden/test_no_raw_threads_in_spawn_paths.py` (regex guard over `src/agenttrace/harness`) |
| Callback vs. contextvar spans | N/A — see `docs/DEPENDENCIES.md` | A manually opened span could attach to the wrong parent if two span-creation mechanisms disagree on "current" | `tests/golden/test_golden_trace.py::test_manual_span_nests_under_its_agent_span` |
| Subprocess | `TRACEPARENT`/`TRACESTATE`/`BAGGAGE` env vars | Missing/stale injection, child killed before flush | `tests/property/test_propagation_coverage.py` (real subprocess spawns via Hypothesis) |
| HTTP | W3C `traceparent` header | Uninstrumented client/server, unauthenticated header injection | Same property test, real HTTP calls; `server.py` only extracts context from authenticated callers |
| Export/storage | n/a | SDK queue overflow, Tempo's per-trace byte cap, late spans | `CountingBatchSpanProcessor.queue_drops`; `evaluator/completeness.py`'s quiescence check |

## The detector table

| Class | Signal | Detector type |
|---|---|---|
| Orphaned subagent | `spawn.outcome` timeout/killed/error vs. instrumentation-miss (returned, no child span) | Deterministic |
| Retry storm | Repeated `(agent, tool, args_sha256)`; repeated spawn objectives; `GraphRecursionError` | Deterministic |
| Silent tool failure | OK-status failure-shaped result flowing into a later tool call's args via a data edge, no retry | Deterministic (explicit pattern) + Candidate (ambiguous/empty result) |
| Tool hallucination, structural | Undeclared tool name vs. `gen_ai.tool.definitions` | Deterministic |
| Tool hallucination, fabricated | Result text mentions a tool with no matching span in the subtree | Candidate → judge |
| Prompt injection | Tainted data-edge into a later tool call's args | Candidate → judge |

Every detector is measured against real generated traces, not just unit
tests on hand-built spans — see `docs/RESULTS.md`.

## Design tradeoffs actually made

- **D1 (topology):** parent/child spans, not links — subagents here are
  awaited by their spawner, which is containment, not detached causality.
- **D3 (span ownership) deviation:** `chat` spans are hand-rolled rather
  than emitted by an external GenAI callback instrumentor. See
  `docs/DEPENDENCIES.md` for the full reasoning (a very recent semconv
  version, a `FakeChatModel` that isn't LangChain's real callback surface,
  and keeping every name pinned to one file that a golden test guards).
- **D4/D5 (propagation):** environment variables for subprocess, W3C
  headers for HTTP, both injected from inside the `execute_tool
  spawn_subagent` span so parentage is exact. Strict mode: a subprocess
  worker started without `TRACEPARENT` raises rather than silently
  becoming a new root.
- **D6 (coverage):** two-phase spawn records (`spawn_intent` event +
  `spawn.outcome` attribute) are the ground truth `verify.py` and the
  orphan detector both key off — independent of whether parentage
  reconstruction itself is correct.
- **D8 (content):** nothing goes in span attributes; a local sha256-keyed
  filesystem store holds payloads behind `agenttrace.content.ref`, kept
  off by default and on only in bench mode.
- **D10 (sampling):** `ParentBased(ALWAYS_ON)` everywhere; tail sampling is
  out of scope (needs the whole trace at one Collector and a decision
  window longer than the run).
- **E1 (evaluation timing):** out-of-band batch evaluation, never inline —
  a separate concern from guardrails, which would be the natural follow-on
  for *prevention* rather than *observability*.
- **E4 (data edges):** chunk-level hashing (sliding word windows, not
  fixed-stride — a first version got this wrong and a real generated trace
  caught it), never embedding similarity. This catches every verbatim flow
  our harness actually produces and deliberately can't see an LLM
  paraphrasing content in its own words — that gap is left to the judge.
- **E7 (verdict storage):** `gen_ai.evaluation.result` log events, not
  synthetic spans grafted into the trace — the observed trace stays
  immutable.

## Known gaps (honest, not glossed over)

- **Real-model judge calibration.** No `ANTHROPIC_API_KEY` in this
  environment — see `docs/RESULTS.md`'s Evaluation section for exactly
  what's implemented vs. exercised.
- **Real-model tier for the fault suite (E8).** All labeled runs here are
  synthetic (`FakeChatModel`-scripted); a real-model tier against
  AgentDojo/InjecAgent-style scenarios is designed for but not run.
- **Scale.** The coverage benchmark, synthetic suite, and 2,000-span shape
  are all run at a smaller N than the design's targets, documented
  explicitly in `docs/RESULTS.md`, with the same code path scaling up via
  a CLI flag.
- **Second-judge-model kappa and human-label agreement**: not implemented
  against live data (need a second paid model / manual labeling); the
  scoring functions in `evaluator/judge/calibration.py` accept whatever
  verdicts/labels are supplied, so this is a call-site change, not a
  rewrite.
- **Budget enforcement across process boundaries.** `RunContext.max_agents`
  is a best-effort, per-process-branch safety guard, not a source of
  truth — grandchildren spawned across a subprocess/HTTP boundary get
  their own `RunContext` instance and don't increment the parent's
  in-memory counter. Coverage (`verify.py`, D6) is the actual source of
  truth for "nothing was lost," not this counter.

## Prior art and standards this builds on

- OTel semantic-conventions v1.41.0 (GenAI spans, `gen_ai.evaluation.result`)
- `open-telemetry/semantic-conventions-genai` (successor repo, still in
  Development as of this build)
- `open-telemetry/opentelemetry-python-genai`, Traceloop's OpenLLMetry
  (evaluated per D3; not used — see `docs/DEPENDENCIES.md`)
- OpenInference/Phoenix (a comparable GenAI observability convention)
- Grafana Tempo/Loki/Prometheus, the OTel Collector's `spanmetrics` connector
- AgentDojo, InjecAgent (referenced designs for a real-model injection
  benchmark tier, not yet run here)

**Gaps in the underlying standards this project ran into:** no standard
format for spawn/lineage edges between agent spans (this project's
`agenttrace.spawn.*` attributes are a local convention); no scorer
provenance field on `gen_ai.evaluation.result` (hence
`agenttrace.evaluator.type`/`agenttrace.evaluator.model` as local
additions); no standard reference format for externally stored content
(hence the `sha256:<hex>` convention in `telemetry/content_store.py`).
