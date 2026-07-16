from __future__ import annotations

import asyncio

import pytest

from app.field_mapping import FIELD_SCORE_STATUS
from app.record_coordinator import (
    RecordCoordinator,
    RequestStatus,
    derive_callback_id,
)
from app.task_registry import RecordKey, TaskRegistry
from app.workflow_state import TriggerSource
from tests.fakes import FakeFeishuClient


@pytest.mark.asyncio
async def test_initial_event_only_accepts_pending_record(record_factory) -> None:
    pending_key = RecordKey("app", "table", "pending")
    failed_key = RecordKey("app", "table", "failed")
    feishu = FakeFeishuClient(
        {
            ("app", "table", "pending"): record_factory(status="待评分"),
            ("app", "table", "failed"): record_factory(status="未通过"),
        }
    )
    registry = TaskRegistry()
    release = asyncio.Event()

    async def runner(command):
        await release.wait()

    coordinator = RecordCoordinator(feishu=feishu, registry=registry, runner=runner)

    accepted = await coordinator.request_score(
        key=pending_key, source=TriggerSource.INITIAL_EVENT
    )
    ignored = await coordinator.request_score(
        key=failed_key, source=TriggerSource.INITIAL_EVENT
    )

    assert accepted.status is RequestStatus.ACCEPTED
    assert ignored.status is RequestStatus.NOT_TRIGGERABLE
    release.set()
    await registry.drain(1)


@pytest.mark.asyncio
async def test_user_rescore_requires_submitter_and_failed_status(record_factory) -> None:
    key = RecordKey("app", "table", "record")
    feishu = FakeFeishuClient(
        {("app", "table", "record"): record_factory(status="未通过")}
    )
    registry = TaskRegistry()
    release = asyncio.Event()

    async def runner(command):
        await release.wait()

    coordinator = RecordCoordinator(feishu=feishu, registry=registry, runner=runner)

    forbidden = await coordinator.request_score(
        key=key,
        source=TriggerSource.USER_RESCORE,
        actor_open_id="ou_other",
        callback_id="click-other",
    )
    accepted = await coordinator.request_score(
        key=key,
        source=TriggerSource.USER_RESCORE,
        actor_open_id="ou_submitter",
        callback_id="click-owner",
    )
    duplicate_running = await coordinator.request_score(
        key=key,
        source=TriggerSource.USER_RESCORE,
        actor_open_id="ou_submitter",
        callback_id="click-second",
    )

    assert forbidden.status is RequestStatus.FORBIDDEN
    assert accepted.status is RequestStatus.ACCEPTED
    assert duplicate_running.status is RequestStatus.ALREADY_RUNNING
    release.set()
    await registry.drain(1)


@pytest.mark.asyncio
async def test_callback_id_is_idempotent(record_factory) -> None:
    key = RecordKey("app", "table", "record")
    feishu = FakeFeishuClient(
        {("app", "table", "record"): record_factory(status="未通过")}
    )
    registry = TaskRegistry()

    async def runner(command):
        return None

    coordinator = RecordCoordinator(feishu=feishu, registry=registry, runner=runner)
    callback_id = derive_callback_id(
        open_message_id="om_1",
        actor_open_id="ou_submitter",
        action_value={
            "action": "rescore",
            "app_token": "app",
            "table_id": "table",
            "record_id": "record",
        },
    )

    first = await coordinator.request_score(
        key=key,
        source=TriggerSource.USER_RESCORE,
        actor_open_id="ou_submitter",
        callback_id=callback_id,
    )
    second = await coordinator.request_score(
        key=key,
        source=TriggerSource.USER_RESCORE,
        actor_open_id="ou_submitter",
        callback_id=callback_id,
    )

    assert first.status is RequestStatus.ACCEPTED
    assert second.status is RequestStatus.DUPLICATE_CALLBACK
    await registry.drain(1)


@pytest.mark.asyncio
async def test_old_card_always_rechecks_current_record_status(record_factory) -> None:
    key = RecordKey("app", "table", "record")
    feishu = FakeFeishuClient(
        {("app", "table", "record"): record_factory(status="已通过")}
    )
    registry = TaskRegistry()

    async def runner(command):
        raise AssertionError("不可启动评分")

    coordinator = RecordCoordinator(feishu=feishu, registry=registry, runner=runner)
    result = await coordinator.request_score(
        key=key,
        source=TriggerSource.USER_RESCORE,
        actor_open_id="ou_submitter",
        callback_id="old-card",
    )

    assert result.status is RequestStatus.NOT_TRIGGERABLE
    assert registry.active_count == 0


@pytest.mark.asyncio
async def test_scoring_status_reports_already_running_without_new_task(record_factory) -> None:
    key = RecordKey("app", "table", "record")
    feishu = FakeFeishuClient(
        {("app", "table", "record"): record_factory(status="评分中")}
    )
    registry = TaskRegistry()

    async def runner(command):
        raise AssertionError("不可创建并行任务")

    coordinator = RecordCoordinator(feishu=feishu, registry=registry, runner=runner)
    result = await coordinator.request_score(
        key=key,
        source=TriggerSource.USER_RESCORE,
        actor_open_id="ou_submitter",
        callback_id="scoring-card",
    )

    assert result.status is RequestStatus.ALREADY_RUNNING
    assert registry.active_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["评分中", "未通过", "已通过", "已驳回", "评分异常"])
async def test_initial_event_ignores_every_non_pending_status(status, record_factory) -> None:
    key = RecordKey("app", "table", "record")
    feishu = FakeFeishuClient(
        {("app", "table", "record"): record_factory(status=status)}
    )
    registry = TaskRegistry()

    async def runner(command):
        raise AssertionError("服务写回事件不得启动评分")

    coordinator = RecordCoordinator(feishu=feishu, registry=registry, runner=runner)
    result = await coordinator.request_score(
        key=key,
        source=TriggerSource.INITIAL_EVENT,
    )

    assert result.status is RequestStatus.NOT_TRIGGERABLE
    assert registry.active_count == 0
