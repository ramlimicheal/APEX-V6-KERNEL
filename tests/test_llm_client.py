"""Tests for the shared LLM client: strict parsing + offline mock."""

import math

import pytest

from apex.llm_client import (
    LLMOutputError,
    MockLLMClient,
    parse_float_score,
    parse_json_schema,
)
from pydantic import BaseModel


class _Score(BaseModel):
    value: float


def test_parse_float_score_valid():
    assert parse_float_score("0.87") == 0.87
    assert parse_float_score("  0.5  ") == 0.5
    assert parse_float_score("`0.99`") == 0.99
    # prose around the number is tolerated
    assert parse_float_score("Confidence: 0.42 out of 1.0") == 0.42


def test_parse_float_score_rejects_infinity():
    # This is the exact bypass the original Layer 1 had.
    with pytest.raises(LLMOutputError):
        parse_float_score("inf")
    with pytest.raises(LLMOutputError):
        parse_float_score("nan")


def test_parse_float_score_rejects_out_of_range():
    with pytest.raises(LLMOutputError):
        parse_float_score("1.5")
    with pytest.raises(LLMOutputError):
        parse_float_score("-0.2")


def test_parse_float_score_rejects_garbage():
    with pytest.raises(LLMOutputError):
        parse_float_score("totally not a number")


def test_parse_json_schema_valid():
    result = parse_json_schema('{"value": 0.5}', _Score)
    assert result.value == 0.5


def test_parse_json_schema_strips_code_fences():
    fenced = "```json\n{\"value\": 0.75}\n```"
    assert parse_json_schema(fenced, _Score).value == 0.75


def test_parse_json_schema_rejects_bad_json():
    with pytest.raises(LLMOutputError):
        parse_json_schema("{not json}", _Score)


def test_parse_json_schema_rejects_schema_violation():
    with pytest.raises(LLMOutputError):
        parse_json_schema('{"wrong_field": 1}', _Score)


@pytest.mark.asyncio
async def test_mock_client_queue_and_default():
    client = MockLLMClient(queue=["a", "b"], default="z")
    assert await client.generate("p1") == "a"
    assert await client.generate("p2") == "b"
    assert await client.generate("p3") == "z"  # queue empty -> default
    assert client.calls == ["p1", "p2", "p3"]


@pytest.mark.asyncio
async def test_mock_client_responder():
    client = MockLLMClient(responder=lambda prompt: prompt.upper())
    assert await client.generate("hi") == "HI"
