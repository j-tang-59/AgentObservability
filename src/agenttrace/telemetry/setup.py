"""TracerProvider construction (D7, D9, D10).

- ParentBased(ALWAYS_ON) everywhere: a partially-sampled trace is a
  fragmented trace, and only a ParentBased sampler honors the sampling
  decision carried in traceparent (D10).
- Schema URL pinned to v1.41.0 (D9).
- BatchSpanProcessor wrapped to count dropped spans — the SDK only logs a
  warning on queue overflow, and a dropped span looks exactly like a
  propagation failure if you don't count it separately (D7).
- OTLP/gRPC exporter to a local Collector by default.
"""

from __future__ import annotations

import atexit
import os
import signal
import sys
import threading
from types import FrameType
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased

from agenttrace.telemetry.semconv import SCHEMA_URL

OTEL_SEMCONV_ENV = "OTEL_SEMCONV_STABILITY_OPT_IN"
OTEL_SEMCONV_VALUE = "gen_ai_latest_experimental"


class DropCountingExporter(SpanExporter):
    """Wraps a real exporter so 'dropped spans' has a real number, not just
    a warning in the logs. BatchSpanProcessor.on_end() itself also drops
    spans silently when its queue is full, before they ever reach an
    exporter — counted separately via self.queue_drops, incremented by
    CountingBatchSpanProcessor below."""

    def __init__(self, inner: SpanExporter) -> None:
        self._inner = inner
        self.export_failures = 0
        self._lock = threading.Lock()

    def export(self, spans: object) -> SpanExportResult:
        result = self._inner.export(spans)  # type: ignore[arg-type]
        if result != SpanExportResult.SUCCESS:
            with self._lock:
                self.export_failures += len(spans)  # type: ignore[arg-type]
        return result

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


class CountingBatchSpanProcessor(BatchSpanProcessor):
    """BatchSpanProcessor that counts spans dropped because the internal
    queue was full. The SDK's internal BatchProcessor only logs a warning
    and increments an (opt-in, OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED)
    metric on overflow via ``self._metrics.drop_items(1)`` — verify.py needs
    a real in-process number regardless of whether that metrics pipeline is
    enabled, so this wraps that exact call site."""

    def __init__(self, exporter: SpanExporter, **kwargs: object) -> None:
        super().__init__(exporter, **kwargs)  # type: ignore[arg-type]
        self.queue_drops = 0
        inner_metrics: Any = self._batch_processor._metrics
        original_drop_items = inner_metrics.drop_items

        def _counting_drop_items(n: int) -> None:
            self.queue_drops += n
            original_drop_items(n)

        inner_metrics.drop_items = _counting_drop_items


def build_resource(service_name: str) -> Resource:
    return Resource.create(
        {
            "service.name": service_name,
            "service.namespace": "agenttrace",
        }
    )


def _default_exporter(otlp_endpoint: str | None) -> SpanExporter:
    # Test-only escape hatch: cross-process propagation tests need every
    # process (parent, subprocess children, the HTTP server) to land spans
    # in one place without standing up a real Collector. Production paths
    # never set this env var.
    test_span_file = os.environ.get("AGENTTRACE_TEST_SPAN_FILE")
    if test_span_file:
        from agenttrace.telemetry.file_exporter import FileSpanExporter

        return FileSpanExporter(test_span_file)
    endpoint = otlp_endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    return OTLPSpanExporter(endpoint=endpoint, insecure=True)


def setup_tracing(
    *,
    service_name: str,
    otlp_endpoint: str | None = None,
    max_queue_size: int = 8192,
    max_export_batch_size: int = 512,
    schedule_delay_millis: int = 2000,
) -> tuple[TracerProvider, CountingBatchSpanProcessor]:
    os.environ.setdefault(OTEL_SEMCONV_ENV, OTEL_SEMCONV_VALUE)

    provider = TracerProvider(
        resource=build_resource(service_name),
        sampler=ParentBased(ALWAYS_ON),
    )
    exporter = DropCountingExporter(_default_exporter(otlp_endpoint))
    processor = CountingBatchSpanProcessor(
        exporter,
        max_queue_size=max_queue_size,
        max_export_batch_size=max_export_batch_size,
        schedule_delay_millis=schedule_delay_millis,
    )
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

    def _flush(*_: object) -> None:
        provider.shutdown()

    atexit.register(_flush)
    if threading.current_thread() is threading.main_thread():

        def _sigterm_handler(signum: int, frame: FrameType | None) -> None:
            _flush()
            sys.exit(0)

        try:
            signal.signal(signal.SIGTERM, _sigterm_handler)
        except (ValueError, OSError):
            pass  # not the main thread / signal not supported on this platform

    return provider, processor


def tracer(name: str = "agenttrace") -> trace.Tracer:
    return trace.get_tracer(name, schema_url=SCHEMA_URL)
