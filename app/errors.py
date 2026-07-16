"""ReviewFlow 的类型化错误与失败分类。

外部网关必须保留错误语义，供编排器决定是自动重试、提示用户修复材料，
还是进入管理员处理队列。这里不包含具体的状态机逻辑。
"""

from __future__ import annotations

from enum import Enum


class ErrorCategory(str, Enum):
    """错误对工作流的处理类别。"""

    TRANSIENT = "瞬时技术失败"
    USER_FIXABLE = "用户可修复的材料失败"
    SYSTEM_HARD_FAILURE = "系统硬失败"


class ReviewFlowError(Exception):
    """带有工作流分类的基础异常。"""

    category = ErrorCategory.SYSTEM_HARD_FAILURE

    @property
    def retryable(self) -> bool:
        return self.category is ErrorCategory.TRANSIENT

    @property
    def user_fixable(self) -> bool:
        return self.category is ErrorCategory.USER_FIXABLE


class FeishuError(ReviewFlowError):
    """飞书网关异常基类，保存诊断所需的 API 上下文。"""

    def __init__(
        self,
        message: str,
        *,
        operation: str,
        code: int | str | None = None,
        status_code: int | None = None,
        resource_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.code = code
        self.status_code = status_code
        self.resource_id = resource_id


class FeishuPermissionError(FeishuError):
    """飞书拒绝访问；具体分类由子类区分应用权限和材料权限。"""


class FeishuDocumentPermissionError(FeishuPermissionError):
    """当前提交的文档或附件未授权给应用，用户可调整共享权限。"""

    category = ErrorCategory.USER_FIXABLE


class FeishuAppPermissionError(FeishuPermissionError):
    """应用缺少 API scope 等平台级权限，需要管理员修复。"""

    category = ErrorCategory.SYSTEM_HARD_FAILURE


class FeishuRateLimitError(FeishuError):
    """飞书限流，适合退避后自动重试。"""

    category = ErrorCategory.TRANSIENT


class FeishuTimeoutError(FeishuError):
    """请求或异步导出超时，适合自动重试。"""

    category = ErrorCategory.TRANSIENT


class FeishuNotFoundError(FeishuError):
    """用户提交的飞书资源不存在、失效或已删除。"""

    category = ErrorCategory.USER_FIXABLE


class FeishuTemporaryServiceError(FeishuError):
    """飞书 5xx、网络不可达或临时繁忙。"""

    category = ErrorCategory.TRANSIENT


class FeishuMaterialError(FeishuError):
    """材料本身不受支持、损坏，或导出任务明确拒绝该材料。"""

    category = ErrorCategory.USER_FIXABLE


class FeishuAppConfigError(FeishuError):
    """应用凭证、token 或网关配置错误。"""

    category = ErrorCategory.SYSTEM_HARD_FAILURE


class FeishuProtocolError(FeishuError):
    """飞书返回成功标记但响应结构不完整等不可恢复协议错误。"""

    category = ErrorCategory.SYSTEM_HARD_FAILURE
