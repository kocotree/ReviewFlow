"""在线文档链接、附件类型和文本长度的轻量解析工具。"""

from __future__ import annotations

import re
from typing import Literal

_MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}
AttachmentKind = Literal["pdf", "docx", "text", "image", "unsupported"]

_DOC_URL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("wiki", re.compile(r"/wiki/([A-Za-z0-9_-]+)")),
    ("docx", re.compile(r"/docx/([A-Za-z0-9_-]+)")),
    ("docs", re.compile(r"/docs/([A-Za-z0-9_-]+)")),
]


def extract_document_ref(url: str) -> tuple[str | None, str]:
    """从飞书 URL 解析 ``(token, wiki/docx/docs)``。"""
    if not url:
        return None, ""
    for kind, pattern in _DOC_URL_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1), kind
    return None, ""


def extract_document_id(url: str) -> str | None:
    token, _ = extract_document_ref(url)
    return token


def _guess_mime_from_filename(filename: str) -> str:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    mapping = {
        "pdf": "application/pdf",
        "docx": _MIME_DOCX,
        "doc": "application/msword",
        "md": "text/markdown",
        "txt": "text/plain",
        "markdown": "text/markdown",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
    }
    return mapping.get(ext, "")


def classify_attachment(mime_type: str, filename: str = "") -> AttachmentKind:
    """把所有受支持格式归入统一转 PDF 流程。"""
    if not mime_type and filename:
        mime_type = _guess_mime_from_filename(filename)
    if mime_type == "application/pdf":
        return "pdf"
    if mime_type in (_MIME_DOCX, "application/msword"):
        return "docx"
    if mime_type in ("text/markdown", "text/x-markdown", "text/plain"):
        return "text"
    if mime_type in IMAGE_MIME_TYPES:
        return "image"
    return "unsupported"
