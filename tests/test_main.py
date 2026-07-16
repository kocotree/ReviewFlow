from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.background_tasks import BackgroundTaskSupervisor
from app.main import AppRuntime, EventDeduplicator, _enqueue_record_changes, create_app
from app.record_coordinator import RecordCoordinator, RequestStatus, ScoreRequestResult
from app.task_registry import RecordKey, TaskRegistry
from app.workflow_state import TriggerSource
from tests.fakes import FakeAIClient, FakeClock, FakeFeishuClient


class StubCoordinator:
    def __init__(self) -> None:
        self.calls = []
        self.release = asyncio.Event()

    async def request_score(self, **kwargs):
        self.calls.append(kwargs)
        await self.release.wait()
        return ScoreRequestResult(RequestStatus.ACCEPTED)


class StubOrchestrator:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def runtime(config, clock: FakeClock | None = None) -> AppRuntime:
    fake_clock = clock or FakeClock()
    feishu = FakeFeishuClient()
    ai = FakeAIClient()
    registry = TaskRegistry(clock=fake_clock.monotonic)
    coordinator = StubCoordinator()
    return AppRuntime(
        config=config,
        feishu=feishu,
        ai=ai,
        orchestrator=StubOrchestrator(),
        registry=registry,
        coordinator=coordinator,
        supervisor=BackgroundTaskSupervisor(),
        event_deduplicator=EventDeduplicator(clock=fake_clock.monotonic),
    )


def event_data(*actions, event_id: str = "evt_1"):
    return SimpleNamespace(
        header=SimpleNamespace(event_id=event_id),
        event=SimpleNamespace(
            file_token="app",
            table_id="table",
            action_list=list(actions),
        ),
    )


def action(record_id: str, kind: str = "record_edited"):
    return SimpleNamespace(record_id=record_id, action=kind)


def test_create_app_does_not_read_configuration_at_import_time() -> None:
    called = False

    def config_factory():
        nonlocal called
        called = True
        raise AssertionError("仅 lifespan 才可读取配置")

    application = create_app(config_factory=config_factory)

    assert application is not None
    assert not called


def test_default_runtime_rejects_non_file_model_before_clients(monkeypatch, config) -> None:
    import app.main as main_module

    config.ai_model = "doubao-text-only"
    monkeypatch.setattr(
        main_module,
        "FeishuClient",
        lambda _: pytest.fail("能力校验应早于飞书客户端创建"),
    )

    with pytest.raises(ValueError, match="不具备 PDF"):
        main_module.build_default_runtime(config)


def test_default_runtime_rejects_missing_libreoffice(monkeypatch, config) -> None:
    import app.main as main_module

    monkeypatch.setattr(main_module, "soffice_available", lambda: False)
    monkeypatch.setattr(
        main_module,
        "FeishuClient",
        lambda _: pytest.fail("自检应早于飞书客户端创建"),
    )

    with pytest.raises(RuntimeError, match="LibreOffice"):
        main_module.build_default_runtime(config)


@pytest.mark.asyncio
async def test_record_event_is_deduplicated_and_delete_is_skipped(config) -> None:
    app_runtime = runtime(config)
    coordinator = app_runtime.coordinator
    data = event_data(action("record"), action("deleted", "record_deleted"))

    _enqueue_record_changes(data, app_runtime)
    _enqueue_record_changes(data, app_runtime)
    await asyncio.sleep(0)

    assert len(coordinator.calls) == 1
    assert coordinator.calls[0]["key"].record_id == "record"
    coordinator.release.set()
    assert await app_runtime.supervisor.drain(1)


@pytest.mark.asyncio
async def test_shutdown_waits_for_admission_then_closes_clients(config) -> None:
    app_runtime = runtime(config)
    coordinator = app_runtime.coordinator
    _enqueue_record_changes(event_data(action("record")), app_runtime)
    await asyncio.sleep(0)

    shutdown = asyncio.create_task(app_runtime.shutdown())
    await asyncio.sleep(0)
    assert not shutdown.done()
    assert not app_runtime.registry.accepting
    assert not app_runtime.orchestrator.closed

    coordinator.release.set()
    await shutdown

    assert app_runtime.orchestrator.closed
    assert app_runtime.ai.closed
    assert app_runtime.feishu.closed


