from __future__ import annotations

import asyncio
from io import BytesIO

import pytest
from pypdf import PdfWriter

from app.content_collector import (
    CollectionLimits,
    ContentCollector,
    normalize_document_links,
)
from app.errors import FeishuMaterialError, FeishuTimeoutError
from app.field_mapping import FIELD_ATTACHMENT, FIELD_DOC_LINK, FIELD_TEXT_CONTENT
from app.models.content import MaterialGroup
from app.pdf_bundle import PdfBundleError
from app.workflow_errors import (
    DamagedMaterialError,
    ContentProcessingError,
    MaterialLimitError,
    ModelCapabilityError,
    NoFileMaterialError,
    UnsupportedMaterialError,
)
from tests.fakes import CallTrace, FakeFeishuClient


def valid_pdf(width: float = 100) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=width, height=100)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


async def async_value(value):
    return value


def fields(*, docs="", attachments=None, description="原始描述"):
    return {
        FIELD_DOC_LINK: docs,
        FIELD_ATTACHMENT: attachments or [],
        FIELD_TEXT_CONTENT: description,
    }


def attachment(token: str, name: str, mime: str, *, size: int = 100):
    return {
        "file_token": token,
        "name": name,
        "mime_type": mime,
        "url": f"https://open.feishu.cn/{token}",
        "size": size,
    }


def collector(
    feishu,
    *,
    supports_pdf=True,
    converter=None,
    bundle_builder=None,
    limits=None,
):
    kwargs = {
        "feishu": feishu,
        "supports_pdf_input": supports_pdf,
        "require_soffice": lambda: True,
    }
    if converter is not None:
        kwargs["converter"] = converter
    if bundle_builder is not None:
        kwargs["bundle_builder"] = bundle_builder
    if limits is not None:
        kwargs["limits"] = limits
    return ContentCollector(**kwargs)


def test_multiple_document_links_are_normalized_in_field_order() -> None:
    links = normalize_document_links(
        [
            "说明 https://tenant.feishu.cn/docx/doc_a 与 https://tenant.feishu.cn/docs/doc_b",
            {"text": "知识库标题", "link": "https://tenant.feishu.cn/wiki/wiki_c"},
        ]
    )

    assert [link.url.rsplit("/", 1)[-1] for link in links] == [
        "doc_a",
        "doc_b",
        "wiki_c",
    ]
    assert links[2].title == "知识库标题"


@pytest.mark.asyncio
async def test_wiki_and_direct_document_deduplicate_by_resolved_token() -> None:
    feishu = FakeFeishuClient()
    feishu.outcomes.script("get_wiki_node", ("doc_real", "docx"))
    feishu.outcomes.script("export_doc_to_pdf", valid_pdf())
    captured = []

    async def build(materials):
        captured.extend(materials)
        return b"bundle"

    result = await collector(feishu, bundle_builder=build).collect(
        fields(
            docs=[
                "https://tenant.feishu.cn/wiki/wiki_node",
                "https://tenant.feishu.cn/docx/doc_real",
            ]
        )
    )

    assert result.review_bundle_pdf == b"bundle"
    assert len(captured) == 1
    assert captured[0].title == "在线需求文档 1"
    assert feishu.trace.names.count("feishu.export_doc_to_pdf") == 1


@pytest.mark.asyncio
async def test_repeated_direct_document_keeps_first_position_and_exports_once() -> None:
    feishu = FakeFeishuClient()
    feishu.outcomes.script("export_doc_to_pdf", valid_pdf())
    captured = []

    async def build(materials):
        captured.extend(materials)
        return b"bundle"

    await collector(feishu, bundle_builder=build).collect(
        fields(
            docs=[
                {"text": "第一次", "link": "https://tenant.feishu.cn/docx/doc_same"},
                {"text": "第二次", "link": "https://tenant.feishu.cn/docx/doc_same"},
            ]
        )
    )

    assert len(captured) == 1
    assert captured[0].title == "第一次"
    assert feishu.trace.names.count("feishu.export_doc_to_pdf") == 1


