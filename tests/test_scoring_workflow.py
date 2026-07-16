from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

import pytest
from pypdf import PdfWriter

from app.ai import AIScoringError, AITransientError
from app.content_collector import ContentCollector
from app.errors import (
    FeishuDocumentPermissionError,
    FeishuMaterialError,
    FeishuNotFoundError,
)
from app.field_mapping import (
    FIELD_AI_SCORE,
    FIELD_DOC_CACHE,
    FIELD_DOC_LINK,
    FIELD_REVISION_ROUNDS,
    FIELD_SCORE_STATUS,
)
from app.models.content import CollectedContent
from app.record_coordinator import RecordCoordinator, RequestStatus
from app.scoring_workflow import ScoringWorkflow, WorkflowOutcome
from app.task_registry import RecordKey, TaskRegistry
from app.workflow_errors import NoFileMaterialError
from app.workflow_errors import ContentProcessingError
from app.workflow_state import TriggerSource
from tests.fakes import CallTrace, FakeAIClient, FakeFeishuClient, FakeNotifier


@dataclass
class FakeCollector:
    result: CollectedContent | None = None
    error: BaseException | None = None
    trace: CallTrace | None = None

    def __post_init__(self) -> None:
        self.calls = []

    async def collect(self, fields):
        self.calls.append(fields.copy())
        if self.trace:
            self.trace.add("collector.collect", fields=fields.copy())
        if self.error:
            raise self.error
        return self.result or CollectedContent(
            original_description="原始描述",
            review_bundle_pdf=b"bundle",
        )


def valid_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


async def execute(
    *,
    config,
    fields,
    source=TriggerSource.INITIAL_EVENT,
    collector=None,
    ai=None,
    notifier=None,
    feishu=None,
    registry=None,
    trace=None,
):
    key = RecordKey("app", "table", "record")
    trace = trace or CallTrace()
    feishu = feishu or FakeFeishuClient(
        {("app", "table", "record"): fields},
        trace=trace,
    )
    ai = ai or FakeAIClient(trace=trace)
    notifier = notifier or FakeNotifier(trace=trace)
    registry = registry or TaskRegistry()
    collector = collector or FakeCollector(trace=trace)
    workflow = ScoringWorkflow(
        config=config,
        feishu=feishu,
        ai=ai,
        collector=collector,
        notifier=notifier,
        registry=registry,
    )
    outcomes = []

    async def runner(command):
        outcomes.append(await workflow.run(command))

    submitted = registry.submit(key=key, source=source, runner=runner)
    assert submitted.accepted
    task = registry.active_entry(key).task
    assert task is not None
    await task
    return outcomes[0], feishu, ai, notifier, collector, trace, registry


@pytest.mark.asyncio
async def test_fixed_execution_order_and_single_final_write(config, record_factory) -> None:
    trace = CallTrace()
    bundle = b"same-bundle"
    outcome, feishu, ai, notifier, collector, trace, _ = await execute(
        config=config,
        fields=record_factory(status="待评分", cache="旧缓存"),
        trace=trace,
        collector=FakeCollector(
            CollectedContent("补充描述", bundle),
            trace=trace,
        ),
        ai=FakeAIClient(
            transcriptions=["本次合并转写"],
            trace=trace,
        ),
        notifier=FakeNotifier(trace=trace),
    )

    assert outcome is WorkflowOutcome.COMPLETED
    assert trace.names == [
        "feishu.get_record",
        "feishu.update_record",
        "collector.collect",
        "ai.transcribe",
        "ai.score",
        "feishu.update_record",
        "notify.passed",
    ]
    updates = [details["fields"] for name, details in trace.calls if name == "feishu.update_record"]
    assert updates[0] == {FIELD_SCORE_STATUS: "评分中"}
    assert set(updates[1]) >= {
        FIELD_AI_SCORE,
        FIELD_SCORE_STATUS,
        FIELD_REVISION_ROUNDS,
        FIELD_DOC_CACHE,
    }
    assert updates[1][FIELD_DOC_CACHE] == "本次合并转写"
    assert updates[1][FIELD_REVISION_ROUNDS] == 0
    assert ai.transcribe_payloads[0].pdf_files[0][0] is bundle
    assert ai.score_payloads[0].pdf_files[0][0] is bundle
    assert ai.transcribe_payloads[0].text == ""
    assert "补充描述" in ai.score_payloads[0].text


