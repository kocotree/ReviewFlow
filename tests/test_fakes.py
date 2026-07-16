from __future__ import annotations

import pytest

from tests.fakes import (
    CallTrace,
    FakeAIClient,
    FakeClock,
    FakeFeishuClient,
    ScriptedOutcomes,
)


def test_scripted_outcomes_return_and_raise_in_order() -> None:
    outcomes = ScriptedOutcomes()
    outcomes.script("op", 1, RuntimeError("boom"), 3)

    assert outcomes.next("op") == 1
    with pytest.raises(RuntimeError, match="boom"):
        outcomes.next("op")
    assert outcomes.next("op") == 3


@pytest.mark.asyncio
async def test_fake_feishu_only_merges_successful_updates() -> None:
    key = ("app", "table", "record")
    fake = FakeFeishuClient({key: {"评分状态": "待评分"}})
    fake.outcomes.script("update_record", False, True)

    assert not await fake.update_record(
        "record", {"评分状态": "评分中"}, app_token="app", table_id="table"
    )
    assert fake.records[key]["评分状态"] == "待评分"
    assert await fake.update_record(
        "record", {"评分状态": "评分中"}, app_token="app", table_id="table"
    )
    assert fake.records[key]["评分状态"] == "评分中"


@pytest.mark.asyncio
async def test_fake_ai_gate_controls_running_call() -> None:
    fake = FakeAIClient()
    started, release = fake.gate()
    task = __import__("asyncio").create_task(fake.score({"bundle": b"pdf"}))

    await started.wait()
    assert not task.done()
    release.set()
    result = await task

    assert result["score"] == 80


@pytest.mark.asyncio
async def test_fake_clock_advances_without_real_wait() -> None:
    clock = FakeClock(initial=10)
    await clock.sleep(2.5)

    assert clock.time() == 12.5
    assert clock.monotonic() == 12.5
    assert clock.sleeps == [2.5]


def test_call_trace_preserves_cross_client_order() -> None:
    trace = CallTrace()
    trace.add("collect")
    trace.add("score")

    assert trace.names == ["collect", "score"]
