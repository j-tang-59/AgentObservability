"""Loads a trace's spans and lazily resolves content refs (D8, E3).

Two span sources share the same SpanRecord shape (trace_model.py): Tempo's
OTLP-JSON query API for real pipeline runs, and the offline file exporter
used by bench/suite.py. Hash/size-only detectors never touch the content
store at all — resolve_ref is only called by detectors that actually need
payload text (E3's "deterministic detectors that work on hashes and sizes
run without ever fetching content").
"""

from __future__ import annotations

import httpx

from agenttrace.telemetry.content_store import ContentStore, get_default_store
from agenttrace.trace_model import SpanRecord, parse_tempo_trace


class TraceLoader:
    def __init__(self, content_store: ContentStore | None = None) -> None:
        self._store = content_store or get_default_store()

    def resolve_ref(self, ref: str | None) -> str | None:
        if not ref:
            return None
        try:
            return self._store.get(ref)
        except FileNotFoundError:
            return None

    def load_from_tempo(self, tempo_url: str, trace_id: str) -> list[SpanRecord]:
        resp = httpx.get(f"{tempo_url}/api/traces/{trace_id}", timeout=10.0)
        resp.raise_for_status()
        return parse_tempo_trace(resp.json())
