"""Tests for the real MCTS Layer 1 (offline, deterministic via MockLLMClient)."""

import pytest

from apex.layer1_mcts import MCTSNode, MCTSSearch, SearchBudget
from apex.llm_client import MockLLMClient


def _responder(prompt: str) -> str:
    """Scripted 'model': expands into 3 candidates, scores everything 0.9."""
    if "refinements" in prompt:
        return "candidate one\ncandidate two\ncandidate three"
    # scoring prompt
    return "0.9"


def test_ucb1_prefers_unvisited():
    parent = MCTSNode("root")
    parent.visits = 1
    child = MCTSNode("c", parent=parent)
    assert child.ucb1() == float("inf")  # unvisited -> explore first


def test_backpropagate_updates_ancestors():
    root = MCTSNode("root")
    child = MCTSNode("c", parent=root)
    root.children.append(child)
    child.backpropagate(1.0)
    assert child.visits == 1 and child.value == 1.0
    assert root.visits == 1 and root.value == 1.0


@pytest.mark.asyncio
async def test_search_grows_tree_and_passes():
    client = MockLLMClient(responder=_responder)
    search = MCTSSearch(client, threshold=0.85, budget=SearchBudget(max_nodes=10, max_llm_calls=20))
    result = await search.search("Build a race-condition-free counter")
    assert result.nodes_created > 1          # tree actually grew
    assert result.llm_calls > 0              # real calls happened
    assert result.best_score >= 0.85         # high-scoring path found
    assert result.passed is True


@pytest.mark.asyncio
async def test_search_respects_node_budget():
    client = MockLLMClient(responder=_responder)
    budget = SearchBudget(max_nodes=5, max_llm_calls=100, wall_clock_seconds=10)
    search = MCTSSearch(client, budget=budget)
    result = await search.search("root design")
    assert result.nodes_created <= 5


@pytest.mark.asyncio
async def test_low_scores_fail_threshold():
    client = MockLLMClient(
        responder=lambda p: ("a\nb\nc" if "refinements" in p else "0.10")
    )
    search = MCTSSearch(client, threshold=0.85, budget=SearchBudget(max_nodes=8, max_llm_calls=16))
    result = await search.search("weak design")
    assert result.best_score < 0.85
    assert result.passed is False


@pytest.mark.asyncio
async def test_unparseable_score_is_penalized_not_trusted():
    # Model returns "inf" (the old bypass). Must be treated as 0.0, not a pass.
    client = MockLLMClient(
        responder=lambda p: ("a\nb\nc" if "refinements" in p else "inf")
    )
    search = MCTSSearch(client, threshold=0.85, budget=SearchBudget(max_nodes=6, max_llm_calls=12))
    result = await search.search("sneaky design")
    assert result.passed is False


@pytest.mark.asyncio
async def test_validation_guided_reselection_skips_rejected_best():
    # alpha scores highest but the external validator rejects it; the search
    # must fall back to the next validated candidate (beta).
    def responder(prompt: str) -> str:
        if "refinements" in prompt:
            return "alpha\nbeta\ngamma"
        if "alpha" in prompt:
            return "0.98"
        if "beta" in prompt:
            return "0.90"
        if "gamma" in prompt:
            return "0.85"
        return "0.50"

    client = MockLLMClient(responder=responder)
    # max_nodes=4 -> only the root expands, so the three candidates stay unique
    # (no duplicate-named grandchildren); iterations bounded by max_llm_calls.
    search = MCTSSearch(client, threshold=0.80,
                        budget=SearchBudget(max_nodes=4, max_llm_calls=30))

    def validator(design: str) -> bool:
        return design != "alpha"        # reject the top candidate

    result = await search.search("root", validator=validator)
    assert result.best_design == "beta"
    assert result.passed is True


@pytest.mark.asyncio
async def test_validation_guided_all_rejected_fails():
    def responder(prompt: str) -> str:
        return "alpha\nbeta\ngamma" if "refinements" in prompt else "0.95"

    search = MCTSSearch(MockLLMClient(responder=responder), threshold=0.80,
                        budget=SearchBudget(max_nodes=4, max_llm_calls=30))
    result = await search.search("root", validator=lambda d: False)
    assert result.passed is False


def test_apply_feedback_penalizes_branch():
    root = MCTSNode("root")
    child = MCTSNode("bad-spec", parent=root)
    root.children.append(child)
    child.visits, child.value = 2, 1.8   # looked good
    root.visits, root.value = 2, 1.8
    search = MCTSSearch(MockLLMClient())
    search.apply_feedback(child, penalty=1.0)
    assert child.value < 1.8             # Z3 rejection dragged it down
