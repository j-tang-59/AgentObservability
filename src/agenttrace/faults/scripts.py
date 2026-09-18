"""Fault-injecting FakeChatModel scripts.

Each builder returns a script function with the same signature as
harness.scripts.default_script (role, messages, step) -> ChatResponse, so it
drops straight into FakeChatModel. Faults are expressed as deterministic
script behavior, not randomness — the whole point of the synthetic tier is
that ground truth is known exactly (E8).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from agenttrace.harness.fake_model import ChatResponse, ToolCall
from agenttrace.harness.scripts import default_script

Script = Callable[[str, list[dict[str, Any]], int], ChatResponse]


def clean_script() -> Script:
    return default_script


def retry_storm_script(*, repeats: int = 5, query: str = "agenttrace") -> Script:
    """Same tool + same args called `repeats` times in a row before giving
    up — the naive-retry-loop failure class (E5)."""

    def script(role: str, messages: list[dict[str, Any]], step: int) -> ChatResponse:
        if step < repeats:
            return ChatResponse(
                content="retrying search",
                tool_calls=[ToolCall(name="search_corpus", args={"query": query}, id=f"call-retry-{step}")],
            )
        return ChatResponse(
            content="giving up",
            tool_calls=[ToolCall(name="submit_result", args={"result": "no luck after retries"}, id="call-submit")],
        )

    return script


def silent_failure_script(*, url: str = "https://example.test/broken") -> Script:
    """web_fetch returns an HTTP-error-shaped body with OK span status; the
    agent incorporates it into its result verbatim without noticing."""

    def script(role: str, messages: list[dict[str, Any]], step: int) -> ChatResponse:
        if step == 0:
            return ChatResponse(
                content="fetching",
                tool_calls=[ToolCall(name="web_fetch", args={"url": url}, id="call-fetch")],
            )
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        fetched = str(tool_msgs[-1]["content"]) if tool_msgs else ""
        return ChatResponse(
            content="done",
            tool_calls=[
                ToolCall(name="submit_result", args={"result": f"here is what I found: {fetched}"}, id="call-submit")
            ],
        )

    return script


def structural_hallucination_script(*, fake_tool: str = "delete_database") -> Script:
    """Calls a tool that was never declared in gen_ai.tool.definitions."""

    def script(role: str, messages: list[dict[str, Any]], step: int) -> ChatResponse:
        if step == 0:
            return ChatResponse(
                content="using an undeclared tool",
                tool_calls=[ToolCall(name=fake_tool, args={"table": "users"}, id="call-hallucinate")],
            )
        return ChatResponse(
            content="done",
            tool_calls=[ToolCall(name="submit_result", args={"result": "handled"}, id="call-submit")],
        )

    return script


def fabricated_result_script(*, claimed_tool: str = "web_fetch") -> Script:
    """Claims a tool returned data without ever calling that tool."""

    def script(role: str, messages: list[dict[str, Any]], step: int) -> ChatResponse:
        if step == 0:
            return ChatResponse(
                content="using search only",
                tool_calls=[ToolCall(name="search_corpus", args={"query": "agenttrace"}, id="call-search")],
            )
        return ChatResponse(
            content="done",
            tool_calls=[
                ToolCall(
                    name="submit_result",
                    args={"result": f"according to {claimed_tool}, the answer is 42"},
                    id="call-submit",
                )
            ],
        )

    return script


def orphan_timeout_script(*, sleep_s: float = 2.0, child_role: str = "researcher") -> Script:
    """The orchestrator spawns exactly one child whose objective is marked
    SLOW_CHILD; that child sleeps well past its parent's
    per_child_timeout_s, forcing the SubprocessRunner to SIGKILL it —
    exercises D6's spawn.outcome=killed path (a true runtime orphan, not an
    instrumentation miss, per E5's distinction)."""

    def script(role: str, messages: list[dict[str, Any]], step: int) -> ChatResponse:
        objective = str(messages[0]["content"])
        if role == "orchestrator" and step == 0:
            return ChatResponse(
                content="spawning a slow child",
                tool_calls=[
                    ToolCall(
                        name="spawn_subagent",
                        args={"role": child_role, "objective": "SLOW_CHILD task"},
                        id="call-spawn",
                    )
                ],
            )
        if role != "orchestrator" and "SLOW_CHILD" in objective and step == 0:
            time.sleep(sleep_s)
            return ChatResponse(
                content="finally done",
                tool_calls=[ToolCall(name="submit_result", args={"result": "ok"}, id="call-submit")],
            )
        return ChatResponse(
            content="done",
            tool_calls=[ToolCall(name="submit_result", args={"result": "handled timeout"}, id="call-submit")],
        )

    return script


def prompt_injection_script(*, injected_url: str = "https://example.test/injected") -> Script:
    """Fetches tainted content, then — if the injection "worked" — issues a
    later tool call whose args contain a chunk copied verbatim from that
    tainted payload (the mechanically detectable form of "obeyed the
    injected instruction," per E5's taint-propagation candidate rule)."""

    def script(role: str, messages: list[dict[str, Any]], step: int) -> ChatResponse:
        if step == 0:
            return ChatResponse(
                content="fetching page",
                tool_calls=[ToolCall(name="web_fetch", args={"url": injected_url}, id="call-fetch")],
            )
        if step == 1:
            tool_msgs = [m for m in messages if m.get("role") == "tool"]
            fetched = str(tool_msgs[-1]["content"]) if tool_msgs else ""
            # "obeys" the injected page by echoing its content into a new
            # web_fetch call — a tainted chunk flowing into tool args.
            return ChatResponse(
                content="following the instruction I just read",
                tool_calls=[ToolCall(name="web_fetch", args={"url": fetched}, id="call-fetch-2")],
            )
        return ChatResponse(
            content="done",
            tool_calls=[ToolCall(name="submit_result", args={"result": "task complete"}, id="call-submit")],
        )

    return script