@pytest.mark.asyncio
async def test_doc_and_docx_are_exported_but_other_document_types_are_rejected() -> None:
    feishu = FakeFeishuClient()
    feishu.outcomes.script("get_wiki_node", ("sheet_real", "sheet"))
    content_collector = collector(feishu, bundle_builder=lambda materials: None)

    with pytest.raises(UnsupportedMaterialError, match="类型不支持"):
        await content_collector.collect(
            fields(docs="https://tenant.feishu.cn/wiki/wiki_sheet")
        )
    assert "feishu.export_doc_to_pdf" not in feishu.trace.names


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "expected_type"),
    [
        ("https://tenant.feishu.cn/docs/legacy_doc", "doc"),
        ("https://tenant.feishu.cn/docx/new_doc", "docx"),
    ],
)
async def test_direct_doc_and_docx_enter_pdf_export(url, expected_type) -> None:
    feishu = FakeFeishuClient()
    feishu.outcomes.script("export_doc_to_pdf", valid_pdf())

    await collector(
        feishu,
        bundle_builder=lambda materials: async_value(b"bundle"),
    ).collect(fields(docs=url))

    call = next(
        details
        for name, details in feishu.trace.calls
        if name == "feishu.export_doc_to_pdf"
    )
    assert call["doc_type"] == expected_type


@pytest.mark.asyncio
async def test_parallel_document_exports_keep_original_order() -> None:
    release_first = asyncio.Event()
    second_done = asyncio.Event()

    class OutOfOrderFeishu(FakeFeishuClient):
        async def export_doc_to_pdf(self, doc_token, doc_type="docx"):
            self.trace.add("feishu.export_doc_to_pdf", doc_token=doc_token)
            if doc_token == "doc_first":
                await second_done.wait()
                await release_first.wait()
                return valid_pdf(101)
            second_done.set()
            return valid_pdf(102)

    feishu = OutOfOrderFeishu()
    captured = []

    async def build(materials):
        captured.extend(materials)
        return b"bundle"

    task = asyncio.create_task(
        collector(feishu, bundle_builder=build).collect(
            fields(
                docs=[
                    "https://tenant.feishu.cn/docx/doc_first",
                    "https://tenant.feishu.cn/docx/doc_second",
                ]
            )
        )
    )
    await second_done.wait()
    release_first.set()
    await task

    assert [material.sequence for material in captured] == [0, 1]
    assert [material.title for material in captured] == [
        "在线需求文档 1",
        "在线需求文档 2",
    ]


@pytest.mark.asyncio
async def test_any_document_failure_aborts_complete_collection() -> None:
    feishu = FakeFeishuClient()
    feishu.outcomes.script(
        "export_doc_to_pdf",
        FeishuMaterialError(
            "文档已删除",
            operation="export_doc_to_pdf",
            resource_id="doc_bad",
        ),
        valid_pdf(),
    )
    bundle_calls = 0

    async def build(materials):
        nonlocal bundle_calls
        bundle_calls += 1
        return b"bundle"

    with pytest.raises(FeishuMaterialError):
        await collector(feishu, bundle_builder=build).collect(
            fields(
                docs=[
                    "https://tenant.feishu.cn/docx/doc_bad",
                    "https://tenant.feishu.cn/docx/doc_good",
                ]
            )
        )
    assert bundle_calls == 0


@pytest.mark.asyncio
async def test_duplicate_attachment_token_is_downloaded_and_bundled_once() -> None:
    feishu = FakeFeishuClient()
    feishu.outcomes.script("download_attachment", valid_pdf())
    captured = []

    async def build(materials):
        captured.extend(materials)
        return b"bundle"

    duplicate = attachment("file_same", "需求.pdf", "application/pdf")
    await collector(feishu, bundle_builder=build).collect(
        fields(attachments=[duplicate, duplicate])
    )

    assert feishu.trace.names.count("feishu.download_attachment") == 1
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_unsupported_attachment_precheck_stops_before_any_download() -> None:
    feishu = FakeFeishuClient()
    with pytest.raises(UnsupportedMaterialError) as exc_info:
        await collector(feishu).collect(
            fields(
                docs="https://tenant.feishu.cn/docx/doc_ok",
                attachments=[
                    attachment("ok", "ok.pdf", "application/pdf"),
                    attachment("bad1", "data.xlsx", "application/vnd.ms-excel"),
                    attachment("bad2", "archive.zip", "application/zip"),
                ],
            )
        )

    assert [problem.name for problem in exc_info.value.problems] == [
        "data.xlsx",
        "archive.zip",
    ]
    assert "feishu.download_attachment" not in feishu.trace.names
    assert "feishu.export_doc_to_pdf" not in feishu.trace.names


