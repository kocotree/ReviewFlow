"""文档/附件文本提取。

从以下来源提取纯文本：
- 飞书在线文档链接（提取 document_id）
- PDF 附件
- Word (docx) 附件
- Markdown / 纯文本附件
"""

import io
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# 支持解析的附件 MIME 类型
SUPPORTED_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/markdown",
    "text/plain",
    "text/x-markdown",
}

# 飞书文档链接模式
FEISHU_DOC_PATTERNS = [
    re.compile(r"feishu\.cn/docx/([A-Za-z0-9_-]+)"),
    re.compile(r"feishu\.cn/wiki/([A-Za-z0-9_-]+)"),
    re.compile(r"feishu\.cn/docs/([A-Za-z0-9_-]+)"),
    re.compile(r"bytedance\.net/docx/([A-Za-z0-9_-]+)"),
    re.compile(r"larkoffice\.com/docx/([A-Za-z0-9_-]+)"),
]


def extract_document_id(url: str) -> str | None:
    """从飞书文档 URL 中提取 document_id。

    Args:
        url: 飞书文档链接，如 https://xxx.feishu.cn/docx/AbCdEfGhI...

    Returns:
        document_id 字符串，解析失败返回 None。
    """
    if not url:
        return None
    for pattern in FEISHU_DOC_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None


def extract_text_from_attachment(
    file_data: bytes,
    mime_type: str,
    filename: str = "",
) -> str | None:
    """从附件字节内容中提取纯文本。

    Args:
        file_data: 附件文件的字节内容。
        mime_type: 文件的 MIME 类型。
        filename: 原始文件名（用于判断扩展名兜底）。

    Returns:
        提取到的文本，失败返回 None。
    """
    # 如果没有 MIME 类型，尝试从文件名推断
    if not mime_type and filename:
        mime_type = _guess_mime_from_filename(filename)

    if mime_type not in SUPPORTED_MIME_TYPES:
        logger.warning("不支持的附件格式: mime=%s file=%s", mime_type, filename)
        return None

    try:
        if mime_type == "application/pdf":
            return _extract_from_pdf(file_data)
        elif mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            return _extract_from_docx(file_data)
        elif mime_type in ("text/markdown", "text/x-markdown", "text/plain"):
            return _extract_from_text(file_data)
    except Exception as e:
        logger.error("附件文本提取失败: file=%s error=%s", filename, e)
        return None

    return None


def _guess_mime_from_filename(filename: str) -> str:
    """从文件名推断 MIME 类型。"""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    mapping = {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "md": "text/markdown",
        "txt": "text/plain",
        "markdown": "text/markdown",
    }
    return mapping.get(ext, "")


def _extract_from_pdf(data: bytes) -> str | None:
    """从 PDF 字节数据中提取文本。"""
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages: list[str] = []
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
        if not pages:
            logger.warning("PDF 中未提取到文本内容")
            return None
        return "\n\n".join(pages)


def _extract_from_docx(data: bytes) -> str | None:
    """从 Word (docx) 字节数据中提取文本。"""
    from docx import Document

    doc = Document(io.BytesIO(data))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    if not paragraphs:
        logger.warning("Word 文档中未提取到文本内容")
        return None
    return "\n\n".join(paragraphs)


def _extract_from_text(data: bytes) -> str | None:
    """从纯文本/Markdown 字节数据中读取内容。"""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("gbk")
        except UnicodeDecodeError:
            logger.warning("文本文件编码识别失败")
            return None


def truncate_content(
    text: str,
    max_chars: int = 8000,
) -> str:
    """智能截断文本，保留前 60% 和后 20%，防止 AI 上下文超长。

    Args:
        text: 原始文本。
        max_chars: 最大字符数（以英文单词数粗略估算不会超过上下文窗口）。

    Returns:
        截断后的文本。若未超出限制则返回原文。
    """
    if len(text) <= max_chars:
        return text

    head_len = int(max_chars * 0.6)
    tail_len = int(max_chars * 0.2)
    omitted = len(text) - head_len - tail_len

    return (
        text[:head_len]
        + f"\n\n...[中间省略 {omitted} 字符]...\n\n"
        + text[-tail_len:]
    )
