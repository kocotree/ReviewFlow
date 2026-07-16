"""统一评分申请入口：状态门控、身份校验、幂等与任务注册。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Awaitable, Callable

from app.errors import FeishuNotFoundError
from app.field_mapping import FIELD_SCORE_STATUS, FIELD_SUBMITTER
from app.task_registry import (
    RecordKey,
    ScoreCommand,
    SubmitStatus,
    TaskRegistry,
)
from app.workflow_state import (
    ADMIN_RECOVERABLE_STATUSES,
    USER_RECOVERABLE_STATUSES,
    ScoreStatus,
    TriggerSource,
)


def extract_open_id(submitter: Any) -> str:
    if isinstance(submitter, list) and submitter:
        first = submitter[0]
        return first.get("id", "") if isinstance(first, dict) else ""
    if isinstance(submitter, dict):
        return str(submitter.get("id", "") or "")
    return str(submitter or "")


class RequestStatus(StrEnum):
    ACCEPTED = "accepted"
    ALREADY_RUNNING = "already_running"
    DUPLICATE_CALLBACK = "duplicate_callback"
    NOT_TRIGGERABLE = "not_triggerable"
    FORBIDDEN = "forbidden"
    RECORD_NOT_FOUND = "record_not_found"
    SHUTTING_DOWN = "shutting_down"


@dataclass(frozen=True)
class ScoreRequestResult:
    status: RequestStatus
    command: ScoreCommand | None = None

    @property
    def accepted(self) -> bool:
        return self.status is RequestStatus.ACCEPTED


WorkflowRunner = Callable[[ScoreCommand], Awaitable[None]]


class IdempotencyCache:
    def __init__(
        self,
        *,
        window_seconds: float = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window_seconds = window_seconds
        self._clock = clock
        self._seen: dict[str, float] = {}

    def claim(self, key: str) -> bool:
        now = self._clock()
        cutoff = now - self._window_seconds
        self._seen = {k: ts for k, ts in self._seen.items() if ts > cutoff}
        if key in self._seen:
            return False
        self._seen[key] = now
        return True

    def release(self, key: str) -> None:
        self._seen.pop(key, None)


def derive_callback_id(
    *,
    open_message_id: str,
    actor_open_id: str,
    action_value: dict[str, Any],
) -> str:
    """从稳定回调字段派生幂等键，不向卡片 payload 增加技术字段。"""
    encoded = json.dumps(
        {
            "message": open_message_id,
            "actor": actor_open_id,
            "value": action_value,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class RecordCoordinator:
    """所有首次事件、用户重评、管理员重试共用的申请入口。"""

    def __init__(
        self,
        *,
        feishu: Any,
        registry: TaskRegistry,
        runner: WorkflowRunner,
        callback_ids: IdempotencyCache | None = None,
    ) -> None:
        self._feishu = feishu
        self._registry = registry
        self._runner = runner
        self._callback_ids = callback_ids or IdempotencyCache()
        self._admission_locks: dict[RecordKey, asyncio.Lock] = {}

    @property
    def registry(self) -> TaskRegistry:
        return self._registry

    async def request_score(
        self,
        *,
        key: RecordKey,
        source: TriggerSource,
        actor_open_id: str = "",
        callback_id: str = "",
    ) -> ScoreRequestResult:
        if callback_id and not self._callback_ids.claim(callback_id):
            return ScoreRequestResult(RequestStatus.DUPLICATE_CALLBACK)

        lock = self._admission_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if not self._registry.accepting:
                self._release_callback(callback_id)
                return ScoreRequestResult(RequestStatus.SHUTTING_DOWN)
            if self._registry.has_active(key):
                return ScoreRequestResult(RequestStatus.ALREADY_RUNNING)

            try:
                fields = await self._feishu.get_record(
                    key.record_id,
                    app_token=key.app_token,
                    table_id=key.table_id,
                )
            except FeishuNotFoundError:
                self._release_callback(callback_id)
                return ScoreRequestResult(RequestStatus.RECORD_NOT_FOUND)
            if fields is None:
                self._release_callback(callback_id)
                return ScoreRequestResult(RequestStatus.RECORD_NOT_FOUND)

            try:
                status = ScoreStatus(fields.get(FIELD_SCORE_STATUS, ScoreStatus.PENDING))
            except ValueError:
                self._release_callback(callback_id)
                return ScoreRequestResult(RequestStatus.NOT_TRIGGERABLE)

            if (
                status is ScoreStatus.SCORING
                and source in {TriggerSource.USER_RESCORE, TriggerSource.ADMIN_RETRY}
            ):
                return ScoreRequestResult(RequestStatus.ALREADY_RUNNING)

            if not self._source_allows_status(source, status):
                self._release_callback(callback_id)
                return ScoreRequestResult(RequestStatus.NOT_TRIGGERABLE)

            if source is TriggerSource.USER_RESCORE:
                submitter_open_id = extract_open_id(fields.get(FIELD_SUBMITTER))
                if not actor_open_id or actor_open_id != submitter_open_id:
                    self._release_callback(callback_id)
                    return ScoreRequestResult(RequestStatus.FORBIDDEN)

            submit = self._registry.submit(
                key=key,
                source=source,
                runner=self._runner,
                actor_open_id=actor_open_id,
                callback_id=callback_id,
            )
            if submit.status is SubmitStatus.ACCEPTED:
                return ScoreRequestResult(RequestStatus.ACCEPTED, submit.command)
            if submit.status is SubmitStatus.ALREADY_RUNNING:
                return ScoreRequestResult(RequestStatus.ALREADY_RUNNING)
            self._release_callback(callback_id)
            return ScoreRequestResult(RequestStatus.SHUTTING_DOWN)

    @staticmethod
    def _source_allows_status(source: TriggerSource, status: ScoreStatus) -> bool:
        if source in (TriggerSource.INITIAL_EVENT, TriggerSource.SCAVENGER):
            return status is ScoreStatus.PENDING
        if source is TriggerSource.USER_RESCORE:
            return status in USER_RECOVERABLE_STATUSES
        if source is TriggerSource.ADMIN_RETRY:
            return status in ADMIN_RECOVERABLE_STATUSES
        return False

    def _release_callback(self, callback_id: str) -> None:
        if callback_id:
            self._callback_ids.release(callback_id)
