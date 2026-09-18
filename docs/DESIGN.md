# Design decision log (as built)

This records each design decision (D1–D11 for Part A, E1–E8 for Part B) as
actually implemented, with the file that owns it and any deviation from the
original spec. See `README.md` for the boundary/detector tables and
`docs/RESULTS.md` for measured numbers.

## Part A

- **D1 — parent/child, not links.** `harness/graph.py::run_agent`; every
  spawn is `await`ed by its spawner.
- **D2 — in-process propagation.** `asyncio.create_task` in
  `graph.py::call_tools` (fan-out), never a raw thread pool — guarded by
  `tests/golden/test_no_raw_threads_in_spawn_paths.py`.
- **D3 — span ownership split.** Topology spans (`workflow_span`,
  `agent_span`, `tool_span`) and leaf `chat` spans are all hand-rolled in
  `telemetry/otel_hooks.py`, behind the `telemetry/hooks.py` Protocol the
  harness calls instead of importing OTel directly. See
  `docs/DEPENDENCIES.md` for why no external GenAI callback instrumentor is
  used, and `tests/golden/test_golden_trace.py`'s manual-span-nesting test
  for the guard the design calls for.
- **D4 — subprocess env-var carrier.** `telemetry/propagation.py`
  (`env_updates_for_subprocess`, `extract_context_from_env` with
  `strict=True` by default) + `harness/runners/subprocess.py` (injects from
  inside the spawn span; `worker.py` extracts before any span opens, raises
  `MissingTraceparentError` if absent, flushes in `finally`). SIGKILL on
  timeout is reported as `spawn.outcome=killed`, distinct from a soft
  `timeout`, because only the runner that actually kills a child knows the
  difference (`run_context.py::ChildKilledError`).
- **D5 — HTTP propagation.** `harness/runners/http.py` opens a CLIENT-kind
  `invoke_agent` span and injects W3C headers from inside it;
  `harness/server.py` only extracts context from a request that already
  passed the bearer-token check.
- **D6 — two-phase spawn records.** `telemetry/otel_hooks.py::spawn_intent`
  / `spawn_outcome`; `verify.py` and `evaluator/detectors/orphans.py` both
  key off these, independently of parentage reconstruction.
- **D7 — export path.** `telemetry/setup.py::CountingBatchSpanProcessor`
  wraps the SDK's internal drop-counting metric (`_metrics.drop_items`)
  rather than reimplementing queue-fullness detection, since the SDK
  version in use restructured `BatchSpanProcessor` internals around a
  private `BatchProcessor` class between when the design was written and
  when this was built — see the docstring there for the exact mechanism.
  `deploy/otelcol.yaml` derives span metrics via the Collector's
  `spanmetrics` connector, not Tempo's metrics-generator (never both).
- **D8 — content store.** `telemetry/content_store.py`, a local
  sha256-keyed filesystem store behind `agenttrace.content.ref`; never in
  span attributes. `deploy/tempo.yaml` raises `trace_idle_period` to 30s.
- **D9 — semconv v1.41.0.** `telemetry/semconv.py`, `docs/SEMCONV.md`.
- **D10 — sampling.** `ParentBased(ALWAYS_ON)`, `telemetry/setup.py`.
- **D11 — evaluator inputs.** Spawn records, tool taint/content refs, chat
  tool-definitions, agent result refs — see `docs/SEMCONV.md`'s attribute
  table for the exact list, all emitted by Part A so Part B never
  re-instruments anything.

## Part B

- **E1 — out-of-band batch evaluation.** `evaluator/service.py::evaluate_spans`
  runs against a completed trace's spans; nothing in the harness calls into
  the evaluator.
- **E2 — completeness.** `evaluator/completeness.py`: structural check
  (root ended, every spawn_intent has an outcome) plus `poll_until_complete`
  for a live-Tempo quiescence loop. The fault suite's offline runs are
  complete by construction (the harness run already finished before spans
  are read back), so the structural check is what's exercised by tests;
  the polling loop is written for a real deployment.
- **E3 — content access.** `evaluator/loader.py::TraceLoader.resolve_ref`;
  hash/size-only detectors (retry storm, structural hallucination, orphans)
  never call it.
- **E4 — causal graph.** `evaluator/graph.py::build_causal_trace`. Control
  edges from span parentage; data edges from chunk-level hashing
  (sliding/stride-1 word windows — see the module docstring for why a fixed
  stride is wrong) of tool args/result content, ordered by span **end**
  time so a `spawn_subagent` span (whose "result" is only known once its
  child returns) correctly comes out as a data consumer of its own child's
  `submit_result`, not the reverse.
- **E5 — deterministic detectors.** `evaluator/detectors/`; see README's
  detector table. Silent-tool-failure specifically separates "explicit
  failure pattern" (confirmed `Finding`) from "empty/short result, no
  pattern match" (`Candidate` only) — an earlier version conflated these
  and over-flagged legitimately-empty results as confirmed findings, caught
  by the precision/recall gate test.
- **E6 — judge.** `evaluator/judge/`. Backend is pluggable
  (`FakeJudgeBackend` / `AnthropicJudgeBackend`, mirroring the harness's own
  `FakeChatModel` pattern). Excerpt built only from candidate-connected
  spans (`judge.py::build_excerpt`); a verdict citing a span ID outside the
  excerpt's evidence set is rejected as an abstention, never trusted.
- **E7 — verdict emission.** `evaluator/emit.py`:
  `gen_ai.evaluation.result` log events via a `LoggerProvider`, plus
  `agenttrace_findings_total` (Prometheus). Never a synthetic span grafted
  into the original trace.
- **E8 — ground truth.** `faults/scenarios.py` + `bench/suite.py`. See
  `docs/RESULTS.md` for the honesty caveat on synthetic vs. real-model
  tiers — only the synthetic tier is exercised in this build.
