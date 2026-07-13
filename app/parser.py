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
from typing import Literal

logger = logging.getLogger(__name__)

# Word (docx) 的 MIME 常量
_MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# 支持解析为纯文本的附件 MIME 类型
SUPPORTED_MIME_TYPES = {
    "application/pdf",
    _MIME_DOCX,
    "text/markdown",
    "text/plain",
    "text/x-markdown",
}

# 允许直传的图片 MIME 类型（豆包稳定支持 jpeg/png/webp；gif/bmp 暂不放）
IMAGE_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
}

# 附件分类结果
AttachmentKind = Literal["pdf", "docx", "text", "image", "unsupported"]

# 飞书文档链接模式（按路径匹配，兼容 feishu.cn / larkoffice.com / bytedance.net
# 等各种域名）。kind 用于区分文档形态：
#   - wiki：知识库节点，token 是节点 token，需再解析出挂载文档的真实 obj_token；
#   - docx：新版文档，token 即 document_id，可直接读取/导出；
#   - docs：旧版文档。
_DOC_URL_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("wiki", re.compile(r"/wiki/([A-Za-z0-9_-]+)")),
    ("docx", re.compile(r"/docx/([A-Za-z0-9_-]+)")),
    ("docs", re.compile(r"/docs/([A-Za-z0-9_-]+)")),
]


def extract_document_ref(url: str) -> tuple[str | None, str]:
    """从飞书文档 URL 中解析出 (token, kind)。

    kind ∈ {"wiki", "docx", "docs"}；解析失败返回 (None, "")。
    wiki 链接的 token 是知识库节点 token，并非真实文档 token，调用方需再经
    Wiki API 解析出挂载文档的 obj_token/obj_type 才能读取或导出。
    """
    if not url:
        return None, ""
    for kind, pattern in _DOC_URL_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1), kind
    return None, ""


def extract_document_id(url: str) -> str | None:
    """从飞书文档 URL 中提取 token（不区分文档形态）。

    Args:
        url: 飞书文档链接，如 https://xxx.feishu.cn/docx/AbCdEfGhI...

    Returns:
        token 字符串，解析失败返回 None。
    """
    token, _ = extract_document_ref(url)
    return token


# 飞书 docx.raw_content 接口会把每张内嵌图片渲染成"裸文件名"独占一行
# （截图/粘贴图默认名 image.png，也可能是 image (1).png、截图.jpg 等）。这些
# 既不是用户撰写的真实内容，又会被评分模型当成"冗余占位符"扣格式分，故在进入
# 评分文本与缓存前统一清洗掉。仅匹配"整行就是一个图片文件名"的情况，避免误删
# 正文中顺带提到文件名的句子。
_IMAGE_PLACEHOLDER_LINE = re.compile(
    r"^[\w一-鿿.\-()（） ]{1,80}\.(?:png|jpe?g|gif|webp|bmp|svg|tiff?)$",
    re.IGNORECASE,
)


def strip_image_placeholders(text: str) -> str:
    """移除飞书 raw_content 里的裸图片占位符行（如独占一行的 image.png）。

    清洗后压缩多余空行，返回纯文本。空输入原样返回。
    """
    if not text:
        return text
    kept = [
        line
        for line in text.splitlines()
        if not _IMAGE_PLACEHOLDER_LINE.match(line.strip())
    ]
    cleaned = "\n".join(kept)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip("\n")


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
    """将附件归类为 pdf / docx / text / image / unsupported。

    用于格式白名单校验（Point 5）与按 provider 能力分流。缺失 MIME
    时用文件名扩展名兜底。

    Args:
        mime_type: 飞书返回的 MIME 类型（可能为空）。
        filename: 原始文件名。

    Returns:
        附件类别。docx/doc 都归为 "docx"（老 .doc 也交由后续转换/抽取处理）。
    """
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


def normalize_image_mime(mime_type: str, filename: str = "") -> str:
    """返回可直传的图片 MIME（缺失时按扩展名兜底），非图片返回空串。"""
    if mime_type in IMAGE_MIME_TYPES:
        return mime_type
    guessed = _guess_mime_from_filename(filename)
    return guessed if guessed in IMAGE_MIME_TYPES else ""


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
