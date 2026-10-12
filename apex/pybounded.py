"""Bounded verification of real Python functions: loops, calls, int lists.

`apex.pyverify.verify_function` proves a postcondition for ALL integer inputs
but rejects loops, calls and containers. This module applies the same
AST -> Z3 idea to a larger subset of Python, at the price of a *bound*:

  (a) loops (`for ... in range(...)`, `for x in xs`, `while`, comprehensions)
      are unrolled up to K iterations (default 8);
  (b) calls to other functions defined in the same source (including nested
      defs and recursion) are inlined up to K nested calls;
  (c) int lists and strings are Z3 arrays with a symbolic length. List/str
      *inputs* have a length between 0 and a cap (default K).

Every unrolled loop and inlined call carries an *unwinding assertion*: if an
input inside the domain could need more than K iterations or K nested calls,
that input is reported as a counterexample of kind "bound". It is never
silently dropped. So "proved_up_to_bound" means: for every input that meets
the precondition (and has lists no longer than the cap), the function stops
within the bound, raises no exception and meets the postcondition. It is NOT
a proof for inputs outside that domain.

Two entry points:
  * `verify_bounded`    - postcondition check (spec mode).
  * `check_equivalence` - bounded equivalence of two implementations.

Honest scope (fail-closed): ints, bools, int lists, strings, lists of int
lists, int->int dicts, None returns. Floats (except the exact pattern
`int(x ** 0.5)`), objects, sets, tuples-as-data, generators (`yield`),
lambdas, try/except and aliasing mutation all raise
`UnsupportedConstructError` with the reason.
"""

from __future__ import annotations

import ast
import itertools
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional, Tuple

import z3

from apex.pyverify import UnsupportedConstructError as Unsupported

BOUND_DEFAULT = 8
SPEC_BOUND_DEFAULT = 64
_BIT_WIDTH = 16          # bitwise ops are exact for operands in [0, 2**16)
_FLOAT_EXACT = 2 ** 52   # int(x ** 0.5) is exact for 0 <= x < 2**52
_SPLIT_MAX = 64          # case-split a nonlinear operand whose range has <= this many values

TRUE, FALSE = z3.BoolVal(True), z3.BoolVal(False)
_ZERO, _ONE = z3.IntVal(0), z3.IntVal(1)

_IntListDT = z3.Datatype("IntList")
_IntListDT.declare("mk", ("elems", z3.ArraySort(z3.IntSort(), z3.IntSort())), ("size", z3.IntSort()))
IntList = _IntListDT.create()
_EMPTY_INTS = z3.K(z3.IntSort(), _ZERO)
_EMPTY_INTLIST = IntList.mk(_EMPTY_INTS, _ZERO)

# Outcome codes of one execution (first event on a path wins).
CODES = {
    "value": 0, "none": 1, "bound": 2, "model_limit": 3,
    "IndexError": 10, "KeyError": 11, "ZeroDivisionError": 12,
    "ValueError": 13, "TypeError": 14,
}
CODE_NAMES = {v: k for k, v in CODES.items()}
_STRING_MODULE = {
    "digits": "0123456789",
    "ascii_uppercase": "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "ascii_lowercase": "abcdefghijklmnopqrstuvwxyz",
}


@dataclass
class BoundedOptions:
    bound: int = BOUND_DEFAULT            # loop iterations / nested calls (K)
    list_cap: int = BOUND_DEFAULT         # max length of list/str inputs
    spec_bound: int = SPEC_BOUND_DEFAULT  # loop bound inside spec helpers
    loops: bool = True                    # extension (a)
    calls: bool = True                    # extension (b)
    lists: bool = True                    # extension (c)
    timeout_ms: int = 120_000
    feasibility_timeout_ms: int = 5_000


@dataclass
class BoundedResult:
    status: str                      # proved_up_to_bound | refuted | unsupported | unknown
    detail: str
    bound: int
    counterexample: Optional[dict] = None   # concrete argument values
    outcome: Optional[str] = None           # predicted outcome kind on the cex
    predicted: object = None                # predicted return value on the cex


@dataclass
class EquivResult:
    status: str                      # equivalent_up_to_bound | different | unsupported | unknown
    detail: str
    bound: int
    counterexample: Optional[dict] = None
    outcome_a: Optional[str] = None
    outcome_b: Optional[str] = None
    predicted_a: object = None
    predicted_b: object = None


# ---------------------------------------------------------------------------
# Symbolic values
# ---------------------------------------------------------------------------

class _Bottom:
    """Value on a path that is already dead (pruned or terminated)."""

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "BOTTOM"


class _NoneVal:
    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "None"


BOTTOM, NONE = _Bottom(), _NoneVal()
_uid_counter = itertools.count()


def _fresh() -> frozenset:
    return frozenset([next(_uid_counter)])


@dataclass(frozen=True)
class SymList:
    """A list (or str) as a canonical Z3 array: indices outside 0..len-1 hold 0."""
    arr: z3.ArrayRef
    length: z3.ArithRef
    kind: Optional[str]           # 'int' | 'str' | 'list' | None (known empty)
    maxlen: Optional[int]         # static upper bound on the length, if known
    uids: frozenset = field(default_factory=_fresh)   # object identity, for aliasing
    inner: Optional[int] = None   # kind 'list': static max length of the inner lists


@dataclass(frozen=True)
class SymDict:
    vals: z3.ArrayRef
    present: z3.ArrayRef
    uids: frozenset = field(default_factory=_fresh)


@dataclass(frozen=True)
class _FuncRef:
    node: ast.FunctionDef
    closure: Optional[dict]


@dataclass(frozen=True)
class _Module:
    name: str


@dataclass(frozen=True)
class _Opaque:
    reason: str


def _and(*xs):
    xs = [x for x in xs if not z3.is_true(x)]
    if any(z3.is_false(x) for x in xs):
        return FALSE
    return TRUE if not xs else xs[0] if len(xs) == 1 else z3.And(*xs)


def _or(*xs):
    xs = [x for x in xs if not z3.is_false(x)]
    if any(z3.is_true(x) for x in xs):
        return TRUE
    return FALSE if not xs else xs[0] if len(xs) == 1 else z3.Or(*xs)


def _not(x):
    return FALSE if z3.is_true(x) else TRUE if z3.is_false(x) else z3.Not(x)


def _ite(c, a, b):
    if z3.is_true(c) or a.eq(b):
        return a
    return b if z3.is_false(c) else z3.If(c, a, b)


def _is_int(v) -> bool:
    return isinstance(v, z3.ArithRef) and v.is_int()


def _zero(kind):
    return _EMPTY_INTLIST if kind == "list" else _ZERO


def _const_int(v) -> Optional[int]:
    v = z3.simplify(v)
    return v.as_long() if z3.is_int_value(v) else None


def _const_list(terms, kind, inner=None) -> SymList:
    arr = z3.K(z3.IntSort(), _zero(kind))
    for i, t in enumerate(terms):
        arr = z3.Store(arr, i, t)
    return SymList(arr, z3.IntVal(len(terms)), kind if terms or kind == "str" else None, len(terms),
                   inner=inner if terms else 0)


def _str_const(s: str) -> SymList:
    return _const_list([z3.IntVal(ord(c)) for c in s], "str")


def _unify_kind(a, b):
    if a is None or a == b:
        return b
    if b is None:
        return a
    raise Unsupported(f"mixing list kinds '{a}' and '{b}'")


def _retype(lst: SymList, kind) -> SymList:
    if lst.kind == kind or kind is None:
        return lst
    if lst.kind is not None:
        raise Unsupported(f"mixing list kinds '{lst.kind}' and '{kind}'")
    return replace(lst, arr=z3.K(z3.IntSort(), _zero(kind)), kind=kind)


def _maxlen_add(a, b):
    return None if a is None or b is None else a + b


def _max_opt(a, b):
    return None if a is None or b is None else max(a, b)


def _inner_of(v):
    return v.maxlen if isinstance(v, SymList) else None


