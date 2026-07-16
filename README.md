# APEX-REAL — A Working LLM Verification Pipeline

**Author**: Ramli T. Michael
**Status**: Working prototype (rebuilt from the original APEX-V6 concept)
**License**: MIT

A four-stage pipeline that wraps an LLM in external, deterministic checks so its
output is verified instead of trusted. Every stage does real work and — this is
the important part — **every stage can actually fail and halt the pipeline.**

> This is a rebuild. The original APEX-V6 layers were mocks (Layer 1 returned a
> hardcoded `0.98`, Layer 2 string-matched `"UNSAT"`, Layer 3 slept and returned
> `VERIFIED_SECURE`, Layer 4 returned constant JSON). Those have been replaced
> with functioning implementations. See "Honesty / Scope" below.

## What each layer really does
- **Layer 1 — MCTS design search** (`apex/layer1_mcts.py`): a real Monte Carlo
  Tree Search (selection via UCB1, LLM-driven expansion, LLM value scoring,
  backpropagation) over design candidates, bounded by node / call / time budgets.
- **Layer 2 — Z3 formal verification** (`apex/layer2_z3.py`): uses the real
  `z3-solver`. An LLM emits SMT-LIB v2 for the *negation* of a safety property;
  Z3 decides it. `unsat` → property proven safe; `sat` → returns a concrete
  counterexample; malformed SMT is rejected.
- **Layer 3 — static-analysis hive** (`apex/layer3_hive.py`): shards a target
  directory and runs the real `bandit` analyzer per shard concurrently,
  aggregating typed findings. Fails on high-severity findings or worker crashes.
- **Layer 4 — adversarial gate** (`apex/layer4_gate.py`): a real LLM adversarial
  review with **prompt-injection hardening** (payload quarantine + delimited
  inert-data block) and fail-closed behavior on breach, malformed output, or
  timeout.
- **Orchestrator** (`apex/orchestrator.py`): runs `L1 → L2 → L3 → L4` in the
  correct dependency order with per-layer metrics and a structured result.

## Advanced modes
These are real techniques from formal-methods / synthesis research, not the
original "Mirrorforge" fiction:
- **Code-level verification** (`apex/pyverify.py`): symbolically executes a real
  Python function (AST → Z3) and proves a postcondition for **all** integer
  inputs, or returns concrete failing inputs. Fail-closed on any unsupported
  construct (loops, calls, floats, …).
  ```bash
  python -m apex --verify-code examples/buggy_max.py \
    --postcondition "result >= a and result >= b"
  # -> REFUTED: counterexample {'a': -1, 'b': 0, 'result': -1}
  ```
- **CEGIS spec repair** (`apex/cegis.py`): when Z3 rejects an LLM-written spec,
  the concrete counterexample (or parser error) is fed back and the model must
  produce a repaired spec, bounded by a round limit. Enable with `--cegis`.
- **Multi-adversary consensus** (Layer 4): run N adversaries at different
  temperatures; **any** breach / malformed / timeout fails the gate. Enable with
  `--adversaries N`.
- **Validation-guided search** (Layer 1): the winning MCTS candidate must pass an
  external validator (e.g. Z3); a rejected candidate gets negative feedback and
  the next-best is tried. This ties the search to real verification, not just
  the LLM's self-reported scores.

## Quickstart
```bash
pip install -r requirements.txt

# 1) Self-contained offline demo (no API key). Should PASS:
python -m apex --offline --demo

# 2) Layer 2 in isolation — real Z3 proving and refuting deadlock-freedom:
python -m examples.deadlock_check

# 3) Real run against a directory (halts at Layer 3 on the vulnerable fixture):
python -m apex --offline \
  --spec "(declare-const x Int)(assert (> x 0))(assert (< x 0))(check-sat)" \
  --target tests/fixtures/vulnerable
```
For real LLM calls (Layers 1/2/4), copy `.env.example` to `.env`, add a
`GEMINI_API_KEY`, and drop `--offline`.

## Tests
```bash
python -m pytest -q
```
The full suite runs **offline with zero API calls** via a deterministic mock
LLM. It covers real proofs and refutations, real bandit findings, injection
quarantine, and every layer's failure path.

## Honesty / Scope — please read
This pipeline proves **specific, human-readable properties** and runs real
static analysis and adversarial review. It does **not**:
- make code "provably correct", "attack-proof", or "unhackable" in general;
- replace human review of *which* properties matter.

The hard, unsolved part is the **specification problem**: Z3 can prove whatever
spec it is given, but nothing guarantees the LLM-generated spec captures what you
actually meant. A wrong spec can be "proven safe" while missing the real bug.
Treat generated specs as drafts a human must review.

Removed from the original project because they were fiction, not engineering:
fabricated metrics (10⁶ node evals, 128 Kubernetes pods), and the "Layer 5 /
Mirrorforge" universe-generation / self-evolution concept.

## Repository layout
```
apex/
  llm_client.py     shared LLM client + strict parsers + offline mock
  layer1_mcts.py    real MCTS (+ validation-guided re-selection)
  layer2_z3.py      real Z3 verification
  layer3_hive.py    real bandit static-analysis hive
  layer4_gate.py    real adversarial gate + injection hardening + consensus
  pyverify.py       AST->Z3 verification of real Python functions
  cegis.py          counterexample-guided spec repair loop
  orchestrator.py   pipeline wiring, metrics, structured result
  __main__.py       CLI entry (python -m apex)
examples/
  deadlock_check.py flagship Z3 demo
  buggy_max.py      sample function with a real bug for --verify-code
tests/              full offline test suite + fixtures
```

## Citation
```
Ramli T. Michael. APEX-REAL: A Working LLM Verification Pipeline. 2026.
https://github.com/ramlimicheal/APEX-V6-KERNEL
```
