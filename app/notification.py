"""消息通知管理 —— 飞书机器人消息发送 + 频率控制。

特性：
- 支持文本消息和卡片消息
- 通知冷却：同一记录两次通知至少间隔 N 分钟
- 每用户每日通知上限
- 通过/未通过通知模板
"""

import logging
import time
from collections import defaultdict
from typing import Any

from app.config import Config
from app.feishu import FeishuClient

logger = logging.getLogger(__name__)


class NotificationManager:
    """消息通知管理器。

    使用示例::

        nm = NotificationManager(config, client)
        await nm.notify_score_failed(
            open_id="ou_xxx",
            record_id="rec_xxx",
            score=55,
            detail="需要补充更多细节...",
            threshold=60,
        )
    """

    def __init__(self, config: Config, feishu: FeishuClient) -> None:
        self._config = config
        self._feishu = feishu

        # 通知时间记录，用于冷却控制
        # {record_id: 最近通知时间戳}
        self._record_last_notify: dict[str, float] = {}

        # 每用户每日通知计数
        # {open_id: {date_str: count}}
        self._user_daily_count: dict[str, dict[str, int]] = defaultdict(dict)

        # 群告警冷却记录（独立于用户通知）
        # {record_id: 最近群告警时间戳}
        self._group_last_notify: dict[str, float] = {}

    def can_notify(self, record_id: str, open_id: str) -> bool:
        """检查是否允许发送通知。

        Args:
            record_id: 记录 ID（用于冷却检查）。
            open_id: 用户 open_id（用于每日上限检查）。

        Returns:
            是否可以发送通知。
        """
        now = time.time()
        today = time.strftime("%Y-%m-%d", time.localtime(now))

        # 检查记录级冷却
        last_time = self._record_last_notify.get(record_id, 0)
        cooldown_seconds = self._config.notification_cooldown_minutes * 60
        if now - last_time < cooldown_seconds:
            logger.info(
                "通知冷却中: record=%s last=%.0fs ago cooldown=%ds",
                record_id, now - last_time, cooldown_seconds,
            )
            return False

        # 检查用户每日上限
        daily = self._user_daily_count.get(open_id, {}).get(today, 0)
        if daily >= self._config.max_daily_notifications_per_user:
            logger.info(
                "用户通知已达每日上限: user=%s count=%d max=%d",
                open_id, daily, self._config.max_daily_notifications_per_user,
            )
            return False

        return True

    def record_notification(self, record_id: str, open_id: str) -> None:
        """记录一次通知发送（调用者需确保 can_notify 已通过）。"""
        now = time.time()
        today = time.strftime("%Y-%m-%d", time.localtime(now))

        self._record_last_notify[record_id] = now

        user_counts = self._user_daily_count[open_id]
        user_counts[today] = user_counts.get(today, 0) + 1

    async def notify_score_failed(
        self,
        open_id: str,
        record_id: str,
        score: int,
        detail: str,
        threshold: int,
        base_url: str = "",
        app_token: str = "",
        table_id: str = "",
    ) -> bool:
        """发送"评分未通过"通知。

        Returns:
            是否发送成功。
        """
        if not self.can_notify(record_id, open_id):
            return False

        # 构建记录链接（如果提供了 base_url）
        record_url = ""
        if base_url and app_token and table_id:
            record_url = (
                f"{base_url}/base/{app_token}"
                f"?table={table_id}&record={record_id}"
            )

        card = _build_failed_card(score, detail, threshold, record_url)
        success = await self._feishu.send_card_message(open_id, card)

        if success:
            self.record_notification(record_id, open_id)
            logger.info(
                "发送未通过通知: user=%s record=%s score=%d",
                open_id, record_id, score,
            )

        return success

    async def notify_score_passed(
        self,
        open_id: str,
        record_id: str,
        score: int,
        threshold: int,
    ) -> bool:
        """发送"评分通过"通知（不计入频率限制）。"""
        card = _build_passed_card(score, threshold)
        success = await self._feishu.send_card_message(open_id, card)
        if success:
            logger.info(
                "发送通过通知: user=%s record=%s score=%d",
                open_id, record_id, score,
            )
        return success

    async def notify_rejected(
        self,
        open_id: str,
        record_id: str,
        score: int,
        detail: str,
        rounds: int,
        admin_open_id: str = "",
    ) -> bool:
        """发送"已驳回"通知（超过最大修改轮次）。"""
        card = _build_rejected_card(score, detail, rounds)
        success = await self._feishu.send_card_message(open_id, card)

        # 同时通知管理员（如果配置了）
        if admin_open_id:
            admin_card = _build_admin_rejected_card(
                open_id, record_id, score, detail, rounds
            )
            await self._feishu.send_card_message(admin_open_id, admin_card)

        return success

    async def notify_format_unsupported(
        self,
        open_id: str,
        record_id: str,
        unsupported_files: list[str],
    ) -> bool:
        """发送"附件格式不符"通知（跳过审核，Point 5）。

        受冷却/每日上限约束，避免用户反复提交坏格式时刷屏。
        """
        if not self.can_notify(record_id, open_id):
            return False

        card = _build_format_unsupported_card(unsupported_files)
        success = await self._feishu.send_card_message(open_id, card)

        if success:
            self.record_notification(record_id, open_id)
            logger.info(
                "发送格式不符通知: user=%s record=%s files=%s",
                open_id, record_id, unsupported_files,
            )

        return success

    async def notify_unprocessable(
        self,
        open_id: str,
        record_id: str,
        reason: str,
    ) -> bool:
        """发送"无可评审内容"通知（可用内容为空，跳过评分）。

        典型场景：用户仅上传了图片，但当前模型不支持图片审核，且无其他
        文本/文档可评。受冷却/每日上限约束。
        """
        if not self.can_notify(record_id, open_id):
            return False

        card = _build_unprocessable_card(reason)
        success = await self._feishu.send_card_message(open_id, card)

        if success:
            self.record_notification(record_id, open_id)
            logger.info(
                "发送无可评审内容通知: user=%s record=%s reason=%s",
                open_id, record_id, reason,
            )

        return success

    async def notify_error_to_group(
        self,
        record_id: str,
        error: str,
        record_url: str = "",
    ) -> bool:
        """向告警群推送"评分异常"卡片（AI 系统性失败）。

        发往 config.notification_group_chat_id（receive_id_type=chat_id）；
        未配置群则跳过。带记录级冷却，避免同一记录反复失败时群内刷屏。
        """
        chat_id = self._config.notification_group_chat_id
        if not chat_id:
            return False

        # 群告警冷却：同一记录复用用户冷却时长
        now = time.time()
        cooldown_seconds = self._config.notification_cooldown_minutes * 60
        last_time = self._group_last_notify.get(record_id, 0)
        if now - last_time < cooldown_seconds:
            logger.info(
                "群告警冷却中: record=%s last=%.0fs ago", record_id, now - last_time
            )
            return False

        card = _build_error_card(record_id, error, record_url)
        success = await self._feishu.send_card_message(
            chat_id, card, receive_id_type="chat_id"
        )

        if success:
            self._group_last_notify[record_id] = now
            logger.info("发送评分异常群告警: record=%s", record_id)

        return success


