"""Flagship Layer 2 demo: prove/refute deadlock-freedom with real Z3.

Scenario: two threads, two locks (A and B). A circular wait - and therefore a
potential deadlock - exists when one thread acquires A-then-B while the other
acquires B-then-A. We encode the NEGATION of "deadlock-free" (i.e. "a circular
wait is possible") and let Z3 decide:

  * BAD ordering  -> Z3 finds the circular wait  -> VIOLATED (counterexample)
  * SAFE ordering -> no circular wait possible    -> SAFE (proved)

Run:
    python -m examples.deadlock_check
"""

from apex.layer2_z3 import Z3Verifier, Verdict

# Each thread's lock acquisition order is modeled as two integer "priorities".
# A deadlock (circular wait) is possible iff the two threads acquire the two
# locks in opposite relative order. We assert that such a circular wait exists.
#
# t1_first < t1_second  means thread 1 takes the lock with the smaller number
# first. Circular wait exists when the threads disagree on ordering.

BAD_ORDERING_SPEC = """
(declare-const t1_A Int)
(declare-const t1_B Int)
(declare-const t2_A Int)
(declare-const t2_B Int)
; Thread 1 acquires A before B  (A has lower acquisition index)
(assert (< t1_A t1_B))
; Thread 2 acquires B before A  (opposite order) -> circular wait
(assert (< t2_B t2_A))
; A circular wait is reachable: t1 holds A wants B, t2 holds B wants A
(assert (and (< t1_A t1_B) (< t2_B t2_A)))
(check-sat)
"""

# In the SAFE design, BOTH threads must acquire locks in the SAME global order
# (a standard deadlock-prevention technique: total lock ordering). We assert a
# circular wait exists AND the global-order rule holds - which is impossible.
SAFE_ORDERING_SPEC = """
(declare-const t1_A Int)
(declare-const t1_B Int)
(declare-const t2_A Int)
(declare-const t2_B Int)
; Global lock ordering enforced: EVERY thread takes A before B.
(assert (< t1_A t1_B))
(assert (< t2_A t2_B))
; Try to also satisfy a circular wait (t2 takes B before A). Contradiction.
(assert (< t2_B t2_A))
(check-sat)
"""


def main() -> None:
    verifier = Z3Verifier()

    print("=" * 60)
    print("CASE 1: Inconsistent lock ordering (should be VIOLATED)")
    print("=" * 60)
    bad = verifier.check(BAD_ORDERING_SPEC)
    print(f"Verdict: {bad.verdict.value}")
    print(f"Passed : {bad.passed}")
    if bad.counterexample:
        print(f"Counterexample (deadlock witness): {bad.counterexample}")

    print()
    print("=" * 60)
    print("CASE 2: Enforced global lock ordering (should be SAFE)")
    print("=" * 60)
    safe = verifier.check(SAFE_ORDERING_SPEC)
    print(f"Verdict: {safe.verdict.value}")
    print(f"Passed : {safe.passed}")

    print()
    ok = bad.verdict == Verdict.VIOLATED and safe.verdict == Verdict.SAFE
    print("DEMO RESULT:", "PASS - Z3 distinguished safe from unsafe" if ok else "UNEXPECTED")


if __name__ == "__main__":
    main()
