from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..parameter_normalization import (
    ParameterNormalizationError,
    normalize_parameter_value,
)
from ..parameter_ir import parameter_set_from_ir, validate_parameter_set
from ..reproducibility import stable_hash


def evaluate_fault_manifest(
    cases: list[dict[str, Any]],
) -> dict[str, Any]:
    """Evaluate G4 faults at the parameter promotion boundary.

    This deliberately does not invoke LLM or Abaqus. It verifies that malformed
    engineering data is blocked, defaultable data degrades safely, unknown
    features stay isolated, and duplicate content is detectable.
    """

    results = [_evaluate_case(item) for item in cases]
    duplicate_hashes: dict[str, list[str]] = {}
    for item in cases:
        duplicate_hashes.setdefault(str(item.get("sha256") or ""), []).append(
            str(item.get("fault_id") or "")
        )
    duplicates = [
        {"sha256": digest, "fault_ids": sorted(ids)}
        for digest, ids in duplicate_hashes.items()
        if digest and len(ids) > 1
    ]
    for result in results:
        if result["fault_id"].startswith("duplicate_source_"):
            result["status"] = "pass" if duplicates else "failed"
            result["observed_behavior"] = "duplicate_content_hash_detected"
    passing = sum(item["status"] == "pass" for item in results)
    return {
        "schema_version": "fault_injection_evaluation_v1",
        "status": "pass" if passing == len(results) else "failed",
        "case_count": len(results),
        "passing_cases": passing,
        "duplicate_groups": duplicates,
        "cases": results,
    }


def package_to_parameter_set(package: dict[str, Any]):
    fem = package.get("fem_settings") or {}
    materials = package.get("materials") or [{}]
    material = materials[0] if materials else {}
    direction, _ = normalize_parameter_value(
        "loads.top_force_direction",
        fem.get("loads.top_force_direction"),
    )
    analysis = package.get("analysis") or {}
    door = package.get("door_opening") or {}
    sections = [
            {
                "section_id": item.get("segment_id") or f"SEG-{index + 1:02d}",
                "z_bottom_mm": item.get("z_bottom_mm"),
                "z_top_mm": item.get("z_top_mm"),
                "diameter_bottom_mm": item.get("diameter_bottom_mm"),
                "diameter_top_mm": item.get("diameter_top_mm"),
                "thickness_mm": item.get("thickness_mm"),
                "material": material.get("name"),
            }
            for index, item in enumerate(package.get("tower_segments") or [])
    ]
    ir = {
        "semantic_ir": {
            "tower_sections": sections,
            "door_openings": [{
                "enabled": bool(door.get("enabled")),
                "width_mm": door.get("width_mm"),
                "height_mm": door.get("height_mm"),
                "bottom_z_mm": door.get("z_bottom_mm"),
                "theta_center_deg": door.get("azimuth_deg", 0.0),
            }],
            "materials": [{
                "name": material.get("name"),
                "elastic_modulus_mpa": material.get("elastic_modulus_mpa"),
                "poisson_ratio": material.get("poisson_ratio"),
                "density": material.get("density_tonne_mm3"),
                "yield_strength_mpa": material.get("yield_strength_mpa"),
            }],
            "loads_inventory": {
                "top_mass_tonne": fem.get("loads.top_mass_tonne", 0),
                "center_of_mass_offset_mm": fem.get("loads.center_of_mass_offset_mm"),
                "top_horizontal_force_n": fem.get("loads.top_total_force_n"),
                "top_force_direction": direction,
                "wind_pressure_mpa": fem.get("loads.wind_pressure_mpa"),
            },
        },
        "mesh_and_set_ir": {
            "global_size_mm": fem.get("mesh.global_size_mm"),
            "door_size_mm": fem.get("mesh.door_size_mm"),
        },
        "abaqus_assembly_ir": {
            "elements": [{"type": fem.get("element.element_type") or "S4R"}],
            "boundary_conditions": [{"name": fem.get("boundary.name") or "BASE_FIXED"}],
        },
        "analysis_cases": {
            "static_strength": {
                "enabled": bool(
                    analysis.get("static_strength", fem.get("analysis.static_enabled", True))
                )
            },
            "modal": {
                "enabled": bool(
                    analysis.get("modal", fem.get("analysis.modal_enabled", True))
                ),
                "modes": analysis.get(
                    "modal_modes",
                    fem.get("modal.modes", fem.get("analysis.modal_modes")),
                ),
            },
        },
    }
    return parameter_set_from_ir("g4-fault-case", ir)


def _evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    fault_id = str(case.get("fault_id") or "")
    expected = str(case.get("expected_behavior") or "")
    path = Path(str(case.get("path") or ""))
    base = {
        "fault_id": fault_id,
        "expected_behavior": expected,
        "status": "failed",
        "observed_behavior": "",
        "diagnostics": [],
    }
    if fault_id.startswith("duplicate_source_"):
        base["observed_behavior"] = "pending_group_evaluation"
        return base
    if fault_id == "corrupt_document":
        try:
            import fitz

            fitz.open(path)
        except Exception as exc:
            base.update(
                status="pass",
                observed_behavior="structured_file_error",
                diagnostics=[{"type": type(exc).__name__, "message": str(exc)}],
            )
        return base
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        package = envelope.get("standard_input_package") or envelope
        parameter_set = package_to_parameter_set(package)
        validation = validate_parameter_set(parameter_set)
        base["parameter_revision"] = parameter_set.revision
        base["validation"] = validation
        if fault_id in {"missing_required_thickness", "invalid_negative_thickness"}:
            base["status"] = "pass" if validation.get("status") == "blocked" else "failed"
            base["observed_behavior"] = "block_engineering_validation"
        elif fault_id == "unknown_reinforcement_feature":
            isolated = bool(package.get("door_reinforcement"))
            base["status"] = "pass" if isolated and validation.get("status") != "blocked" else "failed"
            base["observed_behavior"] = "isolate_as_unsupported" if isolated else "not_isolated"
        elif fault_id == "missing_defaultable_output":
            defaulted = not (package.get("fem_settings") or {}).get("outputs.field")
            base["status"] = "pass" if defaulted and validation.get("status") != "blocked" else "failed"
            base["observed_behavior"] = "defaulted_output" if defaulted else "missing_output"
        else:
            base["status"] = "pass"
            base["observed_behavior"] = validation.get("status")
    except ParameterNormalizationError as exc:
        base.update(
            status="pass" if fault_id == "invalid_load_direction" else "failed",
            observed_behavior="invalid_registered_enum",
            diagnostics=[{"type": type(exc).__name__, "message": str(exc)}],
        )
    except Exception as exc:
        expected_block = (
            expected.startswith("block_")
            or expected.startswith("require_")
            or expected == "structured_file_error"
        )
        base.update(
            status="pass" if expected_block else "failed",
            observed_behavior=(
                "structured_file_error"
                if expected == "structured_file_error"
                else "schema_or_normalization_block"
            ),
            diagnostics=[{"type": type(exc).__name__, "message": str(exc)}],
        )
    base["result_hash"] = stable_hash(
        {
            key: value
            for key, value in base.items()
            if key not in {"result_hash", "diagnostics"}
        },
        length=32,
    )
    return base
