from __future__ import annotations

import io
import zipfile
from dataclasses import replace

import pytest
from starlette.datastructures import Headers, UploadFile

from windtower_backend.api.security import remote_request_authorized, validate_bind_host
from windtower_backend.api.uploads import (
    UploadLimitError,
    cleanup_spooled_uploads,
    spool_uploads,
)
from windtower_backend.config import load_settings


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_remote_mutation_requires_configured_token() -> None:
    settings = replace(load_settings(), api_token=None, desktop_mode=False)
    assert remote_request_authorized(settings, "127.0.0.1", "") == (True, "loopback")
    assert remote_request_authorized(settings, "192.168.1.8", "") == (False, "disabled")


def test_remote_mutation_accepts_matching_token() -> None:
    settings = replace(load_settings(), api_token="secret", desktop_mode=False)
    assert remote_request_authorized(settings, "192.168.1.8", "wrong") == (False, "invalid")
    assert remote_request_authorized(settings, "192.168.1.8", "secret") == (True, "token")


def test_non_loopback_bind_requires_token() -> None:
    validate_bind_host("127.0.0.1", None)
    with pytest.raises(RuntimeError):
        validate_bind_host("0.0.0.0", None)
    validate_bind_host("0.0.0.0", "secret")


@pytest.mark.anyio
async def test_upload_is_rejected_while_streaming_over_limit(tmp_path) -> None:
    settings = replace(
        load_settings(),
        cache_root=tmp_path,
        upload_file_limit_bytes=4,
        upload_batch_limit_bytes=10,
    )
    upload = UploadFile(
        file=io.BytesIO(b"12345"),
        filename="large.txt",
        headers=Headers({"content-type": "text/plain"}),
    )
    with pytest.raises(UploadLimitError):
        await spool_uploads([upload], settings)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.anyio
async def test_office_zip_expansion_ratio_is_limited(tmp_path) -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", b"A" * 100_000)
    settings = replace(
        load_settings(),
        cache_root=tmp_path,
        upload_file_limit_bytes=1024 * 1024,
        upload_batch_limit_bytes=1024 * 1024,
        upload_unpacked_limit_bytes=1024 * 1024,
        upload_max_compression_ratio=2,
    )
    upload = UploadFile(
        file=io.BytesIO(payload.getvalue()),
        filename="bomb.xlsx",
        headers=Headers({"content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}),
    )
    with pytest.raises(UploadLimitError):
        await spool_uploads([upload], settings)


@pytest.mark.anyio
async def test_valid_upload_is_spooled_and_cleaned(tmp_path) -> None:
    settings = replace(load_settings(), cache_root=tmp_path)
    upload = UploadFile(
        file=io.BytesIO(b"mesh.global_size_mm=650"),
        filename="parameters.txt",
        headers=Headers({"content-type": "text/plain"}),
    )
    temp_root, stored = await spool_uploads([upload], settings)
    assert stored[0].read_bytes() == b"mesh.global_size_mm=650"
    assert stored[0].sha256
    cleanup_spooled_uploads(temp_root)
    assert not temp_root.exists()
