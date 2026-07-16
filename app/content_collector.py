"""在线文档与附件的完整、不可降级内容采集。"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Awaitable, Callable, Iterable

from pypdf import PdfReader

from app.docx_convert import attachment_to_pdf, soffice_available
from app.field_mapping import FIELD_ATTACHMENT, FIELD_DOC_LINK, FIELD_TEXT_CONTENT
from app.models.content import CollectedContent, MaterialGroup, PdfMaterial
from app.parser import classify_attachment, extract_document_ref
from app.pdf_bundle import PdfBundleError, build_pdf_bundle
from app.retry import retry_step
from app.workflow_errors import (
    ContentProcessingError,
    DamagedMaterialError,
    MaterialLimitError,
    MaterialProblem,
    ModelCapabilityError,
    NoFileMaterialError,
    UnsupportedMaterialError,
    UserMaterialError,
)

SUPPORTED_DOC_EXPORT_TYPES = frozenset({"doc", "docx"})
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CollectionLimits:
    max_attachment_count: int = 20
    max_single_attachment_bytes: int = 20 * 1024 * 1024
    max_total_attachment_bytes: int = 100 * 1024 * 1024
    max_pdf_pages: int = 300
    max_image_count: int = 20


@dataclass(frozen=True, slots=True)
class DocumentLink:
    url: str
    title: str
    sequence: int


@dataclass(frozen=True, slots=True)
class ResolvedDocument:
    token: str
    doc_type: str
    title: str
    sequence: int


@dataclass(frozen=True, slots=True)
class AttachmentRef:
    file_token: str
    name: str
    mime_type: str
    url: str
    size: int
    sequence: int
    kind: str


def normalize_document_links(value: Any) -> list[DocumentLink]:
    """按字段出现顺序提取字符串、超链接对象或列表中的全部 URL。"""
    collected: list[tuple[str, str]] = []

    def visit(item: Any, inherited_title: str = "") -> None:
        if isinstance(item, str):
            for match in _URL_RE.finditer(item):
                url = match.group(0).rstrip(".,;:!?，。；：！？)]}）】")
                collected.append((url, inherited_title))
            return
        if isinstance(item, dict):
            title = str(item.get("text") or item.get("title") or inherited_title or "")
            direct = item.get("link") or item.get("url") or item.get("href")
            if isinstance(direct, str) and direct:
                visit(direct, title)
                return
            for key, nested in item.items():
                if key not in {"text", "title"}:
                    visit(nested, title)
            if not direct and isinstance(item.get("text"), str):
                visit(item["text"], title)
            return
        if isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested, inherited_title)

    visit(value)
    return [
        DocumentLink(
            url=url,
            title=title.strip() if title.strip() and title.strip() != url else "",
            sequence=index,
        )
        for index, (url, title) in enumerate(collected)
    ]


class ContentCollector:
    def __init__(
        self,
        *,
        feishu: Any,
        supports_pdf_input: bool | Callable[[], bool],
        limits: CollectionLimits | None = None,
        max_attempts: int = 3,
        converter: Callable[[bytes, str], Awaitable[bytes | None]] = attachment_to_pdf,
        bundle_builder: Callable[[Iterable[PdfMaterial]], Awaitable[bytes]] = build_pdf_bundle,
        require_soffice: Callable[[], bool] = soffice_available,
    ) -> None:
        self._feishu = feishu
        self._supports_pdf_input = supports_pdf_input
        self._limits = limits or CollectionLimits()
        self._max_attempts = max_attempts
        self._converter = converter
        self._bundle_builder = bundle_builder
        self._require_soffice = require_soffice

    async def collect(self, fields: dict[str, Any]) -> CollectedContent:
        links = normalize_document_links(fields.get(FIELD_DOC_LINK, ""))
        attachments = self._normalize_attachments(fields.get(FIELD_ATTACHMENT, []))
        self._precheck_attachments(attachments)

        if not links and not attachments:
            raise NoFileMaterialError("请至少上传一个在线需求文档或附件")
        if not self._file_capable():
            raise ModelCapabilityError("当前模型不支持总 PDF 文件输入")

        # 任何非 PDF 附件以及来源分隔页都依赖 LibreOffice。
        needs_conversion = any(ref.kind != "pdf" for ref in attachments) or bool(
            links or attachments
        )
        if needs_conversion and not self._require_soffice():
            raise ModelCapabilityError("LibreOffice 不可用，无法生成统一总 PDF")

        documents = await self._resolve_documents(links)
        document_materials = await self._export_documents(documents)
        attachment_materials = await self._collect_attachments(attachments)
        materials = [*document_materials, *attachment_materials]

        async def build() -> bytes:
            try:
                return await self._bundle_builder(materials)
            except PdfBundleError as exc:
                raise ContentProcessingError(str(exc)) from exc

        bundle = await retry_step(
            "build_pdf_bundle",
            build,
            max_attempts=self._max_attempts,
        )
        return CollectedContent(
            original_description=str(fields.get(FIELD_TEXT_CONTENT, "") or ""),
            review_bundle_pdf=bundle,
        )

    def _file_capable(self) -> bool:
        capability = self._supports_pdf_input
        return bool(capability() if callable(capability) else capability)

    async def _resolve_documents(
        self,
        links: list[DocumentLink],
    ) -> list[ResolvedDocument]:
        async def resolve(link: DocumentLink) -> ResolvedDocument:
            token, kind = extract_document_ref(link.url)
            if not token:
                raise UnsupportedMaterialError(
                    "在线需求文档链接无效或类型不受支持",
                    problems=(MaterialProblem(link.title or link.url, "链接或类型不支持"),),
                )
            doc_token = token
            doc_type = "docx" if kind == "docx" else "doc"
            if kind == "wiki":
                doc_token, doc_type = await retry_step(
                    "resolve_wiki_node",
                    lambda: self._feishu.get_wiki_node(token),
                    max_attempts=self._max_attempts,
                )
            if doc_type not in SUPPORTED_DOC_EXPORT_TYPES:
                raise UnsupportedMaterialError(
                    "在线需求文档类型不支持 PDF 导出",
                    problems=(
                        MaterialProblem(
                            link.title or f"在线需求文档 {link.sequence + 1}",
                            f"类型 {doc_type} 不支持",
                        ),
                    ),
                )
            return ResolvedDocument(
                token=doc_token,
                doc_type=doc_type,
                title=link.title or f"在线需求文档 {link.sequence + 1}",
                sequence=link.sequence,
            )

        results = await asyncio.gather(
            *(resolve(link) for link in links),
            return_exceptions=True,
        )
        self._raise_collected_errors(results)
        unique: list[ResolvedDocument] = []
        seen: set[str] = set()
        for result in results:
            assert isinstance(result, ResolvedDocument)
            if result.token in seen:
                continue
            seen.add(result.token)
            unique.append(result)
        return unique

    async def _export_documents(
        self,
        documents: list[ResolvedDocument],
    ) -> list[PdfMaterial]:
        async def export(document: ResolvedDocument) -> PdfMaterial:
            async def call() -> bytes:
                data = await self._feishu.export_doc_to_pdf(
                    document.token,
                    document.doc_type,
                )
                if not data:
                    raise ContentProcessingError("在线文档 PDF 导出结果为空")
                return data

            data = await retry_step(
                "export_doc_to_pdf",
                call,
                max_attempts=self._max_attempts,
            )
            self._validate_pdf(data, document.title)
            return PdfMaterial(
                sequence=document.sequence,
                group=MaterialGroup.ONLINE_DOC,
                title=document.title,
                pdf_bytes=data,
            )

        results = await asyncio.gather(
            *(export(document) for document in documents),
            return_exceptions=True,
        )
        self._raise_collected_errors(results)
        return [result for result in results if isinstance(result, PdfMaterial)]

    def _normalize_attachments(self, value: Any) -> list[AttachmentRef]:
        if not isinstance(value, list):
            return []
        unique: list[AttachmentRef] = []
        seen_tokens: set[str] = set()
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                continue
            token = str(item.get("file_token", "") or "")
            if token and token in seen_tokens:
                continue
            if token:
                seen_tokens.add(token)
            name = str(item.get("name", "") or "（未命名文件）")
            mime_type = str(item.get("mime_type", "") or "")
            unique.append(
                AttachmentRef(
                    file_token=token,
                    name=name,
                    mime_type=mime_type,
                    url=str(item.get("url") or item.get("tmp_url") or ""),
                    size=int(item.get("size", 0) or 0),
                    sequence=index,
                    kind=classify_attachment(mime_type, name),
                )
            )
        return unique

    def _precheck_attachments(self, attachments: list[AttachmentRef]) -> None:
        unsupported = [
            MaterialProblem(ref.name, "格式不支持")
            for ref in attachments
            if ref.kind == "unsupported"
        ]
        if unsupported:
            raise UnsupportedMaterialError(
                "存在不支持格式的附件",
                problems=tuple(unsupported),
            )

        problems: list[MaterialProblem] = []
        limits = self._limits
        if len(attachments) > limits.max_attachment_count:
            problems.append(
                MaterialProblem(
                    "需求附件",
                    f"附件数量超过上限 {limits.max_attachment_count}",
                )
            )
        total = sum(max(ref.size, 0) for ref in attachments)
        if total > limits.max_total_attachment_bytes:
            problems.append(MaterialProblem("需求附件", "附件总大小超过上限"))
        image_count = sum(ref.kind == "image" for ref in attachments)
        if image_count > limits.max_image_count:
            problems.append(MaterialProblem("需求附件", "图片数量超过上限"))
        for ref in attachments:
            if ref.size > limits.max_single_attachment_bytes:
                problems.append(MaterialProblem(ref.name, "单附件大小超过上限"))
            if not ref.url and not ref.file_token:
                problems.append(MaterialProblem(ref.name, "缺少可下载地址"))
        if problems:
            raise MaterialLimitError("附件资源限制校验失败", problems=tuple(problems))

    async def _collect_attachments(
        self,
        attachments: list[AttachmentRef],
    ) -> list[PdfMaterial]:
        downloaded_total = 0
        total_lock = asyncio.Lock()

        async def collect_one(ref: AttachmentRef) -> PdfMaterial:
            nonlocal downloaded_total
            data = await retry_step(
                "download_attachment",
                lambda: self._feishu.download_attachment(
                    ref.url,
                    file_token=ref.file_token,
                    max_bytes=self._limits.max_single_attachment_bytes,
                ),
                max_attempts=self._max_attempts,
            )
            if len(data) > self._limits.max_single_attachment_bytes:
                raise MaterialLimitError(
                    "下载后的附件超过大小上限",
                    problems=(MaterialProblem(ref.name, "单附件大小超过上限"),),
                )
            async with total_lock:
                downloaded_total += len(data)
                if downloaded_total > self._limits.max_total_attachment_bytes:
                    raise MaterialLimitError(
                        "附件实际下载总大小超过上限",
                        problems=(MaterialProblem("需求附件", "附件总大小超过上限"),),
                    )

            if ref.kind == "pdf":
                pdf = data
            else:
                pdf = None
                for _ in range(self._max_attempts):
                    pdf = await self._converter(data, ref.name)
                    if pdf:
                        break
                if not pdf:
                    raise DamagedMaterialError(
                        "附件无法转换为 PDF",
                        problems=(MaterialProblem(ref.name, "损坏或无法转换"),),
                    )
            self._validate_pdf(pdf, ref.name)
            return PdfMaterial(
                sequence=ref.sequence,
                group=MaterialGroup.ATTACHMENT,
                title=ref.name,
                pdf_bytes=pdf,
            )

        results = await asyncio.gather(
            *(collect_one(ref) for ref in attachments),
            return_exceptions=True,
        )
        self._raise_collected_errors(results)
        return [result for result in results if isinstance(result, PdfMaterial)]

    def _validate_pdf(self, data: bytes, name: str) -> None:
        try:
            reader = PdfReader(BytesIO(data), strict=False)
            if reader.is_encrypted:
                raise ValueError("PDF 已加密")
            pages = len(reader.pages)
            if pages < 1:
                raise ValueError("PDF 没有有效页面")
            if pages > self._limits.max_pdf_pages:
                raise MaterialLimitError(
                    "PDF 页数超过上限",
                    problems=(MaterialProblem(name, "PDF 页数超过上限"),),
                )
        except MaterialLimitError:
            raise
        except Exception as exc:
            raise DamagedMaterialError(
                "材料 PDF 损坏、加密或无法解析",
                problems=(MaterialProblem(name, str(exc)),),
            ) from exc

    @staticmethod
    def _raise_collected_errors(results: list[Any]) -> None:
        errors = [result for result in results if isinstance(result, BaseException)]
        if not errors:
            return
        user_errors = [error for error in errors if isinstance(error, UserMaterialError)]
        if user_errors and len(user_errors) == len(errors):
            problems = tuple(
                problem
                for error in user_errors
                for problem in error.problems
            )
            first = user_errors[0]
            raise type(first)(str(first), problems=problems)
        first = errors[0]
        if isinstance(first, BaseException):
            raise first