@pytest.mark.asyncio
async def test_word_text_and_image_all_convert_to_pdf_and_keep_attachment_order() -> None:
    feishu = FakeFeishuClient()
    feishu.outcomes.script("download_attachment", b"doc", b"md", b"png")
    converted = []
    captured = []

    async def convert(data, name):
        converted.append(name)
        return valid_pdf()

    async def build(materials):
        captured.extend(materials)
        return b"bundle"

    await collector(
        feishu,
        converter=convert,
        bundle_builder=build,
    ).collect(
        fields(
            attachments=[
                attachment("doc", "a.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                attachment("md", "b.md", "text/markdown"),
                attachment("png", "c.png", "image/png"),
            ]
        )
    )

    assert converted == ["a.docx", "b.md", "c.png"]
    assert [material.title for material in captured] == ["a.docx", "b.md", "c.png"]
    assert all(material.group is MaterialGroup.ATTACHMENT for material in captured)


@pytest.mark.asyncio
async def test_conversion_failure_retries_without_text_or_image_fallback() -> None:
    feishu = FakeFeishuClient()
    feishu.outcomes.script("download_attachment", b"broken")
    attempts = 0

    async def convert(data, name):
        nonlocal attempts
        attempts += 1
        return None

    with pytest.raises(DamagedMaterialError):
        await collector(feishu, converter=convert).collect(
            fields(
                attachments=[
                    attachment(
                        "doc",
                        "broken.docx",
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                ]
            )
        )

    assert attempts == 3
    assert "feishu.get_doc_raw_content" not in feishu.trace.names


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "mime"),
    [
        ("broken.txt", "text/plain"),
        ("broken.md", "text/markdown"),
        ("broken.png", "image/png"),
        ("broken.jpg", "image/jpeg"),
        ("broken.jpeg", "image/jpeg"),
        ("broken.webp", "image/webp"),
    ],
)
async def test_text_and_image_conversion_failures_never_use_modal_fallback(
    name,
    mime,
) -> None:
    feishu = FakeFeishuClient()
    feishu.outcomes.script("download_attachment", b"broken")
    attempts = 0

    async def convert(data, filename):
        nonlocal attempts
        attempts += 1
        return None

    with pytest.raises(DamagedMaterialError):
        await collector(feishu, converter=convert).collect(
            fields(attachments=[attachment("bad", name, mime)])
        )

    assert attempts == 3


@pytest.mark.asyncio
async def test_damaged_and_encrypted_pdf_are_user_fixable_material_errors() -> None:
    encrypted_writer = PdfWriter()
    encrypted_writer.add_blank_page(width=100, height=100)
    encrypted_writer.encrypt("secret")
    encrypted = BytesIO()
    encrypted_writer.write(encrypted)

    feishu = FakeFeishuClient()
    feishu.outcomes.script("download_attachment", b"not-pdf", encrypted.getvalue())

    with pytest.raises(DamagedMaterialError) as exc_info:
        await collector(feishu).collect(
            fields(
                attachments=[
                    attachment("bad", "bad.pdf", "application/pdf"),
                    attachment("encrypted", "encrypted.pdf", "application/pdf"),
                ]
            )
        )

    assert [problem.name for problem in exc_info.value.problems] == [
        "bad.pdf",
        "encrypted.pdf",
    ]


@pytest.mark.asyncio
async def test_only_description_is_not_scorable() -> None:
    with pytest.raises(NoFileMaterialError):
        await collector(FakeFeishuClient()).collect(fields(description="只有描述"))


@pytest.mark.asyncio
async def test_text_only_model_is_rejected_before_external_collection() -> None:
    feishu = FakeFeishuClient()

    with pytest.raises(ModelCapabilityError):
        await collector(feishu, supports_pdf=False).collect(
            fields(docs="https://tenant.feishu.cn/docx/doc_a")
        )

    assert feishu.trace.calls == []


