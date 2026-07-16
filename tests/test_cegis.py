"""Tests for the CEGIS counterexample-guided repair loop (offline)."""

import pytest

from apex.cegis import CEGISLoop
from apex.llm_client import MockLLMClient

SAFE_SPEC = "(declare-const x Int)(assert (> x 0))(assert (< x 0))(check-sat)"
VIOLATED_SPEC = "(declare-const x Int)(assert (> x 10))(check-sat)"
BROKEN_SPEC = "(this is not smt"


@pytest.mark.asyncio
async def test_repairs_after_counterexample():
    # Round 1: violated. The repair call returns a safe spec -> round 2 passes.
    client = MockLLMClient(queue=[SAFE_SPEC])
    loop = CEGISLoop(client, max_rounds=4)
    result = await loop.refine("x is impossible", initial_spec=VIOLATED_SPEC)
    assert result.passed is True
    assert result.rounds_used == 2
    assert result.history[0].outcome == "VIOLATED"
    assert "counterexample" in result.history[0].feedback.lower()
    # The repair prompt must contain the concrete counterexample values.
    assert any("x" in call for call in client.calls)


@pytest.mark.asyncio
async def test_gives_up_after_max_rounds_fail_closed():
    # The "model" never fixes it: always returns a violated spec.
    client = MockLLMClient(default=VIOLATED_SPEC)
    loop = CEGISLoop(client, max_rounds=3)
    result = await loop.refine("hopeless", initial_spec=VIOLATED_SPEC)
    assert result.passed is False
    assert result.rounds_used == 3
    assert len(result.history) == 3
    assert all(r.outcome == "VIOLATED" for r in result.history)


@pytest.mark.asyncio
async def test_repairs_parse_error():
    client = MockLLMClient(queue=[SAFE_SPEC])
    loop = CEGISLoop(client, max_rounds=4)
    result = await loop.refine("prop", initial_spec=BROKEN_SPEC)
    assert result.passed is True
    assert result.history[0].outcome == "PARSE_ERROR"


@pytest.mark.asyncio
async def test_immediate_safe_needs_no_repair():
    client = MockLLMClient()
    loop = CEGISLoop(client, max_rounds=4)
    result = await loop.refine("prop", initial_spec=SAFE_SPEC)
    assert result.passed is True
    assert result.rounds_used == 1
    assert client.calls == []  # model never consulted
