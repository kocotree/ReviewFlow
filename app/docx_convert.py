"""Convert supported attachments to PDF with LibreOffice headless.

LibreOffice is intentionally kept behind one small component because it is a
blocking external process and because concurrent ``soffice`` processes can be
expensive.  Every conversion gets an isolated user profile, a hard timeout,
and deterministic temporary-file cleanup.

``docx_to_pdf`` is retained as a compatibility wrapper for the existing
orchestrator.  New code should call ``attachment_to_pdf``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# LibreOffice executable names differ between distributions.
_SOFFICE_CANDIDATES = ("soffice", "libreoffice")

# PDF inputs already satisfy the component contract and are returned unchanged.
PASSTHROUGH_EXTENSIONS = frozenset({".pdf"})
CONVERTIBLE_EXTENSIONS = frozenset(
    {".doc", ".docx", ".txt", ".md", ".png", ".jpg", ".jpeg", ".webp"}
)
SUPPORTED_ATTACHMENT_EXTENSIONS = PASSTHROUGH_EXTENSIONS | CONVERTIBLE_EXTENSIONS

# A corrupt input can leave soffice waiting forever.  Keep both the timeout and
# the global concurrency limit close to the process boundary so every caller is
# protected.  A threading semaphore remains correct across event loops and is
# held until the worker actually exits, even if its awaiting coroutine is
# cancelled.
_CONVERT_TIMEOUT = 60
_MAX_CONCURRENT_CONVERSIONS = 2
_CONVERT_SEMAPHORE = threading.BoundedSemaphore(_MAX_CONCURRENT_CONVERSIONS)


def _find_soffice() -> str | None:
    """Return the LibreOffice executable path, or ``None`` when unavailable."""
    for name in _SOFFICE_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def _extension(filename: str) -> str:
    """Extract a normalized final suffix without trusting the input basename."""
    return Path(filename or "").suffix.lower()


def _convert_sync(data: bytes, filename: str) -> bytes | None:
    """Run one supported attachment conversion (called in a worker thread)."""
    extension = _extension(filename)
    if extension in PASSTHROUGH_EXTENSIONS:
        return data
    if extension not in CONVERTIBLE_EXTENSIONS:
        logger.error("不支持转换为 PDF 的附件格式: file=%s", filename)
        return None

    soffice = _find_soffice()
    if not soffice:
        logger.error("未找到 LibreOffice(soffice)，无法转换附件: file=%s", filename)
        return None

    with _CONVERT_SEMAPHORE:
        return _run_soffice_conversion(soffice, data, filename, extension)


def _run_soffice_conversion(
    soffice: str,
    data: bytes,
    filename: str,
    extension: str,
) -> bytes | None:
    """Convert bytes in an isolated temporary directory and return PDF bytes."""
    work_dir = Path(tempfile.mkdtemp(prefix="attachment2pdf_"))
    proc: subprocess.Popen[bytes] | None = None
    try:
        profile_dir = work_dir / "lo_profile"
        profile_dir.mkdir()

        # Keep the exact normalized input extension so LibreOffice selects the
        # correct import filter (notably .doc vs .docx and .jpg vs .jpeg).
        src_path = work_dir / f"source{extension}"
        src_path.write_bytes(data)

        cmd = [
            soffice,
            "--headless",
            "--nologo",
            "--nodefault",
            "--norestore",
            f"-env:UserInstallation={profile_dir.as_uri()}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(work_dir),
            str(src_path),
        ]

        # soffice can fork child processes.  A separate session lets timeout and
        # cancellation cleanup terminate the complete process group.
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            _, stderr = proc.communicate(timeout=_CONVERT_TIMEOUT)
        except subprocess.TimeoutExpired:
            logger.error("附件转 PDF 超时: file=%s", filename)
            _kill_process_group(proc)
            _reap_process(proc)
            return None

        if proc.returncode != 0:
            logger.error(
                "附件转 PDF 失败: file=%s rc=%s stderr=%s",
                filename,
                proc.returncode,
                stderr.decode("utf-8", "ignore")[:300] if stderr else "",
            )
            return None

        pdf_path = src_path.with_suffix(".pdf")
        if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
            logger.error("附件转 PDF 未生成有效产物: file=%s", filename)
            return None
        return pdf_path.read_bytes()
    except Exception as exc:
        logger.error("附件转 PDF 异常: file=%s error=%s", filename, exc)
        if proc is not None and proc.poll() is None:
            _kill_process_group(proc)
            _reap_process(proc)
        return None
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _kill_process_group(proc: subprocess.Popen[bytes]) -> None:
    """Best-effort termination of soffice and every child it spawned."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _reap_process(proc: subprocess.Popen[bytes]) -> None:
    """Reap a killed process so timed-out conversions do not leave zombies."""
    try:
        proc.communicate(timeout=5)
    except Exception:
        try:
            proc.wait(timeout=1)
        except Exception:
            pass


async def attachment_to_pdf(data: bytes, filename: str) -> bytes | None:
    """Return one attachment as PDF bytes, or ``None`` on conversion failure.

    Existing PDF input is returned unchanged.  Other supported extensions are
    converted by LibreOffice while preserving their original extension.
    """
    return await asyncio.to_thread(_convert_sync, data, filename)


async def docx_to_pdf(data: bytes, filename: str) -> bytes | None:
    """Backward-compatible alias for callers that only convert Word files."""
    return await attachment_to_pdf(data, filename)


def soffice_available() -> bool:
    """Whether LibreOffice is available in the current runtime."""
    return _find_soffice() is not None
