from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from openpyxl.utils.cell import range_boundaries

from .door_derivation import derive_door_candidates
from .fem_facts import build_fem_model_facts, value_type_for_key
from .parse_result import build_parsed_facts
from .standard_input import (
    CANDIDATE_MAPPING_SCHEMA_VERSION,
    STANDARD_INPUT_SCHEMA_VERSION,
    build_input_quality_scores,
    validate_standard_input,
)
from .standard_input.implementation import (
    _door_to_legacy,
    _field,
    _segments_to_tower_points,
)
from .workbook_reader import SheetData, read_workbook_data


FIELD_STATUSES = {"observed", "derived", "defaulted", "inferred", "unresolved"}
ALLOWED_OBJECT_PATHS = {
    "domain.wind_tower.tower_segments",
    "domain.wind_tower.door_opening",
    "generic.fem_settings",
}
SEGMENT_OBJECT_RE = re.compile(r"^domain\.wind_tower\.tower_segments\[(\d+)\]$")
SEGMENT_SCALAR_RE = re.compile(r"^domain\.wind_tower\.tower_segments\[(\d+)\]\.(z_bottom_mm|z_top_mm|diameter_bottom_mm|diameter_top_mm|thickness_mm)$")
DOOR_SCALAR_RE = re.compile(r"^domain\.wind_tower\.door_opening\.(enabled|shape|width_mm|height_mm|z_bottom_mm|corner_radius_mm|azimuth_deg)$")
MATERIAL_OBJECT_RE = re.compile(r"^generic\.materials\[(\d+)\]$")
MATERIAL_SCALAR_RE = re.compile(r"^generic\.materials\[(\d+)\]\.(name|elastic_modulus_mpa|poisson_ratio|density_tonne_mm3|yield_strength_mpa)$")
FEM_SCALAR_PREFIX = "generic.fem_settings."

REQUIRED_SEGMENT_KEYS = {"z_bottom_mm", "z_top_mm", "diameter_bottom_mm", "diameter_top_mm", "thickness_mm"}
REQUIRED_MATERIAL_KEYS = {"name", "elastic_modulus_mpa", "poisson_ratio", "density_tonne_mm3", "yield_strength_mpa"}
REQUIRED_FEM_BENCHMARK_KEYS = {
    "material.name",
    "material.elastic_modulus_mpa",
    "material.poisson_ratio",
    "section.type",
    "section.thickness_source",
    "element.element_type",
    "mesh.strategy",
    "mesh.global_size_mm",
    "mesh.door_size_mm",
    "boundary.name",
    "boundary.target",
    "boundary.dofs",
    "loads.top_total_force_n",
    "loads.top_mass_tonne",
    "loads.wind_pressure_mpa",
    "steps.STEP-1-TOP-REACTION",
    "steps.STEP-2-WIND-PRESSURE",
    "modal.modes",
}


def validate_candidate_mapping(mapping: dict[str, Any], workbook_path: Path | None = None) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    if mapping.get("schema_version") != CANDIDATE_MAPPING_SCHEMA_VERSION:
        issues.append(_issue("schema_version", "fatal", f"Expected {CANDIDATE_MAPPING_SCHEMA_VERSION}."))
    if any(key in mapping for key in ("abaqus_inp", "mesh", "final_ir", "solver_build_ir")):
        issues.append(_issue("forbidden_output", "fatal", "CandidateMappingIR contains a forbidden executable/formal output."))
    fields = mapping.get("fields")
    if not isinstance(fields, list) or not fields:
        issues.append(_issue("fields", "fatal", "CandidateMappingIR fields must be a non-empty list."))
        fields = []
    workbook = read_workbook_data(workbook_path) if workbook_path else None
    seen_paths: set[str] = set()
    for index, field in enumerate(fields):
        if not isinstance(field, dict):
            issues.append(_issue(f"fields[{index}]", "fatal", "Field record must be an object."))
            continue
        path = str(field.get("path") or field.get("canonical_path") or "").strip()
        if not path or not _allowed_path(path):
            issues.append(_issue(f"fields[{index}].path", "fatal", f"Unsupported canonical path: {path or '(empty)'}."))
        if path in seen_paths:
            issues.append(_issue(path, "fatal", "Duplicate canonical path."))
        seen_paths.add(path)
        status = str(field.get("status") or "").lower()
        if status not in FIELD_STATUSES:
            issues.append(_issue(path, "fatal", f"Unsupported field status: {status or '(empty)'}."))
        confidence = field.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0:
            issues.append(_issue(path, "fatal", "Confidence must be between 0 and 1."))
        if str(field.get("criticality") or "").lower() not in {"low", "medium", "high"}:
            issues.append(_issue(path, "fatal", "Criticality must be low, medium, or high."))
        if not str(field.get("mapping_reason") or "").strip():
            issues.append(_issue(path, "fatal", "mapping_reason is required."))
        evidence = field.get("evidence") or _flat_evidence(field)
        if not isinstance(evidence, dict):
            issues.append(_issue(path, "fatal", "Evidence must be an object."))
        elif workbook is not None:
            evidence_check = _validate_evidence(workbook.sheets, evidence)
            if not evidence_check["valid"]:
                issues.append(_issue(path, "fatal", evidence_check["message"]))
    blocked = any(item["severity"] == "fatal" for item in issues)
    return {
        "schema_version": "candidate_mapping_validation_v1",
        "status": "blocked" if blocked else "pass",
        "blocked": blocked,
        "field_count": len(fields),
        "issues": issues,
    }


