"""The recursive agent graph.

One LangGraph StateGraph implements the model<->tool ReAct loop for a single
agent. spawn_subagent, executed from inside the tools node, recursively
invokes this same graph (via whichever Runner is configured for the next
depth), which is how "one graph, run recursively" is satisfied.

Fan-out (multiple spawn_subagent calls issued in the same step) runs the
child invocations as asyncio tasks so contextvars — and therefore whichever
span is "current" — propagate automatically (D2).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, TypedDict

from langgraph.errors import GraphRecursionError as LGGraphRecursionError
from langgraph.graph import END, StateGraph

from agenttrace.harness.fake_model import ChatResponse, FakeChatModel
from agenttrace.harness.run_context import (
    BudgetExceededError,
    ChildKilledError,
    GraphRecursionError,
    RunContext,
)
from agenttrace.harness.tools import (
    ROLE_TOOLS,
    SPAWN_SUBAGENT_SCHEMA,
    SUBMIT_RESULT_SCHEMA,
    ToolDef,
)
from agenttrace.telemetry import semconv
from agenttrace.telemetry.hooks import get_hooks
from agenttrace.telemetry.semconv import TAINT_TRUSTED

SPAWNABLE_ROLES = ("researcher", "verifier")


class AgentState(TypedDict):
    messages: list[dict[str, Any]]
    step: int
    result: str | None


def _tool_definitions(role: str) -> list[dict[str, Any]]:
    defs = [
        {"name": t.name, "description": t.description, "parameters": t.json_schema}
        for t in ROLE_TOOLS[role]
    ]
    if role == "orchestrator":
        defs.append(
            {
                "name": "spawn_subagent",
                "description": "Spawn a child agent to pursue a sub-objective.",
                "parameters": SPAWN_SUBAGENT_SCHEMA,
            }
        )
    defs.append(
        {"name": "submit_result", "description": "Submit the final result.", "parameters": SUBMIT_RESULT_SCHEMA}
    )
    return defs


def _tools_by_name(role: str) -> dict[str, ToolDef]:
    return {t.name: t for t in ROLE_TOOLS[role]}


async def run_agent(
    *,
    role: str,
    objective: str,
    depth: int,
    run_ctx: RunContext,
    model: FakeChatModel,
    agent_name: str | None = None,
    span_kind: str = "INTERNAL",
) -> str:
    """Recursively run one agent to completion; returns its final result string."""
    hooks = get_hooks()
    name = agent_name or role
    tools = _tools_by_name(role)
    tool_defs = _tool_definitions(role)

    async def call_model(state: AgentState) -> dict[str, Any]:
        if state["step"] >= run_ctx.recursion_limit:
            raise GraphRecursionError(f"{name} exceeded recursion_limit={run_ctx.recursion_limit}")
        with hooks.chat_span(semconv.chat_span_name(model.model_name), model=model.model_name):
            resp: ChatResponse = model.invoke(role, state["messages"], state["step"])
            hooks.set_chat_attrs(
                tool_definitions=tool_defs,
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
            )
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": resp.content}
        if resp.tool_calls:
            assistant_msg["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "args": tc.args} for tc in resp.tool_calls
            ]
        return {"messages": state["messages"] + [assistant_msg], "step": state["step"] + 1}

    async def _exec_one_tool(tc: dict[str, Any]) -> dict[str, Any]:
        tool_name = tc["name"]
        args = tc["args"]
        error_type: str | None = None
        result_text: str
        taint = TAINT_TRUSTED
        with hooks.tool_span(semconv.tool_span_name(tool_name), tool_name=tool_name):
            try:
                if tool_name == "submit_result":
                    result_text = str(args.get("result", ""))
                elif tool_name == "spawn_subagent":
                    result_text = await _spawn(args)
                elif tool_name in tools:
                    tool_result = await tools[tool_name].fn(args)
                    result_text, taint = tool_result.text, tool_result.taint
                else:
                    error_type = "UndeclaredToolError"
                    result_text = f"error: unknown tool {tool_name}"
            except Exception as exc:  # noqa: BLE001 - recorded on the span, not swallowed silently
                error_type = type(exc).__name__
                result_text = f"error: {exc}"
            hooks.set_tool_result(args=args, result=result_text, taint=taint, error_type=error_type)
        return {"id": tc["id"], "role": "tool", "name": tool_name, "content": result_text}

    async def _spawn(args: dict[str, Any]) -> str:
        child_role = str(args["role"])
        child_objective = str(args["objective"])
        if child_role not in SPAWNABLE_ROLES:
            raise ValueError(f"role {child_role} is not spawnable")
        if depth + 1 > run_ctx.max_depth:
            raise BudgetExceededError(f"max_depth={run_ctx.max_depth} exceeded")
        run_ctx.reserve_agent_slot()
        runner = run_ctx.runner_for(depth + 1)
        spawn_id = hooks.spawn_intent(
            role=child_role, objective=child_objective, depth=depth + 1, runner_name=runner.name
        )
        try:
            # Each Runner enforces run_ctx.per_child_timeout_s itself, because
            # only the runner knows how to abort its child correctly: a
            # SIGKILL for a subprocess ("killed") vs. a plain cancellation for
            # an in-process task or an HTTP request ("timeout"). Wrapping this
            # in one more asyncio.wait_for here would race the runner's own
            # handling and could report "timeout" for what was actually a
            # forced kill.
            result = await runner.spawn(
                role=child_role,
                objective=child_objective,
                depth=depth + 1,
                run_ctx=run_ctx,
                spawn_id=spawn_id,
            )
            hooks.spawn_outcome(spawn_id, "returned")
            return result
        except ChildKilledError:
            hooks.spawn_outcome(spawn_id, "killed")
            raise
        except TimeoutError:
            hooks.spawn_outcome(spawn_id, "timeout")
            raise
        except Exception:
            hooks.spawn_outcome(spawn_id, "error")
            raise

    async def call_tools(state: AgentState) -> dict[str, Any]:
        last = state["messages"][-1]
        tool_calls = last.get("tool_calls", [])
        tasks = [asyncio.create_task(_exec_one_tool(tc)) for tc in tool_calls]
        results = await asyncio.gather(*tasks)
        new_messages = list(state["messages"]) + list(results)
        submitted = next((r["content"] for r in results if r["name"] == "submit_result"), None)
        return {"messages": new_messages, "result": submitted}

    def route_after_model(state: AgentState) -> str:
        last = state["messages"][-1]
        if last.get("role") == "assistant" and last.get("tool_calls"):
            return "tools"
        return END

    def route_after_tools(state: AgentState) -> str:
        if state.get("result") is not None:
            return END
        return "model"

    graph = StateGraph(AgentState)
    graph.add_node("model", call_model)
    graph.add_node("tools", call_tools)
    graph.set_entry_point("model")
    graph.add_conditional_edges("model", route_after_model, {"tools": "tools", END: END})
    graph.add_conditional_edges("tools", route_after_tools, {"model": "model", END: END})
    compiled = graph.compile()

    initial: AgentState = {
        "messages": [{"role": "user", "content": objective}],
        "step": 0,
        "result": None,
    }

    with hooks.agent_span(
        semconv.agent_span_name(name), agent_name=name, kind=span_kind, role=role, depth=depth
    ):
        try:
            final = await compiled.ainvoke(
                # LangGraph counts every node execution (each model<->tools
                # round is 2), while our own recursion_limit counts model
                # calls only; double it plus slack so our own
                # GraphRecursionError check (in call_model) always fires
                # first and LangGraph's cap is never the binding one.
                initial,
                config={"recursion_limit": run_ctx.recursion_limit * 2 + 4},
            )
        except LGGraphRecursionError as exc:
            raise GraphRecursionError(str(exc)) from exc
        result = final.get("result")
        if result is None:
            # loop ended without submit_result (e.g. model ran out of tool calls)
            last_assistant = next(
                (m for m in reversed(final["messages"]) if m.get("role") == "assistant"), None
            )
            result = last_assistant["content"] if last_assistant else ""
        hooks.set_agent_result(result)
        return result


async def run_workflow(
    *,
    objective: str,
    run_ctx: RunContext,
    model: FakeChatModel,
    workflow_name: str = "agenttrace-demo",
    on_root_span_started: Any | None = None,
    role: str = "orchestrator",
) -> str:
    """on_root_span_started, if given, is called with the root span's hex
    trace ID right after the workflow span opens — used by demo.py/verify.py
    to know which trace to look up afterwards, without changing this
    function's return type for every other caller."""
    hooks = get_hooks()
    with hooks.workflow_span(semconv.workflow_span_name(workflow_name)):
        if on_root_span_started is not None:
            on_root_span_started(hooks.current_trace_id())
        return await run_agent(
            role=role,
            objective=objective,
            depth=0,
            run_ctx=run_ctx,
            model=model,
            agent_name=role,
            span_kind="INTERNAL",
        )


def new_run_id() -> str:
    return uuid.uuid4().hex
