from __future__ import annotations

import pytest

from app.config import Config


def test_valid_config_has_no_errors(config) -> None:
    assert config.validate() == []


def test_doubao_requires_api_key(config) -> None:
    config.ai_api_key = ""

    assert "AI_API_KEY 不能为空" in config.validate()


def test_unsupported_provider_is_rejected_at_startup(config) -> None:
    config.ai_provider = "openai"

    assert "AI_PROVIDER 不受支持: openai" in config.validate()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("webhook_verification_token", "", "WEBHOOK_VERIFICATION_TOKEN 不能为空"),
        ("webhook_encrypt_key", "", "WEBHOOK_ENCRYPT_KEY 不能为空"),
        ("score_threshold", 101, "SCORE_THRESHOLD 必须在 0 到 100 之间"),
        ("max_revision_rounds", 0, "MAX_REVISION_ROUNDS 必须大于 0"),
        ("ai_temperature", -0.1, "AI_TEMPERATURE 必须在 0 到 2 之间"),
        ("ai_score_max_tokens", 0, "AI_SCORE_MAX_TOKENS 必须在 1 到 128000 之间"),
        (
            "ai_transcribe_max_tokens",
            128_001,
            "AI_TRANSCRIBE_MAX_TOKENS 必须在 1 到 128000 之间",
        ),
        (
            "send_circuit_breaker_window_minutes",
            0,
            "SEND_CIRCUIT_BREAKER_WINDOW_MINUTES 必须大于 0",
        ),
        (
            "send_circuit_breaker_max_messages",
            0,
            "SEND_CIRCUIT_BREAKER_MAX_MESSAGES 必须大于 0",
        ),
        ("shutdown_timeout_seconds", 0, "SHUTDOWN_TIMEOUT_SECONDS 必须大于 0"),
        ("port", 70_000, "PORT 必须在 1 到 65535 之间"),
    ],
)
def test_numeric_and_webhook_boundaries_are_validated(
    config,
    field: str,
    value,
    message: str,
) -> None:
    setattr(config, field, value)

    assert message in config.validate()


def test_default_model_is_a_file_capable_doubao_model(monkeypatch) -> None:
    monkeypatch.delenv("AI_MODEL", raising=False)

    config = Config()

    assert config.ai_model.startswith("doubao-seed-2-0-")
