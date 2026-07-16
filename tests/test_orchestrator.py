"""End-to-end orchestrator tests (offline). Proves PASS and each FAIL path."""

from pathlib import Path

import pytest

from apex.layer1_mcts import SearchBudget
from apex.llm_client import MockLLMClient
from apex.orchestrator import ApexOrchestrator, OrchestratorConfig

FIXTURES = Path(__file__).parent / "fixtures"

SAFE_SPEC = "(declare-const x Int)(assert (> x 0))(assert (< x 0))(check-sat)"
VIOLATED_SPEC = "(declare-const x Int)(assert (> x 10))(check-sat)"
SECURE = '{"is_breached": false, "confidence": 0.99, "vulnerability_vector": "SECURE"}'
BREACH = '{"is_breached": true, "confidence": 0.9, "vulnerability_vector": "boom"}'


def _client(score="0.95", verdict=SECURE):
    def responder(prompt: str) -> str:
        if "refinements" in prompt:
            return "a\nb\nc"
        if "Destroyer" in prompt:
            return verdict
        if "SMT-LIB" in prompt:
            return SAFE_SPEC
        return score
    return MockLLMClient(responder=responder)


def _config(**kw):
    kw.setdefault("search_budget", SearchBudget(max_nodes=6, max_llm_calls=12))
    return OrchestratorConfig(**kw)


@pytest.mark.asyncio
async def test_full_pipeline_passes():
    orch = ApexOrchestrator(_client(), _config(layer2_spec=SAFE_SPEC))
    result = await orch.run("Build a safe counter")
    assert result.status == "PASSED"
    assert [m.name for m in result.metrics][0] == "Layer1-MCTS"


@pytest.mark.asyncio
async def test_layer1_failure_halts():
    orch = ApexOrchestrator(_client(score="0.10"), _config(layer2_spec=SAFE_SPEC))
    result = await orch.run("weak design")
    assert result.status == "FAILED"
    assert result.failed_layer == "Layer 1 (MCTS)"


@pytest.mark.asyncio
async def test_layer2_failure_halts_with_counterexample():
    orch = ApexOrchestrator(_client(), _config(layer2_spec=VIOLATED_SPEC))
    result = await orch.run("design")
    assert result.status == "FAILED"
    assert result.failed_layer == "Layer 2 (Z3)"


@pytest.mark.asyncio
async def test_layer3_failure_halts_on_vulnerable_repo():
    orch = ApexOrchestrator(
        _client(), _config(layer2_spec=SAFE_SPEC, layer3_target=FIXTURES / "vulnerable")
    )
    result = await orch.run("design")
    assert result.status == "FAILED"
    assert result.failed_layer == "Layer 3 (Hive)"


@pytest.mark.asyncio
async def test_layer4_breach_halts():
    orch = ApexOrchestrator(_client(verdict=BREACH), _config(layer2_spec=SAFE_SPEC))
    result = await orch.run("design")
    assert result.status == "FAILED"
    assert result.failed_layer == "Layer 4 (Gate)"
