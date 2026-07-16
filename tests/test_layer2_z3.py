"""Tests proving Layer 2 does REAL verification and can REJECT.

The whole point: unlike the original string-matching mock, this layer must be
able to say NO. These tests exercise both outcomes plus error handling.
"""

import pytest

from apex.layer2_z3 import (
    SpecCompilationError,
    SpecGenerator,
    Verdict,
    Z3Verifier,
    verify_property,
)
from apex.llm_client import MockLLMClient
from examples.deadlock_check import BAD_ORDERING_SPEC, SAFE_ORDERING_SPEC


def test_safe_property_is_proved():
    # x > 0 AND x < 0 has no solution -> negation unsat -> SAFE.
    spec = "(declare-const x Int)(assert (> x 0))(assert (< x 0))(check-sat)"
    report = Z3Verifier().check(spec)
    assert report.verdict == Verdict.SAFE
    assert report.passed is True


def test_violation_yields_counterexample():
    # x > 10 is satisfiable -> negation sat -> VIOLATED, with a witness.
    spec = "(declare-const x Int)(assert (> x 10))(check-sat)"
    report = Z3Verifier().check(spec)
    assert report.verdict == Verdict.VIOLATED
    assert report.passed is False
    assert report.counterexample is not None
    assert "x" in report.counterexample


def test_deadlock_bad_ordering_is_violated():
    report = Z3Verifier().check(BAD_ORDERING_SPEC)
    assert report.verdict == Verdict.VIOLATED
    assert report.counterexample  # concrete deadlock witness


def test_deadlock_safe_ordering_is_safe():
    report = Z3Verifier().check(SAFE_ORDERING_SPEC)
    assert report.verdict == Verdict.SAFE
    assert report.passed is True


def test_malformed_spec_raises():
    with pytest.raises(SpecCompilationError):
        Z3Verifier().check("(this is not valid smt")


def test_empty_spec_raises():
    with pytest.raises(SpecCompilationError):
        Z3Verifier().check("   ")


@pytest.mark.asyncio
async def test_verify_property_end_to_end_with_mock_llm():
    # The "LLM" returns a real safe spec; Z3 does the actual deciding.
    safe_spec = "(declare-const x Int)(assert (> x 0))(assert (< x 0))(check-sat)"
    client = MockLLMClient(default=safe_spec)
    report = await verify_property(client, "x must be both positive and negative")
    assert report.verdict == Verdict.SAFE


@pytest.mark.asyncio
async def test_generator_strips_code_fences():
    fenced = "```smt\n(declare-const x Int)(assert (> x 0))(check-sat)\n```"
    client = MockLLMClient(default=fenced)
    spec = await SpecGenerator(client).generate("x positive")
    assert not spec.startswith("```")
    # And it must still be valid for Z3.
    report = Z3Verifier().check(spec)
    assert report.verdict in (Verdict.SAFE, Verdict.VIOLATED)
