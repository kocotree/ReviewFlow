"""发送侧熔断器。

只限制同一记录在短时间内产生的卡片发送，防止代码回声循环把机器人打爆；
不参与评分任务准入，也不限制用户提交或重评。
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Lock
from typing import Callable


@dataclass(frozen=True)
class SendPermit:
    """一次发送预留。

    ``reservation_id`` 非空表示本次发送已占用窗口额度；发送失败时调用方应回滚。
    ``should_alert`` 只在一次熔断周期首次拒绝时为真，避免告警本身刷屏。
    """

    allowed: bool
    reservation_id: str | None = None
    should_alert: bool = False
    count: int = 0


class SendCircuitBreaker:
    """按记录统计卡片发送次数的滑动窗口熔断器。"""

    def __init__(
        self,
        *,
        window_seconds: float,
        max_messages: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds 必须大于 0")
        if max_messages <= 0:
            raise ValueError("max_messages 必须大于 0")
        self._window_seconds = float(window_seconds)
        self._max_messages = int(max_messages)
        self._clock = clock
        self._events: dict[str, deque[tuple[float, str]]] = defaultdict(deque)
        self._tripped: set[str] = set()
        self._lock = Lock()

    @property
    def window_seconds(self) -> float:
        return self._window_seconds

    @property
    def max_messages(self) -> int:
        return self._max_messages

    def acquire(self, record_key: str) -> SendPermit:
        """为一次卡片发送预留额度；超过窗口上限时拒绝。"""
        now = self._clock()
        with self._lock:
            events = self._events[record_key]
            self._prune(record_key, events, now)

            if len(events) >= self._max_messages:
                should_alert = record_key not in self._tripped
                self._tripped.add(record_key)
                return SendPermit(
                    allowed=False,
                    should_alert=should_alert,
                    count=len(events),
                )

            reservation_id = uuid.uuid4().hex
            events.append((now, reservation_id))
            return SendPermit(
                allowed=True,
                reservation_id=reservation_id,
                count=len(events),
            )

    def rollback(self, record_key: str, reservation_id: str | None) -> None:
        """发送失败时释放预留，失败请求不计入实际发卡次数。"""
        if not reservation_id:
            return
        with self._lock:
            events = self._events.get(record_key)
            if not events:
                return
            kept = deque(item for item in events if item[1] != reservation_id)
            if kept:
                self._events[record_key] = kept
            else:
                self._events.pop(record_key, None)
                self._tripped.discard(record_key)

    def _prune(
        self,
        record_key: str,
        events: deque[tuple[float, str]],
        now: float,
    ) -> None:
        cutoff = now - self._window_seconds
        while events and events[0][0] <= cutoff:
            events.popleft()
        if not events:
            self._tripped.discard(record_key)