@pytest.mark.asyncio
async def test_failed_record_edits_never_submit_automatic_score(config, record_factory) -> None:
    app_runtime = runtime(config)
    feishu = app_runtime.feishu
    feishu.records[("app", "table", "record")] = record_factory(status="未通过")
    runs = 0

    async def runner(command) -> None:
        nonlocal runs
        runs += 1

    app_runtime.coordinator = RecordCoordinator(
        feishu=feishu,
        registry=app_runtime.registry,
        runner=runner,
    )

    _enqueue_record_changes(
        event_data(action("record"), event_id="evt-edit-1"), app_runtime
    )
    _enqueue_record_changes(
        event_data(action("record"), event_id="evt-edit-2"), app_runtime
    )
    assert await app_runtime.supervisor.drain(1)

    assert runs == 0
    assert app_runtime.registry.history == ()


@pytest.mark.asyncio
async def test_two_initial_events_create_only_one_scoring_task(config, record_factory) -> None:
    app_runtime = runtime(config)
    feishu = app_runtime.feishu
    feishu.records[("app", "table", "record")] = record_factory(status="待评分")
    started = asyncio.Event()
    release = asyncio.Event()
    runs = 0

    async def runner(command) -> None:
        nonlocal runs
        runs += 1
        started.set()
        await release.wait()

    app_runtime.coordinator = RecordCoordinator(
        feishu=feishu,
        registry=app_runtime.registry,
        runner=runner,
    )
    _enqueue_record_changes(
        event_data(action("record"), event_id="evt-create-1"), app_runtime
    )
    _enqueue_record_changes(
        event_data(action("record"), event_id="evt-create-2"), app_runtime
    )

    await started.wait()
    await asyncio.sleep(0)
    assert runs == 1
    assert app_runtime.registry.active_count == 1

    release.set()
    assert await app_runtime.supervisor.drain(1)
    assert await app_runtime.registry.drain(1)


@pytest.mark.asyncio
async def test_shutdown_waits_for_running_score_before_closing(config) -> None:
    app_runtime = runtime(config)
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(command) -> None:
        started.set()
        await release.wait()

    accepted = app_runtime.registry.submit(
        key=RecordKey("app", "table", "record"),
        source=TriggerSource.INITIAL_EVENT,
        runner=runner,
    )
    assert accepted.accepted
    await started.wait()

    shutdown = asyncio.create_task(app_runtime.shutdown())
    await asyncio.sleep(0)
    assert not shutdown.done()
    assert not app_runtime.orchestrator.closed
    assert not app_runtime.ai.closed

    release.set()
    await shutdown
    assert app_runtime.orchestrator.closed
    assert app_runtime.ai.closed
    assert app_runtime.feishu.closed


def test_event_deduplicator_recovers_after_window(clock) -> None:
    deduplicator = EventDeduplicator(window_seconds=300, clock=clock.monotonic)

    assert deduplicator.claim("evt")
    assert not deduplicator.claim("evt")
    clock.advance(301)
    assert deduplicator.claim("evt")


def test_card_action_route_reuses_lark_verification_and_returns_feedback(config) -> None:
    app_runtime = runtime(config)
    app_runtime.coordinator.release.set()
    application = create_app(
        config_factory=lambda: config,
        runtime_factory=lambda _: app_runtime,
    )
    value = {
        "action": "rescore",
        "app_token": "app",
        "table_id": "table",
        "record_id": "record",
    }
    body = json.dumps(
        {
            "open_id": "ou_submitter",
            "open_message_id": "om_1",
            "open_chat_id": "",
            "action": {"value": value},
        },
        separators=(",", ":"),
    ).encode()
    timestamp = "1700000000"
    nonce = "nonce"
    signature = hashlib.sha1(
        (timestamp + nonce + config.webhook_verification_token).encode() + body
    ).hexdigest()

    with TestClient(application) as client:
        response = client.post(
            "/webhook/card-action",
            content=body,
            headers={
                "X-Lark-Request-Timestamp": timestamp,
                "X-Lark-Request-Nonce": nonce,
                "X-Lark-Signature": signature,
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 200
    assert response.json()["toast"]["content"] == "已提交重新评分"
    assert len(app_runtime.coordinator.calls) == 1
