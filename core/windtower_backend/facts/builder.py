from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from ..importers.parse_result import REQUIRED_FEM_KEYS
from ..importers.windtower_adapter import parse_windtower_workbook
from .models import EngineeringFact, EngineeringFactPackage, FactSource, ParseIssue
from .validators import validate_fact_package


def parse_engineering_facts(excel_path: Path, load_paths: list[Path] | None = None, out_dir: Path | None = None) -> EngineeringFactPackage:
    imported = parse_windtower_workbook(excel_path, load_paths)
    package = build_engineering_fact_package(imported, source_path=excel_path)
    if out_dir is not None:
        from .exporters import write_fact_artifacts

        write_fact_artifacts(package, out_dir)
    return package


def build_engineering_fact_package(imported: dict[str, Any], source_path: str | Path) -> EngineeringFactPackage:
    source_file = Path(source_path).name
    facts: list[EngineeringFact] = []
    issues = _issues_from_import(imported, source_file)

    for index, point in enumerate(imported.get("tower", []), start=1):
        source = _source_from_row(source_file, point, "tower_profile", "tower_parser")
        prefix = f"tower_profile.{index:03d}"
        facts.extend(
            [
                _fact(f"{prefix}.z_mm", "geometry", "confirmed_fact", point.get("z_mm"), "mm", source, "Tower profile elevation."),
                _fact(f"{prefix}.diameter_mm", "geometry", "confirmed_fact", point.get("diameter_mm"), "mm", source, "Tower profile mean diameter."),
                _fact(f"{prefix}.thickness_mm", "geometry", "confirmed_fact", point.get("thickness_mm"), "mm", source, "Tower shell thickness."),
            ]
        )
    tower = list(imported.get("tower", []))
    for index, (lower, upper) in enumerate(zip(tower[:-1], tower[1:]), start=1):
        source = _source_from_row(source_file, lower, "tower_section", "tower_section_deriver")
        height = _number(upper.get("z_mm")) - _number(lower.get("z_mm")) if _number(upper.get("z_mm")) is not None and _number(lower.get("z_mm")) is not None else None
        facts.append(
            _fact(
                f"tower_section.{index:03d}.height_mm",
                "geometry",
                "derived_fact",
                height,
                "mm",
                source,
                "Tower shell section height derived from adjacent profile points.",
                derivation={"inputs": [f"tower_profile.{index:03d}.z_mm", f"tower_profile.{index + 1:03d}.z_mm"], "method": "z_top - z_bottom"},
            )
        )

    for index, flange in enumerate(imported.get("flanges", []), start=1):
        source = _source_from_row(source_file, flange, "flange", "flange_parser")
        for key, unit in [
            ("diameter_mm", "mm"),
            ("bolt_circle_mm", "mm"),
            ("inner_diameter_mm", "mm"),
            ("total_thickness_mm", "mm"),
            ("bolt_count", ""),
            ("bolt_diameter_mm", "mm"),
        ]:
            if flange.get(key) is not None:
                facts.append(_fact(f"flange.{index:02d}.{key}", "geometry", "confirmed_fact", flange.get(key), unit, source, f"Flange {key}."))

    door = imported.get("door", {}) or {}
    for key, value in sorted(door.items()):
        source = FactSource(workbook=source_file, sheet=_sheet(imported, "door"), cell_range="auto", field_name=f"door.{key}", raw_value=value, parser="door_parser")
        facts.append(_fact(f"door.{key}", "geometry", "confirmed_fact", value, "mm" if key not in {"theta_center_deg"} else "deg", source, f"Door parameter {key}."))

    door_derivation = (imported.get("derived_facts", {}) or {}).get("door_derivation", {}) or {}
    for candidate in door_derivation.get("candidates", []) or []:
        shape = candidate.get("shape", "unknown")
        status = "derived_fact" if door_derivation.get("input_status") == "complete_excel" else "assumed_fact"
        source = FactSource(workbook=source_file, sheet=_sheet(imported, "door"), cell_range="derived", field_name=f"door_candidate.{shape}", raw_value=candidate, parser="door_derivation")
        facts.append(
            _fact(
                f"door_candidate.{shape}",
                "geometry",
                status,
                candidate,
                "",
                source,
                "Door modeling candidate; not final model authority until selected and validated.",
                confidence=0.9 if status == "derived_fact" else 0.5,
                derivation={"input_status": door_derivation.get("input_status"), "requires_confirmation": door_derivation.get("requires_confirmation", False)},
            )
        )

    for record in imported.get("fem_param_records", []) or []:
        facts.append(_fact_from_fem_record(source_file, record))

    fem_params = imported.get("fem_params", {}) or {}
    for required in REQUIRED_FEM_KEYS:
        if fem_params.get(required) in {None, ""}:
            source = FactSource(workbook=source_file, sheet=_sheet(imported, "fem_params"), field_name=required, parser="fem_param_validator")
            fact_issue = ParseIssue(issue_id=f"unresolved:{required}", severity="error", category="missing_required_fem_param", message=f"Required FEM parameter is missing: {required}.", fact_id=required, source=source)
            facts.append(_fact(required, _category_for_fem_key(required), "unresolved_fact", None, "", source, "Missing required FEM parameter.", confidence=0.0, issues=[fact_issue]))

    loads_inventory = imported.get("loads_inventory", {}) or {}
    for index, load_case in enumerate(loads_inventory.get("ultimate_cases", [])[:200], start=1):
        source = FactSource(workbook=load_case.get("source_file") or source_file, sheet=load_case.get("sheet", ""), cell_range="auto", field_name="ultimate_load_case", raw_value=load_case, parser="load_workbook_parser")
        facts.append(_fact(f"load.ultimate_case.{index:03d}", "load", "confirmed_fact", load_case, "", source, "Ultimate load case row."))

    sheet_map = imported.get("sheet_map", {}) or {}
    for key, value in sheet_map.items():
        source = FactSource(workbook=source_file, sheet=value or "", field_name=f"sheet_map.{key}", raw_value=value, parser="sheet_detector")
        facts.append(_fact(f"metadata.sheet_map.{key}", "metadata", "confirmed_fact" if value else "unresolved_fact", value, "", source, f"Detected sheet for {key}.", confidence=1.0 if value else 0.0))

    confidence_report = imported.get("confidence_report", {}) or {}
    package = EngineeringFactPackage(
        package_id=f"facts-{uuid.uuid4().hex[:10]}",
        source_file=source_file,
        source_files=list(imported.get("source_files") or [source_file]),
        detected_format=str(imported.get("detected_format", "")),
        facts=facts,
        issues=issues,
        raw_parse_summary={
            "sheet_map": sheet_map,
            "workbook_profile": imported.get("workbook_profile", {}),
            "standard_input_candidate": imported.get("standard_input_candidate") or imported.get("standard_input_package"),
            "standard_input_validation": imported.get("validation_report", {}),
            "llm_parse_assist": imported.get("llm_parse_assist", {}),
            "parsed_facts": imported.get("parsed_facts", {}),
            "confidence_report": confidence_report,
            "llm_assist_required": bool(confidence_report.get("llm_assist_required")),
            "missing_items": imported.get("missing_items", []),
            "warnings": imported.get("warnings", []),
        },
        summary={
            "tower_point_count": len(imported.get("tower", [])),
            "flange_count": len(imported.get("flanges", [])),
            "door_complete": bool((imported.get("parsed_facts", {}) or {}).get("door_complete")),
            "fem_param_count": len(imported.get("fem_params", {}) or {}),
        },
    )
    return validate_fact_package(package)


