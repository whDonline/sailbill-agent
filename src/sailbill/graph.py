"""LangGraph orchestration -- the engine of the agent.

A document flows through a small state machine::

    START → extract → validate → posting → decide → commit → END
                                          ╰──(no charges)────────╯

Why a graph instead of a for-loop:

* **A human can step in.**  ``interrupt_before=["commit"]`` pauses after the
  decisions are made and *before* anything is written to the books; a reviewer
  inspects the proposals and resumes the very same run.
* **A failure has an address.**  Each node is checkpointed, so you can see which
  stage raised and resume instead of re-running OCR and model calls.
* **The flow can branch per document.**  A bill of lading carries no charges, so
  it does not need matching at all.

Implementation note -- the graph channels stay **JSON-native** (a document id and
the trace).  LangGraph's msgpack serde cannot round-trip arbitrary Pydantic
objects: the first version put the whole dataclass in the state and the
checkpoint came back with ``records == []``, so a resumed human-in-the-loop run
silently committed nothing.  The rich state therefore lives in an in-process
registry keyed by ``doc_id`` (see ``docs/graph.md``).
"""

from __future__ import annotations

import time
from typing import Any, Optional, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from .config import Settings
from .extract import extract_document
from .finance import determine_posting
from .matching import match_charge
from .models import BillRecordOut, NormalizedDocument
from .normalize.parties import PartyResolver
from .pipeline import (
    UnitOutcome,
    UnitState,
    build_trace,
    decide_and_build,
    infer_entity,
    pending_record,
)
from .store import BillStore
from .validate import score_charge, validate_extraction


class GraphState(TypedDict, total=False):
    """JSON-native channel: which document is flowing, plus the trace."""

    doc_id: str
    trace: dict[str, Any]


def build_graph(
    *,
    registry: dict[str, UnitState],
    settings: Settings,
    store: BillStore,
    resolver: PartyResolver,
    accounting_entity: Optional[str],
    use_llm: bool,
    commit: bool = True,
    hitl: bool = False,
):
    """Compile the per-document graph.

    Nodes: extract → validate → posting → decide → commit.
    With ``hitl`` the graph pauses *before* ``commit`` and keeps a checkpoint.
    """

    def _ctx(state: GraphState) -> UnitState:
        return registry[state["doc_id"]]

    # -- nodes ------------------------------------------------------------
    def node_extract(state: GraphState) -> dict[str, Any]:
        ctx = _ctx(state)
        docs = [ctx.primary, *ctx.context]
        ctx.extraction = extract_document(
            docs, settings=settings, resolver=resolver, use_llm=use_llm
        )
        return {}

    def node_validate(state: GraphState) -> dict[str, Any]:
        ctx = _ctx(state)
        assert ctx.extraction is not None
        ctx.issues = validate_extraction(ctx.extraction, settings=settings)
        return {}

    def node_posting(state: GraphState) -> dict[str, Any]:
        ctx = _ctx(state)
        assert ctx.extraction is not None
        ctx.entity = (
            accounting_entity
            or settings.accounting_entity
            or infer_entity(ctx.extraction, store, resolver)
        )
        ctx.posting = determine_posting(
            ctx.extraction, accounting_entity=ctx.entity, resolver=resolver
        )
        return {}

    def node_decide(state: GraphState) -> dict[str, Any]:
        ctx = _ctx(state)
        ext, posting = ctx.extraction, ctx.posting
        assert ext is not None and posting is not None

        if not ext.charges:
            ctx.records.append(
                pending_record(ext, posting, ctx.issues, reason="未能从单据中提取到任何费用明细")
            )
            ctx.extras.append(
                {
                    "source_file": ctx.primary.filename,
                    "confidence": 0.0,
                    "issues": [issue.model_dump() for issue in ctx.issues],
                    "warnings": ext.warnings,
                    "extractor": ext.extractor,
                }
            )
            return {}

        for charge in ext.charges:
            outcome = match_charge(charge, ext, store.all(), resolver=resolver)
            confidence = score_charge(
                ext,
                charge,
                issues=ctx.issues,
                match=outcome.match,
                ambiguous_match=outcome.ambiguous,
                settings=settings,
            )
            record, extra = decide_and_build(
                charge,
                ext,
                outcome,
                posting,
                ctx.issues,
                confidence,
                settings=settings,
                primary=ctx.primary,
            )
            ctx.records.append(record)
            ctx.extras.append(extra)
        return {}

    def node_commit(state: GraphState) -> dict[str, Any]:
        ctx = _ctx(state)
        touched = store.apply_records(ctx.records, ctx.extras)
        return {"trace": {"committed_bills": touched}}

    # -- wiring -------------------------------------------------------------
    graph = StateGraph(GraphState)
    graph.add_node("extract", node_extract)
    graph.add_node("validate", node_validate)
    graph.add_node("posting", node_posting)
    graph.add_node("decide", node_decide)
    graph.add_node("commit", node_commit)

    graph.add_edge(START, "extract")
    graph.add_edge("extract", "validate")
    graph.add_edge("validate", "posting")
    graph.add_edge("posting", "decide")
    graph.add_conditional_edges(
        "decide",
        lambda state: "commit" if commit else END,
        {"commit": "commit", END: END},
    )
    if commit:
        graph.add_edge("commit", END)

    return graph.compile(
        checkpointer=MemorySaver(),
        interrupt_before=["commit"] if (hitl and commit) else None,
    )


def run_unit_graph(
    primary: NormalizedDocument,
    context: list[NormalizedDocument],
    *,
    settings: Settings,
    store: BillStore,
    resolver: PartyResolver,
    accounting_entity: Optional[str],
    use_llm: bool,
    hitl: bool = False,
    commit: bool = True,
) -> UnitOutcome:
    """Run one bill unit through the graph and adapt the result."""
    started = time.perf_counter()
    ctx = UnitState(primary=primary, context=list(context))
    registry: dict[str, UnitState] = {primary.doc_id: ctx}

    graph = build_graph(
        registry=registry,
        settings=settings,
        store=store,
        resolver=resolver,
        accounting_entity=accounting_entity,
        use_llm=use_llm,
        commit=commit,
        hitl=hitl,
    )
    config = {"configurable": {"thread_id": primary.doc_id}}
    out = graph.invoke({"doc_id": primary.doc_id}, config=config)
    paused = bool(graph.get_state(config).next)

    outcome = UnitOutcome(
        filename=primary.filename,
        doc_id=primary.doc_id,
        extraction=ctx.extraction,
    )
    outcome.records = list(ctx.records)
    outcome.extras = list(ctx.extras)
    trace = build_trace(ctx, elapsed=time.perf_counter() - started)
    trace["engine"] = "langgraph"
    trace["paused_before_commit"] = paused
    if isinstance(out, dict) and isinstance(out.get("trace"), dict):
        trace.update(out["trace"])
    outcome.trace = trace
    # kept so a reviewer (or a service) can resume the paused run
    outcome.graph = graph
    outcome.graph_config = config
    return outcome


def records_of(outcome: UnitOutcome) -> list[BillRecordOut]:
    """Typed accessor for callers that want validated records back."""
    return [BillRecordOut.model_validate(record.model_dump()) for record in outcome.records]
