from __future__ import annotations

import asyncio
import io
import threading
from types import SimpleNamespace
from typing import Any, Callable

import httpx
import pytest
import requests

import app.feishu as feishu_module
from app.errors import (
    ErrorCategory,
    FeishuAppConfigError,
    FeishuAppPermissionError,
    FeishuDocumentPermissionError,
    FeishuMaterialError,
    FeishuNotFoundError,
    FeishuRateLimitError,
    FeishuTemporaryServiceError,
    FeishuTimeoutError,
)
from app.feishu import FeishuClient


def _response(
    *,
    code: int = 0,
    msg: str = "",
    data: Any = None,
    status_code: int = 200,
    file: Any = None,
) -> Any:
    return SimpleNamespace(
        code=code,
        msg=msg,
        data=data,
        raw=SimpleNamespace(status_code=status_code),
        file=file,
        success=lambda: code == 0,
    )


def _record_sdk(handler: Callable[[Any], Any]) -> Any:
    endpoint = SimpleNamespace(get=handler)
    return SimpleNamespace(
        bitable=SimpleNamespace(
            v1=SimpleNamespace(app_table_record=endpoint),
        )
    )


def _update_sdk(handler: Callable[[Any], Any]) -> Any:
    endpoint = SimpleNamespace(update=handler)
    return SimpleNamespace(
        bitable=SimpleNamespace(
            v1=SimpleNamespace(app_table_record=endpoint),
        )
    )


class _FakeHttpClient:
    def __init__(self, response: httpx.Response | None = None) -> None:
        self.response = response or httpx.Response(
            200,
            content=b"attachment",
            request=httpx.Request("GET", "https://example.test/file"),
        )
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.close_calls = 0

    class _Stream:
        def __init__(self, response: httpx.Response) -> None:
            self.response = response

        async def __aenter__(self) -> httpx.Response:
            return self.response

        async def __aexit__(self, *args: Any) -> None:
            return None

    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
    ) -> "_FakeHttpClient._Stream":
        assert method == "GET"
        self.calls.append((url, headers))
        return self._Stream(self.response)

    async def aclose(self) -> None:
        self.close_calls += 1


async def _wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_sync_sdk_call_runs_off_event_loop(config) -> None:
    main_thread = threading.get_ident()
    call_thread: int | None = None
    started = threading.Event()
    release = threading.Event()

    def blocking_get(_: Any) -> Any:
        nonlocal call_thread
        call_thread = threading.get_ident()
        started.set()
        release.wait(timeout=1)
        record = SimpleNamespace(fields={"评分状态": "待评分"})
        return _response(data=SimpleNamespace(record=record))

    http = _FakeHttpClient()
    client = FeishuClient(
        config,
        sdk_client=_record_sdk(blocking_get),
        http_client=http,  # type: ignore[arg-type]
        tenant_token_provider=lambda: "token",
    )

    task = asyncio.create_task(client.get_record("rec_1"))
    await _wait_until(started.is_set)
    # 若 SDK 在事件循环线程执行，这个协程无法运行到这里。
    await asyncio.sleep(0)
    assert not task.done()
    assert call_thread != main_thread

    release.set()
    assert await task == {"评分状态": "待评分"}
    await client.close()


