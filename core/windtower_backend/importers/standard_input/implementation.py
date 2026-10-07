"""Deterministic normalization for supported engineering input formats."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

from ..door_derivation import derive_door_candidates
from ..fem_facts import (
    REQUIRED_FEM_KEYS,
    build_fem_model_facts,
    parse_value_by_type,
    parse_wind_pressure_profile,
    value_type_for_key,
)
from ..parse_result import build_confidence_report, build_llm_suggestions, build_parsed_facts, build_raw_tables
from ..validation import validate_import_facts
from ..workbook_reader import SheetData, WorkbookData, read_workbook_data


STANDARD_INPUT_SCHEMA_VERSION = "standard_input_v1"
CANDIDATE_MAPPING_SCHEMA_VERSION = "candidate_mapping_ir_v1"
FORMAT_ALIAS_REGISTRY = Path(__file__).resolve().parents[1] / "storage" / "format_alias_registry.json"


def infer_model_family(
    import_meta: dict[str, Any] | None = None,
    modeling_goal: str | None = None,
) -> dict[str, Any]:
    return {
        "model_family": "wind_tower",
        "structure_family": "wind_tower_shell_with_door",
        "confidence": 1.0,
        "source": "github_v1_bounded_scope",
        "alternatives": [],
    }


def build_candidate_mapping_ir(
    candidate: dict[str, Any],
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    package = (
        candidate.get("standard_input_package")
        or candidate.get("standard_input_candidate")
        or {}
    )
    profile = profile or candidate.get("workbook_profile") or {}
    fields = []
    for index, segment in enumerate(package.get("tower_segments") or []):
        for key in (
            "z_bottom_mm",
            "z_top_mm",
            "diameter_bottom_mm",
            "diameter_top_mm",
            "thickness_mm",
        ):
            fields.append(_mapping_field(
                f"domain.wind_tower.tower_segments[{index}].{key}",
                segment.get(key),
            ))
    door = package.get("door_opening") or {}
    for key in ("enabled", "width_mm", "height_mm", "z_bottom_mm"):
        fields.append(_mapping_field(
            f"domain.wind_tower.door_opening.{key}",
            door.get(key),
        ))
    for index, material in enumerate(package.get("materials") or []):
        fields.append(_mapping_field(
            f"generic.materials[{index}]",
            material,
        ))
    fields.append(_mapping_field(
        "generic.fem_settings",
        package.get("fem_settings") or {},
    ))
    return {
        "schema_version": CANDIDATE_MAPPING_SCHEMA_VERSION,
        "model_family": "wind_tower",
        "source_file": (
            candidate.get("source_file")
            or package.get("source_file")
            or profile.get("source_file")
        ),
        "fields": fields,
        "unresolved": candidate.get("missing_items") or [],
        "authority": "candidate_only",
    }


def _mapping_field(path: str, value: Any) -> dict[str, Any]:
    observed = value is not None and value != ""
    return {
        "path": path,
        "normalized_value": value,
        "status": "observed" if observed else "unresolved",
        "confidence": 0.95 if observed else 0.0,
        "source_kind": "file_observed",
        "evidence_ids": [],
    }

FIELD_ALIASES = {
    "material.name": ["material", "材料名", "材料", "steel", "section material"],
    "material.elastic_modulus_mpa": ["elastic", "elastic_modulus", "e", "E\nMPa", "弹性模量"],
    "material.poisson_ratio": ["poisson", "ν", "泊松比"],
    "material.density_tonne_mm3": ["density", "密度"],
    "material.yield_strength_mpa": ["fy", "yield", "屈服"],
    "section.type": ["shell section", "壳截面", "截面"],
    "section.material": ["section material", "截面材料", "材料"],
    "section.thickness_source": ["thickness source", "厚度来源"],
    "element.element_type": ["element_type", "单元类型", "S4R"],
    "mesh.strategy": ["mesh strategy", "网格策略"],
    "mesh.global_size_mm": ["mesh_global", "全局网格尺寸", "global mesh"],
    "mesh.door_size_mm": ["mesh_door", "门洞局部尺寸", "door mesh"],
    "loads.top_total_force_n": ["top_total_force", "塔顶载荷", "Abaqus Fx", "Abaqus Fy"],
    "loads.wind_pressure_mpa": ["wind_pressure", "面压力", "风压"],
    "loads.top_mass_tonne": ["RP_TOP_MASS", "顶部总等效质量", "顶部质量"],
    "loads.top_force_direction": ["top_force_direction", "投影角"],
    "boundary.name": ["boundary", "底部边界", "ENCASTRE"],
    "boundary.target": ["base set", "SET_BASE_RING", "BASE_FIXED"],
    "boundary.dofs": ["DOF", "1-6", "全固定"],
}

STANDARD_HINTS = [
    "tower",
    "segment",
    "z_bottom",
    "z_top",
    "diameter",
    "thickness",
    "door",
    "material",
    "S4R",
    "Shell Section",
    "boundary",
    "load case",
    "modal",
    "frequency",
    "wind pressure",
    "top load",
]

CRITICAL_FEM_KEYS = {"section.type", "element.element_type"}
DEFAULTABLE_FEM_DEFAULTS = {
    "material.name": "STEEL",
    "material.elastic_modulus_mpa": 210000.0,
    "material.poisson_ratio": 0.3,
    "mesh.strategy": "structured_quad_shell",
    "mesh.global_size_mm": 650.0,
    "mesh.door_size_mm": 120.0,
    "loads.top_total_force_n": 0.0,
    "loads.wind_pressure_mpa": 0.0012,
    "boundary.name": "BASE_FIXED",
    "modal.modes": 12,
    "outputs.field": ["U", "RF", "S", "E"],
}
RECOMMENDED_DEFAULTABLE_FEM_KEYS = {"mesh.global_size_mm", "mesh.door_size_mm", "modal.modes", "outputs.field"}


def profile_workbook(path: str | Path, max_preview_rows: int = 40, max_preview_cols: int = 16) -> dict[str, Any]:
    workbook = read_workbook_data(path)
    sheets = [_profile_sheet(sheet, max_preview_rows=max_preview_rows, max_preview_cols=max_preview_cols) for sheet in workbook.sheets.values()]
    fingerprint_text = json.dumps(
        [{"name": sheet["name"], "headers": sheet["header_candidates"][:3], "shape": sheet["shape"]} for sheet in sheets],
        ensure_ascii=False,
        sort_keys=True,
    )
    fmt = detect_workbook_format_from_profile({"sheets": sheets})
    return {
        "schema_version": "workbook_profile_v1",
        "source_file": Path(path).name,
        "sheet_count": len(sheets),
        "sheets": sheets,
        "input_format": fmt["input_format"],
        "input_family": fmt.get("input_family", fmt["input_format"]),
        "format_confidence": fmt["confidence"],
        "format_reasons": fmt["reasons"],
        "profile_hash": hashlib.sha256(fingerprint_text.encode("utf-8")).hexdigest()[:16],
        "standard_target": STANDARD_INPUT_SCHEMA_VERSION,
        "llm_mapping_contract": {
            "target": "CandidateMappingIR",
            "schema_version": CANDIDATE_MAPPING_SCHEMA_VERSION,
            "canonical_payload": "StandardInputPackage",
            "json_only": True,
            "required_trace_fields": ["value", "unit", "status", "evidence", "normalization", "criticality"],
            "field_status_values": ["observed", "derived", "defaulted", "inferred", "unresolved"],
            "forbidden_outputs": ["abaqus_inp", "mesh", "final_ir"],
            "promotion_policy": "LLM writes candidate mappings only; deterministic validation promotes formal IR fields.",
        },
        "token_policy": "Compact sheet previews only; never send complete workbooks to the LLM.",
    }


def detect_workbook_format(workbook: WorkbookData) -> dict[str, Any]:
    sheets = [_profile_sheet(sheet, max_preview_rows=8, max_preview_cols=12) for sheet in workbook.sheets.values()]
    return detect_workbook_format_from_profile({"sheets": sheets})


def detect_workbook_format_from_profile(profile: dict[str, Any]) -> dict[str, Any]:
    names = {str(sheet.get("name", "")).lower() for sheet in profile.get("sheets", [])}
    normalized_names = {_norm(name) for name in names}
    profile_text = _norm(
        json.dumps(
            [
                {
                    "name": sheet.get("name", ""),
                    "headers": sheet.get("header_candidates", [])[:4],
                    "semantic_hints": sheet.get("semantic_hints", {}),
                }
                for sheet in profile.get("sheets", [])
            ],
            ensure_ascii=False,
        )
    )
    semantic_hits = sum(
        1
        for token in ("01global", "02segments", "03door", "04materials", "05fem", "06loadcases")
        if any(token in name for name in normalized_names)
    )
    gpt_hits = sum(
        1
        for token in (
            "gpt",
            "generated",
            "standardinput",
            "standardinputpackage",
            "towersegments",
            "dooropening",
            "femsettings",
            "loadcases",
            "jsonschema",
        )
        if token in profile_text
    )
    legacy_hits = sum(1 for name in normalized_names if any(token in name for token in ("塔节", "塔筒", "法兰", "门洞", "门框", "fem")))
    if semantic_hits >= 3:
        return {"input_format": "semantic_standard_workbook", "input_family": "engineering_workbook", "confidence": min(0.98, 0.55 + semantic_hits * 0.07), "reasons": [f"semantic_sheet_hits={semantic_hits}"]}
    if gpt_hits >= 3:
        return {"input_format": "gpt_generated_workbook", "input_family": "gpt_generated_workbook", "confidence": min(0.9, 0.45 + gpt_hits * 0.06), "reasons": [f"gpt_schema_hits={gpt_hits}"]}
    if legacy_hits >= 2:
        return {"input_format": "windtower_mechanical_interface", "input_family": "engineering_workbook", "confidence": min(0.95, 0.55 + legacy_hits * 0.08), "reasons": [f"legacy_sheet_hits={legacy_hits}"]}
    return {"input_format": "unknown_llm_assisted", "input_family": "unknown_llm_assisted", "confidence": 0.35, "reasons": ["No known workbook sheet pattern matched."]}


def normalize_import_to_standard_input(path: str | Path, enable_llm: bool = False, force_llm: bool = False) -> dict[str, Any]:
    workbook = read_workbook_data(path)
    profile = profile_workbook(path)
    if profile["input_format"] in {"semantic_standard_workbook", "gpt_generated_workbook"}:
        candidate = parse_standard_workbook(path, workbook=workbook, profile=profile)
    elif profile["input_format"] == "windtower_mechanical_interface":
        candidate = parse_windtower_mechanical_interface_workbook(path, workbook=workbook, profile=profile)
    else:
        candidate = _heuristic_standard_input_candidate(path, workbook, profile)
    validation = validate_standard_input(candidate)
    candidate["validation_report"] = validation
    candidate["input_quality_scores"] = build_input_quality_scores(candidate, validation)
    candidate["requires_user_confirmation"] = bool(validation["blocked"] or validation["requires_user_confirmation"])
    llm_required = bool(validation["blocked"] or profile.get("input_format") in {"unknown_llm_assisted", "gpt_generated_workbook"})
    candidate["parse_strategy"] = "llm_assisted" if llm_required else "deterministic"
    candidate["llm_required"] = llm_required
    candidate["promotion_status"] = "promoted" if not validation["blocked"] else "blocked"
    candidate["llm_parse_assist"] = llm_normalize_workbook(
        profile,
        STANDARD_INPUT_SCHEMA_VERSION,
        STANDARD_HINTS,
        enable_llm=enable_llm,
        force_llm=force_llm,
        validation=validation,
    )
    candidate["model_family_detection"] = infer_model_family(candidate)
    candidate["candidate_mapping_ir"] = build_candidate_mapping_ir(candidate, profile)
    return candidate


def build_input_quality_scores(candidate: dict[str, Any], validation: dict[str, Any] | None = None) -> dict[str, Any]:
    validation = validation or candidate.get("validation_report") or validate_standard_input(candidate)
    package = candidate.get("standard_input_package") or candidate.get("standard_input_candidate") or {}
    fem_params = candidate.get("fem_params", {}) or {}
    profile = candidate.get("workbook_profile", {}) or {}
    issues = validation.get("issues", []) or []
    defaults = validation.get("defaults_applied", []) or []
    missing = validation.get("missing_items", []) or []

    segments = package.get("tower_segments", []) or []
    valid_segments = 0
    for segment in segments:
        values = [
            _field_value(segment.get("z_bottom_mm")),
            _field_value(segment.get("z_top_mm")),
            _field_value(segment.get("diameter_bottom_mm")),
            _field_value(segment.get("diameter_top_mm")),
            _field_value(segment.get("thickness_mm")),
        ]
        if all(not _is_missing_value(value) for value in values):
            valid_segments += 1
    geometry_score = 1.0 if segments and valid_segments == len(segments) else (valid_segments / max(len(segments), 1))

    door = package.get("door_opening", {}) or {}
    door_enabled = _field_value(door.get("enabled")) is not False
    if door_enabled:
        door_keys = ["width_mm", "height_mm", "z_bottom_mm"]
        door_score = sum(1 for key in door_keys if not _is_missing_value(_field_value(door.get(key)))) / len(door_keys)
    else:
        door_score = 1.0
    geometry_score = round(min(1.0, geometry_score * 0.75 + door_score * 0.25), 3)

    fem_keys = sorted(set(REQUIRED_FEM_KEYS) | set(DEFAULTABLE_FEM_DEFAULTS) | {"material.elastic_modulus_mpa", "material.poisson_ratio"})
    fem_present = sum(1 for key in fem_keys if not _is_missing_value(fem_params.get(key)))
    fem_score = round(fem_present / max(len(fem_keys), 1), 3)

    loads_inventory = candidate.get("loads_inventory", {}) or {}
    load_keys = ["loads.top_total_force_n", "loads.wind_pressure_mpa", "loads.top_force_direction"]
    load_present = sum(1 for key in load_keys if not _is_missing_value(fem_params.get(key)))
    if loads_inventory.get("ultimate_cases"):
        load_present += 1
        load_total = len(load_keys) + 1
    else:
        load_total = len(load_keys)
    load_score = round(load_present / max(load_total, 1), 3)

    trace_items = 0
    trace_total = 0
    for segment in segments[:5]:
        for key in ("z_bottom_mm", "z_top_mm", "diameter_bottom_mm", "diameter_top_mm", "thickness_mm"):
            trace_total += 1
            value = segment.get(key)
            if isinstance(value, dict) and value.get("source_sheet") and value.get("source_cell_or_range"):
                trace_items += 1
    for record in candidate.get("fem_param_records", [])[:20]:
        trace_total += 1
        if record.get("sheet") and record.get("cell_range"):
            trace_items += 1
    trace_score = round(trace_items / max(trace_total, 1), 3) if trace_total else 0.0

    blocked_count = sum(1 for item in missing if item.get("severity") == "blocked") + sum(1 for item in issues if item.get("severity") == "error")
    warning_count = sum(1 for item in missing if item.get("severity") == "warning") + sum(1 for item in issues if item.get("severity") == "warning")
    readiness = "blocked" if validation.get("blocked") or blocked_count else ("warning" if warning_count or defaults else "pass")
    return {
        "schema_version": "input_quality_scores_v1",
        "readiness": readiness,
        "format_confidence": round(float(profile.get("format_confidence") or candidate.get("format_confidence") or 0.0), 3),
        "geometry_completeness": geometry_score,
        "fem_completeness": fem_score,
        "load_completeness": load_score,
        "traceability_score": trace_score,
        "blocked_issue_count": blocked_count,
        "warning_issue_count": warning_count,
        "defaults_applied_count": len(defaults),
        "recovered_fields_count": len(candidate.get("recovered_fields") or (candidate.get("parse_diagnostics") or {}).get("recovered_fields") or []),
        "llm_required": bool(candidate.get("llm_required") or profile.get("input_format") in {"unknown_llm_assisted", "gpt_generated_workbook"}),
        "policy": "Rule validation decides promotion; LLM output remains advisory until confirmed.",
    }


def parse_windtower_mechanical_interface_workbook(path: str | Path, workbook: WorkbookData | None = None, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    workbook = workbook or read_workbook_data(path)
    profile = profile or profile_workbook(path)
    source_file = Path(path).name
    tower_sheet = _sheet_like(workbook, "塔节", "塔筒", "segment")
    flange_sheet = _sheet_like(workbook, "法兰", "flange")
    door_sheet = _sheet_like(workbook, "门洞", "门框", "door")
    fem_sheet = _sheet_like(workbook, "FEM", "模型参数")
    segments, tower_points, tower_warnings = _parse_mechanical_tower_segments(tower_sheet)
    flanges = _parse_mechanical_flanges(flange_sheet)
    door, door_legacy, door_warnings = _parse_mechanical_door(door_sheet)
    fem_params, fem_records = _parse_key_value_fem_settings(fem_sheet, source_file)
    warnings = [*tower_warnings, *door_warnings]
    material_name = fem_params.get("material.name") or "Q345"
    _set_default_param(fem_params, fem_records, source_file, "material", "name", material_name, "", "mechanical_interface_default", "Default steel material name.")
    _set_default_param(fem_params, fem_records, source_file, "material", "elastic_modulus_mpa", 206000.0, "MPa", "mechanical_interface_default", "Default steel elastic modulus.")
    _set_default_param(fem_params, fem_records, source_file, "material", "poisson_ratio", 0.3, "", "mechanical_interface_default", "Default steel Poisson ratio.")
    _set_default_param(fem_params, fem_records, source_file, "section", "type", "Shell Section", "", "mechanical_interface_default", "Abaqus shell section.")
    _set_default_param(fem_params, fem_records, source_file, "section", "thickness_source", "excel_tower_sections", "", "mechanical_interface_default", "Use tower segment thicknesses.")
    _set_default_param(fem_params, fem_records, source_file, "section", "material", material_name, "", "mechanical_interface_default", "Section material.")
    _set_default_param(fem_params, fem_records, source_file, "element", "element_type", "S4R", "", "mechanical_interface_default", "Default shell element.")
    _set_default_param(fem_params, fem_records, source_file, "mesh", "strategy", "structured_quad_shell", "", "mechanical_interface_default", "Default L0 shell mesh strategy.")
    _set_default_param(fem_params, fem_records, source_file, "mesh", "global_size_mm", 650.0, "mm", "mechanical_interface_default", "Default global mesh size.")
    _set_default_param(fem_params, fem_records, source_file, "mesh", "door_size_mm", 120.0, "mm", "mechanical_interface_default", "Default door mesh size.")
    _set_default_param(fem_params, fem_records, source_file, "boundary", "name", "BASE_FIXED", "", "mechanical_interface_default", "Base fixed boundary.")
    _set_default_param(fem_params, fem_records, source_file, "boundary", "target", "BASE_FIXED", "", "mechanical_interface_default", "Base fixed set.")
    _set_default_param(fem_params, fem_records, source_file, "boundary", "dofs", [1, 2, 3, 4, 5, 6], "", "mechanical_interface_default", "Fixed DOF 1-6.")
    _set_default_param(fem_params, fem_records, source_file, "loads", "top_total_force_n", 0.0, "N", "mechanical_interface_default", "Default top force.")
    _set_default_param(fem_params, fem_records, source_file, "loads", "wind_pressure_mpa", 0.0012, "MPa", "mechanical_interface_default", "Default wind pressure.")
    materials = [
        {
            "name": material_name,
            "elastic_modulus_mpa": fem_params.get("material.elastic_modulus_mpa"),
            "poisson_ratio": fem_params.get("material.poisson_ratio"),
            "density_tonne_mm3": fem_params.get("material.density_tonne_mm3"),
            "yield_strength_mpa": fem_params.get("material.yield_strength_mpa"),
            "source": _record_source(fem_records, "material.name") or _record_source(fem_records, "material.elastic_modulus_mpa") or "mechanical_interface_default",
        }
    ]
    loads_inventory = {
        "source": "mechanical_interface_fem_params",
        "top_total_force_n": fem_params.get("loads.top_total_force_n"),
        "wind_pressure_mpa": fem_params.get("loads.wind_pressure_mpa"),
        "ultimate_cases": [],
        "warnings": [],
    }
    sheet_map = {
        "tower": _name_or_none(tower_sheet),
        "flange": _name_or_none(flange_sheet),
        "door": _name_or_none(door_sheet),
        "fem_params": _name_or_none(fem_sheet),
        "materials": _name_or_none(fem_sheet),
        "load_cases": _name_or_none(fem_sheet),
    }
    recovered_fields = [field for flange in flanges for field in flange.get("recovered_fields", [])]
    missing_items, fact_warnings = validate_import_facts(tower_points, flanges, door_legacy, fem_params, loads_inventory)
    warnings.extend(fact_warnings)
    standard_package = {
        "schema_version": STANDARD_INPUT_SCHEMA_VERSION,
        "source_file": source_file,
        "input_format": "windtower_mechanical_interface",
        "format_confidence": profile.get("format_confidence", 0.0),
        "profile_hash": profile.get("profile_hash"),
        "global_parameters": {},
        "tower_segments": segments,
        "door_opening": door,
        "materials": materials,
        "section_rows": [],
        "fem_settings": _records_to_standard_fields(fem_records),
        "load_cases": [],
        "modal_settings": {},
        "output_requests": [],
        "source_trace": {"sheet_map": sheet_map, "source_file": source_file, "parser_name": "windtower_mechanical_interface_v2"},
    }
    parsed_facts = build_parsed_facts(tower_points, flanges, door_legacy, fem_params, loads_inventory)
    confidence_report = build_confidence_report(sheet_map, tower_points, flanges, door_legacy, fem_params, missing_items, warnings)
    return {
        "source_file": source_file,
        "source_files": [source_file],
        "detected_format": "windtower_mechanical_interface",
        "input_format": "windtower_mechanical_interface",
        "format_confidence": profile.get("format_confidence", 0.0),
        "sheet_map": sheet_map,
        "raw_tables": build_raw_tables(workbook),
        "workbook_profile": profile,
        "standard_input_package": standard_package,
        "standard_input_candidate": standard_package,
        "parsed_facts": parsed_facts,
        "tower": tower_points,
        "flanges": flanges,
        "door": door_legacy,
        "fem_params": fem_params,
        "fem_param_records": fem_records,
        "fem_model_facts": build_fem_model_facts(fem_params, fem_records),
        "derived_facts": {"door_derivation": derive_door_candidates(tower_points, door_legacy)},
        "standard_references": [{"id": "manual_rule:mechanical_interface_adapter", "type": "manual_rule", "title": "Mechanical interface normalization", "note": "Tower section points were converted to StandardInputPackage tower segments."}],
        "loads_inventory": loads_inventory,
        "missing_items": missing_items,
        "warnings": warnings,
        "recovered_fields": recovered_fields,
        "llm_suggestions": build_llm_suggestions(sheet_map, fem_params, missing_items, confidence_report),
        "confidence_report": confidence_report,
        "parse_diagnostics": {
            "missing_items": missing_items,
            "warnings": warnings,
            "recovered_fields": recovered_fields,
            "recovered_fields_count": len(recovered_fields),
            "format_reasons": profile.get("format_reasons", []),
            "parser_name": "windtower_mechanical_interface_v2",
        },
    }


def parse_standard_workbook(path: str | Path, workbook: WorkbookData | None = None, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    workbook = workbook or read_workbook_data(path)
    profile = profile or profile_workbook(path)
    source_file = Path(path).name
    segments = _parse_segments(_sheet_like(workbook, "02", "segment", "塔节"))
    door = _parse_door(_sheet_like(workbook, "03", "door", "门洞"))
    materials, section_rows, top_mass = _parse_materials(_sheet_like(workbook, "04", "material", "材料"))
    fem_params, fem_records = _parse_fem_settings(_sheet_like(workbook, "05", "fem", "有限元"), source_file)
    load_cases, wind_rows = _parse_load_cases(_sheet_like(workbook, "06", "load", "载荷"))
    modal = _parse_modal(_sheet_like(workbook, "07", "modal", "模态"))
    outputs = _parse_outputs(_sheet_like(workbook, "08", "output", "输出"))

    material = materials[0] if materials else {}
    if material:
        _set_param(fem_params, fem_records, "material", "name", material.get("name"), "", source_file, material.get("source", ""), "Primary material name.")
        _set_param(fem_params, fem_records, "material", "elastic_modulus_mpa", material.get("elastic_modulus_mpa"), "MPa", source_file, material.get("source", ""), "Elastic modulus.")
        _set_param(fem_params, fem_records, "material", "poisson_ratio", material.get("poisson_ratio"), "", source_file, material.get("source", ""), "Poisson ratio.")
        _set_param(fem_params, fem_records, "material", "density_tonne_mm3", material.get("density_tonne_mm3"), "tonne/mm^3", source_file, material.get("source", ""), "Density.")
        _set_param(fem_params, fem_records, "material", "yield_strength_mpa", material.get("yield_strength_mpa"), "MPa", source_file, material.get("source", ""), "Yield strength.")
    _set_default_param(fem_params, fem_records, source_file, "section", "type", "Shell Section", "", "standard_input_default", "Abaqus shell section.")
    _set_default_param(fem_params, fem_records, source_file, "section", "thickness_source", "excel_tower_sections", "", "standard_input_default", "Use tower segment thicknesses.")
    _set_default_param(fem_params, fem_records, source_file, "section", "material", material.get("name") or "STEEL", "", "standard_input_default", "Section material.")
    _set_default_param(fem_params, fem_records, source_file, "mesh", "strategy", "structured_quad_shell", "", "standard_input_default", "Default L0 shell mesh strategy.")
    _set_default_param(fem_params, fem_records, source_file, "boundary", "name", "BASE_FIXED", "", "standard_input_default", "Base fixed boundary.")
    _set_default_param(fem_params, fem_records, source_file, "boundary", "target", "BASE_FIXED", "", "standard_input_default", "Base fixed set.")
    _set_default_param(fem_params, fem_records, source_file, "boundary", "dofs", [1, 2, 3, 4, 5, 6], "", "standard_input_default", "Fixed DOF 1-6.")
    _set_default_param(fem_params, fem_records, source_file, "element", "part_name", "WINDTOWER_SHELL_V3", "", "standard_input_default", "Abaqus part name.")
    _set_default_param(fem_params, fem_records, source_file, "element", "instance_name", "WINDTOWER_SHELL_V3-1", "", "standard_input_default", "Abaqus instance name.")
    if top_mass is not None:
        _set_param(fem_params, fem_records, "loads", "top_mass_tonne", top_mass, "tonne", source_file, "04_Materials:auto", "Top equivalent mass.")
    if load_cases:
        primary = load_cases[0]
        fx = _number(primary.get("Fx_N")) or 0.0
        fy = _number(primary.get("Fy_N")) or 0.0
        top_force = math.sqrt(fx * fx + fy * fy)
        _set_param(fem_params, fem_records, "loads", "top_total_force_n", top_force, "N", source_file, primary.get("source", ""), "Top horizontal force magnitude from first load case.")
        _set_param(fem_params, fem_records, "loads", "top_force_direction", _direction_from_fx_fy(fx, fy), "", source_file, primary.get("source", ""), "Dominant top force direction.")
    wind_pressure = _first_number([row.get("pressure_mpa") for row in wind_rows])
    if wind_pressure is not None:
        _set_param(fem_params, fem_records, "loads", "wind_pressure_mpa", wind_pressure, "MPa", source_file, "06_LoadCases:wind", "Wind pressure from simplified tower wind load table.")
    if modal.get("modes"):
        _set_param(fem_params, fem_records, "modal", "modes", modal.get("modes"), "", source_file, modal.get("source", ""), "Modal eigenvalue count.")

    tower_points = _segments_to_tower_points(segments)
    door_legacy = _door_to_legacy(door)
    loads_inventory = {
        "ultimate_cases": [
            {
                "source_file": source_file,
                "sheet": item.get("sheet", "06_LoadCases"),
                "component": "top_envelope",
                "extreme": item.get("case_id"),
                "load_case": item.get("case_id"),
                "Fx_kN": (_number(item.get("Fx_N")) or 0.0) / 1000.0,
                "Fy_kN": (_number(item.get("Fy_N")) or 0.0) / 1000.0,
                "Fz_kN": (_number(item.get("Fz_N")) or 0.0) / 1000.0,
                "Mx_kNm": (_number(item.get("Mx_Nmm")) or 0.0) / 1_000_000.0,
                "My_kNm": (_number(item.get("My_Nmm")) or 0.0) / 1_000_000.0,
                "Mz_kNm": (_number(item.get("Mz_Nmm")) or 0.0) / 1_000_000.0,
            }
            for item in load_cases
        ],
        "wind_pressure_rows": wind_rows,
        "source": "standard_input_workbook",
        "warnings": [],
    }
    sheet_map = {
        "tower": _name_or_none(_sheet_like(workbook, "02", "segment", "塔节")),
        "flange": None,
        "door": _name_or_none(_sheet_like(workbook, "03", "door", "门洞")),
        "fem_params": _name_or_none(_sheet_like(workbook, "05", "fem", "有限元")),
        "materials": _name_or_none(_sheet_like(workbook, "04", "material", "材料")),
        "load_cases": _name_or_none(_sheet_like(workbook, "06", "load", "载荷")),
    }
    missing_items, warnings = validate_import_facts(tower_points, [], door_legacy, fem_params, loads_inventory)
    if not load_cases:
        warnings.append("No static load cases were recognized in the standard input workbook.")
    standard_package = {
        "schema_version": STANDARD_INPUT_SCHEMA_VERSION,
        "source_file": source_file,
        "input_format": profile.get("input_format", "semantic_standard_workbook"),
        "format_confidence": profile.get("format_confidence", 0.0),
        "profile_hash": profile.get("profile_hash"),
        "global_parameters": _parse_global_parameters(_sheet_like(workbook, "01", "global", "总体")),
        "tower_segments": segments,
        "door_opening": door,
        "materials": materials,
        "section_rows": section_rows,
        "fem_settings": _records_to_standard_fields(fem_records),
        "load_cases": load_cases,
        "modal_settings": modal,
        "output_requests": outputs,
        "source_trace": {"sheet_map": sheet_map, "source_file": source_file},
    }
    parsed_facts = build_parsed_facts(tower_points, [], door_legacy, fem_params, loads_inventory)
    confidence_report = build_confidence_report(sheet_map, tower_points, [], door_legacy, fem_params, missing_items, warnings)
    return {
        "source_file": source_file,
        "source_files": [source_file],
        "detected_format": "semantic_standard_workbook",
        "input_format": "semantic_standard_workbook",
        "format_confidence": profile.get("format_confidence", 0.0),
        "sheet_map": sheet_map,
        "raw_tables": build_raw_tables(workbook),
        "workbook_profile": profile,
        "standard_input_package": standard_package,
        "standard_input_candidate": standard_package,
        "parsed_facts": parsed_facts,
        "tower": tower_points,
        "flanges": [],
        "door": door_legacy,
        "fem_params": fem_params,
        "fem_param_records": fem_records,
        "fem_model_facts": build_fem_model_facts(fem_params, fem_records),
        "derived_facts": {"door_derivation": derive_door_candidates(tower_points, door_legacy)},
        "standard_references": [{"id": "manual_rule:standard_input_package", "type": "manual_rule", "title": "Standard input normalization", "note": "Workbook was normalized before facts/IR generation."}],
        "loads_inventory": loads_inventory,
        "missing_items": missing_items,
        "warnings": warnings,
        "llm_suggestions": build_llm_suggestions(sheet_map, fem_params, missing_items, confidence_report),
        "confidence_report": confidence_report,
        "parse_diagnostics": {"missing_items": missing_items, "warnings": warnings, "format_reasons": profile.get("format_reasons", [])},
    }


def validate_standard_input(candidate: dict[str, Any]) -> dict[str, Any]:
    package = candidate.get("standard_input_package") or candidate.get("standard_input_candidate") or candidate
    issues = []
    defaults_applied = []
    missing_items = []
    segments = package.get("tower_segments", [])
    if len(segments) < 1:
        issues.append(_issue("error", "tower_segments", "No tower segments were normalized."))
        missing_items.append({"item": "tower_segments", "severity": "blocked", "reason": "No valid tower shell segments can be formed."})
    previous_top = None
    for index, segment in enumerate(segments, start=1):
        z_bottom = _field_value(segment.get("z_bottom_mm"))
        z_top = _field_value(segment.get("z_top_mm"))
        diameter_bottom = _field_value(segment.get("diameter_bottom_mm"))
        diameter_top = _field_value(segment.get("diameter_top_mm"))
        thickness = _field_value(segment.get("thickness_mm"))
        if None in {z_bottom, z_top, diameter_bottom, diameter_top, thickness}:
            issues.append(_issue("error", f"tower_segments[{index}]", "Tower segment is missing z/diameter/thickness values."))
            missing_items.append({"item": f"tower_segments[{index}]", "severity": "blocked", "reason": "Tower height/diameter/thickness is required for shell geometry."})
            continue
        if float(z_top) <= float(z_bottom):
            issues.append(_issue("error", f"tower_segments[{index}].z_top_mm", "Tower segment top elevation must be above bottom elevation."))
        if previous_top is not None and abs(float(z_bottom) - previous_top) > 1e-6:
            issues.append(_issue("warning", f"tower_segments[{index}].z_bottom_mm", "Tower segments are not contiguous."))
        if float(thickness) <= 0 or float(diameter_bottom) <= 0 or float(diameter_top) <= 0:
            issues.append(_issue("error", f"tower_segments[{index}]", "Tower diameters and thickness must be positive."))
        previous_top = float(z_top)
    door = package.get("door_opening", {})
    if _field_value(door.get("enabled")) is not False:
        for key in ("width_mm", "height_mm", "z_bottom_mm"):
            if _field_value(door.get(key)) is None:
                issues.append(_issue("error", f"door_opening.{key}", f"Door field {key} is missing."))
                missing_items.append({"item": f"door_opening.{key}", "severity": "blocked", "reason": "Door opening is enabled but geometry is incomplete."})
    fem = candidate.get("fem_params", {})
    material_e_missing = _is_missing_value(fem.get("material.elastic_modulus_mpa"))
    material_nu_missing = _is_missing_value(fem.get("material.poisson_ratio"))
    if material_e_missing and material_nu_missing:
        issues.append(_issue("warning", "material", "Material elastic modulus and Poisson ratio are both missing; steel defaults will be used."))
        missing_items.append({"item": "material.elastic_modulus_mpa/material.poisson_ratio", "severity": "warning", "reason": "Default steel material policy can fill this field."})
    for key in REQUIRED_FEM_KEYS:
        if _is_missing_value(fem.get(key)):
            if key in CRITICAL_FEM_KEYS:
                issues.append(_issue("error", key, f"Required FEM key is missing after normalization: {key}."))
                missing_items.append({"item": key, "severity": "blocked", "reason": "Cannot generate a valid S4R shell section without this key."})
            elif key in DEFAULTABLE_FEM_DEFAULTS:
                default_value = DEFAULTABLE_FEM_DEFAULTS[key]
                issues.append(_issue("warning", key, f"FEM key is missing after normalization; default will be used: {key}."))
                missing_items.append({"item": key, "severity": "warning", "reason": "Default policy can fill this field."})
                defaults_applied.append({"field": key, "default_value": default_value, "reason": "warning_release_default"})
            else:
                issues.append(_issue("error", key, f"Required FEM key is missing after normalization: {key}."))
                missing_items.append({"item": key, "severity": "blocked", "reason": "No default policy is available."})
    for key in sorted(RECOMMENDED_DEFAULTABLE_FEM_KEYS):
        if key == "outputs.field" and package.get("output_requests"):
            continue
        if key == "modal.modes" and not _is_missing_value((package.get("modal_settings") or {}).get("modes")):
            continue
        if _is_missing_value(fem.get(key)):
            default_value = DEFAULTABLE_FEM_DEFAULTS[key]
            issues.append(_issue("warning", key, f"Recommended FEM key is missing after normalization; default will be used: {key}."))
            missing_items.append({"item": key, "severity": "warning", "reason": "Recommended default policy can fill this field."})
            defaults_applied.append({"field": key, "default_value": default_value, "reason": "recommended_default"})
    blocked = any(item["severity"] == "error" for item in issues)
    warnings = [item for item in issues if item["severity"] == "warning"]
    return {
        "status": "blocked" if blocked else ("warning" if issues else "pass"),
        "blocked": blocked,
        "issues": issues,
        "warnings": warnings,
        "defaults_applied": defaults_applied,
        "missing_items": missing_items,
        "llm_mapped_fields": candidate.get("llm_mapped_fields", []),
        "requires_user_confirmation": blocked,
        "policy": "Only source-traced fields can be promoted to engineering facts.",
        "parser_name": (candidate.get("parse_diagnostics") or {}).get("parser_name") or package.get("source_trace", {}).get("parser_name") or candidate.get("input_format"),
        "source_sheet_map": package.get("source_trace", {}).get("sheet_map") or candidate.get("sheet_map", {}),
    }


def promote_standard_input(candidate: dict[str, Any], validation: dict[str, Any] | None = None) -> dict[str, Any]:
    validation = validation or validate_standard_input(candidate)
    if validation.get("blocked"):
        return {**candidate, "promotion_status": "blocked", "validation_report": validation}
    return {**candidate, "promotion_status": "promoted", "validation_report": validation}


def remember_input_mapping(profile: dict[str, Any], confirmed_package: dict[str, Any], registry_path: Path = FORMAT_ALIAS_REGISTRY) -> dict[str, Any]:
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry = _read_json(registry_path, {"schema_version": "format_alias_registry_v1", "formats": []})
    entry = {
        "profile_hash": profile.get("profile_hash"),
        "input_format": profile.get("input_format"),
        "source_file": profile.get("source_file"),
        "sheet_names": [sheet.get("name") for sheet in profile.get("sheets", [])],
        "confirmed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "standard_schema": confirmed_package.get("schema_version", STANDARD_INPUT_SCHEMA_VERSION),
        "field_count": len(confirmed_package.get("fem_settings", {})),
    }
    registry["formats"] = [item for item in registry.get("formats", []) if item.get("profile_hash") != entry["profile_hash"]]
    registry["formats"].append(entry)
    registry_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "pass", "registry": str(registry_path), "entry": entry}


def llm_normalize_workbook(
    profile: dict[str, Any],
    schema: str,
    hints: list[str] | None = None,
    enable_llm: bool = False,
    force_llm: bool = False,
    validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    required = bool((validation or {}).get("blocked") or profile.get("input_format") in {"unknown_llm_assisted", "gpt_generated_workbook"})
    triggered = bool(force_llm or required)
    prompt_material = {
        "task": "Identify evidence and map arbitrary engineering workbook previews into CandidateMappingIR JSON.",
        "schema": schema,
        "candidate_schema": CANDIDATE_MAPPING_SCHEMA_VERSION,
        "canonical_payload": "StandardInputPackage",
        "hints": hints or STANDARD_HINTS,
        "profile_hash": profile.get("profile_hash"),
        "input_format": profile.get("input_format"),
        "sheets": profile.get("sheets", [])[:12],
        "validation_issues": (validation or {}).get("issues", []),
        "output_contract": {
            "root": "CandidateMappingIR",
            "field_trace_required": ["value", "unit", "status", "evidence", "normalization", "criticality"],
            "status_values": ["observed", "derived", "defaulted", "inferred", "unresolved"],
        },
        "policy": [
            "Return JSON only.",
            "Stage 1: identify evidence locations, units, raw values, and candidate meanings.",
            "Stage 2: map evidence to CandidateMappingIR fields.",
            "Every mapped field must include value, unit, status, evidence, normalization, and criticality.",
            "LLM output is a mapping advisor only; deterministic validation promotes fields.",
            "Never output Abaqus INP text.",
            "Never output final IR, mesh, or Abaqus input decks.",
            "Use unresolved/requires_confirmation for uncertain fields.",
        ],
    }
    prompt = json.dumps(prompt_material, ensure_ascii=False, sort_keys=True)
    return {
        "enabled": triggered,
        "triggered": triggered,
        "status": "not_called",
        "reason": "llm_disabled_or_api_not_connected_in_importer" if triggered and enable_llm else ("not_required" if not triggered else "rule_only_policy"),
        "provider": "none",
        "model": "none",
        "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16] if triggered else "",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "schema": schema,
        "keywords": hints or STANDARD_HINTS,
        "candidate_policy": "LLM may propose StandardInputPackage candidates only; deterministic validation promotes fields.",
        "mapping_trace_required": ["source_sheet", "source_cell_or_range", "confidence", "mapping_reason", "is_inferred"],
        "required": required,
        "force_llm": force_llm,
    }


def _profile_sheet(sheet: SheetData, max_preview_rows: int, max_preview_cols: int) -> dict[str, Any]:
    preview = []
    non_empty_rows = 0
    header_candidates = []
    numeric_columns: dict[int, int] = {}
    numeric_values: dict[int, list[float]] = {}
    unit_candidates = set()
    semantic_hints: dict[str, int] = {}
    for row_index in range(sheet.nrows):
        values = [sheet.cell(row_index, col) for col in range(min(sheet.ncols, max_preview_cols))]
        compact = [value for value in values if str(value).strip()]
        if compact:
            non_empty_rows += 1
            if len(preview) < max_preview_rows:
                preview.append({"row": row_index + 1, "values": values})
            text_count = sum(1 for value in values if isinstance(value, str) and str(value).strip())
            if text_count >= 2 and len(header_candidates) < 5:
                header_candidates.append({"row": row_index + 1, "values": values})
            for col, value in enumerate(values):
                numeric_value = _number(value)
                if numeric_value is not None:
                    numeric_columns[col] = numeric_columns.get(col, 0) + 1
                    numeric_values.setdefault(col, []).append(float(numeric_value))
                text = str(value)
                normalized_text = _norm(text)
                for hint in STANDARD_HINTS + ["standardinput", "towersegments", "dooropening", "femsettings", "loadcases", "jsonschema"]:
                    normalized_hint = _norm(hint)
                    if normalized_hint and normalized_hint in normalized_text:
                        semantic_hints[hint] = semantic_hints.get(hint, 0) + 1
                if any(unit in text for unit in ("mm", "MPa", "kN", "N·mm", "Hz", "tonne", "m/s", "Pa")):
                    unit_candidates.add(text.strip())
    numeric_stats = {}
    for col, values in sorted(numeric_values.items()):
        if values:
            numeric_stats[str(col + 1)] = {"count": len(values), "min": min(values), "max": max(values)}
    return {
        "name": sheet.name,
        "shape": {"rows": sheet.nrows, "cols": sheet.ncols},
        "non_empty_rows": non_empty_rows,
        "preview": preview,
        "header_candidates": header_candidates,
        "numeric_column_counts": {str(key + 1): value for key, value in sorted(numeric_columns.items())},
        "numeric_column_stats": numeric_stats,
        "unit_candidates": sorted(unit_candidates)[:30],
        "semantic_hints": dict(sorted(semantic_hints.items(), key=lambda item: (-item[1], item[0]))[:20]),
    }


def _parse_global_parameters(sheet: SheetData | None) -> dict[str, Any]:
    if not sheet:
        return {}
    values = {}
    for row in range(sheet.nrows):
        key = str(sheet.cell(row, 2)).strip()
        if not key or key.lower() in {"符号/键名", "key"}:
            continue
        values[key] = _field(sheet.cell(row, 3), str(sheet.cell(row, 4)).strip(), sheet.name, f"R{row + 1}C4", sheet.cell(row, 3), "confirmed")
    return values


def _parse_segments(sheet: SheetData | None) -> list[dict[str, Any]]:
    if not sheet:
        return []
    header_row = _find_row(sheet, ["底标高", "顶标高", "厚度"])
    if header_row is None:
        return []
    headers = [_norm(sheet.cell(header_row, col)) for col in range(sheet.ncols)]
    cols = {
        "segment_id": _find_col(headers, ["塔节id", "segmentid", "塔节"]),
        "z_bottom_mm": _find_col(headers, ["底标高mm", "底标高", "zbottom"]),
        "z_top_mm": _find_col(headers, ["顶标高mm", "顶标高", "ztop"]),
        "diameter_bottom_mm": _find_col(headers, ["底外径mm", "底外径", "bottomdiameter"]),
        "diameter_top_mm": _find_col(headers, ["顶外径mm", "顶外径", "topdiameter"]),
        "thickness_mm": _find_col(headers, ["厚度mm", "厚度", "thickness"]),
    }
    rows = []
    for row in range(header_row + 1, sheet.nrows):
        sid = str(sheet.cell(row, cols["segment_id"] or 0)).strip()
        z_bottom = _number(sheet.cell(row, cols["z_bottom_mm"])) if cols["z_bottom_mm"] is not None else None
        z_top = _number(sheet.cell(row, cols["z_top_mm"])) if cols["z_top_mm"] is not None else None
        d_bottom = _number(sheet.cell(row, cols["diameter_bottom_mm"])) if cols["diameter_bottom_mm"] is not None else None
        d_top = _number(sheet.cell(row, cols["diameter_top_mm"])) if cols["diameter_top_mm"] is not None else None
        thickness = _number(sheet.cell(row, cols["thickness_mm"])) if cols["thickness_mm"] is not None else None
        if not sid or z_bottom is None or z_top is None or d_bottom is None or d_top is None or thickness is None:
            continue
        rows.append(
            {
                "segment_id": _field(sid, "", sheet.name, f"R{row + 1}", sid),
                "z_bottom_mm": _field(z_bottom, "mm", sheet.name, f"R{row + 1}", sheet.cell(row, cols["z_bottom_mm"])),
                "z_top_mm": _field(z_top, "mm", sheet.name, f"R{row + 1}", sheet.cell(row, cols["z_top_mm"])),
                "diameter_bottom_mm": _field(d_bottom, "mm", sheet.name, f"R{row + 1}", sheet.cell(row, cols["diameter_bottom_mm"])),
                "diameter_top_mm": _field(d_top, "mm", sheet.name, f"R{row + 1}", sheet.cell(row, cols["diameter_top_mm"])),
                "thickness_mm": _field(thickness, "mm", sheet.name, f"R{row + 1}", sheet.cell(row, cols["thickness_mm"])),
                "source": f"{sheet.name}!R{row + 1}",
            }
        )
    return rows


def _parse_door(sheet: SheetData | None) -> dict[str, Any]:
    if not sheet:
        return {}
    mapping = {
        "door_enabled": "enabled",
        "door_shape": "shape",
        "door_azimuth": "azimuth_deg",
        "door_z_bottom": "z_bottom_mm",
        "door_width": "width_mm",
        "door_height": "height_mm",
        "door_corner_radius": "corner_radius_mm",
        "door_mesh_size": "mesh_size_mm",
    }
    result = {}
    for row in range(sheet.nrows):
        key = str(sheet.cell(row, 2)).strip()
        if key not in mapping:
            continue
        raw = sheet.cell(row, 3)
        unit = str(sheet.cell(row, 4)).strip()
        value: Any = _number(raw)
        if value is None:
            text = str(raw).strip()
            value = text
            if text.upper() in {"TRUE", "FALSE"}:
                value = text.upper() == "TRUE"
        result[mapping[key]] = _field(value, unit if unit != "-" else "", sheet.name, f"R{row + 1}C4", raw)
    return result


def _parse_materials(sheet: SheetData | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float | None]:
    if not sheet:
        return [], [], None
    materials = []
    header = _find_row(sheet, ["材料名", "MPa"])
    if header is not None:
        for row in range(header + 1, sheet.nrows):
            name = str(sheet.cell(row, 0)).strip()
            if not name or name.startswith("壳截面"):
                break
            if _number(sheet.cell(row, 2)) is None:
                continue
            materials.append(
                {
                    "name": name,
                    "elastic_modulus_mpa": _number(sheet.cell(row, 2)),
                    "poisson_ratio": _number(sheet.cell(row, 3)),
                    "density_tonne_mm3": _number(sheet.cell(row, 4)),
                    "yield_strength_mpa": _number(sheet.cell(row, 5)),
                    "ultimate_strength_mpa": _number(sheet.cell(row, 6)),
                    "source": f"{sheet.name}!R{row + 1}",
                }
            )
    section_rows = []
    top_mass = None
    section_header = _find_row(sheet, ["截面名", "厚度"])
    if section_header is not None:
        for row in range(section_header + 1, sheet.nrows):
            name = str(sheet.cell(row, 0)).strip()
            if name.startswith("SEC_"):
                section_rows.append({"section": name, "segment": sheet.cell(row, 1), "thickness_mm": _number(sheet.cell(row, 2)), "material": sheet.cell(row, 3), "source": f"{sheet.name}!R{row + 1}"})
            key = str(sheet.cell(row, 7)).strip()
            if key == "RP_TOP_MASS":
                top_mass = _number(sheet.cell(row, 8))
    return materials, section_rows, top_mass


def _parse_fem_settings(sheet: SheetData | None, source_file: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    params: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    if not sheet:
        return params, records
    header = _find_row(sheet, ["Abaqus", "推荐值"])
    if header is None:
        return params, records
    key_map = {
        "element_type": ("element", "element_type"),
        "mesh_global": ("mesh", "global_size_mm"),
        "mesh_door": ("mesh", "door_size_mm"),
    }
    for row in range(header + 1, sheet.nrows):
        raw_key = str(sheet.cell(row, 2)).strip()
        if raw_key not in key_map:
            continue
        group, key = key_map[raw_key]
        _set_param(params, records, group, key, sheet.cell(row, 3), str(sheet.cell(row, 4)).strip(), source_file, f"{sheet.name}!R{row + 1}", str(sheet.cell(row, 6)).strip())
    return params, records


def _parse_load_cases(sheet: SheetData | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not sheet:
        return [], []
    cases = []
    header = _find_row(sheet, ["工况ID", "Abaqus Fx"])
    if header is not None:
        headers = [_norm(sheet.cell(header, col)) for col in range(sheet.ncols)]
        cols = {
            "case_id": _find_col(headers, ["工况id", "loadcase"]),
            "Fx_N": _find_col(headers, ["abaqusfxn"]),
            "Fy_N": _find_col(headers, ["abaqusfyn"]),
            "Fz_N": _find_col(headers, ["abaqusfzn"]),
            "Mx_Nmm": _find_col(headers, ["abaqusmxnmm"]),
            "My_Nmm": _find_col(headers, ["abaqusmynmm"]),
            "Mz_Nmm": _find_col(headers, ["abaqusmznmm"]),
        }
        for row in range(header + 1, sheet.nrows):
            case_id = str(sheet.cell(row, cols["case_id"] or 0)).strip()
            if not case_id:
                continue
            if not re.search(r"(ULS|DLC|LC|LOAD)", case_id, re.IGNORECASE):
                continue
            item = {"case_id": case_id, "sheet": sheet.name, "source": f"{sheet.name}!R{row + 1}"}
            for key, col in cols.items():
                if key != "case_id" and col is not None:
                    item[key] = _number(sheet.cell(row, col))
            if any(item.get(key) is not None for key in ("Fx_N", "Fy_N", "Fz_N")):
                cases.append(item)
    wind_rows = []
    wind_header = _find_row(sheet, ["面压力", "节段风力"])
    if wind_header is not None:
        headers = [_norm(sheet.cell(wind_header, col)) for col in range(sheet.ncols)]
        pressure_col = _find_col(headers, ["面压力nmm²", "面压力nmm2", "pressure"])
        segment_col = _find_col(headers, ["塔节", "segment"])
        for row in range(wind_header + 1, sheet.nrows):
            pressure = _number(sheet.cell(row, pressure_col)) if pressure_col is not None else None
            if pressure is None:
                continue
            wind_rows.append({"segment": sheet.cell(row, segment_col) if segment_col is not None else "", "pressure_mpa": pressure, "source": f"{sheet.name}!R{row + 1}"})
    return cases, wind_rows


def _parse_modal(sheet: SheetData | None) -> dict[str, Any]:
    if not sheet:
        return {}
    result = {}
    for row in range(sheet.nrows):
        if str(sheet.cell(row, 6)).strip() == "特征值方法":
            result["eigensolver"] = _field(sheet.cell(row, 7), "", sheet.name, f"R{row + 1}C8", sheet.cell(row, 7))
        text = " ".join(str(sheet.cell(row, col)) for col in range(sheet.ncols))
        match = re.search(r"(\d+)\s*阶", text)
        if match:
            result["modes"] = int(match.group(1))
            result["source"] = f"{sheet.name}!R{row + 1}"
    return result


def _parse_outputs(sheet: SheetData | None) -> list[dict[str, Any]]:
    if not sheet:
        return []
    header = _find_row(sheet, ["分析", "Abaqus输出"])
    rows = []
    if header is not None:
        for row in range(header + 1, sheet.nrows):
            analysis = str(sheet.cell(row, 0)).strip()
            output = str(sheet.cell(row, 2)).strip()
            if analysis and output:
                rows.append({"analysis": analysis, "target": sheet.cell(row, 1), "output": output, "components": sheet.cell(row, 3), "source": f"{sheet.name}!R{row + 1}"})
    return rows


def _parse_mechanical_tower_segments(sheet: SheetData | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    if not sheet:
        return [], [], ["Mechanical interface tower sheet was not found."]
    header_row = _find_row_any(sheet, [["序号"], ["高度", "height"], ["中径", "Dt", "diameter"], ["壁厚", "厚度", "thickness"]])
    if header_row is None:
        return [], [], [f"{sheet.name}: tower section header row was not recognized."]
    headers = [_norm(sheet.cell(header_row, col)) for col in range(sheet.ncols)]
    index_col = _find_col(headers, ["序号", "index", "section"])
    z_col = _find_col(headers, ["高度m", "高度", "height", "z"])
    diameter_col = _find_col(headers, ["中径dtmm", "中径", "dt", "diameter"])
    length_col = _find_col(headers, ["对应截面长度lmm", "截面长度", "长度", "length"])
    thickness_col = _find_col(headers, ["壁厚tmm", "壁厚", "厚度", "thickness"])
    if z_col is None or diameter_col is None or thickness_col is None:
        return [], [], [f"{sheet.name}: tower section columns are incomplete."]
    points: list[dict[str, Any]] = []
    for row in range(header_row + 1, sheet.nrows):
        row_text = _norm(" ".join(str(sheet.cell(row, col)) for col in range(sheet.ncols)))
        if any(
            marker in row_text
            for marker in (
                _norm("法兰参数"),
                _norm("极限载荷"),
                _norm("flange parameters"),
                _norm("ultimate loads"),
            )
        ):
            break
        z_value = _number(sheet.cell(row, z_col))
        diameter = _number(sheet.cell(row, diameter_col))
        thickness = _number(sheet.cell(row, thickness_col))
        if z_value is None and diameter is None and thickness is None:
            continue
        if z_value is None or diameter is None or thickness is None:
            continue
        index_value = _number(sheet.cell(row, index_col)) if index_col is not None else float(len(points))
        points.append(
            {
                "index": int(index_value if index_value is not None else len(points)),
                "z_mm": z_value * 1000.0 if abs(z_value) < 1000.0 else z_value,
                "diameter_mm": diameter,
                "length_mm": _number(sheet.cell(row, length_col)) if length_col is not None else None,
                "thickness_mm": thickness,
                "source": f"{sheet.name}!R{row + 1}",
                "row": row,
            }
        )
    points = sorted(points, key=lambda item: float(item["z_mm"]))
    segments: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, (bottom, top) in enumerate(zip(points, points[1:]), start=1):
        z_bottom = float(bottom["z_mm"])
        z_top = float(top["z_mm"])
        if z_top <= z_bottom:
            warnings.append(f"{sheet.name}: skipped non-positive tower segment at rows {bottom['row'] + 1}-{top['row'] + 1}.")
            continue
        raw_id = f"SEG_{index:03d}"
        source_range = f"R{bottom['row'] + 1}:R{top['row'] + 1}"
        bottom_z_cell = f"R{bottom['row'] + 1}C{z_col + 1}"
        top_z_cell = f"R{top['row'] + 1}C{z_col + 1}"
        bottom_diameter_cell = f"R{bottom['row'] + 1}C{diameter_col + 1}"
        top_diameter_cell = f"R{top['row'] + 1}C{diameter_col + 1}"
        thickness_cell = f"R{bottom['row'] + 1}C{thickness_col + 1}"
        thickness = float(bottom["thickness_mm"])
        segments.append(
            {
                "segment_id": _field(raw_id, "", sheet.name, source_range, raw_id),
                "z_bottom_mm": _field(z_bottom, "mm", sheet.name, bottom_z_cell, bottom["z_mm"]),
                "z_top_mm": _field(z_top, "mm", sheet.name, top_z_cell, top["z_mm"]),
                "diameter_bottom_mm": _field(float(bottom["diameter_mm"]), "mm", sheet.name, bottom_diameter_cell, bottom["diameter_mm"]),
                "diameter_top_mm": _field(float(top["diameter_mm"]), "mm", sheet.name, top_diameter_cell, top["diameter_mm"]),
                "thickness_mm": _field(thickness, "mm", sheet.name, thickness_cell, bottom["thickness_mm"]),
                "source": source_range,
            }
        )
    tower_points = [
        {
            "index": item["index"],
            "z_mm": float(item["z_mm"]),
            "diameter_mm": float(item["diameter_mm"]),
            "length_mm": item.get("length_mm"),
            "thickness_mm": float(item["thickness_mm"]),
            "source": item["source"],
        }
        for item in points
    ]
    return segments, tower_points, warnings


def _parse_mechanical_flanges(sheet: SheetData | None) -> list[dict[str, Any]]:
    if not sheet:
        return []
    header_row = _find_row_any(sheet, [["序号", "index"], ["中径", "Da"], ["螺孔数量", "螺栓数量", "N"]])
    if header_row is None:
        return []
    headers = [_norm(sheet.cell(header_row, col)) for col in range(sheet.ncols)]
    index_col = _find_col(headers, ["序号", "index"])
    diameter_col = _find_col(headers, ["中径da", "中径", "da", "diameter", "mean_diameter"])
    bolt_circle_col = _find_col(headers, ["分布圆直径d1", "分布圆直径", "螺栓圆", "bolt circle", "boltcircle"])
    inner_col = _find_col(headers, ["内径df", "内径", "inner diameter"])
    clamp_col = _find_col(headers, ["夹持厚度", "夹持区高度", "厚度h", "厚度H", "clamp"])
    neck_col = _find_col(headers, ["主体厚度h", "颈部壁厚t", "颈部厚度", "neck"])
    semantic_total_col = _find_col(headers, ["总厚度", "总高度", "法兰总高度", "法兰总厚度", "total thickness", "total height", "total"])
    total_col = semantic_total_col
    bolt_count_col = _find_col(headers, ["螺孔数量n", "螺孔数量", "螺栓数量", "数量n", "bolt count"])
    bolt_diameter_col = _find_col(headers, ["螺孔直径d", "螺孔直径", "螺栓直径", "bolt diameter"])
    template_recovered_total = False
    if total_col is None and _looks_like_l_flange_template(headers, sheet.ncols):
        total_col = 6
        template_recovered_total = True
    if index_col is None and _looks_like_l_flange_template(headers, sheet.ncols):
        index_col = 0
    if diameter_col is None and _looks_like_l_flange_template(headers, sheet.ncols):
        diameter_col = 1
    if bolt_circle_col is None and _looks_like_l_flange_template(headers, sheet.ncols):
        bolt_circle_col = 2
    if inner_col is None and _looks_like_l_flange_template(headers, sheet.ncols):
        inner_col = 3
    if clamp_col is None and _looks_like_l_flange_template(headers, sheet.ncols):
        clamp_col = 4
    if neck_col is None and _looks_like_l_flange_template(headers, sheet.ncols):
        neck_col = 5
    if bolt_count_col is None and _looks_like_l_flange_template(headers, sheet.ncols):
        bolt_count_col = 7
    if bolt_diameter_col is None and _looks_like_l_flange_template(headers, sheet.ncols):
        bolt_diameter_col = 8
    rows: list[dict[str, Any]] = []
    for row in range(header_row + 1, sheet.nrows):
        index = _number(sheet.cell(row, index_col)) if index_col is not None else len(rows) + 1
        diameter = _number(sheet.cell(row, diameter_col)) if diameter_col is not None else None
        if index is None or diameter is None:
            continue
        recovered_fields = []
        if template_recovered_total:
            recovered_fields.append(
                {
                    "field": "flange.total_thickness_mm",
                    "source_sheet": sheet.name,
                    "source_cell_or_range": f"R{row + 1}C{total_col + 1}",
                    "method": "l_flange_template_position",
                    "reason": "Recovered from the seventh column of a recognized 13-column L-flange mechanical interface template.",
                }
            )
        rows.append(
            {
                "index": int(index),
                "diameter_mm": diameter,
                "bolt_circle_mm": _number(sheet.cell(row, bolt_circle_col)) if bolt_circle_col is not None else None,
                "inner_diameter_mm": _number(sheet.cell(row, inner_col)) if inner_col is not None else None,
                "clamp_thickness_mm": _number(sheet.cell(row, clamp_col)) if clamp_col is not None else None,
                "neck_thickness_mm": _number(sheet.cell(row, neck_col)) if neck_col is not None else None,
                "total_thickness_mm": _number(sheet.cell(row, total_col)) if total_col is not None else None,
                "bolt_count": int(_number(sheet.cell(row, bolt_count_col)) or 0) if bolt_count_col is not None else 0,
                "bolt_diameter_mm": _number(sheet.cell(row, bolt_diameter_col)) if bolt_diameter_col is not None else None,
                "source": f"{sheet.name}!R{row + 1}",
                "source_trace": {
                    "sheet": sheet.name,
                    "row": row + 1,
                    "total_thickness_source": f"R{row + 1}C{total_col + 1}" if total_col is not None else None,
                    "total_thickness_method": "template_position" if template_recovered_total else "header_alias",
                },
                "recovery_reason": "l_flange_template_position" if template_recovered_total else None,
                "recovered_fields": recovered_fields,
            }
        )
    return rows


def _looks_like_l_flange_template(headers: list[str], ncols: int) -> bool:
    if ncols < 9:
        return False
    joined = "".join(headers)
    required = ["序号", "中径", "内径"]
    if all(_norm(item) in joined for item in required):
        return True
    return all(token in joined for token in ("da", "df")) and ("螺孔" in joined or "bolt" in joined)


def _parse_mechanical_door(sheet: SheetData | None) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    if not sheet:
        return {}, {}, ["Mechanical interface door sheet was not found."]
    symbols: dict[str, tuple[Any, str]] = {}
    for row in range(sheet.nrows):
        row_values = [sheet.cell(row, col) for col in range(sheet.ncols)]
        symbol = _mechanical_door_symbol(row_values)
        if not symbol:
            continue
        numeric_values = [
            (col, _strict_cell_number(value), value)
            for col, value in enumerate(row_values)
        ]
        numeric_values = [item for item in numeric_values if item[1] is not None]
        if not numeric_values:
            continue
        col, value, raw = numeric_values[-1]
        symbols[symbol] = (value, f"R{row + 1}C{col + 1}", raw)
    legacy = {symbol: value for symbol, (value, _cell, _raw) in symbols.items()}
    warnings = []
    width = symbols.get("X2")
    height = symbols.get("Y2")
    z_bottom = symbols.get("H")
    if width is None:
        warnings.append(f"{sheet.name}: inner ellipse short axis X2 was not found.")
    if height is None:
        warnings.append(f"{sheet.name}: inner ellipse long axis Y2 was not found.")
    if z_bottom is None:
        warnings.append(f"{sheet.name}: door-bottom candidate H was not found.")
    door = {
        "enabled": _field(True, "", sheet.name, "auto", True, "assumed_fact", 0.85),
        "shape": _field("ellipse", "", sheet.name, "auto", "ellipse", "assumed_fact", 0.85),
        "azimuth_deg": _field(0.0, "deg", sheet.name, "auto", 0.0, "assumed_fact", 0.75),
    }
    if width is not None:
        door["width_mm"] = _field(width[0], "mm", sheet.name, width[1], width[2])
    if height is not None:
        door["height_mm"] = _field(height[0], "mm", sheet.name, height[1], height[2])
    if symbols.get("X1") is not None:
        value, cell, raw = symbols["X1"]
        door["outer_width_mm"] = _field(value, "mm", sheet.name, cell, raw)
    if symbols.get("Y1") is not None:
        value, cell, raw = symbols["Y1"]
        door["outer_height_mm"] = _field(value, "mm", sheet.name, cell, raw)
    if z_bottom is not None:
        door["z_bottom_mm"] = _field(z_bottom[0], "mm", sheet.name, z_bottom[1], z_bottom[2], "assumed_fact", 0.85)
    if symbols.get("D") is not None:
        value, cell, raw = symbols["D"]
        door["reinforcement_width_mm"] = _field(value, "mm", sheet.name, cell, raw)
    if symbols.get("W") is not None:
        value, cell, raw = symbols["W"]
        door["frame_thickness_mm"] = _field(value, "mm", sheet.name, cell, raw)
    return door, legacy, warnings


def _parse_key_value_fem_settings(sheet: SheetData | None, source_file: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    params: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    if not sheet:
        return params, records
    header_row = _find_row_any(sheet, [["参数组", "group"], ["key"], ["value"]])
    if header_row is None:
        return params, records
    headers = [_norm(sheet.cell(header_row, col)) for col in range(sheet.ncols)]
    group_col = _find_col(headers, ["参数组", "group"])
    key_col = _find_col(headers, ["key"])
    value_col = _find_col(headers, ["value", "推荐值", "数值"])
    unit_col = _find_col(headers, ["unit", "单位"])
    source_col = _find_col(headers, ["source", "来源"])
    desc_col = _find_col(headers, ["description", "说明"])
    if group_col is None or key_col is None or value_col is None:
        return params, records
    for row in range(header_row + 1, sheet.nrows):
        group = str(sheet.cell(row, group_col)).strip()
        key = str(sheet.cell(row, key_col)).strip()
        if not group or not key:
            continue
        raw_value = sheet.cell(row, value_col)
        unit = str(sheet.cell(row, unit_col)).strip() if unit_col is not None else ""
        source = f"{sheet.name}!R{row + 1}C{value_col + 1}"
        source_note = str(sheet.cell(row, source_col)).strip() if source_col is not None else ""
        description = str(sheet.cell(row, desc_col)).strip() if desc_col is not None else source_note
        _set_param(params, records, group, key, raw_value, unit, source_file, source, description)
    return params, records


def _heuristic_standard_input_candidate(path: str | Path, workbook: WorkbookData, profile: dict[str, Any]) -> dict[str, Any]:
    source_file = Path(path).name
    sheet_map = {"tower": None, "flange": None, "door": None, "fem_params": None}
    missing_items = [{"item": "standard_input", "reason": "Workbook format is unknown; LLM normalization is required before deterministic promotion."}]
    warnings = ["Unknown workbook format. A compact workbook profile was created for LLM-assisted normalization."]
    confidence_report = {
        "strategy": "llm_format_normalizer_required",
        "score": 0.2,
        "status": "needs_llm_assist",
        "checks": {"known_format": False},
        "counts": {"missing_items": len(missing_items), "warnings": len(warnings)},
        "llm_assist_required": True,
        "llm_trigger_reasons": ["unknown_workbook_format"],
    }
    return {
        "source_file": source_file,
        "source_files": [source_file],
        "detected_format": "unknown_llm_assisted",
        "input_format": "unknown_llm_assisted",
        "format_confidence": profile.get("format_confidence", 0.35),
        "sheet_map": sheet_map,
        "raw_tables": build_raw_tables(workbook),
        "workbook_profile": profile,
        "standard_input_candidate": {"schema_version": STANDARD_INPUT_SCHEMA_VERSION, "source_file": source_file, "input_format": "unknown_llm_assisted", "tower_segments": [], "source_trace": {"sheet_map": sheet_map}},
        "standard_input_package": {"schema_version": STANDARD_INPUT_SCHEMA_VERSION, "source_file": source_file, "input_format": "unknown_llm_assisted", "tower_segments": [], "source_trace": {"sheet_map": sheet_map}},
        "parsed_facts": build_parsed_facts([], [], {}, {}, {}),
        "tower": [],
        "flanges": [],
        "door": {},
        "fem_params": {},
        "fem_param_records": [],
        "fem_model_facts": build_fem_model_facts({}, []),
        "derived_facts": {"door_derivation": derive_door_candidates([], {})},
        "standard_references": [{"id": "manual_rule:llm_format_normalizer", "type": "manual_rule", "title": "LLM format normalizer required", "note": "Unknown workbooks must be normalized and validated before FEM build."}],
        "loads_inventory": {},
        "missing_items": missing_items,
        "warnings": warnings,
        "llm_suggestions": build_llm_suggestions(sheet_map, {}, missing_items, confidence_report),
        "confidence_report": confidence_report,
        "parse_diagnostics": {"missing_items": missing_items, "warnings": warnings, "format_reasons": profile.get("format_reasons", [])},
    }


def _segments_to_tower_points(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not segments:
        return []
    points = []
    first = segments[0]
    points.append(
        {
            "index": 0,
            "z_mm": _field_value(first["z_bottom_mm"]),
            "diameter_mm": _field_value(first["diameter_bottom_mm"]),
            "thickness_mm": _field_value(first["thickness_mm"]),
            "source": first["source"],
        }
    )
    for index, segment in enumerate(segments, start=1):
        points.append(
            {
                "index": index,
                "z_mm": _field_value(segment["z_top_mm"]),
                "diameter_mm": _field_value(segment["diameter_top_mm"]),
                "thickness_mm": _field_value(segment["thickness_mm"]),
                "source": segment["source"],
            }
        )
    return points


def _door_to_legacy(door: dict[str, Any]) -> dict[str, Any]:
    if not door:
        return {}
    return {
        "X2": _field_value(door.get("width_mm")),
        "Y2": _field_value(door.get("height_mm")),
        "H": _field_value(door.get("z_bottom_mm")),
        "R": _field_value(door.get("corner_radius_mm")),
        "theta_center_deg": _field_value(door.get("azimuth_deg")) or 0.0,
        "shape": str(_field_value(door.get("shape")) or "ROUNDED_RECTANGLE").lower(),
        "enabled": bool(_field_value(door.get("enabled")) is not False),
        "X1": _field_value(door.get("outer_width_mm")),
        "Y1": _field_value(door.get("outer_height_mm")),
        "D": _field_value(door.get("reinforcement_width_mm")),
        "W": _field_value(door.get("frame_thickness_mm")),
    }


def _records_to_standard_fields(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        record["path"]: _field(
            record.get("value"),
            record.get("unit", ""),
            record.get("sheet", ""),
            record.get("cell_range", ""),
            record.get("raw_value"),
            "defaulted" if record.get("source_kind") == "capability_default" else (
                "confirmed_fact" if record.get("confidence", 0) else "unresolved_fact"
            ),
            record.get("confidence", 0.0),
        )
        for record in records
    }


def _set_default_param(params: dict[str, Any], records: list[dict[str, Any]], source_file: str, group: str, key: str, value: Any, unit: str, source: str, description: str) -> None:
    path = f"{group}.{key}"
    if _is_blank_value(params.get(path)):
        _set_param(params, records, group, key, value, unit, source_file, source, description, confidence=0.85)


def _set_param(params: dict[str, Any], records: list[dict[str, Any]], group: str, key: str, value: Any, unit: str, source_file: str, source: str, description: str, confidence: float = 1.0) -> None:
    path = f"{group}.{key}"
    value_type = value_type_for_key(path)
    parsed_value = (
        parse_wind_pressure_profile(value)
        if path == "loads.wind_pressure_profile"
        else (value if isinstance(value, list) else parse_value_by_type(value, value_type))
    )
    if parsed_value is None:
        parsed_value = value
    params[path] = parsed_value
    sheet, cell_range = _split_source(source)
    is_default = str(source).endswith("_default")
    records.append(
        {
            "path": path,
            "group": group,
            "key": key,
            "value": parsed_value,
            "raw_value": value,
            "value_type": value_type,
            "unit": unit,
            "source": source,
            "sheet": sheet,
            "cell_range": cell_range,
            "description": description,
            "confidence": confidence if not _is_blank_value(parsed_value) else 0.0,
            "mapping_confidence": confidence if not _is_blank_value(parsed_value) else 0.0,
            "source_kind": "capability_default" if is_default else "file_observed",
            "requires_confirmation": is_default,
            "confirmation_status": "pending" if is_default else "not_required",
            "locator_status": "missing" if is_default else ("exact" if cell_range and cell_range != "auto" else "region"),
        }
    )


def _field(value: Any, unit: str, sheet: str, cell_range: str, raw_value: Any, status: str = "confirmed_fact", confidence: float = 1.0) -> dict[str, Any]:
    return {
        "value": value,
        "unit": unit,
        "source_sheet": sheet,
        "source_cell_or_range": cell_range,
        "raw_value": raw_value,
        "confidence": confidence,
        "status": status if not _is_blank_value(value) else "unresolved_fact",
    }


def _field_value(field: Any) -> Any:
    if isinstance(field, dict) and "value" in field:
        return field.get("value")
    return field


def _is_blank_value(value: Any) -> bool:
    return value is None or value == ""


def _contains_assumed(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("status") in {"assumed_fact", "requires_confirmation", "unresolved_fact"}:
            return True
        return any(_contains_assumed(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_assumed(item) for item in value)
    return False


def _issue(severity: str, path: str, message: str) -> dict[str, Any]:
    return {"severity": severity, "path": path, "message": message}


def _sheet_like(workbook: WorkbookData, *tokens: str) -> SheetData | None:
    normalized_tokens = [_norm(token) for token in tokens if token]
    for sheet in workbook.sheets.values():
        name = _norm(sheet.name)
        if any(token and token in name for token in normalized_tokens):
            return sheet
    return None


def _name_or_none(sheet: SheetData | None) -> str | None:
    return sheet.name if sheet else None


def _find_row(sheet: SheetData, tokens: list[str]) -> int | None:
    normalized = [_norm(token) for token in tokens]
    for row in range(sheet.nrows):
        text = _norm(" ".join(str(sheet.cell(row, col)) for col in range(sheet.ncols)))
        if all(token in text for token in normalized):
            return row
    return None


def _find_row_any(sheet: SheetData, groups: list[list[str]]) -> int | None:
    normalized_groups = [[_norm(token) for token in group if _norm(token)] for group in groups]
    for row in range(sheet.nrows):
        text = _norm(" ".join(str(sheet.cell(row, col)) for col in range(sheet.ncols)))
        if all(any(token in text for token in group) for group in normalized_groups if group):
            return row
    return None


def _find_col(headers: list[str], aliases: list[str]) -> int | None:
    normalized = [_norm(alias) for alias in aliases]
    for index, header in enumerate(headers):
        if any(alias and alias in header for alias in normalized):
            return index
    return None


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    match = re.search(r"[-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?", text, re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _strict_cell_number(value: Any) -> float | None:
    """Return a number only when the complete cell represents a numeric value."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?", text, re.IGNORECASE):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _is_missing_value(value: Any) -> bool:
    """Treat only None and an empty string as missing; lists are valid outputs."""
    return value is None or value == ""


