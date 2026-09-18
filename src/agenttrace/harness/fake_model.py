"""A deterministic, scripted chat model used for $0 CI runs.

Scripts are keyed by role + step index so a run is fully reproducible: no
network call, no randomness, fixed simulated latency.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    id: str


@dataclass
class ChatResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = "fake-chat-v1"
    input_tokens: int = 0
    output_tokens: int = 0


ScriptFn = Callable[[str, list[dict[str, Any]], int], ChatResponse]


class FakeChatModel:
    """Scripted model: script(role, messages, step) -> ChatResponse."""

    def __init__(self, script: ScriptFn, latency_s: float = 0.0) -> None:
        self._script = script
        self._latency_s = latency_s
        self.model_name = "fake-chat-v1"

    def invoke(self, role: str, messages: list[dict[str, Any]], step: int) -> ChatResponse:
        if self._latency_s:
            time.sleep(self._latency_s)
        resp = self._script(role, messages, step)
        # cheap deterministic token counts so cost roll-up has something to sum
        resp.input_tokens = resp.input_tokens or sum(len(str(m.get("content", ""))) for m in messages) // 4
        resp.output_tokens = resp.output_tokens or len(resp.content) // 4
        return resp