@pytest.mark.asyncio
async def test_enter_scoring_failure_stops_before_collection_and_ai(config, record_factory) -> None:
    feishu = FakeFeishuClient(
        {("app", "table", "record"): record_factory(status="待评分")}
    )
    feishu.outcomes.script("update_record", False, False, False)
    collector = FakeCollector()
    ai = FakeAIClient()

    outcome, *_ = await execute(
        config=config,
        fields=record_factory(),
        feishu=feishu,
        collector=collector,
        ai=ai,
    )

    assert outcome is WorkflowOutcome.ENTRY_FAILED
    assert collector.calls == []
    assert ai.score_payloads == []
    assert ai.transcribe_payloads == []


@pytest.mark.asyncio
async def test_transcription_failure_retries_only_transcription_and_blocks_score(
    config,
    record_factory,
) -> None:
    ai = FakeAIClient(
        transcriptions=[
            AITransientError("temporary"),
            AITransientError("temporary"),
            AITransientError("temporary"),
        ]
    )
    collector = FakeCollector()

    outcome, feishu, ai, notifier, *_ = await execute(
        config=config,
        fields=record_factory(status="待评分", rounds=2, cache="旧缓存"),
        collector=collector,
        ai=ai,
    )

    assert outcome is WorkflowOutcome.TECHNICAL_FAILED
    assert len(collector.calls) == 1
    assert len(ai.transcribe_payloads) == 3
    assert ai.score_payloads == []
    stored = feishu.records[("app", "table", "record")]
    assert stored[FIELD_SCORE_STATUS] == "评分异常"
    assert stored[FIELD_REVISION_ROUNDS] == 2
    assert stored[FIELD_DOC_CACHE] == "旧缓存"
    assert [kind for kind, _ in notifier.notifications] == ["admin_error"]


@pytest.mark.asyncio
async def test_score_transient_retry_does_not_repeat_completed_steps(
    config,
    record_factory,
    valid_score_result,
) -> None:
    ai = FakeAIClient(
        score_results=[
            AITransientError("temporary"),
            AITransientError("temporary"),
            valid_score_result,
        ],
        transcriptions=["cache"],
    )
    collector = FakeCollector()

    outcome, _, ai, _, collector, *_ = await execute(
        config=config,
        fields=record_factory(status="待评分"),
        collector=collector,
        ai=ai,
    )

    assert outcome is WorkflowOutcome.COMPLETED
    assert len(collector.calls) == 1
    assert len(ai.transcribe_payloads) == 1
    assert len(ai.score_payloads) == 3


@pytest.mark.asyncio
async def test_invalid_ai_response_is_system_error_without_round_or_user_card(
    config,
    record_factory,
) -> None:
    ai = FakeAIClient(
        score_results=[AIScoringError("schema invalid")],
        transcriptions=["cache"],
    )

    outcome, feishu, ai, notifier, *_ = await execute(
        config=config,
        fields=record_factory(status="待评分", rounds=1),
        ai=ai,
    )

    assert outcome is WorkflowOutcome.TECHNICAL_FAILED
    assert len(ai.score_payloads) == 1
    stored = feishu.records[("app", "table", "record")]
    assert stored[FIELD_SCORE_STATUS] == "评分异常"
    assert stored[FIELD_REVISION_ROUNDS] == 1
    assert [kind for kind, _ in notifier.notifications] == ["admin_error"]


