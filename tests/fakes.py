"""核心工作流使用的可编程测试替身。"""

from __future__ import annotations

import asyncio
import copy
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Iterable


@dataclass
class CallTrace:
    """跨多个 fake 保存严格调用顺序。"""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def add(self, name: str, **details: Any) -> None:
        self.calls.append((name, details))

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class ScriptedOutcomes:
    """为指定操作按调用顺序返回值或抛出异常。"""

    def __init__(self) -> None:
        self._outcomes: dict[str, Deque[Any]] = defaultdict(deque)

    def script(self, operation: str, *outcomes: Any) -> None:
        self._outcomes[operation].extend(outcomes)

    def has(self, operation: str) -> bool:
        return bool(self._outcomes.get(operation))

    def next(self, operation: str, default: Any = None) -> Any:
        queue = self._outcomes.get(operation)
        outcome = queue.popleft() if queue else default
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeClock:
    """同时提供墙上时钟、单调时钟和异步 sleep 的可控时钟。"""

    def __init__(self, initial: float = 0.0) -> None:
        self.current = float(initial)
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.current

    def monotonic(self) -> float:
        return self.current

    def now_utc(self) -> datetime:
        return datetime.fromtimestamp(self.current, tz=timezone.utc)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.advance(seconds)
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        self.current += float(seconds)


class FakeFeishuClient:
    """内存记录仓库 + 飞书外部调用捕获器。"""

    def __init__(
        self,
        records: dict[tuple[str, str, str], dict[str, Any]] | None = None,
        *,
        trace: CallTrace | None = None,
    ) -> None:
        self.records = copy.deepcopy(records or {})
        self.trace = trace or CallTrace()
        self.outcomes = ScriptedOutcomes()
        self.cards: list[dict[str, Any]] = []
        self.closed = False

    @staticmethod
    def key(app_token: str, table_id: str, record_id: str) -> tuple[str, str, str]:
        return app_token, table_id, record_id

    async def get_record(
        self,
        record_id: str,
        *,
        app_token: str = "",
        table_id: str = "",
    ) -> dict[str, Any] | None:
        self.trace.add(
            "feishu.get_record",
            app_token=app_token,
            table_id=table_id,
            record_id=record_id,
        )
        default = copy.deepcopy(self.records.get(self.key(app_token, table_id, record_id)))
        return self.outcomes.next("get_record", default)

    async def update_record(
        self,
        record_id: str,
        fields: dict[str, Any],
        *,
        app_token: str = "",
        table_id: str = "",
    ) -> bool:
        self.trace.add(
            "feishu.update_record",
            app_token=app_token,
            table_id=table_id,
            record_id=record_id,
            fields=copy.deepcopy(fields),
        )
        success = bool(self.outcomes.next("update_record", True))
        if success:
            key = self.key(app_token, table_id, record_id)
            self.records.setdefault(key, {}).update(copy.deepcopy(fields))
        return success

    async def get_wiki_node(self, node_token: str) -> tuple[str, str] | None:
        self.trace.add("feishu.get_wiki_node", node_token=node_token)
        return self.outcomes.next("get_wiki_node")

    async def export_doc_to_pdf(self, doc_token: str, doc_type: str = "docx") -> bytes:
        self.trace.add(
            "feishu.export_doc_to_pdf", doc_token=doc_token, doc_type=doc_type
        )
        return self.outcomes.next("export_doc_to_pdf", b"%PDF-fake-doc")

    async def get_doc_raw_content(self, document_id: str) -> str:
        self.trace.add("feishu.get_doc_raw_content", document_id=document_id)
        if not self.outcomes.has("get_doc_raw_content"):
            raise AssertionError("新评分流程禁止调用 raw_content")
        return self.outcomes.next("get_doc_raw_content")

    async def download_attachment(
        self,
        url: str = "",
        *,
        file_token: str = "",
        max_bytes: int | None = None,
    ) -> bytes:
        self.trace.add(
            "feishu.download_attachment",
            url=url,
            file_token=file_token,
            max_bytes=max_bytes,
        )
        return self.outcomes.next("download_attachment", b"%PDF-fake-attachment")

    async def send_card_message(
        self,
        receive_id: str,
        card_json: dict[str, Any],
        *,
        receive_id_type: str = "open_id",
    ) -> bool:
        call = {
            "receive_id": receive_id,
            "receive_id_type": receive_id_type,
            "card": copy.deepcopy(card_json),
        }
        self.trace.add("feishu.send_card_message", **call)
        success = bool(self.outcomes.next("send_card_message", True))
        if success:
            self.cards.append(call)
        return success

    async def list_scoring_records(
        self,
        *,
        app_token: str = "",
        table_id: str = "",
    ) -> list[dict[str, Any]]:
        self.trace.add(
            "feishu.list_scoring_records",
            app_token=app_token,
            table_id=table_id,
        )
        return self.outcomes.next("list_scoring_records", [])

    async def close(self) -> None:
        self.trace.add("feishu.close")
        self.closed = True