@pytest.mark.asyncio
async def test_list_scoring_records_returns_system_last_modified_time(config) -> None:
    requests_seen = []
    responses = iter(
        [
            _response(
                data=SimpleNamespace(
                    items=[
                        SimpleNamespace(
                            record_id="rec_1",
                            fields={"评分状态": "评分中"},
                            last_modified_time=123_000,
                        )
                    ],
                    has_more=True,
                    page_token="next",
                )
            ),
            _response(
                data=SimpleNamespace(
                    items=[
                        SimpleNamespace(
                            record_id="rec_2",
                            fields={"评分状态": "评分中"},
                            last_modified_time=456_000,
                        )
                    ],
                    has_more=False,
                    page_token="",
                )
            ),
        ]
    )

    def search(request):
        requests_seen.append(request)
        return next(responses)

    sdk = SimpleNamespace(
        bitable=SimpleNamespace(
            v1=SimpleNamespace(
                app_table_record=SimpleNamespace(search=search),
            )
        )
    )
    client = FeishuClient(
        config,
        sdk_client=sdk,
        http_client=_FakeHttpClient(),  # type: ignore[arg-type]
        tenant_token_provider=lambda: "token",
    )

    records = await client.list_scoring_records(app_token="app", table_id="table")

    assert records == [
        {
            "record_id": "rec_1",
            "fields": {"评分状态": "评分中"},
            "last_modified_time": 123_000,
        },
        {
            "record_id": "rec_2",
            "fields": {"评分状态": "评分中"},
            "last_modified_time": 456_000,
        },
    ]
    assert requests_seen[0].page_token is None
    assert requests_seen[1].page_token == "next"
    condition = requests_seen[0].request_body.filter.conditions[0]
    assert condition.field_name == "评分状态"
    assert condition.value == ["评分中"]
    assert requests_seen[0].request_body.automatic_fields
    await client.close()


@pytest.mark.asyncio
async def test_sdk_concurrency_is_bounded(config) -> None:
    lock = threading.Lock()
    release = threading.Event()
    active = 0
    peak = 0

    def blocking_get(_: Any) -> Any:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        release.wait(timeout=1)
        with lock:
            active -= 1
        return _response(
            data=SimpleNamespace(record=SimpleNamespace(fields={"ok": True}))
        )

    client = FeishuClient(
        config,
        sdk_client=_record_sdk(blocking_get),
        http_client=_FakeHttpClient(),  # type: ignore[arg-type]
        tenant_token_provider=lambda: "token",
        sdk_concurrency=2,
    )
    tasks = [asyncio.create_task(client.get_record(f"rec_{i}")) for i in range(4)]
    await _wait_until(lambda: peak == 2)
    await asyncio.sleep(0.01)
    assert peak == 2
    assert sum(task.done() for task in tasks) == 0

    release.set()
    await asyncio.gather(*tasks)
    assert peak == 2
    await client.close()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_release_running_sdk_slot(config) -> None:
    release = threading.Event()
    entered = 0
    lock = threading.Lock()

    def blocking_get(_: Any) -> Any:
        nonlocal entered
        with lock:
            entered += 1
        release.wait(timeout=1)
        return _response(
            data=SimpleNamespace(record=SimpleNamespace(fields={"ok": True}))
        )

    client = FeishuClient(
        config,
        sdk_client=_record_sdk(blocking_get),
        http_client=_FakeHttpClient(),  # type: ignore[arg-type]
        tenant_token_provider=lambda: "token",
        sdk_concurrency=1,
    )
    first = asyncio.create_task(client.get_record("rec_1"))
    await _wait_until(lambda: entered == 1)
    first.cancel()
    second = asyncio.create_task(client.get_record("rec_2"))
    await asyncio.sleep(0.01)
    assert entered == 1

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await second == {"ok": True}
    assert entered == 2
    await client.close()


