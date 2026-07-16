from __future__ import annotations

from typing import Any

import pytest

from app.config import Config, get_config
from app.field_mapping import (
    FIELD_ATTACHMENT,
    FIELD_DOC_CACHE,
    FIELD_DOC_LINK,
    FIELD_IS_RPA,
    FIELD_REQUIREMENT_TITLE,
    FIELD_REVISION_ROUNDS,
    FIELD_SCORE_STATUS,
    FIELD_SUBMITTER,
    FIELD_TEXT_CONTENT,
)
from tests.fakes import CallTrace, FakeClock


@pytest.fixture(autouse=True)
def clear_config_cache() -> None:
    get_config.cache_clear()
    yield
    get_config.cache_clear()


@pytest.fixture
def config() -> Config:
    return Config(
        feishu_app_id="cli_test",
        feishu_app_secret="secret_test",
        bitable_app_token="app_test",
        bitable_table_id="tbl_test",
        feishu_base_url="https://example.feishu.cn",
        webhook_verification_token="verify_test",
        webhook_encrypt_key="encrypt_test",
        ai_provider="doubao",
        ai_api_key="ai_test",
        ai_model="doubao-seed-2-0-pro-260215",
        score_threshold=70,
        max_revision_rounds=5,
        notification_group_chat_id="oc_admin",
        attachment_allowed_hosts=("example.test", "open.feishu.cn"),
    )


@pytest.fixture
def trace() -> CallTrace:
    return CallTrace()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(initial=1_700_000_000.0)


@pytest.fixture
def valid_score_result() -> dict[str, Any]:
    return {
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


@pytest.fixture
def attachment_factory():
    def factory(
        *,
        file_token: str = "file_1",
        name: str = "需求.pdf",
        mime_type: str = "application/pdf",
        url: str = "https://open.feishu.cn/file/file_1",
        size: int = 1024,
    ) -> dict[str, Any]:
        return {
            "file_token": file_token,
            "name": name,
            "mime_type": mime_type,
            "url": url,
            "size": size,
        }

    return factory


@pytest.fixture
def record_factory():
    def factory(
        *,
        status: str = "待评分",
        docs: Any = "",
        attachments: list[dict[str, Any]] | None = None,
        description: str = "原始需求描述",
        rounds: int = 0,
        cache: str = "",
        submitter: str = "ou_submitter",
        is_rpa: Any = "是",
        requirement_title: Any = "测试需求",
    ) -> dict[str, Any]:
        return {
            FIELD_IS_RPA: is_rpa,
            FIELD_REQUIREMENT_TITLE: requirement_title,
            FIELD_SCORE_STATUS: status,
            FIELD_DOC_LINK: docs,
            FIELD_ATTACHMENT: attachments or [],
            FIELD_TEXT_CONTENT: description,
            FIELD_REVISION_ROUNDS: rounds,
            FIELD_DOC_CACHE: cache,
            FIELD_SUBMITTER: [{"id": submitter, "name": "测试用户"}],
        }

    return factory
