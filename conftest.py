"""Repository-wide pytest bootstrap for the source-layout backend."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parent
CORE_ROOT = WORKSPACE_ROOT / "core"
EXAMPLE_ROOT = WORKSPACE_ROOT / "tests" / "fixtures"
TEST_RUNTIME_ROOT = Path(tempfile.gettempdir()) / "windtower-autofem-pytest" / str(os.getpid())

os.environ.setdefault("WINDTOWER_DATA_ROOT", str(TEST_RUNTIME_ROOT / "data"))
os.environ.setdefault("WINDTOWER_WORK_ROOT", str(TEST_RUNTIME_ROOT / "abaqus-work"))
os.environ.setdefault("WINDTOWER_LOG_ROOT", str(TEST_RUNTIME_ROOT / "logs"))
os.environ.setdefault("WINDTOWER_CACHE_ROOT", str(TEST_RUNTIME_ROOT / "cache"))

if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))
