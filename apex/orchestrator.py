"""APEX orchestrator - wires the four REAL layers together.

This ports the genuinely good ideas from the old `apex.txt` V-7.0 sketch
(unified state, metrics collection, per-layer status, structured result) onto
the real layer APIs, and fixes its mistakes:

- It called methods that never existed. This calls the real APIs.
- It ran Layers 2 and 3 in parallel, which contradicts the dependency chain.
  Here the layers run in the correct order: L1 -> L2 -> L3 -> L4. The only
  concurrency is *inside* a layer (L3 workers, L1 candidate scoring).
- Every layer can genuinely FAIL and halt the pipeline.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional

from apex.cegis import CEGISLoop
from apex.layer1_mcts import MCTSSearch, SearchBudget
from apex.layer2_z3 import Verdict, Z3Verifier, verify_property
from apex.layer3_hive import BanditHive
from apex.layer4_gate import AdversarialGate, PipelineBreachError
from apex.llm_client import BaseLLMClient

logger = logging.getLogger("apex.orchestrator")


class LayerStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"


@dataclass
class LayerMetric:
    name: str
    status: LayerStatus
    duration: float
    detail: str = ""


@dataclass
class PipelineResult:
    status: str                      # "PASSED" or "FAILED"
    failed_layer: Optional[str] = None
    error: str = ""
    metrics: List[LayerMetric] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"Pipeline: {self.status}"]
        if self.failed_layer:
            lines.append(f"Failed at: {self.failed_layer} - {self.error}")
        for m in self.metrics:
            lines.append(f"  [{m.status.value:7}] {m.name:16} {m.duration:6.2f}s {m.detail}")
        return "\n".join(lines)


@dataclass
class OrchestratorConfig:
    layer1_threshold: float = 0.85
    layer2_property: str = ""            # if empty, Layer 2 uses a supplied spec
    layer2_spec: Optional[str] = None    # direct SMT-LIB (skips LLM spec-gen)
    layer3_target: Optional[Path] = None
    layer3_high_severity_budget: int = 0
    layer4_timeout: float = 10.0
    search_budget: SearchBudget = field(default_factory=SearchBudget)
    # Advanced modes:
    enable_cegis: bool = False           # Layer 2: counterexample-guided repair
    cegis_rounds: int = 4
    num_adversaries: int = 1             # Layer 4: >1 enables consensus


class ApexOrchestrator:
    def __init__(self, client: BaseLLMClient, config: OrchestratorConfig) -> None:
        self.client = client
        self.config = config

    async def run(self, request: str) -> PipelineResult:
        metrics: List[LayerMetric] = []

        # ---- Layer 1: MCTS design search -------------------------------
        t0 = time.monotonic()
        search = MCTSSearch(
            self.client,
            threshold=self.config.layer1_threshold,
            budget=self.config.search_budget,
        )
        l1 = await search.search(request)
        d1 = time.monotonic() - t0
        if not l1.passed:
            metrics.append(LayerMetric("Layer1-MCTS", LayerStatus.FAIL, d1,
                                       f"best score {l1.best_score:.2f} < {self.config.layer1_threshold}"))
            return PipelineResult("FAILED", "Layer 1 (MCTS)",
                                  "search did not reach confidence threshold", metrics)
        metrics.append(LayerMetric("Layer1-MCTS", LayerStatus.PASS, d1,
                                   f"score {l1.best_score:.2f}, {l1.nodes_created} nodes"))

        # ---- Layer 2: Z3 formal verification (optionally CEGIS) --------
        t0 = time.monotonic()
        prop = self.config.layer2_property or l1.best_design
        if self.config.enable_cegis and self.config.layer2_spec is None:
            cegis = CEGISLoop(self.client, max_rounds=self.config.cegis_rounds)
            cres = await cegis.refine(prop)
            d2 = time.monotonic() - t0
            if not cres.passed:
                metrics.append(LayerMetric("Layer2-Z3", LayerStatus.FAIL, d2,
                                           f"CEGIS gave up after {cres.rounds_used} rounds"))
                return PipelineResult("FAILED", "Layer 2 (Z3/CEGIS)",
                                      "could not repair spec to a proof", metrics)
            metrics.append(LayerMetric("Layer2-Z3", LayerStatus.PASS, d2,
                                       f"proven safe via CEGIS in {cres.rounds_used} round(s)"))
        else:
            if self.config.layer2_spec is not None:
                l2 = Z3Verifier().check(self.config.layer2_spec)
            else:
                l2 = await verify_property(self.client, prop)
            d2 = time.monotonic() - t0
            if l2.verdict != Verdict.SAFE:
                detail = f"{l2.verdict.value}"
                if l2.counterexample:
                    detail += f" counterexample={l2.counterexample}"
                metrics.append(LayerMetric("Layer2-Z3", LayerStatus.FAIL, d2, detail))
                return PipelineResult("FAILED", "Layer 2 (Z3)",
                                      f"property not proven safe ({l2.verdict.value})", metrics)
            metrics.append(LayerMetric("Layer2-Z3", LayerStatus.PASS, d2, "property proven safe"))

        # ---- Layer 3: static-analysis hive -----------------------------
        if self.config.layer3_target is not None:
            t0 = time.monotonic()
            hive = BanditHive(high_severity_budget=self.config.layer3_high_severity_budget)
            l3 = await hive.run(self.config.layer3_target)
            d3 = time.monotonic() - t0
            if not l3.passed:
                metrics.append(LayerMetric("Layer3-Hive", LayerStatus.FAIL, d3,
                                           f"{l3.high_severity_count} high-severity, {l3.crashed_count} crashed"))
                return PipelineResult("FAILED", "Layer 3 (Hive)",
                                      "static analysis found blocking issues", metrics)
            metrics.append(LayerMetric("Layer3-Hive", LayerStatus.PASS, d3,
                                       f"{len(l3.all_findings())} findings, none blocking"))
            l3_payload = {
                "findings": [f.__dict__ for f in l3.all_findings()],
                "design": l1.best_design,
            }
        else:
            metrics.append(LayerMetric("Layer3-Hive", LayerStatus.SKIPPED, 0.0, "no target dir"))
            l3_payload = {"design": l1.best_design}

        # ---- Layer 4: adversarial gate (optionally multi-adversary) ----
        t0 = time.monotonic()
        gate = AdversarialGate(self.client, timeout_seconds=self.config.layer4_timeout)
        try:
            if self.config.num_adversaries > 1:
                rep = await gate.evaluate_consensus(
                    l3_payload, num_adversaries=self.config.num_adversaries
                )
                detail = f"consensus {rep.secure_votes}/{rep.num_adversaries} secure"
            else:
                await gate.evaluate(l3_payload)
                detail = "certified secure"
            d4 = time.monotonic() - t0
            metrics.append(LayerMetric("Layer4-Gate", LayerStatus.PASS, d4, detail))
        except PipelineBreachError as exc:
            d4 = time.monotonic() - t0
            metrics.append(LayerMetric("Layer4-Gate", LayerStatus.FAIL, d4, str(exc)))
            return PipelineResult("FAILED", "Layer 4 (Gate)", str(exc), metrics)

        return PipelineResult("PASSED", metrics=metrics)
