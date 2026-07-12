"""docx/doc → PDF 转换（基于 LibreOffice headless）。

豆包 Chat API 的文件模态「当前仅支持 PDF」，docx/doc 不能直传，因此在
把 Word 文档发给豆包前需先转成 PDF。转换用系统的 LibreOffice
(`soffice --headless --convert-to pdf`) 完成。

上线关键点（见设计打磨结论）：
- soffice 是同步阻塞子进程，用 asyncio.to_thread 丢到线程池，避免冻结
  FastAPI 事件循环。
- soffice 默认同一用户配置目录只允许一个实例，并发会互相锁死；每次调用
  用独立的 -env:UserInstallation 临时目录规避。
- 损坏文档可能让 soffice 永久挂起，设 timeout 并杀掉整个进程组
  （soffice 会 fork 子进程）。

任一失败返回 None，由调用方回退到 python-docx 抽文本。
"""

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# LibreOffice 可执行文件名候选（不同发行版/系统命名不同）
_SOFFICE_CANDIDATES = ("soffice", "libreoffice")

# 单次转换超时（秒）。含 soffice 冷启动，给足余量。
_CONVERT_TIMEOUT = 60


def _find_soffice() -> str | None:
    """定位 soffice 可执行文件路径，找不到返回 None。"""
    for name in _SOFFICE_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def _convert_sync(data: bytes, filename: str) -> bytes | None:
    """同步执行 docx→PDF 转换（在线程池中运行）。"""
    soffice = _find_soffice()
    if not soffice:
        logger.error("未找到 LibreOffice(soffice)，无法转换 docx→PDF")
        return None

    # 每次转换使用独立临时目录：放源文件、产物、以及 soffice 用户配置。
    work_dir = Path(tempfile.mkdtemp(prefix="docx2pdf_"))
    profile_dir = work_dir / "lo_profile"

    # 保留原始扩展名，让 soffice 正确识别输入格式（.doc / .docx）。
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "docx"
    if ext not in ("doc", "docx"):
        ext = "docx"
    src_path = work_dir / f"source.{ext}"
    src_path.write_bytes(data)

    cmd = [
        soffice,
        "--headless",
        "--norestore",
        f"-env:UserInstallation=file://{profile_dir}",
        "--convert-to",
        "pdf",
        "--outdir",
        str(work_dir),
        str(src_path),
    ]

    proc: subprocess.Popen | None = None
    try:
        # start_new_session=True 使子进程成为新进程组组长，便于超时时整组杀死。
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            _, stderr = proc.communicate(timeout=_CONVERT_TIMEOUT)
        except subprocess.TimeoutExpired:
            logger.error("docx→PDF 转换超时: file=%s", filename)
            _kill_process_group(proc)
            return None

        if proc.returncode != 0:
            logger.error(
                "docx→PDF 转换失败: file=%s rc=%s stderr=%s",
                filename, proc.returncode,
                stderr.decode("utf-8", "ignore")[:300] if stderr else "",
            )
            return None

        pdf_path = src_path.with_suffix(".pdf")
        if not pdf_path.exists():
            logger.error("docx→PDF 未生成产物: file=%s", filename)
            return None
        return pdf_path.read_bytes()
    except Exception as e:
        logger.error("docx→PDF 转换异常: file=%s error=%s", filename, e)
        if proc is not None and proc.poll() is None:
            _kill_process_group(proc)
        return None
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _kill_process_group(proc: subprocess.Popen) -> None:
    """杀掉子进程所在的整个进程组（soffice 会 fork 子进程）。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        # 兜底：至少杀主进程
        try:
            proc.kill()
        except Exception:
            pass


async def docx_to_pdf(data: bytes, filename: str) -> bytes | None:
    """将 docx/doc 字节内容转换为 PDF 字节内容。

    在线程池中执行阻塞的 soffice 调用，避免阻塞事件循环。

    Args:
        data: Word 文档字节内容。
        filename: 原始文件名（用于识别 .doc/.docx 扩展名）。

    Returns:
        PDF 字节内容，失败返回 None（调用方应回退到文本抽取）。
    """
    return await asyncio.to_thread(_convert_sync, data, filename)


def soffice_available() -> bool:
    """当前环境是否可用 LibreOffice（用于启动自检/日志）。"""
    return _find_soffice() is not None
