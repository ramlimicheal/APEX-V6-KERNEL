"""Layer 1 - Exogenous Tree Search, implemented for real.

The original `layer1_mcts_alpha_routing.py` defined `compute_ucb1` but never
called it, and "search" was a single hardcoded 0.98. This module runs a real
MCTS loop over concrete design candidates:

    selection  -> descend via UCB1 to a leaf
    expansion  -> ask the LLM to propose k candidate refinements (children)
    evaluation -> ask the LLM to score a candidate's viability in [0, 1]
                  (parsed strictly; inf/nan/out-of-range are rejected)
    backprop   -> propagate the reward up to the root

Hard budgets (max nodes, max LLM calls, wall-clock) bound the search so it can
never run away. A Z3-rejection feedback hook lets Layer 2 push a negative
reward back onto the branch that produced a bad spec - the feedback loop the
original README described but never implemented.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional, Union

from apex.llm_client import BaseLLMClient, LLMOutputError, parse_float_score

logger = logging.getLogger("apex.layer1")


class MCTSNode:
    """A node in the search tree. Each node represents a design candidate."""

    def __init__(self, design: str, parent: Optional["MCTSNode"] = None) -> None:
        self.design = design
        self.parent = parent
        self.children: List["MCTSNode"] = []
        self.visits = 0
        self.value = 0.0            # cumulative reward
        self.expanded = False

    @property
    def mean_value(self) -> float:
        return self.value / self.visits if self.visits else 0.0

    def ucb1(self, exploration: float = 1.41) -> float:
        """Upper Confidence Bound. Unvisited nodes get priority (inf)."""
        if self.visits == 0:
            return float("inf")
        exploit = self.value / self.visits
        assert self.parent is not None
        explore = exploration * math.sqrt(math.log(self.parent.visits) / self.visits)
        return exploit + explore

    def best_child(self, exploration: float = 1.41) -> "MCTSNode":
        return max(self.children, key=lambda c: c.ucb1(exploration))

    def backpropagate(self, reward: float) -> None:
        node: Optional[MCTSNode] = self
        while node is not None:
            node.visits += 1
            node.value += reward
            node = node.parent


@dataclass
class SearchBudget:
    max_nodes: int = 25
    max_llm_calls: int = 40
    wall_clock_seconds: float = 30.0


@dataclass
class SearchResult:
    best_design: str
    best_score: float
    nodes_created: int
    llm_calls: int
    elapsed: float
    passed: bool
    root: MCTSNode = field(repr=False, default=None)  # type: ignore[assignment]


_EXPAND_PROMPT = """You are refining a software design via tree search.
Given the current design candidate, propose {k} distinct, more concrete
refinements. Output each refinement on its own line, no numbering, no prose."""

_SCORE_PROMPT = """Score the viability of this design candidate as a single
float between 0.0 and 1.0 (1.0 = clearly correct and robust). Output ONLY the
number."""


class MCTSSearch:
    """Runs the real MCTS loop using an LLM for expansion + evaluation."""

    def __init__(
        self,
        client: BaseLLMClient,
        *,
        threshold: float = 0.85,
        children_per_expansion: int = 3,
        exploration: float = 1.41,
        budget: Optional[SearchBudget] = None,
    ) -> None:
        self.client = client
        self.threshold = threshold
        self.k = children_per_expansion
        self.exploration = exploration
        self.budget = budget or SearchBudget()
        self._llm_calls = 0
        self._nodes = 0

    async def _expand(self, node: MCTSNode) -> None:
        prompt = f"{_EXPAND_PROMPT.format(k=self.k)}\n\nCURRENT DESIGN:\n{node.design}"
        text = await self.client.generate(prompt, temperature=0.3)
        self._llm_calls += 1
        lines = [ln.strip("-* \t") for ln in text.splitlines() if ln.strip()]
        # Never create more children than the remaining node budget allows.
        remaining = max(0, self.budget.max_nodes - self._nodes)
        for line in lines[: min(self.k, remaining)]:
            node.children.append(MCTSNode(line, parent=node))
            self._nodes += 1
        node.expanded = True

    async def _evaluate(self, node: MCTSNode) -> float:
        prompt = f"{_SCORE_PROMPT}\n\nDESIGN:\n{node.design}"
        text = await self.client.generate(prompt, temperature=0.0)
        self._llm_calls += 1
        try:
            return parse_float_score(text)
        except LLMOutputError:
            # A model that won't produce a valid score is penalized, not trusted.
            logger.warning("[Layer1] unparseable score -> reward 0.0")
            return 0.0

    def _budget_exhausted(self, start: float) -> bool:
        # NOTE: max_nodes bounds EXPANSION (see _expand), not iteration count.
        # Iterations are bounded by LLM calls / wall-clock so that already-
        # created leaves still get visited and evaluated once the tree is full.
        return (
            self._llm_calls >= self.budget.max_llm_calls
            or (time.monotonic() - start) >= self.budget.wall_clock_seconds
        )

    def apply_feedback(self, node: MCTSNode, penalty: float = 1.0) -> None:
        """Push a negative reward onto a branch (e.g. after a Z3 rejection)."""
        node.backpropagate(-abs(penalty))

    async def search(
        self,
        root_design: str,
        *,
        validator: Optional[Callable[[str], Union[bool, Awaitable[bool]]]] = None,
        max_validation_attempts: int = 3,
    ) -> SearchResult:
        """Run MCTS. If `validator` is given, the winning candidate must pass an
        external check (e.g. Z3 / code verification). A rejected candidate gets
        negative feedback and the next-best candidate is tried, up to
        `max_validation_attempts` - so the search is guided by real verification,
        not just the LLM's own scores.
        """
        start = time.monotonic()
        root = MCTSNode(root_design)
        self._nodes = 1
        self._llm_calls = 0

        while not self._budget_exhausted(start):
            # 1. SELECTION - descend via UCB1 to a leaf.
            node = root
            while node.children:
                node = node.best_child(self.exploration)

            # 2. EXPANSION - grow the frontier (respecting the node budget).
            if not node.expanded and self._nodes < self.budget.max_nodes:
                await self._expand(node)
                if node.children:
                    node = node.children[0]

            # 3. EVALUATION.
            reward = await self._evaluate(node)

            # 4. BACKPROPAGATION.
            node.backpropagate(reward)

        elapsed = time.monotonic() - start

        if validator is None:
            best = self._best_node(root)
            return SearchResult(
                best.design, best.mean_value, self._nodes, self._llm_calls,
                elapsed, best.mean_value >= self.threshold, root,
            )

        # Validation-guided re-selection.
        tried: set = set()
        for _ in range(max_validation_attempts):
            best = self._best_node(root, exclude=tried)
            if best is root or best.mean_value <= 0:
                break
            tried.add(id(best))
            ok = validator(best.design)
            if inspect.isawaitable(ok):
                ok = await ok
            if ok:
                return SearchResult(
                    best.design, best.mean_value, self._nodes, self._llm_calls,
                    time.monotonic() - start, best.mean_value >= self.threshold, root,
                )
            # Rejected by real verification -> negative feedback, try next.
            self.apply_feedback(best, penalty=1.0)

        # Nothing validated.
        best = self._best_node(root)
        return SearchResult(
            best.design, best.mean_value, self._nodes, self._llm_calls,
            time.monotonic() - start, False, root,
        )

    @staticmethod
    def _best_node(root: MCTSNode, exclude: Optional[set] = None) -> MCTSNode:
        exclude = exclude or set()
        best = root
        stack = [root]
        while stack:
            n = stack.pop()
            if id(n) not in exclude and n.visits and n.mean_value >= best.mean_value:
                best = n
            stack.extend(n.children)
        return best
