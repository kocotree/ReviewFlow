"""飞书通知策略与发送侧熔断；卡片模板位于 ``card_templates``。"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from app.card_action import ACTION_ADMIN_RETRY, build_score_action_value
from app.card_templates import (
    build_circuit_breaker_card,
    build_error_card,
    build_failed_card,
    build_material_error_card,
    build_passed_card,
    build_rejected_card,
    record_url,
    rescore_value,
)
from app.config import Config
from app.send_circuit_breaker import SendCircuitBreaker
from app.workflow_errors import MaterialProblem

logger = logging.getLogger(__name__)


class NotificationManager:
    """所有通知共用发送熔断，不再维护用户冷却或每日计数。"""

    def __init__(
        self,
        config: Config,
        feishu: Any,
        *,
        send_circuit_breaker: SendCircuitBreaker | None = None,
    ) -> None:
        self._config = config
        self._feishu = feishu
        self._send_circuit_breaker = send_circuit_breaker or SendCircuitBreaker(
            window_seconds=config.send_circuit_breaker_window_minutes * 60,
            max_messages=config.send_circuit_breaker_max_messages,
        )

    async def _send_card(
        self,
        *,
        record_id: str,
        receive_id: str,
        card: dict[str, Any],
        receive_id_type: str = "open_id",
    ) -> bool:
        permit = self._send_circuit_breaker.acquire(record_id)
        if not permit.allowed:
            logger.critical(
                "卡片发送熔断: record=%s count=%d window=%ds",
                record_id,
                permit.count,
                int(self._send_circuit_breaker.window_seconds),
            )
            if permit.should_alert:
                await self._notify_circuit_breaker_to_group(record_id, permit.count)
            return False
        try:
            success = await self._feishu.send_card_message(
                receive_id,
                card,
                receive_id_type=receive_id_type,
            )
        except Exception:
            self._send_circuit_breaker.rollback(record_id, permit.reservation_id)
            raise
        if not success:
            self._send_circuit_breaker.rollback(record_id, permit.reservation_id)
        return success

    async def _notify_circuit_breaker_to_group(
        self,
        record_id: str,
        observed_count: int,
    ) -> bool:
        chat_id = self._config.notification_group_chat_id
        if not chat_id:
            logger.error("卡片发送熔断已触发，但未配置管理员告警群")
            return False
        return await self._feishu.send_card_message(
            chat_id,
            build_circuit_breaker_card(
                record_id=record_id,
                observed_count=observed_count,
                window_minutes=self._config.send_circuit_breaker_window_minutes,
                max_messages=self._config.send_circuit_breaker_max_messages,
            ),
            receive_id_type="chat_id",
        )

    async def notify_score_failed(
        self,
        *,
        open_id: str,
        record_id: str,
        score: int,
        detail: str,
        threshold: int,
        base_url: str = "",
        app_token: str = "",
        table_id: str = "",
        rounds: int = 0,
        max_rounds: int = 0,
    ) -> bool:
        action_value = rescore_value(app_token, table_id, record_id)
        show_rescore = bool(action_value) and (
            not max_rounds or rounds < max_rounds
        )
        return await self._send_card(
            record_id=record_id,
            receive_id=open_id,
            card=build_failed_card(
                score=score,
                detail=detail,
                threshold=threshold,
                record_url=record_url(base_url, app_token, table_id, record_id),
                action_value=action_value if show_rescore else None,
            ),
        )

    async def notify_score_passed(
        self,
        *,
        open_id: str,
        record_id: str,
        score: int,
        threshold: int,
        highlights: str = "",
        improvements: str = "",
        **_: Any,
    ) -> bool:
        return await self._send_card(
            record_id=record_id,
            receive_id=open_id,
            card=build_passed_card(score, threshold, highlights, improvements),
        )

    async def notify_rejected(
        self,
        *,
        open_id: str,
        record_id: str,
        score: int,
        detail: str,
        rounds: int,
        **_: Any,
    ) -> bool:
        return await self._send_card(
            record_id=record_id,
            receive_id=open_id,
            card=build_rejected_card(score, detail, rounds),
        )

    async def notify_material_error(
        self,
        *,
        open_id: str,
        record_id: str,
        kind: str,
        reason: str,
        problems: Iterable[Any] = (),
        app_token: str = "",
        table_id: str = "",
        base_url: str = "",
    ) -> bool:
        normalized = [
            (
                str(getattr(problem, "name", "") or "问题材料"),
                str(getattr(problem, "reason", "") or reason),
            )
            for problem in problems
        ]
        return await self._send_card(
            record_id=record_id,
            receive_id=open_id,
            card=build_material_error_card(
                kind=kind,
                reason=reason,
                problems=normalized,
                record_url=record_url(base_url, app_token, table_id, record_id),
                action_value=rescore_value(app_token, table_id, record_id),
            ),
        )

    async def notify_format_unsupported(
        self,
        open_id: str,
        record_id: str,
        unsupported_files: list[str],
        **locator: Any,
    ) -> bool:
        return await self.notify_material_error(
            open_id=open_id,
            record_id=record_id,
            kind="unsupported",
            reason="存在不支持格式的附件",
            problems=[
                MaterialProblem(name=name, reason="格式不支持")
                for name in unsupported_files
            ],
            **locator,
        )

    async def notify_unprocessable(
        self,
        open_id: str,
        record_id: str,
        reason: str,
        **locator: Any,
    ) -> bool:
        return await self.notify_material_error(
            open_id=open_id,
            record_id=record_id,
            kind="no_file",
            reason=reason,
            **locator,
        )

    async def notify_error_to_group(
        self,
        *,
        record_id: str,
        error: str,
        record_url: str = "",
        app_token: str = "",
        table_id: str = "",
    ) -> bool:
        chat_id = self._config.notification_group_chat_id
        if not chat_id:
            return False
        action_value = (
            build_score_action_value(
                action=ACTION_ADMIN_RETRY,
                app_token=app_token,
                table_id=table_id,
                record_id=record_id,
            )
            if app_token and table_id and record_id
            else None
        )
        return await self._send_card(
            record_id=record_id,
            receive_id=chat_id,
            receive_id_type="chat_id",
            card=build_error_card(record_id, error, record_url, action_value),
        )
