from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

import pytest

import app.ai as ai_module
from app.ai import AIClient, AIScoringError, ScoringPayload
from app.models.scoring import ScoringDimensions, ScoringResult


VALID_RESULT: dict[str, Any] = {
    "score": 80,
    "detail": "材料完整，逻辑清晰。",
    "highlights": "目标明确。",
    "improvements": "补充量化指标。",
    "dimensions": {
        "completeness": 24,
        "logic": 24,
        "format": 16,
        "quality": 16,
    },
}


class StubCompletions:
    def __init__(self, responses: list[str | None]) -> None:
        self._responses = iter(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        content = next(self._responses)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


class StubOpenAIClient:
    def __init__(self, responses: list[str | None]) -> None:
        self.completions = StubCompletions(responses)
        self.chat = SimpleNamespace(completions=self.completions)


@pytest.fixture
def parser_client(config: Any) -> AIClient:
    return AIClient(config, client=StubOpenAIClient([]))  # type: ignore[arg-type]


def encode(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def test_scoring_models_accept_complete_consistent_result() -> None:
    result = ScoringResult.model_validate(VALID_RESULT)

    assert isinstance(result.dimensions, ScoringDimensions)
    assert result.dimensions.total == result.score == 80


@pytest.mark.parametrize(
    "wrapper",
    [
        lambda body: body,
        lambda body: f"```json\n{body}\n```",
        lambda body: f"```\n{body}\n```",
        lambda body: f"评分结果如下：\n{body}\n请查收。",
    ],
)
def test_parse_response_allows_only_documented_json_recovery(
    parser_client: AIClient,
    wrapper: Any,
) -> None:
    result = parser_client._parse_response(wrapper(encode(VALID_RESULT)))

    assert result == VALID_RESULT


@pytest.mark.parametrize(
    "invalid_payload",
    [
        [],
        {},
        {**VALID_RESULT, "score": "80"},
        {**VALID_RESULT, "detail": 123},
        {
            **VALID_RESULT,
            "dimensions": {**VALID_RESULT["dimensions"], "logic": "24"},
        },
        {**VALID_RESULT, "unexpected": "field"},
    ],
    ids=[
        "array",
        "empty-object",
        "score-string",
        "detail-number",
        "dimension-string",
        "extra-field",
    ],
)
def test_parse_response_rejects_wrong_shape_and_types(
    parser_client: AIClient,
    invalid_payload: Any,
) -> None:
    with pytest.raises(AIScoringError):
        parser_client._parse_response(encode(invalid_payload))


@pytest.mark.parametrize(
    "invalid_payload",
    [
        {**VALID_RESULT, "score": 101},
        {
            **VALID_RESULT,
            "dimensions": {**VALID_RESULT["dimensions"], "completeness": 31},
        },
        {
            **VALID_RESULT,
            "dimensions": {**VALID_RESULT["dimensions"], "quality": -1},
        },
        {**VALID_RESULT, "score": 79},
    ],
    ids=["score-range", "dimension-upper-range", "dimension-lower-range", "sum"],
)
def test_parse_response_rejects_ranges_and_inconsistent_total(
    parser_client: AIClient,
    invalid_payload: dict[str, Any],
) -> None:
    with pytest.raises(AIScoringError):
        parser_client._parse_response(encode(invalid_payload))


@pytest.mark.parametrize(
    ("field_name", "limit"),
    [("detail", 500), ("highlights", 150), ("improvements", 250)],
)
def test_parse_response_enforces_text_length_limits(
    parser_client: AIClient,
    field_name: str,
    limit: int,
) -> None:
    at_limit = copy.deepcopy(VALID_RESULT)
    at_limit[field_name] = "字" * limit
    assert parser_client._parse_response(encode(at_limit))[field_name] == "字" * limit

    too_long = copy.deepcopy(VALID_RESULT)
    too_long[field_name] = "字" * (limit + 1)
    with pytest.raises(AIScoringError):
        parser_client._parse_response(encode(too_long))


@pytest.mark.parametrize(
    "response",
    [
        "not json",
        "",
        "   ",
        '{"score": 80, "detail": "truncated"',
    ],
    ids=["plain-text", "empty", "whitespace", "truncated-object"],
)
def test_parse_response_rejects_non_json_without_field_salvage(
    parser_client: AIClient,
    response: str,
) -> None:
    with pytest.raises(AIScoringError):
        parser_client._parse_response(response)


@pytest.mark.asyncio
async def test_score_accepts_only_scoring_payload(config: Any) -> None:
    client = AIClient(config, client=StubOpenAIClient([]))  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        await client.score(  # type: ignore[call-arg]
            text_content="文本",
            doc_content="文档",
            attachment_content="附件",
        )


@pytest.mark.asyncio
async def test_one_openai_client_is_reused_for_text_and_file_scoring(
    config: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid_json = encode(VALID_RESULT)
    stub = StubOpenAIClient([valid_json, valid_json])
    constructor_calls: list[dict[str, Any]] = []

    def build_client(**kwargs: Any) -> StubOpenAIClient:
        constructor_calls.append(kwargs)
        return stub

    monkeypatch.setattr(ai_module, "AsyncOpenAI", build_client)
    client = AIClient(config)

    assert await client.score(ScoringPayload(text="纯文本")) == VALID_RESULT

    file_payload = ScoringPayload(
        text="原始描述",
        pdf_files=[(b"%PDF-test", "review.pdf")],
    )
    assert await client.score(file_payload) == VALID_RESULT

    assert len(constructor_calls) == 1
    assert len(stub.completions.calls) == 2
