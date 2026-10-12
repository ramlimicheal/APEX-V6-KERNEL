"""Run APEX on the 40 QuixBugs programs and write bench/results_<step>.json.

    python -m bench.run --step baseline   # original apex.pyverify.verify_function
    python -m bench.run --step loops      # + (a) bounded loops
    python -m bench.run --step calls      # + (b) inlined calls / recursion
    python -m bench.run --step lists      # + (c) int lists / strings as Z3 arrays
    python -m bench.run --step equiv      # (d) bounded equivalence buggy vs correct

Pair outcomes (spec mode, steps baseline..lists):
  caught       buggy refuted with a counterexample that a REAL run of the buggy
               file confirms (wrong output, exception or timeout), AND the
               correct version proved (up to the bound)
  false_alarm  the correct version is refuted
  unsupported  either version (or its spec) is outside the modeled subset, or
               the solver gave up; the exact reason is recorded
  missed       everything else (buggy proved, or its counterexample did not
               reproduce in a real run)
Equivalence mode: caught = a symbolic difference that real runs of both files
reproduce; false_alarm = correct-vs-correct shows any difference.
"""

import argparse
import dataclasses
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from apex.pybounded import BoundedOptions, Unsupported, check_equivalence, verify_bounded
from apex.pyverify import verify_function
from bench import specs as specs_module
from bench.realrun import run_real, value_of
from bench.specs import EQUIV_SCOPE, SPECS

ROOT = Path(__file__).resolve().parent
QB = ROOT / "quixbugs"
QUIXBUGS_SHA = "4257f44b0ff1181dedaedee6a447e133219fcebf"
HELPERS = (ROOT / "specs.py").read_text()
WALL_LIMIT_S = 1800

STEP_OPTIONS = {
    "loops": BoundedOptions(loops=True, calls=False, lists=False),
    "calls": BoundedOptions(loops=True, calls=True, lists=False),
    "lists": BoundedOptions(loops=True, calls=True, lists=True),
    "equiv": BoundedOptions(bound=12, list_cap=4),
}


def _paths(name):
    return QB / "python_programs" / f"{name}.py", QB / "correct_python_programs" / f"{name}.py"


def _concrete_holds(expr, env):
    ns = {k: v for k, v in vars(specs_module).items() if not k.startswith("__")}
    ns.update(env)
    try:
        return bool(eval(expr, ns))   # noqa: S307 - spec text from bench/specs.py
    except Exception as exc:          # a malformed result (e.g. None) violates the spec
        return f"spec raised {type(exc).__name__}"


def _replay_spec(name, spec, cex):
    """Run the real buggy file on the counterexample and judge it with the spec."""
    buggy, _ = _paths(name)
    arg_names = [a.split(":")[0].strip() for a in spec.args.split(",")]
    args = [cex[a] for a in arg_names]
    if _concrete_holds(f"({spec.pre}) and ({spec.domain})", dict(cex)) is not True:
        return {"confirmed": False, "note": "counterexample does not meet the precondition"}
    real = run_real(str(buggy), name, args)
    if real["kind"] != "value":
        return {"confirmed": True, "real": real}
    holds = _concrete_holds(spec.post, {**cex, "result": value_of(real)})
    return {"confirmed": holds is not True, "real": real, "post_on_real_output": holds}


def _verify_one(step, src, name, spec):
    if step == "baseline":
        r = verify_function(src, spec.post)
        status = "proved" if r.proven else ("refuted" if r.counterexample else "unknown")
        return {"status": status, "detail": r.detail, "counterexample": r.counterexample}
    opts = dataclasses.replace(STEP_OPTIONS[step], list_cap=spec.cap)
    r = verify_bounded(src, name, spec.args, spec.post, f"({spec.pre}) and ({spec.domain})", HELPERS, opts)
    return {"status": r.status, "detail": r.detail, "counterexample": r.counterexample,
            "predicted_outcome": r.outcome, "predicted": r.predicted}


def evaluate_spec(step, name):
    spec = SPECS[name]
    res = {"name": name, "partial_spec": spec.partial, "domain": spec.domain, "list_cap": spec.cap}
    for version, path in zip(("buggy", "correct"), _paths(name)):
        try:
            res[version] = _verify_one(step, path.read_text(), name, spec)
        except Unsupported as exc:
            res[version] = {"status": "unsupported", "detail": str(exc)}
        except Exception as exc:   # the original verifier is not always fail-closed
            res[version] = {"status": "unsupported",
                            "detail": f"verifier crashed: {type(exc).__name__}: {str(exc)[:120]}"}
    b, c = res["buggy"], res["correct"]
    for version, r in (("buggy", b), ("correct", c)):
        if r["status"] in ("unsupported", "unknown"):
            return {**res, "outcome": "unsupported", "reason": f"{version}: {r['detail']}"}
    if c["status"] == "refuted":
        return {**res, "outcome": "false_alarm", "reason": f"correct version refuted on {c['counterexample']}"}
    if b["status"] != "refuted":
        return {**res, "outcome": "missed", "reason": "buggy version was not refuted"}
    if step == "baseline":   # the original verifier never reaches here on QuixBugs
        return {**res, "outcome": "missed", "reason": "baseline counterexamples are not replayed"}
    replay = _replay_spec(name, spec, b["counterexample"])
    b["replay"] = replay
    if replay["confirmed"]:
        return {**res, "outcome": "caught", "reason": f"counterexample {b['counterexample']} confirmed by a real run"}
    return {**res, "outcome": "missed", "reason": "counterexample did not reproduce in a real run (spurious)"}


