"""Tool definitions and role -> allowed-tool-set mapping.

Tool functions are plain async callables: (args, ctx) -> str. spawn_subagent
is wired up separately in graph.py because it needs access to the recursive
runner machinery and the shared run budget.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agenttrace.harness.mockweb import pages
from agenttrace.telemetry.semconv import TAINT_TRUSTED, TAINT_UNTRUSTED

CORPUS_ROOT = Path(__file__).parent / "corpus"


@dataclass(frozen=True)
class ToolResult:
    text: str
    taint: str = TAINT_TRUSTED


ToolFn = Callable[[dict[str, Any]], Awaitable[ToolResult]]


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    json_schema: dict[str, Any]
    fn: ToolFn


async def _search_corpus(args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query", "")).lower()
    hits: list[str] = []
    taint = TAINT_TRUSTED
    for sub in ("trusted", "untrusted"):
        for path in sorted((CORPUS_ROOT / sub).glob("*.txt")):
            text = path.read_text()
            if query in text.lower():
                hits.append(f"{sub}/{path.name}: {text.strip()}")
                if sub == "untrusted":
                    taint = TAINT_UNTRUSTED
    return ToolResult(text="\n".join(hits) if hits else "no results found", taint=taint)


async def _read_file(args: dict[str, Any]) -> ToolResult:
    rel = str(args.get("path", ""))
    path = (CORPUS_ROOT / rel).resolve()
    if CORPUS_ROOT not in path.parents and path != CORPUS_ROOT:
        raise ValueError("path escapes corpus root")
    if not path.exists():
        return ToolResult(text=f"error: file not found: {rel}")
    taint = TAINT_UNTRUSTED if rel.startswith("untrusted/") else TAINT_TRUSTED
    return ToolResult(text=path.read_text(), taint=taint)


async def _web_fetch(args: dict[str, Any]) -> ToolResult:
    url = str(args.get("url", ""))
    return ToolResult(text=pages.fetch(url), taint=TAINT_UNTRUSTED)


SEARCH_CORPUS = ToolDef(
    name="search_corpus",
    description="Search trusted and untrusted corpus files for a query string.",
    json_schema={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
    fn=_search_corpus,
)

READ_FILE = ToolDef(
    name="read_file",
    description="Read a file from the corpus by relative path, e.g. trusted/facts.txt",
    json_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
    fn=_read_file,
)

WEB_FETCH = ToolDef(
    name="web_fetch",
    description="Fetch a URL from the mock web. Content returned is untrusted.",
    json_schema={
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    },
    fn=_web_fetch,
)

# spawn_subagent and submit_result are structurally special (control-flow, not
# pure data tools) and are added per-agent in graph.py, but their schemas live
# here so every role's gen_ai.tool.definitions is assembled from one place.
SPAWN_SUBAGENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "role": {"type": "string", "enum": ["researcher", "verifier"]},
        "objective": {"type": "string"},
    },
    "required": ["role", "objective"],
}

SUBMIT_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"result": {"type": "string"}},
    "required": ["result"],
}

ROLE_TOOLS: dict[str, list[ToolDef]] = {
    "orchestrator": [SEARCH_CORPUS, READ_FILE],
    "researcher": [SEARCH_CORPUS, READ_FILE, WEB_FETCH],
    "verifier": [SEARCH_CORPUS, READ_FILE],
}

ALL_ROLES = tuple(ROLE_TOOLS.keys())
