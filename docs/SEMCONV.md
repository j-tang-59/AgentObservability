# Semantic conventions mapping

Pinned to OTel semantic-conventions **v1.41.0** (GenAI). Every name below is
defined exactly once, in `src/agenttrace/telemetry/semconv.py`; a golden
test (`tests/golden/test_no_stray_semconv_literals.py`) fails the build if a
`gen_ai.*` or `agenttrace.*` string literal appears anywhere else in `src/`.

`OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` is set by
`telemetry/setup.py::setup_tracing` before any tracer is created.

## Span mapping

| Harness concept | Span name | Kind |
|---|---|---|
| Graph run | `invoke_workflow {name}` | INTERNAL |
| Agent (in-process/subprocess) | `invoke_agent {gen_ai.agent.name}` | INTERNAL |
| Parent calling an HTTP agent | `invoke_agent {gen_ai.agent.name}` | CLIENT |
| Tool call | `execute_tool {gen_ai.tool.name}` | INTERNAL |
| Model call | `chat {gen_ai.request.model}` | CLIENT |

## Attributes

| Attribute | Where set | Purpose |
|---|---|---|
| `gen_ai.operation.name` | every topology span | operation discriminator |
| `gen_ai.agent.name`, `agenttrace.agent.role`, `agenttrace.agent.depth` | `invoke_agent` | agent identity |
| `agenttrace.spawn.id` | `invoke_agent` (child) + `execute_tool spawn_subagent` (parent) | D6 spawn-record linkage |
| `agenttrace.spawn.outcome` | `execute_tool spawn_subagent` | returned / timeout / killed / error |
| `agenttrace.spawn.depth`, `agenttrace.spawn.runner`, `agenttrace.spawn.objective_sha256`(+`_ref`) | `spawn_intent` event | D6 spawn intent |
| `gen_ai.tool.name` | `execute_tool` | tool identity |
| `agenttrace.tool.args_sha256`, `agenttrace.tool.result_bytes`, (+`_ref`s) | `execute_tool` | D8 content refs, never raw payloads |
| `agenttrace.taint` | `execute_tool` | `trusted` \| `untrusted`, set at capture time |
| `gen_ai.tool.definitions` | `chat` | simplified tool list, for structural-hallucination detection |
| `gen_ai.usage.{input,output}_tokens` | `chat` | cost roll-up |
| `agenttrace.content.sha256`(+`agenttrace.agent.result_ref`) | `invoke_agent` | final result content ref |
| `gen_ai.evaluation.name/score.value/score.label/explanation` | `gen_ai.evaluation.result` log event | E7 verdict |
| `agenttrace.evaluator.type`, `agenttrace.evaluator.model` | same log event | verdict provenance (no standard field for this yet) |

## Baggage (allowlisted to run_id + depth + spawn.id only — never payload content)

`agenttrace.run_id`, `agenttrace.depth`, `agenttrace.spawn.id` (reuses the
same attribute name as the span-level `agenttrace.spawn.id`, deliberately —
see `telemetry/otel_hooks.py`).
