"""非评分后台任务（如 webhook 准入检查）的引用与关闭管理。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)


class BackgroundTaskSupervisor:
    def __init__(self) -> None:
        self._accepting = True
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def accepting(self) -> bool:
        return self._accepting

    @property
    def task_count(self) -> int:
        return len(self._tasks)

    def spawn(self, coroutine: Coroutine[Any, Any, Any], *, name: str) -> bool:
        if not self._accepting:
            coroutine.close()
            return False
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return True

    def _done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except BaseException:
            logger.exception("后台准入任务异常: task=%s", task.get_name())

    def stop_accepting(self) -> None:
        self._accepting = False

    async def drain(self, timeout_seconds: float) -> bool:
        self.stop_accepting()
        tasks = [task for task in tuple(self._tasks) if not task.done()]
        if not tasks:
            return True
        try:
            async with asyncio.timeout(timeout_seconds):
                await asyncio.gather(*tasks, return_exceptions=True)
            return True
        except TimeoutError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            return False
