"""Command-line entry point for the APEX pipeline.

Examples:
    # Fully offline demo (no API key needed) using the deadlock SMT spec:
    python -m apex --offline --demo

    # Real run against a target repo, verifying a natural-language property:
    python -m apex --target ./some_repo --property "x must stay positive"
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from apex.llm_client import MockLLMClient, build_client
from apex.orchestrator import ApexOrchestrator, OrchestratorConfig
from apex.layer1_mcts import SearchBudget

# A safe SMT spec (unsat negation) used by the offline demo so Layer 2 passes.
_DEMO_SAFE_SPEC = "(declare-const x Int)(assert (> x 0))(assert (< x 0))(check-sat)"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="apex", description="APEX verification pipeline")
    p.add_argument("--request", default="Build a race-condition-free counter",
                   help="the design request fed to Layer 1")
    p.add_argument("--target", type=Path, default=None,
                   help="directory to run Layer 3 static analysis on")
    p.add_argument("--property", dest="property", default="",
                   help="natural-language safety property for Layer 2")
    p.add_argument("--spec", default=None, help="direct SMT-LIB v2 for Layer 2")
    p.add_argument("--offline", action="store_true",
                   help="use the deterministic mock LLM (no API key)")
    p.add_argument("--demo", action="store_true",
                   help="run a self-contained offline demo that PASSES")
    p.add_argument("--cegis", action="store_true",
                   help="Layer 2: counterexample-guided spec repair")
    p.add_argument("--adversaries", type=int, default=1,
                   help="Layer 4: number of adversaries (>1 = consensus)")
    p.add_argument("--verify-code", type=Path, default=None,
                   help="verify a Python file against --postcondition and exit")
    p.add_argument("--postcondition", default="result >= 0",
                   help="postcondition for --verify-code")
    p.add_argument("--model", default="gemini-1.5-flash")
    return p.parse_args()


async def _run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")

    # Code-verification mode is synchronous and needs no LLM.
    if args.verify_code is not None:
        from apex.pyverify import verify_function, UnsupportedConstructError
        source = args.verify_code.read_text(encoding="utf-8")
        try:
            result = verify_function(source, args.postcondition)
        except UnsupportedConstructError as exc:
            print(f"UNSUPPORTED: {exc}")
            return 2
        print(result)
        return 0 if result.proven else 1

    if args.demo:
        # Deterministic offline pass: mock scores high, provides a safe spec.
        client = MockLLMClient(
            responder=lambda prompt: (
                "candidate a\ncandidate b\ncandidate c" if "refinements" in prompt
                else _DEMO_SAFE_SPEC if "SMT-LIB" in prompt
                else '{"is_breached": false, "confidence": 0.99, "vulnerability_vector": "SECURE"}'
                if "Destroyer" in prompt
                else "0.95"
            )
        )
        config = OrchestratorConfig(
            layer2_spec=_DEMO_SAFE_SPEC,
            search_budget=SearchBudget(max_nodes=8, max_llm_calls=16),
        )
    else:
        client = build_client(args.offline, model=args.model) if not args.offline \
            else MockLLMClient(default="0.95")
        config = OrchestratorConfig(
            layer2_property=args.property,
            layer2_spec=args.spec,
            layer3_target=args.target,
            enable_cegis=args.cegis,
            num_adversaries=args.adversaries,
            search_budget=SearchBudget(max_nodes=12, max_llm_calls=24),
        )

    orchestrator = ApexOrchestrator(client, config)
    result = await orchestrator.run(args.request)
    print("\n" + result.summary())
    return 0 if result.status == "PASSED" else 1


def main() -> int:
    return asyncio.run(_run(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
