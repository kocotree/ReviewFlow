from __future__ import annotations

from dataclasses import FrozenInstanceError
from io import BytesIO

import pytest
from docx import Document
from pypdf import PdfReader, PdfWriter

from app import pdf_bundle
from app.models.content import CollectedContent, MaterialGroup, PdfMaterial


def _pdf_with_widths(*widths: float) -> bytes:
    writer = PdfWriter()
    for width in widths:
        writer.add_blank_page(width=width, height=700)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _encrypted_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("secret")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _zero_page_pdf() -> bytes:
    writer = PdfWriter()
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


async def test_bundle_uses_group_and_stable_explicit_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    materials = [
        PdfMaterial(0, MaterialGroup.ATTACHMENT, "附件甲", _pdf_with_widths(301)),
        PdfMaterial(2, MaterialGroup.ONLINE_DOC, "文档乙", _pdf_with_widths(202)),
        PdfMaterial(1, MaterialGroup.ONLINE_DOC, "文档甲", _pdf_with_widths(201)),
        # Equal group/sequence values retain input order rather than using title.
        PdfMaterial(0, MaterialGroup.ATTACHMENT, "附件乙", _pdf_with_widths(302)),
    ]
    separator_width = {
        "文档甲": 901,
        "文档乙": 902,
        "附件甲": 903,
        "附件乙": 904,
    }
    calls: list[tuple[str, str]] = []

    async def fake_separator(source_type: str, title: str) -> bytes:
        calls.append((source_type, title))
        return _pdf_with_widths(separator_width[title])

    monkeypatch.setattr(pdf_bundle, "_create_source_separator_pdf", fake_separator)

    bundle = await pdf_bundle.build_pdf_bundle(materials)
    reader = PdfReader(BytesIO(bundle))

    assert [float(page.mediabox.width) for page in reader.pages] == [
        901,
        201,
        902,
        202,
        903,
        301,
        904,
        302,
    ]
    assert calls == [
        ("在线需求文档", "文档甲"),
        ("在线需求文档", "文档乙"),
        ("附件", "附件甲"),
        ("附件", "附件乙"),
    ]


async def test_separator_contains_source_type_and_chinese_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_convert(data: bytes, filename: str) -> bytes:
        document = Document(BytesIO(data))
        captured["filename"] = filename
        captured["paragraphs"] = [paragraph.text for paragraph in document.paragraphs]
        captured["image_descriptions"] = [
            shape._inline.docPr.get("descr") for shape in document.inline_shapes
        ]
        return _pdf_with_widths(500)

    monkeypatch.setattr(pdf_bundle, "attachment_to_pdf", fake_convert)

    result = await pdf_bundle._create_source_separator_pdf(
        "在线需求文档", "中文需求标题"
    )

    assert captured["filename"] == "source-separator.docx"
    embedded_text = " ".join(
        [
            *captured["paragraphs"],  # type: ignore[arg-type]
            *captured["image_descriptions"],  # type: ignore[arg-type]
        ]
    )
    assert "在线需求文档" in embedded_text
    assert "中文需求标题" in embedded_text
    assert len(PdfReader(BytesIO(result)).pages) == 1


def test_separator_title_is_cleaned_and_truncated() -> None:
    cleaned = pdf_bundle.sanitize_source_title(
        "\x00 需求\n文档 https://example.test/tmp?token=secret "
        "file_token=hidden 终稿"
    )
    truncated = pdf_bundle.sanitize_source_title("中" * 200, max_chars=20)

    assert cleaned == "需求 文档 终稿"
    assert truncated == "中" * 17 + "..."
    assert len(truncated) == 20


async def test_separator_conversion_failure_aborts_the_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failed_conversion(data: bytes, filename: str) -> None:
        return None

    monkeypatch.setattr(pdf_bundle, "attachment_to_pdf", failed_conversion)
    material = PdfMaterial(
        0,
        MaterialGroup.ATTACHMENT,
        "损坏材料.docx",
        _pdf_with_widths(100),
    )

    with pytest.raises(pdf_bundle.PdfBundleError, match="来源分隔页生成失败"):
        await pdf_bundle.build_pdf_bundle([material])


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"not-a-pdf", "损坏或无法解析"),
        (_encrypted_pdf(), "已加密"),
        (_zero_page_pdf(), "不包含有效页面"),
    ],
)
async def test_invalid_material_pdf_raises_a_clear_error(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
    message: str,
) -> None:
    async def fake_separator(source_type: str, title: str) -> bytes:
        return _pdf_with_widths(500)

    monkeypatch.setattr(pdf_bundle, "_create_source_separator_pdf", fake_separator)
    material = PdfMaterial(0, MaterialGroup.ATTACHMENT, "问题材料", body)

    with pytest.raises(pdf_bundle.PdfBundleError, match=message):
        await pdf_bundle.build_pdf_bundle([material])


def test_collected_content_is_frozen_and_keeps_the_scoring_bundle() -> None:
    bundle = _pdf_with_widths(100)
    content = CollectedContent(
        original_description="这是总 PDF 之外的原始描述",
        review_bundle_pdf=bundle,
        collection_warnings=("提示",),
    )

    assert content.original_description == "这是总 PDF 之外的原始描述"
    assert content.review_bundle_pdf is bundle
    with pytest.raises(FrozenInstanceError):
        content.original_description = "不可修改"  # type: ignore[misc]
