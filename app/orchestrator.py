"""评分协调器薄门面。

内容解析、外部网关、状态写回和通知均由独立组件负责；本模块只把命令交给
``ScoringWorkflow``，并保留一个便于脚本调用的 ``process_record`` 入口。
"""

from __future__ import annotations

import asyncio

from app.scoring_workflow import ScoringWorkflow, WorkflowOutcome
from app.task_registry import RecordKey, ScoreCommand, SubmitStatus, TaskRegistry
from app.workflow_state import ScoreStatus, TriggerSource

STATUS_PENDING = ScoreStatus.PENDING.value
STATUS_SCORING = ScoreStatus.SCORING.value
STATUS_PASSED = ScoreStatus.PASSED.value
STATUS_FAILED = ScoreStatus.FAILED.value
STATUS_REJECTED = ScoreStatus.REJECTED.value
STATUS_ERROR = ScoreStatus.ERROR.value


class Orchestrator:
    def __init__(
        self,
        *,
        workflow: ScoringWorkflow,
        registry: TaskRegistry,
    ) -> None:
        self._workflow = workflow
        self._registry = registry

    async def process_command(self, command: ScoreCommand) -> WorkflowOutcome:
        return await self._workflow.run(command)

    async def process_record(
        self,
        app_token: str,
        table_id: str,
        record_id: str,
        *,
        source: TriggerSource = TriggerSource.INITIAL_EVENT,
        actor_open_id: str = "",
    ) -> WorkflowOutcome | None:
        """脚本兼容入口；Web 服务应统一经 ``RecordCoordinator`` 申请任务。"""
        outcomes: list[WorkflowOutcome] = []

        async def runner(command: ScoreCommand) -> None:
            outcomes.append(await self.process_command(command))

        key = RecordKey(app_token, table_id, record_id)
        submitted = self._registry.submit(
            key=key,
            source=source,
            runner=runner,
            actor_open_id=actor_open_id,
        )
        if submitted.status is not SubmitStatus.ACCEPTED:
            return None
        entry = self._registry.active_entry(key)
        task = entry.task if entry else None
        if task is not None:
            await task
        return outcomes[0] if outcomes else None

    async def close(self) -> None:
        """协调器本身不拥有外部资源，由应用运行时统一关闭。"""
        await asyncio.sleep(0)
