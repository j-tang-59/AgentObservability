"""E7: emit verdicts as gen_ai.evaluation.result log events (→ Loki via the
Collector) plus Prometheus counters (agenttrace_findings_total), never as
synthetic spans grafted into the original trace — the observed trace stays
immutable and every coverage/span-count computation in Part A stays correct
without having to filter out evaluator-invented spans.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from opentelemetry._logs import Logger, LogRecord, get_logger, set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from prometheus_client import Counter, Histogram

from agenttrace.evaluator.findings import Finding
from agenttrace.telemetry import semconv

AGENTTRACE_FINDINGS_TOTAL = Counter(
    "agenttrace_findings_total",
    "Findings emitted by the evaluator, by failure class and evaluator type.",
    ["failure_class", "evaluator_type"],
)
AGENTTRACE_EVAL_LATENCY = Histogram(
    "agenttrace_evaluation_latency_seconds",
    "Wall-clock time to evaluate one completed trace after it became eligible.",
)


@dataclass
class EmitterConfig:
    service_name: str = "agenttrace-evaluator"
    otlp_endpoint: str | None = None


def build_logger(config: EmitterConfig | None = None) -> Logger:
    config = config or EmitterConfig()
    test_span_file = os.environ.get("AGENTTRACE_TEST_LOG_FILE")
    if test_span_file:
        from agenttrace.telemetry.file_log_exporter import FileLogExporter

        exporter = FileLogExporter(test_span_file)
    else:
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter

        endpoint = config.otlp_endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
        exporter = OTLPLogExporter(endpoint=endpoint, insecure=True)  # type: ignore[assignment]

    provider = LoggerProvider(resource=Resource.create({"service.name": config.service_name}))
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    set_logger_provider(provider)
    return get_logger(config.service_name)


def emit_finding(logger: Logger, finding: Finding) -> None:
    trace_id_int = int(finding.trace_id, 16) if finding.trace_id else 0
    span_id_int = int(finding.evidence_span_ids[0], 16) if finding.evidence_span_ids else 0

    logger.emit(
        LogRecord(
            timestamp=time.time_ns(),
            trace_id=trace_id_int,
            span_id=span_id_int,
            body=finding.explanation,
            attributes={
                semconv.GEN_AI_EVALUATION_NAME: finding.failure_class,
                semconv.GEN_AI_EVALUATION_SCORE_LABEL: semconv.EVAL_LABEL_DETECTED,
                semconv.GEN_AI_EVALUATION_EXPLANATION: finding.explanation,
                semconv.AT_EVALUATOR_TYPE: finding.evaluator_type,
                "evidence_span_ids": ",".join(finding.evidence_span_ids),
            },
        )
    )
    AGENTTRACE_FINDINGS_TOTAL.labels(failure_class=finding.failure_class, evaluator_type=finding.evaluator_type).inc()


def emit_abstention(logger: Logger, *, trace_id: str, failure_class: str, evaluator_type: str, explanation: str) -> None:
    trace_id_int = int(trace_id, 16) if trace_id else 0
    logger.emit(
        LogRecord(
            timestamp=time.time_ns(),
            trace_id=trace_id_int,
            span_id=0,
            body=explanation,
            attributes={
                semconv.GEN_AI_EVALUATION_NAME: failure_class,
                semconv.GEN_AI_EVALUATION_SCORE_LABEL: semconv.EVAL_LABEL_ABSTAINED,
                semconv.GEN_AI_EVALUATION_EXPLANATION: explanation,
                semconv.AT_EVALUATOR_TYPE: evaluator_type,
            },
        )
    )
    AGENTTRACE_FINDINGS_TOTAL.labels(failure_class=failure_class, evaluator_type=evaluator_type).inc(0)
