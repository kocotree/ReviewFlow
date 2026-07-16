"""进程内评分任务注册表、每记录串行与 fencing。"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Awaitable, Callable

from app.workflow_state import TriggerSource

logger = logging.getLogger(__name__)


@dataclass(frozen=True, order=True)
class RecordKey:
    app_token: str
    table_id: str
    record_id: str

    def __str__(self) -> str:
        return f"{self.app_token}:{self.table_id}:{self.record_id}"


class TaskState(StrEnum):
    RECEIVED = "received"
    STARTED = "started"
    COMPLETED = "completed"


@dataclass(frozen=True)
class ScoreCommand:
    key: RecordKey
    source: TriggerSource
    fence: int
    requested_at: float
    actor_open_id: str = ""
    callback_id: str = ""


@dataclass
class TaskEntry:
    command: ScoreCommand
    state: TaskState = TaskState.RECEIVED
    started_at: float | None = None
    completed_at: float | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    error: BaseException | None = field(default=None, repr=False)


class SubmitStatus(StrEnum):
    ACCEPTED = "accepted"
    ALREADY_RUNNING = "already_running"
    SHUTTING_DOWN = "shutting_down"


@dataclass(frozen=True)
class SubmitResult:
    status: SubmitStatus
    command: ScoreCommand | None = None

    @property
    def accepted(self) -> bool:
        return self.status is SubmitStatus.ACCEPTED


TaskRunner = Callable[[ScoreCommand], Awaitable[None]]


class TaskRegistry:
    """保存所有后台评分任务，并为每次入队分配递增 fencing。"""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._accepting = True
        self._active: dict[RecordKey, TaskEntry] = {}
        self._history: list[TaskEntry] = []
        self._fences: dict[RecordKey, int] = {}

    @property
    def accepting(self) -> bool:
        return self._accepting

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def history(self) -> tuple[TaskEntry, ...]:
        return tuple(self._history)

    def active_entry(self, key: RecordKey) -> TaskEntry | None:
        return self._active.get(key)

    def has_active(self, key: RecordKey) -> bool:
        entry = self._active.get(key)
        return bool(entry and entry.task and not entry.task.done())

    def is_current(self, key: RecordKey, fence: int) -> bool:
        entry = self._active.get(key)
        return bool(
            entry
            and entry.command.fence == fence
            and entry.task
            and not entry.task.done()
        )

    def submit(
        self,
        *,
        key: RecordKey,
        source: TriggerSource,
        runner: TaskRunner,
        actor_open_id: str = "",
        callback_id: str = "",
    ) -> SubmitResult:
        """注册并启动任务；同一完整记录键只允许一个在岗任务。"""
        if not self._accepting:
            return SubmitResult(SubmitStatus.SHUTTING_DOWN)
        if self.has_active(key):
            return SubmitResult(SubmitStatus.ALREADY_RUNNING)

        fence = self._fences.get(key, 0) + 1
        self._fences[key] = fence
        command = ScoreCommand(
            key=key,
            source=source,
            fence=fence,
            requested_at=self._clock(),
            actor_open_id=actor_open_id,
            callback_id=callback_id,
        )
        entry = TaskEntry(command=command)
        self._active[key] = entry
        self._history.append(entry)
        logger.info(
            "评分任务已接收: key=%s source=%s fence=%d",
            key,
            source,
            fence,
        )
        task = asyncio.create_task(
            self._run_entry(entry, runner),
            name=f"score:{key}:fence-{fence}",
        )
        entry.task = task
        return SubmitResult(SubmitStatus.ACCEPTED, command)

    async def _run_entry(self, entry: TaskEntry, runner: TaskRunner) -> None:
        entry.state = TaskState.STARTED
        entry.started_at = self._clock()
        logger.info(
            "评分任务已开始: key=%s source=%s fence=%d",
            entry.command.key,
            entry.command.source,
            entry.command.fence,
        )
        try:
            await runner(entry.command)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            entry.error = exc
            logger.exception(
                "评分后台任务异常: key=%s fence=%d",
                entry.command.key,
                entry.command.fence,
            )
        finally:
            entry.state = TaskState.COMPLETED
            entry.completed_at = self._clock()
            current = self._active.get(entry.command.key)
            if current is entry:
                self._active.pop(entry.command.key, None)
            logger.info(
                "评分任务已完成: key=%s source=%s fence=%d",
                entry.command.key,
                entry.command.source,
                entry.command.fence,
            )

    def stop_accepting(self) -> None:
        self._accepting = False

    async def drain(self, timeout_seconds: float) -> bool:
        """停止接单并等待在岗任务；超时后取消剩余任务。"""
        self.stop_accepting()
        tasks = [
            entry.task
            for entry in tuple(self._active.values())
            if entry.task is not None and not entry.task.done()
        ]
        if not tasks:
            return True
        try:
            async with asyncio.timeout(timeout_seconds):
                await asyncio.gather(*tasks, return_exceptions=True)
            return True
        except TimeoutError:
            logger.warning("评分任务 drain 超时，取消剩余任务: count=%d", len(tasks))
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            return False
