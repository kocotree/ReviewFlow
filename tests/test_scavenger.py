from __future__ import annotations

import asyncio

import pytest

from app.record_coordinator import RecordCoordinator
from app.scavenger import ScoringScavenger
from app.task_registry import RecordKey, TaskRegistry
from app.workflow_state import TriggerSource
from tests.fakes import FakeClock, FakeFeishuClient


def build_scavenger(feishu, coordinator, registry, clock):
    return ScoringScavenger(
        feishu=feishu,
        coordinator=coordinator,
        registry=registry,
        app_token="app",
        table_id="table",
        orphan_timeout_seconds=300,
        interval_seconds=60,
        clock=clock.time,
    )


@pytest.mark.asyncio
async def test_only_timed_out_record_without_live_task_is_reset_and_requeued(
    record_factory,
) -> None:
    clock = FakeClock(initial=1_000)
    key = ("app", "table", "orphan")
    feishu = FakeFeishuClient({key: record_factory(status="评分中")})
    feishu.outcomes.script(
        "list_scoring_records",
        [{"record_id": "orphan", "last_modified_time": 600_000}],
    )
    registry = TaskRegistry(clock=clock.monotonic)
    release = asyncio.Event()
    commands = []

    async def runner(command):
        commands.append(command)
        await release.wait()

    coordinator = RecordCoordinator(feishu=feishu, registry=registry, runner=runner)
    scavenger = build_scavenger(feishu, coordinator, registry, clock)

    assert await scavenger.run_once() == 1
    await asyncio.sleep(0)
    assert feishu.records[key]["评分状态"] == "待评分"
    assert commands[0].source is TriggerSource.SCAVENGER
    assert commands[0].fence == 1

    release.set()
    assert await registry.drain(1)


@pytest.mark.asyncio
async def test_slow_live_task_is_never_reset_even_when_timestamp_is_old(
    record_factory,
) -> None:
    clock = FakeClock(initial=1_000)
    key = RecordKey("app", "table", "slow")
    feishu = FakeFeishuClient(
        {("app", "table", "slow"): record_factory(status="评分中")}
    )
    feishu.outcomes.script(
        "list_scoring_records",
        [{"record_id": "slow", "last_modified_time": 100_000}],
    )
    registry = TaskRegistry(clock=clock.monotonic)
    release = asyncio.Event()

    async def runner(command):
        await release.wait()

    registry.submit(key=key, source=TriggerSource.INITIAL_EVENT, runner=runner)
    await asyncio.sleep(0)
    coordinator = RecordCoordinator(feishu=feishu, registry=registry, runner=runner)
    scavenger = build_scavenger(feishu, coordinator, registry, clock)

    assert await scavenger.run_once() == 0
    assert feishu.trace.names.count("feishu.update_record") == 0

    release.set()
    assert await registry.drain(1)


@pytest.mark.asyncio
async def test_fresh_scoring_record_is_not_reset(record_factory) -> None:
    clock = FakeClock(initial=1_000)
    feishu = FakeFeishuClient(
        {("app", "table", "fresh"): record_factory(status="评分中")}
    )
    feishu.outcomes.script(
        "list_scoring_records",
        [{"record_id": "fresh", "last_modified_time": 900_000}],
    )
    registry = TaskRegistry(clock=clock.monotonic)

    async def runner(command):
        raise AssertionError("新鲜记录不可重排")

    coordinator = RecordCoordinator(feishu=feishu, registry=registry, runner=runner)
    scavenger = build_scavenger(feishu, coordinator, registry, clock)

    assert await scavenger.run_once() == 0
    assert registry.active_count == 0