@pytest.mark.asyncio
async def test_export_workflow_concurrency_is_bounded(config, monkeypatch) -> None:
    monkeypatch.setattr(feishu_module, "_EXPORT_POLL_INTERVAL", 0)
    calls: list[str] = []
    first_create_started = threading.Event()
    release_first_create = threading.Event()
    create_count = 0
    main_thread = threading.get_ident()
    read_threads: list[int] = []

    def create(_: Any) -> Any:
        nonlocal create_count
        create_count += 1
        calls.append(f"create:{create_count}")
        if create_count == 1:
            first_create_started.set()
            release_first_create.wait(timeout=1)
        return _response(data=SimpleNamespace(ticket=f"ticket_{create_count}"))

    def get(_: Any) -> Any:
        calls.append("get")
        result = SimpleNamespace(
            job_status=0, file_token="file_pdf", job_error_msg=None
        )
        return _response(data=SimpleNamespace(result=result))

    class TrackedFile(io.BytesIO):
        def read(self, *args: Any, **kwargs: Any) -> bytes:
            read_threads.append(threading.get_ident())
            return super().read(*args, **kwargs)

    def download(_: Any) -> Any:
        calls.append("download")
        return _response(file=TrackedFile(b"%PDF-test"))

    export_task = SimpleNamespace(create=create, get=get, download=download)
    sdk = SimpleNamespace(
        drive=SimpleNamespace(v1=SimpleNamespace(export_task=export_task))
    )
    client = FeishuClient(
        config,
        sdk_client=sdk,
        http_client=_FakeHttpClient(),  # type: ignore[arg-type]
        tenant_token_provider=lambda: "token",
        sdk_concurrency=4,
        export_concurrency=1,
    )

    first = asyncio.create_task(client.export_doc_to_pdf("doc_1"))
    second = asyncio.create_task(client.export_doc_to_pdf("doc_2"))
    await _wait_until(first_create_started.is_set)
    await asyncio.sleep(0.01)
    assert calls == ["create:1"]

    release_first_create.set()
    assert await asyncio.gather(first, second) == [b"%PDF-test", b"%PDF-test"]
    assert calls == [
        "create:1",
        "get",
        "download",
        "create:2",
        "get",
        "download",
    ]
    assert read_threads and all(thread != main_thread for thread in read_threads)
    await client.close()


@pytest.mark.parametrize(
    ("response", "error_type", "category"),
    [
        (
            _response(code=99991672, msg="Access denied: scope required", status_code=403),
            FeishuAppPermissionError,
            ErrorCategory.SYSTEM_HARD_FAILURE,
        ),
        (
            _response(code=403, msg="Forbidden", status_code=403),
            FeishuDocumentPermissionError,
            ErrorCategory.USER_FIXABLE,
        ),
        (
            _response(code=99991429, msg="rate limit", status_code=429),
            FeishuRateLimitError,
            ErrorCategory.TRANSIENT,
        ),
        (
            _response(code=1254290, msg="TooManyRequest", status_code=200),
            FeishuRateLimitError,
            ErrorCategory.TRANSIENT,
        ),
        (
            _response(code=230020, msg="request frequency limit", status_code=200),
            FeishuRateLimitError,
            ErrorCategory.TRANSIENT,
        ),
        (
            _response(code=404, msg="document not found", status_code=404),
            FeishuNotFoundError,
            ErrorCategory.USER_FIXABLE,
        ),
        (
            _response(code=1254043, msg="RecordIdNotFound", status_code=200),
            FeishuNotFoundError,
            ErrorCategory.USER_FIXABLE,
        ),
        (
            _response(
                code=1254001,
                msg="unsupported document type",
                status_code=200,
            ),
            FeishuMaterialError,
            ErrorCategory.USER_FIXABLE,
        ),
        (
            _response(code=500, msg="internal server error", status_code=500),
            FeishuTemporaryServiceError,
            ErrorCategory.TRANSIENT,
        ),
        (
            _response(code=1254291, msg="Write conflict", status_code=200),
            FeishuTemporaryServiceError,
            ErrorCategory.TRANSIENT,
        ),
        (
            _response(code=1255001, msg="InternalError", status_code=200),
            FeishuTemporaryServiceError,
            ErrorCategory.TRANSIENT,
        ),
        (
            _response(code=401, msg="invalid access token", status_code=401),
            FeishuAppConfigError,
            ErrorCategory.SYSTEM_HARD_FAILURE,
        ),
    ],
)
def test_document_api_errors_keep_type_and_category(
    response, error_type, category
) -> None:
    error = feishu_module._typed_error(
        operation="export_doc_to_pdf",
        message=response.msg,
        code=response.code,
        status_code=response.raw.status_code,
        resource_id="doc_1",
        document_level=True,
    )

    assert isinstance(error, error_type)
    assert error.category is category
    assert error.operation == "export_doc_to_pdf"
    assert error.resource_id == "doc_1"


