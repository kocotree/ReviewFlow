"""FastAPI Webhook 服务 —— 接收飞书多维表格事件并触发评分。

启动方式::

    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response
from lark_oapi.core.model import RawRequest, RawResponse
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

from app.config import get_config
from app.orchestrator import Orchestrator

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---- 全局编排器 ----
_orchestrator: Orchestrator | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理。"""
    global _orchestrator
    cfg = get_config()
    _orchestrator = Orchestrator(config=cfg)
    logger.info(
        "RPA Score 服务已启动, provider=%s model=%s threshold=%d",
        cfg.ai_provider, cfg.ai_model, cfg.score_threshold,
    )
    yield
    if _orchestrator:
        await _orchestrator.close()
    logger.info("RPA Score 服务已关闭")


app = FastAPI(
    title="RPA Score - 飞书多维表格 AI 评分服务",
    version="1.0.0",
    lifespan=lifespan,
)


# ---- 事件去重 ----
class EventDeduplicator:
    """基于 event_id 的去重器，默认窗口 5 分钟。"""

    def __init__(self, window_seconds: int = 300) -> None:
        self._window = window_seconds
        self._cache: dict[str, float] = {}

    def is_duplicate(self, event_id: str) -> bool:
        now = time.time()
        self._cache = {
            k: v
            for k, v in self._cache.items()
            if now - v < self._window
        }
        if event_id in self._cache:
            return True
        self._cache[event_id] = now
        return False


_deduplicator = EventDeduplicator()


# ---- 飞书事件分发器 ----
def _build_event_handler() -> EventDispatcherHandler:
    """构建飞书事件分发处理器。"""
    cfg = get_config()

    def on_record_changed(data: Any) -> None:
        """多维表格记录变更回调。"""
        event = data.event
        app_token = event.file_token
        table_id = event.table_id

        # 去重
        event_id = getattr(data.header, "event_id", "")
        if event_id and _deduplicator.is_duplicate(event_id):
            logger.debug("重复事件跳过: %s", event_id)
            return

        # action_list 包含变更的记录列表，遍历处理每条记录
        action_list = getattr(event, "action_list", []) or []
        for action in action_list:
            record_id = getattr(action, "record_id", None)
            action_type = getattr(action, "action", "") or ""

            if not record_id:
                continue
            # 只处理新增和编辑，跳过删除
            if action_type == "record_deleted":
                logger.info("记录已删除，跳过: record=%s", record_id)
                continue

            logger.info(
                "记录变更: app=%s table=%s record=%s action=%s event_id=%s",
                app_token, table_id, record_id, action_type, event_id,
            )

            # 异步处理
            async def safe_process(rid: str = record_id) -> None:
                if _orchestrator:
                    await _orchestrator.process_record(app_token, table_id, rid)

            asyncio.create_task(safe_process())

    return EventDispatcherHandler.builder(
        encrypt_key=cfg.webhook_encrypt_key,
        verification_token=cfg.webhook_verification_token,
    ).register_p2_drive_file_bitable_record_changed_v1(
        on_record_changed
    ).build()


_event_handler = _build_event_handler()


# ---- 路由 ----

@app.get("/")
async def health_check():
    """健康检查。"""
    cfg = get_config()
    return {
        "status": "ok",
        "service": "rpa-score",
        "ai_provider": cfg.ai_provider,
        "score_threshold": cfg.score_threshold,
    }


@app.post("/webhook/event")
async def webhook_event(request: Request):
    """飞书事件订阅回调 URL。

    由 EventDispatcherHandler 自动处理：
    - URL 验证（challenge）：解密后返回 {"challenge": "..."}
    - 加密事件：解密 → 签名校验 → 分发到对应处理器
    """
    body = await request.body()
    # FastAPI 的 request.headers 转 dict 后 key 变为小写，但飞书 SDK
    # 期望原始大小写格式。需要手动恢复常见 header 的大小写。
    raw_headers = dict(request.headers)
    headers: dict[str, str] = {}
    for k, v in raw_headers.items():
        # 恢复飞书签名相关 header 的标准大小写
        kl = k.lower()
        if kl == "x-lark-request-timestamp":
            headers["X-Lark-Request-Timestamp"] = v
        elif kl == "x-lark-request-nonce":
            headers["X-Lark-Request-Nonce"] = v
        elif kl == "x-lark-signature":
            headers["X-Lark-Signature"] = v
        else:
            headers[k] = v

    # 构造 RawRequest 给 SDK 的 EventDispatcherHandler
    raw_req = RawRequest()
    raw_req.body = body
    raw_req.headers = headers
    raw_req.uri = str(request.url)

    # 交由 SDK 处理（解密、验证、分发、challenge 响应）
    raw_resp: RawResponse = _event_handler.do(raw_req)

    return Response(
        content=raw_resp.content,
        status_code=raw_resp.status_code,
        media_type="application/json; charset=utf-8",
    )


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
