from __future__ import annotations

import hmac
import ipaddress
from typing import Iterable

from fastapi import Request
from fastapi.responses import JSONResponse

from ..config import Settings


PUBLIC_PATHS = frozenset({
    "/api/health",
    "/api/desktop/health",
    "/api/desktop/version",
})


def install_api_security(app: object, settings: Settings) -> None:
    """Protect non-loopback access while preserving existing local clients."""

    middleware = getattr(app, "middleware")

    @middleware("http")
    async def require_api_credentials(request: Request, call_next):
        if request.method == "OPTIONS" or not request.url.path.startswith("/api/"):
            return await call_next(request)
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        if settings.desktop_mode and settings.desktop_token:
            supplied = request.headers.get("x-windtower-desktop-token", "")
            if not hmac.compare_digest(supplied, settings.desktop_token):
                return _unauthorized("Desktop session credential is invalid.")
            return await call_next(request)

        client_host = request.client.host if request.client else ""
        allowed, reason = remote_request_authorized(
            settings,
            client_host,
            request.headers.get("x-windtower-api-token", ""),
        )
        if allowed:
            return await call_next(request)
        if reason == "disabled":
            return JSONResponse(
                status_code=403,
                content={"detail": "Remote API access is disabled until WINDTOWER_API_TOKEN is configured."},
            )
        return _unauthorized("API credential is invalid.")


def validate_bind_host(
    host: str,
    api_token: str | None,
    *,
    allowed_loopback_names: Iterable[str] = ("localhost",),
) -> None:
    if host.lower() in {name.lower() for name in allowed_loopback_names} or is_loopback_host(host):
        return
    if not api_token:
        raise RuntimeError("Refusing non-loopback backend binding without WINDTOWER_API_TOKEN.")


def is_loopback_host(host: str) -> bool:
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip()).is_loopback
    except ValueError:
        return False


def remote_request_authorized(
    settings: Settings,
    client_host: str,
    supplied_token: str,
) -> tuple[bool, str]:
    if is_loopback_host(client_host):
        return True, "loopback"
    if not settings.api_token:
        return False, "disabled"
    if hmac.compare_digest(supplied_token, settings.api_token):
        return True, "token"
    return False, "invalid"


def _unauthorized(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"detail": message},
        headers={"WWW-Authenticate": "Bearer"},
    )
