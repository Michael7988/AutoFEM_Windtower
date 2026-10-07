from __future__ import annotations

import io
import json
import mimetypes
import urllib.error
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


MAX_ARCHIVE_BYTES = 500 * 1024 * 1024
MAX_ARCHIVE_FILES = 2000
ALLOWED_ROOT_FILES = {"manifest.json", "docling.json", "document.md"}


class DoclingUnavailable(RuntimeError):
    pass


class DoclingArtifactError(RuntimeError):
    pass


@dataclass(frozen=True)
class DoclingConversion:
    manifest: dict[str, Any]
    document: dict[str, Any]
    markdown: str
    assets: dict[str, bytes]


class DoclingClient:
    def __init__(self, base_url: str | None, timeout_seconds: int = 300):
        self.base_url = (base_url or "").rstrip("/")
        self.timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def health(self) -> dict[str, Any]:
        if not self.configured:
            return {"status": "unconfigured", "configured": False}
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=min(self.timeout_seconds, 10)) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            return {"status": "unavailable", "configured": True, "error": f"{type(exc).__name__}: {exc}"}

    def convert(self, path: Path, source_sha256: str, options: dict[str, Any] | None = None) -> DoclingConversion:
        if not self.configured:
            raise DoclingUnavailable("DOCLING_SERVICE_URL is not configured")
        boundary = "----windtower-" + uuid.uuid4().hex
        body = _multipart_body(boundary, path, source_sha256, options or {})
        request = urllib.request.Request(
            f"{self.base_url}/v1/convert",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/zip",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = response.read(MAX_ARCHIVE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            detail = exc.read(8192).decode("utf-8", errors="replace")
            raise DoclingUnavailable(f"Docling HTTP {exc.code}: {detail}") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise DoclingUnavailable(f"Docling request failed: {exc}") from exc
        if len(payload) > MAX_ARCHIVE_BYTES:
            raise DoclingArtifactError("Docling archive exceeds maximum size")
        return read_conversion_archive(payload, expected_source_sha256=source_sha256)


def read_conversion_archive(payload: bytes, expected_source_sha256: str) -> DoclingConversion:
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise DoclingArtifactError("Docling response is not a valid ZIP archive") from exc
    members = archive.infolist()
    if len(members) > MAX_ARCHIVE_FILES:
        raise DoclingArtifactError("Docling archive contains too many files")
    total = 0
    names: set[str] = set()
    for member in members:
        name = member.filename.replace("\\", "/")
        pure = PurePosixPath(name)
        if pure.is_absolute() or ".." in pure.parts:
            raise DoclingArtifactError(f"Unsafe Docling archive path: {name}")
        if name not in ALLOWED_ROOT_FILES and not name.startswith("assets/"):
            raise DoclingArtifactError(f"Unexpected Docling artifact: {name}")
        total += member.file_size
        if total > MAX_ARCHIVE_BYTES:
            raise DoclingArtifactError("Expanded Docling archive exceeds maximum size")
        names.add(name)
    missing = ALLOWED_ROOT_FILES - names
    if missing:
        raise DoclingArtifactError(f"Docling archive is missing: {sorted(missing)}")
    manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    if manifest.get("schema_version") != "docling_conversion_manifest_v1":
        raise DoclingArtifactError("Unsupported Docling manifest schema")
    if manifest.get("source_sha256") != expected_source_sha256:
        raise DoclingArtifactError("Docling source SHA-256 mismatch")
    document = json.loads(archive.read("docling.json").decode("utf-8"))
    markdown = archive.read("document.md").decode("utf-8")
    assets = {
        name: archive.read(name)
        for name in sorted(names)
        if name.startswith("assets/") and not name.endswith("/")
    }
    return DoclingConversion(manifest=manifest, document=document, markdown=markdown, assets=assets)


def _multipart_body(boundary: str, path: Path, source_sha256: str, options: dict[str, Any]) -> bytes:
    chunks: list[bytes] = []
    marker = boundary.encode("ascii")

    def field(name: str, value: str) -> None:
        chunks.extend([
            b"--" + marker + b"\r\n",
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
            value.encode("utf-8"), b"\r\n",
        ])

    field("source_sha256", source_sha256)
    field("options", json.dumps(options, ensure_ascii=False, sort_keys=True))
    filename = path.name.replace('"', "_")
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    chunks.extend([
        b"--" + marker + b"\r\n",
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"),
        f"Content-Type: {media_type}\r\n\r\n".encode("ascii"),
        path.read_bytes(), b"\r\n",
        b"--" + marker + b"--\r\n",
    ])
    return b"".join(chunks)
