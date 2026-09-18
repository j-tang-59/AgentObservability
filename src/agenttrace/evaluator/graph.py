"""Causal trace graph (E4): control edges from span parentage + spawn
records, data edges recovered by chunk-level hashing of tool
args/result content.

Why chunk hashing and not embeddings: our harness's real data flows are
verbatim — a tool's result text is echoed into a later tool call's args (a
naive "incorporate the fetched page" pattern, or the exact mechanism a child
agent's result becomes its parent's spawn_subagent tool-span result). Chunk
hashing catches all of that with zero false links; it cannot see an LLM
paraphrasing content in its own words (E4's "paraphrase gap"), which is left
to the judge (E6) rather than papered over with embedding similarity that
would invent edges that aren't really there.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import networkx as nx

from agenttrace.evaluator.loader import TraceLoader
from agenttrace.telemetry import semconv
from agenttrace.trace_model import SpanRecord

CHUNK_WORDS = 4
MIN_CHUNK_CHARS = 8


def normalize(text: str) -> str:
    return " ".join(text.split()).lower()


def extract_arg_strings(raw_json_args: str) -> str:
    """Tool args are stored as a JSON-serialized dict (D11); the JSON
    punctuation around a string value corrupts chunk boundaries at the
    start/end of the value, so pull out just the string leaves before
    chunking rather than hashing the raw JSON blob."""
    try:
        parsed = json.loads(raw_json_args)
    except (json.JSONDecodeError, TypeError):
        return raw_json_args

    strings: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, str):
            strings.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(parsed)
    return " ".join(strings)


def chunks_of(text: str) -> set[str]:
    """Sliding (stride-1) word windows, not fixed non-overlapping blocks —
    a shared substring between producer and consumer text can start at any
    word offset in either string, and a fixed stride would silently miss
    verbatim overlaps that don't happen to land on the same boundary."""
    words = normalize(text).split(" ")
    out: set[str] = set()
    if len(words) <= CHUNK_WORDS:
        joined = " ".join(words)
        if len(joined) >= MIN_CHUNK_CHARS:
            out.add(joined)
        return out
    for i in range(len(words) - CHUNK_WORDS + 1):
        chunk = " ".join(words[i : i + CHUNK_WORDS])
        if len(chunk) >= MIN_CHUNK_CHARS:
            out.add(chunk)
    return out


@dataclass
class CausalTrace:
    spans: list[SpanRecord]
    graph: nx.DiGraph[str]
    by_id: dict[str, SpanRecord] = field(default_factory=dict)

    def children(self, span_id: str) -> list[SpanRecord]:
        return [self.by_id[c] for c in self.graph.successors(span_id) if self.graph[span_id][c].get("kind") == "control"]

    def descendants(self, span_id: str) -> list[SpanRecord]:
        return [self.by_id[d] for d in nx.descendants(self.graph, span_id)]

    def data_consumers(self, span_id: str) -> list[SpanRecord]:
        return [
            self.by_id[c]
            for c in self.graph.successors(span_id)
            if self.graph[span_id][c].get("kind") == "data"
        ]

    def data_producers(self, span_id: str) -> list[SpanRecord]:
        return [
            self.by_id[p]
            for p in self.graph.predecessors(span_id)
            if self.graph[p][span_id].get("kind") == "data"
        ]

    def tool_spans(self) -> list[SpanRecord]:
        return [s for s in self.spans if s.name.startswith(semconv.SPAN_EXECUTE_TOOL)]

    def agent_spans(self) -> list[SpanRecord]:
        return [s for s in self.spans if s.name.startswith(semconv.SPAN_INVOKE_AGENT)]

    def enclosing_agent(self, span_id: str) -> SpanRecord | None:
        """Walk up control-parents to the nearest invoke_agent span."""
        current = self.by_id.get(span_id)
        seen: set[str] = set()
        while current and current.parent_span_id and current.parent_span_id not in seen:
            seen.add(current.parent_span_id)
            parent = self.by_id.get(current.parent_span_id)
            if parent is None:
                return None
            if parent.name.startswith(semconv.SPAN_INVOKE_AGENT):
                return parent
            current = parent
        return None


def build_causal_trace(spans: list[SpanRecord], loader: TraceLoader) -> CausalTrace:
    g: nx.DiGraph[str] = nx.DiGraph()
    by_id = {s.span_id: s for s in spans}
    for s in spans:
        g.add_node(s.span_id)
    for s in spans:
        if s.parent_span_id and s.parent_span_id in by_id:
            g.add_edge(s.parent_span_id, s.span_id, kind="control")

    # Data edges: chunk-hash every tool span's resolvable result content
    # against every OTHER tool span's args content that becomes known
    # later. Ordered by END time, not start time: a result (including a
    # spawn_subagent span's result, which IS the child's returned value) is
    # only fully known when its span ends, so end-time order is what makes
    # a child's submit_result a "producer" for its own parent's
    # spawn_subagent span — the parent's tool span starts first but ends
    # only after the child (and its result) is fully known.
    tool_spans = sorted(
        (s for s in spans if s.name.startswith(semconv.SPAN_EXECUTE_TOOL)),
        key=lambda s: s.end_time_unix_nano,
    )

    for i, producer in enumerate(tool_spans):
        producer_result_ref = producer.attributes.get(semconv.AT_TOOL_RESULT_REF)
        producer_result = loader.resolve_ref(producer_result_ref) if isinstance(producer_result_ref, str) else None
        if not producer_result:
            continue
        producer_chunks = chunks_of(producer_result)
        if not producer_chunks:
            continue
        for consumer in tool_spans[i + 1 :]:
            if consumer.span_id == producer.span_id:
                continue
            consumer_args_ref = consumer.attributes.get(semconv.AT_TOOL_ARGS_REF)
            consumer_args_raw = loader.resolve_ref(consumer_args_ref) if isinstance(consumer_args_ref, str) else None
            if not consumer_args_raw:
                continue
            consumer_chunks = chunks_of(extract_arg_strings(consumer_args_raw))
            if producer_chunks & consumer_chunks:
                g.add_edge(producer.span_id, consumer.span_id, kind="data")

    return CausalTrace(spans=spans, graph=g, by_id=by_id)
