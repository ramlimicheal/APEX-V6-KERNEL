"""Shared LLM client for all APEX layers.

Design goals (the opposite of the original mocks):
- One place to configure the model, retries, timeouts, and rate limiting.
- STRICT parsing of model output. The original code did `float(text)` which
  happily accepts "inf" / "nan" and let a bogus score pass Layer 1. We reject
  anything that is not a finite float in [0, 1], and validate JSON via Pydantic.
- An offline `MockLLMClient` with the SAME interface so the entire test suite
  runs deterministically with zero API calls / zero cost.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from typing import Callable, Dict, List, Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

logger = logging.getLogger("apex.llm")

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """Raised when the LLM call fails or returns unusable output."""


class LLMOutputError(LLMError):
    """Raised specifically when model output cannot be parsed/validated."""


# ---------------------------------------------------------------------------
# Strict parsers - shared by every layer so validation is consistent.
# ---------------------------------------------------------------------------

def parse_float_score(text: str) -> float:
    """Parse a confidence score, rejecting anything not finite in [0, 1].

    This closes the `float("inf") < 0.95 == False` bypass in the original
    Layer 1 code (layer1_mcts_alpha_routing.py:32).
    """
    if text is None:
        raise LLMOutputError("score text was None")
    cleaned = text.strip().strip("`").strip()
    # Grab the first token that looks like a number if the model added prose.
    for token in cleaned.replace(",", " ").split():
        try:
            value = float(token)
        except ValueError:
            continue
        if not math.isfinite(value):
            raise LLMOutputError(f"score not finite: {token!r}")
        if not (0.0 <= value <= 1.0):
            raise LLMOutputError(f"score out of range [0,1]: {value}")
        return value
    raise LLMOutputError(f"no numeric score found in {text!r}")


def _strip_code_fences(text: str) -> str:
    """Remove ```json ... ``` fences the model may wrap JSON in."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        # drop first fence line and any trailing fence
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def parse_json_schema(text: str, schema: Type[T]) -> T:
    """Parse + validate model output against a Pydantic schema."""
    if text is None:
        raise LLMOutputError("json text was None")
    candidate = _strip_code_fences(text)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise LLMOutputError(f"invalid JSON: {exc}") from exc
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        raise LLMOutputError(f"schema validation failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Client interface
# ---------------------------------------------------------------------------

class BaseLLMClient:
    """Common interface. Both the real and mock clients implement `generate`."""

    async def generate(self, prompt: str, *, temperature: float = 0.0) -> str:
        raise NotImplementedError


class GeminiClient(BaseLLMClient):
    """Real client wrapping google-generativeai with retry + rate limiting."""

    def __init__(
        self,
        model: str = "gemini-1.5-flash",
        *,
        api_key: Optional[str] = None,
        max_retries: int = 3,
        timeout_seconds: float = 30.0,
        concurrency: int = 8,
    ) -> None:
        self.model_name = model
        self.max_retries = max_retries
        self.timeout_seconds = timeout_seconds
        self._semaphore = asyncio.Semaphore(concurrency)

        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise LLMError(
                "GEMINI_API_KEY not set. Use --offline / MockLLMClient for tests, "
                "or copy .env.example to .env and add your key."
            )
        # Imported lazily so offline usage never requires the SDK.
        import google.generativeai as genai

        genai.configure(api_key=key)
        self._model = genai.GenerativeModel(model)

    async def generate(self, prompt: str, *, temperature: float = 0.0) -> str:
        async with self._semaphore:
            last_exc: Optional[Exception] = None
            for attempt in range(1, self.max_retries + 1):
                try:
                    return await asyncio.wait_for(
                        self._call(prompt, temperature), timeout=self.timeout_seconds
                    )
                except asyncio.TimeoutError as exc:
                    last_exc = exc
                    logger.warning("LLM timeout (attempt %d/%d)", attempt, self.max_retries)
                except Exception as exc:  # noqa: BLE001 - SDK raises many types
                    last_exc = exc
                    logger.warning("LLM error (attempt %d/%d): %s", attempt, self.max_retries, exc)
                # exponential backoff
                await asyncio.sleep(min(2 ** attempt * 0.5, 8.0))
            raise LLMError(f"LLM failed after {self.max_retries} attempts: {last_exc}")

    async def _call(self, prompt: str, temperature: float) -> str:
        import google.generativeai as genai  # noqa: F401 (config already done)

        def _sync() -> str:
            resp = self._model.generate_content(
                prompt,
                generation_config={"temperature": temperature},
            )
            return resp.text

        return await asyncio.to_thread(_sync)


class MockLLMClient(BaseLLMClient):
    """Deterministic offline client for tests and `--offline` runs.

    You can drive it with either a fixed response, a queue of responses, or a
    callable that maps the prompt to a response. This lets tests script exactly
    what "the model" says without any network access.
    """

    def __init__(
        self,
        *,
        default: str = "0.99",
        responder: Optional[Callable[[str], str]] = None,
        queue: Optional[List[str]] = None,
    ) -> None:
        self.default = default
        self.responder = responder
        self.queue = list(queue) if queue else None
        self.calls: List[str] = []

    async def generate(self, prompt: str, *, temperature: float = 0.0) -> str:
        self.calls.append(prompt)
        await asyncio.sleep(0)  # yield control, mimic awaitable
        if self.responder is not None:
            return self.responder(prompt)
        if self.queue:
            return self.queue.pop(0)
        return self.default


def build_client(offline: bool, **kwargs) -> BaseLLMClient:
    """Factory: return a mock client when offline, otherwise the real one."""
    if offline:
        return MockLLMClient(**kwargs)
    return GeminiClient(**kwargs)
