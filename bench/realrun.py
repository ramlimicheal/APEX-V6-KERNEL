"""Run a real QuixBugs file on one input, in a separate Python process.

Every counterexample APEX reports is replayed here before it may count:
a symbolic result is only evidence once real Python agrees.
"""

import ast
import json
import subprocess
import sys

TIMEOUT_S = 5

_CHILD = r"""
import ast, importlib.util, json, sys
path, fn, args = sys.argv[1], sys.argv[2], ast.literal_eval(sys.argv[3])
spec = importlib.util.spec_from_file_location("quixbugs_prog", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
try:
    result = getattr(mod, fn)(*args)
    print(json.dumps({"kind": "value", "repr": repr(result)}))
except BaseException as exc:
    print(json.dumps({"kind": type(exc).__name__}))
"""


def run_real(path: str, fn: str, args: list) -> dict:
    """{'kind': 'value', 'repr': ...} | {'kind': '<ExceptionName>'} | {'kind': 'TIMEOUT'}."""
    try:
        proc = subprocess.run([sys.executable, "-I", "-c", _CHILD, path, fn, repr(list(args))],
                              capture_output=True, text=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"kind": "TIMEOUT"}
    lines = proc.stdout.strip().splitlines()
    if not lines:
        return {"kind": "CRASH", "stderr": proc.stderr[-300:]}
    return json.loads(lines[-1])


def value_of(outcome: dict):
    return ast.literal_eval(outcome["repr"])
