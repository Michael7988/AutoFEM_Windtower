from __future__ import annotations

import hashlib
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from fastapi import UploadFile

from ..config import Settings


ARCHIVE_SUFFIXES = {".docx", ".pptx", ".xlsx", ".xlsm", ".zip"}
CHUNK_SIZE = 1024 * 1024


class UploadLimitError(ValueError):
    pass


@dataclass(frozen=True)
class SpooledUpload:
    filename: str
    media_type: str
    path: Path
    size: int
    sha256: str

    def read_bytes(self) -> bytes:
        return self.path.read_bytes()


async def spool_uploads(
    uploads: Iterable[UploadFile],
    settings: Settings,
) -> tuple[Path, list[SpooledUpload]]:
    cache_root = settings.cache_root or Path(tempfile.gettempdir())
    cache_root.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="windtower-upload-", dir=str(cache_root)))
    stored: list[SpooledUpload] = []
    batch_size = 0
    try:
        for index, upload in enumerate(uploads, start=1):
            item = await _spool_one(upload, temp_root, index, settings)
            batch_size += item.size
            if batch_size > settings.upload_batch_limit_bytes:
                raise UploadLimitError(
                    f"Upload batch exceeds {settings.upload_batch_limit_bytes // 1024 // 1024} MB."
                )
            stored.append(item)
        return temp_root, stored
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise


def cleanup_spooled_uploads(temp_root: Path) -> None:
    shutil.rmtree(temp_root, ignore_errors=True)


async def _spool_one(
    upload: UploadFile,
    temp_root: Path,
    index: int,
    settings: Settings,
) -> SpooledUpload:
    filename = Path(upload.filename or "unnamed").name
    path = temp_root / f"{index:02d}-{_safe_name(filename)}"
    digest = hashlib.sha256()
    size = 0
    with path.open("wb") as handle:
        while True:
            chunk = await upload.read(CHUNK_SIZE)
            if not chunk:
                break
            size += len(chunk)
            if size > settings.upload_file_limit_bytes:
                raise UploadLimitError(
                    f"{filename} exceeds {settings.upload_file_limit_bytes // 1024 // 1024} MB."
                )
            digest.update(chunk)
            handle.write(chunk)
    if size == 0:
        raise UploadLimitError(f"{filename} is empty.")
    _validate_archive(path, filename, settings)
    return SpooledUpload(
        filename=filename,
        media_type=upload.content_type or "application/octet-stream",
        path=path,
        size=size,
        sha256=digest.hexdigest(),
    )


def _validate_archive(path: Path, filename: str, settings: Settings) -> None:
    if Path(filename).suffix.lower() not in ARCHIVE_SUFFIXES:
        return
    if not zipfile.is_zipfile(path):
        raise UploadLimitError(f"{filename} is not a valid ZIP/Office container.")
    total_compressed = 0
    total_unpacked = 0
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            total_compressed += max(0, item.compress_size)
            total_unpacked += max(0, item.file_size)
            if total_unpacked > settings.upload_unpacked_limit_bytes:
                raise UploadLimitError(
                    f"{filename} expands beyond {settings.upload_unpacked_limit_bytes // 1024 // 1024} MB."
                )
    ratio = total_unpacked / max(1, total_compressed)
    if ratio > settings.upload_max_compression_ratio:
        raise UploadLimitError(
            f"{filename} compression ratio {ratio:.1f} exceeds {settings.upload_max_compression_ratio:.1f}."
        )


def _safe_name(filename: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in filename)
    return safe[:120] or "upload"
