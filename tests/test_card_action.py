from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.card_action import (
    ACTION_ADMIN_RETRY,
    ACTION_RESCORE,
    CardActionService,
    DecodedCardAction,
    build_score_action_value,
)
from app.record_coordinator import RequestStatus, ScoreRequestResult


@dataclass
class StubCoordinator:
    status: RequestStatus

    def __post_init__(self) -> None:
        self.calls = []

    async def request_score(self, **kwargs):
        self.calls.append(kwargs)
        return ScoreRequestResult(self.status)


def decoded(action: str = ACTION_RESCORE, *, chat_id: str = "") -> DecodedCardAction:
    return DecodedCardAction(
        actor_open_id="ou_user",
        open_message_id="om_1",
        open_chat_id=chat_id,
        action_value=build_score_action_value(
            action=action,
            app_token="app",
            table_id="table",
            record_id="record",
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (RequestStatus.ACCEPTED, "已提交重新评分"),
        (RequestStatus.DUPLICATE_CALLBACK, "已提交重新评分"),
        (RequestStatus.ALREADY_RUNNING, "正在评分"),
        (RequestStatus.NOT_TRIGGERABLE, "不可重新评分"),
        (RequestStatus.FORBIDDEN, "原提报人"),
    ],
)
async def test_user_action_feedback_distinguishes_outcome(status, expected) -> None:
    coordinator = StubCoordinator(status)
    service = CardActionService(coordinator=coordinator)

    response = await service.handle(decoded())

    assert expected in response.content


@pytest.mark.asyncio
async def test_unknown_action_is_rejected() -> None:
    coordinator = StubCoordinator(RequestStatus.ACCEPTED)
    service = CardActionService(coordinator=coordinator)
    action = DecodedCardAction(
        actor_open_id="ou_user",
        open_message_id="om_1",
        open_chat_id="",
        action_value={
            "action": "delete",
            "app_token": "app",
            "table_id": "table",
            "record_id": "record",
        },
    )

    response = await service.handle(action)

    assert response.status_code == 400
    assert not coordinator.calls


@pytest.mark.asyncio
async def test_admin_retry_requires_callback_from_admin_group() -> None:
    coordinator = StubCoordinator(RequestStatus.ACCEPTED)
    service = CardActionService(coordinator=coordinator, admin_chat_id="oc_admin")

    forbidden = await service.handle(decoded(ACTION_ADMIN_RETRY, chat_id="oc_other"))
    accepted = await service.handle(decoded(ACTION_ADMIN_RETRY, chat_id="oc_admin"))

    assert forbidden.status_code == 403
    assert "已提交管理员重试" in accepted.content
    assert len(coordinator.calls) == 1


def test_action_value_contains_only_action_and_record_locator() -> None:
    value = build_score_action_value(
        action=ACTION_RESCORE,
        app_token="app",
        table_id="table",
        record_id="record",
    )

    assert set(value) == {"action", "app_token", "table_id", "record_id"}
