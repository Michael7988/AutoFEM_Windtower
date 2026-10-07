from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..ir.models import IrBundle
from .models import ValidationBundle, ValidationIssue, ValidationReport


def validate_ir_layers(ir_bundle: IrBundle) -> ValidationBundle:
    schema = _schema_report(ir_bundle)
    geometry = _geometry_report(ir_bundle)
    physics = _physics_report(ir_bundle)
    abaqus = _abaqus_report(ir_bundle)
    reports = [schema, geometry, physics, abaqus]
    status = "blocked" if any(report.status == "blocked" for report in reports) else ("warning" if any(report.status == "warning" for report in reports) else "pass")
    issues = [issue for report in reports for issue in report.issues]
    return ValidationBundle(
        status=status,
        schema_validation_report=schema,
        geometry_validation_report=geometry,
        physics_validation_report=physics,
        abaqus_semantic_validation_report=abaqus,
        summary={
            "status": status,
            "issue_count": len(issues),
            "error_count": sum(1 for issue in issues if issue.severity == "error"),
            "warning_count": sum(1 for issue in issues if issue.severity == "warning"),
            "blocked_reports": [report.report_type for report in reports if report.status == "blocked"],
        },
    )


def write_validation_artifacts(bundle: ValidationBundle, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "schema_validation_report.json": out_dir / "schema_validation_report.json",
        "geometry_validation_report.json": out_dir / "geometry_validation_report.json",
        "physics_validation_report.json": out_dir / "physics_validation_report.json",
        "abaqus_semantic_validation_report.json": out_dir / "abaqus_semantic_validation_report.json",
        "validation_summary.json": out_dir / "validation_summary.json",
    }
    artifacts["schema_validation_report.json"].write_text(json.dumps(bundle.schema_validation_report.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts["geometry_validation_report.json"].write_text(json.dumps(bundle.geometry_validation_report.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts["physics_validation_report.json"].write_text(json.dumps(bundle.physics_validation_report.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts["abaqus_semantic_validation_report.json"].write_text(json.dumps(bundle.abaqus_semantic_validation_report.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts["validation_summary.json"].write_text(json.dumps(bundle.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    return artifacts


def _schema_report(bundle: IrBundle) -> ValidationReport:
    checks = {
        "fact_ir_present": bool(bundle.fact_ir),
        "model_ir_present": bool(bundle.model_ir),
        "abaqus_ir_present": bool(bundle.abaqus_ir),
        "legacy_ir_present": bool(bundle.legacy_ir),
        "unit_system_present": bool(bundle.model_ir.get("unit_system")),
        "source_trace_present": bool(bundle.model_ir.get("source_trace") or bundle.abaqus_ir.get("source_trace")),
    }
    issues = _issues_from_checks(checks, "schema", "Required IR layer or metadata is missing.")
    for item in bundle.unresolved:
        issues.append(
            ValidationIssue(
                field=str(item.get("fact_id")),
                severity="error",
                category="schema",
                message=str(item.get("message") or "Unresolved fact blocks IR validation."),
                source=item.get("source"),
                fact_ref=str(item.get("fact_id")),
            )
        )
    return _report("schema_validation", checks, issues)


def _geometry_report(bundle: IrBundle) -> ValidationReport:
    geometry = bundle.model_ir.get("geometry", {})
    sections = geometry.get("tower_sections", [])
    doors = geometry.get("door_openings", [])
    checks = {
        "tower_sections_present": bool(sections),
        "tower_has_at_least_two_sections": len(sections) >= 2,
        "tower_sections_positive_height": all(_num(s.get("z_top_mm")) > _num(s.get("z_bottom_mm")) for s in sections),
        "tower_dimensions_positive": all(_num(s.get("diameter_bottom_mm")) > 0 and _num(s.get("diameter_top_mm")) > 0 and _num(s.get("thickness_mm")) > 0 for s in sections),
        "door_geometry_valid": _door_geometry_valid(doors, sections),
    }
    issues = _issues_from_checks(checks, "geometry", "Geometry validation failed.")
    return _report("geometry_validation", checks, issues)


def _physics_report(bundle: IrBundle) -> ValidationReport:
    model = bundle.model_ir
    material = (model.get("materials") or [{}])[0]
    loads = model.get("loads", {})
    mesh = model.get("mesh", {})
    checks = {
        "material_name_present": bool(material.get("name")),
        "elastic_modulus_positive": _num(material.get("elastic_modulus_mpa")) > 0,
        "poisson_ratio_valid": 0 <= _num(material.get("poisson_ratio")) < 0.5,
        "yield_strength_present": material.get("yield_strength_mpa") is not None,
        "mesh_size_positive": _num(mesh.get("global_size_mm")) > 0 and _num(mesh.get("door_size_mm")) > 0,
        "top_load_nonnegative": _num(loads.get("top_total_force_n") or loads.get("top_horizontal_force_n")) >= 0,
        "wind_pressure_nonnegative": _num(loads.get("wind_pressure_mpa")) >= 0,
    }
    issues = []
    for key, ok in checks.items():
        if ok:
            continue
        severity = "warning" if key == "yield_strength_present" else "error"
        issues.append(ValidationIssue(field=key, severity=severity, category="physics", message="Physical parameter check failed."))
    if not loads.get("load_workbook_summary"):
        issues.append(ValidationIssue(field="model_ir.loads.load_workbook_summary", severity="warning", category="physics", message="No external load workbook summary is attached."))
    return _report("physics_validation", checks, issues)


def _abaqus_report(bundle: IrBundle) -> ValidationReport:
    abaqus = bundle.abaqus_ir
    steps = abaqus.get("steps", [])
    step_names = {step.get("name") for step in steps}
    loads = abaqus.get("loads", [])
    checks = {
        "s4r_element_declared": any(element.get("type") == "S4R" for element in abaqus.get("elements", [])),
        "shell_section_declared": any(section.get("type") == "Shell Section" for section in abaqus.get("sections", [])),
        "boundary_declared": bool(abaqus.get("boundary_conditions")),
        "static_step_declared": any(step.get("procedure") in {"static", "Static"} or "STATIC" in str(step.get("name", "")).upper() for step in steps),
        "modal_step_declared": any(step.get("procedure") in {"frequency", "modal"} or "MODAL" in str(step.get("name", "")).upper() for step in steps),
        "experimental_buckling_deferred": not any(step.get("procedure") in {"buckling", "buckle"} or "BUCK" in str(step.get("name", "")).upper() for step in steps),
        "load_step_refs_valid": all(not load.get("step") or load.get("step") in step_names for load in loads),
        "required_sets_declared": bool(abaqus.get("sets", {}).get("required_nsets")),
        "required_surfaces_declared": bool(abaqus.get("surfaces")),
    }
    issues = _issues_from_checks(checks, "abaqus_semantic", "Abaqus semantic validation failed.")
    return _report("abaqus_semantic_validation", checks, issues)


def _report(report_type: str, checks: dict[str, Any], issues: list[ValidationIssue]) -> ValidationReport:
    status = "blocked" if any(issue.severity == "error" for issue in issues) else ("warning" if issues else "pass")
    return ValidationReport(report_type=report_type, status=status, checks=checks, issues=issues)


def _issues_from_checks(checks: dict[str, Any], category: str, message: str) -> list[ValidationIssue]:
    return [
        ValidationIssue(field=field, severity="error", category=category, message=message)
        for field, ok in checks.items()
        if ok is False
    ]


def _door_geometry_valid(doors: list[dict[str, Any]], sections: list[dict[str, Any]]) -> bool:
    if not doors:
        return True
    door = doors[0]
    if not door.get("enabled"):
        return True
    width = _num(door.get("width_mm"))
    height = _num(door.get("height_mm"))
    bottom = _num(door.get("bottom_z_mm"))
    tower_height = max((_num(section.get("z_top_mm")) for section in sections), default=0.0)
    return width > 0 and height > 0 and bottom >= 0 and bottom + height <= tower_height


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
