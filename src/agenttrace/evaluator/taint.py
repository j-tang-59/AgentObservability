"""Forward taint propagation along data edges (E4).

Sources are tool spans whose agenttrace.taint attribute is "untrusted"
(set at capture time by the harness — web_fetch and untrusted-corpus reads,
per D11). Taint propagates forward along data edges, including up through a
spawn_subagent span whose result chunk-matches a tainted descendant's
output — so a parent that consumes a child's tainted result is itself
exposed, exactly as E4 specifies.
"""

from __future__ import annotations

from agenttrace.evaluator.graph import CausalTrace
from agenttrace.telemetry.semconv import AT_TAINT, TAINT_UNTRUSTED


def compute_tainted_span_ids(trace: CausalTrace) -> set[str]:
    tainted: set[str] = {
        s.span_id for s in trace.tool_spans() if s.attributes.get(AT_TAINT) == TAINT_UNTRUSTED
    }

    changed = True
    while changed:
        changed = False
        for span_id in list(tainted):
            for consumer in trace.data_consumers(span_id):
                if consumer.span_id not in tainted:
                    tainted.add(consumer.span_id)
                    changed = True

    return tainted
