from __future__ import annotations

import pytest

from app.notification import NotificationManager
from app.send_circuit_breaker import SendCircuitBreaker
from app.workflow_errors import MaterialProblem
from tests.fakes import FakeClock, FakeFeishuClient


def buttons(card):
    return [
        button
        for element in card["elements"]
        if element.get("tag") == "action"
        for button in element.get("actions", [])
    ]


@pytest.mark.asyncio
async def test_failed_card_has_view_and_rescore_buttons(config) -> None:
    feishu = FakeFeishuClient()
    manager = NotificationManager(config, feishu)

    assert await manager.notify_score_failed(
        open_id="ou_user",
        record_id="record",
        score=60,
        detail="补充验收标准",
        threshold=70,
        base_url="https://example.feishu.cn",
        app_token="app",
        table_id="table",
        rounds=1,
        max_rounds=5,
    )

    card = feishu.cards[0]["card"]
    card_buttons = buttons(card)
    assert [button["text"]["content"] for button in card_buttons] == [
        "查看并修改",
        "重新评分",
    ]
    assert card_buttons[1]["value"] == {
        "action": "rescore",
        "app_token": "app",
        "table_id": "table",
        "record_id": "record",
    }


@pytest.mark.asyncio
async def test_failed_card_hides_rescore_when_round_limit_reached(config) -> None:
    feishu = FakeFeishuClient()
    manager = NotificationManager(config, feishu)

    await manager.notify_score_failed(
        open_id="ou_user",
        record_id="record",
        score=60,
        detail="detail",
        threshold=70,
        app_token="app",
        table_id="table",
        rounds=5,
        max_rounds=5,
    )

    assert buttons(feishu.cards[0]["card"]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["no_file", "unsupported", "damaged", "limit"])
async def test_all_fixable_material_cards_include_rescore(kind, config) -> None:
    feishu = FakeFeishuClient()
    manager = NotificationManager(config, feishu)

    await manager.notify_material_error(
        open_id="ou_user",
        record_id="record",
        kind=kind,
        reason="请修复材料",
        problems=(
            MaterialProblem("问题一.pdf", "损坏"),
            MaterialProblem("问题二.zip", "格式不支持"),
        ),
        app_token="app",
        table_id="table",
        base_url="https://example.feishu.cn",
    )

    card = feishu.cards[0]["card"]
    rendered = str(card)
    assert "问题一.pdf" in rendered
    assert "问题二.zip" in rendered
    assert any(button["text"]["content"] == "重新评分" for button in buttons(card))


@pytest.mark.asyncio
async def test_admin_error_card_has_retry_action(config) -> None:
    feishu = FakeFeishuClient()
    manager = NotificationManager(config, feishu)

    await manager.notify_error_to_group(
        record_id="record",
        error="AI schema invalid",
        record_url="https://example.feishu.cn/record",
        app_token="app",
        table_id="table",
    )

    card_buttons = buttons(feishu.cards[0]["card"])
    retry = next(button for button in card_buttons if button["text"]["content"] == "重试评分")
    assert retry["value"]["action"] == "admin_retry"
    assert feishu.cards[0]["receive_id_type"] == "chat_id"


@pytest.mark.asyncio
async def test_rejected_card_never_contains_rescore_button(config) -> None:
    feishu = FakeFeishuClient()
    manager = NotificationManager(config, feishu)

    await manager.notify_rejected(
        open_id="ou_user",
        record_id="record",
        score=60,
        detail="detail",
        rounds=5,
        app_token="app",
        table_id="table",
    )

    assert buttons(feishu.cards[0]["card"]) == []


@pytest.mark.asyncio
async def test_old_business_cooldown_and_daily_cap_are_not_applied(config) -> None:
    config.notification_cooldown_minutes = 10_000
    config.max_daily_notifications_per_user = 0
    feishu = FakeFeishuClient()
    manager = NotificationManager(config, feishu)

    for score in (80, 81):
        assert await manager.notify_score_passed(
            open_id="ou_user",
            record_id="record",
            score=score,
            threshold=70,
        )

    assert len(feishu.cards) == 2


@pytest.mark.asyncio
async def test_send_circuit_breaker_blocks_and_alerts_group_once(config) -> None:
    clock = FakeClock()
    breaker = SendCircuitBreaker(
        window_seconds=300,
        max_messages=1,
        clock=clock.time,
    )
    feishu = FakeFeishuClient()
    manager = NotificationManager(config, feishu, send_circuit_breaker=breaker)

    assert await manager.notify_score_passed(
        open_id="ou_user",
        record_id="record",
        score=80,
        threshold=70,
    )
    assert not await manager.notify_score_passed(
        open_id="ou_user",
        record_id="record",
        score=81,
        threshold=70,
    )
    assert not await manager.notify_score_passed(
        open_id="ou_user",
        record_id="record",
        score=82,
        threshold=70,
    )

    assert len(feishu.cards) == 2
    assert feishu.cards[1]["receive_id_type"] == "chat_id"
