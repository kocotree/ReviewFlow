from __future__ import annotations

import asyncio

import pytest

from app.background_tasks import BackgroundTaskSupervisor


@pytest.mark.asyncio
async def test_supervisor_keeps_reference_and_drains() -> None:
    supervisor = BackgroundTaskSupervisor()
    started = asyncio.Event()
    release = asyncio.Event()

    async def work() -> None:
        started.set()
        await release.wait()

    assert supervisor.spawn(work(), name="admission")
    await started.wait()
    assert supervisor.task_count == 1

    drain = asyncio.create_task(supervisor.drain(1))
    await asyncio.sleep(0)
    assert not drain.done()

    release.set()
    assert await drain
    assert supervisor.task_count == 0


@pytest.mark.asyncio
async def test_supervisor_refuses_new_coroutine_after_shutdown() -> None:
    supervisor = BackgroundTaskSupervisor()
    supervisor.stop_accepting()

    async def work() -> None:
        raise AssertionError("不应运行")

    assert not supervisor.spawn(work(), name="late")