def _fact(
    fact_id: str,
    category: str,
    status: str,
    value: Any,
    unit: str,
    source: FactSource,
    description: str,
    confidence: float = 1.0,
    issues: list[ParseIssue] | None = None,
    derivation: dict[str, Any] | None = None,
) -> EngineeringFact:
    return EngineeringFact(
        fact_id=fact_id,
        category=category,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        value=value,
        unit=unit,
        source=source,
        confidence=confidence,
        issues=issues or [],
        derivation=derivation,
        description=description,
    )


def _fact_from_fem_record(source_file: str, record: dict[str, Any]) -> EngineeringFact:
    path = str(record.get("path"))
    source = FactSource(
        workbook=source_file,
        sheet="FEM模型参数",
        cell_range=str(record.get("source") or "excel"),
        field_name=path,
        raw_value=record.get("raw_value"),
        parser="typed_fem_param_parser",
    )
    status = "confirmed_fact" if record.get("value") is not None else "unresolved_fact"
    return _fact(path, _category_for_fem_key(path), status, record.get("value"), str(record.get("unit") or ""), source, str(record.get("description") or f"FEM parameter {path}."), confidence=float(record.get("confidence", 1.0) or 0.0))


def _issues_from_import(imported: dict[str, Any], source_file: str) -> list[ParseIssue]:
    issues: list[ParseIssue] = []
    source = FactSource(workbook=source_file, parser="import_validation")
    for index, item in enumerate(imported.get("missing_items", []) or [], start=1):
        issues.append(ParseIssue(issue_id=f"import_missing:{index}", severity="error", category="missing_item", message=str(item.get("reason") or item), source=source))
    for index, warning in enumerate(imported.get("warnings", []) or [], start=1):
        issues.append(ParseIssue(issue_id=f"import_warning:{index}", severity="warning", category="import_warning", message=str(warning), source=source))
    return issues


def _source_from_row(source_file: str, row: dict[str, Any], field_name: str, parser: str) -> FactSource:
    sheet, cell_range = _split_source(row.get("source"))
    return FactSource(workbook=source_file, sheet=sheet, cell_range=cell_range, field_name=field_name, raw_value=row, parser=parser)


def _split_source(source: Any) -> tuple[str, str]:
    text = str(source or "")
    if "!" in text:
        sheet, cell_range = text.split("!", 1)
        return sheet, cell_range
    return "", text


def _sheet(imported: dict[str, Any], key: str) -> str:
    return str((imported.get("sheet_map", {}) or {}).get(key) or "")


def _category_for_fem_key(path: str) -> str:
    prefix = path.split(".", 1)[0]
    return {
        "material": "material",
        "section": "section",
        "element": "section",
        "mesh": "mesh",
        "loads": "load",
        "boundary": "boundary",
        "steps": "analysis",
        "interaction": "analysis",
        "outputs": "analysis",
    }.get(prefix, "metadata")


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