def _merge(c, a, b):
    """Python value `a if c else b`; BOTTOM marks a dead side."""
    if a is BOTTOM:
        return b
    if b is BOTTOM or a is b:
        return a
    if isinstance(a, z3.ExprRef) and isinstance(b, z3.ExprRef):
        if a.sort() != b.sort():
            raise Unsupported("branches produce values of different types (bool/int)")
        return _ite(c, a, b)
    if isinstance(a, SymList) and isinstance(b, SymList):
        kind = _unify_kind(a.kind, b.kind)
        a, b = _retype(a, kind), _retype(b, kind)
        maxlen = None if a.maxlen is None or b.maxlen is None else max(a.maxlen, b.maxlen)
        return SymList(_ite(c, a.arr, b.arr), _ite(c, a.length, b.length), kind, maxlen, a.uids | b.uids,
                       _max_opt(a.inner, b.inner))
    if isinstance(a, SymDict) and isinstance(b, SymDict):
        return SymDict(_ite(c, a.vals, b.vals), _ite(c, a.present, b.present), a.uids | b.uids)
    if isinstance(a, tuple) and isinstance(b, tuple) and len(a) == len(b):
        return tuple(_merge(c, x, y) for x, y in zip(a, b))
    if a is NONE and b is NONE:
        return NONE
    if isinstance(a, _FuncRef) and isinstance(b, _FuncRef) and a.node is b.node:
        return a
    raise Unsupported(f"branches produce incompatible values ({type(a).__name__} / {type(b).__name__})")


def _join(parts):
    """Merge (guard, env) pairs of mutually exclusive paths into one env."""
    live = [(g, e) for g, e in parts if not z3.is_false(g)]
    if not live:
        return (parts[0][1] if parts else {}), FALSE
    env = {}
    for name in set().union(*(e.keys() for _, e in live)):
        vals = [(g, e[name]) for g, e in live if name in e]
        v = vals[-1][1]
        for g, x in reversed(vals[:-1]):
            v = _merge(g, x, v)
        env[name] = v
    return env, _or(*[g for g, _ in live])


def _holds(v, uids) -> bool:
    if isinstance(v, (SymList, SymDict)):
        return bool(v.uids & uids)
    if isinstance(v, tuple):
        return any(_holds(x, uids) for x in v)
    return False


class _Frame:
    def __init__(self, closure: Optional[dict]):
        self.closure = closure
        self.returns: List[Tuple[z3.BoolRef, object]] = []
        self.loops: List[dict] = []


class _Path:
    """The state of all executions that reach a program point: env + guard."""

    def __init__(self, frame: _Frame, env: dict, alive):
        self.frame, self.env, self.alive = frame, env, alive

    def fork(self, alive) -> "_Path":
        return _Path(self.frame, dict(self.env), alive)

    def view(self, alive) -> "_Path":
        return _Path(self.frame, self.env, alive)


@dataclass
class _Outcome:
    code: z3.ArithRef
    value: object
    events: list


# ---------------------------------------------------------------------------
# Program loading
# ---------------------------------------------------------------------------

def load_globals(source: str, functions_only: bool = False) -> dict:
    try:
        module = ast.parse(source)
    except SyntaxError as exc:
        raise Unsupported(f"source does not parse: {exc}") from exc
    env: dict = {}
    for node in module.body:
        if isinstance(node, ast.FunctionDef):
            env[node.name] = _FuncRef(node, None)
        elif functions_only:
            continue
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name
                env[name] = _Module("string") if alias.name == "string" else \
                    _Opaque(f"module '{alias.name}' is not modeled")
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                env[alias.asname or alias.name] = _Opaque(
                    f"'{alias.name}' from module '{node.module}' is not modeled")
    return env


# ---------------------------------------------------------------------------
# Solver
# ---------------------------------------------------------------------------