def _first_number(values: list[Any]) -> float | None:
    for value in values:
        number = _number(value)
        if number is not None:
            return number
    return None


def _direction_from_fx_fy(fx: float, fy: float) -> str:
    if abs(fy) > abs(fx):
        return "+Y" if fy >= 0 else "-Y"
    return "+X" if fx >= 0 else "-X"


def _split_source(source: str) -> tuple[str, str]:
    if "!" in source:
        return source.split("!", 1)
    if ":" in source:
        return source.split(":", 1)
    return source, "auto"


def _record_source(records: list[dict[str, Any]], path: str) -> str | None:
    for record in records:
        if record.get("path") == path:
            return str(record.get("source") or "")
    return None


def _mechanical_door_symbol(row_values: list[Any]) -> str | None:
    text_values = [str(value).strip() for value in row_values if str(value).strip()]
    joined = _norm(" ".join(text_values))
    raw_symbols = set(text_values)
    for symbol in ("Y1", "X1", "Y2", "X2", "D", "W", "H", "P"):
        if symbol in raw_symbols:
            return symbol
    symbol_map = {
        "Y1": ["外椭圆长轴", "y1"],
        "X1": ["外椭圆短轴", "x1"],
        "Y2": ["内椭圆长轴", "净通道高度", "y2"],
        "X2": ["内椭圆短轴", "净通道宽度", "x2"],
        "D": ["中间距"],
        "W": ["厚度w", "门框板材厚度"],
        "H": ["与底法兰端面距离", "门洞底"],
        "P": ["与塔筒中心线距离"],
    }
    for symbol, aliases in symbol_map.items():
        if any(_norm(alias) and _norm(alias) in joined for alias in aliases):
            return symbol
    return None


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _norm(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    text = text.replace("（", "(").replace("）", ")")
    return re.sub(r"[\s\[\]{}()_\-./:：，,;；·•]+", "", text)
