"""Tests for bounded verification (loops, calls, lists) and bounded equivalence."""

import pytest

from apex.pybounded import BoundedOptions, check_equivalence, verify_bounded
from apex.pyverify import UnsupportedConstructError

SUM_TO = """
def sum_to(n):
    total = 0
    for i in range(n):
        total += i
    return total
"""

BUGGY_MAX = """
def list_max(xs):
    best = 0
    for x in xs:
        if x > best:
            best = x
    return best
"""

GOOD_MAX = """
def list_max(xs):
    best = xs[0]
    for x in xs[1:]:
        if x > best:
            best = x
    return best
"""

FACT = """
def fact(n):
    if n <= 1:
        return 1
    return n * fact(n - 1)
"""

MAX_POST = "all(result >= x for x in xs) and result in xs"


def test_bounded_loop_is_proved_inside_the_domain():
    r = verify_bounded(SUM_TO, "sum_to", "n: int", "2 * result == n * (n - 1)", pre="0 <= n <= 6")
    assert r.status == "proved_up_to_bound"
    assert r.bound == 8


def test_needing_more_than_k_iterations_is_a_counterexample_not_a_proof():
    r = verify_bounded(SUM_TO, "sum_to", "n: int", "result >= 0", pre="0 <= n <= 20")
    assert r.status == "refuted"
    assert r.outcome == "bound"
    assert r.counterexample["n"] > 8


def test_list_bug_gives_a_real_counterexample():
    r = verify_bounded(BUGGY_MAX, "list_max", "xs: list[int]", MAX_POST, pre="len(xs) >= 1")
    assert r.status == "refuted"
    xs = r.counterexample["xs"]
    assert len(xs) >= 1 and max(xs) < 0   # the real bug: all-negative lists return 0


def test_correct_list_max_is_proved_up_to_bound():
    r = verify_bounded(GOOD_MAX, "list_max", "xs: list[int]", MAX_POST, pre="len(xs) >= 1",
                       options=BoundedOptions(list_cap=4))
    assert r.status == "proved_up_to_bound"


def test_index_error_is_reported_as_the_outcome():
    r = verify_bounded(GOOD_MAX, "list_max", "xs: list[int]", MAX_POST)
    assert r.status == "refuted"
    assert r.outcome == "IndexError"
    assert r.counterexample == {"xs": []}


def test_recursion_is_inlined_up_to_the_bound():
    r = verify_bounded(FACT, "fact", "n: int", "result >= n", pre="1 <= n <= 5")
    assert r.status == "proved_up_to_bound"


def test_extension_flags_fail_closed():
    with pytest.raises(UnsupportedConstructError, match="calls"):
        verify_bounded(FACT, "fact", "n: int", "result >= 1", options=BoundedOptions(calls=False))
    with pytest.raises(UnsupportedConstructError, match="loops"):
        verify_bounded(SUM_TO, "sum_to", "n: int", "result >= 0", options=BoundedOptions(loops=False))


@pytest.mark.parametrize("src, args, reason", [
    ("def f(x):\n    return x * 0.5\n", "x: int", "float"),
    ("def f(xs):\n    for x in xs:\n        yield x\n", "xs: list[int]", "generator"),
    ("def f(n):\n    return n\n", "n: node", "Node"),
    ("def f(xs):\n    ys = xs\n    ys.append(1)\n    return len(xs)\n", "xs: list[int]", "aliasing"),
])
def test_unsupported_constructs_are_rejected_with_a_reason(src, args, reason):
    with pytest.raises(UnsupportedConstructError, match=reason):
        verify_bounded(src, "f", args, "True")


def test_equivalence_finds_a_distinguishing_input():
    r = check_equivalence(BUGGY_MAX, GOOD_MAX, "list_max", "xs: list[int]", pre="len(xs) >= 1",
                          options=BoundedOptions(list_cap=3))
    assert r.status == "different"
    xs = r.counterexample["xs"]
    assert r.predicted_a == 0 and r.predicted_b == max(xs) < 0


def test_equivalence_of_a_program_with_itself():
    r = check_equivalence(GOOD_MAX, GOOD_MAX, "list_max", "xs: list[int]",
                          options=BoundedOptions(list_cap=3))
    assert r.status == "equivalent_up_to_bound"
