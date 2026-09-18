"""Shared, run-level state: budgets, runner selection, spawn bookkeeping."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class GraphRecursionError(Exception):
    """Raised when an agent's own model<->tool loop exceeds recursion_limit."""


class BudgetExceededError(Exception):
    """Raised when a spawn would exceed max_depth / max_fanout / max_agents."""


class ChildKilledError(Exception):
    """Raised by a runner that had to forcibly kill a child (e.g. SIGKILL a
    subprocess that missed its timeout). Distinct from a plain timeout: the
    parent knows for certain the child could not flush its own telemetry."""


class Runner(Protocol):
    name: str

    async def spawn(
        self,
        *,
        role: str,
        objective: str,
        depth: int,
        run_ctx: RunContext,
        spawn_id: str,
    ) -> str:
        """Run a child agent to completion and return its result string."""
        ...


@dataclass
class RunContext:
    run_id: str
    max_depth: int = 3
    max_fanout: int = 3
    max_agents: int = 100
    recursion_limit: int = 8
    per_child_timeout_s: float = 30.0
    runner_plan: list[str] = field(default_factory=lambda: ["inproc"])
    http_base_url: str = "http://127.0.0.1:8765"
    runners: dict[str, Runner] = field(default_factory=dict)
    _agents_spawned: int = field(default=1)  # root counts as 1

    def reserve_agent_slot(self) -> None:
        if self._agents_spawned >= self.max_agents:
            raise BudgetExceededError(
                f"max_agents budget of {self.max_agents} exceeded"
            )
        self._agents_spawned += 1

    @property
    def agents_spawned(self) -> int:
        return self._agents_spawned

    def runner_name_for_depth(self, depth: int) -> str:
        return self.runner_plan[depth] if depth < len(self.runner_plan) else self.runner_plan[-1]

    def runner_for(self, depth: int) -> Runner:
        return self.runners[self.runner_name_for_depth(depth)]

    def to_spawn_payload(self, *, role: str, objective: str, depth: int) -> dict[str, object]:
        return {
            "role": role,
            "objective": objective,
            "depth": depth,
            "run_id": self.run_id,
            "max_depth": self.max_depth,
            "max_fanout": self.max_fanout,
            "max_agents": self.max_agents,
            "recursion_limit": self.recursion_limit,
            "per_child_timeout_s": self.per_child_timeout_s,
            "runner_plan": self.runner_plan,
            "http_base_url": self.http_base_url,
            "agents_spawned_so_far": self._agents_spawned,
        }
