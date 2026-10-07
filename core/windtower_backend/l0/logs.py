"""Pure Abaqus log collection and failure classification."""

from __future__ import annotations

import re
from pathlib import Path


def stage_has_fatal_error(text: str) -> bool:
    upper = text.upper()
    return bool(re.search(r"THE PROGRAM HAS DISCOVERED\s+[1-9]\d*\s+FATAL ERRORS?", upper)) or any(
        marker in upper
        for marker in (
            "ABAQUS/ANALYSIS EXITED WITH ERRORS",
            "ABAQUS/STANDARD ANALYSIS EXITED WITH AN ERROR",
            "SYSTEM ERROR CREATEFILE",
            "***ERROR:",
        )
    )


def collect_stage_logs(stage_dir: Path, job_id: str, console_path: Path) -> str:
    pieces = []
    if console_path.exists():
        pieces.append(console_path.read_text(encoding="utf-8", errors="ignore"))
    for suffix in (".dat", ".msg", ".sta", ".log"):
        path = stage_dir / f"{job_id}{suffix}"
        if path.exists():
            pieces.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(pieces)


def important_lines(text: str, limit: int = 12) -> list[str]:
    keywords = ("license", "error", "warning", "fail", "abort", "odb", "completed", "analysis")
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if line and any(keyword in line.lower() for keyword in keywords):
            lines.append(line[:300])
        if len(lines) >= limit:
            break
    return lines
