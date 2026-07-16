"""评分工作流：状态流转与固定步骤协调。"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from app.ai import AIClient, ScoringPayload
from app.config import Config
from app.errors import ErrorCategory, FeishuNotFoundError, ReviewFlowError
from app.field_mapping import (
    FIELD_AI_SCORE,
    FIELD_REVISION_ROUNDS,
    FIELD_SCORE_STATUS,
    FIELD_SUBMITTER,
)
from app.record_coordinator import extract_open_id, extract_requirement_title
from app.result_writer import ResultWriter
from app.retry import retry_step
from app.task_registry import ScoreCommand, TaskRegistry
from app.workflow_errors import FinalWriteError
from app.workflow_state import ScoreStatus, TriggerSource

logger = logging.getLogger(__name__)


class WorkflowOutcome(StrEnum):
    COMPLETED = "completed"
    MATERIAL_FAILED = "material_failed"
    TECHNICAL_FAILED = "technical_failed"
    ENTRY_FAILED = "entry_failed"
    FINAL_WRITE_FAILED = "final_write_failed"
    STALE = "stale"
    RECORD_MISSING = "record_missing"


class ScoringWorkflow:
    def __init__(
        self,
        *,
        config: Config,
        feishu: Any,
        ai: AIClient,
        collector: Any,
        notifier: Any,
        registry: TaskRegistry,
        writer: ResultWriter | None = None,
        max_attempts: int = 3,
    ) -> None:
        self._config = config
        self._feishu = feishu
        self._ai = ai
        self._collector = collector
        self._notifier = notifier
        self._registry = registry
        self._writer = writer or ResultWriter(
            feishu=feishu,
            registry=registry,
            max_attempts=max_attempts,
        )
        self._max_attempts = max_attempts

    async def run(self, command: ScoreCommand) -> WorkflowOutcome:
        try:
            fields = await retry_step(
                "get_record_snapshot",
                lambda: self._feishu.get_record(
                    command.key.record_id,
                    app_token=command.key.app_token,
                    table_id=command.key.table_id,
                ),
                max_attempts=self._max_attempts,
            )
        except FeishuNotFoundError:
            return WorkflowOutcome.RECORD_MISSING

        if not fields:
            return WorkflowOutcome.RECORD_MISSING
        rounds = self._rounds(fields)
        open_id = extract_open_id(fields.get(FIELD_SUBMITTER))
        requirement_title = extract_requirement_title(fields, command.key.record_id)

        try:
            entered = await self._writer.enter_scoring(command)
        except ReviewFlowError as exc:
            logger.error("进入评分中失败: key=%s error=%s", command.key, exc)
            return WorkflowOutcome.ENTRY_FAILED
        if not entered:
            return WorkflowOutcome.STALE

        try:
            collected = await self._collector.collect(fields)
            score_payload = ScoringPayload(
                text=self._score_prompt(collected.original_description),
                pdf_files=[(collected.review_bundle_pdf, "review-bundle.pdf")],
            )
            result = await retry_step(
                "score_review_bundle",
                lambda: self._ai.score(score_payload),
                max_attempts=self._max_attempts,
            )

            if not self._registry.is_current(command.key, command.fence):
                logger.warning(
                    "评分结果因 fencing 失效而作废: key=%s fence=%d",
                    command.key,
                    command.fence,
                )
                return WorkflowOutcome.STALE

            score = int(result["score"])
            new_rounds = rounds + (
                1 if command.source is TriggerSource.USER_RESCORE else 0
            )
            passed = score >= self._config.score_threshold
            if passed:
                final_status = ScoreStatus.PASSED
            elif (
                command.source is TriggerSource.USER_RESCORE
                and new_rounds >= self._config.max_revision_rounds
            ):
                final_status = ScoreStatus.REJECTED
            else:
                final_status = ScoreStatus.FAILED

            final_fields = {
                FIELD_AI_SCORE: score,
                FIELD_SCORE_STATUS: final_status.value,
                FIELD_REVISION_ROUNDS: new_rounds,
            }
            written = await self._writer.write_final(command, final_fields)
            if not written:
                return WorkflowOutcome.STALE

            await self._notify_completed(
                command=command,
                open_id=open_id,
                requirement_title=requirement_title,
                status=final_status,
                score=score,
                detail=result["detail"],
                highlights=result.get("highlights", ""),
                improvements=result.get("improvements", ""),
                rounds=new_rounds,
            )
            return WorkflowOutcome.COMPLETED
        except FinalWriteError as exc:
            logger.error("最终评分写回失败: key=%s error=%s", command.key, exc)
            await self._notify_admin_error(command, exc, requirement_title)
            return WorkflowOutcome.FINAL_WRITE_FAILED
        except ReviewFlowError as exc:
            if exc.category is ErrorCategory.USER_FIXABLE:
                await self._recover_user_material_failure(
                    command=command,
                    open_id=open_id,
                    requirement_title=requirement_title,
                    error=exc,
                )
                return WorkflowOutcome.MATERIAL_FAILED
            await self._recover_technical_failure(command, exc, requirement_title)
            return WorkflowOutcome.TECHNICAL_FAILED
        except Exception as exc:
            logger.exception("评分工作流未分类异常: key=%s", command.key)
            await self._recover_technical_failure(command, exc, requirement_title)
            return WorkflowOutcome.TECHNICAL_FAILED

    async def _recover_user_material_failure(
        self,
        *,
        command: ScoreCommand,
        open_id: str,
        requirement_title: str,
        error: ReviewFlowError,
    ) -> None:
        try:
            await self._writer.set_status(command, ScoreStatus.FAILED)
        except ReviewFlowError:
            logger.exception("材料失败状态写回失败: key=%s", command.key)
            return
        if not open_id:
            return
        problems = getattr(error, "problems", ())
        await self._notifier.notify_material_error(
            open_id=open_id,
            record_id=command.key.record_id,
            requirement_title=requirement_title,
            kind=getattr(error, "notification_kind", "material_error"),
            reason=str(error),
            problems=problems,
            app_token=command.key.app_token,
            table_id=command.key.table_id,
            base_url=self._config.feishu_base_url,
        )

    async def _recover_technical_failure(
        self,
        command: ScoreCommand,
        error: BaseException,
        requirement_title: str,
    ) -> None:
        try:
            await self._writer.set_status(command, ScoreStatus.ERROR)
        except ReviewFlowError:
            logger.exception("评分异常状态写回失败: key=%s", command.key)
        await self._notify_admin_error(command, error, requirement_title)

    async def _notify_admin_error(
        self,
        command: ScoreCommand,
        error: BaseException,
        requirement_title: str,
    ) -> None:
        await self._notifier.notify_error_to_group(
            record_id=command.key.record_id,
            requirement_title=requirement_title,
            error=str(error),
            record_url=self._record_url(command),
            app_token=command.key.app_token,
            table_id=command.key.table_id,
        )

    async def _notify_completed(
        self,
        *,
        command: ScoreCommand,
        open_id: str,
        requirement_title: str,
        status: ScoreStatus,
        score: int,
        detail: str,
        highlights: str,
        improvements: str,
        rounds: int,
    ) -> None:
        if not open_id:
            return
        common = {
            "open_id": open_id,
            "record_id": command.key.record_id,
            "requirement_title": requirement_title,
            "app_token": command.key.app_token,
            "table_id": command.key.table_id,
        }
        if status is ScoreStatus.PASSED:
            await self._notifier.notify_score_passed(
                **common,
                score=score,
                threshold=self._config.score_threshold,
                highlights=highlights,
                improvements=improvements,
            )
        elif status is ScoreStatus.REJECTED:
            await self._notifier.notify_rejected(
                **common,
                score=score,
                detail=detail,
                rounds=rounds,
            )
        else:
            await self._notifier.notify_score_failed(
                **common,
                score=score,
                detail=detail,
                threshold=self._config.score_threshold,
                rounds=rounds,
                max_rounds=self._config.max_revision_rounds,
                base_url=self._config.feishu_base_url,
            )

    @staticmethod
    def _status(fields: dict[str, Any]) -> ScoreStatus:
        try:
            return ScoreStatus(fields.get(FIELD_SCORE_STATUS, ScoreStatus.PENDING))
        except ValueError:
            return ScoreStatus.PENDING

    @staticmethod
    def _rounds(fields: dict[str, Any]) -> int:
        try:
            return max(0, int(fields.get(FIELD_REVISION_ROUNDS, 0) or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _score_prompt(original_description: str) -> str:
        description = original_description.strip() or "（无补充描述）"
        return (
            "请结合随附的唯一总 PDF 对本次提交进行综合评分。\n\n"
            "=== 原始描述（总 PDF 之外的补充文本）===\n"
            f"{description}"
        )

    def _record_url(self, command: ScoreCommand) -> str:
        base = self._config.feishu_base_url
        if not base:
            return ""
        return (
            f"{base}/base/{command.key.app_token}"
            f"?table={command.key.table_id}&record={command.key.record_id}"
        )
