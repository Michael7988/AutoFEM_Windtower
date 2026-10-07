"""PyInstaller/desktop entry point for the local FastAPI process."""

from __future__ import annotations

import argparse

import uvicorn

from windtower_backend.api.security import validate_bind_host
from windtower_backend.app import app
from windtower_backend.config import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="WindTower CAE local backend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    validate_bind_host(args.host, load_settings().api_token)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
