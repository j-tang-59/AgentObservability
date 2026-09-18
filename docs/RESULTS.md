# Results

All numbers below were produced by actually running the harness, the
evaluator, and (for the coverage/bytes-per-trace numbers) the real
docker-compose pipeline — none are estimated. Reproduce with:

```
uv run python -m agenttrace.bench.report --coverage-runs 20 --overhead-runs 8 --eval-reps 10
```

for everything except the live-Tempo bytes/trace number, which requires
`make up` and is described separately below.

**Scope note.** The design target is >=200 mixed-runner runs for the
coverage benchmark and a ~2,000-span shape. This report's defaults are
smaller (20 coverage runs, a 156-agent/905-span shape) so it finishes in a
few seconds instead of the docker-and-many-subprocesses time a 200-run,
2000-span sweep would take. Every number is still a real measurement, not a
projection — raising `--coverage-runs` reproduces the full-size version with
the exact same code path. The same scoping applies to Phase 4/6's synthetic
suite (10 reps/scenario here vs. >=300 in the design target) and to the
LLM-judge numbers (FakeJudgeBackend only — no `ANTHROPIC_API_KEY` was
available in this environment; see the Evaluation section below).

## Trace reconstruction (Part A)

### Span shapes

| Shape | Fan-out | Depth | Expected agents | Measured spans | Spans/agent |
|---|---|---|---|---|---|
| Standard benchmark shape | 3 | 3 | 40 | 227 | 5.67 |
| Larger shape | 5 | 3 | 156 | 905 | 5.80 |

(The design doc's illustrative "~12–15 spans/agent" assumed a chat span per
tool-calling round plus tool spans; this harness's actual per-agent span
count — one `chat` span, N `execute_tool` spans, one `invoke_agent` span —
comes out closer to 5–6/agent for a 2-round agent, which is what's measured
above.)

### Coverage (D6/verify.py), 20 mixed-runner runs

Each run cycles through `[inproc]`, `[inproc, subprocess]`, `[subprocess]`
runner plans at fan-out 2 / depth 2.

| Metric | Result |
|---|---|
| Runs fully OK (coverage=1.0, 1 root, 0 missing parents, 0 drops) | 20/20 |
| Runs with root count != 1 | 0 |
| Total missing-parent spans across all runs | 0 |
| Total SDK-queue drops across all runs | 0 |
| Average spawn coverage | 1.0 (100%) |

### Live pipeline: one 40-agent, 4-runner-type trace, real Tempo

Run via `make demo`-equivalent (fan-out 3, depth 3, runner plan
`inproc,subprocess,http,inproc`), verified against a real Tempo instance
(`docker compose up`, then `agenttrace.verify`):

```json
{
  "trace_id_uniform": true,
  "root_count": 1,
  "missing_parent_count": 0,
  "spawn_intents": 39,
  "spawn_matched": 39,
  "coverage": 1.0,
  "sdk_drops": null,
  "ok": true
}
```

39/39 spawns correctly matched and parented — including HTTP-routed hops
that legitimately produce two `invoke_agent` spans per logical agent (a
CLIENT-kind wrapper on the caller's side, an INTERNAL span on the server
side, per D5) without breaking the 1-root/0-missing-parent invariant.

Trace size, as returned by Tempo's `/api/traces/{id}` OTLP-JSON query API
for that same 236-span trace: **229,960 bytes (~975 bytes/span)**. This is
the query-response JSON size, not Tempo's internal columnar storage size —
a reasonable proxy for "how big is a trace," and importantly it stays small
regardless of whether content capture is on, because payloads never touch
span attributes (D8) — only sha256 refs do.

### Instrumentation overhead (paired on/off, fixed-latency FakeChatModel)

Fan-out 3 / depth 2 shape (13 agents, ~74 spans), 8 paired runs:

| Metric | Value |
|---|---|
| Avg wall-clock, telemetry off | 32.4 ms |
| Avg wall-clock, telemetry on | 38.4 ms |
| Overhead | 18.4% |
| Per-span cost | ~80.7 µs/span |

This is measured against a near-zero-latency FakeChatModel, so the
*percentage* overhead is an upper bound — against any real model call
(tens to hundreds of ms), the same ~81µs/span in absolute terms becomes a
negligible fraction of wall-clock time. The absolute per-span cost is the
number that generalizes.

## Evaluation (Part B)

70 labeled synthetic runs (7 scenarios x 10 reps: clean, retry_storm,
silent_tool_failure, structural tool hallucination, fabricated tool result,
orphaned subagent, prompt injection).

### Deterministic detectors

| Class | N | Precision | Recall |
|---|---|---|---|
| retry_storm | 10 | 1.00 | 1.00 |
| silent_tool_failure | 10 | 1.00 | 1.00 |
| tool_hallucination_structural | 10 | 1.00 | 1.00 |
| orphaned_subagent | 10 | 1.00 | 1.00 |

**Clean-run false-positive rate: 0/10 (0.0%).**

Getting to 100%/0% took two real bug fixes surfaced by this exact
measurement (see Phase 5's commit): a fixed-stride chunk hasher that missed
verbatim overlaps not landing on the same word boundary, and a `clean`
scenario objective whose single-letter search query incidentally matched
the untrusted corpus.

### Headline metric

Runs with a detected failure whose harness-level outcome showed no
obvious error signal (no "error"/"traceback" substring in the returned
result, i.e. the kind of run a human skimming output would call
successful): **40 of 60 non-clean labeled runs (66.7%)**. This is the
thesis of Part B: even in a fully synthetic suite where every failure is
deliberately injected, a majority of them are invisible without trace-level
analysis.

### LLM-as-judge

**No `ANTHROPIC_API_KEY` was available in this environment.** The judge
pipeline (excerpt building, prompting, strict-JSON parsing/validation,
scoring) is fully implemented and exercised end-to-end against
`FakeJudgeBackend` — a deterministic scripted backend, the same pattern as
the harness's `FakeChatModel` — which proves the *plumbing* (evidence-ID
validation rejects hallucinated citations, `uncertain` is never forced into
yes/no, position-sensitivity shuffling is wired up) but says nothing about
real judgment quality. The numbers below are FakeJudgeBackend numbers,
reported as exactly that — they are not a substitute for real-model
calibration:

| Metric | Value (FakeJudgeBackend) |
|---|---|
| Verdicts scored | 20 |
| Abstention rate | 0.0% |
| Avg tokens/verdict (synthetic char-count proxy) | 409.5 |
| Evaluation latency (deterministic detectors, per trace) | 0.25 ms avg |

**What's missing relative to the design's E6/E8 targets, and why:**
precision/recall/F1 against a *real* model's injection/fabrication behavior,
Cohen's kappa against a second judge model, and human-label agreement on a
real-model tier. All three need either a paid API key or manually labeled
real-model runs, neither available in this build. `evaluator/judge/backends.py`'s
`AnthropicJudgeBackend` is fully implemented, type-checked against the
installed SDK, and takes a `judge_tracer` override for the exact same
same-process-testing reasons documented in `tests/property/`; wiring in a
real key and real-model scenarios (e.g. AgentDojo/InjecAgent-style, per the
design doc) is the natural next step and requires no code changes to the
scoring/calibration layer.
