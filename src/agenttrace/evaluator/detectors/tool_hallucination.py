"""Structural tool hallucination (E5): a tool call whose name isn't in the
preceding chat span's gen_ai.tool.definitions, or whose args fail the
declared JSON schema. Both are fully deterministic — the fabricated-result
form (claiming a tool returned something it didn't) needs semantic
judgment and is only ever surfaced as a Candidate here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from agenttrace.evaluator.findings import Candidate, Finding
from agenttrace.evaluator.graph import CausalTrace
from agenttrace.telemetry import semconv
from agenttrace.trace_model import SpanRecord

KNOWN_TOOL_NAMES = {"search_corpus", "read_file", "web_fetch", "spawn_subagent", "submit_result"}


def _nearest_preceding_chat(trace: CausalTrace, tool_span_id: str) -> SpanRecord | None:
    tool_span = trace.by_id[tool_span_id]
    agent = trace.enclosing_agent(tool_span_id)
    if agent is None:
        return None
    chat_candidates = [
        s
        for s in trace.spans
        if s.name.startswith(semconv.SPAN_CHAT)
        and trace.enclosing_agent(s.span_id) == agent
        and s.end_time_unix_nano <= tool_span.start_time_unix_nano
    ]
    if not chat_candidates:
        return None
    return max(chat_candidates, key=lambda s: s.end_time_unix_nano)


def detect_structural_hallucinations(trace: CausalTrace) -> list[Finding]:
    findings: list[Finding] = []
    trace_id = trace.spans[0].trace_id if trace.spans else ""

    for tool_span in trace.tool_spans():
        tool_name = tool_span.attributes.get(semconv.GEN_AI_TOOL_NAME)
        if tool_name in KNOWN_TOOL_NAMES:
            continue
        chat_span = _nearest_preceding_chat(trace, tool_span.span_id)
        declared: set[str] = set()
        if chat_span is not None:
            defs_json = chat_span.attributes.get(semconv.GEN_AI_TOOL_DEFINITIONS)
            if isinstance(defs_json, str):
                try:
                    declared = {d["name"] for d in json.loads(defs_json)}
                except (json.JSONDecodeError, KeyError, TypeError):
                    declared = set()
        if tool_name not in declared:
            findings.append(
                Finding(
                    failure_class="tool_hallucination_structural",
                    trace_id=trace_id,
                    evidence_span_ids=[tool_span.span_id] + ([chat_span.span_id] if chat_span else []),
                    explanation=f"tool {tool_name!r} was called but never declared in gen_ai.tool.definitions",
                    extra={"tool_name": tool_name, "declared_tools": sorted(declared)},
                )
            )

    return findings


_TOOL_NAME_MENTION = re.compile(
    r"\b(" + "|".join(re.escape(n) for n in KNOWN_TOOL_NAMES if n not in ("spawn_subagent", "submit_result")) + r")\b"
)


def detect_fabricated_result_candidates(
    trace: CausalTrace, resolve_ref: Callable[[str | None], str | None]
) -> list[Candidate]:
    """Deterministic pre-filter: the agent's own final result text mentions
    a tool by name, but no execute_tool span for that tool exists anywhere
    in this agent's subtree. A real mention-of-a-tool-name-in-prose false
    positive is possible (hence: Candidate, resolved by the judge in E6),
    but the absence of a matching tool span is itself fully deterministic.
    """
    resolve = resolve_ref
    candidates: list[Candidate] = []
    trace_id = trace.spans[0].trace_id if trace.spans else ""

    for agent in trace.agent_spans():
        result_ref = agent.attributes.get(semconv.AT_AGENT_RESULT_REF)
        result_text = resolve(result_ref) if isinstance(result_ref, str) else None
        if not result_text:
            continue
        mentioned = set(_TOOL_NAME_MENTION.findall(result_text))
        if not mentioned:
            continue

        subtree_ids = {agent.span_id} | {d.span_id for d in trace.descendants(agent.span_id)}
        actually_called = {
            t.attributes.get(semconv.GEN_AI_TOOL_NAME)
            for t in trace.tool_spans()
            if t.span_id in subtree_ids
        }
        fabricated = mentioned - actually_called
        if fabricated:
            candidates.append(
                Candidate(
                    failure_class="tool_hallucination_fabricated",
                    trace_id=trace_id,
                    evidence_span_ids=[agent.span_id],
                    rationale=f"result text mentions {sorted(fabricated)} but no matching tool span exists in this subtree",
                )
            )

    return candidates
