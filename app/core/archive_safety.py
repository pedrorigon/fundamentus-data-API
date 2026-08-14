from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from zipfile import ZipFile

import httpx

MAX_ARCHIVE_MEMBERS = 512
MAX_ARCHIVE_MEMBER_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 200


class ArchiveSafetyError(ValueError):
    """Raised before an external archive can exceed a processing budget."""


async def read_bounded_body(response: httpx.Response, max_bytes: int) -> bytes:
    """Stream a response into memory while enforcing its decoded byte limit."""
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            declared_bytes = int(declared)
        except ValueError:
            declared_bytes = None
        if declared_bytes is not None and declared_bytes > max_bytes:
            raise ArchiveSafetyError("response exceeds the download limit")

    payload = bytearray()
    async for chunk in response.aiter_bytes():
        if len(payload) + len(chunk) > max_bytes:
            raise ArchiveSafetyError("response exceeds the download limit")
        payload.extend(chunk)
    return bytes(payload)


@contextmanager
def open_validated_zip(payload: bytes) -> Iterator[ZipFile]:
    """Open a ZIP only after its central directory fits fixed expansion budgets."""
    with ZipFile(BytesIO(payload)) as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ArchiveSafetyError("archive has too many members")

        expanded_bytes = 0
        for member in members:
            if member.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                raise ArchiveSafetyError("archive member exceeds the expansion limit")
            expanded_bytes += member.file_size
            if expanded_bytes > MAX_ARCHIVE_EXPANDED_BYTES:
                raise ArchiveSafetyError("archive exceeds the total expansion limit")
            if member.file_size and (
                member.compress_size == 0
                or member.file_size > member.compress_size * MAX_ARCHIVE_COMPRESSION_RATIO
            ):
                raise ArchiveSafetyError("archive member exceeds the compression ratio limit")
        yield archive
