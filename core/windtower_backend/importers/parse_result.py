from __future__ import annotations

from typing import Any, TypedDict

from .workbook_reader import WorkbookData


REQUIRED_SHEETS = ("tower", "flange", "door", "fem_params")
REQUIRED_DOOR_KEYS = ("X2", "Y2", "H")
REQUIRED_FEM_KEYS = (
    "material.name",
    "material.elastic_modulus_mpa",
    "material.poisson_ratio",
    "section.type",
    "element.element_type",
    "mesh.strategy",
    "loads.top_total_force_n",
    "loads.wind_pressure_mpa",
    "boundary.name",
)


class LlmParseAssistResult(TypedDict):
    enabled: bool
    authority: str
    policy: str
    suggested_sheet_map: dict[str, str | None]
    suggested_field_aliases: dict[str, str]
    missing_required_items: list[dict[str, Any]]
    suspicious_values: list[dict[str, Any]]
    patch_candidates: list[dict[str, Any]]
    rationale: str


class ExcelParseResult(TypedDict, total=False):
    source_file: str
    detected_format: str
    sheet_map: dict[str, str | None]
    raw_tables: dict[str, Any]
    parsed_facts: dict[str, Any]
    fem_params: dict[str, Any]
    fem_param_records: list[dict[str, Any]]
    fem_model_facts: dict[str, Any]
    derived_facts: dict[str, Any]
    standard_references: list[dict[str, Any]]
    warnings: list[str]
    missing_items: list[dict[str, Any]]
    llm_suggestions: LlmParseAssistResult
    confidence_report: dict[str, Any]


def build_raw_tables(workbook: WorkbookData, max_rows: int = 12, max_cols: int = 12) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    for name, sheet in workbook.sheets.items():
        rows = []
        for row_index in range(min(sheet.nrows, max_rows)):
            rows.append([sheet.cell(row_index, col_index) for col_index in range(min(sheet.ncols, max_cols))])
        tables[name] = {
            "rows": sheet.nrows,
            "cols": sheet.ncols,
            "preview": rows,
            "preview_limited": sheet.nrows > max_rows or sheet.ncols > max_cols,
        }
    return tables


def build_parsed_facts(
    tower: list[dict[str, Any]],
    flanges: list[dict[str, Any]],
    door: dict[str, Any],
    fem_params: dict[str, Any],
    loads_inventory: dict[str, Any],
) -> dict[str, Any]:
    return {
        "tower_point_count": len(tower),
        "tower_section_count": max(len(tower) - 1, 0),
        "flange_count": len(flanges),
        "door_keys": sorted(door),
        "door_complete": all(_has_number(door.get(key)) for key in REQUIRED_DOOR_KEYS),
        "fem_param_count": len(fem_params),
        "fem_required_keys_present": [key for key in REQUIRED_FEM_KEYS if fem_params.get(key) not in {None, ""}],
        "fem_required_keys_missing": [key for key in REQUIRED_FEM_KEYS if fem_params.get(key) in {None, ""}],
        "load_case_count": len(loads_inventory.get("ultimate_cases", [])),
        "fatigue_case_count": len(loads_inventory.get("fatigue_equivalent", [])),
        "occurrence_count": int(loads_inventory.get("occurrence_count") or 0),
        "source_locations": {
            "tower_first": tower[0].get("source") if tower else None,
            "tower_last": tower[-1].get("source") if tower else None,
            "flange_first": flanges[0].get("source") if flanges else None,
        },
    }


