"""Tests for code-level verification of real Python functions."""

import pytest

from apex.pyverify import UnsupportedConstructError, verify_function

CORRECT_ABS_DIFF = """
def abs_diff(a, b):
    if a > b:
        return a - b
    else:
        return b - a
"""

BUGGY_DIFF = """
def diff(a, b):
    return a - b
"""

MAX3 = """
def max3(a, b, c):
    if a >= b and a >= c:
        return a
    if b >= c:
        return b
    return c
"""

BUGGY_MAX3 = """
def max3(a, b, c):
    if a > b:
        return a
    return b
"""

CLAMP = """
def clamp(x):
    return 0 if x < 0 else (10 if x > 10 else x)
"""


def test_proves_abs_diff_nonnegative_for_all_inputs():
    result = verify_function(CORRECT_ABS_DIFF, "result >= 0")
    assert result.proven is True
    assert result.counterexample is None


def test_refutes_buggy_diff_with_concrete_inputs():
    result = verify_function(BUGGY_DIFF, "result >= 0")
    assert result.proven is False
    ce = result.counterexample
    assert ce is not None
    # The counterexample must ACTUALLY violate the property when run.
    assert ce["a"] - ce["b"] < 0


def test_proves_max3_with_fallthrough_ifs():
    result = verify_function(MAX3, "result >= a and result >= b and result >= c")
    assert result.proven is True


def test_refutes_buggy_max3_missing_c():
    result = verify_function(BUGGY_MAX3, "result >= a and result >= b and result >= c")
    assert result.proven is False
    ce = result.counterexample
    # Real violating inputs: c exceeds the returned value.
    real = max(ce["a"], ce["b"])
    assert ce["c"] > real or real < ce["c"] or not (
        real >= ce["a"] and real >= ce["b"] and real >= ce["c"]
    )


def test_proves_ternary_clamp_bounds():
    result = verify_function(CLAMP, "result >= 0 and result <= 10")
    assert result.proven is True


def test_rejects_loops_fail_closed():
    src = "def f(n):\n    total = 0\n    for i in range(n):\n        total = total + i\n    return total\n"
    with pytest.raises(UnsupportedConstructError):
        verify_function(src, "result >= 0")


def test_rejects_calls_fail_closed():
    src = "def f(x):\n    return abs(x)\n"
    with pytest.raises(UnsupportedConstructError):
        verify_function(src, "result >= 0")


def test_rejects_missing_return_path():
    src = "def f(x):\n    if x > 0:\n        return x\n"
    with pytest.raises(UnsupportedConstructError):
        verify_function(src, "result >= 0")
