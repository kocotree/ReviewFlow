"""恢复非优雅退出后遗留在“评分中”的孤儿记录。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from app.field_mapping import FIELD_SCORE_STATUS
from app.record_coordinator import RecordCoordinator
from app.retry import retry_step
from app.task_registry import RecordKey, TaskRegistry
from app.workflow_errors import StateWriteError
from app.workflow_state import ScoreStatus, TriggerSource

logger = logging.getLogger(__name__)


class ScoringScavenger:
    def __init__(
        self,
        *,
        feishu: Any,
        coordinator: RecordCoordinator,
        registry: TaskRegistry,
        app_token: str,
        table_id: str,
        orphan_timeout_seconds: float,
        interval_seconds: float,
        clock: Callable[[], float] = time.time,
        max_attempts: int = 3,
    ) -> None:
        self._feishu = feishu
        self._coordinator = coordinator
        self._registry = registry
        self._app_token = app_token
        self._table_id = table_id
        self._orphan_timeout_seconds = orphan_timeout_seconds
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._max_attempts = max_attempts

    async def run_once(self) -> int:
        records = await retry_step(
            "list_scoring_records",
            lambda: self._feishu.list_scoring_records(
                app_token=self._app_token,
                table_id=self._table_id,
            ),
            max_attempts=self._max_attempts,
        )
        recovered = 0
        now_ms = int(self._clock() * 1000)
        timeout_ms = int(self._orphan_timeout_seconds * 1000)
        for record in records:
            record_id = str(record.get("record_id", "") or "")
            if not record_id:
                continue
            key = RecordKey(self._app_token, self._table_id, record_id)
            last_modified = int(record.get("last_modified_time", 0) or 0)
            if now_ms - last_modified < timeout_ms:
                continue
            if self._registry.has_active(key):
                continue

            async def reset() -> bool:
                result = await self._feishu.update_record(
                    record_id,
                    {FIELD_SCORE_STATUS: ScoreStatus.PENDING.value},
                    app_token=self._app_token,
                    table_id=self._table_id,
                )
                if result is False:
                    raise StateWriteError("清道夫复位评分状态失败")
                return True

            await retry_step(
                "reset_orphan_scoring_record",
                reset,
                max_attempts=self._max_attempts,
            )
            result = await self._coordinator.request_score(
                key=key,
                source=TriggerSource.SCAVENGER,
            )
            if result.accepted:
                recovered += 1
                logger.warning("已恢复评分中孤儿记录: key=%s", key)
        return recovered

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("评分孤儿清道夫扫描失败")
            try:
                async with asyncio.timeout(self._interval_seconds):
                    await stop_event.wait()
            except TimeoutError:
                pass
