"""D2 guard: spawn paths must use asyncio.create_task (which copies
contextvars automatically), never a raw Thread / ThreadPoolExecutor /
run_in_executor, which silently break context propagation and turn a child
span into a new root."""

from __future__ import annotations

import re
from pathlib import Path

HARNESS_DIR = Path(__file__).parent.parent.parent / "src" / "agenttrace" / "harness"
# Matched only as an actual call (name immediately followed by "("), so
# prose mentions of these names in docstrings/comments don't false-positive.
FORBIDDEN = re.compile(r"\b(ThreadPoolExecutor|threading\.Thread|run_in_executor)\s*\(")


def test_no_raw_thread_usage_in_harness_spawn_paths() -> None:
    offenders: list[str] = []
    for path in HARNESS_DIR.rglob("*.py"):
        text = path.read_text()
        for match in FORBIDDEN.finditer(text):
            offenders.append(f"{path.relative_to(HARNESS_DIR.parent.parent)}: {match.group(0)}")
    assert not offenders, (
        "harness spawn paths must use asyncio.create_task, not raw threads "
        "(D2) — contextvars, and therefore the current OTel span, are not "
        "copied automatically otherwise:\n" + "\n".join(offenders)
    )
