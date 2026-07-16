"""CEGIS - Counterexample-Guided spec repair (real closed-loop verification).

The original APEX README *claimed* "failure stack trace fed back as negative
reward" but implemented nothing. This module is the real version, modeled on
counterexample-guided inductive synthesis (CEGIS), a standard technique in
program synthesis research:

    1. LLM proposes a formal spec for the property.
    2. Z3 checks it.
    3. If Z3 finds a counterexample (or the spec doesn't parse), the CONCRETE
       failure - actual violating values, or the actual parser error - is fed
       back to the LLM, which must produce a repaired spec.
    4. Repeat within a bounded number of rounds. Fail-closed if never proven.

The counterexample is the whole point: the model isn't told "try again", it is
shown exactly WHICH values break its previous attempt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from apex.layer2_z3 import (
    SpecCompilationError,
    SpecGenerator,
    Verdict,
    VerdictReport,
    Z3Verifier,
)
from apex.llm_client import BaseLLMClient

logger = logging.getLogger("apex.cegis")


@dataclass
class CEGISRound:
    round_number: int
    spec: str
    outcome: str            # SAFE / VIOLATED / UNKNOWN / PARSE_ERROR
    feedback: str = ""      # what was sent back to the model


@dataclass
class CEGISResult:
    passed: bool
    rounds_used: int
    final_spec: str
    final_report: Optional[VerdictReport]
    history: List[CEGISRound] = field(default_factory=list)


_REPAIR_PROMPT = """You are a formal-methods translator repairing a rejected
SMT-LIB v2 specification. You are BANNED from prose and program code.

PROPERTY TO ENCODE (as the NEGATION - assert a violating state is reachable,
end with (check-sat)):
{property}

YOUR PREVIOUS ATTEMPT:
{previous_spec}

WHY IT WAS REJECTED:
{failure}

Output ONLY the corrected raw SMT-LIB v2 text. No markdown, no explanation.
"""


class CEGISLoop:
    def __init__(
        self,
        client: BaseLLMClient,
        *,
        max_rounds: int = 4,
        verifier: Optional[Z3Verifier] = None,
    ) -> None:
        self.client = client
        self.max_rounds = max_rounds
        self.verifier = verifier or Z3Verifier()
        self.generator = SpecGenerator(client)

    async def _repair(self, prop: str, previous_spec: str, failure: str) -> str:
        prompt = _REPAIR_PROMPT.format(
            property=prop, previous_spec=previous_spec, failure=failure
        )
        text = await self.client.generate(prompt, temperature=0.2)
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
        return cleaned

    async def refine(
        self, property_description: str, *, initial_spec: Optional[str] = None
    ) -> CEGISResult:
        history: List[CEGISRound] = []
        spec = initial_spec or await self.generator.generate(property_description)

        for round_no in range(1, self.max_rounds + 1):
            try:
                report = self.verifier.check(spec)
            except SpecCompilationError as exc:
                failure = f"Z3 parse error: {exc}"
                history.append(CEGISRound(round_no, spec, "PARSE_ERROR", failure))
                logger.warning("[CEGIS] round %d: parse error, repairing", round_no)
                if round_no == self.max_rounds:
                    break
                spec = await self._repair(property_description, spec, failure)
                continue

            if report.verdict == Verdict.SAFE:
                history.append(CEGISRound(round_no, spec, "SAFE"))
                logger.info("[CEGIS] proven safe in round %d", round_no)
                return CEGISResult(True, round_no, spec, report, history)

            if report.verdict == Verdict.VIOLATED:
                failure = (
                    "Z3 found a CONCRETE counterexample proving your encoding "
                    f"admits a violating state: {report.counterexample}. "
                    "Adjust the constraints so this state is impossible if and "
                    "only if the real property holds."
                )
            else:  # UNKNOWN
                failure = "Z3 returned 'unknown'. Simplify to a decidable encoding."

            history.append(CEGISRound(round_no, spec, report.verdict.value, failure))
            logger.warning("[CEGIS] round %d: %s, repairing", round_no, report.verdict.value)
            if round_no == self.max_rounds:
                return CEGISResult(False, round_no, spec, report, history)
            spec = await self._repair(property_description, spec, failure)

        # Only reachable via parse-error exhaustion.
        return CEGISResult(False, self.max_rounds, spec, None, history)
