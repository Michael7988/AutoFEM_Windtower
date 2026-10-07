"""Small, import-safe runtime fixes for the legacy Windows/Anaconda stack."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any


_DLL_HANDLES: list[Any] = []


def bootstrap_windows_openssl() -> dict[str, str]:
    """Make Anaconda's OpenSSL DLLs visible before importing ``ssl``/requests.

    The function is deliberately idempotent and has no effect outside Windows.
    Keeping the directory handle alive is required by ``os.add_dll_directory``.
    """

    if os.name != "nt":
        return {"status": "not_applicable", "platform": os.name}
    dll_dir = Path(sys.prefix) / "Library" / "bin"
    if not dll_dir.is_dir():
        return {"status": "not_found", "directory": str(dll_dir)}
    current = os.environ.get("PATH", "")
    parts = [part for part in current.split(os.pathsep) if part]
    if str(dll_dir).casefold() not in {part.casefold() for part in parts}:
        os.environ["PATH"] = str(dll_dir) + os.pathsep + current
    if hasattr(os, "add_dll_directory") and not _DLL_HANDLES:
        _DLL_HANDLES.append(os.add_dll_directory(str(dll_dir)))
    return {"status": "ready", "directory": str(dll_dir)}
