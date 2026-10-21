"""LangGraph orchestration (optional engine).

The sequential pipeline answers *"what are the steps"*; a graph answers the
harder production questions:

* **Where can a human step in?**  ``interrupt_before=["commit"]`` pauses the run
  after the decisions are made and *before* anything is written to the books.
  A reviewer inspects the proposals and resumes the same graph.
* **Where exactly did a document fail?**  Checkpoints land between stages, so a
  resumed run does not redo OCR or model calls.
* **Can the flow differ per document?**  Conditional edges route a document with
  no charges straight past matching.

Everything is reused from :mod:`sailbill.pipeline` -- the nodes are the *same*
stage functions the classic engine calls in sequence, which is why the two
engines are verified to produce identical records
(``tests/test_graph.py::test_graph_matches_classic``).

Design note: the graph **state holds only JSON-native data** (a document id and
the trace).  The rich objects (``UnitState`` with Pydantic models) live in an
in-process registry keyed by that id, because a checkpointer must be able to
serialise every channel -- and LangGraph's msgpack serde cannot round-trip
arbitrary Pydantic graphs.  That keeps the checkpoint small and portable; the
trade-off is documented in ``docs/graph.md``.

Install with::

    pip install -e ".[graph]"
"""

from __future__ import annotations

import time
from typing import Any, Optional, TypedDict

from .config import Settings
from .models import BillRecordOut, NormalizedDocument
from .normalize.parties import PartyResolver
from .pipeline import (
    UnitOutcome,
    UnitState,
    stage_decide,
    stage_extract,
    stage_posting,
    stage_trace,
    stage_validate,
)
from .store import BillStore


class GraphState(TypedDict, total=False):
    """JSON-native channel: which document is flowing, plus the trace."""

    doc_id: str
    trace: dict[str, Any]


def _require_langgraph() -> None:
    try:
        from langgraph.graph import END, START, StateGraph  # noqa: F401
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            '使用 --engine langgraph 需要安装 LangGraph：pip install -e ".[graph]"'
        ) from exc


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

    Nodes: extract → validate → posting → decide → commit → finish.
    With ``hitl`` the graph pauses *before* ``commit`` and keeps a checkpoint.
    """
    _require_langgraph()
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph

    def _ctx(state: GraphState) -> UnitState:
        return registry[state["doc_id"]]

    def node_extract(state: GraphState) -> dict[str, Any]:
        stage_extract(_ctx(state), settings=settings, resolver=resolver, use_llm=use_llm)
        return {}

    def node_validate(state: GraphState) -> dict[str, Any]:
        stage_validate(_ctx(state), settings=settings)
        return {}

    def node_posting(state: GraphState) -> dict[str, Any]:
        stage_posting(
            _ctx(state),
            settings=settings,
            store=store,
            resolver=resolver,
            accounting_entity=accounting_entity,
        )
        return {}

    def node_decide(state: GraphState) -> dict[str, Any]:
        stage_decide(_ctx(state), settings=settings, store=store, resolver=resolver)
        return {}

    def node_commit(state: GraphState) -> dict[str, Any]:
        ctx = _ctx(state)
        touched = store.apply_records(ctx.records, ctx.extras)
        return {"trace": {"committed_bills": touched}}

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

    checkpointer = MemorySaver() if hitl else None
    return graph.compile(
        checkpointer=checkpointer,
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
    out = graph.invoke({"doc_id": primary.doc_id}, config=config) if hitl else graph.invoke(
        {"doc_id": primary.doc_id}
    )

    paused = bool(graph.get_state(config).next) if hitl else False

    outcome = UnitOutcome(
        filename=primary.filename,
        doc_id=primary.doc_id,
        extraction=ctx.extraction,
    )
    outcome.records = list(ctx.records)
    outcome.extras = list(ctx.extras)
    trace = stage_trace(ctx, elapsed=time.perf_counter() - started)
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
    """Typed accessor used by callers that want validated records back."""
    return [BillRecordOut.model_validate(record.model_dump()) for record in outcome.records]