def _same_behaviour(x, y):
    if x["kind"] != y["kind"]:
        return False
    return x["kind"] != "value" or x["repr"] == y["repr"]


def evaluate_equiv(name):
    spec = SPECS[name]
    buggy, correct = _paths(name)
    res = {"name": name, "in_scope": name in EQUIV_SCOPE}
    opts = STEP_OPTIONS["equiv"]
    pre = f"({spec.pre}) and ({spec.domain})"
    try:
        r = check_equivalence(buggy.read_text(), correct.read_text(), name, spec.args, pre, HELPERS, opts)
        cc = check_equivalence(correct.read_text(), correct.read_text(), name, spec.args, pre, HELPERS, opts)
    except Unsupported as exc:
        return {**res, "outcome": "unsupported", "reason": str(exc)}
    res["buggy_vs_correct"] = vars(r)
    res["correct_vs_correct"] = vars(cc)
    if "unknown" in (r.status, cc.status):
        return {**res, "outcome": "unsupported", "reason": "solver returned unknown"}
    if cc.status == "different":
        return {**res, "outcome": "false_alarm", "reason": f"correct-vs-correct differs on {cc.counterexample}"}
    if r.status != "different":
        return {**res, "outcome": "missed", "reason": "no difference found within the bound"}
    arg_names = [a.split(":")[0].strip() for a in spec.args.split(",")]
    args = [r.counterexample[a] for a in arg_names]
    real_b, real_c = run_real(str(buggy), name, args), run_real(str(correct), name, args)
    res["replay"] = {"buggy": real_b, "correct": real_c}
    if _same_behaviour(real_b, real_c):
        return {**res, "outcome": "missed", "reason": "symbolic difference did not reproduce in real runs (spurious)"}
    return {**res, "outcome": "caught", "reason": f"input {r.counterexample}: buggy {real_b} vs correct {real_c}"}


def evaluate(step, name, cap=None):
    if cap is not None:   # --cap: measure a program at another list cap (not used for results)
        SPECS[name] = dataclasses.replace(SPECS[name], cap=cap)
    t0 = time.time()
    out = evaluate_equiv(name) if step == "equiv" else evaluate_spec(step, name)
    out["seconds"] = round(time.time() - t0, 1)
    return out


def _run_isolated(step, name):
    """One program per process, so a pathological solver run cannot stall the benchmark."""
    try:
        proc = subprocess.run([sys.executable, "-m", "bench.run", "--step", step, "--one", name],
                              capture_output=True, text=True, timeout=WALL_LIMIT_S, cwd=ROOT.parent)
    except subprocess.TimeoutExpired:
        return {"name": name, "outcome": "unsupported", "reason": f"exceeded the {WALL_LIMIT_S}s wall-clock limit"}
    if proc.returncode != 0:
        return {"name": name, "outcome": "unsupported", "reason": "harness error: " + proc.stderr.strip()[-400:]}
    return json.loads(proc.stdout)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--step", required=True, choices=["baseline", "loops", "calls", "lists", "equiv"])
    ap.add_argument("--one", help="evaluate a single program and print JSON (used internally)")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--cap", type=int, help="with --one: override the program's list cap")
    args = ap.parse_args()
    if args.one:
        print(json.dumps(evaluate(args.step, args.one, args.cap), default=repr))
        return
    names = sorted(SPECS)
    with ThreadPoolExecutor(args.jobs) as pool:
        results = list(pool.map(lambda n: _run_isolated(args.step, n), names))
    counts = {k: sum(r["outcome"] == k for r in results) for k in ("caught", "missed", "false_alarm", "unsupported")}
    opts = STEP_OPTIONS.get(args.step)
    doc = {
        "step": args.step,
        "quixbugs_commit": QUIXBUGS_SHA,
        "options": None if opts is None else vars(opts),
        "summary": counts,
        "programs": results,
    }
    out = ROOT / f"results_{args.step}.json"
    out.write_text(json.dumps(doc, indent=1, default=repr) + "\n")
    print(f"{args.step}: {counts} -> {out.relative_to(ROOT.parent)}")
    for r in results:
        print(f"  {r['name']:28s} {r['outcome']:12s} {r.get('seconds', '')!s:>6}  {r['reason'][:110]}")


if __name__ == "__main__":
    main()
