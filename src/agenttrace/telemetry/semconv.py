"""Single source of truth for every gen_ai.* and agenttrace.* attribute/span name.

Pinned to OTel semantic-conventions v1.41.0 (GenAI). A golden test
(tests/golden/test_no_stray_semconv_literals.py) fails the build if a
`gen_ai.` or `agenttrace.` string literal appears anywhere else in src/.
"""

from __future__ import annotations

SCHEMA_URL = "https://opentelemetry.io/schemas/1.41.0"

# --- span name prefixes (operation names) ---------------------------------
SPAN_INVOKE_WORKFLOW = "invoke_workflow"
SPAN_INVOKE_AGENT = "invoke_agent"
SPAN_EXECUTE_TOOL = "execute_tool"
SPAN_CHAT = "chat"

# --- gen_ai.* attributes ----------------------------------------------------
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"
GEN_AI_AGENT_ID = "gen_ai.agent.id"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"
GEN_AI_TOOL_DEFINITIONS = "gen_ai.tool.definitions"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"

# gen_ai.evaluation.result event (E7)
EVENT_EVALUATION_RESULT = "gen_ai.evaluation.result"
GEN_AI_EVALUATION_NAME = "gen_ai.evaluation.name"
GEN_AI_EVALUATION_SCORE_VALUE = "gen_ai.evaluation.score.value"
GEN_AI_EVALUATION_SCORE_LABEL = "gen_ai.evaluation.score.label"
GEN_AI_EVALUATION_EXPLANATION = "gen_ai.evaluation.explanation"

# --- agenttrace.* custom attributes ----------------------------------------
AT_SPAWN_ID = "agenttrace.spawn.id"
AT_SPAWN_OUTCOME = "agenttrace.spawn.outcome"
AT_SPAWN_DEPTH = "agenttrace.spawn.depth"
AT_SPAWN_RUNNER = "agenttrace.spawn.runner"
AT_SPAWN_OBJECTIVE_REF = "agenttrace.spawn.objective_ref"
AT_SPAWN_OBJECTIVE_SHA256 = "agenttrace.spawn.objective_sha256"

AT_AGENT_ROLE = "agenttrace.agent.role"
AT_AGENT_DEPTH = "agenttrace.agent.depth"
AT_AGENT_RESULT_REF = "agenttrace.agent.result_ref"

AT_TOOL_ARGS_SHA256 = "agenttrace.tool.args_sha256"
AT_TOOL_RESULT_BYTES = "agenttrace.tool.result_bytes"
AT_TOOL_ARGS_REF = "agenttrace.tool.args_ref"
AT_TOOL_RESULT_REF = "agenttrace.tool.result_ref"
AT_TAINT = "agenttrace.taint"

AT_CONTENT_REF = "agenttrace.content.ref"
AT_CONTENT_SHA256 = "agenttrace.content.sha256"

AT_EVALUATOR_TYPE = "agenttrace.evaluator.type"
AT_EVALUATOR_MODEL = "agenttrace.evaluator.model"

# baggage keys (D4): allowlisted to run_id + depth only, never payload content
AT_BAGGAGE_RUN_ID = "agenttrace.run_id"
AT_BAGGAGE_DEPTH = "agenttrace.depth"

# event names
EVENT_SPAWN_INTENT = "spawn_intent"

# enums -----------------------------------------------------------------
TAINT_UNTRUSTED = "untrusted"
TAINT_TRUSTED = "trusted"

SPAWN_OUTCOME_RETURNED = "returned"
SPAWN_OUTCOME_TIMEOUT = "timeout"
SPAWN_OUTCOME_KILLED = "killed"
SPAWN_OUTCOME_ERROR = "error"

EVAL_TYPE_DETERMINISTIC = "deterministic"
EVAL_TYPE_LLM_JUDGE = "llm_judge"

EVAL_LABEL_DETECTED = "detected"
EVAL_LABEL_NOT_DETECTED = "not_detected"
EVAL_LABEL_ABSTAINED = "abstained"

RUNNER_INPROC = "inproc"
RUNNER_SUBPROCESS = "subprocess"
RUNNER_HTTP = "http"


def agent_span_name(agent_name: str) -> str:
    return f"{SPAN_INVOKE_AGENT} {agent_name}"


def workflow_span_name(workflow_name: str) -> str:
    return f"{SPAN_INVOKE_WORKFLOW} {workflow_name}"


def tool_span_name(tool_name: str) -> str:
    return f"{SPAN_EXECUTE_TOOL} {tool_name}"


def chat_span_name(model: str) -> str:
    return f"{SPAN_CHAT} {model}"
