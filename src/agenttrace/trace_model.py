"""Shared in-memory span representation used by verify.py and the evaluator.

Both loaders (Tempo's OTLP-JSON query API, and the offline file exporter
used by the fault-injection suite) normalize into this same SpanRecord list,
so verify.py and evaluator/loader.py share one code path for everything
downstream of "list of spans in a trace."
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any


def b64_to_hex(value: str) -> str:
    return base64.b64decode(value).hex()


def _flatten_attrs(attrs: list[dict[str, Any]] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for attr in attrs or []:
        key = attr["key"]
        value_obj = attr.get("value", {})
        for vkey in ("stringValue", "intValue", "boolValue", "doubleValue"):
            if vkey in value_obj:
                out[key] = value_obj[vkey]
                break
    return out


@dataclass
class SpanRecord:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    kind: str
    attributes: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)
    status: str = "STATUS_CODE_UNSET"
    start_time_unix_nano: int = 0
    end_time_unix_nano: int = 0


def parse_tempo_trace(trace_json: dict[str, Any]) -> list[SpanRecord]:
    records: list[SpanRecord] = []
    for batch in trace_json.get("batches", []):
        for scope_spans in batch.get("scopeSpans", []):
            for sp in scope_spans.get("spans", []):
                events = [
                    {"name": e.get("name"), "attributes": _flatten_attrs(e.get("attributes"))}
                    for e in sp.get("events", [])
                ]
                records.append(
                    SpanRecord(
                        trace_id=b64_to_hex(sp["traceId"]),
                        span_id=b64_to_hex(sp["spanId"]),
                        parent_span_id=b64_to_hex(sp["parentSpanId"]) if sp.get("parentSpanId") else None,
                        name=sp["name"],
                        kind=sp.get("kind", ""),
                        attributes=_flatten_attrs(sp.get("attributes")),
                        events=events,
                        status=sp.get("status", {}).get("code", "STATUS_CODE_UNSET"),
                        start_time_unix_nano=int(sp.get("startTimeUnixNano", 0)),
                        end_time_unix_nano=int(sp.get("endTimeUnixNano", 0)),
                    )
                )
    return records


def from_file_exporter_dicts(spans: list[dict[str, Any]]) -> list[SpanRecord]:
    return [
        SpanRecord(
            trace_id=str(s["trace_id"]),
            span_id=str(s["span_id"]),
            parent_span_id=s.get("parent_span_id"),
            name=str(s["name"]),
            kind=str(s.get("kind", "")),
            attributes=dict(s.get("attributes", {})),
            events=list(s.get("events", [])),
            status=str(s.get("status", "STATUS_CODE_UNSET")),
            start_time_unix_nano=int(s.get("start_time_unix_nano", 0)),
            end_time_unix_nano=int(s.get("end_time_unix_nano", 0)),
        )
        for s in spans
    ]
