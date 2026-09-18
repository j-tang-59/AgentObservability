"""Deterministic FakeChatModel scripts.

Objectives are small encoded strings so a benchmark run's shape (fan-out,
depth) is fully determined by the objective text alone — this lets the same
script run identically in-process, in a subprocess, or behind HTTP.

Objective grammar: "fanout:<n>|depth:<d>|<free text>"
  - depth is the number of further spawn levels remaining (not the absolute
    tree depth); when it reaches 0 the agent does one search + submits.
  - a bare objective with no "fanout:" prefix is always a leaf.
"""

from __future__ import annotations

import re
from typing import Any

from agenttrace.harness.fake_model import ChatResponse, ToolCall

_SPEC_RE = re.compile(r"^fanout:(\d+)\|depth:(\d+)\|(.*)$", re.DOTALL)

_CHILD_ROLES = ("researcher", "verifier")


def parse_objective(objective: str) -> tuple[int, int, str] | None:
    m = _SPEC_RE.match(objective)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), m.group(3)


def make_objective(fanout: int, depth: int, text: str) -> str:
    return f"fanout:{fanout}|depth:{depth}|{text}"


def default_script(role: str, messages: list[dict[str, Any]], step: int) -> ChatResponse:
    objective = str(messages[0]["content"])
    spec = parse_objective(objective)

    if step == 0:
        if spec and spec[1] > 0 and role in ("orchestrator", "researcher", "verifier"):
            fanout, depth, text = spec
            calls = [
                ToolCall(
                    name="spawn_subagent",
                    args={
                        "role": _CHILD_ROLES[i % len(_CHILD_ROLES)],
                        "objective": make_objective(fanout, depth - 1, f"{text} :: child {i}"),
                    },
                    id=f"call-spawn-{i}",
                )
                for i in range(fanout)
            ]
            return ChatResponse(content="spawning subagents", tool_calls=calls)
        query = (spec[2] if spec else objective).split()[0] if (spec[2] if spec else objective) else "agenttrace"
        return ChatResponse(
            content="searching corpus",
            tool_calls=[ToolCall(name="search_corpus", args={"query": query}, id="call-search-0")],
        )

    if step == 1:
        last_tool_results = [m for m in messages if m.get("role") == "tool"]
        summary = "; ".join(str(m["content"])[:80] for m in last_tool_results)
        return ChatResponse(
            content="done",
            tool_calls=[ToolCall(name="submit_result", args={"result": f"result for {role}: {summary}"}, id="call-submit-0")],
        )

    return ChatResponse(
        content="done",
        tool_calls=[ToolCall(name="submit_result", args={"result": f"fallback result for {role}"}, id="call-submit-fallback")],
    )
