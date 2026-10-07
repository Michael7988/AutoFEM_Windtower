from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import BACKEND_ROOT, PROJECT_ROOT, Settings


CRITICAL_CONFIG_PATHS = (
    BACKEND_ROOT / "llm_api_config.yaml",
    BACKEND_ROOT / "requirements.txt",
    PROJECT_ROOT / "requirements-lock-py38.txt",
)


def stable_hash(payload: Any, *, length: int = 16) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:length]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_inp_sha256(path: Path, project_id: str | None = None) -> str:
    """Hash semantic INP text while ignoring generated naming/comment noise."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if project_id:
        text = text.replace(project_id, "<PROJECT>")
    text = re.sub(r"(?im)^\*\*.*generated.*$", "", text)
    text = "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_environment_manifest(settings: Settings) -> dict[str, Any]:
    revision, dirty = _git_revision(PROJECT_ROOT)
    packages = _installed_packages()
    return {
        # Preserve the v1 fields consumed by existing reports.
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "llm_provider": getattr(settings, "engineering_llm_provider", "qwen"),
        "llm_model": settings.qwen_model,
        "llm_configured": bool(settings.qwen_api_key),
        "abaqus_command_configured": bool(settings.abaqus_cmd),
        "secrets_recorded": False,
        # Additive v2 reproducibility fields.
        "schema_version": "experiment_environment_v2",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": revision,
        "git_dirty": dirty,
        "implementation_sha256": _implementation_hash(BACKEND_ROOT),
        "dependencies": packages,
        "dependencies_sha256": _stable_hash(packages),
        "runtime_versions": {
            "abaqus": _abaqus_release(settings.abaqus_cmd),
            "gmsh": packages.get("gmsh"),
            "pyvista": packages.get("pyvista"),
            "vtk": packages.get("vtk"),
        },
        "llm_parameters": {
            "model": settings.qwen_model,
            "base_url": settings.qwen_base_url,
            "temperature": settings.qwen_temperature,
            "max_tokens": settings.qwen_max_tokens,
            "timeout_seconds": settings.qwen_timeout,
        },
        "configuration_hashes": _path_hashes(CRITICAL_CONFIG_PATHS),
    }


def append_paper_traceability(
    benchmark_root: Path,
    *,
    campaign_id: str,
    environment: dict[str, Any],
    dataset_manifest_sha256: str,
    result_root: Path,
) -> Path:
    path = benchmark_root / "paper_traceability.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": "paper_traceability_v1",
        "campaign_id": campaign_id,
        "git_commit": environment.get("git_commit"),
        "implementation_sha256": environment.get("implementation_sha256"),
        "dependencies_sha256": environment.get("dependencies_sha256"),
        "dataset_manifest_sha256": dataset_manifest_sha256,
        "result_root": str(result_root),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def _git_revision(root: Path) -> tuple[str | None, bool | None]:
    try:
        revision = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            ).stdout.strip()
        )
        return revision, dirty
    except (OSError, subprocess.SubprocessError):
        return None, None


def _installed_packages() -> dict[str, str]:
    names = {
        "fastapi", "gmsh", "numpy", "openpyxl", "pydantic", "pymupdf",
        "pyvista", "requests", "trame", "trame-server", "trame-vtk",
        "trame-vuetify", "uvicorn", "vtk", "xlrd",
    }
    result: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = str(distribution.metadata.get("Name") or "").lower()
        if name in names:
            result[name] = distribution.version
    return dict(sorted(result.items()))


def _implementation_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        if "storage" in path.parts or "__pycache__" in path.parts:
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _path_hashes(paths: Iterable[Path]) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for path in paths:
        result[path.name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return result


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _abaqus_release(command: str) -> str | None:
    executable = Path(command)
    if not executable.exists():
        return None
    try:
        result = subprocess.run(
            [command, "information=release"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=10,
        )
        lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        return next((line for line in lines if "abaqus" in line.lower()), None)
    except (OSError, subprocess.SubprocessError):
        return None
