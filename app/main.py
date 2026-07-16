"""FastAPI Webhook 服务：事件准入、卡片动作与任务生命周期。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from lark_oapi.card.action_handler import CardActionHandler
from lark_oapi.card.model import Card
from lark_oapi.core.model import RawRequest, RawResponse
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

from app.ai import AIClient, doubao_supports_file
from app.background_tasks import BackgroundTaskSupervisor
from app.card_action import CardActionService, DecodedCardAction
from app.config import Config, get_config
from app.content_collector import CollectionLimits, ContentCollector
from app.docx_convert import soffice_available
from app.feishu import FeishuClient
from app.notification import NotificationManager
from app.orchestrator import Orchestrator
from app.record_coordinator import RecordCoordinator
from app.scavenger import ScoringScavenger
from app.scoring_workflow import ScoringWorkflow
from app.task_registry import RecordKey, TaskRegistry
from app.workflow_state import TriggerSource

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class _HealthCheckLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 5:
            method, path, status = args[1], args[2], args[4]
            try:
                ok = int(status) < 400
            except (TypeError, ValueError):
                ok = False
            if method == "GET" and path == "/" and ok:
                return False
        return True


logging.getLogger("uvicorn.access").addFilter(_HealthCheckLogFilter())


class EventDeduplicator:
    """只有成功接纳 webhook 后才保留 event_id 的滑动窗口幂等缓存。"""

    def __init__(
        self,
        window_seconds: int = 300,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = window_seconds
        self._clock = clock
        self._cache: dict[str, float] = {}

    def claim(self, event_id: str) -> bool:
        now = self._clock()
        cutoff = now - self._window
        self._cache = {key: value for key, value in self._cache.items() if value > cutoff}
        if event_id in self._cache:
            return False
        self._cache[event_id] = now
        return True

    def release(self, event_id: str) -> None:
        self._cache.pop(event_id, None)

    def is_duplicate(self, event_id: str) -> bool:
        """兼容旧调用；首次返回 False，窗口内后续调用返回 True。"""
        return not self.claim(event_id)


@dataclass
class AppRuntime:
    config: Config
    feishu: FeishuClient
    ai: AIClient
    orchestrator: Orchestrator
    registry: TaskRegistry
    coordinator: RecordCoordinator
    supervisor: BackgroundTaskSupervisor
    event_deduplicator: EventDeduplicator
    event_handler: EventDispatcherHandler | None = None
    card_handler: CardActionHandler | None = None
    card_service: CardActionService | None = None
    scavenger: ScoringScavenger | None = None
    scavenger_stop: asyncio.Event | None = None

    async def shutdown(self) -> None:
        timeout = float(getattr(self.config, "shutdown_timeout_seconds", 30))
        if self.scavenger_stop is not None:
            self.scavenger_stop.set()
        self.supervisor.stop_accepting()
        self.registry.stop_accepting()
        await self.supervisor.drain(timeout)
        await self.registry.drain(timeout)
        try:
            await self.orchestrator.close()
        finally:
            try:
                await self.feishu.close()
            finally:
                await self.ai.close()


RuntimeFactory = Callable[[Config], AppRuntime]


def build_default_runtime(config: Config) -> AppRuntime:
    if not doubao_supports_file(config.ai_model):
        raise ValueError(f"AI_MODEL 不具备 PDF/视觉文件能力: {config.ai_model}")
    if not soffice_available():
        raise RuntimeError("LibreOffice(soffice) 不可用，无法构建总 PDF")
    feishu = FeishuClient(config)
    ai = AIClient(config)
    registry = TaskRegistry()
    supervisor = BackgroundTaskSupervisor()
    notifier = NotificationManager(config, feishu)
    collector = ContentCollector(
        feishu=feishu,
        supports_pdf_input=lambda: ai.supports_pdf_input,
        limits=CollectionLimits(
            max_attachment_count=config.max_attachment_count,
            max_single_attachment_bytes=config.max_single_attachment_mb
            * 1024
            * 1024,
            max_total_attachment_bytes=config.max_total_attachment_mb * 1024 * 1024,
            max_pdf_pages=config.max_pdf_pages,
            max_image_count=config.max_image_count,
        ),
    )
    workflow = ScoringWorkflow(
        config=config,
        feishu=feishu,
        ai=ai,
        collector=collector,
        notifier=notifier,
        registry=registry,
    )
    orchestrator = Orchestrator(workflow=workflow, registry=registry)

    coordinator = RecordCoordinator(
        feishu=feishu,
        registry=registry,
        runner=orchestrator.process_command,
    )
    runtime = AppRuntime(
        config=config,
        feishu=feishu,
        ai=ai,
        orchestrator=orchestrator,
        registry=registry,
        coordinator=coordinator,
        supervisor=supervisor,
        event_deduplicator=EventDeduplicator(),
    )
    runtime.scavenger = ScoringScavenger(
        feishu=feishu,
        coordinator=coordinator,
        registry=registry,
        app_token=config.bitable_app_token,
        table_id=config.bitable_table_id,
        orphan_timeout_seconds=config.scoring_orphan_timeout_seconds,
        interval_seconds=config.scavenger_interval_seconds,
    )
    return runtime


def _enqueue_record_changes(data: Any, runtime: AppRuntime) -> None:
    event = data.event
    app_token = event.file_token
    table_id = event.table_id
    event_id = str(getattr(data.header, "event_id", "") or "")

    if event_id and not runtime.event_deduplicator.claim(event_id):
        logger.debug("重复事件跳过: %s", event_id)
        return

    accepted_any = False
    action_list = getattr(event, "action_list", []) or []
    for action in action_list:
        record_id = getattr(action, "record_id", None)
        action_type = getattr(action, "action", "") or ""
        if not record_id:
            continue
        if action_type == "record_deleted":
            logger.info("记录已删除，跳过: record=%s", record_id)
            continue

        key = RecordKey(app_token, table_id, record_id)
        logger.info(
            "记录事件已接收: key=%s action=%s event_id=%s",
            key,
            action_type,
            event_id,
        )
        accepted = runtime.supervisor.spawn(
            runtime.coordinator.request_score(
                key=key,
                source=TriggerSource.INITIAL_EVENT,
            ),
            name=f"event-admission:{key}",
        )
        accepted_any = accepted_any or accepted

    if event_id and action_list and not accepted_any:
        # 关闭期间未能成功接纳时释放 event_id，避免把未入队事件误记成完成。
        # 纯删除事件属于正常完成，不需要重投。
        contains_processable = any(
            getattr(action, "record_id", None)
            and (getattr(action, "action", "") or "") != "record_deleted"
            for action in action_list
        )
        if contains_processable:
            runtime.event_deduplicator.release(event_id)


def _build_event_handler(config: Config, runtime: AppRuntime) -> EventDispatcherHandler:
    def on_record_changed(data: Any) -> None:
        _enqueue_record_changes(data, runtime)

    builder = EventDispatcherHandler.builder(
        encrypt_key=config.webhook_encrypt_key,
        verification_token=config.webhook_verification_token,
    ).register_p2_drive_file_bitable_record_changed_v1(on_record_changed)

    # 某些 lark-oapi 版本额外暴露补发事件注册方法；存在时使用同一处理器。
    registration = getattr(
        builder,
        "registration_p2_drive_file_bitable_record_changed_v1",
        None,
    )
    if callable(registration):
        builder = registration(on_record_changed)
    return builder.build()


def _build_card_handler(config: Config) -> CardActionHandler:
    def decode(card: Card) -> dict[str, Any]:
        return {
            "_decoded_card_action": True,
            "actor_open_id": card.open_id or "",
            "open_message_id": card.open_message_id or "",
            "open_chat_id": card.open_chat_id or "",
            "action_value": card.action.value if card.action else {},
        }

    return CardActionHandler.builder(
        encrypt_key=config.webhook_encrypt_key,
        verification_token=config.webhook_verification_token,
    ).register(decode).build()


def _raw_request(request: Request, body: bytes) -> RawRequest:
    raw_headers = dict(request.headers)
    headers: dict[str, str] = {}
    canonical = {
        "x-lark-request-timestamp": "X-Lark-Request-Timestamp",
        "x-lark-request-nonce": "X-Lark-Request-Nonce",
        "x-lark-signature": "X-Lark-Signature",
    }
    for key, value in raw_headers.items():
        headers[canonical.get(key.lower(), key)] = value
    raw_req = RawRequest()
    raw_req.body = body
    raw_req.headers = headers
    raw_req.uri = str(request.url)
    return raw_req


def create_app(
    *,
    config_factory: Callable[[], Config] = get_config,
    runtime_factory: RuntimeFactory = build_default_runtime,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = config_factory()
        runtime = runtime_factory(config)
        runtime.event_handler = _build_event_handler(config, runtime)
        runtime.card_handler = _build_card_handler(config)
        runtime.card_service = CardActionService(
            coordinator=runtime.coordinator,
            admin_chat_id=config.notification_group_chat_id,
        )
        if runtime.scavenger is not None:
            runtime.scavenger_stop = asyncio.Event()
            runtime.supervisor.spawn(
                runtime.scavenger.run(runtime.scavenger_stop),
                name="scoring-scavenger",
            )
        app.state.runtime = runtime
        logger.info(
            "ReviewFlow 服务已启动, provider=%s model=%s threshold=%d",
            config.ai_provider,
            config.ai_model,
            config.score_threshold,
        )
        try:
            yield
        finally:
            await runtime.shutdown()
            logger.info("ReviewFlow 服务已关闭")

    application = FastAPI(
        title="ReviewFlow - 飞书多维表格 AI 评分服务",
        version="2.0.0",
        lifespan=lifespan,
    )

    @application.get("/")
    async def health_check(request: Request) -> dict[str, Any]:
        runtime: AppRuntime = request.app.state.runtime
        return {
            "status": "ok",
            "service": "reviewflow",
            "ai_provider": runtime.config.ai_provider,
            "score_threshold": runtime.config.score_threshold,
            "accepting_tasks": runtime.registry.accepting,
        }

    @application.post("/webhook/event")
    async def webhook_event(request: Request) -> Response:
        runtime: AppRuntime = request.app.state.runtime
        if not runtime.supervisor.accepting or runtime.event_handler is None:
            return JSONResponse({"message": "service shutting down"}, status_code=503)
        body = await request.body()
        raw_resp: RawResponse = runtime.event_handler.do(_raw_request(request, body))
        return Response(
            content=raw_resp.content,
            status_code=raw_resp.status_code,
            media_type="application/json; charset=utf-8",
        )

    @application.post("/webhook/card-action")
    async def card_action(request: Request) -> Response:
        runtime: AppRuntime = request.app.state.runtime
        if runtime.card_handler is None or runtime.card_service is None:
            return JSONResponse({"message": "service unavailable"}, status_code=503)
        body = await request.body()
        raw_resp: RawResponse = await asyncio.to_thread(
            runtime.card_handler.do,
            _raw_request(request, body),
        )
        if raw_resp.status_code != 200:
            return Response(
                content=raw_resp.content,
                status_code=raw_resp.status_code,
                media_type="application/json; charset=utf-8",
            )

        try:
            decoded_payload = json.loads(raw_resp.content or b"{}")
        except (TypeError, json.JSONDecodeError):
            return JSONResponse({"message": "invalid card callback"}, status_code=400)

        # URL verification challenge 已由 SDK 完成，直接透传。
        if not decoded_payload.get("_decoded_card_action"):
            return Response(
                content=raw_resp.content,
                status_code=200,
                media_type="application/json; charset=utf-8",
            )

        decoded = DecodedCardAction(
            actor_open_id=str(decoded_payload.get("actor_open_id", "") or ""),
            open_message_id=str(decoded_payload.get("open_message_id", "") or ""),
            open_chat_id=str(decoded_payload.get("open_chat_id", "") or ""),
            action_value=decoded_payload.get("action_value", {}) or {},
        )
        feedback = await runtime.card_service.handle(decoded)
        # 已完成验签的业务拒绝也以 200 回执，避免飞书按 5xx/4xx重复投递。
        return JSONResponse(feedback.as_lark_payload(), status_code=200)

    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    cfg = get_config()
    uvicorn.run(
        "app.main:app",
        host=cfg.host,
        port=cfg.port,
        log_level=cfg.log_level.lower(),
        reload=True,
    )