def candidate_mapping_to_standard_input(mapping: dict[str, Any], workbook_path: Path, require_benchmark_fields: bool = False) -> dict[str, Any]:
    validation = validate_candidate_mapping(mapping, workbook_path)
    if validation["blocked"]:
        return {"promotion_status": "blocked", "candidate_validation": validation}

    segments: dict[int, dict[str, Any]] = {}
    door: dict[str, Any] = {}
    materials: dict[int, dict[str, Any]] = {}
    fem_settings: dict[str, Any] = {}
    traces: dict[str, dict[str, Any]] = {}
    for field in mapping["fields"]:
        path = str(field.get("path") or field.get("canonical_path"))
        value = _normalized_value(field)
        traces[path] = deepcopy(field.get("evidence") or _flat_evidence(field))
        if path == "domain.wind_tower.tower_segments":
            if isinstance(value, list):
                for index, item in enumerate(value):
                    segments[index] = _normalized_object(item, field.get("unit"), "segment")
            continue
        match = SEGMENT_OBJECT_RE.match(path)
        if match:
            segments[int(match.group(1))] = _normalized_object(value, field.get("unit"), "segment")
            continue
        match = SEGMENT_SCALAR_RE.match(path)
        if match:
            segments.setdefault(int(match.group(1)), {})[match.group(2)] = _normalize_scalar(value, field.get("normalized_unit") or field.get("unit"), match.group(2))
            continue
        if path == "domain.wind_tower.door_opening":
            door.update(_normalized_object(value, field.get("unit"), "door"))
            continue
        match = DOOR_SCALAR_RE.match(path)
        if match:
            door[match.group(1)] = _normalize_scalar(value, field.get("normalized_unit") or field.get("unit"), match.group(1))
            continue
        match = MATERIAL_OBJECT_RE.match(path)
        if match:
            materials[int(match.group(1))] = _normalized_object(value, field.get("unit"), "material")
            continue
        match = MATERIAL_SCALAR_RE.match(path)
        if match:
            materials.setdefault(int(match.group(1)), {})[match.group(2)] = _normalize_scalar(value, field.get("normalized_unit") or field.get("unit"), match.group(2))
            continue
        if path == "generic.fem_settings":
            if isinstance(value, dict):
                fem_settings.update(_flatten_fem_settings(value))
            continue
        if path.startswith(FEM_SCALAR_PREFIX):
            fem_key = path[len(FEM_SCALAR_PREFIX) :]
            fem_settings[fem_key] = _normalize_scalar(value, field.get("normalized_unit") or field.get("unit"), fem_key)

    ordered_segments = [segments[index] for index in sorted(segments)]
    ordered_materials = [materials[index] for index in sorted(materials)]
    if ordered_materials:
        material = ordered_materials[0]
        fem_settings.setdefault("material.name", material.get("name"))
        fem_settings.setdefault("material.elastic_modulus_mpa", material.get("elastic_modulus_mpa"))
        fem_settings.setdefault("material.poisson_ratio", material.get("poisson_ratio"))
        fem_settings.setdefault("material.density_tonne_mm3", material.get("density_tonne_mm3"))
        fem_settings.setdefault("material.yield_strength_mpa", material.get("yield_strength_mpa"))
        fem_settings.setdefault("section.material", material.get("name"))

    package = {
        "schema_version": STANDARD_INPUT_SCHEMA_VERSION,
        "source_file": workbook_path.name,
        "input_format": "llm_candidate_promoted",
        "unit_system": "mm-N-MPa-tonne",
        "tower_segments": [_wrap_segment(item, workbook_path.name, traces, index) for index, item in enumerate(ordered_segments)],
        "door_opening": _wrap_object(door, workbook_path.name, traces.get("domain.wind_tower.door_opening", {})),
        "materials": [_wrap_object(item, workbook_path.name, traces.get(f"generic.materials[{index}]", {})) for index, item in enumerate(ordered_materials)],
        "fem_settings": fem_settings,
        "source_trace": {
            "parser_name": "qwen_candidate_mapping_v1",
            "source_file": workbook_path.name,
            "profile_hash": mapping.get("profile_hash"),
            "candidate_paths": sorted(traces),
        },
    }
    fem_records = _fem_records(fem_settings, workbook_path.name, traces)
    candidate = {
        "source_file": workbook_path.name,
        "source_files": [workbook_path.name],
        "detected_format": "unknown_llm_assisted",
        "input_format": "llm_candidate_promoted",
        "format_confidence": 0.35,
        "sheet_map": _sheet_map(traces),
        "workbook_profile": {},
        "standard_input_candidate": package,
        "standard_input_package": package,
        "tower": _segments_to_tower_points(package["tower_segments"]),
        "flanges": [],
        "door": _door_to_legacy(package["door_opening"]),
        "fem_params": fem_settings,
        "fem_param_records": fem_records,
        "fem_model_facts": build_fem_model_facts(fem_settings, fem_records),
        "derived_facts": {},
        "standard_references": [{"id": "llm_candidate_mapping_v1", "type": "candidate_mapping", "title": "Evidence-grounded Qwen mapping", "note": "Formal FEM IR remains deterministic."}],
        "loads_inventory": _loads_inventory(fem_settings),
        "missing_items": [],
        "warnings": ["Synthetic benchmark input was normalized through CandidateMappingIR; not for engineering design."],
        "llm_suggestions": {"authority": "advisory_only"},
        "confidence_report": {"status": "mapped", "llm_assist_required": False, "llm_trigger_reasons": [], "candidate_field_count": len(mapping["fields"])},
        "parse_diagnostics": {"parser_name": "qwen_candidate_mapping_v1", "candidate_validation": validation, "source_trace": traces},
        "candidate_mapping_ir": mapping,
        "llm_candidate_mapping_ir": mapping,
        "llm_required": True,
        "parse_strategy": "llm_forced_parse",
    }
    candidate["derived_facts"] = {"door_derivation": derive_door_candidates(candidate["tower"], candidate["door"])}
    candidate["parsed_facts"] = build_parsed_facts(candidate["tower"], [], candidate["door"], fem_settings, candidate["loads_inventory"])
    standard_validation = validate_standard_input(candidate)
    if require_benchmark_fields:
        missing = sorted(key for key in REQUIRED_FEM_BENCHMARK_KEYS if fem_settings.get(key) in {None, ""})
        if missing:
            for key in missing:
                standard_validation.setdefault("issues", []).append(_issue(key, "fatal", "Benchmark-critical FEM field is missing from the LLM candidate."))
            standard_validation["blocked"] = True
            standard_validation["status"] = "blocked"
    candidate["validation_report"] = standard_validation
    candidate["candidate_validation"] = validation
    candidate["standard_input_validation"] = standard_validation
    candidate["input_quality_scores"] = build_input_quality_scores(candidate, standard_validation)
    candidate["requires_user_confirmation"] = standard_validation["blocked"]
    candidate["promotion_status"] = "blocked" if standard_validation["blocked"] else "candidate_validated"
    candidate["llm_promotion_status"] = candidate["promotion_status"]
    return candidate