class _Solver:
    """Z3 checks against a growing list of facts.

    Each check uses a fresh solver: incremental push/pop on these array/lambda
    formulas produced a model that did not satisfy the query. Every `sat`
    model is re-evaluated against the query and only trusted if it holds, so a
    solver glitch can only ever yield `unknown`, never a fake counterexample.
    An `unknown` is retried once with Z3's QF_NIA strategy, which can settle
    bounded nonlinear integer problems the default strategy gives up on.
    """

    def __init__(self):
        self.facts: list = []
        self.reason = ""

    def add(self, *facts) -> None:
        self.facts.extend(facts)

    def check(self, cond, timeout_ms: int, fallback: bool = True):
        query = z3.And(*self.facts, cond)
        self.reason = ""
        strategies = (z3.Solver, lambda: z3.SolverFor("QF_NIA")) if fallback else (z3.Solver,)
        for make in strategies:
            s = make()
            s.set("timeout", timeout_ms)
            s.add(query)
            try:
                res = s.check()
            except z3.Z3Exception as exc:
                res, self.reason = z3.unknown, f"{self.reason}; {exc}"
            if res == z3.sat:
                model = s.model()
                if z3.is_true(model.eval(query, model_completion=True)):
                    return res, model
                res, self.reason = z3.unknown, f"{self.reason}; model failed validation"
            if res == z3.unsat:
                return res, None
            self.reason += f"; {s.reason_unknown()}"
        return z3.unknown, None


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class _Executor:
    def __init__(self, globals_: dict, solver: _Solver, opts: BoundedOptions, program: bool):
        self.globals = globals_
        self.solver = solver
        self.opts = opts
        self.program = program
        self.bound = opts.bound if program else opts.spec_bound
        self.events: List[Tuple[z3.BoolRef, str, str]] = []
        self.depth = 0
        self.callers: List[_Path] = []

    # -- infrastructure -----------------------------------------------------

    def _need(self, feature: str, what: str) -> None:
        if self.program and not getattr(self.opts, feature):
            step = {"loops": "a", "calls": "b", "lists": "c"}[feature]
            raise Unsupported(f"{what} needs extension ({step}) '{feature}', not enabled at this step")

    def _feasible(self, cond) -> bool:
        if z3.is_false(cond):
            return False
        if z3.is_true(cond):
            return True
        res = self.solver.check(cond, self.opts.feasibility_timeout_ms, fallback=False)[0]
        return res != z3.unsat   # unknown counts as feasible: we only lose pruning

    def _raise(self, kind: str, guard, detail: str) -> None:
        if not z3.is_false(guard):
            self.events.append((guard, kind, detail))

    def _lookup(self, name: str, p: _Path):
        for scope in (p.env, p.frame.closure or {}, self.globals):
            if name in scope:
                v = scope[name]
                if isinstance(v, _Opaque):
                    raise Unsupported(v.reason)
                return v
        raise Unsupported(f"unknown name: {name}")

    def _has_name(self, name: str, p: _Path) -> bool:
        return any(name in s for s in (p.env, p.frame.closure or {}, self.globals))

    # -- entry points ------------------------------------------------------

    def run(self, fn_name: str, args: list) -> _Outcome:
        fn = self.globals.get(fn_name)
        if not isinstance(fn, _FuncRef):
            raise Unsupported(f"function '{fn_name}' not found")
        if any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(fn.node)):
            raise Unsupported("generator functions (yield) are not modeled")
        frame = _Frame(None)
        returns = self._invoke(fn, args, TRUE, frame)
        nones = [g for g, v in returns if v is NONE]
        real = [(g, v) for g, v in returns if v is not NONE]
        value = BOTTOM
        if real:
            value = real[-1][1]
            for g, v in reversed(real[:-1]):
                value = _merge(g, v, value)
        code = z3.IntVal(-1)
        for g in reversed(nones):
            code = z3.If(g, CODES["none"], code)
        for g, _ in reversed(real):
            code = z3.If(g, CODES["value"], code)
        for g, kind, _ in reversed(self.events):
            code = z3.If(g, CODES[kind], code)
        return _Outcome(code, value, list(self.events))

    def eval_spec(self, expr: str, env: dict, guard):
        node = ast.parse(expr, mode="eval").body
        p = _Path(_Frame(None), dict(env), guard)
        return self._truthy(self._eval(node, p))

    def issue(self):
        return _or(*[g for g, _, _ in self.events])

    # -- calls -------------------------------------------------------------

    def _invoke(self, fn: _FuncRef, args: list, alive, frame: _Frame):
        a = fn.node.args
        if a.vararg or a.kwarg or a.kwonlyargs or a.defaults or a.posonlyargs:
            raise Unsupported("only plain positional parameters are modeled (no defaults / *args)")
        if len(a.args) != len(args):
            raise Unsupported(f"{fn.node.name}() called with {len(args)} args, expects {len(a.args)}")
        q = _Path(frame, {x.arg: v for x, v in zip(a.args, args)}, alive)
        self.depth += 1
        try:
            self._block(fn.node.body, q)
        finally:
            self.depth -= 1
        if not z3.is_false(q.alive):
            frame.returns.append((q.alive, NONE))
        return [(g, v) for g, v in frame.returns if v is not BOTTOM and not z3.is_false(g)]

    def _call_user(self, fn: _FuncRef, args: list, p: _Path):
        self._need("calls", f"call to {fn.node.name}()")
        if self.depth >= self.bound:
            self._raise("bound", p.alive, f"more than {self.bound} nested calls")
            return BOTTOM
        if not self._feasible(p.alive):
            return BOTTOM
        self.callers.append(p)
        try:
            returns = self._invoke(fn, args, p.alive, _Frame(fn.closure))
        finally:
            self.callers.pop()
        nones = [g for g, v in returns if v is NONE]
        real = [(g, v) for g, v in returns if v is not NONE]
        if real and nones:
            raise Unsupported(f"{fn.node.name}() returns None on some paths and a value on others")
        if not real:
            return NONE if nones else BOTTOM
        value = real[-1][1]
        for g, v in reversed(real[:-1]):
            value = _merge(g, v, value)
        return value

    # -- statements --------------------------------------------------------

    def _block(self, stmts, p: _Path) -> None:
        for s in stmts:
            if z3.is_false(p.alive):
                return
            n = len(self.events)
            self._stmt(s, p)
            if len(self.events) > n and not isinstance(s, (ast.If, ast.For, ast.While)):
                p.alive = _and(p.alive, _not(_or(*[g for g, _, _ in self.events[n:]])))

    def _stmt(self, s, p: _Path) -> None:
        method = getattr(self, "_s_" + type(s).__name__, None)
        if method is None:
            raise Unsupported(f"unsupported statement: {type(s).__name__}")
        method(s, p)

    def _s_Expr(self, s, p):
        if isinstance(s.value, ast.Constant) and isinstance(s.value.value, str):
            return  # docstring
        if self._eval(s.value, p) is BOTTOM:
            p.alive = FALSE

    def _s_Pass(self, s, p):
        pass

    def _s_Assign(self, s, p):
        if len(s.targets) != 1:
            raise Unsupported("chained assignment a = b = ...")
        v = self._eval(s.value, p)
        if v is BOTTOM:
            p.alive = FALSE
            return
        self._assign(s.targets[0], v, p)

    def _s_AugAssign(self, s, p):
        t = s.target
        if isinstance(t, ast.Name):
            old = self._lookup(t.id, p)
            if t.id not in p.env:
                raise Unsupported("augmented assignment to an enclosing-scope variable")
            if isinstance(old, SymList):
                self._mutating(t.id, p)
        elif isinstance(t, ast.Subscript) and not isinstance(t.slice, ast.Slice):
            old = self._eval(t, p)
        else:
            raise Unsupported("augmented assignment target")
        rhs = self._eval(s.value, p)
        if old is BOTTOM or rhs is BOTTOM:
            p.alive = FALSE
            return
        new = self._binop(s.op, old, rhs, p)
        if isinstance(t, ast.Name) and isinstance(old, SymList):
            new = replace(new, uids=old.uids)
        self._assign(t, new, p)

    def _s_Return(self, s, p):
        v = NONE if s.value is None else self._eval(s.value, p)
        if v is not BOTTOM:
            p.frame.returns.append((p.alive, v))
        p.alive = FALSE

    def _s_FunctionDef(self, s, p):
        if s.decorator_list:
            raise Unsupported("decorated functions")
        p.env[s.name] = _FuncRef(s, p.env)

    def _s_Import(self, s, p):
        raise Unsupported("import inside a function body")

    _s_ImportFrom = _s_Import

    def _s_If(self, s, p):
        test = self._eval(s.test, p)
        if test is BOTTOM:
            p.alive = FALSE
            return
        c = self._truthy(test)
        g = p.alive
        then_p, else_p = p.fork(_and(g, c)), p.fork(_and(g, _not(c)))
        self._block(s.body, then_p)
        self._block(s.orelse, else_p)
        p.env, p.alive = _join([(then_p.alive, then_p.env), (else_p.alive, else_p.env)])

    def _s_Break(self, s, p):
        if not p.frame.loops:
            raise Unsupported("break outside loop")
        p.frame.loops[-1]["breaks"].append((p.alive, dict(p.env)))
        p.alive = FALSE

    def _s_Continue(self, s, p):
        if not p.frame.loops:
            raise Unsupported("continue outside loop")
        p.frame.loops[-1]["continues"].append((p.alive, dict(p.env)))
        p.alive = FALSE

    def _s_While(self, s, p):
        self._need("loops", "while loop")
        if s.orelse:
            raise Unsupported("while ... else")
        self._loop(p, None, lambda t: (self._truthy(self._eval(s.test, p)), None), None, s.body)

    def _s_For(self, s, p):
        self._need("loops", "for loop")
        if s.orelse:
            raise Unsupported("for ... else")
        count, item = self._plan(s.iter, p)
        self._loop(p, count, item, lambda q, it: self._assign(s.target, it, q), s.body)

    def _loop(self, p, count, item, bind, body) -> None:
        """Unroll up to the bound; an input needing more iterations is a 'bound' event."""
        ctx = {"breaks": [], "continues": []}
        exits = []
        limit = self.bound if count is None else min(count, self.bound)
        p.frame.loops.append(ctx)
        try:
            for t in range(limit + 1):
                if z3.is_false(p.alive):
                    break
                if t == limit:
                    if count is not None and count <= self.bound:
                        exits.append((p.alive, dict(p.env)))   # iterable exhausted
                    else:
                        cond, _ = item(t)
                        self._raise("bound", _and(p.alive, cond),
                                    f"loop needs more than {self.bound} iterations")
                        exits.append((_and(p.alive, _not(cond)), dict(p.env)))
                    break
                cond, it = item(t)
                exits.append((_and(p.alive, _not(cond)), dict(p.env)))
                enter = _and(p.alive, cond)
                if not self._feasible(enter):
                    break
                p.alive = enter
                if bind is not None:
                    bind(p, it)
                self._block(body, p)
                if ctx["continues"]:
                    p.env, p.alive = _join([(p.alive, p.env)] + ctx["continues"])
                    ctx["continues"] = []
        finally:
            p.frame.loops.pop()
        p.env, p.alive = _join(exits + ctx["breaks"])

    # -- assignment & mutation --------------------------------------------

    def _assign(self, target, v, p: _Path) -> None:
        if isinstance(target, ast.Name):
            p.env[target.id] = v
        elif isinstance(target, (ast.Tuple, ast.List)):
            self._unpack(target.elts, v, p)
        elif isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
            self._setitem(target, v, p)
        else:
            raise Unsupported(f"assignment target {type(target).__name__}")

    def _unpack(self, targets, v, p) -> None:
        stars = [i for i, t in enumerate(targets) if isinstance(t, ast.Starred)]
        if isinstance(v, tuple):
            if stars or len(v) != len(targets):
                raise Unsupported("tuple unpacking shape")
            for t, x in zip(targets, v):
                self._assign(t, x, p)
            return
        if not isinstance(v, SymList):
            raise Unsupported(f"unpacking a {type(v).__name__}")
        if len(stars) > 1 or (stars and stars[0] != len(targets) - 1):
            raise Unsupported("starred unpacking other than `a, *rest = xs`")
        fixed = len(targets) - len(stars)
        bad = v.length < fixed if stars else v.length != fixed
        self._raise("ValueError", _and(p.alive, bad), "wrong number of values to unpack")
        for i in range(fixed):
            self._assign(targets[i], self._elem(v, z3.IntVal(i)), p)
        if stars:
            rest = self._slice(v, z3.IntVal(fixed), None)
            if v.kind == "str":
                raise Unsupported("starred unpacking of a str")
            self._assign(targets[-1].value, rest, p)

    def _mutating(self, name: str, p: _Path) -> None:
        """Lists are values here; refuse in-place mutation that another name could see."""
        uids = p.env[name].uids
        others = [v for k, v in p.env.items() if k != name]
        for q in self.callers:
            others.extend(q.env.values())
        if any(_holds(v, uids) for v in others):
            raise Unsupported(f"in-place mutation of '{name}', which another name also references "
                              "(aliasing is not modeled)")

    def _setitem(self, target, v, p) -> None:
        name = target.value.id
        if name not in p.env:
            raise Unsupported("item assignment to an enclosing-scope variable")
        base = p.env[name]
        if isinstance(base, SymDict):
            self._mutating(name, p)
            k = self._int(self._eval(target.slice, p))
            p.env[name] = SymDict(z3.Store(base.vals, k, self._int(v)), z3.Store(base.present, k, TRUE), base.uids)
            return
        if not isinstance(base, SymList) or base.kind == "str":
            raise Unsupported(f"item assignment on {type(base).__name__}")
        self._mutating(name, p)
        if isinstance(target.slice, ast.Slice):
            if target.slice.step is not None:
                raise Unsupported("extended slice assignment")
            lo = None if target.slice.lower is None else self._int(self._eval(target.slice.lower, p))
            hi = base.length if target.slice.upper is None else self._int(self._eval(target.slice.upper, p))
            if not isinstance(v, SymList):
                raise Unsupported("slice assignment from a non-list")
            new = self._concat(self._concat(self._slice(base, None, lo), v), self._slice(base, hi, None))
            p.env[name] = replace(new, uids=base.uids)
            return
        i = self._index(base, self._int(self._eval(target.slice, p)), p)
        kind, term = self._elem_term(v)
        base = _retype(base, _unify_kind(base.kind, kind))
        p.env[name] = replace(base, arr=z3.Store(base.arr, i, term), inner=_max_opt(base.inner, _inner_of(v)))

    # -- expressions -------------------------------------------------------

    def _eval(self, node, p: _Path):
        method = getattr(self, "_e_" + type(node).__name__, None)
        if method is None:
            raise Unsupported(f"unsupported expression: {type(node).__name__}")
        return method(node, p)

    def _e_Constant(self, node, p):
        v = node.value
        if isinstance(v, bool):
            return z3.BoolVal(v)
        if isinstance(v, int):
            return z3.IntVal(v)
        if v is None:
            return NONE
        if isinstance(v, str):
            self._need("lists", "string constant")
            return _str_const(v)
        raise Unsupported(f"unsupported constant: {v!r} ({type(v).__name__})")

    def _e_Name(self, node, p):
        return self._lookup(node.id, p)

    def _e_Attribute(self, node, p):
        base = self._eval(node.value, p)
        if isinstance(base, _Module) and node.attr in _STRING_MODULE:
            return _str_const(_STRING_MODULE[node.attr])
        raise Unsupported(f"attribute access .{node.attr}")

    def _e_Tuple(self, node, p):
        return tuple(self._eval(e, p) for e in node.elts)

    def _e_List(self, node, p):
        self._need("lists", "list literal")
        vals = [self._eval(e, p) for e in node.elts]
        if any(v is BOTTOM for v in vals):
            return BOTTOM
        kind, terms, inner = None, [], 0
        for v in vals:
            k, t = self._elem_term(v)
            kind = _unify_kind(kind, k)
            terms.append(t)
            inner = _max_opt(inner, _inner_of(v))
        return _const_list(terms, kind, inner)

    def _e_Dict(self, node, p):
        self._need("lists", "dict literal")
        if node.keys:
            raise Unsupported("non-empty dict literal")
        return SymDict(z3.K(z3.IntSort(), _ZERO), z3.K(z3.IntSort(), FALSE))

    def _e_ListComp(self, node, p):
        return self._pairs_to_list(self._comp_pairs(node, p))

    _e_GeneratorExp = _e_ListComp

    def _e_IfExp(self, node, p):
        c = self._truthy(self._eval(node.test, p))
        a = self._eval(node.body, p.view(_and(p.alive, c)))
        b = self._eval(node.orelse, p.view(_and(p.alive, _not(c))))
        return _merge(c, a, b)

    def _e_BoolOp(self, node, p):
        is_and = isinstance(node.op, ast.And)
        vals, guard = [], p.alive
        for e in node.values:
            v = self._eval(e, p.view(guard))
            vals.append(v)
            if v is BOTTOM:
                break
            t = self._truthy(v)
            guard = _and(guard, t if is_and else _not(t))
        if all(isinstance(v, z3.BoolRef) for v in vals):
            return _and(*vals) if is_and else _or(*vals)
        result = vals[-1]
        for v in reversed(vals[:-1]):
            t = self._truthy(v)
            result = _merge(t, result, v) if is_and else _merge(t, v, result)
        return result

    def _e_UnaryOp(self, node, p):
        v = self._eval(node.operand, p)
        if v is BOTTOM:
            return BOTTOM
        if isinstance(node.op, ast.Not):
            return _not(self._truthy(v))
        if isinstance(node.op, ast.USub):
            return -self._int(v)
        if isinstance(node.op, ast.UAdd):
            return self._int(v)
        raise Unsupported(f"unsupported unary op: {type(node.op).__name__}")

    def _e_BinOp(self, node, p):
        if isinstance(node.op, ast.Pow) and isinstance(node.right, ast.Constant) \
                and isinstance(node.right.value, float):
            raise Unsupported("float arithmetic (x ** 0.5 outside int(...))")
        a, b = self._eval(node.left, p), self._eval(node.right, p)
        if a is BOTTOM or b is BOTTOM:
            return BOTTOM
        return self._binop(node.op, a, b, p)

    def _binop(self, op, a, b, p):
        if isinstance(a, SymList) or isinstance(b, SymList):
            if isinstance(op, ast.Add) and isinstance(a, SymList) and isinstance(b, SymList):
                return self._concat(a, b)
            if isinstance(op, ast.Mult):
                lst, n = (a, b) if isinstance(a, SymList) else (b, a)
                return self._repeat(lst, self._int(n))
            raise Unsupported(f"operator {type(op).__name__} on lists")
        a, b = self._int(a), self._int(b)
        if isinstance(op, ast.Add):
            return a + b
        if isinstance(op, ast.Sub):
            return a - b
        if isinstance(op, ast.Mult):
            if _const_int(a) is None and _const_int(b) is None:
                for x, y in ((a, b), (b, a)):
                    rng = self._small_range(y, p)
                    if rng:
                        return self._split(y, rng, lambda c, x=x: x * c)
            return a * b
        if isinstance(op, (ast.FloorDiv, ast.Mod)):
            self._raise("ZeroDivisionError", _and(p.alive, b == 0), "division by zero")
            is_div = isinstance(op, ast.FloorDiv)

            a_rng = self._small_range(a, p) if _const_int(a) is None else None

            def by_const(c, a=a):
                if c == 0:
                    return _ZERO
                if a_rng:   # small dividend too: a lookup table, no arithmetic left
                    return self._split(a, a_rng, lambda x: z3.IntVal(x // c if is_div else x % c))
                q = a / c if c > 0 else (-a) / (-c)
                return q if is_div else a - c * q
            if _const_int(b) is not None:
                return by_const(_const_int(b))
            rng = self._small_range(b, p)
            if rng:
                return self._split(b, rng, by_const)
            q = z3.If(b > 0, a / b, (-a) / (-b))
            return q if is_div else a - b * q
        if isinstance(op, ast.Pow):
            e = _const_int(b)
            if e is None or not 0 <= e <= 64:
                raise Unsupported("** with a symbolic or large exponent")
            r = _ONE
            for _ in range(e):
                r = r * a
            return r
        if isinstance(op, (ast.LShift, ast.RShift)):
            s = _const_int(b)
            if s is None or s < 0:
                raise Unsupported("shift by a symbolic amount")
            return a * (2 ** s) if isinstance(op, ast.LShift) else a / (2 ** s)
        if isinstance(op, (ast.BitAnd, ast.BitOr, ast.BitXor)):
            hi = 2 ** _BIT_WIDTH
            self._raise("model_limit", _and(p.alive, z3.Or(a < 0, a >= hi, b < 0, b >= hi)),
                        f"bitwise operand outside [0, 2**{_BIT_WIDTH})")
            f = {ast.BitAnd: z3.And, ast.BitOr: z3.Or, ast.BitXor: z3.Xor}[type(op)]
            return z3.Sum([z3.If(f(x, y), 2 ** i, 0)
                           for i, (x, y) in enumerate(zip(self._bits(a), self._bits(b)))])
        raise Unsupported(f"unsupported operator: {type(op).__name__}")

    def _small_range(self, v, p) -> Optional[Tuple[int, int]]:
        """A small [lo, hi] that v provably stays in on every live path, else None."""
        lo = next((L for L in (0, -8, -32) if not self._feasible(_and(p.alive, v < L))), None)
        if lo is None:
            return None
        for hi in (8, 16, 32, 64):
            if hi - lo + 1 > _SPLIT_MAX:
                return None
            if not self._feasible(_and(p.alive, v > hi)):
                return lo, hi
        return None

    def _split(self, v, rng, fn):
        """fn(v) as a case split over the constants v can take: keeps the formula linear."""
        lo, hi = rng
        r = _ZERO   # v is outside [lo, hi] only on dead paths, where any value will do
        for c in range(hi, lo - 1, -1):
            r = z3.If(v == c, fn(c), r)
        return r

    def _bits(self, v):
        """Fresh Bool bits with v == sum(bit_i * 2**i) whenever 0 <= v < 2**_BIT_WIDTH."""
        bits = [z3.FreshBool("bit") for _ in range(_BIT_WIDTH)]
        self.solver.add(z3.Implies(z3.And(v >= 0, v < 2 ** _BIT_WIDTH),
                                   v == z3.Sum([z3.If(b, 2 ** i, 0) for i, b in enumerate(bits)])))
        return bits

    def _e_Compare(self, node, p):
        left, parts, guard = self._eval(node.left, p), [], p.alive
        for op, right_node in zip(node.ops, node.comparators):
            right = self._eval(right_node, p.view(guard))
            if left is BOTTOM or right is BOTTOM:
                return BOTTOM
            c = self._compare(op, left, right, p)
            parts.append(c)
            guard = _and(guard, c)
            left = right
        return _and(*parts)

    def _compare(self, op, a, b, p):
        if isinstance(op, (ast.Eq, ast.NotEq)):
            e = self._eq(a, b)
            return e if isinstance(op, ast.Eq) else _not(e)
        if isinstance(op, (ast.Is, ast.IsNot)):
            if b is not NONE:
                raise Unsupported("`is` with something other than None")
            e = TRUE if a is NONE else FALSE
            return e if isinstance(op, ast.Is) else _not(e)
        if isinstance(op, (ast.In, ast.NotIn)):
            if not isinstance(b, SymList) or b.kind == "str":
                raise Unsupported("`in` on something other than a list")
            e = _or(*[_and(c, self._eq(x, a)) for c, x in self._value_pairs(b, p)])
            return e if isinstance(op, ast.In) else _not(e)
        a, b = self._int(a), self._int(b)
        return {ast.Lt: a < b, ast.LtE: a <= b, ast.Gt: a > b, ast.GtE: a >= b}[type(op)] \
            if type(op) in (ast.Lt, ast.LtE, ast.Gt, ast.GtE) else self._bad_cmp(op)

    def _bad_cmp(self, op):
        raise Unsupported(f"unsupported comparison: {type(op).__name__}")

    def _eq(self, a, b):
        if isinstance(a, z3.ExprRef) and isinstance(b, z3.ExprRef):
            if a.sort() == b.sort():
                return a == b
            return self._int(a) == self._int(b)
        if isinstance(a, SymList) and isinstance(b, SymList):
            if "str" in (a.kind, b.kind) and a.kind != b.kind:
                return FALSE
            kind = _unify_kind(a.kind, b.kind)
            a, b = _retype(a, kind), _retype(b, kind)
            if a.maxlen is not None and b.maxlen is not None:
                m = min(a.maxlen, b.maxlen)
                return _and(a.length == b.length,
                            *[z3.Select(a.arr, i) == z3.Select(b.arr, i) for i in range(m)])
            return _and(a.length == b.length, a.arr == b.arr)   # canonical arrays
        if a is NONE or b is NONE:
            return TRUE if a is b else FALSE
        if isinstance(a, tuple) and isinstance(b, tuple):
            if len(a) != len(b):
                return FALSE
            return _and(*[self._eq(x, y) for x, y in zip(a, b)])
        if isinstance(a, SymList) != isinstance(b, SymList):
            return FALSE
        raise Unsupported(f"equality between {type(a).__name__} and {type(b).__name__}")

    def _e_Subscript(self, node, p):
        base = self._eval(node.value, p)
        if base is BOTTOM:
            return BOTTOM
        if isinstance(node.slice, ast.Slice):
            sl = node.slice
            if not isinstance(base, SymList):
                raise Unsupported(f"slicing a {type(base).__name__}")
            if sl.step is not None:
                step = self._eval(sl.step, p)
                if sl.lower is None and sl.upper is None and _const_int(self._int(step)) == -1:
                    return self._reversed(base)
                raise Unsupported("slice with a step")
            lo = None if sl.lower is None else self._int(self._eval(sl.lower, p))
            hi = None if sl.upper is None else self._int(self._eval(sl.upper, p))
            return self._slice(base, lo, hi)
        idx = self._eval(node.slice, p)
        if idx is BOTTOM:
            return BOTTOM
        if isinstance(base, SymList):
            return self._elem(base, self._index(base, self._int(idx), p))
        if isinstance(base, SymDict):
            k = self._int(idx)
            self._raise("KeyError", _and(p.alive, _not(z3.Select(base.present, k))), "missing dict key")
            return z3.Select(base.vals, k)
        if isinstance(base, tuple):
            i = _const_int(self._int(idx))
            if i is None:
                raise Unsupported("symbolic tuple index")
            return base[i]
        raise Unsupported(f"indexing a {type(base).__name__}")

    def _e_Call(self, node, p):
        f = node.func
        if isinstance(f, ast.Attribute):
            return self._method(node, p)
        if not isinstance(f, ast.Name):
            raise Unsupported("call of a computed function")
        if self._has_name(f.id, p):
            fn = self._lookup(f.id, p)
            if not isinstance(fn, _FuncRef):
                raise Unsupported(f"call of non-function '{f.id}'")
            if node.keywords:
                raise Unsupported("keyword arguments to a user function")
            args = [self._eval(a, p) for a in node.args]
            if any(a is BOTTOM for a in args):
                return BOTTOM
            return self._call_user(fn, args, p)
        builtin = getattr(self, "_b_" + f.id, None)
        if builtin is None:
            raise Unsupported(f"call to unsupported function: {f.id}()")
        return builtin(node, p)

    def _method(self, node, p):
        f = node.func
        if isinstance(f.value, ast.Name) and f.attr in ("append", "extend", "pop") \
                and isinstance(self._lookup(f.value.id, p), SymList):
            name = f.value.id
            if name not in p.env:
                raise Unsupported(f"mutation of enclosing-scope variable '{name}'")
            self._mutating(name, p)
            base = p.env[name]
            if base.kind == "str":
                raise Unsupported(f"str.{f.attr}()")
            if f.attr == "pop":
                if node.args:
                    raise Unsupported("list.pop(index)")
                self._raise("IndexError", _and(p.alive, base.length <= 0), "pop from empty list")
                last = base.length - 1
                v = self._elem(base, last)
                new = replace(base, arr=z3.Store(base.arr, last, _zero(base.kind)), length=last)
                p.env[name] = new
                return v
            if len(node.args) != 1:
                raise Unsupported(f"list.{f.attr}() arity")
            if f.attr == "append":
                v = self._eval(node.args[0], p)
                if v is BOTTOM:
                    return BOTTOM
                kind, term = self._elem_term(v)
                base = _retype(base, _unify_kind(base.kind, kind))
                new = replace(base, arr=z3.Store(base.arr, base.length, term), length=base.length + 1,
                              maxlen=_maxlen_add(base.maxlen, 1), inner=_max_opt(base.inner, _inner_of(v)))
            else:
                other = self._iterable_list(node.args[0], p)
                if other is BOTTOM:
                    return BOTTOM
                c = self._concat(base, other)
                new = replace(c, uids=base.uids)
            p.env[name] = new
            return NONE
        raise Unsupported(f"method call .{f.attr}()")

    # -- builtins ----------------------------------------------------------

    def _args(self, node, p, n):
        if node.keywords or len(node.args) != n:
            raise Unsupported(f"{node.func.id}() with this argument shape")
        return [self._eval(a, p) for a in node.args]

    def _b_len(self, node, p):
        (v,) = self._args(node, p, 1)
        if isinstance(v, SymList):
            return v.length
        if isinstance(v, tuple):
            return z3.IntVal(len(v))
        if v is BOTTOM:
            return BOTTOM
        raise Unsupported(f"len() of {type(v).__name__}")

    def _b_abs(self, node, p):
        (v,) = self._args(node, p, 1)
        v = self._int(v)
        return z3.If(v < 0, -v, v)

    def _b_ord(self, node, p):
        (v,) = self._args(node, p, 1)
        if not isinstance(v, SymList) or v.kind != "str":
            raise Unsupported("ord() of a non-str")
        self._raise("TypeError", _and(p.alive, v.length != 1), "ord() expects one character")
        return z3.Select(v.arr, 0)

    def _b_int(self, node, p):
        if len(node.args) == 1 and not node.keywords:
            a = node.args[0]
            if isinstance(a, ast.BinOp) and isinstance(a.op, ast.Pow) and isinstance(a.right, ast.Constant) \
                    and a.right.value == 0.5:
                return self._isqrt(self._int(self._eval(a.left, p)), p)
        (v,) = self._args(node, p, 1)
        return self._int(v)

    def _isqrt(self, x, p):
        """int(x ** 0.5) == isqrt(x) exactly for 0 <= x < 2**52."""
        self._raise("TypeError", _and(p.alive, x < 0), "int() of a complex number")
        self._raise("model_limit", _and(p.alive, x >= _FLOAT_EXACT), "x ** 0.5 beyond exact float range")
        rng = self._small_range(x, p)
        if rng:
            return z3.Sum([z3.If(x >= c * c, 1, 0) for c in range(1, int(rng[1] ** 0.5) + 1)])
        s = z3.FreshInt("isqrt")
        self.solver.add(z3.Implies(x >= 0, z3.And(s >= 0, s * s <= x, x < (s + 1) * (s + 1))))
        return s

    def _b_list(self, node, p):
        if len(node.args) != 1 or node.keywords:
            raise Unsupported("list() arity")
        v = self._iterable_list(node.args[0], p)
        if isinstance(v, SymList):
            return replace(v, uids=_fresh())
        return v

    def _b_reversed(self, node, p):
        (v,) = self._args(node, p, 1)
        if not isinstance(v, SymList):
            raise Unsupported("reversed() of a non-list")
        return self._reversed(v)

    def _b_range(self, node, p):
        return self._iterable_list(node, p)

    def _b_any(self, node, p):
        if len(node.args) != 1 or node.keywords:
            raise Unsupported("any() arity")
        return _or(*[_and(g, self._truthy(v)) for g, v in self._pairs(node.args[0], p)])

    def _b_all(self, node, p):
        if len(node.args) != 1 or node.keywords:
            raise Unsupported("all() arity")
        return _not(_or(*[_and(g, _not(self._truthy(v))) for g, v in self._pairs(node.args[0], p)]))

    def _b_sum(self, node, p):
        if len(node.args) != 1 or node.keywords:
            raise Unsupported("sum() arity")
        total = _ZERO
        for g, v in self._pairs(node.args[0], p):
            total = total + _ite(g, self._int(v), _ZERO)
        return total

    def _b_max(self, node, p):
        return self._minmax(node, p, True)

    def _b_min(self, node, p):
        return self._minmax(node, p, False)

    def _minmax(self, node, p, is_max):
        key = None
        for kw in node.keywords:
            if kw.arg == "key" and isinstance(kw.value, ast.Name) and kw.value.id == "len":
                key = "len"
            else:
                raise Unsupported(f"{node.func.id}() keyword '{kw.arg}'")
        if len(node.args) == 1:
            pairs = self._pairs(node.args[0], p)
            self._raise("ValueError", _and(p.alive, _not(_or(*[g for g, _ in pairs]))),
                        f"{node.func.id}() of an empty sequence")
        else:
            pairs = [(TRUE, self._eval(a, p)) for a in node.args]
        cur, have = None, FALSE
        for g, v in pairs:
            if v is BOTTOM:
                continue
            if cur is None:
                cur, have = v, g
                continue
            kv, kc = (v.length, cur.length) if key else (self._int(v), self._int(cur))
            better = kv > kc if is_max else kv < kc
            cur = _merge(_and(g, _or(_not(have), better)), v, cur)
            have = _or(have, g)
        return BOTTOM if cur is None else cur

    # -- iteration ---------------------------------------------------------

    def _plan(self, node, p) -> Tuple[Optional[int], Callable]:
        """How a for-loop / comprehension iterates: (static count or None, t -> (cond, item))."""
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not self._has_name(node.func.id, p):
            name = node.func.id
            if name == "range":
                args = [self._int(v) for v in self._args(node, p, len(node.args))]
                if not 1 <= len(args) <= 3:
                    raise Unsupported("range() arity")
                start, stop, step = (_ZERO, args[0], 1) if len(args) == 1 else (args[0], args[1], 1)
                if len(args) == 3:
                    step = _const_int(args[2])
                    if not step:
                        raise Unsupported("range() with a symbolic or zero step")
                cs, ce = _const_int(start), _const_int(stop)
                count = len(range(cs, ce, step)) if cs is not None and ce is not None else None

                def item(t, start=start, stop=stop, step=step):
                    v = z3.simplify(start + t * step)
                    return (v < stop if step > 0 else v > stop), v
                return count, item
            if name == "enumerate":
                if len(node.args) != 1 or node.keywords:
                    raise Unsupported("enumerate() with a start")
                count, inner = self._plan(node.args[0], p)

                def item(t, inner=inner):
                    c, v = inner(t)
                    return c, (z3.IntVal(t), v)
                return count, item
        v = self._eval(node, p)
        if v is BOTTOM:
            return 0, lambda t: (FALSE, BOTTOM)
        if isinstance(v, SymList):
            return v.maxlen, lambda t: (z3.IntVal(t) < v.length, self._elem(v, z3.IntVal(t)))
        if isinstance(v, tuple):
            return len(v), lambda t: (TRUE, v[t])
        raise Unsupported(f"iteration over {type(v).__name__}")

    def _value_pairs(self, lst: SymList, p):
        return self._pairs_from_plan(lst.maxlen, lambda t: (z3.IntVal(t) < lst.length,
                                                             self._elem(lst, z3.IntVal(t))), p)

    def _pairs_from_plan(self, count, item, p):
        pairs = []
        limit = self.bound if count is None else min(count, self.bound)
        for t in range(limit):
            cond, it = item(t)
            g = _and(p.alive, cond)
            if not self._feasible(g):
                return pairs
            pairs.append((g, it))
        if count is None or count > self.bound:
            cond, _ = item(limit)
            self._raise("bound", _and(p.alive, cond), f"sequence longer than {self.bound}")
        return pairs

    def _pairs(self, node, p):
        """(guard, element) pairs for anything iterable, including generator expressions."""
        if isinstance(node, (ast.GeneratorExp, ast.ListComp)):
            return self._comp_pairs(node, p)
        count, item = self._plan(node, p)
        return self._pairs_from_plan(count, item, p)

    def _comp_pairs(self, node, p):
        self._need("loops", "comprehension")
        if len(node.generators) != 1 or node.generators[0].is_async:
            raise Unsupported("comprehension with several `for` clauses")
        gen = node.generators[0]
        out = []
        for g, it in self._pairs(gen.iter, p):
            q = _Path(p.frame, dict(p.env), g)
            self._assign(gen.target, it, q)
            for cond in gen.ifs:
                q.alive = _and(q.alive, self._truthy(self._eval(cond, q)))
            v = self._eval(node.elt, q)
            if v is not BOTTOM and not z3.is_false(q.alive):
                out.append((q.alive, v))
        return out

    def _iterable_list(self, node, p):
        if isinstance(node, (ast.GeneratorExp, ast.ListComp)) or (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("range", "enumerate") and not self._has_name(node.func.id, p)):
            return self._pairs_to_list(self._pairs(node, p))
        v = self._eval(node, p)
        if v is BOTTOM or isinstance(v, SymList):
            return v
        raise Unsupported(f"expected a list, got {type(v).__name__}")

    def _pairs_to_list(self, pairs) -> SymList:
        kind, arr, length, inner = None, None, _ZERO, 0
        for g, v in pairs:
            inner = _max_opt(inner, _inner_of(v))
            k, term = self._elem_term(v)
            kind = _unify_kind(kind, k)
            if arr is None:
                arr = z3.K(z3.IntSort(), _zero(kind))
            stored = z3.Store(arr, length, term)
            arr = _ite(g, stored, arr)
            length = z3.simplify(_ite(g, length + 1, length))
        if arr is None:
            return _const_list([], None)
        return SymList(arr, length, kind, len(pairs), inner=inner)

    # -- list primitives ---------------------------------------------------

    def _elem_term(self, v):
        if _is_int(v):
            return "int", v
        if isinstance(v, z3.BoolRef):
            return "int", z3.If(v, _ONE, _ZERO)
        if isinstance(v, SymList):
            if v.kind == "str":
                raise Unsupported("lists of strings")
            if v.kind == "list":
                raise Unsupported("lists nested more than two levels")
            arr = v.arr if v.kind == "int" else _EMPTY_INTS
            return "list", IntList.mk(arr, v.length)
        raise Unsupported(f"list element of type {type(v).__name__}")

    def _index(self, lst: SymList, idx, p):
        n = lst.length
        c = _const_int(idx)
        i = idx if c is not None and c >= 0 else z3.If(idx < 0, idx + n, idx)
        self._raise("IndexError", _and(p.alive, z3.Or(i < 0, i >= n)), "index out of range")
        return i

    def _elem(self, lst: SymList, i):
        if lst.kind is None:
            return BOTTOM
        e = z3.Select(lst.arr, i)
        if lst.kind == "int":
            return e
        if lst.kind == "str":
            return SymList(z3.Store(_EMPTY_INTS, 0, e), _ONE, "str", 1)
        return SymList(IntList.elems(e), IntList.size(e), "int", lst.inner, lst.uids)

    def _concat(self, a: SymList, b: SymList) -> SymList:
        if not isinstance(a, SymList) or not isinstance(b, SymList):
            raise Unsupported("concatenating a list with a non-list")
        if "str" in (a.kind, b.kind) and a.kind != b.kind:
            raise Unsupported("concatenating str with list")
        kind = _unify_kind(a.kind, b.kind)
        a, b = _retype(a, kind), _retype(b, kind)
        maxlen = _maxlen_add(a.maxlen, b.maxlen)
        inner = _max_opt(a.inner, b.inner)
        if _const_int(a.length) == 0:
            return SymList(b.arr, b.length, kind, b.maxlen, inner=inner)
        nb = _const_int(b.length)
        if nb is not None and nb <= 4:
            arr = a.arr
            for t in range(nb):
                arr = z3.Store(arr, a.length + t, z3.Select(b.arr, t))
            return SymList(arr, z3.simplify(a.length + nb), kind, maxlen, inner=inner)
        j = z3.Int("__j")
        arr = z3.Lambda([j], z3.If(j < a.length, z3.Select(a.arr, j), z3.Select(b.arr, j - a.length)))
        return SymList(arr, a.length + b.length, kind, maxlen, inner=inner)

    def _slice(self, lst: SymList, lo, hi) -> SymList:
        n = lst.length

        def clamp(v, default):
            if v is None:
                return default
            c = _const_int(v)
            v2 = v if c is not None and c >= 0 else z3.If(v < 0, v + n, v)
            return z3.If(v2 < 0, _ZERO, z3.If(v2 > n, n, v2))
        lo2, hi2 = clamp(lo, _ZERO), clamp(hi, n)
        if lo is None and hi is None:
            return replace(lst, uids=_fresh())
        new_len = z3.If(hi2 > lo2, hi2 - lo2, _ZERO)
        j = z3.Int("__j")
        arr = z3.Lambda([j], z3.If(z3.And(j >= 0, j < new_len), z3.Select(lst.arr, j + lo2), _zero(lst.kind)))
        return SymList(arr, new_len, lst.kind, lst.maxlen, inner=lst.inner)

    def _reversed(self, lst: SymList) -> SymList:
        n, j = lst.length, z3.Int("__j")
        arr = z3.Lambda([j], z3.If(z3.And(j >= 0, j < n), z3.Select(lst.arr, n - 1 - j), _zero(lst.kind)))
        return SymList(arr, n, lst.kind, lst.maxlen, inner=lst.inner)

    def _repeat(self, lst: SymList, n) -> SymList:
        if _const_int(lst.length) != 1:
            raise Unsupported("repeating a list that is not a one-element literal")
        v, j = z3.Select(lst.arr, 0), z3.Int("__j")
        length = z3.If(n > 0, n, _ZERO)
        arr = z3.Lambda([j], z3.If(z3.And(j >= 0, j < length), v, _zero(lst.kind)))
        c = _const_int(n)
        return SymList(arr, length, lst.kind, None if c is None else max(c, 0), inner=lst.inner)

    # -- coercions ---------------------------------------------------------

    def _int(self, v):
        if _is_int(v):
            return v
        if isinstance(v, z3.BoolRef):
            return z3.If(v, _ONE, _ZERO)
        raise Unsupported(f"expected an int, got {type(v).__name__}")

    def _truthy(self, v):
        if isinstance(v, z3.BoolRef):
            return v
        if _is_int(v):
            return v != 0
        if isinstance(v, SymList):
            return v.length > 0
        if v is NONE or v is BOTTOM:
            return FALSE
        if isinstance(v, tuple):
            return z3.BoolVal(bool(v))
        raise Unsupported(f"truth value of {type(v).__name__}")


# ---------------------------------------------------------------------------
# Inputs and counterexamples
# ---------------------------------------------------------------------------

_TYPE_REASONS = {
    "node": "graph / linked-list Node objects are not modeled",
    "dict": "dict arguments are not modeled",
    "float": "floats are not modeled",
    "list[tuple]": "lists of tuples are not modeled",
    "list[float|str]": "mixed float/str lists are not modeled",
    "list[int|str]": "mixed int/str lists are not modeled",
    "list[any]": "arbitrarily nested lists are not modeled",
    "list[node]": "graph Node objects are not modeled",
}


def parse_arg_types(spec: str) -> List[Tuple[str, str]]:
    out = []
    for part in spec.split(","):
        name, _, typ = part.partition(":")
        out.append((name.strip(), typ.replace(" ", "")))
    return out


def _make_inputs(arg_types, opts: BoundedOptions):
    env, cons = {}, []
    for name, typ in arg_types:
        if typ == "int":
            env[name] = z3.Int(name)
        elif typ in ("list[int]", "str"):
            if not opts.lists:
                raise Unsupported("list/str arguments need extension (c) 'lists', not enabled at this step")
            cap = opts.list_cap
            n = z3.Int(f"{name}.len")
            xs = [z3.Int(f"{name}[{i}]") for i in range(cap)]
            arr = _EMPTY_INTS
            for i, x in enumerate(xs):
                arr = z3.Store(arr, i, z3.If(i < n, x, _ZERO))
            cons += [n >= 0, n <= cap]
            if typ == "str":   # printable ASCII characters
                cons += [z3.Implies(i < n, z3.And(x >= 32, x <= 126)) for i, x in enumerate(xs)]
            env[name] = SymList(arr, n, "int" if typ == "list[int]" else "str", cap)
        else:
            raise Unsupported(_TYPE_REASONS.get(typ, f"argument type '{typ}' is not modeled"))
    return env, cons


def concrete(model: z3.ModelRef, v, limit: int = 64):
    """Turn a symbolic value into a plain Python value under `model`."""
    def num(t):
        r = model.eval(t, model_completion=True)
        return r.as_long() if z3.is_int_value(r) else None
    if v is NONE:
        return None
    if isinstance(v, z3.BoolRef):
        return z3.is_true(model.eval(v, model_completion=True))
    if _is_int(v):
        return num(v)
    if isinstance(v, SymList):
        n = min(num(v.length) or 0, limit)
        if v.kind == "list":
            out = []
            for i in range(n):
                e = z3.Select(v.arr, i)
                m = min(num(IntList.size(e)) or 0, limit)
                out.append([num(z3.Select(IntList.elems(e), k)) for k in range(m)])
            return out
        items = [num(z3.Select(v.arr, i)) for i in range(n)]
        if v.kind == "str":
            return "".join(chr(x) if x is not None and 0 <= x < 0x110000 else "?" for x in items)
        return items
    if isinstance(v, tuple):
        return tuple(concrete(model, x, limit) for x in v)
    return repr(v)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_BAD_CODES = ("none", "bound", "IndexError", "KeyError", "ZeroDivisionError", "ValueError", "TypeError")


def _prepare(arg_types: str, pre: str, helpers: str, opts: BoundedOptions):
    solver = _Solver()
    inputs, cons = _make_inputs(parse_arg_types(arg_types), opts)
    solver.add(*cons)
    spec_globals = load_globals(helpers, functions_only=True)
    pre_ex = _Executor(spec_globals, solver, opts, program=False)
    pre_term = pre_ex.eval_spec(pre, inputs, TRUE)
    return solver, inputs, spec_globals, pre_term, pre_ex.issue()


def _check(solver: _Solver, cond, opts):
    return solver.check(cond, opts.timeout_ms)


def _code_name(model, code):
    return CODE_NAMES.get(model.eval(code, model_completion=True).as_long(), "unknown")


def verify_bounded(source: str, fn_name: str, arg_types: str, post: str, pre: str = "True",
                   helpers: str = "", options: Optional[BoundedOptions] = None) -> BoundedResult:
    """Check `post` (over the args and `result`) for every input meeting `pre`, up to the bound.

    `arg_types` is e.g. "arr: list[int], k: int". `pre`/`post` are Python
    expressions that may call the helper functions defined in `helpers`.
    Raises `UnsupportedConstructError` when the program or spec leaves the
    modeled subset.
    """
    opts = options or BoundedOptions()
    k = opts.bound
    solver, inputs, spec_globals, pre_term, pre_issue = _prepare(arg_types, pre, helpers, opts)
    res, _ = _check(solver, _and(pre_issue), opts)
    if res == z3.sat:
        raise Unsupported("precondition cannot be evaluated within the spec bound")
    solver.add(pre_term)
    if _check(solver, TRUE, opts)[0] == z3.unsat:
        return BoundedResult("unsupported", "precondition is unsatisfiable", k)

    out = _Executor(load_globals(source), solver, opts, program=True).run(
        fn_name, [inputs[n] for n, _ in parse_arg_types(arg_types)])
    value_ok = out.code == CODES["value"]
    if out.value is BOTTOM:
        post_term, post_issue = TRUE, FALSE
    else:
        post_ex = _Executor(spec_globals, solver, opts, program=False)
        post_term = post_ex.eval_spec(post, {**inputs, "result": out.value}, value_ok)
        post_issue = post_ex.issue()
    bad = _or(_and(value_ok, _not(post_term)), *[out.code == CODES[c] for c in _BAD_CODES])

    res, model = _check(solver, bad, opts)
    if res == z3.sat:
        kind = _code_name(model, out.code)
        cex = {n: concrete(model, inputs[n]) for n, _ in parse_arg_types(arg_types)}
        predicted = concrete(model, out.value) if kind == "value" else None
        detail = "postcondition violated" if kind == "value" else f"execution ends with {kind}"
        return BoundedResult("refuted", detail, k, cex, kind, predicted)
    if res == z3.unknown:
        return BoundedResult("unknown", f"solver returned unknown ({solver.reason})", k)

    limit = _or(out.code == CODES["model_limit"], post_issue)
    res, model = _check(solver, limit, opts)
    if res != z3.unsat:
        return BoundedResult("unsupported", "a reachable input leaves the exact model "
                             "(bitwise/float range or spec bound)", k)
    res, _ = _check(solver, out.code == -1, opts)
    if res != z3.unsat:
        raise Unsupported("internal: some path has no outcome")
    return BoundedResult("proved_up_to_bound",
                         f"holds for every input meeting the precondition with lists of length <= "
                         f"{opts.list_cap}, loops <= {k} iterations and <= {k} nested calls", k)


def check_equivalence(source_a: str, source_b: str, fn_name: str, arg_types: str, pre: str = "True",
                      helpers: str = "", options: Optional[BoundedOptions] = None) -> EquivResult:
    """Search for an input (meeting `pre`) on which A and B behave differently.

    Behaviour = return value, None, or exception type. Inputs on which B (the
    reference) exceeds the bound, or either side leaves the exact model, are
    excluded; A exceeding the bound while B finishes counts as a difference.
    """
    opts = options or BoundedOptions()
    solver, inputs, _, pre_term, pre_issue = _prepare(arg_types, pre, helpers, opts)
    res, _ = _check(solver, _and(pre_issue), opts)
    if res == z3.sat:
        raise Unsupported("precondition cannot be evaluated within the spec bound")
    solver.add(pre_term)
    args = [inputs[n] for n, _ in parse_arg_types(arg_types)]
    out_a = _Executor(load_globals(source_a), solver, opts, program=True).run(fn_name, args)
    out_b = _Executor(load_globals(source_b), solver, opts, program=True).run(fn_name, args)
    excluded = _or(out_b.code == CODES["bound"], out_b.code == CODES["model_limit"],
                   out_a.code == CODES["model_limit"])
    both_values = _and(out_a.code == 0, out_b.code == 0)
    if out_a.value is BOTTOM or out_b.value is BOTTOM:
        same_value = FALSE
    else:
        try:
            same_value = _Executor({}, solver, opts, program=False)._eq(out_a.value, out_b.value)
        except Unsupported:
            same_value = FALSE
    diff = _or(out_a.code != out_b.code, _and(both_values, _not(same_value)))
    res, model = _check(solver, _and(_not(excluded), diff), opts)
    k = opts.bound
    if res == z3.sat:
        ka, kb = _code_name(model, out_a.code), _code_name(model, out_b.code)
        return EquivResult(
            "different", "implementations disagree", k,
            {n: concrete(model, inputs[n]) for n, _ in parse_arg_types(arg_types)}, ka, kb,
            concrete(model, out_a.value) if ka == "value" else None,
            concrete(model, out_b.value) if kb == "value" else None)
    if res == z3.unknown:
        return EquivResult("unknown", f"solver returned unknown ({solver.reason})", k)
    return EquivResult("equivalent_up_to_bound",
                       f"no difference for inputs meeting the precondition with lists of length <= "
                       f"{opts.list_cap}, loops <= {k} iterations and <= {k} nested calls", k)
