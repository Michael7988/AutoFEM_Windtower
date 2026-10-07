from __future__ import annotations

import csv
import html
import json
from pathlib import Path
from typing import Any

from .models import EngineeringFactPackage
from .validators import validate_fact_package


def write_fact_artifacts(package: EngineeringFactPackage, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    package = validate_fact_package(package)
    artifacts = {
        "engineering_facts.json": out_dir / "engineering_facts.json",
        "parse_validation_report.json": out_dir / "parse_validation_report.json",
        "traceability_matrix.csv": out_dir / "traceability_matrix.csv",
        "input_manifest.json": out_dir / "input_manifest.json",
        "parse_report.md": out_dir / "parse_report.md",
        "parse_report.html": out_dir / "parse_report.html",
    }
    artifacts["engineering_facts.json"].write_text(json.dumps(package.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts["parse_validation_report.json"].write_text(json.dumps(_validation_report(package), ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts["input_manifest.json"].write_text(json.dumps(_input_manifest(package, artifacts), ensure_ascii=False, indent=2), encoding="utf-8")
    _write_traceability_csv(package, artifacts["traceability_matrix.csv"])
    markdown = _markdown_report(package, artifacts)
    artifacts["parse_report.md"].write_text(markdown, encoding="utf-8")
    artifacts["parse_report.html"].write_text(_html_report(markdown), encoding="utf-8")
    return artifacts


def _validation_report(package: EngineeringFactPackage) -> dict[str, Any]:
    return {
        "package_id": package.package_id,
        "source_file": package.source_file,
        "status": package.status,
        "summary": package.summary,
        "issues": [issue.model_dump() for issue in package.issues],
        "llm_policy": package.llm_policy,
    }


def _input_manifest(package: EngineeringFactPackage, artifacts: dict[str, Path]) -> dict[str, Any]:
    return {
        "package_id": package.package_id,
        "source_file": package.source_file,
        "source_files": package.source_files,
        "created_at": package.created_at,
        "parser_version": package.parser_version,
        "detected_format": package.detected_format,
        "unit_system": package.unit_system,
        "artifact_index": {name: str(path) for name, path in artifacts.items()},
        "replay_command": f"python -m windtower_backend.automation.parse_facts --excel \"{package.source_file}\"",
    }


def _write_traceability_csv(package: EngineeringFactPackage, path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "fact_id",
                "category",
                "status",
                "value",
                "unit",
                "workbook",
                "sheet",
                "cell_range",
                "field_name",
                "raw_value",
                "parser",
                "confidence",
                "issue_count",
            ],
        )
        writer.writeheader()
        for fact in package.facts:
            writer.writerow(
                {
                    "fact_id": fact.fact_id,
                    "category": fact.category,
                    "status": fact.status,
                    "value": _compact(fact.value),
                    "unit": fact.unit,
                    "workbook": fact.source.workbook,
                    "sheet": fact.source.sheet,
                    "cell_range": fact.source.cell_range,
                    "field_name": fact.source.field_name,
                    "raw_value": _compact(fact.source.raw_value),
                    "parser": fact.source.parser,
                    "confidence": fact.confidence,
                    "issue_count": len(fact.issues),
                }
            )


def _markdown_report(package: EngineeringFactPackage, artifacts: dict[str, Path]) -> str:
    summary = package.summary
    lines = [
        "# Engineering Fact Parse Report",
        "",
        f"- Source file: `{package.source_file}`",
        f"- Status: `{package.status}`",
        f"- Detected format: `{package.detected_format}`",
        f"- Unit system: `{package.unit_system}`",
        f"- Facts: {summary.get('fact_count', len(package.facts))}",
        f"- Confirmed / derived / assumed / unresolved: {summary.get('confirmed_count', 0)} / {summary.get('derived_count', 0)} / {summary.get('assumed_count', 0)} / {summary.get('unresolved_count', 0)}",
        f"- LLM assist required: {summary.get('llm_assist_required', False)}",
        "",
        "## Parse Counts",
        "",
        f"- Tower profile points: {summary.get('tower_point_count', 0)}",
        f"- Flanges: {summary.get('flange_count', 0)}",
        f"- Door complete: {summary.get('door_complete', False)}",
        f"- FEM parameters: {summary.get('fem_param_count', 0)}",
        "",
        "## Issues",
        "",
    ]
    if package.issues:
        for issue in package.issues:
            lines.append(f"- `{issue.severity}` `{issue.category}`: {issue.message}")
    else:
        lines.append("- none")
    lines.extend(["", "## Artifacts", ""])
    for name, path in artifacts.items():
        lines.append(f"- `{name}`: `{path}`")
    lines.extend(["", "## LLM Authority Boundary", "", package.llm_policy])
    return "\n".join(lines)


def _html_report(markdown: str) -> str:
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>Engineering Fact Parse Report</title>"
        "<style>body{font-family:Segoe UI,Microsoft YaHei,Arial,sans-serif;line-height:1.6;max-width:1100px;margin:32px auto;padding:0 24px;color:#172033}"
        "pre{white-space:pre-wrap;background:#f6f8fb;border:1px solid #d8e0ec;padding:16px;border-radius:6px}</style>"
        "</head><body><pre>"
        + html.escape(markdown)
        + "</pre></body></html>"
    )


def _compact(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return "" if value is None else str(value)
