"""Optional checks against the *real* assignment samples.

Those files live in ``TestSamples/`` (git-ignored, because they are real
customer documents).  When they are not present -- on CI, or on a fresh clone --
every test here is skipped, so the suite stays runnable everywhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sailbill.config import SETTINGS
from sailbill.pipeline import run_paths
from sailbill.store import BillStore

ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT / "TestSamples"

pytestmark = pytest.mark.skipif(
    not REAL.exists() or not any(REAL.iterdir()),
    reason="TestSamples/ 不存在（真实样本不随仓库分发）",
)


def _find(needle: str) -> Path | None:
    for path in REAL.iterdir():
        if needle in path.name:
            return path
    return None


def test_real_debit_note_arithmetic_closes(samples_dir):
    """The US-style DN: three charges must add up to the printed total."""
    from dataclasses import replace

    target = _find("示例账单2")
    if target is None:
        pytest.skip("未找到 示例账单2.pdf")

    result = run_paths(
        [target],
        settings=replace(SETTINGS, offline=True),
        store=BillStore(),
        use_llm=False,
        accounting_entity="AEBO TCO IMPORT LLC",
    )
    amounts = [record.charge_details.amount for record in result.records]
    assert len(amounts) == 3
    assert round(sum(amounts), 2) == 1410.40
    assert {r.charge_details.currency for r in result.records} == {"USD"}
    assert {r.financial_attributes.posting_type for r in result.records} == {"AP"}


def test_real_debit_note_without_entity_is_not_booked(samples_dir):
    """No accounting entity -> no AR/AP -> must go to review, never default."""
    from dataclasses import replace

    target = _find("示例账单2")
    if target is None:
        pytest.skip("未找到 示例账单2.pdf")
    result = run_paths([target], settings=replace(SETTINGS, offline=True), store=BillStore(), use_llm=False)
    assert all(record.operation.value == "PENDING_REVIEW" for record in result.records)
    assert all(record.financial_attributes.posting_type is None for record in result.records)


def test_real_bill_of_lading_is_not_a_bill(samples_dir):
    """A B/L has no charges: it must be flagged, not forced into records."""
    from dataclasses import replace

    target = _find("示例账单1")
    if target is None:
        pytest.skip("未找到 示例账单1.pdf")
    result = run_paths([target], settings=replace(SETTINGS, offline=True), store=BillStore(), use_llm=False)
    assert all(record.operation.value == "PENDING_REVIEW" for record in result.records)
    reasoning = " ".join(result.records[0].audit_trail.ai_reasoning)
    assert "NO_CHARGE" in reasoning or "费用明细" in reasoning
