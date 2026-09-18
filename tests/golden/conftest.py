from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased

from agenttrace.telemetry.content_store import ContentStore
from agenttrace.telemetry.hooks import reset_hooks, set_hooks
from agenttrace.telemetry.otel_hooks import OtelHooks


@pytest.fixture
def memory_exporter(tmp_path: object) -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=ParentBased(ALWAYS_ON))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("agenttrace-test")
    store = ContentStore(root=tmp_path)  # type: ignore[arg-type]
    set_hooks(OtelHooks(tracer=tracer, content_store=store, capture_content=True))
    exporter.tracer = tracer  # type: ignore[attr-defined]
    try:
        yield exporter
    finally:
        reset_hooks()
        provider.shutdown()