# ---- 卡片模板 ----

def _build_failed_card(
    score: int,
    detail: str,
    threshold: int,
    record_url: str = "",
) -> dict[str, Any]:
    """构建"未通过"通知卡片。"""
    header_color = "red"
    elements: list[dict] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"您的提交 **未通过** AI 自动评审，请修改后重新提交。"
            },
        },
        {
            "tag": "hr",
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**当前评分**: {score} 分（通过线: {threshold} 分）\n"
                    f"**差距**: 差 {threshold - score} 分"
                ),
            },
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**改进建议**:\n{detail}"
            },
        },
    ]

    # 如果有记录链接，添加跳转按钮
    if record_url:
        elements.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "查看并修改"},
                    "url": record_url,
                    "type": "primary",
                }
            ],
        })

    return {
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "⚠️ AI 评审未通过",
            },
            "template": header_color,
        },
        "elements": elements,
    }


def _build_passed_card(score: int, threshold: int) -> dict[str, Any]:
    """构建"通过"通知卡片。"""
    return {
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "✅ AI 评审已通过",
            },
            "template": "green",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"恭喜！您的提交已通过 AI 自动评审。\n"
                        f"**评分**: {score} 分（通过线: {threshold} 分）"
                    ),
                },
            },
        ],
    }


def _build_rejected_card(
    score: int,
    detail: str,
    rounds: int,
) -> dict[str, Any]:
    """构建"已驳回"通知卡片。"""
    return {
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "🚫 提交已被驳回",
            },
            "template": "red",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"您的提交经过 **{rounds} 次** 修改仍未通过评审，已被驳回。\n\n"
                        f"**最终评分**: {score} 分\n"
                        f"**评审意见**: {detail}\n\n"
                        f"请联系管理员获取进一步帮助。"
                    ),
                },
            },
        ],
    }