@pytest.mark.parametrize(
    ("operation", "code", "message", "error_type", "category"),
    [
        (
            "get_export_task",
            3,
            "internal error",
            FeishuTemporaryServiceError,
            ErrorCategory.TRANSIENT,
        ),
        (
            "get_export_task",
            108,
            "export failed",
            FeishuTimeoutError,
            ErrorCategory.TRANSIENT,
        ),
        (
            "get_export_task",
            109,
            "export failed",
            FeishuDocumentPermissionError,
            ErrorCategory.USER_FIXABLE,
        ),
        (
            "get_export_task",
            111,
            "export failed",
            FeishuNotFoundError,
            ErrorCategory.USER_FIXABLE,
        ),
        (
            "create_export_task",
            600,
            "hybrid resource expired",
            FeishuTemporaryServiceError,
            ErrorCategory.TRANSIENT,
        ),
    ],
)
def test_export_business_status_mapping(
    operation, code, message, error_type, category
) -> None:
    error = feishu_module._typed_error(
        operation=operation,
        message=message,
        code=code,
        document_level=True,
    )

    assert isinstance(error, error_type)
    assert error.category is category


@pytest.mark.asyncio
async def test_sdk_timeout_is_typed_transient_error(config) -> None:
    def timeout(_: Any) -> Any:
        raise requests.Timeout("read timed out")

    client = FeishuClient(
        config,
        sdk_client=_record_sdk(timeout),
        http_client=_FakeHttpClient(),  # type: ignore[arg-type]
        tenant_token_provider=lambda: "token",
    )
    with pytest.raises(FeishuTimeoutError) as exc_info:
        await client.get_record("rec_1")

    assert exc_info.value.retryable
    await client.close()


@pytest.mark.asyncio
async def test_update_error_raises_instead_of_returning_false(config) -> None:
    client = FeishuClient(
        config,
        sdk_client=_update_sdk(
            lambda _: _response(
                code=99991429, msg="rate limit", status_code=429
            )
        ),
        http_client=_FakeHttpClient(),  # type: ignore[arg-type]
        tenant_token_provider=lambda: "token",
    )

    with pytest.raises(FeishuRateLimitError):
        await client.update_record("rec_1", {"评分状态": "评分中"})

    await client.close()


@pytest.mark.asyncio
async def test_download_uses_injected_token_provider_and_reused_http_client(
    config,
) -> None:
    main_thread = threading.get_ident()
    token_threads: list[int] = []

    def token_provider() -> str:
        token_threads.append(threading.get_ident())
        return "tenant-token"

    # sdk_client 故意不提供任何私有配置；下载只能使用显式 token provider。
    sdk_without_config = SimpleNamespace()
    http = _FakeHttpClient()
    client = FeishuClient(
        config,
        sdk_client=sdk_without_config,
        http_client=http,  # type: ignore[arg-type]
        tenant_token_provider=token_provider,
    )

    assert await client.download_attachment("https://example.test/a") == b"attachment"
    assert await client.download_attachment("https://example.test/b") == b"attachment"
    assert [url for url, _ in http.calls] == [
        "https://example.test/a",
        "https://example.test/b",
    ]
    assert all(headers["Authorization"] == "Bearer tenant-token" for _, headers in http.calls)
    assert token_threads and all(thread != main_thread for thread in token_threads)

    await client.close()
    await client.close()
    assert http.close_calls == 1


