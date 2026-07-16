"""Tests for the real Layer 3 bandit hive.

Proves the hive can FAIL: a deliberately vulnerable fixture must be flagged,
and a clean fixture must pass. Requires the `bandit` CLI (in requirements).
"""

import importlib.util
from pathlib import Path

import pytest

from apex.layer3_hive import BanditHive, shard_paths

FIXTURES = Path(__file__).parent / "fixtures"

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("bandit") is None, reason="bandit not installed"
)


def test_shard_paths_groups_files():
    modules = shard_paths(FIXTURES)
    assert modules  # found python files
    all_files = [f for files in modules.values() for f in files]
    assert any(f.name == "bad_code.py" for f in all_files)


@pytest.mark.asyncio
async def test_hive_flags_vulnerable_code():
    hive = BanditHive(high_severity_budget=0)
    report = await hive.run(FIXTURES / "vulnerable")
    assert report.high_severity_count >= 1
    assert report.passed is False
    # The specific injection finding should be present.
    assert any("B602" in f.test_id or "shell" in f.message.lower()
               for f in report.all_findings())


@pytest.mark.asyncio
async def test_hive_passes_clean_code():
    hive = BanditHive(high_severity_budget=0)
    report = await hive.run(FIXTURES / "clean")
    assert report.high_severity_count == 0
    assert report.crashed_count == 0
    assert report.passed is True
