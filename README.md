# AgentTrace

Recursive multi-agent harness observability: every run — across in-process,
subprocess, and HTTP subagents — reconstructs into exactly one trace, plus an
out-of-band evaluator that detects agent failure classes via causal trace
analysis.

## Tech stack

- **Language / runtime:** Python 3.12, managed with `uv`
- **Agent harness:** LangGraph, with in-process / subprocess / HTTP runners
- **Tracing:** OpenTelemetry (API, SDK, OTLP/gRPC exporter), GenAI semantic
  conventions
- **Observability backend:** OpenTelemetry Collector, Grafana Tempo (traces),
  Loki (logs), Prometheus (metrics), Grafana — all via Docker Compose
- **HTTP layer:** FastAPI + Uvicorn (agent server), httpx (client)
- **Evaluator:** deterministic detectors + LLM-as-judge (Anthropic API,
  optional), graph analysis via `networkx`
- **Data/validation:** Pydantic
- **Testing:** pytest, Hypothesis (property-based), mypy, ruff
- **Demo:** a static HTML/CSS/JS client-side simulation (`index.html`), plus
  an optional local FastAPI backend (`demo_server.py`) that runs the real
  harness for a live version of the demo

## Quickstart

```bash
uv sync
uv run pytest -q       # full test suite, $0, deterministic
make up                # docker-compose: Collector, Tempo, Loki, Prometheus, Grafana
make demo               # one mixed-runner run + verify.py against real Tempo
make down
```

See `docs/DESIGN.md`, `docs/RESULTS.md`, and `docs/DEPENDENCIES.md` for the
full design/decision log and measured results.