@pytest.mark.asyncio
async def test_material_failure_becomes_failed_without_round_or_ai(config, record_factory) -> None:
    collector = FakeCollector(error=NoFileMaterialError("请上传文档"))
    ai = FakeAIClient()

    outcome, feishu, ai, notifier, *_ = await execute(
        config=config,
        fields=record_factory(status="待评分", rounds=2, cache="旧缓存"),
        collector=collector,
        ai=ai,
    )

    assert outcome is WorkflowOutcome.MATERIAL_FAILED
    assert ai.transcribe_payloads == []
    assert ai.score_payloads == []
    stored = feishu.records[("app", "table", "record")]
    assert stored[FIELD_SCORE_STATUS] == "未通过"
    assert stored[FIELD_REVISION_ROUNDS] == 2
    assert stored[FIELD_DOC_CACHE] == "旧缓存"
    assert [kind for kind, _ in notifier.notifications] == ["material_error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        FeishuDocumentPermissionError(
            "文档未授权",
            operation="export_doc_to_pdf",
            resource_id="doc",
        ),
        FeishuNotFoundError(
            "文档已删除",
            operation="export_doc_to_pdf",
            resource_id="doc",
        ),
        FeishuMaterialError(
            "文档类型不支持",
            operation="export_doc_to_pdf",
            resource_id="doc",
        ),
    ],
)
async def test_online_document_user_errors_become_failed_without_round(
    config,
    record_factory,
    error,
) -> None:
    outcome, feishu, ai, notifier, *_ = await execute(
        config=config,
        fields=record_factory(status="待评分", rounds=1, cache="旧缓存"),
        collector=FakeCollector(error=error),
    )

    assert outcome is WorkflowOutcome.MATERIAL_FAILED
    assert ai.score_payloads == []
    stored = feishu.records[("app", "table", "record")]
    assert stored[FIELD_SCORE_STATUS] == "未通过"
    assert stored[FIELD_REVISION_ROUNDS] == 1
    assert stored[FIELD_DOC_CACHE] == "旧缓存"
    assert [kind for kind, _ in notifier.notifications] == ["material_error"]


@pytest.mark.asyncio
async def test_pdf_bundle_failure_never_calls_ai_or_writes_partial_result(
    config,
    record_factory,
) -> None:
    outcome, feishu, ai, notifier, *_ = await execute(
        config=config,
        fields=record_factory(status="待评分", rounds=1, cache="旧缓存"),
        collector=FakeCollector(error=ContentProcessingError("PDF merge failed")),
    )

    assert outcome is WorkflowOutcome.TECHNICAL_FAILED
    assert ai.transcribe_payloads == []
    assert ai.score_payloads == []
    stored = feishu.records[("app", "table", "record")]
    assert stored[FIELD_SCORE_STATUS] == "评分异常"
    assert stored[FIELD_REVISION_ROUNDS] == 1
    assert stored[FIELD_DOC_CACHE] == "旧缓存"
    assert [kind for kind, _ in notifier.notifications] == ["admin_error"]


@pytest.mark.asyncio
async def test_description_documents_and_attachments_use_one_composite_score_call(
    config,
    record_factory,
    attachment_factory,
) -> None:
    record = record_factory(
        status="待评分",
        docs="https://tenant.feishu.cn/docx/doc_a",
        attachments=[attachment_factory()],
        description="补充业务目标",
    )
    feishu = FakeFeishuClient({("app", "table", "record"): record})
    feishu.outcomes.script("export_doc_to_pdf", valid_pdf())
    feishu.outcomes.script("download_attachment", valid_pdf())
    captured_materials = []

    async def build(materials):
        captured_materials.extend(materials)
        return b"composite-bundle"

    content_collector = ContentCollector(
        feishu=feishu,
        supports_pdf_input=True,
        bundle_builder=build,
        require_soffice=lambda: True,
    )
    ai = FakeAIClient(transcriptions=["文档与附件合并转写"])

    outcome, feishu, ai, *_ = await execute(
        config=config,
        fields=record,
        feishu=feishu,
        collector=content_collector,
        ai=ai,
    )

    assert outcome is WorkflowOutcome.COMPLETED
    assert len(captured_materials) == 2
    assert len(ai.score_payloads) == 1
    assert len(ai.score_payloads[0].pdf_files) == 1
    assert ai.score_payloads[0].pdf_files[0][0] == b"composite-bundle"
    assert "补充业务目标" in ai.score_payloads[0].text
    assert (
        feishu.records[("app", "table", "record")][FIELD_DOC_CACHE]
        == "文档与附件合并转写"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "rounds", "score", "expected_status", "expected_rounds"),
    [
        (TriggerSource.INITIAL_EVENT, 0, 60, "未通过", 0),
        (TriggerSource.USER_RESCORE, 3, 60, "未通过", 4),
        (TriggerSource.USER_RESCORE, 4, 60, "已驳回", 5),
        (TriggerSource.USER_RESCORE, 4, 80, "已通过", 5),
        (TriggerSource.ADMIN_RETRY, 4, 60, "未通过", 4),
    ],
)
async def test_round_and_terminal_status_rules(
    config,
    record_factory,
    valid_score_result,
    source,
    rounds,
    score,
    expected_status,
    expected_rounds,
) -> None:
    result = {
        **valid_score_result,
        "score": score,
        "dimensions": (
            valid_score_result["dimensions"]
            if score == 80
            else {"completeness": 18, "logic": 18, "format": 12, "quality": 12}
        ),
    }
    ai = FakeAIClient(score_results=[result], transcriptions=["cache"])

    _, feishu, *_ = await execute(
        config=config,
        fields=record_factory(status="待评分", rounds=rounds),
        source=source,
        ai=ai,
    )

    stored = feishu.records[("app", "table", "record")]
    assert stored[FIELD_SCORE_STATUS] == expected_status
    assert stored[FIELD_REVISION_ROUNDS] == expected_rounds


