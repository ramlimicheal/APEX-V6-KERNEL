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


# --- Review fixes (PR #1): one test per soundness fix ---------------------------

def test_variable_bound_on_some_paths_only_raises_unbound_local():
    src = "def f(x):\n    if x:\n        y = 1\n    return y\n"
    r = verify_bounded(src, "f", "x: int", "result == 1")
    assert r.status == "refuted"
    assert r.outcome == "UnboundLocalError" and r.counterexample == {"x": 0}


def test_mutating_a_row_shared_through_a_shallow_copy_is_rejected():
    src = ("def f(n):\n    m = [[1]]\n    row = list(m)[0]\n    row.append(2)\n"
           "    return len(m[0])\n")   # Python: 2 (the row is shared)
    with pytest.raises(UnsupportedConstructError, match="aliasing"):
        verify_bounded(src, "f", "n: int", "result == 1")


def test_append_reads_the_list_after_evaluating_its_argument():
    src = "def f(n):\n    xs = [1, 2]\n    xs.append(xs.pop())\n    return len(xs)\n"
    assert verify_bounded(src, "f", "n: int", "result == 2").status == "proved_up_to_bound"


def test_side_effects_inside_a_comprehension_are_rejected():
    src = "def f(n):\n    xs = []\n    ys = [xs.append(1) for _ in range(1)]\n    return len(xs)\n"
    with pytest.raises(UnsupportedConstructError, match="comprehension"):
        verify_bounded(src, "f", "n: int", "result == 0")


def test_side_effects_in_a_skipped_operand_are_rejected():
    src = "def f(flag):\n    xs = []\n    flag and xs.append(1)\n    return len(xs)\n"
    with pytest.raises(UnsupportedConstructError, match="short-circuit"):
        verify_bounded(src, "f", "flag: int", "result == 0")


def test_spec_helper_past_its_bound_is_unsupported_not_refuted():
    helpers = "def count_up(n):\n    i = 0\n    while i < n:\n        i += 1\n    return i\n"
    r = verify_bounded("def f(x):\n    return x\n", "f", "x: int", "count_up(x) == result",
                       pre="0 <= x <= 100", helpers=helpers, options=BoundedOptions(spec_bound=8))
    assert r.status == "unsupported"


def test_any_stops_at_the_first_true_element():
    src = "def f(xs):\n    return any(1 // x > 0 for x in xs)\n"
    r = verify_bounded(src, "f", "xs: list[int]", "result", pre="xs == [1, 0]")
    assert r.status == "proved_up_to_bound"   # Python never evaluates 1 // 0 here


def test_bound_counts_nested_calls_not_the_entry_function():
    src = "def h(n):\n    return n + 1\ndef g(n):\n    return h(n)\ndef f(n):\n    return g(n)\n"
    one = BoundedOptions(bound=1)
    assert verify_bounded(src, "g", "n: int", "result == n + 1", options=one).status == "proved_up_to_bound"
    r = verify_bounded(src, "f", "n: int", "result == n + 1", options=one)
    assert r.status == "refuted" and r.outcome == "bound"


def test_builtins_propagate_a_dead_path_like_len():
    r = verify_bounded("def f(n):\n    return abs([][0])\n", "f", "n: int", "result >= 0")
    assert r.status == "refuted" and r.outcome == "IndexError"


def test_gcd_spec_requires_zero_for_zero_inputs():
    from bench.specs import is_gcd
    assert is_gcd(0, 0, 0)
    assert not is_gcd(5, 0, 0)


def test_str_inputs_are_printable_ascii_only():
    # The modeled domain: str inputs of length <= cap made of characters with codes 32..126.
    src = "def f(s):\n    for ch in s:\n        if ch == '\\n':\n            return 1\n    return 0\n"
    assert verify_bounded(src, "f", "s: str", "result == 0").status == "proved_up_to_bound"
    r = verify_bounded("def f(s):\n    return s[0]\n", "f", "s: str", "True")
    assert r.status == "refuted" and r.counterexample == {"s": ""}


def test_decorated_functions_are_rejected():
    src = "@staticmethod\ndef f(x):\n    return x\n"
    with pytest.raises(UnsupportedConstructError, match="decorated"):
        verify_bounded(src, "f", "x: int", "result == x")


def test_closure_over_a_changing_variable_is_rejected():
    src = ("def f(x):\n    def helper():\n        return x\n    if x > 0:\n        x += 1\n"
           "    return helper()\n")
    with pytest.raises(UnsupportedConstructError, match="closures"):
        verify_bounded(src, "f", "x: int", "result == x")