def build_confidence_report(
    sheet_map: dict[str, str | None],
    tower: list[dict[str, Any]],
    flanges: list[dict[str, Any]],
    door: dict[str, Any],
    fem_params: dict[str, Any],
    missing_items: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    checks = {
        "has_tower_sheet": bool(sheet_map.get("tower")),
        "has_flange_sheet": bool(sheet_map.get("flange")),
        "has_door_sheet": bool(sheet_map.get("door")),
        "has_fem_params_sheet": bool(sheet_map.get("fem_params")),
        "tower_profile_sufficient": len(tower) >= 2,
        "tower_z_strictly_increasing": _tower_z_strictly_increasing(tower),
        "flange_fields_complete": bool(flanges) and all(_has_number(item.get("total_thickness_mm")) for item in flanges),
        "door_fields_complete": all(_has_number(door.get(key)) for key in REQUIRED_DOOR_KEYS),
        "fem_required_fields_complete": all(fem_params.get(key) not in {None, ""} for key in REQUIRED_FEM_KEYS),
        "unit_system_declared": bool(fem_params.get("meta.unit_system")),
        "source_locations_available": all(item.get("source") for item in tower[:1] + flanges[:1]),
    }
    llm_reasons = _llm_trigger_reasons(checks, missing_items, warnings)
    score = sum(1 for ok in checks.values() if ok) / max(len(checks), 1)
    return {
        "strategy": "deterministic_first_llm_assisted",
        "score": round(score, 3),
        "status": "needs_llm_assist" if llm_reasons else ("pass" if not missing_items else "warning"),
        "checks": checks,
        "counts": {
            "tower_points": len(tower),
            "flanges": len(flanges),
            "door_values": len(door),
            "fem_params": len(fem_params),
            "missing_items": len(missing_items),
            "warnings": len(warnings),
        },
        "llm_assist_required": bool(llm_reasons),
        "llm_trigger_reasons": llm_reasons,
    }


def build_llm_suggestions(
    sheet_map: dict[str, str | None],
    fem_params: dict[str, Any],
    missing_items: list[dict[str, Any]],
    confidence_report: dict[str, Any],
) -> LlmParseAssistResult:
    reasons = confidence_report.get("llm_trigger_reasons", [])
    return {
        "enabled": bool(reasons),
        "authority": "advisory_only",
        "policy": "LLM suggestions must become patch_candidates and must not overwrite parsed facts directly.",
        "suggested_sheet_map": {key: value for key, value in sheet_map.items() if value},
        "suggested_field_aliases": {},
        "missing_required_items": missing_items,
        "suspicious_values": [],
        "patch_candidates": [],
        "rationale": _build_rationale(reasons, fem_params),
    }


def _llm_trigger_reasons(checks: dict[str, bool], missing_items: list[dict[str, Any]], warnings: list[str]) -> list[str]:
    reasons = []
    if not checks["has_tower_sheet"] or not checks["tower_profile_sufficient"]:
        reasons.append("tower_sheet_or_header_unrecognized")
    if not checks["has_door_sheet"] or not checks["door_fields_complete"]:
        reasons.append("door_fields_incomplete_or_unrecognized")
    if not checks["has_fem_params_sheet"] or not checks["fem_required_fields_complete"]:
        reasons.append("fem_key_value_parameters_missing")
    if not checks["unit_system_declared"]:
        reasons.append("unit_system_missing_or_ambiguous")
    if missing_items:
        reasons.append("required_items_missing")
    if any("unit" in warning.lower() for warning in warnings):
        reasons.append("unit_warning_detected")
    return sorted(set(reasons))


def _build_rationale(reasons: list[str], fem_params: dict[str, Any]) -> str:
    if not reasons:
        return "Deterministic parser produced a complete structured result; LLM assistance is not required for this workbook."
    missing_fem = [key for key in REQUIRED_FEM_KEYS if fem_params.get(key) in {None, ""}]
    pieces = ["Deterministic parsing produced low-confidence or incomplete items; LLM may propose advisory mappings or patch candidates."]
    if missing_fem:
        pieces.append("Missing FEM keys: " + ", ".join(missing_fem) + ".")
    pieces.append("Reasons: " + ", ".join(reasons) + ".")
    return " ".join(pieces)


def _tower_z_strictly_increasing(tower: list[dict[str, Any]]) -> bool:
    previous = None
    for point in tower:
        current = _number(point.get("z_mm"))
        if current is None:
            return False
        if previous is not None and current <= previous:
            return False
        previous = current
    return bool(tower)


def _has_number(value: Any) -> bool:
    return _number(value) is not None


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
