"""Frozen E1-E4 paper campaign definitions for the four real wind-tower cases."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

from .e1_real_engineering import discover_real_case_pairs


CAMPAIGN_SCHEMA = "trusted_fem_campaign_v3"
EXPECTED_TOWER_COUNTS = {
    "T01": {"points": 51, "segments": 50, "flanges": 5},
    "T02": {"points": 49, "segments": 48, "flanges": 5},
    "T03": {"points": 50, "segments": 49, "flanges": 5},
    "T04": {"points": 49, "segments": 48, "flanges": 5, "ultimate_load_cases": 5},
}


def prepare_campaign(source_root: str | Path, campaign_root: str | Path) -> dict[str, Any]:
    source = Path(source_root).resolve()
    root = Path(campaign_root).resolve()
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"Campaign directory must be empty and immutable: {root}")
    root.mkdir(parents=True, exist_ok=True)
    pairs = discover_real_case_pairs(source)
    files: list[dict[str, Any]] = []
    for pair in pairs:
        for role in ("raw", "fem"):
            path = Path(pair[role]).resolve()
            files.append(_file_entry(path, f"{pair['case_id']}_{role}"))
    for name, role in (
        ("windtower_shell_static_stiffness_v1.inp", "T02_static_reference"),
        ("10MW_Wind_Tower_S4R_Door.inp", "10MW_external_reference"),
    ):
        path = source / name
        if path.is_file():
            files.append(_file_entry(path, role))
    git = _git_state(source)
    manifest: dict[str, Any] = {
        "schema_version": CAMPAIGN_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "development_frozen" if git["dirty"] else "formal_frozen",
        "formal_result_eligible": not git["dirty"],
        "git": git,
        "model": "qwen3.8-max",
        "temperature": 0.0,
        "compiler_profile": {
            "linear_verification": {"static_nlgeom": False, "modal_prestress": False},
            "engineering_extended": {"static_nlgeom": True, "modal_prestress": True},
        },
        "expected_tower_counts": EXPECTED_TOWER_COUNTS,
        "files": files,
        "experiments": {
            "E1": _e1_definition(),
            "E2": _e2_definition(),
            "E3": _e3_definition(),
            "E4": _e4_definition(),
        },
        "truth_policy": "Score only after immutable candidate output; truth never controls promotion or run retention.",
    }
    _write_json(root / "campaign_manifest.json", manifest)
    for experiment_id in ("E1", "E2", "E3", "E4"):
        directory = root / experiment_id
        directory.mkdir()
        _write_json(directory / "protocol.json", manifest["experiments"][experiment_id])
        (directory / "runs").mkdir()
        (directory / "results").mkdir()
    return manifest


def campaign_status(campaign_root: str | Path) -> dict[str, Any]:
    root = Path(campaign_root).resolve()
    manifest = json.loads((root / "campaign_manifest.json").read_text(encoding="utf-8"))
    changed = []
    for item in manifest["files"]:
        path = Path(item["path"])
        if not path.is_file() or _sha256(path) != item["sha256"]:
            changed.append(item["role"])
    experiments = {}
    for experiment_id in ("E1", "E2", "E3", "E4"):
        runs = list((root / experiment_id / "runs").glob("*.json"))
        experiments[experiment_id] = {"run_count": len(runs), "status": "not_run" if not runs else "partial"}
    return {
        "schema_version": CAMPAIGN_SCHEMA,
        "input_integrity": "pass" if not changed else "fail",
        "changed_inputs": changed,
        "formal_result_eligible": bool(manifest.get("formal_result_eligible")) and not changed,
        "experiments": experiments,
    }


def _e1_definition() -> dict[str, Any]:
    return {
        "research_question": "Can four real engineering packages form accurate, stable and cell-traceable candidates?",
        "conditions": ["R0", "R1-E", "R1-F", "P"],
        "scoring_domains": ["engineering_fields", "fem_fields"],
        "main": {"cases": 4, "R0_repetitions": 1, "R1_repetitions": 5, "top_level_llm_runs": 20},
        "supplement": {"families": ["blank_note", "duplicate_appendix", "numeric_display"], "cases": 12, "R0_repetitions": 1, "R1_repetitions": 3},
        "metrics": ["active_field_accuracy", "case_exact", "normalization_accuracy", "exact_locator_rate", "evidence_coverage", "unsupported_promotion_rate", "repeat_consistency"],
    }


def _e2_definition() -> dict[str, Any]:
    fault_types = [
        "missing_active_thickness",
        "nonpositive_thickness_or_noncontinuous_elevation",
        "load_unit_1000x_mismatch",
        "conflicting_thickness_sources",
        "door_outside_host_section",
        "unsupported_reference_contact_or_pretension",
    ]
    cases = [
        {"fault_id": f"E2-{case_id}-{index + 1:02d}", "case_id": case_id, "fault_type": fault}
        for case_id in EXPECTED_TOWER_COUNTS
        for index, fault in enumerate(fault_types)
    ]
    return {
        "research_question": "Do dual-domain gates block invalid, conflicting and unsupported inputs?",
        "fault_cases": cases,
        "reference_capability_classes": ["compatible", "partially_compatible", "incompatible"],
        "reliability": ["damaged_file", "idempotent_confirmation", "restart_recovery", "stale_revision", "failed_reference_replacement_preserves_state"],
        "primary_metrics": ["unsafe_accept_rate", "false_block_rate"],
        "environment_outcomes": ["environment_skip"],
        "metrics": ["unsafe_accept_rate", "false_block_rate", "stale_revision_rejection", "idempotent_confirmation", "conflict_detection", "physical_anomaly_detection", "incompatibility_block_rate"],
    }


def _e3_definition() -> dict[str, Any]:
    tasks = [
        ("M01", ["geometry.tower_sections.0.thickness_mm"]),
        ("M02", ["geometry.door.width_mm"]),
        ("M03", ["geometry.door.height_mm"]),
        ("M04", ["material.elastic_modulus_mpa"]),
        ("M05", ["material.density"]),
        ("M06", ["mesh.global_size_mm"]),
        ("M07", ["loads.wind_pressure_mpa"]),
        ("M08", ["analysis.modal_modes"]),
        ("M09", ["mesh.global_size_mm", "mesh.door_size_mm"]),
        ("M10", ["loads.top_horizontal_force_n", "loads.wind_pressure_mpa"]),
        ("M11", ["analysis.static_nlgeom", "analysis.modal_prestress"]),
        ("M12", ["geometry.door.width_mm", "geometry.door.height_mm", "mesh.door_size_mm"]),
    ]
    return {
        "research_question": "Are revisions accurate, consumption-closed and deterministically rebuildable?",
        "tasks": [{"task_id": task_id, "keys": keys, "expressions": ["direct", "professional_zh", "colloquial_zh", "en"]} for task_id, keys in tasks],
        "rebuild_repetitions": 20,
        "hashes": ["semantic_hash", "model_hash_sha256", "normalized_inp_sha256", "artifact_sha256"],
        "metrics": ["exact_change_match", "non_target_preservation", "parent_immutability", "stale_revision_rejection", "active_parameter_consumption", "mesh_set_identity", "normalized_inp_hash_identity"],
    }


def _e4_definition() -> dict[str, Any]:
    return {
        "research_question": "Can formal parameters produce checkable, solvable, readable and numerically consistent shell models?",
        "all_cases": {"datacheck": ["static", "modal"], "profile": "linear_verification", "solvers": ["abaqus2020", "native"]},
        "references": {"T02": "windtower_shell_static_stiffness_v1.inp", "10MW": "10MW_Wind_Tower_S4R_Door.inp"},
        "mesh_sensitivity_cases": ["T02", "10MW"],
        "mesh_levels": ["coarse", "medium", "fine"],
        "medium_repetitions": 3,
        "supplement_profile": "engineering_extended",
        "warning_outcomes": ["pass_without_warning", "completed_with_warning", "blocked", "environment_skip"],
        "deferred_metric": ["MAC"],
        "targets": {"datacheck": 1.0, "odb_fields": 1.0, "mass_relative_error": 0.01, "eta_F": 0.01, "eta_M": 0.01, "top_displacement_relative_error": 0.05, "frequency_relative_error": 0.05, "last_two_mesh_change": 0.05},
    }


def _file_entry(path: Path, role: str) -> dict[str, Any]:
    return {"role": role, "path": str(path), "sha256": _sha256(path), "size_bytes": path.stat().st_size}


def _git_state(source: Path) -> dict[str, Any]:
    repository = next((parent for parent in [source, *source.parents] if (parent / ".git").exists()), source)
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, capture_output=True, text=True, check=False).stdout.strip()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repository, capture_output=True, text=True, check=False).stdout
    return {"revision": revision or "unknown", "dirty": bool(status.strip()), "dirty_entries": len(status.splitlines())}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
