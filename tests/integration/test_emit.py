"""E7: verdicts are emitted as gen_ai.evaluation.result log events carrying
the evaluated span's trace/span IDs, plus a findings counter."""

from __future__ import annotations

import os

from agenttrace.evaluator.emit import (
    AGENTTRACE_FINDINGS_TOTAL,
    EmitterConfig,
    build_logger,
    emit_finding,
)
from agenttrace.evaluator.findings import Finding
from agenttrace.telemetry import semconv
from agenttrace.telemetry.file_log_exporter import read_logs


def test_emit_finding_writes_gen_ai_evaluation_result_event(tmp_path) -> None:
    log_file = tmp_path / "logs.jsonl"
    os.environ["AGENTTRACE_TEST_LOG_FILE"] = str(log_file)
    logger = build_logger(EmitterConfig(service_name="agenttrace-evaluator-test"))

    finding = Finding(
        failure_class="retry_storm",
        trace_id="ab" * 16,
        evidence_span_ids=["cd" * 8],
        explanation="tool called too many times",
    )
    before = AGENTTRACE_FINDINGS_TOTAL.labels(failure_class="retry_storm", evaluator_type="deterministic")._value.get()
    emit_finding(logger, finding)

    from opentelemetry import _logs as logs_api

    provider = logs_api.get_logger_provider()
    provider.force_flush()  # type: ignore[union-attr]

    logs = read_logs(str(log_file))
    assert len(logs) == 1
    entry = logs[0]
    assert entry["trace_id"] == finding.trace_id
    assert entry["attributes"][semconv.GEN_AI_EVALUATION_NAME] == "retry_storm"
    assert entry["attributes"][semconv.GEN_AI_EVALUATION_SCORE_LABEL] == semconv.EVAL_LABEL_DETECTED
    assert entry["attributes"][semconv.AT_EVALUATOR_TYPE] == semconv.EVAL_TYPE_DETERMINISTIC

    after = AGENTTRACE_FINDINGS_TOTAL.labels(failure_class="retry_storm", evaluator_type="deterministic")._value.get()
    assert after == before + 1
