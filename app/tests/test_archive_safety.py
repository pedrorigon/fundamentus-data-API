from __future__ import annotations

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
import pytest

from app.core import archive_safety
from app.core.archive_safety import (
    ArchiveSafetyError,
    open_validated_zip,
    read_bounded_body,
)


def _archive(files: dict[str, bytes], *, compression: int = ZIP_DEFLATED) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_bounded_body_accepts_payload_at_the_limit() -> None:
    response = httpx.Response(200, content=b"safe")

    assert await read_bounded_body(response, 4) == b"safe"


@pytest.mark.asyncio
async def test_bounded_body_rejects_declared_and_streamed_overruns() -> None:
    declared = httpx.Response(200, headers={"content-length": "5"}, content=b"")
    streamed = httpx.Response(200, headers={"content-length": "invalid"}, content=b"large")

    with pytest.raises(ArchiveSafetyError, match="download limit"):
        await read_bounded_body(declared, 4)
    with pytest.raises(ArchiveSafetyError, match="download limit"):
        await read_bounded_body(streamed, 4)


def test_validated_zip_accepts_a_bounded_archive() -> None:
    with open_validated_zip(_archive({"data.csv": b"a;b\n1;2\n"})) as archive:
        assert archive.read("data.csv") == b"a;b\n1;2\n"


def test_validated_zip_rejects_excess_members(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(archive_safety, "MAX_ARCHIVE_MEMBERS", 1)

    with pytest.raises(ArchiveSafetyError, match="too many members"):
        with open_validated_zip(_archive({"a": b"1", "b": b"2"})):
            pass


def test_validated_zip_rejects_member_and_total_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _archive({"a": b"123", "b": b"456"})
    monkeypatch.setattr(archive_safety, "MAX_ARCHIVE_MEMBER_BYTES", 2)
    with pytest.raises(ArchiveSafetyError, match="member exceeds"):
        with open_validated_zip(payload):
            pass

    monkeypatch.setattr(archive_safety, "MAX_ARCHIVE_MEMBER_BYTES", 3)
    monkeypatch.setattr(archive_safety, "MAX_ARCHIVE_EXPANDED_BYTES", 5)
    with pytest.raises(ArchiveSafetyError, match="total expansion"):
        with open_validated_zip(payload):
            pass


def test_validated_zip_rejects_suspicious_compression_ratio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(archive_safety, "MAX_ARCHIVE_COMPRESSION_RATIO", 1)

    with pytest.raises(ArchiveSafetyError, match="compression ratio"):
        with open_validated_zip(_archive({"data": b"A" * 1024})):
            pass
