"""飞书 Open API 网关。

``lark-oapi`` 的业务接口是同步实现。所有可能发起网络请求的 SDK 调用都经
``asyncio.to_thread`` 隔离，并由信号量限制并发，避免阻塞 FastAPI 事件循环。
网关失败统一抛出 :mod:`app.errors` 中的类型化异常，不再用 ``None`` / ``False``
丢失错误原因。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any, TypeVar

import httpx
import lark_oapi as lark
import requests
from lark_oapi.api.bitable.v1 import (
    AppTableRecord,
    GetAppTableRecordRequest,
    UpdateAppTableRecordRequest,
)
from lark_oapi.api.docx.v1 import RawContentDocumentRequest
from lark_oapi.api.drive.v1 import (
    CreateExportTaskRequest,
    DownloadExportTaskRequest,
    ExportTask,
    GetExportTaskRequest,
)
from lark_oapi.api.im.v1 import CreateMessageRequest
from lark_oapi.api.im.v1.model.create_message_request_body import (
    CreateMessageRequestBody,
)
from lark_oapi.api.wiki.v2 import GetNodeSpaceRequest
from lark_oapi.core.model import Config as LarkConfig
from lark_oapi.core.token import TokenManager

from app.config import Config
from app.errors import (
    FeishuAppConfigError,
    FeishuAppPermissionError,
    FeishuDocumentPermissionError,
    FeishuError,
    FeishuMaterialError,
    FeishuNotFoundError,
    FeishuProtocolError,
    FeishuRateLimitError,
    FeishuTemporaryServiceError,
    FeishuTimeoutError,
)

_T = TypeVar("_T")

# 导出任务轮询参数
_EXPORT_POLL_INTERVAL = 1.5
_EXPORT_POLL_MAX_TRIES = 20
_EXPORT_JOB_SUCCESS = 0

# 单进程默认并发上限。SDK 信号量限制同时占用默认线程池的飞书调用数量；
# 导出信号量限制完整的「创建 -> 轮询 -> 下载」工作流数量。
_DEFAULT_SDK_CONCURRENCY = 8
_DEFAULT_EXPORT_CONCURRENCY = 2
_DEFAULT_SDK_CLOSE_TIMEOUT = 30.0

_RATE_LIMIT_CODES = {429, 230020, 1254290, 99991429, 99991629}
_NOT_FOUND_CODES = {404, 1254043, 99991404, 1069914}
_PERMISSION_CODES = {403, 99991403, 99991672, 99991673}
_TEMPORARY_CODES = {
    1254291,  # Bitable write conflict
    1255001,
    1255002,
    1255003,
    1255004,
    1255005,
}
_EXPORT_TIMEOUT_STATUSES = {108}
_EXPORT_PERMISSION_STATUSES = {109, 110}
_EXPORT_NOT_FOUND_STATUSES = {111, 123}
_EXPORT_TEMPORARY_STATUSES = {3}
_EXPORT_TEMPORARY_API_CODES = {600}  # hybrid resource expired，稍后重试
_APP_CONFIG_CODES = {
    401,
    99991401,
    99991661,
    99991663,
    99991664,
    99991665,
}

_RATE_LIMIT_MARKERS = (
    "rate limit",
    "too many request",
    "toomanyrequest",
    "限流",
    "频率限制",
)
_TIMEOUT_MARKERS = ("timeout", "timed out", "超时")
_NOT_FOUND_MARKERS = (
    "not found",
    "recordidnotfound",
    "does not exist",
    "file token invalid",
    "不存在",
    "已删除",
    "链接失效",
)
_PERMISSION_MARKERS = (
    "permission",
    "forbidden",
    "access denied",
    "no access",
    "无权限",
    "权限不足",
    "拒绝访问",
)
_APP_SCOPE_MARKERS = (
    "scope",
    "tenant_access_token",
    "app_access_token",
    "app id",
    "app_id",
    "app secret",
    "app_secret",
    "应用权限",
    "权限范围",
    "授权范围",
)
_APP_CONFIG_MARKERS = (
    "invalid access token",
    "access token expired",
    "invalid app",
    "app not found",
    "invalid credential",
    "unauthorized",
    "凭证",
    "令牌失效",
    "令牌过期",
)
_TEMPORARY_MARKERS = (
    "internal server error",
    "internalerror",
    "rpcerror",
    "write conflict",
    "service unavailable",
    "bad gateway",
    "temporarily unavailable",
    "server busy",
    "系统繁忙",
    "服务不可用",
    "临时不可用",
)

logger = logging.getLogger(__name__)


def _contains_any(message: str, markers: tuple[str, ...]) -> bool:
    normalized = message.casefold()
    return any(marker in normalized for marker in markers)


def _response_status(response: Any) -> int | None:
    raw = getattr(response, "raw", None)
    status = getattr(raw, "status_code", None)
    return status if isinstance(status, int) else None


def _typed_error(
    *,
    operation: str,
    message: str,
    code: int | str | None = None,
    status_code: int | None = None,
    resource_id: str | None = None,
    document_level: bool = False,
) -> FeishuError:
    """把 SDK/HTTP 的错误细节映射为稳定的工作流错误类型。"""

    numeric_code = code if isinstance(code, int) else None
    display = message or "飞书接口返回未知错误"
    kwargs = {
        "operation": operation,
        "code": code,
        "status_code": status_code,
        "resource_id": resource_id,
    }

    if (
        status_code == 429
        or numeric_code in _RATE_LIMIT_CODES
        or _contains_any(display, _RATE_LIMIT_MARKERS)
    ):
        return FeishuRateLimitError(display, **kwargs)

    if (
        status_code in {408, 504}
        or (
            operation == "get_export_task"
            and numeric_code in _EXPORT_TIMEOUT_STATUSES
        )
        or _contains_any(display, _TIMEOUT_MARKERS)
    ):
        return FeishuTimeoutError(display, **kwargs)

    if (
        status_code == 401
        or numeric_code in _APP_CONFIG_CODES
        or _contains_any(display, _APP_CONFIG_MARKERS)
    ):
        return FeishuAppConfigError(display, **kwargs)

    if (
        status_code == 404
        or numeric_code in _NOT_FOUND_CODES
        or (
            operation == "get_export_task"
            and numeric_code in _EXPORT_NOT_FOUND_STATUSES
        )
        or _contains_any(display, _NOT_FOUND_MARKERS)
    ):
        return FeishuNotFoundError(display, **kwargs)

    if (
        status_code == 403
        or numeric_code in _PERMISSION_CODES
        or (
            operation == "get_export_task"
            and numeric_code in _EXPORT_PERMISSION_STATUSES
        )
        or _contains_any(display, _PERMISSION_MARKERS)
    ):
        if _contains_any(display, _APP_SCOPE_MARKERS) or not document_level:
            return FeishuAppPermissionError(display, **kwargs)
        return FeishuDocumentPermissionError(display, **kwargs)

    if (
        (status_code is not None and status_code >= 500)
        or (numeric_code is not None and 500 <= numeric_code < 600)
        or numeric_code in _TEMPORARY_CODES
        or (
            operation == "get_export_task"
            and numeric_code in _EXPORT_TEMPORARY_STATUSES
        )
        or (
            operation == "create_export_task"
            and numeric_code in _EXPORT_TEMPORARY_API_CODES
        )
        or _contains_any(display, _TEMPORARY_MARKERS)
    ):
        return FeishuTemporaryServiceError(display, **kwargs)

    # 已被飞书明确判为失败、但无已知错误码的文档操作，通常是链接、类型或
    # 材料本身的问题。SDK 自身抛出的未知异常没有 code/status，不走此分支。
    if document_level and (code is not None or status_code is not None):
        return FeishuMaterialError(display, **kwargs)
    return FeishuProtocolError(display, **kwargs)


class FeishuClient:
    """异步友好的飞书 API 客户端。

    ``sdk_client``、``http_client`` 与 ``tenant_token_provider`` 可注入，便于单元
    测试及在宿主应用中统一配置。注入的 HTTP client 也由本实例拥有，并在
    :meth:`close` 中集中关闭。
    """

    def __init__(
        self,
        config: Config,
        *,
        sdk_client: Any | None = None,
        http_client: httpx.AsyncClient | None = None,
        tenant_token_provider: Callable[[], str] | None = None,
        sdk_concurrency: int = _DEFAULT_SDK_CONCURRENCY,
        export_concurrency: int = _DEFAULT_EXPORT_CONCURRENCY,
        sdk_close_timeout: float = _DEFAULT_SDK_CLOSE_TIMEOUT,
    ) -> None:
        if sdk_concurrency < 1:
            raise ValueError("sdk_concurrency 必须大于 0")
        if export_concurrency < 1:
            raise ValueError("export_concurrency 必须大于 0")
        if sdk_close_timeout < 0:
            raise ValueError("sdk_close_timeout 不能小于 0")

        self._config = config
        self._client = sdk_client or (
            lark.Client.builder()
            .app_id(config.feishu_app_id)
            .app_secret(config.feishu_app_secret)
            .log_level(
                lark.LogLevel.DEBUG
                if config.log_level == "DEBUG"
                else lark.LogLevel.INFO
            )
            .build()
        )
        self._http = http_client or httpx.AsyncClient(timeout=30.0)
        self._sdk_slots = asyncio.Semaphore(sdk_concurrency)
        self._export_slots = asyncio.Semaphore(export_concurrency)
        self._sdk_tasks: set[asyncio.Task[Any]] = set()
        self._sdk_close_timeout = sdk_close_timeout
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._http_closed = False

        if tenant_token_provider is None:
            # TokenManager 需要 SDK Config。显式构造配置，避免读取
            # SDK client 的私有配置属性。
            token_config = LarkConfig()
            token_config.app_id = config.feishu_app_id
            token_config.app_secret = config.feishu_app_secret
            tenant_token_provider = lambda: TokenManager.get_self_tenant_token(
                token_config
            )
        self._tenant_token_provider = tenant_token_provider

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("FeishuClient 已关闭")

    async def _sdk_call(
        self,
        operation: str,
        call: Callable[[], _T],
        *,
        resource_id: str | None = None,
        document_level: bool = False,
    ) -> _T:
        """在线程中执行一个同步 SDK 调用，并保留异常类型。"""

        self._ensure_open()
        await self._sdk_slots.acquire()
        if self._closed:
            self._sdk_slots.release()
            raise RuntimeError("FeishuClient 已关闭")
        thread_task = asyncio.create_task(asyncio.to_thread(call))
        self._sdk_tasks.add(thread_task)

        def release_slot(completed: asyncio.Task[_T]) -> None:
            # permit 与真实线程任务绑定：即使等待它的协程被取消，也不能在阻塞
            # 调用仍运行时放出名额，否则会突破 SDK 并发上限。
            self._sdk_slots.release()
            self._sdk_tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()  # 消费被取消调用的后台异常

        thread_task.add_done_callback(release_slot)
        try:
            return await asyncio.shield(thread_task)
        except asyncio.CancelledError:
            # Python 无法中止已启动的工作线程；立即传播取消以免拖死任务 drain，
            # 但 permit 仍由 done callback 持有，close() 会有界等待底层线程。
            raise
        except FeishuError:
            raise
        except (requests.Timeout, httpx.TimeoutException, TimeoutError) as exc:
            raise FeishuTimeoutError(
                str(exc) or f"{operation} 超时",
                operation=operation,
                resource_id=resource_id,
            ) from exc
        except (
            requests.RequestException,
            httpx.TransportError,
            ConnectionError,
            OSError,
        ) as exc:
            raise FeishuTemporaryServiceError(
                str(exc) or f"{operation} 临时不可用",
                operation=operation,
                resource_id=resource_id,
            ) from exc
        except Exception as exc:
            code = getattr(exc, "code", None)
            status_code = getattr(exc, "status_code", None)
            message = str(getattr(exc, "msg", "") or exc)
            error = _typed_error(
                operation=operation,
                message=message,
                code=code,
                status_code=status_code,
                resource_id=resource_id,
                document_level=document_level,
            )
            if operation == "get_tenant_access_token" and not error.retryable:
                raise FeishuAppConfigError(
                    message or "获取飞书租户访问令牌失败",
                    operation=operation,
                    code=code,
                    status_code=status_code,
                    resource_id=resource_id,
                ) from exc
            raise error from exc

    @staticmethod
    def _checked_response(
        response: _T,
        *,
        operation: str,
        resource_id: str | None = None,
        document_level: bool = False,
    ) -> _T:
        if response is None:
            raise FeishuProtocolError(
                "飞书 SDK 未返回响应",
                operation=operation,
                resource_id=resource_id,
            )
        try:
            success = bool(response.success())  # type: ignore[attr-defined]
        except Exception as exc:
            raise FeishuProtocolError(
                "飞书 SDK 响应缺少 success()",
                operation=operation,
                resource_id=resource_id,
            ) from exc
        if success:
            return response

        code = getattr(response, "code", None)
        message = str(getattr(response, "msg", "") or "飞书接口调用失败")
        raise _typed_error(
            operation=operation,
            message=message,
            code=code,
            status_code=_response_status(response),
            resource_id=resource_id,
            document_level=document_level,
        )

    # ---- 多维表格 ----

    async def get_record(
        self, record_id: str, *, app_token: str = "", table_id: str = ""
    ) -> dict[str, Any]:
        """获取单条记录；失败或记录不存在时抛出类型化异常。"""

        app_token = app_token or self._config.bitable_app_token
        table_id = table_id or self._config.bitable_table_id
        req = (
            GetAppTableRecordRequest.builder()
            .app_token(app_token)
            .table_id(table_id)
            .record_id(record_id)
            .build()
        )
        resp = await self._sdk_call(
            "get_record",
            lambda: self._client.bitable.v1.app_table_record.get(req),
            resource_id=record_id,
        )
        self._checked_response(resp, operation="get_record", resource_id=record_id)

        record: AppTableRecord | None = getattr(getattr(resp, "data", None), "record", None)
        if record is None:
            raise FeishuNotFoundError(
                "多维表记录不存在",
                operation="get_record",
                resource_id=record_id,
            )
        return dict(record.fields or {})

    async def update_record(
        self,
        record_id: str,
        fields: dict[str, Any],
        *,
        app_token: str = "",
        table_id: str = "",
    ) -> bool:
        """更新记录；成功返回 ``True``，失败直接抛出异常。"""

        app_token = app_token or self._config.bitable_app_token
        table_id = table_id or self._config.bitable_table_id
        body = AppTableRecord.builder().fields(fields).build()
        req = (
            UpdateAppTableRecordRequest.builder()
            .app_token(app_token)
            .table_id(table_id)
            .record_id(record_id)
            .request_body(body)
            .build()
        )
        resp = await self._sdk_call(
            "update_record",
            lambda: self._client.bitable.v1.app_table_record.update(req),
            resource_id=record_id,
        )
        self._checked_response(resp, operation="update_record", resource_id=record_id)
        return True

    # ---- 飞书文档 ----

    async def get_doc_raw_content(self, document_id: str) -> str:
        """获取文档纯文本；访问或接口失败时抛出类型化异常。"""

        req = RawContentDocumentRequest.builder().document_id(document_id).build()
        resp = await self._sdk_call(
            "get_doc_raw_content",
            lambda: self._client.docx.v1.document.raw_content(req),
            resource_id=document_id,
            document_level=True,
        )
        self._checked_response(
            resp,
            operation="get_doc_raw_content",
            resource_id=document_id,
            document_level=True,
        )
        data = getattr(resp, "data", None)
        if data is None:
            raise FeishuProtocolError(
                "文档内容响应缺少 data",
                operation="get_doc_raw_content",
                resource_id=document_id,
            )
        return str(getattr(data, "content", "") or "")

    async def get_wiki_node(self, node_token: str) -> tuple[str, str]:
        """解析 Wiki 节点，返回真实文档的 ``(obj_token, obj_type)``。"""

        req = GetNodeSpaceRequest.builder().token(node_token).build()
        resp = await self._sdk_call(
            "get_wiki_node",
            lambda: self._client.wiki.v2.space.get_node(req),
            resource_id=node_token,
            document_level=True,
        )
        self._checked_response(
            resp,
            operation="get_wiki_node",
            resource_id=node_token,
            document_level=True,
        )
        node = getattr(getattr(resp, "data", None), "node", None)
        if node is None:
            raise FeishuNotFoundError(
                "Wiki 节点不存在或已删除",
                operation="get_wiki_node",
                resource_id=node_token,
            )
        if not node.obj_token or not node.obj_type:
            raise FeishuMaterialError(
                "Wiki 节点未挂载可导出的文档",
                operation="get_wiki_node",
                resource_id=node_token,
            )
        return node.obj_token, node.obj_type

    async def export_doc_to_pdf(
        self, doc_token: str, doc_type: str = "docx"
    ) -> bytes:
        """将飞书云文档导出为 PDF。

        轮询等待使用异步 ``sleep``，只有每次同步 SDK 请求进入工作线程。完整导出
        流程受独立信号量限制，避免大量任务同时轮询飞书。
        """

        self._ensure_open()
        async with self._export_slots:
            create_req = (
                CreateExportTaskRequest.builder()
                .request_body(
                    ExportTask.builder()
                    .file_extension("pdf")
                    .token(doc_token)
                    .type(doc_type)
                    .build()
                )
                .build()
            )
            create_resp = await self._sdk_call(
                "create_export_task",
                lambda: self._client.drive.v1.export_task.create(create_req),
                resource_id=doc_token,
                document_level=True,
            )
            self._checked_response(
                create_resp,
                operation="create_export_task",
                resource_id=doc_token,
                document_level=True,
            )
            ticket = getattr(getattr(create_resp, "data", None), "ticket", None)
            if not ticket:
                raise FeishuProtocolError(
                    "创建导出任务成功但未返回 ticket",
                    operation="create_export_task",
                    resource_id=doc_token,
                )

            file_token: str | None = None
            for _ in range(_EXPORT_POLL_MAX_TRIES):
                await asyncio.sleep(_EXPORT_POLL_INTERVAL)
                get_req = (
                    GetExportTaskRequest.builder()
                    .ticket(ticket)
                    .token(doc_token)
                    .build()
                )
                get_resp = await self._sdk_call(
                    "get_export_task",
                    lambda: self._client.drive.v1.export_task.get(get_req),
                    resource_id=doc_token,
                    document_level=True,
                )
                self._checked_response(
                    get_resp,
                    operation="get_export_task",
                    resource_id=doc_token,
                    document_level=True,
                )
                result = getattr(getattr(get_resp, "data", None), "result", None)
                if result is None:
                    continue
                if result.job_status == _EXPORT_JOB_SUCCESS:
                    file_token = result.file_token
                    if not file_token:
                        raise FeishuProtocolError(
                            "导出任务完成但未返回 file_token",
                            operation="get_export_task",
                            resource_id=doc_token,
                        )
                    break
                if result.job_error_msg:
                    raise _typed_error(
                        operation="get_export_task",
                        message=str(result.job_error_msg),
                        code=result.job_status,
                        resource_id=doc_token,
                        document_level=True,
                    )

            if not file_token:
                raise FeishuTimeoutError(
                    "文档导出任务在轮询期限内未完成",
                    operation="get_export_task",
                    resource_id=doc_token,
                )

            dl_req = (
                DownloadExportTaskRequest.builder().file_token(file_token).build()
            )
            dl_resp = await self._sdk_call(
                "download_export_task",
                lambda: self._client.drive.v1.export_task.download(dl_req),
                resource_id=doc_token,
                document_level=True,
            )
            self._checked_response(
                dl_resp,
                operation="download_export_task",
                resource_id=doc_token,
                document_level=True,
            )
            exported_file = getattr(dl_resp, "file", None)
            if exported_file is None:
                raise FeishuProtocolError(
                    "下载导出文件成功但响应中没有文件",
                    operation="download_export_task",
                    resource_id=doc_token,
                )
            content = await self._sdk_call(
                "read_exported_pdf",
                exported_file.read,
                resource_id=doc_token,
                document_level=True,
            )
            if not isinstance(content, (bytes, bytearray)):
                raise FeishuProtocolError(
                    "导出文件读取结果不是字节内容",
                    operation="read_exported_pdf",
                    resource_id=doc_token,
                )
            return bytes(content)

    # ---- 消息通知 ----

    async def send_text_message(
        self, receive_id: str, text: str, *, receive_id_type: str = "open_id"
    ) -> bool:
        """发送文本消息；失败抛异常。"""

        content = json.dumps({"text": text})
        return await self._send_message(
            receive_id, receive_id_type, "text", content, "send_text_message"
        )

    async def send_card_message(
        self,
        receive_id: str,
        card_json: dict[str, Any],
        *,
        receive_id_type: str = "open_id",
    ) -> bool:
        """发送消息卡片；失败抛异常。"""

        content = json.dumps(card_json, ensure_ascii=False)
        return await self._send_message(
            receive_id,
            receive_id_type,
            "interactive",
            content,
            "send_card_message",
        )

    async def _send_message(
        self,
        receive_id: str,
        receive_id_type: str,
        msg_type: str,
        content: str,
        operation: str,
    ) -> bool:
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type(msg_type)
            .content(content)
            .build()
        )
        req = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(body)
            .build()
        )
        resp = await self._sdk_call(
            operation,
            lambda: self._client.im.v1.message.create(req),
            resource_id=receive_id,
        )
        self._checked_response(resp, operation=operation, resource_id=receive_id)
        return True

    # ---- 附件下载 ----

    async def download_attachment(self, url: str) -> bytes:
        """使用复用的 HTTP client 下载附件；失败抛出类型化异常。"""

        self._ensure_open()
        token = await self._sdk_call(
            "get_tenant_access_token", self._tenant_token_provider
        )
        if not token:
            raise FeishuAppConfigError(
                "飞书租户访问令牌为空",
                operation="get_tenant_access_token",
            )

        try:
            response = await self._http.get(
                url, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.TimeoutException as exc:
            raise FeishuTimeoutError(
                str(exc) or "附件下载超时",
                operation="download_attachment",
                resource_id=url,
            ) from exc
        except httpx.InvalidURL as exc:
            raise FeishuMaterialError(
                str(exc) or "附件下载地址无效",
                operation="download_attachment",
                resource_id=url,
            ) from exc
        except httpx.TransportError as exc:
            raise FeishuTemporaryServiceError(
                str(exc) or "附件下载网络不可达",
                operation="download_attachment",
                resource_id=url,
            ) from exc

        if not response.is_success:
            raise _typed_error(
                operation="download_attachment",
                message=response.text,
                status_code=response.status_code,
                resource_id=url,
                document_level=True,
            )
        return response.content

    async def close(self) -> None:
        """有界等待遗留 SDK 线程，并集中关闭复用 HTTP client。"""

        async with self._close_lock:
            if self._http_closed:
                return
            self._closed = True
            pending_sdk = tuple(self._sdk_tasks)
            if pending_sdk:
                try:
                    async with asyncio.timeout(self._sdk_close_timeout):
                        await asyncio.gather(
                            *(asyncio.shield(task) for task in pending_sdk),
                            return_exceptions=True,
                        )
                except TimeoutError:
                    logger.warning(
                        "等待飞书 SDK 线程结束超时: pending=%d timeout=%.1fs",
                        sum(not task.done() for task in pending_sdk),
                        self._sdk_close_timeout,
                    )
            await self._http.aclose()
            self._http_closed = True
