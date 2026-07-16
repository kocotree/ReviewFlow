from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from app.send_circuit_breaker import SendCircuitBreaker
from tests.fakes import FakeClock


def test_blocks_n_plus_one_and_alerts_once_per_window() -> None:
    clock = FakeClock()
    breaker = SendCircuitBreaker(
        window_seconds=300,
        max_messages=2,
        clock=clock.time,
    )

    assert breaker.acquire("record").allowed
    assert breaker.acquire("record").allowed
    first_block = breaker.acquire("record")
    second_block = breaker.acquire("record")

    assert not first_block.allowed
    assert first_block.should_alert
    assert not second_block.allowed
    assert not second_block.should_alert

    clock.advance(301)
    assert breaker.acquire("record").allowed


def test_failed_send_reservation_can_be_rolled_back() -> None:
    breaker = SendCircuitBreaker(window_seconds=300, max_messages=1)
    permit = breaker.acquire("record")

    breaker.rollback("record", permit.reservation_id)

    assert breaker.acquire("record").allowed


def test_concurrent_acquire_never_exceeds_limit() -> None:
    breaker = SendCircuitBreaker(window_seconds=300, max_messages=20)

    with ThreadPoolExecutor(max_workers=40) as pool:
        permits = list(pool.map(lambda _: breaker.acquire("record"), range(40)))

    assert sum(permit.allowed for permit in permits) == 20
    assert sum(
        (not permit.allowed) and permit.should_alert for permit in permits
    ) == 1