def compare_standard_input_to_ground_truth(candidate: dict[str, Any], truth: dict[str, Any], relative_tolerance: float = 0.001) -> dict[str, Any]:
    package = candidate.get("standard_input_package") or {}
    comparisons: list[dict[str, Any]] = []
    _compare_segments(comparisons, package.get("tower_segments") or [], truth.get("tower_segments") or [], relative_tolerance)
    _compare_mapping(comparisons, "door_opening", _plain(package.get("door_opening") or {}), truth.get("door_opening") or {}, ("enabled", "width_mm", "height_mm", "z_bottom_mm"), relative_tolerance)
    materials = package.get("materials") or []
    truth_materials = truth.get("materials") or []
    _compare_mapping(comparisons, "materials[0]", _plain(materials[0] if materials else {}), truth_materials[0] if truth_materials else {}, tuple(REQUIRED_MATERIAL_KEYS), relative_tolerance)
    fem = candidate.get("fem_params") or package.get("fem_settings") or {}
    truth_fem = truth.get("fem_settings") or {}
    _compare_mapping(comparisons, "fem_settings", fem, truth_fem, tuple(sorted(REQUIRED_FEM_BENCHMARK_KEYS)), relative_tolerance)
    failures = [item for item in comparisons if not item["pass"]]
    return {
        "schema_version": "llm_ground_truth_comparison_v1",
        "status": "pass" if not failures else "blocked",
        "blocked": bool(failures),
        "relative_tolerance": relative_tolerance,
        "field_count": len(comparisons),
        "passed_count": len(comparisons) - len(failures),
        "accuracy": round((len(comparisons) - len(failures)) / max(len(comparisons), 1), 6),
        "failures": failures,
        "comparisons": comparisons,
    }


