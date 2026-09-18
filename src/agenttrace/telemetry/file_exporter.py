"""A SpanExporter that appends one JSON line per span to a shared file.

Test-only: lets a property test collect spans from the parent process AND
every subprocess/HTTP-server child into one place without a real Collector,
so cross-process propagation (D4, D5) can be asserted without docker.
Appends are O_APPEND, which is atomic for writes under PIPE_BUF on POSIX —
good enough for line-sized JSON records from a handful of concurrent
processes in a test.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult


def span_to_dict(span: ReadableSpan) -> dict[str, object]:
    ctx = span.context
    parent = span.parent
    return {
        "name": span.name,
        "trace_id": format(ctx.trace_id, "032x") if ctx else None,
        "span_id": format(ctx.span_id, "016x") if ctx else None,
        "parent_span_id": format(parent.span_id, "016x") if parent else None,
        "kind": span.kind.name,
        "attributes": dict(span.attributes or {}),
        "events": [
            {"name": e.name, "attributes": dict(e.attributes or {})} for e in (span.events or [])
        ],
        "status": span.status.status_code.name,
        "start_time_unix_nano": span.start_time,
        "end_time_unix_nano": span.end_time,
    }


class FileSpanExporter(SpanExporter):
    def __init__(self, path: str) -> None:
        self._path = path

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        lines = [json.dumps(span_to_dict(s)) for s in spans]
        if not lines:
            return SpanExportResult.SUCCESS
        data = ("\n".join(lines) + "\n").encode()
        fd = os.open(self._path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def read_spans(path: str) -> list[dict[str, object]]:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]
