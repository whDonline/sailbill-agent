"""LangGraph engine: parity with the classic engine, commit, and HITL pause.

The parity test is the important one -- it pins the graph to the sequential
pipeline so a refactor of either side cannot silently change the books.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from sailbill.config import SETTINGS
from sailbill.output import as_json, summarize
from sailbill.pipeline import run_paths
from sailbill.store import BillStore

pytest.importorskip("langgraph", reason="需要 pip install -e '.[graph]'")

SAMPLE_FILES = [
    "debit_note.pdf",
    "debit_note.xlsx",
    "credit_note.docx",
    "booking_email.eml",
    "ar_debit_note.pdf",
    "pending_debit_note.pdf",
]


def _settings(settings):
    return replace(settings, offline=True)


def _paths(samples_dir):
    return [samples_dir / name for name in SAMPLE_FILES]


# --------------------------------------------------------------------------- #
# parity
# --------------------------------------------------------------------------- #
def test_graph_matches_classic(samples_dir, settings):
    classic = run_paths(
        _paths(samples_dir),
        settings=_settings(settings),
        store=BillStore(),
        use_llm=False,
        engine="classic",
    )
    graph = run_paths(
        _paths(samples_dir),
        settings=_settings(settings),
        store=BillStore(),
        use_llm=False,
        engine="langgraph",
    )
    assert [as_json(r) for r in classic.records] == [as_json(r) for r in graph.records]
    assert summarize(classic.records) == summarize(graph.records)
    assert all(unit.trace.get("engine") == "langgraph" for unit in graph.units)


def test_graph_trace_marks_engine(samples_dir, settings):
    result = run_paths(
        [_paths(samples_dir)[0]],
        settings=_settings(settings),
        store=BillStore(),
        use_llm=False,
        engine="langgraph",
    )
    trace = result.traces[0]
    assert trace["engine"] == "langgraph"
    assert trace["paused_before_commit"] is False
    assert trace["charges"] == 5


# --------------------------------------------------------------------------- #
# commit
# --------------------------------------------------------------------------- #
def test_commit_writes_create_and_update_only(samples_dir, settings):
    store = BillStore.from_file(samples_dir / "existing_bills.json")
    before = len(store)
    result = run_paths(
        [samples_dir / "debit_note.pdf"],
        settings=_settings(settings),
        store=store,
        use_llm=False,
        engine="langgraph",
        commit=True,
    )
    operations = [record.operation.value for record in result.records]
    assert operations.count("NO_CHANGE") == 2
    # the update landed on the matched bill, not on a new one
    assert store.get("B-1001") is not None
    thc = [line for line in store.get("B-1001").lines if line.charge_item == "Terminal Handling Charge"]
    assert thc and thc[0].amount == 300.0
    # the two CREATE rows go to an AUTO bill for this reference number
    assert len(store) == before + 1
    created = store.get("AUTO-DN-202609-001")
    assert created is not None
    assert {line.charge_item for line in created.lines} == {"Customs Clearance Fee", "Handling Charge"}


def test_commit_is_idempotent(samples_dir, settings):
    store = BillStore.from_file(samples_dir / "existing_bills.json")
    kwargs = dict(
        settings=_settings(settings), store=store, use_llm=False, engine="langgraph", commit=True
    )
    run_paths([samples_dir / "debit_note.pdf"], **kwargs)
    after_first = len(store.all())
    lines_first = sum(len(bill.lines) for bill in store.all())
    run_paths([samples_dir / "debit_note.pdf"], **kwargs)
    assert len(store.all()) == after_first
    assert sum(len(bill.lines) for bill in store.all()) == lines_first


def test_pending_review_is_never_committed(samples_dir, settings):
    store = BillStore()
    result = run_paths(
        [samples_dir / "pending_debit_note.pdf"],
        settings=_settings(settings),
        store=store,
        use_llm=False,
        engine="langgraph",
        commit=True,
    )
    assert all(record.operation.value == "PENDING_REVIEW" for record in result.records)
    assert len(store) == 0


def test_empty_store_instance_is_not_replaced(samples_dir, settings):
    """Regression: ``BillStore`` had ``__len__`` but no ``__bool__``, so an empty
    store was falsy and ``store or BillStore()`` threw the caller's object away --
    commits landed in a store nobody could read."""
    store = BillStore()
    assert store  # truthy even when empty
    run_paths(
        [samples_dir / "debit_note.pdf"],
        settings=_settings(settings),
        store=store,
        use_llm=False,
        accounting_entity="XYZ Freight Forwarding Ltd",
        engine="classic",
        commit=True,
    )
    assert len(store) == 1  # one AUTO bill holding all five CREATE rows
    created = store.get("AUTO-DN-202609-001")
    assert created is not None
    assert len(created.lines) == 5
    assert abs(sum(line.amount for line in created.lines) - 3220.00) <= 0.01


# --------------------------------------------------------------------------- #
# human in the loop
# --------------------------------------------------------------------------- #
def test_hitl_pauses_before_commit_and_resumes(samples_dir, settings):
    store = BillStore.from_file(samples_dir / "existing_bills.json")
    before = len(store)
    result = run_paths(
        [samples_dir / "debit_note.pdf"],
        settings=_settings(settings),
        store=store,
        use_llm=False,
        engine="langgraph",
        hitl=True,
        commit=True,
    )
    unit = result.units[0]
    assert unit.trace["paused_before_commit"] is True
    assert len(store) == before  # nothing booked yet
    assert len(unit.records) == 5  # proposals are ready for review

    # a reviewer resumes the paused run: now the writes go through
    unit.graph.invoke(None, config=unit.graph_config)
    assert len(store) == before + 1
    assert store.get("B-1001") is not None


def test_hitl_without_commit_does_not_pause(samples_dir, settings):
    store = BillStore()
    result = run_paths(
        [samples_dir / "debit_note.pdf"],
        settings=_settings(settings),
        store=store,
        use_llm=False,
        engine="langgraph",
        hitl=True,
        commit=False,
    )
    assert result.units[0].trace["paused_before_commit"] is False
