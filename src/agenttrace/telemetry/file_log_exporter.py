"""Test-only LogRecord exporter, mirroring file_exporter.py for spans — lets
tests assert on emitted gen_ai.evaluation.result events without a running
Collector/Loki."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence

from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk._logs.export import LogRecordExporter, LogRecordExportResult


def log_to_dict(readable: ReadableLogRecord) -> dict[str, object]:
    record = readable.log_record
    return {
        "body": record.body,
        "trace_id": format(record.trace_id, "032x") if record.trace_id else None,
        "span_id": format(record.span_id, "016x") if record.span_id else None,
        "attributes": dict(record.attributes or {}),
        "timestamp": record.timestamp,
    }


class FileLogExporter(LogRecordExporter):
    def __init__(self, path: str) -> None:
        self._path = path

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        lines = [json.dumps(log_to_dict(ld)) for ld in batch]
        if not lines:
            return LogRecordExportResult.SUCCESS
        data = ("\n".join(lines) + "\n").encode()
        fd = os.open(self._path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        return LogRecordExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def read_logs(path: str) -> list[dict[str, object]]:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]
