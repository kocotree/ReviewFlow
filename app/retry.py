"""只包裹单步外部操作的技术重试。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.errors import ReviewFlowError

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


async def retry_step(
    operation: str,
    call: Callable[[], Awaitable[_T]],
    *,
    max_attempts: int = 3,
    base_delay_seconds: float = 0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> _T:
    """重试可重试异常；已成功步骤的结果由调用方持有并复用。"""
    if max_attempts < 1:
        raise ValueError("max_attempts 必须大于 0")
    for attempt in range(1, max_attempts + 1):
        try:
            return await call()
        except ReviewFlowError as exc:
            if not exc.retryable or attempt >= max_attempts:
                raise
            delay = base_delay_seconds * (2 ** (attempt - 1))
            logger.warning(
                "单步操作失败，准备重试: operation=%s attempt=%d/%d error=%s",
                operation,
                attempt,
                max_attempts,
                exc,
            )
            if delay > 0:
                await sleep(delay)
    raise AssertionError("unreachable")
