"""D9 guard: every gen_ai.* / agenttrace.* string literal must live in
telemetry/semconv.py so a dependency bump or a drive-by edit can't silently
drift span/attribute names out of sync with the pinned v1.41.0 schema."""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).parent.parent.parent / "src" / "agenttrace"
SEMCONV_FILE = SRC / "telemetry" / "semconv.py"
PATTERN = re.compile(r'["\'](gen_ai\.[a-zA-Z0-9_.]+|agenttrace\.[a-zA-Z0-9_.]+)["\']')
# Real agenttrace.* semconv attributes never use these as their second
# segment; agenttrace.harness.worker etc. are Python module paths, not
# telemetry attribute names.
MODULE_PATH_SEGMENTS = {"harness", "telemetry", "evaluator", "faults", "bench"}


def _is_module_path(literal: str) -> bool:
    parts = literal.split(".")
    return len(parts) > 1 and parts[1] in MODULE_PATH_SEGMENTS


def test_no_stray_gen_ai_or_agenttrace_literals_outside_semconv() -> None:
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if path == SEMCONV_FILE:
            continue
        text = path.read_text()
        for match in PATTERN.finditer(text):
            if _is_module_path(match.group(1)):
                continue
            offenders.append(f"{path.relative_to(SRC)}: {match.group(0)}")
    assert not offenders, "gen_ai./agenttrace. literals must live in telemetry/semconv.py:\n" + "\n".join(
        offenders
    )