def _allowed_path(path: str) -> bool:
    return bool(path in ALLOWED_OBJECT_PATHS or SEGMENT_OBJECT_RE.match(path) or SEGMENT_SCALAR_RE.match(path) or DOOR_SCALAR_RE.match(path) or MATERIAL_OBJECT_RE.match(path) or MATERIAL_SCALAR_RE.match(path) or path.startswith(FEM_SCALAR_PREFIX))


def _normalized_value(field: dict[str, Any]) -> Any:
    normalization = field.get("normalization")
    if isinstance(normalization, dict) and "normalized_value" in normalization:
        return _decode_json_value(normalization["normalized_value"])
    if "normalized_value" in field:
        return _decode_json_value(field["normalized_value"])
    return _decode_json_value(field.get("value"))


def _flat_evidence(field: dict[str, Any]) -> dict[str, Any]:
    return {
        "sheet": field.get("source_sheet"),
        "cell_range": field.get("source_cell_or_range"),
        "raw_value": field.get("raw_value"),
    }


def _normalized_object(value: Any, unit: Any, kind: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    units = unit if isinstance(unit, dict) else {}
    normalized = {key: _normalize_scalar(item, units.get(key), key) for key, item in value.items()}
    if kind == "material" and "density_tonne_per_mm3" in normalized and "density_tonne_mm3" not in normalized:
        normalized["density_tonne_mm3"] = normalized.pop("density_tonne_per_mm3")
    return normalized


def _flatten_fem_settings(value: dict[str, Any]) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, dict):
            for child, child_value in item.items():
                canonical_child = "density_tonne_mm3" if child == "density_tonne_per_mm3" else child
                flattened[f"{key}.{canonical_child}"] = child_value
        else:
            flattened[key] = item
    return flattened


def _normalize_scalar(value: Any, unit: Any, key: str) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str) and key in {"enabled"}:
        return value.strip().lower() in {"true", "yes", "1", "on", "enabled", "是", "启用"}
    if not isinstance(value, (int, float)):
        return value
    unit_text = str(unit or "").strip().lower().replace("²", "2").replace("³", "3")
    number = float(value)
    if key.endswith("_mm") or key in {"mesh.global_size_mm", "mesh.door_size_mm"}:
        return number * {"m": 1000.0, "cm": 10.0, "mm": 1.0, "": 1.0}.get(unit_text, 1.0)
    if key.endswith("_n") or key == "loads.top_total_force_n":
        return number * {"kn": 1000.0, "n": 1.0, "": 1.0}.get(unit_text, 1.0)
    if key.endswith("_mpa") or "pressure_mpa" in key or "modulus_mpa" in key or "strength_mpa" in key:
        return number * {"gpa": 1000.0, "mpa": 1.0, "kpa": 0.001, "pa": 1e-6, "": 1.0}.get(unit_text, 1.0)
    if key.endswith("density_tonne_mm3"):
        return number * {"kg/m3": 1e-12, "tonne/mm3": 1.0, "t/mm3": 1.0, "": 1.0}.get(unit_text, 1.0)
    return number


