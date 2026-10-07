from __future__ import annotations

import uuid
from pathlib import Path


def stage_reference(reference_root: Path, content: bytes) -> Path:
    reference_root.mkdir(parents=True, exist_ok=True)
    staged = reference_root / f".reference-{uuid.uuid4().hex}.tmp"
    staged.write_bytes(content)
    return staged


def commit_reference(staged: Path, stored_name: str) -> Path:
    """Atomically install a staged INP and remove superseded references."""
    reference_root = staged.parent
    target = reference_root / stored_name
    staged.replace(target)
    for existing in reference_root.glob("*.inp"):
        if existing != target:
            existing.unlink()
    return target


def discard_staged_reference(staged: Path | None) -> None:
    if staged is not None and staged.exists():
        staged.unlink()
