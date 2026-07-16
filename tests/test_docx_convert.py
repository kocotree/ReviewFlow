from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app import docx_convert


@pytest.mark.parametrize(
    ("filename", "expected_extension"),
    [
        ("需求.DOC", ".doc"),
        ("需求.docx", ".docx"),
        ("说明.TXT", ".txt"),
        ("README.md", ".md"),
        ("原型.PNG", ".png"),
        ("页面.jpg", ".jpg"),
        ("页面.JPEG", ".jpeg"),
        ("页面.webp", ".webp"),
    ],
)
async def test_supported_extension_is_preserved_for_libreoffice(
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    expected_extension: str,
) -> None:
    captured: list[str] = []

    monkeypatch.setattr(docx_convert, "_find_soffice", lambda: "/fake/soffice")

    def fake_conversion(
        soffice: str,
        data: bytes,
        original_filename: str,
        extension: str,
    ) -> bytes:
        assert soffice == "/fake/soffice"
        assert data == b"source"
        assert original_filename == filename
        captured.append(extension)
        return b"pdf"

    monkeypatch.setattr(docx_convert, "_run_soffice_conversion", fake_conversion)

    assert await docx_convert.attachment_to_pdf(b"source", filename) == b"pdf"
    assert captured == [expected_extension]


async def test_pdf_is_returned_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    source = b"%PDF-existing"
    monkeypatch.setattr(
        docx_convert,
        "_find_soffice",
        lambda: pytest.fail("PDF passthrough must not start LibreOffice"),
    )

    result = await docx_convert.attachment_to_pdf(source, "already.PDF")

    assert result is source


async def test_conversion_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docx_convert, "_find_soffice", lambda: None)

    assert await docx_convert.attachment_to_pdf(b"broken", "broken.docx") is None


async def test_global_conversion_semaphore_caps_parallel_soffice_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    maximum_active = 0
    lock = threading.Lock()

    monkeypatch.setattr(docx_convert, "_find_soffice", lambda: "/fake/soffice")

    def slow_conversion(*args) -> bytes:
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.04)
        with lock:
            active -= 1
        return b"pdf"

    monkeypatch.setattr(docx_convert, "_run_soffice_conversion", slow_conversion)

    results = await asyncio.gather(
        *(
            docx_convert.attachment_to_pdf(b"source", f"material-{index}.txt")
            for index in range(8)
        )
    )

    assert results == [b"pdf"] * 8
    assert maximum_active == docx_convert._MAX_CONCURRENT_CONVERSIONS


def test_timeout_kills_reaps_and_removes_temporary_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "conversion"
    work_dir.mkdir()
    events: list[str] = []

    class TimedOutProcess:
        pid = 1234
        returncode = None

        def communicate(self, timeout: int):
            raise subprocess.TimeoutExpired("soffice", timeout)

        def poll(self):
            return None

    process = TimedOutProcess()
    monkeypatch.setattr(docx_convert.tempfile, "mkdtemp", lambda **kwargs: str(work_dir))
    monkeypatch.setattr(docx_convert.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        docx_convert,
        "_kill_process_group",
        lambda proc: events.append("killed"),
    )
    monkeypatch.setattr(
        docx_convert,
        "_reap_process",
        lambda proc: events.append("reaped"),
    )

    result = docx_convert._run_soffice_conversion(
        "/fake/soffice", b"source", "broken.docx", ".docx"
    )

    assert result is None
    assert events == ["killed", "reaped"]
    assert not work_dir.exists()