def _wrap_segment(segment: dict[str, Any], source_file: str, traces: dict[str, dict[str, Any]], index: int) -> dict[str, Any]:
    object_trace = traces.get(f"domain.wind_tower.tower_segments[{index}]", traces.get("domain.wind_tower.tower_segments", {}))
    wrapped = {"segment_id": segment.get("segment_id") or f"SEG-{index + 1:02d}", "source": _source_text(object_trace, source_file)}
    for key in REQUIRED_SEGMENT_KEYS:
        trace = traces.get(f"domain.wind_tower.tower_segments[{index}].{key}", object_trace)
        wrapped[key] = _field(segment.get(key), "mm", str(trace.get("sheet") or trace.get("source_sheet") or ""), str(trace.get("cell_range") or trace.get("source_cell_or_range") or ""), trace.get("raw_value"), "confirmed_fact", float(trace.get("confidence") or 1.0))
    return wrapped


def _wrap_object(values: dict[str, Any], source_file: str, trace: dict[str, Any]) -> dict[str, Any]:
    if not values:
        return {}
    sheet = str(trace.get("sheet") or trace.get("source_sheet") or "")
    cells = str(trace.get("cell_range") or trace.get("source_cell_or_range") or "")
    return {key: _field(value, _canonical_unit(key), sheet, cells, trace.get("raw_value"), "confirmed_fact", 1.0) for key, value in values.items()}


