"""内容采集与工作流使用的业务错误。"""

from __future__ import annotations

from dataclasses import dataclass

from app.errors import ErrorCategory, ReviewFlowError


@dataclass(frozen=True, slots=True)
class MaterialProblem:
    name: str
    reason: str


class UserMaterialError(ReviewFlowError):
    category = ErrorCategory.USER_FIXABLE
    notification_kind = "material_error"

    def __init__(
        self,
        message: str,
        *,
        problems: tuple[MaterialProblem, ...] = (),
    ) -> None:
        super().__init__(message)
        self.problems = problems


class NoFileMaterialError(UserMaterialError):
    notification_kind = "no_file"


class UnsupportedMaterialError(UserMaterialError):
    notification_kind = "unsupported"


class DamagedMaterialError(UserMaterialError):
    notification_kind = "damaged"


class MaterialLimitError(UserMaterialError):
    notification_kind = "limit"


class ModelCapabilityError(ReviewFlowError):
    category = ErrorCategory.SYSTEM_HARD_FAILURE


class ContentProcessingError(ReviewFlowError):
    category = ErrorCategory.TRANSIENT


class TranscriptionError(ReviewFlowError):
    category = ErrorCategory.TRANSIENT


class StateWriteError(ReviewFlowError):
    category = ErrorCategory.TRANSIENT


class FinalWriteError(StateWriteError):
    """最终原子写回重试耗尽；保留评分中供清道夫恢复。"""
