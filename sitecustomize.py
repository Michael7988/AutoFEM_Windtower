"""Repository-local Windows DLL bootstrap for the frozen Python environment.

The installed Anaconda Python 3.8 keeps OpenSSL DLLs in ``Library/bin`` but the
directory is absent from PATH in non-Anaconda shells.  Python imports this file
during normal site initialization when commands are run from the repository.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any, Optional


_DLL_DIRECTORY_HANDLE: Optional[Any] = None


def _bootstrap_conda_dll_directory() -> None:
    global _DLL_DIRECTORY_HANDLE
    if os.name != "nt":
        return
    directory = Path(sys.prefix) / "Library" / "bin"
    if not directory.is_dir():
        return
    entries = os.environ.get("PATH", "").split(os.pathsep)
    if str(directory).lower() not in {entry.lower() for entry in entries}:
        os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
    add_directory = getattr(os, "add_dll_directory", None)
    if add_directory is not None:
        try:
            _DLL_DIRECTORY_HANDLE = add_directory(str(directory))
        except OSError:
            _DLL_DIRECTORY_HANDLE = None


_bootstrap_conda_dll_directory()
