from __future__ import annotations

import asyncio
import logging

import pytest

from app.task_registry import RecordKey, SubmitStatus, TaskRegistry, TaskState
from app.workflow_state import TriggerSource
from tests.fakes import FakeClock


@pytest.mark.asyncio
async def test_registry_tracks_state_fence_and_removes_completed_task(caplog) -> None:
    caplog.set_level(logging.INFO, logger="app.task_registry")
    clock = FakeClock()
    registry = TaskRegistry(clock=clock.monotonic)
    key = RecordKey("app", "table", "record")
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(command):
        started.set()
        await release.wait()
        assert registry.is_current(key, command.fence)

    result = registry.submit(
        key=key,
        source=TriggerSource.INITIAL_EVENT,
        runner=runner,
        requirement_title="支付审批需求",
    )
    assert result.status is SubmitStatus.ACCEPTED
    assert result.command and result.command.fence == 1
    assert registry.active_entry(key).state is TaskState.RECEIVED

    await started.wait()
    assert registry.active_entry(key).state is TaskState.STARTED
    assert registry.has_active(key)

    duplicate = registry.submit(
        key=key,
        source=TriggerSource.USER_RESCORE,
        runner=runner,
    )
    assert duplicate.status is SubmitStatus.ALREADY_RUNNING

    running_task = registry.active_entry(key).task
    release.set()
    assert running_task is not None
    await running_task
    await asyncio.sleep(0)

    assert not registry.has_active(key)
    assert registry.history[0].state is TaskState.COMPLETED
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        '评分任务已接收: 需求标题="支付审批需求" record_id=record'
        in message
        for message in messages
    )
    assert any(
        '评分任务已开始: 需求标题="支付审批需求" record_id=record'
        in message
        for message in messages
    )
    assert any(
        '评分任务已完成: 需求标题="支付审批需求" record_id=record'
        in message
        and "duration=" in message
        for message in messages
    )


@pytest.mark.asyncio
async def test_drain_stops_accepting_and_waits_for_running_task() -> None:
    registry = TaskRegistry()
    key = RecordKey("app", "table", "record")
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(command):
        started.set()
        await release.wait()

    accepted = registry.submit(
        key=key,
        source=TriggerSource.INITIAL_EVENT,
        runner=runner,
    )
    await started.wait()
    drain_task = asyncio.create_task(registry.drain(timeout_seconds=1))
    await asyncio.sleep(0)

    refused = registry.submit(
        key=RecordKey("app", "table", "other"),
        source=TriggerSource.INITIAL_EVENT,
        runner=runner,
    )
    assert refused.status is SubmitStatus.SHUTTING_DOWN
    assert not drain_task.done()

    release.set()
    assert await drain_task
    assert accepted.command is not None


def test_complete_record_key_prevents_cross_table_collision() -> None:
    first = RecordKey("app-a", "table", "record")
    second = RecordKey("app-b", "table", "record")

    assert first != second
    assert str(first) == "app-a:table:record"
