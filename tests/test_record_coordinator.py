from __future__ import annotations

import asyncio

import pytest

from app.errors import FeishuNotFoundError
from app.record_coordinator import (
    RecordCoordinator,
    RequestStatus,
    derive_callback_id,
    extract_requirement_title,
    is_rpa_enabled,
)
from app.task_registry import RecordKey, TaskRegistry
from app.workflow_state import TriggerSource
from tests.fakes import FakeFeishuClient


@pytest.mark.asyncio
async def test_initial_event_only_accepts_pending_record(record_factory) -> None:
    pending_key = RecordKey("app", "table", "pending")
    failed_key = RecordKey("app", "table", "failed")
    pending_fields = record_factory(status="待评分")
    pending_fields["需求标题"] = [{"text": "支付审批需求\n第二期"}]
    feishu = FakeFeishuClient(
        {
            ("app", "table", "pending"): pending_fields,
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
    assert accepted.command
    assert accepted.command.requirement_title == "支付审批需求 第二期"
    assert ignored.status is RequestStatus.NOT_TRIGGERABLE
    release.set()
    await registry.drain(1)


def test_requirement_title_falls_back_to_record_id(record_factory) -> None:
    fields = record_factory(requirement_title=None)

    assert extract_requirement_title(fields, "record") == "record"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("是", True),
        (True, True),
        (["是"], True),
        ({"text": "是"}, True),
        ("否", False),
        (False, False),
        (None, False),
        ("", False),
    ],
)
def test_rpa_gate_accepts_only_explicit_yes(value, expected) -> None:
    assert is_rpa_enabled(value) is expected


@pytest.mark.asyncio
async def test_non_rpa_record_never_enters_scoring(record_factory) -> None:
    key = RecordKey("app", "table", "record")
    feishu = FakeFeishuClient(
        {("app", "table", "record"): record_factory(is_rpa="否")}
    )
    registry = TaskRegistry()

    async def runner(command):
        raise AssertionError("非 RPA 记录不可启动评分")

    coordinator = RecordCoordinator(feishu=feishu, registry=registry, runner=runner)

    result = await coordinator.request_score(
        key=key,
        source=TriggerSource.INITIAL_EVENT,
    )

    assert result.status is RequestStatus.NOT_RPA
    assert registry.active_count == 0


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


@pytest.mark.asyncio
async def test_missing_record_exception_maps_to_not_found() -> None:
    key = RecordKey("app", "table", "missing")
    feishu = FakeFeishuClient()
    feishu.outcomes.script(
        "get_record",
        FeishuNotFoundError(
            "记录不存在",
            operation="get_record",
            resource_id="missing",
        ),
    )
    registry = TaskRegistry()

    async def runner(command):
        raise AssertionError("不存在的记录不可入队")

    coordinator = RecordCoordinator(feishu=feishu, registry=registry, runner=runner)
    result = await coordinator.request_score(
        key=key,
        source=TriggerSource.INITIAL_EVENT,
    )

    assert result.status is RequestStatus.RECORD_NOT_FOUND
    assert registry.active_count == 0
