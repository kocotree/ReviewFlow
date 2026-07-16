"""评分工作流状态与触发来源。"""

from __future__ import annotations

from enum import StrEnum


class ScoreStatus(StrEnum):
    PENDING = "待评分"
    SCORING = "评分中"
    PASSED = "已通过"
    FAILED = "未通过"
    REJECTED = "已驳回"
    ERROR = "评分异常"


class TriggerSource(StrEnum):
    INITIAL_EVENT = "initial_event"
    USER_RESCORE = "user_rescore"
    ADMIN_RETRY = "admin_retry"
    SCAVENGER = "scavenger"


USER_RECOVERABLE_STATUSES = frozenset({ScoreStatus.FAILED})
ADMIN_RECOVERABLE_STATUSES = frozenset({ScoreStatus.ERROR})
