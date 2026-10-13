"""Code-level verification: prove properties of REAL Python functions with Z3.

This is the step beyond Layer 2's design-spec checking: it takes actual Python
source code, symbolically executes it into Z3 constraints via the AST, and then
either

  * PROVES a postcondition holds for ALL possible integer inputs
    (not some inputs - all of them, mathematically), or
  * returns a CONCRETE counterexample: real input values that break the
    contract, ready to paste into a failing unit test.

Honest scope (fail-closed, like everything else in this pipeline):
- Supported subset: single function, integer arguments, pure expressions with
  + - * unary-minus, comparisons, and/or/not, if/elif/else, ternary, return.
- Anything outside the subset raises `UnsupportedConstructError` rather than
  guessing. Loops, calls, floats, strings, side effects: rejected.
This mirrors how real bounded verifiers start; growing the subset is
engineering, not magic. `apex.pybounded` grows it (bounded loops, inlined
calls, int lists) at the price of proofs that hold only up to a bound.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import z3

logger = logging.getLogger("apex.pyverify")


class UnsupportedConstructError(Exception):
    """Raised when the source uses syntax outside the verified subset."""


@dataclass
class VerifyResult:
    proven: bool
    counterexample: Optional[Dict[str, int]]
    detail: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        if self.proven:
            return f"PROVEN for all inputs ({self.detail})"
        return f"REFUTED: counterexample {self.counterexample}"


# ---------------------------------------------------------------------------
# AST -> Z3 translation
# ---------------------------------------------------------------------------

_CMP = {
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
}

_BIN = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
}


def _translate_expr(node: ast.expr, env: Dict[str, z3.ExprRef]) -> z3.ExprRef:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            return z3.BoolVal(node.value)
        if isinstance(node.value, int):
            return z3.IntVal(node.value)
        raise UnsupportedConstructError(f"unsupported constant: {node.value!r}")

    if isinstance(node, ast.Name):
        if node.id not in env:
            raise UnsupportedConstructError(f"unknown name: {node.id}")
        return env[node.id]

    if isinstance(node, ast.BinOp):
        op = type(node.op)
        if op not in _BIN:
            raise UnsupportedConstructError(f"unsupported operator: {op.__name__}")
        return _BIN[op](_translate_expr(node.left, env), _translate_expr(node.right, env))

    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.USub):
            return -_translate_expr(node.operand, env)
        if isinstance(node.op, ast.Not):
            return z3.Not(_translate_expr(node.operand, env))
        raise UnsupportedConstructError(f"unsupported unary op: {type(node.op).__name__}")

    if isinstance(node, ast.Compare):
        if len(node.ops) != 1 or len(node.comparators) != 1:
            raise UnsupportedConstructError("chained comparisons not supported")
        op = type(node.ops[0])
        if op not in _CMP:
            raise UnsupportedConstructError(f"unsupported comparison: {op.__name__}")
        return _CMP[op](
            _translate_expr(node.left, env), _translate_expr(node.comparators[0], env)
        )

    if isinstance(node, ast.BoolOp):
        parts = [_translate_expr(v, env) for v in node.values]
        return z3.And(*parts) if isinstance(node.op, ast.And) else z3.Or(*parts)

    if isinstance(node, ast.IfExp):
        return z3.If(
            _translate_expr(node.test, env),
            _translate_expr(node.body, env),
            _translate_expr(node.orelse, env),
        )

    raise UnsupportedConstructError(f"unsupported expression: {type(node).__name__}")


def _translate_body(stmts: List[ast.stmt], env: Dict[str, z3.ExprRef]) -> z3.ExprRef:
    """Convert a statement list (every path must return) into one Z3 value."""
    if not stmts:
        raise UnsupportedConstructError("a code path is missing a return statement")

    head, rest = stmts[0], stmts[1:]

    # Skip a leading docstring.
    if isinstance(head, ast.Expr) and isinstance(head.value, ast.Constant) \
            and isinstance(head.value.value, str):
        return _translate_body(rest, env)

    if isinstance(head, ast.Return):
        if head.value is None:
            raise UnsupportedConstructError("bare `return` (no value) not supported")
        return _translate_expr(head.value, env)

    if isinstance(head, ast.If):
        test = _translate_expr(head.test, env)
        then_val = _translate_body(head.body, env)
        # `else:` branch if present, otherwise fall through to the code after the if.
        else_val = _translate_body(head.orelse if head.orelse else rest, env)
        return z3.If(test, then_val, else_val)

    raise UnsupportedConstructError(f"unsupported statement: {type(head).__name__}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def verify_function(source: str, postcondition: str) -> VerifyResult:
    """Prove `postcondition` holds for ALL integer inputs of the function.

    `postcondition` is a Python boolean expression over the function's argument
    names and `result`, e.g. "result >= 0" or "result >= a and result >= b".
    """
    try:
        module = ast.parse(source)
    except SyntaxError as exc:
        raise UnsupportedConstructError(f"source does not parse: {exc}") from exc

    funcs = [n for n in module.body if isinstance(n, ast.FunctionDef)]
    if len(funcs) != 1:
        raise UnsupportedConstructError("expected exactly one function definition")
    fn = funcs[0]
    if fn.args.kwonlyargs or fn.args.vararg or fn.args.kwarg or fn.args.defaults:
        raise UnsupportedConstructError("only plain positional int args supported")

    env: Dict[str, z3.ExprRef] = {a.arg: z3.Int(a.arg) for a in fn.args.args}
    result_expr = _translate_body(fn.body, env)

    result_var = z3.Int("result")
    post_ast = ast.parse(postcondition, mode="eval")
    post_expr = _translate_expr(post_ast.body, {**env, "result": result_var})

    solver = z3.Solver()
    solver.add(result_var == result_expr)
    solver.add(z3.Not(post_expr))          # look for ANY violating input
    outcome = solver.check()

    if outcome == z3.unsat:
        logger.info("[pyverify] proven: %s", postcondition)
        return VerifyResult(True, None, f"no integer input can violate '{postcondition}'")

    if outcome == z3.sat:
        model = solver.model()
        counter = {
            name: model[var].as_long()          # type: ignore[union-attr]
            for name, var in env.items()
            if model[var] is not None
        }
        counter["result"] = model[result_var].as_long()  # type: ignore[union-attr]
        logger.warning("[pyverify] refuted with %s", counter)
        return VerifyResult(False, counter, f"'{postcondition}' violated")

    return VerifyResult(False, None, "solver returned unknown")
