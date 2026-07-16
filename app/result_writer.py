"""评分状态与最终结果的统一写回边界。"""

from __future__ import annotations

from typing import Any

from app.field_mapping import FIELD_SCORE_STATUS
from app.retry import retry_step
from app.task_registry import ScoreCommand, TaskRegistry
from app.workflow_errors import FinalWriteError, StateWriteError
from app.workflow_state import ScoreStatus


class ResultWriter:
    def __init__(
        self,
        *,
        feishu: Any,
        registry: TaskRegistry,
        max_attempts: int = 3,
    ) -> None:
        self._feishu = feishu
        self._registry = registry
        self._max_attempts = max_attempts

    async def enter_scoring(self, command: ScoreCommand) -> bool:
        return await self.update(
            command,
            {FIELD_SCORE_STATUS: ScoreStatus.SCORING.value},
            operation="enter_scoring",
        )

    async def set_status(self, command: ScoreCommand, status: ScoreStatus) -> bool:
        return await self.update(
            command,
            {FIELD_SCORE_STATUS: status.value},
            operation=f"set_status_{status.value}",
        )

    async def write_final(
        self,
        command: ScoreCommand,
        fields: dict[str, Any],
    ) -> bool:
        try:
            return await self.update(command, fields, operation="write_final")
        except StateWriteError as exc:
            raise FinalWriteError(str(exc)) from exc

    async def update(
        self,
        command: ScoreCommand,
        fields: dict[str, Any],
        *,
        operation: str,
    ) -> bool:
        if not self._registry.is_current(command.key, command.fence):
            return False

        async def call() -> bool:
            if not self._registry.is_current(command.key, command.fence):
                return False
            result = await self._feishu.update_record(
                command.key.record_id,
                fields,
                app_token=command.key.app_token,
                table_id=command.key.table_id,
            )
            if result is False:
                raise StateWriteError(f"{operation} 写回失败")
            return True

        return await retry_step(
            operation,
            call,
            max_attempts=self._max_attempts,
        )