@pytest.mark.asyncio
async def test_attachment_file_token_prefers_official_drive_media_sdk(config) -> None:
    read_threads = []

    class TrackedFile(io.BytesIO):
        def read(self, *args: Any, **kwargs: Any) -> bytes:
            read_threads.append(threading.get_ident())
            return super().read(*args, **kwargs)

    requests_seen = []

    def download(request):
        requests_seen.append(request)
        return _response(file=TrackedFile(b"attachment-via-sdk"))

    sdk = SimpleNamespace(
        drive=SimpleNamespace(
            v1=SimpleNamespace(media=SimpleNamespace(download=download))
        )
    )
    http = _FakeHttpClient()
    client = FeishuClient(
        config,
        sdk_client=sdk,
        http_client=http,  # type: ignore[arg-type]
        tenant_token_provider=lambda: pytest.fail("SDK 下载不需要手工 tenant token"),
    )

    result = await client.download_attachment(
        "https://evil.example/should-not-be-used",
        file_token="file_token_1",
    )

    assert result == b"attachment-via-sdk"
    assert requests_seen[0].file_token == "file_token_1"
    assert http.calls == []
    assert read_threads
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/file",
        "https://evil.example/file?tenant_access_token=secret",
    ],
)
async def test_download_rejects_unapproved_url_before_requesting_token(config, url) -> None:
    token_calls = 0

    def token_provider() -> str:
        nonlocal token_calls
        token_calls += 1
        return "must-not-be-used"

    http = _FakeHttpClient()
    client = FeishuClient(
        config,
        sdk_client=SimpleNamespace(),
        http_client=http,  # type: ignore[arg-type]
        tenant_token_provider=token_provider,
    )

    with pytest.raises(FeishuMaterialError) as exc_info:
        await client.download_attachment(url)

    assert token_calls == 0
    assert http.calls == []
    assert "secret" not in str(exc_info.value)
    await client.close()


@pytest.mark.asyncio
async def test_streaming_download_stops_when_size_limit_is_exceeded(config) -> None:
    response = httpx.Response(
        200,
        content=b"0123456789",
        request=httpx.Request("GET", "https://example.test/file"),
    )
    client = FeishuClient(
        config,
        sdk_client=SimpleNamespace(),
        http_client=_FakeHttpClient(response),  # type: ignore[arg-type]
        tenant_token_provider=lambda: "token",
    )

    with pytest.raises(FeishuMaterialError, match="大小超过"):
        await client.download_attachment(
            "https://example.test/file?temporary_token=hidden",
            max_bytes=5,
        )

    await client.close()


@pytest.mark.asyncio
async def test_token_provider_failure_is_app_config_error(config) -> None:
    def broken_token_provider() -> str:
        raise ValueError("obtain tenant token failed")

    client = FeishuClient(
        config,
        sdk_client=SimpleNamespace(),
        http_client=_FakeHttpClient(),  # type: ignore[arg-type]
        tenant_token_provider=broken_token_provider,
    )

    with pytest.raises(FeishuAppConfigError) as exc_info:
        await client.download_attachment("https://example.test/file")

    assert exc_info.value.category is ErrorCategory.SYSTEM_HARD_FAILURE
    await client.close()


@pytest.mark.asyncio
async def test_token_provider_5xx_remains_transient(config) -> None:
    class TokenServiceError(Exception):
        code = 500
        msg = "internal server error"

    def broken_token_provider() -> str:
        raise TokenServiceError()

    client = FeishuClient(
        config,
        sdk_client=SimpleNamespace(),
        http_client=_FakeHttpClient(),  # type: ignore[arg-type]
        tenant_token_provider=broken_token_provider,
    )

    with pytest.raises(FeishuTemporaryServiceError) as exc_info:
        await client.download_attachment("https://example.test/file")

    assert exc_info.value.retryable
    await client.close()


@pytest.mark.asyncio
async def test_close_waits_for_cancelled_sdk_thread(config) -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking_get(_: Any) -> Any:
        started.set()
        release.wait(timeout=1)
        return _response(
            data=SimpleNamespace(record=SimpleNamespace(fields={"ok": True}))
        )

    http = _FakeHttpClient()
    client = FeishuClient(
        config,
        sdk_client=_record_sdk(blocking_get),
        http_client=http,  # type: ignore[arg-type]
        tenant_token_provider=lambda: "token",
    )
    request_task = asyncio.create_task(client.get_record("rec_1"))
    await _wait_until(started.is_set)
    request_task.cancel()
    close_task = asyncio.create_task(client.close())
    await asyncio.sleep(0.01)

    assert not close_task.done()
    assert http.close_calls == 0

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await request_task
    await close_task
    assert http.close_calls == 1
