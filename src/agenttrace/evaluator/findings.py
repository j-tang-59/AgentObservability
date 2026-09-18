"""Shared result types for detectors and the judge."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agenttrace.telemetry.semconv import EVAL_TYPE_DETERMINISTIC


@dataclass
class Finding:
    failure_class: str
    trace_id: str
    evaluator_type: str = EVAL_TYPE_DETERMINISTIC
    evidence_span_ids: list[str] = field(default_factory=list)
    explanation: str = ""
    confidence: float = 1.0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Candidate:
    """A deterministic pre-filter's output that needs a semantic call to
    resolve (E1's principle: deterministic detectors surface candidates,
    the judge only ever sees candidates, never raw spans)."""

    failure_class: str
    trace_id: str
    evidence_span_ids: list[str]
    rationale: str