def _fem_records(fem: dict[str, Any], source_file: str, traces: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    object_trace = traces.get("generic.fem_settings", {})
    records = []
    for path, value in sorted(fem.items()):
        trace = traces.get(f"generic.fem_settings.{path}", object_trace)
        records.append({
            "path": path,
            "group": path.split(".", 1)[0],
            "key": path.split(".", 1)[1] if "." in path else path,
            "value": value,
            "raw_value": trace.get("raw_value", value),
            "value_type": value_type_for_key(path),
            "unit": _canonical_unit(path),
            "source": _source_text(trace, source_file),
            "sheet": trace.get("sheet") or trace.get("source_sheet") or "",
            "cell_range": trace.get("cell_range") or trace.get("source_cell_or_range") or "",
            "description": "LLM candidate mapping validated by deterministic promotion.",
            "confidence": 1.0,
        })
    return records


def _loads_inventory(fem: dict[str, Any]) -> dict[str, Any]:
    return {
        "top_total_force_n": fem.get("loads.top_total_force_n"),
        "top_mass_tonne": fem.get("loads.top_mass_tonne"),
        "wind_pressure_mpa": fem.get("loads.wind_pressure_mpa"),
        "wind_surface": fem.get("loads.wind_surface", "WINDWARD_SURFACE"),
        "ultimate_cases": [],
    }


def _sheet_map(traces: dict[str, dict[str, Any]]) -> dict[str, str]:
    sheets = [str(item.get("sheet") or item.get("source_sheet") or "") for item in traces.values()]
    sheets = [item for item in sheets if item]
    return {"llm_evidence": sheets[0] if sheets else ""}


def _source_text(trace: dict[str, Any], source_file: str) -> str:
    sheet = trace.get("sheet") or trace.get("source_sheet") or ""
    cells = trace.get("cell_range") or trace.get("source_cell_or_range") or ""
    return f"{sheet}!{cells}" if sheet or cells else source_file


def _canonical_unit(key: str) -> str:
    if key.endswith("_mm") or ".global_size_mm" in key or ".door_size_mm" in key:
        return "mm"
    if key.endswith("_mpa"):
        return "MPa"
    if key.endswith("_n"):
        return "N"
    if key.endswith("_tonne"):
        return "tonne"
    if "density_tonne_mm3" in key:
        return "tonne/mm^3"
    return ""


def _validate_evidence(sheets: dict[str, SheetData], evidence: dict[str, Any]) -> dict[str, Any]:
    sheet_name = str(evidence.get("sheet") or evidence.get("source_sheet") or "").strip()
    cell_range = str(evidence.get("cell_range") or evidence.get("source_cell_or_range") or "").strip()
    if sheet_name not in sheets:
        return {"valid": False, "message": f"Evidence sheet does not exist: {sheet_name or '(empty)'}."}
    bounds = _range_bounds(cell_range)
    if bounds is None:
        return {"valid": False, "message": f"Evidence cell/range is invalid: {cell_range or '(empty)'}."}
    min_col, min_row, max_col, max_row = bounds
    sheet = sheets[sheet_name]
    values = [sheet.cell(row - 1, col - 1) for row in range(min_row, max_row + 1) for col in range(min_col, max_col + 1)]
    if not any(str(value).strip() for value in values):
        return {"valid": False, "message": f"Evidence range is empty: {sheet_name}!{cell_range}."}
    raw = evidence.get("raw_value")
    if raw is not None and not _raw_is_supported(raw, values):
        return {"valid": False, "message": f"Evidence range does not contain the declared raw value: {sheet_name}!{cell_range}."}
    return {"valid": True, "message": "Evidence range is present and non-empty."}


def _range_bounds(value: str) -> tuple[int, int, int, int] | None:
    text = value.strip().replace("$", "")
    if not text:
        return None
    match = re.fullmatch(r"R(\d+)C(\d+)(?::R(\d+)C(\d+))?", text, re.IGNORECASE)
    if match:
        r1, c1, r2, c2 = match.groups()
        return int(c1), int(r1), int(c2 or c1), int(r2 or r1)
    try:
        return range_boundaries(text)
    except ValueError:
        return None


def _raw_is_supported(raw: Any, evidence_values: list[Any]) -> bool:
    decoded = _decode_json_value(raw)
    if decoded is not raw:
        raw = decoded
    elif isinstance(raw, str):
        compact_raw = re.sub(r"\s+", "", raw).replace(";", ",").casefold()
        compact_evidence = re.sub(r"\s+", "", ",".join(str(item) for item in evidence_values if item not in {None, ""})).casefold()
        if compact_raw == compact_evidence:
            return True
    raw_values = _flatten(raw)
    evidence = [item for item in evidence_values if item not in {None, ""}]
    for expected in raw_values:
        if expected in {None, ""}:
            continue
        if not any(_equivalent(expected, actual) for actual in evidence):
            return False
    return True


def _decode_json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return value


def _flatten(value: Any) -> list[Any]:
    if isinstance(value, dict):
        result: list[Any] = []
        for item in value.values():
            result.extend(_flatten(item))
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            result.extend(_flatten(item))
        return result
    return [value]


def _equivalent(left: Any, right: Any) -> bool:
    try:
        return math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-9)
    except (TypeError, ValueError):
        return str(left).strip().casefold() == str(right).strip().casefold()


def _compare_segments(out: list[dict[str, Any]], actual: list[dict[str, Any]], expected: list[dict[str, Any]], tolerance: float) -> None:
    out.append({"path": "tower_segments.length", "actual": len(actual), "expected": len(expected), "pass": len(actual) == len(expected)})
    for index, truth in enumerate(expected):
        item = _plain(actual[index]) if index < len(actual) else {}
        for key in REQUIRED_SEGMENT_KEYS:
            out.append(_comparison(f"tower_segments[{index}].{key}", item.get(key), truth.get(key), tolerance))


def _compare_mapping(out: list[dict[str, Any]], prefix: str, actual: dict[str, Any], expected: dict[str, Any], keys: tuple[str, ...], tolerance: float) -> None:
    for key in keys:
        out.append(_comparison(f"{prefix}.{key}", actual.get(key), expected.get(key), tolerance))


def _comparison(path: str, actual: Any, expected: Any, tolerance: float) -> dict[str, Any]:
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        try:
            passed = math.isclose(float(actual), float(expected), rel_tol=tolerance, abs_tol=max(1e-9, abs(float(expected)) * tolerance))
        except (TypeError, ValueError):
            passed = False
    elif isinstance(expected, list):
        passed = list(actual or []) == expected
    else:
        passed = str(actual).strip().casefold() == str(expected).strip().casefold()
    return {"path": path, "actual": actual, "expected": expected, "pass": passed}


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        if "value" in value:
            return value.get("value")
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _issue(path: str, severity: str, message: str) -> dict[str, Any]:
    return {"path": path, "severity": severity, "message": message}