@pytest.mark.asyncio
async def test_final_write_failure_leaves_scoring_for_scavenger_and_alerts_admin(
    config,
    record_factory,
) -> None:
    feishu = FakeFeishuClient(
        {("app", "table", "record"): record_factory(status="待评分")}
    )
    feishu.outcomes.script("update_record", True, False, False, False)

    outcome, feishu, _, notifier, *_ = await execute(
        config=config,
        fields=record_factory(),
        feishu=feishu,
    )

    assert outcome is WorkflowOutcome.FINAL_WRITE_FAILED
    assert feishu.records[("app", "table", "record")][FIELD_SCORE_STATUS] == "评分中"
    assert [kind for kind, _ in notifier.notifications] == ["admin_error"]


@pytest.mark.asyncio
async def test_fencing_discards_zombie_result_before_final_write(config, record_factory) -> None:
    class ExpiringRegistry(TaskRegistry):
        def __init__(self):
            super().__init__()
            self.checks = 0

        def is_current(self, key, fence):
            self.checks += 1
            return self.checks < 3

    registry = ExpiringRegistry()

    outcome, feishu, *_ = await execute(
        config=config,
        fields=record_factory(status="待评分"),
        registry=registry,
    )

    assert outcome is WorkflowOutcome.STALE
    updates = [
        details for name, details in feishu.trace.calls if name == "feishu.update_record"
    ]
    assert len(updates) == 1
    assert updates[0]["fields"] == {FIELD_SCORE_STATUS: "评分中"}


@pytest.mark.asyncio
async def test_rescore_admission_and_workflow_each_read_latest_record_snapshot(
    config,
    record_factory,
) -> None:
    old = record_factory(status="未通过", docs="https://tenant/docx/old")
    latest = record_factory(status="未通过", docs="https://tenant/docx/latest")
    feishu = FakeFeishuClient()
    feishu.outcomes.script("get_record", old, latest)
    registry = TaskRegistry()
    collector = FakeCollector(CollectedContent("new", b"bundle"))
    workflow = ScoringWorkflow(
        config=config,
        feishu=feishu,
        ai=FakeAIClient(transcriptions=["cache"]),
        collector=collector,
        notifier=FakeNotifier(),
        registry=registry,
    )
    coordinator = RecordCoordinator(
        feishu=feishu,
        registry=registry,
        runner=workflow.run,
    )

    result = await coordinator.request_score(
        key=RecordKey("app", "table", "record"),
        source=TriggerSource.USER_RESCORE,
        actor_open_id="ou_submitter",
        callback_id="click",
    )
    assert result.status is RequestStatus.ACCEPTED
    task = registry.active_entry(RecordKey("app", "table", "record")).task
    assert task is not None
    await task

    assert collector.calls[0][FIELD_DOC_LINK] == "https://tenant/docx/latest"
