"""One entrypoint every process (main script, worker, server) calls to turn
on real telemetry. Honors the standard OTEL_SDK_DISABLED env var: when set,
hooks stay NoOp and the harness behaves exactly as it does in Phase 1 — the
parity test (tests/integration/test_otel_disabled_parity.py) asserts this.
"""

from __future__ import annotations

import os

from opentelemetry.sdk.trace import TracerProvider

from agenttrace.telemetry.content_store import get_default_store
from agenttrace.telemetry.hooks import reset_hooks, set_hooks
from agenttrace.telemetry.otel_hooks import OtelHooks
from agenttrace.telemetry.setup import setup_tracing, tracer


def sdk_disabled() -> bool:
    return os.environ.get("OTEL_SDK_DISABLED", "false").strip().lower() == "true"


def init_telemetry(service_name: str) -> TracerProvider | None:
    if sdk_disabled():
        reset_hooks()
        return None
    capture = os.environ.get("AGENTTRACE_CAPTURE_CONTENT", "false").strip().lower() == "true"
    provider, _processor = setup_tracing(service_name=service_name)
    set_hooks(OtelHooks(tracer=tracer(service_name), content_store=get_default_store(), capture_content=capture))
    return provider
