"""`python -m agenttrace.verify --trace-id <hex>` (D6, Phase 3 gate).

Fetches a completed trace from Tempo by trace ID and proves reconstruction:
trace-ID uniformity, root count, missing-parent count, and spawn coverage
(children correctly attached / children spawned, from D6 spawn records).
SDK-level dropped-span counts are process-local (the exporting process's own
CountingBatchSpanProcessor) and can't be recovered from Tempo after the
fact — callers that need that number pass it in explicitly (demo.py prints
it; a real pipeline would ship it as a metric instead).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any

import httpx

from agenttrace.telemetry import semconv
from agenttrace.trace_model import SpanRecord, parse_tempo_trace

__all__ = ["SpanRecord", "VerifyReport", "fetch_trace", "main", "parse_tempo_trace", "verify_spans", "verify_trace_id"]


@dataclass
class VerifyReport:
    trace_ids: set[str]
    root_count: int
    root_names: list[str]
    missing_parent_span_ids: list[str]
    spawn_intents: int
    spawn_matched: int
    spawn_mismatches: list[str] = field(default_factory=list)
    sdk_drops: int | None = None

    @property
    def coverage(self) -> float:
        if self.spawn_intents == 0:
            return 1.0
        return self.spawn_matched / self.spawn_intents

    @property
    def ok(self) -> bool:
        return (
            len(self.trace_ids) == 1
            and self.root_count == 1
            and not self.missing_parent_span_ids
            and self.coverage == 1.0
            and (self.sdk_drops is None or self.sdk_drops == 0)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id_uniform": len(self.trace_ids) == 1,
            "root_count": self.root_count,
            "root_names": self.root_names,
            "missing_parent_count": len(self.missing_parent_span_ids),
            "missing_parent_span_ids": self.missing_parent_span_ids,
            "spawn_intents": self.spawn_intents,
            "spawn_matched": self.spawn_matched,
            "coverage": self.coverage,
            "spawn_mismatches": self.spawn_mismatches,
            "sdk_drops": self.sdk_drops,
            "ok": self.ok,
        }


def verify_spans(spans: list[SpanRecord], *, sdk_drops: int | None = None) -> VerifyReport:
    trace_ids = {s.trace_id for s in spans}
    by_span_id = {s.span_id: s for s in spans}
    roots = [s for s in spans if not s.parent_span_id]
    missing_parents = [
        s.span_id for s in spans if s.parent_span_id and s.parent_span_id not in by_span_id
    ]

    tool_spans = [s for s in spans if s.name == semconv.tool_span_name("spawn_subagent")]
    agent_spans_by_spawn_id: dict[str, list[SpanRecord]] = {}
    for s in spans:
        if s.name.startswith(semconv.SPAN_INVOKE_AGENT) and semconv.AT_SPAWN_ID in s.attributes:
            agent_spans_by_spawn_id.setdefault(s.attributes[semconv.AT_SPAWN_ID], []).append(s)

    spawn_intents = 0
    spawn_matched = 0
    mismatches: list[str] = []
    for tool_span in tool_spans:
        intent_events = [e for e in tool_span.events if e["name"] == semconv.EVENT_SPAWN_INTENT]
        for event in intent_events:
            spawn_intents += 1
            spawn_id = event["attributes"].get(semconv.AT_SPAWN_ID)
            outcome = tool_span.attributes.get(semconv.AT_SPAWN_OUTCOME)
            matches = agent_spans_by_spawn_id.get(spawn_id, [])
            correctly_parented = [m for m in matches if m.parent_span_id == tool_span.span_id]
            if outcome == "returned" and len(correctly_parented) >= 1:
                spawn_matched += 1
            else:
                mismatches.append(
                    f"spawn_id={spawn_id} outcome={outcome} matches={len(matches)} "
                    f"correctly_parented={len(correctly_parented)}"
                )

    return VerifyReport(
        trace_ids=trace_ids,
        root_count=len(roots),
        root_names=[r.name for r in roots],
        missing_parent_span_ids=missing_parents,
        spawn_intents=spawn_intents,
        spawn_matched=spawn_matched,
        spawn_mismatches=mismatches,
        sdk_drops=sdk_drops,
    )


def fetch_trace(tempo_url: str, trace_id: str) -> dict[str, Any]:
    resp = httpx.get(f"{tempo_url}/api/traces/{trace_id}", timeout=10.0)
    resp.raise_for_status()
    return dict(resp.json())


def verify_trace_id(tempo_url: str, trace_id: str, *, sdk_drops: int | None = None) -> VerifyReport:
    trace_json = fetch_trace(tempo_url, trace_id)
    spans = parse_tempo_trace(trace_json)
    return verify_spans(spans, sdk_drops=sdk_drops)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-id", required=True)
    parser.add_argument("--tempo-url", default="http://localhost:3200")
    parser.add_argument("--sdk-drops", type=int, default=None)
    args = parser.parse_args()

    report = verify_trace_id(args.tempo_url, args.trace_id, sdk_drops=args.sdk_drops)
    print(json.dumps(report.to_dict(), indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