def _build_admin_rejected_card(
    user_open_id: str,
    record_id: str,
    score: int,
    detail: str,
    rounds: int,
) -> dict[str, Any]:
    """构建管理员"驳回通知"卡片。"""
    return {
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "🔔 记录被驳回需人工介入",
            },
            "template": "yellow",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"有提交已超过最大修改轮次：\n"
                        f"**用户**: {user_open_id}\n"
                        f"**记录 ID**: {record_id}\n"
                        f"**修改轮次**: {rounds}\n"
                        f"**最终评分**: {score} 分\n"
                        f"**评审意见**: {detail}"
                    ),
                },
            },
        ],
    }


# 允许的附件格式（用于提示用户）
_ALLOWED_FORMATS_HINT = "docx / doc、md、pdf、图片（png/jpg/jpeg/webp）"


def _build_format_unsupported_card(unsupported_files: list[str]) -> dict[str, Any]:
    """构建"附件格式不符"通知卡片。"""
    files_text = "、".join(unsupported_files) if unsupported_files else "（未知文件）"
    return {
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "⚠️ 附件格式不符，已跳过评审",
            },
            "template": "red",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"您的提交包含 **不支持的附件格式**，已跳过自动评审。\n\n"
                        f"**不支持的文件**: {files_text}\n"
                        f"**允许的格式**: {_ALLOWED_FORMATS_HINT}\n\n"
                        f"请替换为受支持的格式后重新提交。"
                    ),
                },
            },
        ],
    }


def _build_unprocessable_card(reason: str) -> dict[str, Any]:
    """构建"无可评审内容"通知卡片。"""
    return {
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "⚠️ 暂无可评审内容",
            },
            "template": "red",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"您的提交暂时 **无法进行自动评审**。\n\n"
                        f"**原因**: {reason}\n\n"
                        f"请补充文本描述或受支持的文档后重新提交。"
                    ),
                },
            },
        ],
    }


def _build_error_card(
    record_id: str,
    error: str,
    record_url: str = "",
) -> dict[str, Any]:
    """构建"评分异常"群告警卡片（AI 系统性失败，需人工介入）。"""
    elements: list[dict] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"一条记录 AI 自动评分 **失败**，已置为「评分异常」终止态，"
                    f"需人工介入。\n\n"
                    f"**记录 ID**: {record_id}\n"
                    f"**错误信息**: {error}"
                ),
            },
        },
    ]

    if record_url:
        elements.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "查看记录"},
                    "url": record_url,
                    "type": "primary",
                }
            ],
        })

    return {
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "🛑 评分异常告警",
            },
            "template": "red",
        },
        "elements": elements,
    }
