"""Shared pytest fixtures.

Note: ``tmp_path`` is avoided on purpose -- the CI sandbox this project is
developed in forbids deleting directories, and pytest's tmp-dir teardown needs
to.  Everything lives under ``out/`` (git-ignored) instead.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SAMPLES = ROOT / "samples"
# stable, writable, git-ignored scratch area
SCRATCH = ROOT / "out" / "test-scratch"


@pytest.fixture(scope="session")
def samples_dir() -> Path:
    if not SAMPLES.exists() or not any(SAMPLES.iterdir()):
        pytest.skip("samples/ 为空，请先运行 python scripts/make_samples.py")
    return SAMPLES


@pytest.fixture()
def scratch() -> Path:
    path = SCRATCH / uuid.uuid4().hex[:8]
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture()
def store(samples_dir):
    from sailbill.store import BillStore

    return BillStore.from_file(samples_dir / "existing_bills.json")


@pytest.fixture()
def settings(scratch):
    from dataclasses import replace

    from sailbill.config import SETTINGS

    return replace(SETTINGS, offline=True, ocr_cache=True, cache_dir=scratch / "cache")