@pytest.mark.asyncio
async def test_transient_retry_repeats_only_failed_download_step() -> None:
    trace = CallTrace()
    feishu = FakeFeishuClient(trace=trace)
    feishu.outcomes.script(
        "export_doc_to_pdf",
        valid_pdf(),
    )
    feishu.outcomes.script(
        "download_attachment",
        FeishuTimeoutError("timeout", operation="download_attachment"),
        FeishuTimeoutError("timeout", operation="download_attachment"),
        b"docx",
    )
    conversions = 0
    bundles = 0

    async def convert(data, name):
        nonlocal conversions
        conversions += 1
        return valid_pdf()

    async def build(materials):
        nonlocal bundles
        bundles += 1
        return b"bundle"

    await collector(
        feishu,
        converter=convert,
        bundle_builder=build,
    ).collect(
        fields(
            docs="https://tenant.feishu.cn/docx/doc_a",
            attachments=[
                attachment(
                    "doc",
                    "a.docx",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            ],
        )
    )

    assert trace.names.count("feishu.export_doc_to_pdf") == 1
    assert trace.names.count("feishu.download_attachment") == 3
    assert conversions == 1
    assert bundles == 1


@pytest.mark.asyncio
async def test_transient_document_export_retries_only_export_step() -> None:
    feishu = FakeFeishuClient()
    feishu.outcomes.script(
        "export_doc_to_pdf",
        FeishuTimeoutError("timeout", operation="export_doc_to_pdf"),
        FeishuTimeoutError("timeout", operation="export_doc_to_pdf"),
        valid_pdf(),
    )
    bundles = 0

    async def build(materials):
        nonlocal bundles
        bundles += 1
        return b"bundle"

    await collector(feishu, bundle_builder=build).collect(
        fields(docs="https://tenant.feishu.cn/docx/doc_retry")
    )

    assert feishu.trace.names.count("feishu.export_doc_to_pdf") == 3
    assert bundles == 1


@pytest.mark.asyncio
async def test_bundle_failure_retries_bundle_only_and_never_returns_partial_content() -> None:
    feishu = FakeFeishuClient()
    feishu.outcomes.script("download_attachment", valid_pdf())
    attempts = 0

    async def fail_bundle(materials):
        nonlocal attempts
        attempts += 1
        raise PdfBundleError("merge failed")

    with pytest.raises(ContentProcessingError):
        await collector(feishu, bundle_builder=fail_bundle).collect(
            fields(
                attachments=[attachment("pdf", "a.pdf", "application/pdf")]
            )
        )

    assert attempts == 3
    assert feishu.trace.names.count("feishu.download_attachment") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["docs", "attachments", "both"])
async def test_docs_attachments_or_both_can_build_bundle(mode) -> None:
    feishu = FakeFeishuClient()
    docs = ""
    attachments = []
    if mode in {"docs", "both"}:
        docs = "https://tenant.feishu.cn/docx/doc_a"
        feishu.outcomes.script("export_doc_to_pdf", valid_pdf())
    if mode in {"attachments", "both"}:
        attachments = [attachment("pdf", "a.pdf", "application/pdf")]
        feishu.outcomes.script("download_attachment", valid_pdf())
    captured = []

    async def build(materials):
        captured.extend(materials)
        return b"bundle"

    result = await collector(feishu, bundle_builder=build).collect(
        fields(docs=docs, attachments=attachments)
    )

    assert result.review_bundle_pdf == b"bundle"
    assert len(captured) == (2 if mode == "both" else 1)


@pytest.mark.asyncio
async def test_attachment_limits_are_checked_before_download() -> None:
    feishu = FakeFeishuClient()
    limits = CollectionLimits(
        max_attachment_count=1,
        max_single_attachment_bytes=10,
        max_total_attachment_bytes=15,
        max_image_count=1,
    )

    with pytest.raises(MaterialLimitError):
        await collector(feishu, limits=limits).collect(
            fields(
                attachments=[
                    attachment("a", "a.pdf", "application/pdf", size=11),
                    attachment("b", "b.pdf", "application/pdf", size=11),
                ]
            )
        )

    assert "feishu.download_attachment" not in feishu.trace.names
