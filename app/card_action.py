"""飞书消息卡片动作解析与业务响应。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.record_coordinator import (
    RecordCoordinator,
    RequestStatus,
    derive_callback_id,
)
from app.task_registry import RecordKey
from app.workflow_state import TriggerSource


ACTION_RESCORE = "rescore"
ACTION_ADMIN_RETRY = "admin_retry"


@dataclass(frozen=True)
class DecodedCardAction:
    actor_open_id: str
    open_message_id: str
    open_chat_id: str
    action_value: dict[str, Any]


@dataclass(frozen=True)
class CardActionResponse:
    content: str
    kind: str = "info"
    status_code: int = 200
    replacement_card: dict[str, Any] | None = None

    def as_lark_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "toast": {"type": self.kind, "content": self.content}
        }
        if self.replacement_card is not None:
            payload["card"] = self.replacement_card
        return payload


class CardActionService:
    def __init__(
        self,
        *,
        coordinator: RecordCoordinator,
        admin_chat_id: str = "",
    ) -> None:
        self._coordinator = coordinator
        self._admin_chat_id = admin_chat_id

    async def handle(self, decoded: DecodedCardAction) -> CardActionResponse:
        value = decoded.action_value
        action = str(value.get("action", "") or "")
        if action not in {ACTION_RESCORE, ACTION_ADMIN_RETRY}:
            return CardActionResponse("未知的卡片操作", kind="error", status_code=400)

        required = ("app_token", "table_id", "record_id")
        if any(not isinstance(value.get(name), str) or not value.get(name) for name in required):
            return CardActionResponse("卡片缺少记录定位信息", kind="error", status_code=400)

        source = (
            TriggerSource.USER_RESCORE
            if action == ACTION_RESCORE
            else TriggerSource.ADMIN_RETRY
        )
        if source is TriggerSource.ADMIN_RETRY:
            if not self._admin_chat_id or decoded.open_chat_id != self._admin_chat_id:
                return CardActionResponse("无权执行管理员重试", kind="error", status_code=403)

        key = RecordKey(
            app_token=value["app_token"],
            table_id=value["table_id"],
            record_id=value["record_id"],
        )
        callback_id = derive_callback_id(
            open_message_id=decoded.open_message_id,
            actor_open_id=decoded.actor_open_id,
            action_value=value,
        )
        result = await self._coordinator.request_score(
            key=key,
            source=source,
            actor_open_id=decoded.actor_open_id,
            callback_id=callback_id,
        )
        return self._response_for(result.status, source)

    @staticmethod
    def _response_for(
        status: RequestStatus,
        source: TriggerSource,
    ) -> CardActionResponse:
        if status in {RequestStatus.ACCEPTED, RequestStatus.DUPLICATE_CALLBACK}:
            content = (
                "已提交管理员重试"
                if source is TriggerSource.ADMIN_RETRY
                else "已提交重新评分"
            )
            return CardActionResponse(
                content,
                replacement_card=_submitted_card(
                    "管理员重试已受理"
                    if source is TriggerSource.ADMIN_RETRY
                    else "重新评分已受理"
                ),
            )
        if status is RequestStatus.ALREADY_RUNNING:
            return CardActionResponse("正在评分，请勿重复点击", kind="warning")
        if status is RequestStatus.FORBIDDEN:
            return CardActionResponse("只有原提报人可以重新评分", kind="error", status_code=403)
        if status is RequestStatus.NOT_RPA:
            return CardActionResponse(
                "该记录的“是否RPA”不是“是”，不触发评分",
                kind="warning",
            )
        if status is RequestStatus.NOT_TRIGGERABLE:
            return CardActionResponse("当前记录状态不可重新评分", kind="warning", status_code=409)
        if status is RequestStatus.RECORD_NOT_FOUND:
            return CardActionResponse("记录不存在或已删除", kind="error", status_code=404)
        return CardActionResponse("服务正在关闭，请稍后重试", kind="warning", status_code=503)


def build_score_action_value(
    *,
    action: str,
    app_token: str,
    table_id: str,
    record_id: str,
) -> dict[str, str]:
    """卡片 payload 只包含动作类型和记录定位信息。"""
    if action not in {ACTION_RESCORE, ACTION_ADMIN_RETRY}:
        raise ValueError(f"不支持的卡片动作: {action}")
    return {
        "action": action,
        "app_token": app_token,
        "table_id": table_id,
        "record_id": record_id,
    }


def _submitted_card(title: str) -> dict[str, Any]:
    """回调往返后替换旧卡片，禁用按钮仅作为 UX 提示。"""
    return {
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "评分任务已提交，请等待机器人返回结果。",
                },
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "正在评分"},
                        "type": "default",
                        "disabled": True,
                    }
                ],
            },
        ],
    }
