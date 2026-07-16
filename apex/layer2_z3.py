"""Layer 2 - Neuro-Symbolic Enforcement, for real this time.

The original `layer2_neuro_symbolic_grounding.py` claimed to use Z3 but actually
did `if "UNSAT" in text`. This module uses the real `z3-solver`:

- `Z3Verifier.check(spec)` parses SMT-LIB v2 text and actually solves it.
- A satisfiable safety-violation encoding yields a concrete counterexample
  (the model), which is exactly what you want a verifier to hand you.
- Malformed SMT from an LLM raises `SpecCompilationError` with the real parser
  error attached - this becomes the negative-reward signal for Layer 1.

Interpretation convention used throughout APEX:
  We encode the NEGATION of the safety property (i.e. "a bad state is
  reachable"). Therefore:
    * z3 result `unsat`  -> the bad state is impossible -> PROPERTY HOLDS (safe)
    * z3 result `sat`    -> the bad state is reachable  -> PROPERTY VIOLATED
                            (`model` is a counterexample / witness)
    * z3 result `unknown`-> solver could not decide -> treated as NOT verified
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional

import z3

from apex.llm_client import BaseLLMClient

logger = logging.getLogger("apex.layer2")


class Verdict(str, Enum):
    SAFE = "SAFE"            # property holds (unsat negation)
    VIOLATED = "VIOLATED"    # counterexample exists (sat negation)
    UNKNOWN = "UNKNOWN"      # solver could not decide


class SpecCompilationError(Exception):
    """Raised when SMT-LIB text cannot be parsed by Z3."""


@dataclass
class VerdictReport:
    verdict: Verdict
    passed: bool                    # True only when verdict == SAFE
    counterexample: Optional[Dict[str, str]] = field(default=None)
    detail: str = ""

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        base = f"[Layer2] {self.verdict.value}"
        if self.counterexample:
            base += f" counterexample={self.counterexample}"
        return base


class Z3Verifier:
    """Runs real Z3 over SMT-LIB v2 specifications."""

    def check(self, smt_spec: str) -> VerdictReport:
        if not smt_spec or not smt_spec.strip():
            raise SpecCompilationError("empty SMT specification")

        try:
            # parse_smt2_string returns the asserted constraints; it raises
            # z3.Z3Exception on malformed input.
            constraints = z3.parse_smt2_string(smt_spec)
        except z3.Z3Exception as exc:
            raise SpecCompilationError(f"Z3 could not parse specification: {exc}") from exc

        solver = z3.Solver()
        solver.add(constraints)
        result = solver.check()

        if result == z3.unsat:
            logger.info("[Layer2] Z3 proved property holds (negation unsat).")
            return VerdictReport(Verdict.SAFE, True, detail="negation unsatisfiable")

        if result == z3.sat:
            model = solver.model()
            counter = {str(d.name()): str(model[d]) for d in model.decls()}
            logger.warning("[Layer2] Z3 found counterexample: %s", counter)
            return VerdictReport(
                Verdict.VIOLATED, False, counterexample=counter,
                detail="negation satisfiable (bad state reachable)",
            )

        logger.warning("[Layer2] Z3 returned unknown.")
        return VerdictReport(Verdict.UNKNOWN, False, detail="solver returned unknown")


# ---------------------------------------------------------------------------
# LLM-driven spec generation (optional path used by the full pipeline).
# ---------------------------------------------------------------------------

_SPEC_SYSTEM_PROMPT = """You are a formal-methods translator.
You are BANNED from writing prose or program code.
Translate the requested safety property into SMT-LIB v2 that encodes the
NEGATION of the property (i.e. assert that a violating state is reachable),
followed by (check-sat).
Output ONLY raw SMT-LIB v2 text. No markdown, no explanation.
"""


class SpecGenerator:
    """Uses an LLM to translate a constrained property description to SMT-LIB."""

    def __init__(self, client: BaseLLMClient) -> None:
        self.client = client

    async def generate(self, property_description: str) -> str:
        prompt = f"{_SPEC_SYSTEM_PROMPT}\nPROPERTY:\n{property_description}\n"
        text = await self.client.generate(prompt, temperature=0.0)
        # Strip accidental code fences; keep it permissive, Z3 will validate.
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
        return cleaned


async def verify_property(client: BaseLLMClient, property_description: str) -> VerdictReport:
    """End-to-end Layer 2: LLM writes the spec, Z3 decides it."""
    spec = await SpecGenerator(client).generate(property_description)
    return Z3Verifier().check(spec)