class FakeAIClient:
    """可控制开始/释放时机的文件评分模型替身。"""

    def __init__(
        self,
        *,
        supports_pdf: bool = True,
        score_results: Iterable[Any] | None = None,
        transcriptions: Iterable[Any] | None = None,
        trace: CallTrace | None = None,
    ) -> None:
        self.supports_pdf = supports_pdf
        self.trace = trace or CallTrace()
        self.outcomes = ScriptedOutcomes()
        if score_results:
            self.outcomes.script("score", *score_results)
        if transcriptions:
            self.outcomes.script("transcribe", *transcriptions)
        self.score_payloads: list[Any] = []
        self.transcribe_payloads: list[Any] = []
        self.started: asyncio.Event | None = None
        self.release: asyncio.Event | None = None
        self.closed = False

    def gate(self) -> tuple[asyncio.Event, asyncio.Event]:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        return self.started, self.release

    async def _wait_gate(self) -> None:
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()

    async def transcribe(self, payload: Any) -> str:
        self.trace.add("ai.transcribe", payload=payload)
        self.transcribe_payloads.append(payload)
        await self._wait_gate()
        return self.outcomes.next("transcribe", "合并转写")

    async def score(self, payload: Any) -> Any:
        self.trace.add("ai.score", payload=payload)
        self.score_payloads.append(payload)
        await self._wait_gate()
        return self.outcomes.next(
            "score",
            {
                "score": 80,
                "detail": "合格",
                "highlights": "结构清晰",
                "improvements": "补充数据依据",
                "dimensions": {
                    "completeness": 24,
                    "logic": 24,
                    "format": 16,
                    "quality": 16,
                },
            },
        )

    async def close(self) -> None:
        self.trace.add("ai.close")
        self.closed = True


class FakeNotifier:
    """按业务语义记录通知，不耦合具体卡片 JSON。"""

    def __init__(self, *, trace: CallTrace | None = None) -> None:
        self.trace = trace or CallTrace()
        self.notifications: list[tuple[str, dict[str, Any]]] = []

    async def _record(self, notification_type: str, **payload: Any) -> bool:
        self.trace.add(f"notify.{notification_type}", **payload)
        self.notifications.append((notification_type, payload))
        return True

    async def notify_score_passed(self, **payload: Any) -> bool:
        return await self._record("passed", **payload)

    async def notify_score_failed(self, **payload: Any) -> bool:
        return await self._record("failed", **payload)

    async def notify_rejected(self, **payload: Any) -> bool:
        return await self._record("rejected", **payload)

    async def notify_material_error(self, **payload: Any) -> bool:
        return await self._record("material_error", **payload)

    async def notify_error_to_group(self, **payload: Any) -> bool:
        return await self._record("admin_error", **payload)
