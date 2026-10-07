"""Deterministic S4R wind-tower generation and Abaqus execution."""

from __future__ import annotations

import asyncio
import csv
import html as html_lib
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..build_context import BuildContext, legacy_consumption_report
from ..capability_profile import (
    CAPABILITY_HASH_SHA256,
    CAPABILITY_ID,
    CAPABILITY_REVISION,
    CANONICAL_DEFAULTS,
)
from ..config import Settings
from ..audit import append_audit_event
from ..fea_agent.abaqus_diagnostics import build_abaqus_diagnostic_report
from ..fea_agent.equilibrium import evaluate_equilibrium
from ..meshing.door_quality import validate_door_input, validate_door_mesh_topology
from ..meshing.models import normalize_controls, normalize_door
from ..modeling import (
    ProjectState,
    build_preview_mesh,
    find_flange_ranges,
    geometry_from_ir,
    stat,
    write_json,
)
from ..meshing.sets import door_reinforcement_elements, element_center, windward_surface_groups
from ..output_contract import (
    normalize_output_contract,
    output_contract_audit,
    resolved_output_matrix,
    validate_output_contract,
)
from ..parameter_ir import (
    ParameterSet,
    apply_parameter_set_to_ir,
    parameter_definitions_from_set,
    validate_parameter_set,
)
from ..reproducibility import file_sha256, normalized_inp_sha256


COMPILER_VERSION = "l0-compiler-v4-trace-equilibrium"
UNIT_SYSTEM = {
    "name": "mm-N-MPa-tonne-s",
    "length": "mm",
    "force": "N",
    "stress": "MPa",
    "mass": "tonne",
    "time": "s",
}
GRAVITY_MM_PER_S2 = 9810.0
DEFAULT_TOP_MASS_TONNE = 350.0
DEFAULT_TOP_THRUST_X_N = 800000.0
DEFAULT_CODE_TOP_THRUST_X_N = 500000.0
DEFAULT_WIND_PRESSURE_MPA = 0.0012
DEFAULT_RNA_ECCENTRICITY_MM = 3000.0
TOP_RP_NODE = 900001
TOP_MASS_ELEMENT = 900001
TOP_ROTARY_INERTIA_ELEMENT = 900002
STEEL_DENSITY_TONNE_PER_MM3 = 7.85e-9
L0_JOB_TYPES = ("static_strength", "modal")
EXPERIMENTAL_L0_JOB_TYPES = ("buckling_screening",)
ABAQUS_NAME_RE = re.compile(r"[^A-Za-z0-9_]")


def update_ir_mesh_gate(
    ir: dict[str, Any],
    mesh_quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    quality = mesh_quality or {}
    blocked = bool(quality.get("blocked"))
    errors = quality.get("errors") or []
    warnings = quality.get("warnings") or []
    ir["mesh_automation_gate"] = {
        "schema_version": "mesh_gate_v1",
        "layer": "model",
        "status": "blocked" if blocked else ("warning" if warnings else "pass"),
        "blocked": blocked,
        "errors": errors,
        "warnings": warnings,
    }
    ir["mesh_handoff_contract"] = {
        "schema_version": "mesh_handoff_v1",
        "allowed": not blocked,
        "element_type": "S4R",
    }
    return ir


def validate_l0(state: ProjectState, ir: dict[str, Any] | None = None) -> dict[str, Any]:
    next_ir = enrich_l0_ir(state, ir)
    _write_parameter_consumption(state, next_ir)
    _write_l0_artifact(state, "ir", "l0_ir.json", next_ir)
    state.ir = next_ir
    state.artifacts["l0_ir.json"] = state.root / "ir" / "l0_ir.json"
    return {
        "status": "blocked" if next_ir["validation_report"]["blocked"] else "pass",
        "ir": next_ir,
        "validation_report": next_ir["validation_report"],
        "model_hash": next_ir["model_hash"],
    }


def mesh_l0(state: ProjectState, ir: dict[str, Any] | None = None) -> dict[str, Any]:
    next_ir = enrich_l0_ir(state, ir)
    _write_parameter_consumption(state, next_ir)
    if next_ir["validation_report"]["blocked"]:
        report = _blocked_mesh_report(next_ir)
        _write_l0_artifact(state, "mesh", "mesh_quality_report.json", report)
        state.ir = next_ir
        state.artifacts["mesh_quality_report.json"] = state.root / "mesh" / "mesh_quality_report.json"
        return report
    report = build_l0_mesh_report(next_ir)
    _write_l0_artifact(state, "mesh", "mesh_quality_report.json", report)
    state.ir = next_ir
    state.artifacts["mesh_quality_report.json"] = state.root / "mesh" / "mesh_quality_report.json"
    return report


def build_l0(state: ProjectState, ir: dict[str, Any] | None = None) -> dict[str, Any]:
    next_ir = enrich_l0_ir(state, ir)
    _write_parameter_consumption(state, next_ir)
    if next_ir["validation_report"]["blocked"]:
        return {
            "status": "blocked",
            "model_hash": next_ir["model_hash"],
            "semantic_hash": next_ir["semantic_hash"],
            "model_hash_sha256": next_ir["model_hash_sha256"],
            "jobs": {},
            "errors": next_ir["validation_report"]["errors"],
            "warnings": next_ir["validation_report"]["warnings"],
        }
    mesh_report = build_l0_mesh_report(next_ir)
    if next_ir["validation_report"]["blocked"] or mesh_report["quality_report"]["blocked"]:
        return {
            "status": "blocked",
            "model_hash": next_ir["model_hash"],
            "semantic_hash": next_ir["semantic_hash"],
            "model_hash_sha256": next_ir["model_hash_sha256"],
            "jobs": {},
            "errors": next_ir["validation_report"]["errors"] + mesh_report["quality_report"]["errors"],
            "warnings": next_ir["validation_report"]["warnings"] + mesh_report["quality_report"]["warnings"],
        }

    tower, flanges, door = geometry_from_ir(next_ir)
    if _uses_tower_shell_scope(next_ir):
        flanges = []
    mesh_cfg = next_ir["mesh_and_set_ir"]
    nodes, elements, door_nodes = build_preview_mesh(tower, door, mesh_cfg)
    flange_ranges = _active_flange_ranges(tower, flanges)
    segments = _segments_from_element_centers(nodes, elements, tower)
    sets = build_l0_sets(nodes, elements, door_nodes, flange_ranges, next_ir)
    next_ir.setdefault("load_contract", {})["frozen_resultants"] = freeze_static_load_resultants(
        next_ir, nodes, elements, segments, tower, sets
    )
    next_ir["model_hash_sha256"] = build_model_hash_sha256(next_ir)
    next_ir["model_hash"] = next_ir["model_hash_sha256"][:12]

    inp_dir = state.root / "inp"
    inp_dir.mkdir(parents=True, exist_ok=True)
    jobs: dict[str, Any] = {}
    for job_type in _requested_l0_job_types(next_ir):
        path = _inp_path(inp_dir, state.project_id, next_ir, job_type)
        path.parent.mkdir(parents=True, exist_ok=True)
        compiler_job_type = _compiler_job_type(job_type)
        write_l0_inp(path, compiler_job_type, next_ir, nodes, elements, segments, tower, sets)
        checks = static_check_l0_inp(path, compiler_job_type, next_ir["model_spec_ir"]["outputs"])
        state.artifacts[path.name] = path
        jobs[job_type] = {
            "job_type": job_type,
            "analysis_type": "buckling_screening" if job_type == "buckling_screening" else job_type,
            "artifact_name": path.name,
            "path": str(path),
            "calculation_inp": job_type in L0_JOB_TYPES,
            "experimental": job_type not in L0_JOB_TYPES,
            "checks": checks,
            "status": "ready" if all(checks.values()) else "blocked",
            "normalized_inp_sha256": normalized_inp_sha256(path, state.project_id),
            "artifact_sha256": file_sha256(path),
        }

    build_id = f"{state.project_id}-r{(next_ir.get('parameter_set') or {}).get('revision', 1)}"
    report = {
        "schema_version": "deterministic_build_report_v2",
        "build_id": build_id,
        "status": "ready" if all(job["status"] == "ready" for job in jobs.values()) else "blocked",
        "model_hash": next_ir["model_hash"],
        "semantic_hash": next_ir["semantic_hash"],
        "model_hash_sha256": next_ir["model_hash_sha256"],
        "capability_id": CAPABILITY_ID,
        "capability_revision": CAPABILITY_REVISION,
        "capability_hash_sha256": CAPABILITY_HASH_SHA256,
        "jobs": jobs,
        "calculation_jobs": {name: job for name, job in jobs.items() if job.get("calculation_inp")},
        "experimental_jobs": {name: job for name, job in jobs.items() if job.get("experimental")},
        "mesh_report": mesh_report,
        "load_contract": next_ir.get("load_contract", {}),
        "load_summary": next_ir.get("analysis_cases", {}).get("static_strength", {}),
        "analysis_readiness": next_ir.get("analysis_readiness", {}),
        "modal_policy": next_ir.get("modal_policy", {}),
        "output_contract": next_ir.get("model_spec_ir", {}).get("outputs", {}),
        "output_request_audit": next_ir.get("output_contract_audit", {}),
        "generator": "wind_tower_s4r_adapter",
        "parameter_revision": (next_ir.get("parameter_set") or {}).get("revision", 1),
        "errors": [],
        "warnings": next_ir["validation_report"]["warnings"] + mesh_report["quality_report"]["warnings"],
    }
    _attach_consumption_artifact_hashes(next_ir)
    report["parameter_consumption"] = next_ir.get("parameter_consumption", {})
    _write_parameter_consumption(state, next_ir)
    _write_l0_artifact(state, "inp", "l0_build_report.json", report)
    _write_l0_artifact(state, "inp", "load_contract.json", next_ir.get("load_contract", {}))
    state.artifacts["l0_build_report.json"] = state.root / "inp" / "l0_build_report.json"
    state.artifacts["inp/load_contract.json"] = state.root / "inp" / "load_contract.json"
    state.artifacts["load_contract.json"] = state.root / "inp" / "load_contract.json"
    state.ir = next_ir
    write_json(state.root / "ir.json", next_ir)
    state.artifacts["ir.json"] = state.root / "ir.json"
    append_audit_event(
        state.root / "audit" / "events.jsonl",
        "inp_build_completed",
        {
            "project_id": state.project_id,
            "build_id": build_id,
            "parameter_revision": report["parameter_revision"],
            "generator": report["generator"],
            "jobs": {
                key: {
                    "status": value.get("status"),
                    "artifact_name": value.get("artifact_name"),
                }
                for key, value in jobs.items()
            },
        },
    )
    state.artifacts["audit/events.jsonl"] = state.root / "audit" / "events.jsonl"
    return report


def mesh_vtk_preview_l0(state: ProjectState, ir: dict[str, Any] | None = None) -> dict[str, Any]:
    next_ir = enrich_l0_ir(state, ir)
    if next_ir["validation_report"]["blocked"]:
        report = _blocked_mesh_report(next_ir)
        return {
            "status": "blocked",
            "model_hash": next_ir.get("model_hash"),
            "artifact_name": "",
            "artifact_url": "",
            "scalar_arrays": [],
            "cell_data_arrays": [],
            "point_data_arrays": [],
            "mesh_stats": report["mesh_stats"],
            "quality_report": report["quality_report"],
            "feature_sets": {},
            "warnings": report["warnings"],
            "fallback_required": True,
        }
    tower, flanges, door = geometry_from_ir(next_ir)
    if _uses_tower_shell_scope(next_ir):
        flanges = []
    mesh_cfg = next_ir["mesh_and_set_ir"]
    nodes, elements, door_nodes = build_preview_mesh(tower, door, mesh_cfg)
    flange_ranges = _active_flange_ranges(tower, flanges)
    sets = build_l0_sets(nodes, elements, door_nodes, flange_ranges, next_ir)
    quality = compute_mesh_quality(nodes, elements, sets, next_ir)
    update_ir_mesh_gate(next_ir, quality)
    mesh_dir = state.root / "mesh"
    mesh_dir.mkdir(parents=True, exist_ok=True)
    vtk_name = "l0_true_mesh_preview.vtk"
    vtk_path = mesh_dir / vtk_name
    vtk_meta = _write_l0_vtk_preview(vtk_path, nodes, elements, sets, quality)
    artifact_key = f"mesh/{vtk_name}"
    state.artifacts[artifact_key] = vtk_path
    state.artifacts[vtk_name] = vtk_path
    metadata = {
        "status": "blocked" if quality["blocked"] else ("warning" if quality["warnings"] else "pass"),
        "model_hash": next_ir["model_hash"],
        "artifact_name": artifact_key,
        "artifact_url": f"/api/projects/{state.project_id}/artifacts/{artifact_key}",
        "scalar_arrays": vtk_meta["cell_data_arrays"],
        "cell_data_arrays": vtk_meta["cell_data_arrays"],
        "point_data_arrays": vtk_meta["point_data_arrays"],
        "mesh_stats": {
            "nodes": len(nodes),
            "s4r_elements": len(elements),
            "door_boundary_nodes": len(sets["door_nodes"]),
            "door_patch_elements": len(sets["door_patch_elements"]),
            "door_neighbor_elements": len(sets["door_neighbor_elements"]),
            "windward_spos_elements": len(sets["wind_spos"]),
            "windward_sneg_elements": len(sets["wind_sneg"]),
            "global_size_mm": mesh_cfg["global_size_mm"],
            "door_size_mm": mesh_cfg["door_size_mm"],
            "flange_size_mm": mesh_cfg["flange_size_mm"],
            "element_family": "S4R shell",
        },
        "quality_report": quality,
        "mesh_automation_gate": next_ir.get("mesh_automation_gate"),
        "mesh_handoff_contract": next_ir.get("mesh_handoff_contract"),
        "focus_targets": {
            "door": _door_focus_target(nodes, elements, sets, door, mesh_cfg),
        },
        "feature_sets": {
            "door_patch_elements": len(sets["door_patch_elements"]),
            "door_neighbor_elements": len(sets["door_neighbor_elements"]),
            "windward_elements": len(set(sets["wind_spos"]) | set(sets["wind_sneg"])),
            "high_aspect_ratio_elements": vtk_meta["high_aspect_ratio_elements"],
        },
        "viewer_defaults": {
            "scalar": "feature_code",
            "show_edges": True,
            "background": "white",
            "notes": [
                "VTK preview uses the real S4R shell mesh generated before INP compilation.",
                "feature_code: 1=door patch, 2=windward surface, 4=high aspect ratio.",
            ],
        },
        "warnings": [item["message"] for item in quality["warnings"]],
        "fallback_required": False,
    }
    _write_l0_artifact(state, "mesh", "vtk_preview.json", metadata)
    state.artifacts["mesh/vtk_preview.json"] = state.root / "mesh" / "vtk_preview.json"
    state.ir = next_ir
    return metadata


def mesh_pyvista_preview_l0(state: ProjectState, ir: dict[str, Any] | None = None) -> dict[str, Any]:
    next_ir = enrich_l0_ir(state, ir)
    html_name = "pyvista_preview.html"
    meta_name = "pyvista_preview.json"
    mesh_data_name = "pyvista_preview_mesh.json"
    overlay_name = "assembly_overlay.json"
    html_key = f"mesh/{html_name}"
    meta_key = f"mesh/{meta_name}"
    mesh_data_key = f"mesh/{mesh_data_name}"
    overlay_key = f"mesh/{overlay_name}"
    if next_ir["validation_report"]["blocked"]:
        report = _blocked_mesh_report(next_ir)
        return {
            "status": "blocked",
            "viewer": "pyvista_html",
            "source": "direct_pyvista_unstructured_grid",
            "model_hash": next_ir.get("model_hash"),
            "artifact_name": "",
            "artifact_url": "",
            "mesh_stats": report["mesh_stats"],
            "quality_report": report["quality_report"],
            "warnings": report["warnings"],
            "fallback_required": False,
        }

    tower, flanges, door = geometry_from_ir(next_ir)
    if _uses_tower_shell_scope(next_ir):
        flanges = []
    mesh_cfg = next_ir["mesh_and_set_ir"]
    nodes, elements, door_nodes = build_preview_mesh(tower, door, mesh_cfg)
    flange_ranges = _active_flange_ranges(tower, flanges)
    sets = build_l0_sets(nodes, elements, door_nodes, flange_ranges, next_ir)
    quality = compute_mesh_quality(nodes, elements, sets, next_ir)
    update_ir_mesh_gate(next_ir, quality)

    mesh_dir = state.root / "mesh"
    mesh_dir.mkdir(parents=True, exist_ok=True)
    html_path = mesh_dir / html_name
    mesh_data_path = mesh_dir / mesh_data_name
    overlay_path = mesh_dir / overlay_name
    preview_result = _write_pyvista_preview(html_path, nodes, elements, sets, quality)
    mesh_data_meta = _write_pyvista_mesh_data(mesh_data_path, nodes, elements, sets, quality)
    overlay_meta = _write_assembly_overlay(overlay_path, nodes, elements, sets, next_ir)
    warnings = [item["message"] for item in quality["warnings"]] + preview_result["warnings"]
    metadata = {
        "status": (
            "blocked"
            if quality["blocked"]
            else ("warning" if quality["warnings"] or preview_result["warnings"] else "pass")
        ),
        "viewer": preview_result["viewer"],
        "source": "direct_pyvista_unstructured_grid",
        "model_hash": next_ir["model_hash"],
        "artifact_name": html_key,
        "artifact_url": f"/api/projects/{state.project_id}/artifacts/{html_key}",
        "mesh_data_name": mesh_data_key,
        "mesh_data_url": f"/api/projects/{state.project_id}/artifacts/{mesh_data_key}",
        "assembly_overlay_name": overlay_key,
        "assembly_overlay_url": f"/api/projects/{state.project_id}/artifacts/{overlay_key}",
        "mesh_stats": _preview_mesh_stats(nodes, elements, sets, mesh_cfg),
        "quality_report": quality,
        "mesh_automation_gate": next_ir.get("mesh_automation_gate"),
        "mesh_handoff_contract": next_ir.get("mesh_handoff_contract"),
        "feature_sets": {
            "door_patch_elements": len(sets["door_patch_elements"]),
            "door_neighbor_elements": len(sets["door_neighbor_elements"]),
            "windward_elements": len(set(sets["wind_spos"]) | set(sets["wind_sneg"])),
            "high_aspect_ratio_elements": preview_result["high_aspect_ratio_elements"],
        },
        "mesh_data": mesh_data_meta,
        "assembly_overlay": overlay_meta,
        "warnings": warnings,
        "fallback_required": preview_result["fallback_required"],
    }
    _write_l0_artifact(state, "mesh", meta_name, metadata)
    state.artifacts[html_key] = html_path
    state.artifacts[html_name] = html_path
    state.artifacts[mesh_data_key] = mesh_data_path
    state.artifacts[mesh_data_name] = mesh_data_path
    state.artifacts[overlay_key] = overlay_path
    state.artifacts[overlay_name] = overlay_path
    state.artifacts[meta_key] = state.root / "mesh" / meta_name
    state.ir = next_ir
    return metadata


def open_pyvista_desktop_l0(state: ProjectState, ir: dict[str, Any] | None = None) -> dict[str, Any]:
    preview = mesh_pyvista_preview_l0(state, ir)
    if preview.get("status") == "blocked":
        return {
            "status": "blocked",
            "message": "PyVista desktop viewer was not launched because mesh generation is blocked.",
            "preview": preview,
            "warnings": preview.get("warnings", []),
        }
    mesh_data_name = preview.get("mesh_data_name")
    if not mesh_data_name:
        return {
            "status": "blocked",
            "message": "PyVista desktop viewer was not launched because mesh data artifact is missing.",
            "preview": preview,
            "warnings": ["mesh_data_name is missing from preview metadata."],
        }
    mesh_data_path = state.root / str(mesh_data_name)
    if not mesh_data_path.exists():
        return {
            "status": "blocked",
            "message": "PyVista desktop viewer was not launched because mesh data artifact does not exist.",
            "preview": preview,
            "warnings": [f"Missing mesh data artifact: {mesh_data_name}"],
        }
    mesh_dir = state.root / "mesh"
    launcher_path = mesh_dir / "open_pyvista_desktop.py"
    stdout_path = mesh_dir / "pyvista_desktop_stdout.log"
    stderr_path = mesh_dir / "pyvista_desktop_stderr.log"
    launcher_path.write_text(_pyvista_desktop_launcher_script(), encoding="utf-8")
    python_exe = _desktop_python_executable()
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
        process = subprocess.Popen(
            [python_exe, str(launcher_path), str(mesh_data_path)],
            cwd=str(mesh_dir),
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
            close_fds=False,
        )
    state.artifacts["mesh/open_pyvista_desktop.py"] = launcher_path
    state.artifacts["mesh/pyvista_desktop_stdout.log"] = stdout_path
    state.artifacts["mesh/pyvista_desktop_stderr.log"] = stderr_path
    return {
        "status": "launched",
        "viewer": "pyvista_desktop",
        "pid": process.pid,
        "python": python_exe,
        "launcher": "mesh/open_pyvista_desktop.py",
        "mesh_data_name": mesh_data_name,
        "logs": {
            "stdout": "mesh/pyvista_desktop_stdout.log",
            "stderr": "mesh/pyvista_desktop_stderr.log",
        },
        "preview": preview,
        "warnings": preview.get("warnings", []),
    }


def datacheck_l0(
    state: ProjectState,
    settings: Settings,
    job_type: str = "all",
    ir: dict[str, Any] | None = None,
    build_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return run_l0_job(
        state, settings, job_type=job_type, mode="datacheck", ir=ir,
        build_report=build_report,
    )


def run_l0(state: ProjectState, settings: Settings, job_type: str = "all", ir: dict[str, Any] | None = None) -> dict[str, Any]:
    return run_l0_job(state, settings, job_type=job_type, mode="run", ir=ir)


def submit_l0(
    state: ProjectState,
    settings: Settings,
    job_type: str = "all",
    ir: dict[str, Any] | None = None,
    run_postprocess: bool = True,
) -> dict[str, Any]:
    submissions_dir = state.root / "jobs" / "submissions"
    submissions_dir.mkdir(parents=True, exist_ok=True)
    submission_id = f"sub-{uuid.uuid4().hex[:12]}"
    status_path = submissions_dir / f"{submission_id}.json"
    payload = {
        "submission_id": submission_id,
        "project_id": state.project_id,
        "job_type": job_type,
        "run_postprocess": run_postprocess,
        "status": "queued",
        "created_at": time.time(),
        "updated_at": time.time(),
        "jobs": {},
        "categories": [],
        "artifacts": {},
        "postprocess": None,
    }
    _write_submission_status(status_path, payload)
    worker = threading.Thread(
        target=_run_l0_submission_worker,
        args=(state, settings, status_path, job_type, deepcopy(ir) if ir is not None else None, run_postprocess),
        daemon=True,
    )
    worker.start()
    return payload


def get_l0_submission_status(state: ProjectState, submission_id: str) -> dict[str, Any]:
    status_path = state.root / "jobs" / "submissions" / f"{submission_id}.json"
    if not status_path.exists():
        raise FileNotFoundError(f"L0 submission not found: {submission_id}")
    return json.loads(status_path.read_text(encoding="utf-8"))


def _run_l0_submission_worker(
    state: ProjectState,
    settings: Settings,
    status_path: Path,
    job_type: str,
    ir: dict[str, Any] | None,
    run_postprocess: bool,
) -> None:
    status = json.loads(status_path.read_text(encoding="utf-8"))
    try:
        status.update({"status": "running", "updated_at": time.time()})
        _write_submission_status(status_path, status)
        run_result = run_l0_job(state, settings, job_type=job_type, mode="run", ir=ir)
        status.update(
            {
                "status": run_result.get("status", "blocked"),
                "updated_at": time.time(),
                "jobs": run_result.get("jobs", {}),
                "categories": run_result.get("categories", []),
                "run": run_result,
                "artifacts": _submission_artifacts(run_result),
            }
        )
        _write_submission_status(status_path, status)
        if run_postprocess and status["status"] in {"pass", "warning"}:
            post = postprocess_l0(state, settings)
            status.update(
                {
                    "status": _merge_submission_status(status["status"], post.get("status")),
                    "updated_at": time.time(),
                    "postprocess": post,
                    "results": post,
                    "artifacts": {**status.get("artifacts", {}), **post.get("artifacts", {})},
                }
            )
            _write_submission_status(status_path, status)
    except Exception as exc:
        status.update(
            {
                "status": "blocked",
                "updated_at": time.time(),
                "categories": status.get("categories", []) + [{"category": "submission_error", "severity": "fatal", "message": str(exc)}],
            }
        )
        _write_submission_status(status_path, status)


def _write_submission_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _merge_submission_status(run_status: str | None, post_status: str | None) -> str:
    statuses = {str(run_status or ""), str(post_status or "")}
    if "blocked" in statuses:
        return "blocked"
    if "warning" in statuses:
        return "warning"
    if "pass" in statuses:
        return "pass"
    return str(run_status or post_status or "warning")


def _submission_artifacts(run_result: dict[str, Any]) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    for job in run_result.get("jobs", {}).values():
        for label, name in (job.get("artifacts") or {}).items():
            artifacts[str(label)] = str(name)
    return artifacts


def job_status_l0(state: ProjectState, job_id: str) -> dict[str, Any]:
    for job_dir in (state.root / "jobs").glob("*"):
        status_path = job_dir / "job_status.json"
        if status_path.exists():
            data = json.loads(status_path.read_text(encoding="utf-8"))
            if data.get("job_id") == job_id:
                return data
    return {"job_id": job_id, "status": "not_found", "categories": [{"category": "job_missing", "severity": "warning", "message": "Job status was not found."}]}


def postprocess_l0(
    state: ProjectState,
    settings: Settings | None = None,
    *,
    odb_paths: dict[str, Path | None] | None = None,
    results_dir_override: Path | None = None,
    expected_job_types: set[str] | None = None,
) -> dict[str, Any]:
    state.ir = enrich_l0_ir(state, state.ir)
    results_dir = results_dir_override or (state.root / "results")
    results_dir.mkdir(parents=True, exist_ok=True)
    static_odb = odb_paths.get("static_strength") if odb_paths is not None else _find_job_artifact(state, "static_strength", ".odb")
    modal_odb = odb_paths.get("modal") if odb_paths is not None else _find_job_artifact(state, "modal", ".odb")
    buckling_enabled = _experimental_buckling_enabled(state.ir)
    expected = expected_job_types or set(_requested_l0_job_types(state.ir))
    buckling_odb = (
        odb_paths.get("buckling_screening")
        if odb_paths is not None and buckling_enabled
        else (_find_job_artifact(state, "buckling_screening", ".odb") if buckling_enabled else None)
    )
    warnings: list[str] = []
    script_path = results_dir / "extract_l0_odb.py"
    script_path.write_text(_odb_extraction_script(), encoding="utf-8")
    state.artifacts["extract_l0_odb.py"] = script_path

    summary = {
        "status": "pass",
        "model_hash": state.ir.get("model_hash"),
        "stress_extraction": {
            "position": "integration_point",
            "shell_section_policy": "max_over_section_points",
            "averaging": False,
        },
        "artifacts": {},
        "warnings": warnings,
    }
    availability_path = results_dir / "output_availability_report.json"
    availability_path.write_text(
        json.dumps(
            {
                "schema_version": "abaqus_output_availability_v1",
                "status": "pending_odb_extraction",
                "output_contract": state.ir.get("model_spec_ir", {}).get("outputs", {}),
                "requested_output_matrix": resolved_output_matrix(state.ir["model_spec_ir"]["outputs"]),
                "jobs": {},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    windows = state.ir.get("derived_geometry", {}).get("flange_elevation_stress_windows", [])
    _write_csv(results_dir / "stress_windows.csv", _placeholder_stress_rows(windows))
    _write_csv(results_dir / "door_stress_summary.csv", _placeholder_door_rows(state.ir))
    _write_csv(results_dir / "modal_frequencies.csv", _placeholder_modal_rows(modal_odb))
    if buckling_enabled:
        _write_csv(results_dir / "buckling_factors.csv", _placeholder_buckling_rows(buckling_odb))
    _write_csv(results_dir / "top_displacement_summary.csv", _placeholder_displacement_rows(static_odb))
    (results_dir / "top_displacement_summary.json").write_text(
        json.dumps({"status": "pending_odb_extraction" if not static_odb else "pending_extraction", "static_odb": str(static_odb) if static_odb else ""}, indent=2),
        encoding="utf-8",
    )
    mesh_report = build_l0_mesh_report(state.ir) if not state.ir.get("validation_report", {}).get("blocked") else _blocked_mesh_report(state.ir)
    (results_dir / "mesh_summary.json").write_text(json.dumps(mesh_report.get("mesh_stats", {}), indent=2), encoding="utf-8")

    if "static_strength" in expected and not static_odb:
        warnings.append("Static ODB not found; result CSV files contain pending placeholders.")
        summary["status"] = "warning"
    if "modal" in expected and not modal_odb:
        warnings.append("Modal ODB not found; modal frequencies are pending.")
        summary["status"] = "warning"
    if buckling_enabled and not buckling_odb:
        warnings.append("Buckling ODB not found; buckling factors are pending.")
        summary["status"] = "warning"
    equilibrium = {
        "status": "pending" if warnings else "pass",
        "checks": {
            "total_mass": "pending_odb_extraction",
            "gravity_reaction": "pending_odb_extraction",
            "horizontal_reaction": "pending_odb_extraction",
            "base_moment": "pending_odb_extraction",
        },
    }
    (results_dir / "equilibrium_check.json").write_text(json.dumps(equilibrium, indent=2), encoding="utf-8")
    summary["artifacts"] = {
        "odb_extraction_script": "extract_l0_odb.py",
        "stress_windows": "stress_windows.csv",
        "door_stress_summary": "door_stress_summary.csv",
        "modal_frequencies": "modal_frequencies.csv",
        "top_displacement_summary": "top_displacement_summary.csv",
        "mesh_summary": "mesh_summary.json",
        "equilibrium_check": "equilibrium_check.json",
        "output_availability_report": "output_availability_report.json",
    }
    if buckling_enabled:
        summary["artifacts"]["buckling_factors"] = "buckling_factors.csv"
    extracted_summary_path = results_dir / "results_summary.json"
    if extracted_summary_path.exists():
        try:
            extracted_summary_path.unlink()
        except OSError:
            pass
    extraction = _try_extract_l0_odb(
        state,
        settings,
        script_path,
        static_odb,
        modal_odb,
        buckling_odb,
        expected_job_types=expected,
    )
    summary["odb_extraction"] = extraction
    if extraction.get("status") == "blocked":
        warnings.append("ODB extraction script failed; result CSV files may contain pending placeholders.")
        summary["status"] = "warning"
    elif extraction.get("status") == "pass":
        if extracted_summary_path.exists():
            try:
                extracted_summary = json.loads(extracted_summary_path.read_text(encoding="utf-8"))
                summary.update({key: value for key, value in extracted_summary.items() if key not in {"artifacts", "warnings"}})
            except Exception:
                pass
        if warnings and summary.get("status") == "pass":
            summary["status"] = "warning"
        equilibrium_result = _evaluate_extracted_equilibrium(state.ir, results_dir)
        summary["equilibrium_check"] = equilibrium_result
        if equilibrium_result.get("status") == "blocked":
            summary["status"] = "blocked"
            warnings.append("Force/moment equilibrium closure failed or required RF/RM data is missing.")
    result_mesh = _write_odb_result_mesh(state, results_dir)
    if result_mesh.get("status") == "warning":
        warnings.extend(result_mesh.get("warnings", []))
        summary["status"] = "warning"
    summary["artifacts"].update(
        {
            "odb_result_mesh": "odb_result_mesh.json",
            "static_field_data": "static_field_data.json",
            "overall_static_summary": "overall_static_summary.csv",
            "reaction_resultants": "reaction_resultants.json",
        }
    )
    summary["warnings"] = warnings
    (results_dir / "results_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (results_dir / "result_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    for name in [
        "results_summary.json",
        "result_summary.json",
        "extract_l0_odb.py",
        "stress_windows.csv",
        "door_stress_summary.csv",
        "modal_frequencies.csv",
        "top_displacement_summary.csv",
        "top_displacement_summary.json",
        "mesh_summary.json",
        "equilibrium_check.json",
        "reaction_resultants.json",
        "odb_result_mesh.json",
        "static_field_data.json",
        "overall_static_summary.csv",
        "output_availability_report.json",
    ]:
        if (results_dir / name).exists():
            state.artifacts[name] = results_dir / name
    if buckling_enabled:
        state.artifacts["buckling_factors.csv"] = results_dir / "buckling_factors.csv"
    return summary


def _evaluate_extracted_equilibrium(ir: dict[str, Any], results_dir: Path) -> dict[str, Any]:
    path = results_dir / "reaction_resultants.json"
    reactions: dict[str, Any] | None = None
    if path.is_file():
        try:
            reactions = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            reactions = None
    frozen = (ir.get("load_contract") or {}).get("frozen_resultants")
    result = evaluate_equilibrium(frozen, reactions)
    (results_dir / "equilibrium_check.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def _try_extract_l0_odb(
    state: ProjectState,
    settings: Settings | None,
    script_path: Path,
    static_odb: Path | None,
    modal_odb: Path | None,
    buckling_odb: Path | None,
    expected_job_types: set[str] | None = None,
) -> dict[str, Any]:
    if not any((static_odb, modal_odb, buckling_odb)):
        return {"status": "skipped", "reason": "No ODB files were found."}
    if settings is None:
        return {"status": "skipped", "reason": "Abaqus settings were not provided."}
    abaqus_available = Path(settings.abaqus_cmd).exists() or shutil.which(settings.abaqus_cmd) is not None
    if not abaqus_available:
        return {"status": "blocked", "reason": f"Abaqus command not found: {settings.abaqus_cmd}"}
    results_dir = script_path.parent
    env = {
        **dict(os.environ),
        "L0_STATIC_ODB": str(static_odb.resolve()) if static_odb else "",
        "L0_MODAL_ODB": str(modal_odb.resolve()) if modal_odb else "",
        "L0_BUCKLING_ODB": str(buckling_odb.resolve()) if buckling_odb else "",
        "L0_ENABLE_BUCKLING": "1" if _experimental_buckling_enabled(state.ir) else "0",
        "L0_EXPECTED_JOBS_JSON": json.dumps(sorted(expected_job_types or {"static_strength", "modal"})),
        "L0_INSTANCE": abaqus_name(state.ir.get("mesh_and_set_ir", {}).get("naming_rules", {}).get("instance", "WINDTOWER_SHELL_L0-1"), "WINDTOWER_SHELL_L0_1"),
        "L0_OUTPUT_CONTRACT_JSON": json.dumps(state.ir.get("model_spec_ir", {}).get("outputs", {}), ensure_ascii=True),
        "L0_OUTPUT_MATRIX_JSON": json.dumps(resolved_output_matrix(state.ir["model_spec_ir"]["outputs"]), ensure_ascii=True),
    }
    command = [settings.abaqus_cmd, "python", str(script_path.resolve())]
    try:
        result = subprocess.run(
            command,
            cwd=results_dir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=settings.abaqus_odb_timeout_seconds,
        )
    except Exception as exc:
        (results_dir / "odb_extract_run.log").write_text(str(exc), encoding="utf-8")
        return {"status": "blocked", "command": " ".join(command), "reason": str(exc)}
    log_text = (result.stdout or "") + "\n" + (result.stderr or "")
    (results_dir / "odb_extract_run.log").write_text(log_text, encoding="utf-8", errors="ignore")
    state.artifacts["odb_extract_run.log"] = results_dir / "odb_extract_run.log"
    diagnostic = build_abaqus_diagnostic_report(log_text, result.returncode, "postprocess")
    return {
        "status": "pass" if result.returncode == 0 else "blocked",
        "returncode": result.returncode,
        "command": " ".join(command),
        "log": "odb_extract_run.log",
        "diagnostic_report": diagnostic,
    }


def _write_odb_result_mesh(state: ProjectState, results_dir: Path) -> dict[str, Any]:
    results_dir.mkdir(parents=True, exist_ok=True)
    field_path = results_dir / "static_field_data.json"
    if field_path.exists():
        try:
            field_data = json.loads(field_path.read_text(encoding="utf-8"))
        except Exception:
            field_data = {"status": "blocked", "warnings": ["static_field_data.json could not be parsed."]}
    else:
        field_data = {
            "status": "pending",
            "warnings": ["Static ODB field data is not available; no numerical result values were emitted."],
        }
        field_path.write_text(json.dumps(field_data, ensure_ascii=False, indent=2), encoding="utf-8")

    scalar_data = field_data.get("scalars") or {}
    vector_data = field_data.get("vectors") or {}
    required_fields = {
        "S_Mises_by_element": scalar_data.get("S_Mises_by_element"),
        "U_mag_by_node": scalar_data.get("U_mag_by_node"),
        "U_by_node": vector_data.get("U_by_node"),
    }
    missing_fields = [name for name, value in required_fields.items() if not isinstance(value, dict) or not value]
    if field_data.get("status") != "pass" or missing_fields:
        status = "pending" if field_data.get("status") == "pending" else "blocked"
        payload = {
            "schema_version": "odb_result_mesh_v1",
            "status": status,
            "source": field_data.get("source", "Abaqus ODB extraction"),
            "missing_fields": missing_fields,
            "points": [],
            "deformed_points": [],
            "quads": [],
            "cell_data": {},
            "point_data": {},
            "scalar_arrays": [],
            "summary": None,
            "warnings": field_data.get("warnings", []),
        }
        path = results_dir / "odb_result_mesh.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        return {"status": status, "warnings": payload["warnings"], "artifact": "odb_result_mesh.json"}

    ir = enrich_l0_ir(state, state.ir)
    tower, flanges, door = geometry_from_ir(ir)
    mesh_cfg = ir["mesh_and_set_ir"]
    nodes, elements, door_nodes = build_preview_mesh(tower, door, mesh_cfg)
    flange_ranges = _active_flange_ranges(tower, flanges)
    sets = build_l0_sets(nodes, elements, door_nodes, flange_ranges, ir)
    quality = compute_mesh_quality(nodes, elements, sets, ir)
    ordered_nodes = sorted(nodes)
    ordered_elements = sorted(elements)
    node_index = {nid: index for index, nid in enumerate(ordered_nodes)}
    element_mises = {str(key): float(value) for key, value in (field_data.get("scalars", {}).get("S_Mises_by_element") or {}).items() if _is_number(value)}
    node_u_mag = {str(key): float(value) for key, value in (field_data.get("scalars", {}).get("U_mag_by_node") or {}).items() if _is_number(value)}
    node_u_vec = field_data.get("vectors", {}).get("U_by_node") or {}

    points: list[list[float]] = []
    deformed_points: list[list[float]] = []
    u_mag_values: list[float] = []
    for nid in ordered_nodes:
        x, y, z = nodes[nid]
        points.append([float(x), float(y), float(z)])
        vec = node_u_vec.get(str(nid), [0.0, 0.0, 0.0])
        if not isinstance(vec, list) or len(vec) < 3:
            vec = [0.0, 0.0, 0.0]
        ux, uy, uz = (float(vec[0] or 0.0), float(vec[1] or 0.0), float(vec[2] or 0.0))
        deformed_points.append([float(x + ux), float(y + uy), float(z + uz)])
        u_mag_values.append(float(node_u_mag.get(str(nid), math.sqrt(ux * ux + uy * uy + uz * uz))))

    quads: list[list[int]] = []
    s_mises_values: list[float] = []
    feature_codes: list[int] = []
    door_patch = set(sets.get("door_patch_elements", []))
    windward = set(sets.get("wind_spos", [])) | set(sets.get("wind_sneg", []))
    for eid in ordered_elements:
        conn = elements[eid][:4]
        quads.append([node_index[nid] for nid in conn])
        s_mises_values.append(float(element_mises.get(str(eid), 0.0)))
        code = 0
        if eid in door_patch:
            code += 1
        if eid in windward:
            code += 2
        feature_codes.append(code)

    bounds = {
        "min": [min((point[index] for point in points), default=0.0) for index in range(3)],
        "max": [max((point[index] for point in points), default=0.0) for index in range(3)],
    }
    payload = {
        "schema_version": "odb_result_mesh_v1",
        "status": "pass" if field_data.get("status") == "pass" else "warning",
        "source": field_data.get("source", "Abaqus ODB extraction"),
        "units": {"length": "mm", "stress": "MPa", "displacement": "mm"},
        "points": points,
        "deformed_points": deformed_points,
        "quads": quads,
        "cell_data": {"element_id": ordered_elements, "feature_code": feature_codes, "S_Mises": s_mises_values},
        "point_data": {"node_id": ordered_nodes, "U_mag": u_mag_values},
        "scalar_arrays": ["S_Mises", "U_mag"],
        "default_scalar": "S_Mises",
        "bounds": bounds,
        "quality_report": quality,
        "summary": {
            "max_mises": max(s_mises_values, default=0.0),
            "max_u_mag": max(u_mag_values, default=0.0),
            "door_patch_elements": len(door_patch),
            "node_count": len(points),
            "element_count": len(quads),
        },
        "warnings": field_data.get("warnings", []),
    }
    path = results_dir / "odb_result_mesh.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return {"status": payload["status"], "warnings": payload["warnings"], "artifact": "odb_result_mesh.json"}


def _is_number(value: Any) -> bool:
    try:
        float(value)
        return True
    except Exception:
        return False


def _odb_extraction_script() -> str:
    return r'''# Run with: abaqus python extract_l0_odb.py
from __future__ import print_function

import csv
import json
import os
import sys

try:
    from odbAccess import openOdb
    from abaqusConstants import INTEGRATION_POINT
except Exception as exc:
    print("This script must be run with Abaqus Python:", exc)
    sys.exit(2)


def write_csv(path, rows, default_fields=None):
    fields = sorted(set(k for row in rows for k in row)) if rows else (default_fields or ["status"])
    with open(path, "w") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def mises_values_for_set(odb, step_name, instance_name, set_name):
    step = odb.steps[step_name]
    if not step.frames:
        return []
    frame = step.frames[-1]
    if "S" not in frame.fieldOutputs.keys():
        return []
    stress = frame.fieldOutputs["S"]
    instance = odb.rootAssembly.instances[instance_name]
    if set_name not in instance.elementSets.keys():
        return []
    region = instance.elementSets[set_name]
    subset = stress.getSubset(region=region, position=INTEGRATION_POINT)
    values = {}
    for value in subset.values:
        label = value.elementLabel
        mises = getattr(value, "mises", None)
        if mises is None:
            continue
        values[label] = max(values.get(label, 0.0), float(mises))
    return list(values.items())


def element_centroid(instance, label):
    try:
        for element in instance.elements:
            if element.label == label:
                pts = []
                for node_label in element.connectivity:
                    node = instance.getNodeFromLabel(node_label)
                    pts.append(node.coordinates)
                if not pts:
                    return ""
                x = sum(p[0] for p in pts) / len(pts)
                y = sum(p[1] for p in pts) / len(pts)
                z = sum(p[2] for p in pts) / len(pts)
                return "%.6g,%.6g,%.6g" % (x, y, z)
    except Exception:
        return ""
    return ""


def stats(rows):
    vals = sorted(v for _, v in rows)
    if not vals:
        return {"element_count": 0, "max_mises": "pending_odb_extraction", "mean_mises": "pending_odb_extraction", "p95_mises": "pending_odb_extraction", "p99_mises": "pending_odb_extraction", "max_element_label": "", "max_element_centroid": ""}
    def pct(p):
        idx = min(len(vals) - 1, int(round((len(vals) - 1) * p)))
        return vals[idx]
    max_label = sorted(rows, key=lambda item: item[1])[-1][0]
    return {
        "element_count": len(vals),
        "max_mises": vals[-1],
        "mean_mises": sum(vals) / len(vals),
        "p95_mises": pct(0.95),
        "p99_mises": pct(0.99),
        "max_element_label": max_label,
        "max_element_centroid": "",
    }


def last_step_name(odb):
    keys = list(odb.steps.keys())
    return keys[-1] if keys else None


def first_frame_with_field(odb, field_name):
    for step_name in reversed(list(odb.steps.keys())):
        step = odb.steps[step_name]
        for frame in reversed(step.frames):
            if field_name in frame.fieldOutputs:
                return step_name, frame
    return None, None


def node_set_values(odb, instance_name, set_name, field_name):
    step_name, frame = first_frame_with_field(odb, field_name)
    if frame is None:
        return []
    field = frame.fieldOutputs[field_name]
    region = None
    if set_name in odb.rootAssembly.nodeSets:
        region = odb.rootAssembly.nodeSets[set_name]
    elif instance_name in odb.rootAssembly.instances.keys():
        instance = odb.rootAssembly.instances[instance_name]
        if set_name in instance.nodeSets.keys():
            region = instance.nodeSets[set_name]
    if region is None:
        return []
    try:
        subset = field.getSubset(region=region)
    except Exception:
        return []
    return subset.values


def extract_reaction_resultants(odb, instance_name, set_name="BASE_FIXED"):
    result = {"schema_version": "reaction_resultants_v1", "steps": {}}
    instance = odb.rootAssembly.instances[instance_name]
    region = None
    if set_name in odb.rootAssembly.nodeSets:
        region = odb.rootAssembly.nodeSets[set_name]
    elif set_name in instance.nodeSets.keys():
        region = instance.nodeSets[set_name]
    coordinates = {}
    for node in instance.nodes:
        coordinates[int(node.label)] = [float(value) for value in node.coordinates]
    for step_name in odb.steps.keys():
        step = odb.steps[step_name]
        if not step.frames:
            continue
        frame = step.frames[-1]
        rf_available = "RF" in frame.fieldOutputs.keys() and region is not None
        rm_available = "RM" in frame.fieldOutputs.keys() and region is not None
        rf_values = []
        rm_values = []
        try:
            if rf_available:
                rf_values = frame.fieldOutputs["RF"].getSubset(region=region).values
            if rm_available:
                rm_values = frame.fieldOutputs["RM"].getSubset(region=region).values
        except Exception:
            rf_available = False
            rm_available = False
            rf_values = []
            rm_values = []
        force = [0.0, 0.0, 0.0]
        moment = [0.0, 0.0, 0.0]
        for value in rf_values:
            data = list(value.data)
            while len(data) < 3:
                data.append(0.0)
            label = int(value.nodeLabel)
            xyz = coordinates.get(label, [0.0, 0.0, 0.0])
            for index in range(3):
                force[index] += float(data[index])
            moment[0] += xyz[1] * data[2] - xyz[2] * data[1]
            moment[1] += xyz[2] * data[0] - xyz[0] * data[2]
            moment[2] += xyz[0] * data[1] - xyz[1] * data[0]
        for value in rm_values:
            data = list(value.data)
            while len(data) < 3:
                data.append(0.0)
            for index in range(3):
                moment[index] += float(data[index])
        result["steps"][str(step_name)] = {
            "force_n": force,
            "moment_nmm": moment,
            "rf_available": bool(rf_available),
            "rm_available": bool(rm_available),
            "rf_value_count": len(rf_values),
            "rm_value_count": len(rm_values),
            "set": set_name,
            "frame": len(step.frames) - 1,
            "frame_value": getattr(frame, "frameValue", None),
        }
    with open("reaction_resultants.json", "w") as handle:
        json.dump(result, handle, indent=2)
    return result


def all_node_displacements(odb, instance_name):
    step_name, frame = first_frame_with_field(odb, "U")
    if frame is None:
        return {}, {}
    field = frame.fieldOutputs["U"]
    instance = odb.rootAssembly.instances[instance_name]
    try:
        subset = field.getSubset(region=instance)
    except Exception:
        subset = field
    vectors = {}
    magnitudes = {}
    for value in subset.values:
        label = getattr(value, "nodeLabel", None)
        if label is None:
            continue
        data = list(getattr(value, "data", [0.0, 0.0, 0.0]))
        while len(data) < 3:
            data.append(0.0)
        vectors[str(label)] = [float(data[0]), float(data[1]), float(data[2])]
        magnitudes[str(label)] = vector_mag(data)
    return vectors, magnitudes


def vector_mag(data):
    try:
        return sum(float(x) * float(x) for x in data) ** 0.5
    except Exception:
        return 0.0


def extract_static(static_odb, instance_name):
    if not static_odb or not os.path.exists(static_odb):
        return {"status": "pending", "reason": "static ODB missing"}
    odb = openOdb(static_odb, readOnly=True)
    reaction_resultants = extract_reaction_resultants(odb, instance_name)
    step_name = last_step_name(odb)
    instance = odb.rootAssembly.instances[instance_name]
    door_rows = []
    for set_name in [
        "DOOR_NEIGHBOR_ELEMENTS", "DOOR_PATCH_ELEMENTS",
        "DOOR_REINFORCEMENT_ELEMENTS", "DOOR_REINFORCEMENT_ADJACENT_ELEMENTS",
        "DOOR_REINFORCEMENT_CHECK_ELEMENTS",
    ]:
        item = {"set_name": set_name}
        values = mises_values_for_set(odb, step_name, instance_name, set_name) if step_name else []
        item.update(stats(values))
        if item.get("max_element_label"):
            item["max_element_centroid"] = element_centroid(instance, item["max_element_label"])
        door_rows.append(item)
    write_csv("door_stress_summary.csv", door_rows, ["set_name", "element_count", "max_mises", "mean_mises", "p95_mises", "p99_mises", "max_element_label", "max_element_centroid"])

    flange_rows = []
    for set_name in sorted(instance.elementSets.keys()):
        if str(set_name).startswith("FLG_WIN_"):
            item = {"set_name": set_name}
            item.update(stats(mises_values_for_set(odb, step_name, instance_name, set_name) if step_name else []))
            flange_rows.append(item)
    write_csv("stress_windows.csv", flange_rows, ["set_name", "element_count", "max_mises", "mean_mises", "p95_mises", "p99_mises"])

    top_values = node_set_values(odb, instance_name, "TOP_LOAD_NODES", "U")
    top_u = [vector_mag(v.data) for v in top_values]
    base_rf = node_set_values(odb, instance_name, "BASE_FIXED", "RF")
    rf = [0.0, 0.0, 0.0]
    for value in base_rf:
        data = value.data
        for i in range(min(3, len(data))):
            rf[i] += float(data[i])
    all_stats = stats(mises_values_for_set(odb, step_name, instance_name, "ALL_ELEMENTS") if step_name else [])
    all_mises = dict((str(label), value) for label, value in (mises_values_for_set(odb, step_name, instance_name, "ALL_ELEMENTS") if step_name else []))
    u_vectors, u_magnitudes = all_node_displacements(odb, instance_name)
    with open("static_field_data.json", "w") as f:
        json.dump({
            "status": "pass",
            "source": static_odb,
            "step": step_name or "",
            "scalars": {
                "S_Mises_by_element": all_mises,
                "U_mag_by_node": u_magnitudes,
            },
            "vectors": {
                "U_by_node": u_vectors,
            },
        }, f, indent=2)
    overall = [{
        "step": step_name or "",
        "max_top_displacement": max(top_u) if top_u else "pending_odb_extraction",
        "all_elements_max_mises": all_stats.get("max_mises"),
        "all_elements_mean_mises": all_stats.get("mean_mises"),
        "all_elements_p95_mises": all_stats.get("p95_mises"),
        "all_elements_p99_mises": all_stats.get("p99_mises"),
        "base_rf1": rf[0],
        "base_rf2": rf[1],
        "base_rf3": rf[2],
        "base_reaction_magnitude": vector_mag(rf),
    }]
    write_csv("overall_static_summary.csv", overall, ["step", "max_top_displacement", "all_elements_max_mises", "all_elements_mean_mises", "all_elements_p95_mises", "all_elements_p99_mises", "base_rf1", "base_rf2", "base_rf3", "base_reaction_magnitude"])
    odb.close()
    return {"status": "pass", "source": static_odb, "overall": overall[0], "door": door_rows, "reaction_resultants": reaction_resultants}


def extract_modal(modal_odb, instance_name):
    if not modal_odb or not os.path.exists(modal_odb):
        write_csv("modal_frequencies.csv", [{"mode": "", "frequency_hz": "pending_odb_extraction", "odb": ""}])
        return {"status": "pending", "reason": "modal ODB missing"}
    odb = openOdb(modal_odb, readOnly=True)
    rows = []
    mode = 1
    for step_name in odb.steps.keys():
        step = odb.steps[step_name]
        for frame in step.frames:
            freq = getattr(frame, "frequency", None)
            if freq is None:
                desc = getattr(frame, "description", "")
                import re
                match = re.search(r"freq(?:uency)?\\s*=\\s*([-+0-9.Ee]+)", desc, re.I)
                freq = float(match.group(1)) if match else None
            if freq is not None and float(freq) > 0.0:
                rows.append({"mode": mode, "frequency_hz": float(freq), "step": step_name})
                mode += 1
    write_csv("modal_frequencies.csv", rows, ["mode", "frequency_hz", "step"])
    odb.close()
    return {"status": "pass" if rows else "pending", "mode_count": len(rows)}


def extract_buckling(buckling_odb):
    if not buckling_odb or not os.path.exists(buckling_odb):
        write_csv("buckling_factors.csv", [{"mode": "", "buckling_factor": "pending_odb_extraction", "odb": ""}])
        return {"status": "pending", "reason": "buckling ODB missing"}
    odb = openOdb(buckling_odb, readOnly=True)
    rows = []
    mode = 1
    for step_name in odb.steps.keys():
        step = odb.steps[step_name]
        for frame in step.frames:
            desc = getattr(frame, "description", "")
            factor = getattr(frame, "eigenValue", None)
            if factor is None:
                import re
                match = re.search(r"(?:eigenvalue|load proportionality factor|factor)\\s*=\\s*([-+0-9.Ee]+)", desc, re.I)
                factor = float(match.group(1)) if match else None
            if factor is not None:
                rows.append({"mode": mode, "buckling_factor": float(factor), "step": step_name})
                mode += 1
    write_csv("buckling_factors.csv", rows, ["mode", "buckling_factor", "step"])
    odb.close()
    return {"status": "pass" if rows else "pending", "mode_count": len(rows)}


def procedure_for_step(step_name, step):
    name = str(step_name).upper()
    procedure = str(getattr(step, "procedure", "")).upper()
    if "FREQUENCY" in name or "FREQUENCY" in procedure:
        return "frequency"
    if "BUCKL" in name or "BUCKL" in procedure:
        return "buckling"
    return "static_general"


def expected_field_variables(matrix, procedure):
    field = matrix.get(procedure, {}).get("field", {})
    variables = []
    for item in field.get("node", []) + field.get("element", []):
        variables.extend(item.get("variables", []))
    return sorted(set(str(value) for value in variables))


def field_variable_present(requested, actual_fields):
    if requested in actual_fields:
        return True
    if requested == "NFORC":
        return any(str(value).startswith("NFORC") for value in actual_fields)
    return False


def node_labels_for_set(odb, set_name):
    if set_name not in odb.rootAssembly.nodeSets.keys():
        return []
    labels = []
    pending = list(odb.rootAssembly.nodeSets[set_name].nodes)
    while pending:
        item = pending.pop()
        if hasattr(item, "label"):
            labels.append(int(item.label))
        else:
            try:
                pending.extend(list(item))
            except Exception:
                pass
    return sorted(set(labels))


def inspect_odb_outputs(odb_path, job_name, matrix):
    if not odb_path or not os.path.exists(odb_path):
        return {"status": "missing", "odb": odb_path or "", "steps": []}
    odb = openOdb(odb_path, readOnly=True)
    step_rows = []
    for step_name in odb.steps.keys():
        step = odb.steps[step_name]
        procedure = procedure_for_step(step_name, step)
        frame = step.frames[-1] if step.frames else None
        actual_fields = sorted(list(frame.fieldOutputs.keys())) if frame is not None else []
        value_counts = {}
        if frame is not None:
            for variable in actual_fields:
                try:
                    value_counts[str(variable)] = len(frame.fieldOutputs[variable].values)
                except Exception:
                    value_counts[str(variable)] = None
        history_regions = []
        all_history_variables = set()
        for region_key in step.historyRegions.keys():
            region = step.historyRegions[region_key]
            variables = sorted(list(region.historyOutputs.keys()))
            all_history_variables.update(str(value) for value in variables)
            history_regions.append({
                "key": str(region_key),
                "description": str(getattr(region, "description", "")),
                "variables": variables,
            })
        requested_fields = expected_field_variables(matrix, procedure)
        missing_fields = sorted(value for value in requested_fields if not field_variable_present(value, actual_fields))
        requested_history = []
        history = matrix.get(procedure, {}).get("history", {})
        for item in history.get("node", []) + history.get("energy", []):
            requested_history.extend(item.get("variables", []))
        if history.get("source"):
            requested_history.extend(history.get("variables", []))
        missing_history = sorted(set(str(value) for value in requested_history) - all_history_variables)
        history_region_checks = []
        for item in history.get("node", []):
            labels = node_labels_for_set(odb, str(item.get("region")))
            matched = []
            region_variables = set()
            for region_item in history_regions:
                key = region_item["key"]
                if any(key.endswith(".%s" % label) for label in labels):
                    matched.append(key)
                    region_variables.update(str(value) for value in region_item["variables"])
            expected_variables = [str(value) for value in item.get("variables", [])]
            history_region_checks.append({
                "requested_region": str(item.get("region")),
                "node_labels": labels,
                "matched_history_region_keys": matched,
                "actual_variables": sorted(region_variables),
                "missing_requested_variables": sorted(set(expected_variables) - region_variables),
            })
        nforc_keys = [value for value in actual_fields if str(value).startswith("NFORC")]
        nforc_value_count = sum(int(value_counts.get(value) or 0) for value in nforc_keys)
        step_rows.append({
            "step_name": str(step_name),
            "procedure": procedure,
            "frame_count": len(step.frames),
            "actual_field_variables": actual_fields,
            "field_value_counts": value_counts,
            "history_regions": history_regions,
            "actual_history_variables": sorted(all_history_variables),
            "requested_field_variables": requested_fields,
            "missing_requested_field_variables": missing_fields,
            "requested_history_variables": sorted(set(str(value) for value in requested_history)),
            "missing_requested_history_variables": missing_history,
            "requested_history_region_checks": history_region_checks,
            "nforc": {
                "requested": "NFORC" in requested_fields,
                "present": bool(nforc_keys),
                "odb_component_keys": nforc_keys,
                "value_count": nforc_value_count,
            },
            "solver_representation_notes": [
                "Abaqus stores shell NFORC as component field keys NFORC1 through NFORC6."
            ] if nforc_keys else [],
        })
    odb.close()
    missing_fields = sum(len(item["missing_requested_field_variables"]) for item in step_rows)
    return {
        "status": "pass" if step_rows and missing_fields == 0 else "warning",
        "job": job_name,
        "odb": odb_path,
        "steps": step_rows,
        "summary": {"step_count": len(step_rows), "missing_requested_field_count": missing_fields},
    }


def main():
    static_odb = os.environ.get("L0_STATIC_ODB")
    modal_odb = os.environ.get("L0_MODAL_ODB")
    buckling_odb = os.environ.get("L0_BUCKLING_ODB")
    enable_buckling = os.environ.get("L0_ENABLE_BUCKLING") == "1"
    try:
        expected_jobs = set(json.loads(os.environ.get("L0_EXPECTED_JOBS_JSON", "[\"static_strength\", \"modal\"]")))
    except Exception:
        expected_jobs = set(["static_strength", "modal"])
    instance = os.environ.get("L0_INSTANCE", "WINDTOWER_SHELL_L0-1")
    try:
        output_contract = json.loads(os.environ.get("L0_OUTPUT_CONTRACT_JSON", "{}"))
        output_matrix = json.loads(os.environ.get("L0_OUTPUT_MATRIX_JSON", "{}"))
    except Exception:
        output_contract = {}
        output_matrix = {}
    availability = {
        "schema_version": "abaqus_output_availability_v1",
        "status": "pass",
        "output_contract": output_contract,
        "requested_output_matrix": output_matrix,
        "jobs": {},
    }
    if "static_strength" in expected_jobs:
        availability["jobs"]["static_strength"] = inspect_odb_outputs(static_odb, "static_strength", output_matrix)
    if "modal" in expected_jobs:
        availability["jobs"]["modal"] = inspect_odb_outputs(modal_odb, "modal", output_matrix)
    if enable_buckling:
        availability["jobs"]["buckling_screening"] = inspect_odb_outputs(buckling_odb, "buckling_screening", output_matrix)
    if any(item.get("status") != "pass" for item in availability["jobs"].values()):
        availability["status"] = "warning"
    with open("output_availability_report.json", "w") as f:
        json.dump(availability, f, indent=2)
    summary = {
        "status": "pass",
        "source": "Abaqus ODB",
        "static": extract_static(static_odb, instance) if "static_strength" in expected_jobs else {"status": "not_requested"},
        "modal": extract_modal(modal_odb, instance) if "modal" in expected_jobs else {"status": "not_requested"},
        "stress_extraction": {"position": "integration_point", "shell_section_policy": "max_over_section_points", "averaging": False},
        "output_availability": availability,
    }
    status_items = []
    if "static_strength" in expected_jobs:
        status_items.append(summary["static"])
    if "modal" in expected_jobs:
        status_items.append(summary["modal"])
    if enable_buckling:
        summary["buckling"] = extract_buckling(buckling_odb)
        status_items.append(summary["buckling"])
    else:
        summary["buckling_screening"] = {"status": "disabled_scope_deferred"}
    if any(item.get("status") in ("pending", "blocked") for item in status_items):
        summary["status"] = "warning"
    if availability.get("status") != "pass":
        summary["status"] = "warning"
    with open("results_summary.json", "w") as f:
        json.dump(summary, f, indent=2)


if __name__ == "__main__":
    main()
'''


def results_l0(state: ProjectState) -> dict[str, Any]:
    summary_path = state.root / "results" / "results_summary.json"
    if not summary_path.exists():
        return {"status": "missing", "message": "Run L0 postprocess first.", "artifacts": {}}
    return json.loads(summary_path.read_text(encoding="utf-8"))


def enrich_l0_ir(state: ProjectState, ir: dict[str, Any] | None = None) -> dict[str, Any]:
    next_ir = deepcopy(ir or state.ir)
    next_ir = _prepare_parameter_consumption(next_ir)
    next_ir["unit_system"] = UNIT_SYSTEM
    next_ir["coordinate_system"] = {
        "origin": "tower_base_center",
        "z": "tower height direction",
        "r": "radial direction",
        "theta": "circumferential direction, degrees",
        "door_position": "theta_center_deg + bottom_z_mm + center_z_mm",
    }
    _ensure_l0_defaults(next_ir)
    next_ir["source_trace"] = build_source_trace(next_ir)
    next_ir["derived_geometry"] = build_derived_geometry(next_ir)
    next_ir["validation_report"] = validate_l0_ir(next_ir)
    next_ir["semantic_hash"] = (
        (next_ir.get("parameter_consumption") or {}).get("semantic_hash")
        or _full_sha256(next_ir.get("semantic_ir") or {})
    )
    next_ir["model_hash_sha256"] = build_model_hash_sha256(next_ir)
    next_ir["model_hash"] = next_ir["model_hash_sha256"][:12]
    update_ir_mesh_gate(next_ir)
    return next_ir


def _prepare_parameter_consumption(ir: dict[str, Any]) -> dict[str, Any]:
    raw_parameter_set = ir.get("parameter_set")
    if not isinstance(raw_parameter_set, dict):
        ir["parameter_consumption"] = legacy_consumption_report()
        return ir
    try:
        parameter_set = ParameterSet.model_validate(raw_parameter_set)
    except Exception as exc:
        ir["parameter_consumption"] = {
            **legacy_consumption_report(),
            "status": "blocked",
            "errors": [{
                "field": "parameter_set",
                "code": "PARAMETER_SET_INVALID",
                "message": str(exc),
            }],
            "warnings": [],
        }
        return ir

    validation = validate_parameter_set(parameter_set)
    definitions = parameter_definitions_from_set(parameter_set)
    conditional_required = {
        "geometry.door.width_mm", "geometry.door.height_mm",
        "geometry.door.bottom_z_mm", "geometry.door.theta_center_deg",
        "mesh.door_size_mm", "mesh.patch_margin_u_mm",
        "mesh.patch_margin_z_mm", "mesh.hole_layers",
        "analysis.modal_modes",
    }
    context = BuildContext(
        parameter_set.model_dump(mode="json"),
        known_keys=set(definitions),
        required_keys=(
            {key for key, definition in definitions.items() if definition.required}
            | conditional_required
        ),
    )
    consumer_contract = {
        "geometry": ("TowerGeometry", "geometry_normalization", ["semantic_ir", "derived_geometry"]),
        "material": ("MaterialSection", "material_normalization", ["material_section"]),
        "mesh": ("S4RMesh", "mesh_normalization", ["mesh", "sets"]),
        "loads": ("LoadCompiler", "load_normalization", ["load_contract"]),
        "analysis": ("AnalysisCompiler", "analysis_normalization", ["analysis_cases", "output_contract"]),
        "boundary": ("BoundaryCompiler", "boundary_normalization", ["base_boundary"]),
    }
    for key, definition in definitions.items():
        consumer, stage, artifacts = consumer_contract[definition.group]
        if key.startswith("geometry.door."):
            consumer, stage, artifacts = "DoorOpening", "door_normalization", ["door_geometry", "door_sets"]
        context.consume(key, consumer, stage, derived_artifacts=artifacts)
    report = context.finalize()
    if validation.get("status") != "pass":
        report["status"] = "blocked"
        report["errors"] = list(validation.get("issues") or []) + report["errors"]
    ir["parameter_consumption"] = report
    if report["status"] != "blocked":
        # The formal ParameterSet is the authority. Applying it changes only the
        # canonical IR views and never mutates or fills the ParameterSet itself.
        ir = apply_parameter_set_to_ir(parameter_set, ir)
        ir["parameter_consumption"] = report
    return ir


def _attach_consumption_artifact_hashes(ir: dict[str, Any]) -> None:
    report = ir.get("parameter_consumption") or {}
    parameters = report.get("parameters") or {}
    semantic = ir.get("semantic_ir") or {}
    logical_payloads = {
        "semantic_ir": semantic,
        "derived_geometry": ir.get("derived_geometry") or {},
        "door_geometry": (semantic.get("door_openings") or [{}])[0],
        "door_sets": (ir.get("mesh_and_set_ir") or {}).get("required_sets") or [],
        "material_section": semantic.get("materials") or [],
        "mesh": ir.get("mesh_and_set_ir") or {},
        "sets": (ir.get("mesh_and_set_ir") or {}).get("required_sets") or [],
        "load_contract": ir.get("load_contract") or {},
        "analysis_cases": ir.get("analysis_cases") or {},
        "output_contract": (ir.get("model_spec_ir") or {}).get("outputs") or {},
        "base_boundary": (ir.get("abaqus_assembly_ir") or {}).get("boundary_conditions") or [],
    }
    hashes = {name: _full_sha256(payload) for name, payload in logical_payloads.items()}
    for entry in parameters.values():
        entry["artifact_sha256"] = {
            name: hashes[name]
            for name in entry.get("derived_artifacts") or []
            if name in hashes
        }
    report["artifact_sha256"] = hashes


def _ensure_l0_defaults(ir: dict[str, Any]) -> None:
    decisions = ir.setdefault("modeling_decision_ir", {})
    decisions["level"] = "L0"
    decisions.setdefault("model_scope", "tower_shell_with_door")
    model_spec = ir.setdefault("model_spec_ir", {})
    fem_parameters = ir.setdefault("fem_parameters", {})
    assembly = ir.setdefault("abaqus_assembly_ir", {})
    raw_outputs = model_spec.get("outputs")
    if raw_outputs is None:
        raw_outputs = fem_parameters.get("outputs")
    if raw_outputs is None:
        raw_outputs = assembly.get("outputs")
    legacy_field = (
        ir.get("raw_excel_ir", {})
        .get("standard_input_package", {})
        .get("fem_settings", {})
        .get("outputs.field")
    )
    output_contract = normalize_output_contract(raw_outputs, legacy_field=legacy_field)
    model_spec["outputs"] = output_contract
    fem_parameters["outputs"] = output_contract
    assembly["outputs"] = output_contract
    ir["output_contract_audit"] = output_contract_audit(output_contract)
    if _uses_tower_shell_scope(ir):
        # Preserve Excel facts separately, but keep flange entities out of the active L0 model.
        ir.setdefault("semantic_ir", {})["flanges"] = []
        decisions["flange_strategy"] = "excluded_from_l0_scope"
    ir["semantic_ir"]["unit_system"] = "mm-N-MPa-tonne-s"
    material = ir["semantic_ir"]["materials"][0] if ir["semantic_ir"].get("materials") else {}
    provided = {
        "material_density": material.get("density") is not None or material.get("density_tonne_mm3") is not None,
    }
    material.setdefault("density", STEEL_DENSITY_TONNE_PER_MM3)
    door = ir["semantic_ir"]["door_openings"][0] if ir["semantic_ir"].get("door_openings") else {}
    door.setdefault("theta_center_deg", 0.0)
    mesh = ir.setdefault("mesh_and_set_ir", {})
    mesh["strategy"] = mesh.get("strategy") or CANONICAL_DEFAULTS["mesh.strategy"]
    mesh.setdefault("global_size_mm", CANONICAL_DEFAULTS["mesh.global_size_mm"])
    mesh["door_size_mm"] = float(mesh.get("door_size_mm") or CANONICAL_DEFAULTS["mesh.door_size_mm"])
    mesh.setdefault("hole_layers", CANONICAL_DEFAULTS["mesh.hole_layers"])
    mesh.setdefault("patch_margin_u_mm", CANONICAL_DEFAULTS["mesh.patch_margin_u_mm"])
    mesh.setdefault("patch_margin_z_mm", CANONICAL_DEFAULTS["mesh.patch_margin_z_mm"])
    mesh.setdefault(
        "mesh_quality",
        {
            "max_aspect_ratio": 10.0,
            "max_skew_angle": 70.0,
            "max_warpage_angle": 30.0,
            "min_jacobian": 0.05,
            "max_transition_ratio": 1.4,
        },
    )
    mesh["required_sets"] = ["BASE_FIXED", "TOP_LOAD_NODES"]
    if door.get("enabled"):
        mesh["required_sets"].extend(
            ["DOOR_OPENING_NODES", "DOOR_PATCH_ELEMENTS", "DOOR_NEIGHBOR_ELEMENTS"]
        )
    mesh["required_surfaces"] = ["WINDWARD_SURFACE"]
    loads = ir["semantic_ir"].setdefault("loads_inventory", {})
    provided.update(
        {
            "top_equivalent_mass": loads.get("top_mass_tonne") is not None,
            "side_wind_pressure": loads.get("wind_pressure_mpa") is not None,
            "top_horizontal_thrust": loads.get("top_horizontal_force_n") is not None
            or loads.get("top_total_force_n") is not None,
            "top_eccentricity": loads.get("center_of_mass_offset_mm") is not None,
        }
    )
    existing_static = ir.get("analysis_cases", {}).get("static_strength", {})
    existing_modal = ir.get("analysis_cases", {}).get("modal", {})
    existing_buckling = ir.get("analysis_cases", {}).get("buckling", {})
    top_force = float(loads.get("top_horizontal_force_n") or loads.get("top_total_force_n") or existing_static.get("top_horizontal_force_n") or existing_static.get("top_force_x_n") or DEFAULT_TOP_THRUST_X_N)
    top_direction = _normalize_top_direction(loads.get("top_force_direction") or existing_static.get("top_force_direction") or "+X")
    wind_pressure = float(loads.get("wind_pressure_mpa") or DEFAULT_WIND_PRESSURE_MPA)
    wind_direction = float(loads.get("wind_direction_deg") or 0.0)
    wind_vector = normalize_wind_direction(
        loads.get("wind_direction_vector"), wind_direction
    )
    load_cases = _ensure_code_load_ir(ir, loads, top_force, top_direction, wind_pressure, wind_direction)
    active_load_case = _normalize_load_case_id(str(ir.get("active_load_case") or ir.get("analysis_cases", {}).get("active_load_case") or "LC00_reference_excel"))
    active = load_cases.get(active_load_case) or load_cases["LC00_reference_excel"]
    active_top = active["top_load"]["components"]
    if any(loads.get(key) is not None for key in (
        "top_force_parallel_n", "top_force_transverse_n", "top_force_vertical_n",
        "top_moment_roll_nmm", "top_moment_pitch_nmm", "top_moment_yaw_nmm",
    )):
        active_top = rotate_local_top_loads(
            loads,
            wind_vector,
            mass_tonne=float(DEFAULT_TOP_MASS_TONNE if loads.get("top_mass_tonne") is None else loads.get("top_mass_tonne")),
            eccentricity_mm=float(loads.get("center_of_mass_offset_mm") or 0.0),
        )
        active["top_load"]["components"] = active_top
    active_wind = active["wind_load"]
    fx = float(active_top.get("Fx", 0.0) or 0.0)
    fy = float(active_top.get("Fy", 0.0) or 0.0)
    top_force = (fx * fx + fy * fy) ** 0.5
    wind_pressure = float(active_wind.get("fallback_pressure_mpa") or wind_pressure)
    loads["active_load_case"] = active["case_id"]
    loads["top_total_force_n"] = top_force
    loads["top_horizontal_force_n"] = top_force
    loads["top_force_direction"] = top_direction
    loads["top_force_fx_n"] = fx
    loads["top_force_fy_n"] = fy
    loads["top_force_fz_n"] = float(active_top.get("Fz", 0.0) or 0.0)
    loads["top_moment_mx_nmm"] = float(active_top.get("Mx", 0.0) or 0.0)
    loads["top_moment_my_nmm"] = float(active_top.get("My", 0.0) or 0.0)
    loads["top_moment_mz_nmm"] = float(active_top.get("Mz", 0.0) or 0.0)
    loads["wind_pressure_mpa"] = wind_pressure
    loads["wind_pressure_profile"] = active_wind.get("profile", [])
    loads["wind_direction_deg"] = wind_direction
    loads["wind_direction_vector"] = list(wind_vector)
    ir["top_equivalent_system"] = {
        "mass_tonne": float(DEFAULT_TOP_MASS_TONNE if loads.get("top_mass_tonne") is None else loads.get("top_mass_tonne")),
        "center_of_mass_offset_mm": float(loads.get("center_of_mass_offset_mm") or DEFAULT_RNA_ECCENTRICITY_MM),
        "rotary_inertia": {
            "Ixx": _optional_float(loads.get("top_rotary_inertia_Ixx_tonne_mm2")),
            "Iyy": _optional_float(loads.get("top_rotary_inertia_Iyy_tonne_mm2")),
            "Izz": _optional_float(loads.get("top_rotary_inertia_Izz_tonne_mm2")),
            "enabled": bool(loads.get("top_rotary_inertia_enabled")),
        },
        "coupling_type": "distributing",
        "coupled_region": "top_ring",
    }
    ir["wind_load"] = {
        "type": "equivalent_surface_pressure",
        "direction": list(wind_vector),
        "region": "windward_half",
        "distribution": "constant" if not loads.get("wind_pressure_profile") else "height_piecewise",
        "pressure_unit": "MPa",
        "pressure_mpa": wind_pressure,
        "pressure_profile": active_wind.get("profile", []),
        "active_load_case": active["case_id"],
        "wind_direction_deg": wind_direction,
        "wind_direction_vector": list(wind_vector),
    }
    requested_profile = ir.get("analysis_profile") or fem_parameters.get("analysis.profile")
    if requested_profile:
        profile = str(requested_profile)
    else:
        profile = "engineering_extended" if bool(existing_static.get("nlgeom") or existing_modal.get("prestress")) else "linear_verification"
    if profile not in {"linear_verification", "engineering_extended"}:
        profile = "linear_verification"
    static_nlgeom = bool(existing_static.get("nlgeom")) if "nlgeom" in existing_static else profile == "engineering_extended"
    modal_prestress = bool(existing_modal.get("prestress")) if "prestress" in existing_modal else profile == "engineering_extended"
    ir["analysis_profile"] = profile
    ir["analysis_cases"] = {
        "active_load_case": active["case_id"],
        "static_strength": {
            "nlgeom": static_nlgeom,
            "top_horizontal_force_n": top_force,
            "top_force_direction": top_direction,
            "top_force_fx_n": fx,
            "top_force_fy_n": fy,
            "top_force_fz_n": loads["top_force_fz_n"],
            "top_force_x_n": fx,
            "top_moment_mx_nmm": loads["top_moment_mx_nmm"],
            "top_moment_my_nmm": loads["top_moment_my_nmm"],
            "top_moment_mz_nmm": loads["top_moment_mz_nmm"],
            "wind_pressure_mpa": wind_pressure,
            "wind_pressure_profile": active_wind.get("profile", []),
            "load_case_id": active["case_id"],
            "wind_direction_vector": list(wind_vector),
        },
        "modal": {
            "modes": int(existing_modal.get("modes") or CANONICAL_DEFAULTS["analysis.modal_modes"]),
            "prestress": modal_prestress,
            "preload_case": active["case_id"] if modal_prestress else None,
            "load_case_independent": not modal_prestress,
        },
        "buckling_screening": {
            "enabled": _experimental_buckling_enabled(ir),
            "modes": int(existing_buckling.get("modes") or 10),
            "scope": "experimental_screening_only",
            "reference_load": "gravity + top loads + wind pressure",
        },
    }
    existing_frozen_resultants = (
        (ir.get("load_contract") or {}).get("frozen_resultants")
    )
    ir["load_contract"] = build_load_contract(ir, active, loads, provided=provided)
    if existing_frozen_resultants:
        ir["load_contract"]["frozen_resultants"] = existing_frozen_resultants
    ir["load_readiness"] = summarize_load_readiness(ir["load_contract"])
    ir["analysis_readiness"] = {
        "status": "blocked" if ir["load_readiness"]["missing_or_defaulted"] else "pass",
        "policy": "block_formal_generation_until_confirmed",
        "message": "Critical loads are source-traced and confirmed." if not ir["load_readiness"]["missing_or_defaulted"] else "Critical loads are defaulted or missing; only a watermarked draft preview is allowed.",
    }
    ir["modal_policy"] = {
        "default": "prestressed_modal" if modal_prestress else "no_prestress_modal",
        "comparison_available": "no_prestress_modal" if modal_prestress else "prestressed_modal",
        "mode_shape_note": "Abaqus modal deformations are display-scaled mode shapes, not physical displacement magnitudes.",
        "preload_case": active["case_id"],
    }


def build_load_contract(
    ir: dict[str, Any],
    active: dict[str, Any],
    loads: dict[str, Any],
    *,
    provided: dict[str, bool] | None = None,
) -> dict[str, Any]:
    provided = provided or {}
    material = (ir.get("semantic_ir", {}).get("materials") or [{}])[0]
    static = ir.get("analysis_cases", {}).get("static_strength", {})
    top = ir.get("top_equivalent_system", {})
    top_mass = float(top.get("mass_tonne") or DEFAULT_TOP_MASS_TONNE)
    gravity = float(loads.get("gravity_acceleration_mm_s2") or GRAVITY_MM_PER_S2)
    vertical = float(static.get("top_force_fz_n") or (-top_mass * gravity))
    eccentricity = float(top.get("center_of_mass_offset_mm") or DEFAULT_RNA_ECCENTRICITY_MM)
    moment_my = float(static.get("top_moment_my_nmm") or abs(vertical) * eccentricity)
    wind_profile = static.get("wind_pressure_profile") if isinstance(static.get("wind_pressure_profile"), list) else []

    def field(name: str, value: Any, unit: str, status: str, source: str, note: str = "") -> dict[str, Any]:
        return {"name": name, "value": value, "unit": unit, "status": status, "source": source, "note": note}

    source = "excel_or_standard_input"
    default_source = "engineering_default_requires_confirmation"
    top_mass_status = "confirmed" if provided.get("top_equivalent_mass", loads.get("top_mass_tonne") is not None) else "defaulted"
    wind_status = "confirmed" if provided.get("side_wind_pressure", loads.get("wind_pressure_mpa") is not None or bool(wind_profile)) else "defaulted"
    thrust_status = "confirmed" if provided.get("top_horizontal_thrust", loads.get("top_horizontal_force_n") is not None or loads.get("top_total_force_n") is not None) else "defaulted"
    eccentricity_status = "confirmed" if provided.get("top_eccentricity", loads.get("center_of_mass_offset_mm") is not None) else "defaulted"
    return {
        "unit_system": UNIT_SYSTEM,
        "active_load_case": active.get("case_id"),
        "policy": "block_formal_generation_until_confirmed",
        "required_components": [
            "tower_self_weight",
            "top_equivalent_mass",
            "top_vertical_weight",
            "side_wind_pressure",
            "top_horizontal_thrust",
            "top_eccentric_moment",
        ],
        "components": {
            "tower_self_weight": field(
                "tower_self_weight",
                gravity,
                "mm/s^2",
                "derived" if material.get("density") else "defaulted",
                "material.density + gravity",
                "Written as *Dload, GRAV.",
            ),
            "material_density": field("material_density", material.get("density") or STEEL_DENSITY_TONNE_PER_MM3, "tonne/mm^3", "confirmed" if provided.get("material_density", False) else "defaulted", source if provided.get("material_density", False) else default_source),
            "top_equivalent_mass": field("top_equivalent_mass", top_mass, "tonne", top_mass_status, source if top_mass_status == "confirmed" else default_source),
            "top_vertical_weight": field("top_vertical_weight", vertical, "N", "derived", "top_equivalent_mass * gravity", "Applied on TOP_RP_NSET DOF 3."),
            "side_wind_pressure": field("side_wind_pressure", float(static.get("wind_pressure_mpa") or DEFAULT_WIND_PRESSURE_MPA), "MPa", wind_status, source if wind_status == "confirmed" else default_source),
            "wind_pressure_profile": field("wind_pressure_profile", wind_profile, "MPa by height band", "confirmed" if wind_profile else "not_provided", source if wind_profile else "not_required_for_constant_pressure"),
            "top_horizontal_thrust": field(
                "top_horizontal_thrust",
                {
                    "magnitude_n": float(static.get("top_horizontal_force_n") or 0.0),
                    "fx_n": float(static.get("top_force_fx_n") or 0.0),
                    "fy_n": float(static.get("top_force_fy_n") or 0.0),
                    "direction": static.get("top_force_direction") or "+X",
                },
                "N",
                thrust_status,
                source if thrust_status == "confirmed" else default_source,
            ),
            "top_eccentricity": field("top_eccentricity", eccentricity, "mm", eccentricity_status, source if eccentricity_status == "confirmed" else default_source),
            "top_eccentric_moment": field("top_eccentric_moment", {"mx_nmm": float(static.get("top_moment_mx_nmm") or 0.0), "my_nmm": moment_my, "mz_nmm": float(static.get("top_moment_mz_nmm") or 0.0)}, "Nmm", "derived", "top_vertical_weight * eccentricity or source moments"),
        },
        "warnings": [],
    }


def summarize_load_readiness(contract: dict[str, Any]) -> dict[str, Any]:
    components = contract.get("components", {})
    missing_or_defaulted = [
        name
        for name, item in components.items()
        if item.get("status") in {"missing", "defaulted", "llm_candidate"}
        and name in set(contract.get("required_components", [])) | {"material_density", "top_eccentricity"}
    ]
    return {
        "status": "blocked" if missing_or_defaulted else "pass",
        "missing_or_defaulted": missing_or_defaulted,
        "requires_confirmation": missing_or_defaulted,
        "policy": contract.get("policy", "block_formal_generation_until_confirmed"),
    }


def _vadd(left: list[float], right: list[float]) -> list[float]:
    return [left[index] + right[index] for index in range(3)]


def _vcross(left: tuple[float, float, float] | list[float], right: list[float]) -> list[float]:
    return [
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    ]


def _quad_area_vector(
    nodes: dict[int, tuple[float, float, float]], conn: list[int]
) -> list[float]:
    p0, p1, p2, p3 = (nodes[nid] for nid in conn[:4])
    def tri(a: tuple[float, float, float], b: tuple[float, float, float], c: tuple[float, float, float]) -> list[float]:
        ab = [b[i] - a[i] for i in range(3)]
        ac = [c[i] - a[i] for i in range(3)]
        return [0.5 * value for value in _vcross(ab, ac)]
    return _vadd(tri(p0, p1, p2), tri(p0, p2, p3))


def freeze_static_load_resultants(
    ir: dict[str, Any],
    nodes: dict[int, tuple[float, float, float]],
    elements: dict[int, list[int]],
    segments: dict[int, int],
    tower: list[dict[str, Any]],
    sets: dict[str, Any],
) -> dict[str, Any]:
    """Integrate the actual discrete mesh loads before INP compilation."""
    material = (ir.get("semantic_ir", {}).get("materials") or [{}])[0]
    density = float(material.get("density") or material.get("density_tonne_mm3") or STEEL_DENSITY_TONNE_PER_MM3)
    gravity = float(
        (ir.get("load_contract", {}).get("components", {}).get("tower_self_weight", {}) or {}).get("value")
        or GRAVITY_MM_PER_S2
    )
    force_gravity = [0.0, 0.0, 0.0]
    moment_gravity = [0.0, 0.0, 0.0]
    total_mass = 0.0
    reinforcement_ids = set(sets.get("door_reinforcement_elements") or [])
    door = (ir.get("semantic_ir", {}).get("door_openings") or [{}])[0]
    reinforcement_thickness = float(door.get("frame_thickness_mm") or 0.0)
    for eid, conn in elements.items():
        area_vector = _quad_area_vector(nodes, conn)
        area = math.sqrt(sum(value * value for value in area_vector))
        segment_index = max(1, min(int(segments.get(eid, 1)), len(tower) - 1))
        thickness = (
            reinforcement_thickness
            if eid in reinforcement_ids and reinforcement_thickness > 0.0
            else float(tower[segment_index - 1].get("thickness_mm") or 0.0)
        )
        mass = density * thickness * area
        total_mass += mass
        force = [0.0, 0.0, -mass * gravity]
        center = element_center(nodes, conn)
        force_gravity = _vadd(force_gravity, force)
        moment_gravity = _vadd(moment_gravity, _vcross(center, force))

    static = ir.get("analysis_cases", {}).get("static_strength", {})
    top = ir.get("top_equivalent_system", {})
    vertical = float(static.get("top_force_fz_n") or (-float(top.get("mass_tonne") or DEFAULT_TOP_MASS_TONNE) * gravity))
    step1_force = _vadd(force_gravity, [0.0, 0.0, vertical])
    step1_moment = list(moment_gravity)

    wind_force = [0.0, 0.0, 0.0]
    wind_moment = [0.0, 0.0, 0.0]
    profile_by_element: dict[int, float] = {}
    for band in sets.get("wind_bands") or []:
        pressure = float(band.get("pressure_mpa") or 0.0)
        for eid in list(band.get("spos") or []) + list(band.get("sneg") or []):
            profile_by_element[int(eid)] = pressure
    default_pressure = float(static.get("wind_pressure_mpa") or DEFAULT_WIND_PRESSURE_MPA)
    for side, sign in (("wind_spos", -1.0), ("wind_sneg", 1.0)):
        for eid in sets.get(side) or []:
            area_vector = _quad_area_vector(nodes, elements[eid])
            pressure = profile_by_element.get(eid, default_pressure)
            force = [sign * pressure * value for value in area_vector]
            center = element_center(nodes, elements[eid])
            wind_force = _vadd(wind_force, force)
            wind_moment = _vadd(wind_moment, _vcross(center, force))

    top_force = [
        float(static.get("top_force_fx_n") or 0.0),
        float(static.get("top_force_fy_n") or 0.0),
        0.0,
    ]
    top_z = max(point[2] for point in nodes.values())
    top_force_moment = _vcross((0.0, 0.0, top_z), top_force)
    explicit_moment = [
        float(static.get("top_moment_mx_nmm") or 0.0),
        float(static.get("top_moment_my_nmm") or 0.0),
        float(static.get("top_moment_mz_nmm") or 0.0),
    ]
    step2_force = _vadd(_vadd(step1_force, wind_force), top_force)
    step2_moment = _vadd(_vadd(_vadd(step1_moment, wind_moment), top_force_moment), explicit_moment)
    provenance = {
        "integration": "discrete_S4R_area_centroid_v1",
        "element_count": len(elements),
        "shell_mass_tonne": total_mass,
        "density_tonne_mm3": density,
        "gravity_mm_s2": gravity,
    }
    return {
        "STEP-1-GRAVITY-TOPMASS": {
            "force_n": step1_force,
            "moment_nmm": step1_moment,
            **provenance,
        },
        "STEP-2-STATIC-WIND-THRUST-MOMENT": {
            "force_n": step2_force,
            "moment_nmm": step2_moment,
            "wind_force_n": wind_force,
            "wind_moment_nmm": wind_moment,
            **provenance,
        },
    }


def _ensure_code_load_ir(ir: dict[str, Any], loads: dict[str, Any], top_force: float, top_direction: str, wind_pressure: float, wind_direction: float) -> dict[str, Any]:
    top_mass = float(DEFAULT_TOP_MASS_TONNE if loads.get("top_mass_tonne") is None else loads.get("top_mass_tonne"))
    eccentricity = float(loads.get("center_of_mass_offset_mm") or DEFAULT_RNA_ECCENTRICITY_MM)
    tower_height = max((float(s.get("z_top_mm") or 0.0) for s in ir.get("semantic_ir", {}).get("tower_sections", [])), default=100000.0)
    source_trace = {
        "source": loads.get("source", "excel/default"),
        "priority": ["manufacturer_6_component_loads", "FEM_Excel_explicit_loads", "code_engineering_estimate"],
        "confidence": "engineering_default_requires_confirmation",
    }
    ir["load_basis_ir"] = {
        "standard_branch": "IEC 61400/DNV wind turbine DLC concept + simplified tower wind pressure expression",
        "unit_system": "mm-N-MPa-tonne-s",
        "basic_wind_speed_m_s": 30.0,
        "terrain_category": "II",
        "air_density_kg_m3": 1.25,
        "cscd": 1.0,
        "cf_cylinder": 0.70,
        "gamma_wind_uls": 1.35,
        "gamma_dead_load_uls": 1.10,
        "source_trace": source_trace,
        "notes": [
            "LC00 preserves the current reference load for comparison.",
            "LC01-LC07 are traceable engineering estimates for automated FEM-method development, not certification-grade aeroelastic DLC simulations.",
            "Manufacturer yaw-bearing 6-component loads should override engineering defaults when available.",
        ],
    }
    ir["load_case_selection_policy"] = {
        "default_set": "recommended_minimum",
        "static_load_cases": [
            "LC00_reference_excel",
            "LC01_DLC1_1_operating_ntm_characteristic",
            "LC02_DLC1_3_operating_etm_uls",
            "LC03_DLC6_1_parked_ewm50_yaw_pos",
            "LC04_DLC6_1_parked_ewm50_yaw_neg",
            "LC05_DLC6_2_parked_grid_loss_yaw_90",
            "LC06_DLC6_2_parked_grid_loss_yaw_180",
            "LC07_serviceability_characteristic",
        ],
        "modal_policy": "one modal job per model; modal is not repeated for every static load case by default.",
        "experimental_buckling_policy": "disabled by default; GMNIA/Riks/initial imperfections are deferred.",
    }
    fx0, fy0, fz0 = _top_force_components(top_force, top_direction)
    fx1, fy1, fz1 = _top_force_components(float(loads.get("code_top_horizontal_force_n") or DEFAULT_CODE_TOP_THRUST_X_N), top_direction)
    profile_lc1 = _code_wind_profile(tower_height, scale=1.0)
    profile_lc2 = _code_wind_profile(tower_height, scale=1.35)
    profile_extreme = _code_wind_profile(tower_height, scale=1.65)
    profile_service = _code_wind_profile(tower_height, scale=0.70)
    input_profile = loads.get("wind_pressure_profile")
    if not isinstance(input_profile, list):
        input_profile = []
    load_cases = {
        "LC00_reference_excel": _load_case(
            "LC00_reference_excel",
            "Current reference load retained for result comparison.",
            fx0,
            fy0,
            top_mass,
            eccentricity,
            wind_pressure,
            input_profile,
            1.0,
            source_trace,
            dlc="reference",
            design_situation="reference",
            limit_state="reference",
        ),
        "LC01_DLC1_1_operating_ntm_characteristic": _load_case(
            "LC01_DLC1_1_operating_ntm_characteristic",
            "DLC 1.1 normal power production / normal turbulence characteristic estimate.",
            fx1,
            fy1,
            top_mass,
            eccentricity,
            profile_lc1[-1]["pressure_mpa"] if profile_lc1 else wind_pressure,
            profile_lc1,
            1.0,
            source_trace,
            dlc="DLC1.1",
            design_situation="power_production",
            limit_state="characteristic",
        ),
        "LC02_DLC1_3_operating_etm_uls": _load_case(
            "LC02_DLC1_3_operating_etm_uls",
            "DLC 1.3 power production / extreme turbulence ULS estimate.",
            fx1 * 1.35,
            fy1 * 1.35,
            top_mass,
            eccentricity,
            profile_lc2[-1]["pressure_mpa"] if profile_lc2 else wind_pressure,
            profile_lc2,
            1.10,
            source_trace,
            dlc="DLC1.3",
            design_situation="power_production_extreme_turbulence",
            limit_state="ULS",
        ),
        "LC03_DLC6_1_parked_ewm50_yaw_pos": _load_case(
            "LC03_DLC6_1_parked_ewm50_yaw_pos",
            "DLC 6.1 parked 50-year extreme wind, positive yaw representative case.",
            abs(fx1) * 0.30,
            abs(fx1) * 0.15,
            top_mass,
            eccentricity,
            profile_extreme[-1]["pressure_mpa"],
            profile_extreme,
            1.10,
            source_trace,
            dlc="DLC6.1",
            design_situation="parked_extreme_wind_yaw_positive",
            limit_state="ULS",
            yaw_deg=15.0,
        ),
        "LC04_DLC6_1_parked_ewm50_yaw_neg": _load_case(
            "LC04_DLC6_1_parked_ewm50_yaw_neg",
            "DLC 6.1 parked 50-year extreme wind, negative yaw representative case.",
            abs(fx1) * 0.30,
            -abs(fx1) * 0.15,
            top_mass,
            eccentricity,
            profile_extreme[-1]["pressure_mpa"],
            profile_extreme,
            1.10,
            source_trace,
            dlc="DLC6.1",
            design_situation="parked_extreme_wind_yaw_negative",
            limit_state="ULS",
            yaw_deg=-15.0,
        ),
        "LC05_DLC6_2_parked_grid_loss_yaw_90": _load_case(
            "LC05_DLC6_2_parked_grid_loss_yaw_90",
            "DLC 6.2 parked extreme wind with grid loss, 90 degree yaw representative case.",
            0.0,
            abs(fx1) * 0.35,
            top_mass,
            eccentricity,
            profile_extreme[-1]["pressure_mpa"] * 1.05,
            _code_wind_profile(tower_height, scale=1.75),
            1.10,
            source_trace,
            dlc="DLC6.2",
            design_situation="parked_extreme_wind_grid_loss_yaw_90",
            limit_state="ULS",
            yaw_deg=90.0,
        ),
        "LC06_DLC6_2_parked_grid_loss_yaw_180": _load_case(
            "LC06_DLC6_2_parked_grid_loss_yaw_180",
            "DLC 6.2 parked extreme wind with grid loss, 180 degree yaw representative case.",
            -abs(fx1) * 0.35,
            0.0,
            top_mass,
            eccentricity,
            profile_extreme[-1]["pressure_mpa"] * 1.05,
            _code_wind_profile(tower_height, scale=1.75),
            1.10,
            source_trace,
            dlc="DLC6.2",
            design_situation="parked_extreme_wind_grid_loss_yaw_180",
            limit_state="ULS",
            yaw_deg=180.0,
        ),
        "LC07_serviceability_characteristic": _load_case(
            "LC07_serviceability_characteristic",
            "Serviceability characteristic load for displacement and deformation checks.",
            fx1 * 0.70,
            fy1 * 0.70,
            top_mass,
            eccentricity,
            profile_service[-1]["pressure_mpa"],
            profile_service,
            1.0,
            source_trace,
            dlc="serviceability",
            design_situation="serviceability_characteristic",
            limit_state="SLS",
        ),
    }
    # The canonical direction is a global Cartesian axis.  Preserve the
    # gravity component and add the explicit directional force when +Z/-Z is
    # selected; X/Y remain represented by Fx/Fy as before.
    if abs(fz0) > 0.0:
        load_cases["LC00_reference_excel"]["top_load"]["components"]["Fz"] += fz0
    if abs(fz1) > 0.0:
        for case_id in load_cases:
            if case_id != "LC00_reference_excel":
                load_cases[case_id]["top_load"]["components"]["Fz"] += fz1
    load_cases["LC0_current_reference"] = dict(load_cases["LC00_reference_excel"], case_id="LC0_current_reference", alias_for="LC00_reference_excel")
    load_cases["LC1_characteristic_code"] = dict(load_cases["LC01_DLC1_1_operating_ntm_characteristic"], case_id="LC1_characteristic_code", alias_for="LC01_DLC1_1_operating_ntm_characteristic")
    load_cases["LC2_uls_code"] = dict(load_cases["LC02_DLC1_3_operating_etm_uls"], case_id="LC2_uls_code", alias_for="LC02_DLC1_3_operating_etm_uls")
    ir["load_case_library"] = load_cases
    ir["load_cases_ir"] = load_cases
    return load_cases


def _load_case(
    case_id: str,
    description: str,
    fx: float,
    fy: float,
    mass_tonne: float,
    eccentricity_mm: float,
    fallback_pressure_mpa: float,
    profile: list[dict[str, Any]],
    gravity_factor: float,
    source_trace: dict[str, Any],
    dlc: str = "",
    design_situation: str = "",
    limit_state: str = "",
    yaw_deg: float | None = None,
) -> dict[str, Any]:
    fz = -mass_tonne * GRAVITY_MM_PER_S2 * gravity_factor
    my = abs(fz) * eccentricity_mm
    return {
        "case_id": case_id,
        "description": description,
        "dlc": dlc,
        "design_situation": design_situation,
        "limit_state": limit_state,
        "yaw_deg": yaw_deg,
        "included_in_recommended_minimum": case_id.startswith("LC0") or case_id.startswith("LC01") or case_id.startswith("LC02") or case_id.startswith("LC03") or case_id.startswith("LC04") or case_id.startswith("LC05") or case_id.startswith("LC06") or case_id.startswith("LC07"),
        "requires_confirmation": source_trace.get("confidence") == "engineering_default_requires_confirmation",
        "top_load": {
            "target": "TOP_RP_NSET",
            "components": {"Fx": fx, "Fy": fy, "Fz": fz, "Mx": 0.0, "My": my, "Mz": 0.0},
            "source_trace": source_trace,
            "calculation_trace": {
                "Fz": f"-{mass_tonne:g} tonne * 9810 mm/s^2 * {gravity_factor:g}",
                "My": f"abs(Fz) * {eccentricity_mm:g} mm",
                "Fx_Fy": "explicit Excel/reference value for LC0; engineering characteristic estimate for LC1; partial-factor scale for LC2",
            },
        },
        "wind_load": {
            "target": "WIND_BAND_*",
            "fallback_surface": "WINDWARD_SURFACE",
            "fallback_pressure_mpa": fallback_pressure_mpa,
            "profile": profile,
            "source_trace": source_trace,
            "calculation_trace": "p_band = cscd * cf * qp(z); LC2 multiplies wind action by gamma_wind_uls=1.35",
        },
    }


def _normalize_load_case_id(value: str) -> str:
    aliases = {
        "all": "LC00_reference_excel",
        "recommended_minimum": "LC00_reference_excel",
        "LC0_current_reference": "LC00_reference_excel",
        "LC1_characteristic_code": "LC01_DLC1_1_operating_ntm_characteristic",
        "LC2_uls_code": "LC02_DLC1_3_operating_etm_uls",
    }
    return aliases.get(value, value)


def normalize_wind_direction(value: Any, fallback_angle_deg: float = 0.0) -> tuple[float, float, float]:
    """Return a normalized horizontal vector pointing toward the windward side."""
    if isinstance(value, str):
        value = [item for item in re.split(r"[,;，、\s]+", value.strip()) if item]
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            x = float(value[0])
            y = float(value[1])
            z = float(value[2]) if len(value) > 2 else 0.0
        except (TypeError, ValueError):
            x = y = z = 0.0
        length = math.sqrt(x * x + y * y + z * z)
        if length > 1.0e-12 and abs(z) <= 1.0e-9 * length:
            horizontal = math.hypot(x, y)
            return x / horizontal, y / horizontal, 0.0
    angle = math.radians(float(fallback_angle_deg or 0.0))
    return math.cos(angle), math.sin(angle), 0.0


def rotate_local_top_loads(
    loads: dict[str, Any],
    wind_vector: tuple[float, float, float],
    *,
    mass_tonne: float,
    eccentricity_mm: float,
) -> dict[str, float]:
    """Rotate a wind-local six-component yaw-bearing load into global DOFs.

    ``wind_vector`` points toward the windward face, while positive parallel
    force acts downwind. Positive pitch acts in the same fore-aft bending
    sense as a downwind RNA centre-of-mass offset.
    """
    ex, ey, _ = normalize_wind_direction(wind_vector)
    downwind = (-ex, -ey)
    transverse = (-ey, ex)
    parallel_force = float(loads.get("top_force_parallel_n") or loads.get("top_total_force_n") or 0.0)
    transverse_force = float(loads.get("top_force_transverse_n") or 0.0)
    vertical_force = float(loads.get("top_force_vertical_n") or (-mass_tonne * GRAVITY_MM_PER_S2))
    roll = float(loads.get("top_moment_roll_nmm") or 0.0)
    pitch_external = float(loads.get("top_moment_pitch_nmm") or 0.0)
    pitch_total = pitch_external + abs(vertical_force) * eccentricity_mm
    yaw = float(loads.get("top_moment_yaw_nmm") or 0.0)
    return {
        "Fx": parallel_force * downwind[0] + transverse_force * transverse[0],
        "Fy": parallel_force * downwind[1] + transverse_force * transverse[1],
        "Fz": vertical_force,
        "Mx": roll * ex + pitch_total * ey,
        "My": roll * ey - pitch_total * ex,
        "Mz": yaw,
    }


def _code_wind_profile(tower_height_mm: float, scale: float = 1.0, bands: int = 5) -> list[dict[str, Any]]:
    tower_height_mm = max(float(tower_height_mm or 100000.0), 1000.0)
    qp_base = [0.00070, 0.00085, 0.00100, 0.00112, 0.00125]
    cf = 0.70
    profile = []
    for index in range(bands):
        z0 = tower_height_mm * index / bands
        z1 = tower_height_mm * (index + 1) / bands
        qp = qp_base[min(index, len(qp_base) - 1)]
        profile.append(
            {
                "band_id": f"WIND_BAND_{index + 1:02d}",
                "z_min_mm": z0,
                "z_max_mm": z1,
                "qp_mpa": qp,
                "cscd": 1.0,
                "cf": cf,
                "pressure_mpa": qp * cf * scale,
                "calculation": "pressure_mpa = qp_mpa * cscd * cf * load_factor",
            }
        )
    return profile


def build_source_trace(ir: dict[str, Any]) -> dict[str, Any]:
    sheet_map = ir.get("raw_excel_ir", {}).get("sheet_map", {})
    fem_sheet = sheet_map.get("fem_params") or sheet_map.get("fem") or "FEM模型参数"
    load_source = ir["semantic_ir"].get("loads_inventory", {}).get("source", "excel/default")
    return {
        "tower_sections": [{"section_id": s.get("section_id"), "source": s.get("source", "excel:tower"), "sheet": sheet_map.get("tower", "tower")} for s in ir["semantic_ir"].get("tower_sections", [])],
        "materials": [{"name": m.get("name"), "source": m.get("source", "excel"), "sheet": fem_sheet} for m in ir["semantic_ir"].get("materials", [])],
        "door_openings": [{"source": d.get("source", "excel:door"), "sheet": sheet_map.get("door", "door")} for d in ir["semantic_ir"].get("door_openings", [])],
        "loads": {
            "source": load_source,
            "sheet": fem_sheet,
            "top_total_force_n": {"source": load_source, "sheet": fem_sheet, "key": "loads.top_total_force_n"},
            "top_force_direction": {"source": load_source, "sheet": fem_sheet, "key": "loads.top_force_direction"},
            "wind_pressure_mpa": {"source": load_source, "sheet": fem_sheet, "key": "loads.wind_pressure_mpa"},
            "wind_surface": {"source": load_source, "sheet": fem_sheet, "key": "loads.wind_surface"},
            "boundary": {"source": load_source, "sheet": fem_sheet, "keys": ["boundary.name", "boundary.target", "boundary.dofs"]},
        },
    }


def build_derived_geometry(ir: dict[str, Any]) -> dict[str, Any]:
    sections = ir["semantic_ir"].get("tower_sections", [])
    derived_sections = []
    for section in sections:
        height = float(section["z_top_mm"]) - float(section["z_bottom_mm"])
        db = float(section["diameter_bottom_mm"])
        dt = float(section["diameter_top_mm"])
        derived_sections.append(
            {
                "section_id": section["section_id"],
                "height_mm": height,
                "mean_diameter_mm": 0.5 * (db + dt),
                "taper_mm_per_mm": (dt - db) / height if height else 0.0,
                "radius_bottom_mm": 0.5 * db,
                "radius_top_mm": 0.5 * dt,
            }
        )
    total_height = max((float(s["z_top_mm"]) for s in sections), default=0.0)
    door = ir["semantic_ir"].get("door_openings", [{}])[0]
    door_center = door.get("center_z_mm")
    door_section = None
    for section in sections:
        if door_center is not None and section["z_bottom_mm"] <= door_center <= section["z_top_mm"]:
            door_section = section["section_id"]
            break
    windows = []
    for flange in ir["semantic_ir"].get("flanges", []):
        z = float(flange.get("elevation_mm") or 0.0)
        diameter = float(flange.get("mean_diameter_mm") or 0.0)
        window_height = diameter
        windows.append(
            {
                "flange_id": flange.get("flange_id"),
                "z_flange": z,
                "mean_diameter_mm": diameter,
                "window_height": window_height,
                "z_min": max(0.0, z - window_height),
                "z_max": min(total_height, z + window_height),
            }
        )
    return {
        "tower_height_mm": total_height,
        "base_radius_mm": 0.5 * float(sections[0]["diameter_bottom_mm"]) if sections else 0.0,
        "top_radius_mm": 0.5 * float(sections[-1]["diameter_top_mm"]) if sections else 0.0,
        "sections": derived_sections,
        "door": {
            "center_z_mm": door_center,
            "bottom_z_mm": door.get("bottom_z_mm"),
            "theta_center_deg": door.get("theta_center_deg", 0.0),
            "aspect_ratio": (float(door.get("height_mm") or 0.0) / float(door.get("width_mm") or 1.0)),
            "section_id": door_section,
        },
        "flange_elevation_stress_windows": windows,
    }


def validate_l0_ir(ir: dict[str, Any]) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    assumptions: list[str] = []
    auto_fixed: list[str] = []
    consumption = ir.get("parameter_consumption") or {}
    errors.extend(consumption.get("errors") or [])
    warnings.extend(consumption.get("warnings") or [])
    sections = ir["semantic_ir"].get("tower_sections", [])
    material = ir["semantic_ir"].get("materials", [{}])[0]
    door = ir["semantic_ir"].get("door_openings", [{}])[0]
    if not sections:
        errors.append({"field": "semantic_ir.tower_sections", "message": "Tower sections are required."})
    if not material.get("elastic_modulus_mpa") or material.get("poisson_ratio") is None:
        errors.append({"field": "semantic_ir.materials", "message": "Elastic modulus and Poisson ratio are required."})
    if not door.get("enabled"):
        warnings.append({"field": "semantic_ir.door_openings", "message": "Door is missing; L0 can build a no-door draft but door stress extraction will be pending."})
    elif not all(door.get(key) for key in ("width_mm", "height_mm", "bottom_z_mm")):
        errors.append({"field": "semantic_ir.door_openings", "code": "DOOR_DIMENSION_INVALID", "message": "Door width, height and bottom elevation are required."})
    elif sections:
        center_z = float(door.get("center_z_mm") or (float(door.get("bottom_z_mm") or 0.0) + 0.5 * float(door.get("height_mm") or 0.0)))
        radius = _radius_at_z(sections, center_z)
        door_errors = validate_door_input(
            door,
            ir.get("mesh_and_set_ir", {}),
            float(sections[-1].get("z_top_mm") or 0.0),
            2.0 * math.pi * radius if radius else None,
        )
        errors.extend(door_errors)
    rotary = ir["top_equivalent_system"].get("rotary_inertia", {})
    if rotary.get("enabled") and not all(rotary.get(key) is not None for key in ("Ixx", "Iyy", "Izz")):
        warnings.append({"field": "top_equivalent_system.rotary_inertia", "message": "Rotary inertia is enabled but one or more components are missing."})
    static = ir.get("analysis_cases", {}).get("static_strength", {})
    if float(static.get("top_horizontal_force_n") or 0.0) < 0:
        errors.append({"field": "analysis_cases.static_strength.top_horizontal_force_n", "message": "Top horizontal force magnitude must be non-negative."})
    if float(static.get("wind_pressure_mpa") or 0.0) < 0:
        errors.append({"field": "analysis_cases.static_strength.wind_pressure_mpa", "message": "Wind pressure must be non-negative."})
    load_readiness = ir.get("load_readiness", {})
    formal_confirmation_contract = any(
        isinstance(item, dict) and ({"confirmation_status", "requires_confirmation"} & set(item))
        for item in (ir.get("parameter_evidence_map") or {}).values()
    )
    for field_name in load_readiness.get("missing_or_defaulted", []):
        issue = {
            "field": f"load_contract.{field_name}",
            "code": "FORMAL_PARAMETER_UNCONFIRMED",
            "message": f"Critical load component '{field_name}' is missing or defaulted; formal mesh/INP generation is blocked until engineer confirmation.",
        }
        if formal_confirmation_contract:
            errors.append(issue)
        else:
            issue["message"] += " Legacy direct-L0 input is retained as a warning for backward compatibility."
            warnings.append(issue)
    output_contract = ir.get("model_spec_ir", {}).get("outputs", {})
    errors.extend(validate_output_contract(output_contract))
    if output_contract.get("profile") == "global_to_local":
        requested_region = str(output_contract.get("nforc_region") or "")
        local_region = requested_region.rsplit(".", 1)[-1]
        available_regions = {"ALL_ELEMENTS", "DOOR_PATCH_ELEMENTS", "DOOR_NEIGHBOR_ELEMENTS"}
        available_regions.update(f"SEG_{index:03d}" for index in range(1, len(sections) + 1))
        available_regions.update(
            f"FLG_WIN_{str(item.get('flange_id', '')).split('_')[-1]}"
            for item in ir.get("derived_geometry", {}).get("flange_elevation_stress_windows", [])
        )
        if local_region not in available_regions:
            errors.append({
                "field": "model_spec_ir.outputs.nforc_region",
                "code": "OUTPUT_REGION_NOT_FOUND",
                "message": f"NFORC ELSET '{requested_region}' is not generated by the active L0 mesh contract.",
            })
    assumptions.append("L0 uses S4R shell elements and homogeneous Shell Section by tower segment.")
    if ir.get("analysis_cases", {}).get("modal", {}).get("prestress"):
        assumptions.append("Engineering-extended configuration uses a prestressed modal step.")
    else:
        assumptions.append("Linear-verification configuration uses a non-prestressed modal step.")
    assumptions.append("Flange elevation stress windows are shell monitoring windows, not true flange connection stress.")
    return {"errors": errors, "warnings": warnings, "assumptions": assumptions, "auto_fixed_items": auto_fixed, "blocked": bool(errors)}


def _full_sha256(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_model_hash_sha256(ir: dict[str, Any]) -> str:
    payload = {
        "capability_id": CAPABILITY_ID,
        "capability_revision": CAPABILITY_REVISION,
        "capability_hash_sha256": CAPABILITY_HASH_SHA256,
        "compiler_version": COMPILER_VERSION,
        "unit_system": ir.get("unit_system"),
        "semantic_hash": ir.get("semantic_hash"),
        "mesh_and_set_ir": ir.get("mesh_and_set_ir"),
        "top_equivalent_system": ir.get("top_equivalent_system"),
        "wind_load": ir.get("wind_load"),
        "analysis_cases": ir.get("analysis_cases"),
        "load_contract": ir.get("load_contract"),
        "output_contract": ir.get("model_spec_ir", {}).get("outputs"),
    }
    return _full_sha256(payload)


def build_model_hash(ir: dict[str, Any]) -> str:
    """Backward-compatible short alias for external callers."""
    return build_model_hash_sha256(ir)[:12]


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _top_force_components(magnitude: float, direction: str) -> tuple[float, float, float]:
    normalized = _normalize_top_direction(direction)
    if normalized == "-X":
        return -abs(magnitude), 0.0, 0.0
    if normalized == "+Y":
        return 0.0, abs(magnitude), 0.0
    if normalized == "-Y":
        return 0.0, -abs(magnitude), 0.0
    if normalized == "+Z":
        return 0.0, 0.0, abs(magnitude)
    if normalized == "-Z":
        return 0.0, 0.0, -abs(magnitude)
    return abs(magnitude), 0.0, 0.0


def _normalize_top_direction(direction: Any) -> str:
    text = str(direction).strip().upper()
    if text in {"1", "DOF1", "U1", "X", "+X"}:
        return "+X"
    if text in {"-1", "-X"}:
        return "-X"
    if text in {"2", "DOF2", "U2", "Y", "+Y"}:
        return "+Y"
    if text in {"-2", "-Y"}:
        return "-Y"
    if text in {"3", "DOF3", "U3", "Z", "+Z"}:
        return "+Z"
    if text in {"-3", "-Z"}:
        return "-Z"
    return "+X"


def build_l0_mesh_report(ir: dict[str, Any]) -> dict[str, Any]:
    tower, flanges, door = geometry_from_ir(ir)
    mesh_cfg = ir["mesh_and_set_ir"]
    nodes, elements, door_nodes = build_preview_mesh(tower, door, mesh_cfg)
    flange_ranges = _active_flange_ranges(tower, flanges)
    sets = build_l0_sets(nodes, elements, door_nodes, flange_ranges, ir)
    quality = compute_mesh_quality(nodes, elements, sets, ir)
    wind_spos, wind_sneg = windward_surface_groups(nodes, elements)
    set_stats = [
        stat("BASE_FIXED", "NSET", len(sets["base_nodes"])),
        stat("TOP_LOAD_NODES", "NSET", len(sets["top_nodes"])),
        stat("DOOR_OPENING_NODES", "NSET", len(sets["door_nodes"])),
        stat("DOOR_PATCH_ELEMENTS", "ELSET", len(sets["door_patch_elements"])),
        stat("DOOR_NEIGHBOR_ELEMENTS", "ELSET", len(sets["door_neighbor_elements"])),
        stat("WINDWARD_SPOS_ELEMENTS", "ELSET", len(wind_spos)),
        stat("WINDWARD_SNEG_ELEMENTS", "ELSET", len(wind_sneg)),
        stat("WINDWARD_SURFACE", "SURFACE", len(set(wind_spos) | set(wind_sneg))),
    ]
    for name, values in sets["flange_window_elements"].items():
        set_stats.append(stat(name, "ELSET", len(values)))
    update_ir_mesh_gate(ir, quality)
    return {
        "status": "blocked" if quality["blocked"] else ("warning" if quality["warnings"] else "pass"),
        "model_hash": ir["model_hash"],
        "mesh_stats": {
            "nodes": len(nodes),
            "s4r_elements": len(elements),
            "door_boundary_nodes": len(sets["door_nodes"]),
            "door_patch_elements": len(sets["door_patch_elements"]),
            "door_neighbor_elements": len(sets["door_neighbor_elements"]),
            "windward_spos_elements": len(wind_spos),
            "windward_sneg_elements": len(wind_sneg),
            "global_size_mm": mesh_cfg["global_size_mm"],
            "door_size_mm": mesh_cfg["door_size_mm"],
            "flange_size_mm": mesh_cfg["flange_size_mm"],
            "element_family": "S4R shell",
        },
        "set_stats": set_stats,
        "quality_report": quality,
        "mesh_automation_gate": ir.get("mesh_automation_gate"),
        "mesh_handoff_contract": ir.get("mesh_handoff_contract"),
        "warnings": [item["message"] for item in quality["warnings"]],
    }


def _blocked_mesh_report(ir: dict[str, Any]) -> dict[str, Any]:
    update_ir_mesh_gate(ir, {"blocked": True, "errors": ir.get("validation_report", {}).get("errors", []), "warnings": ir.get("validation_report", {}).get("warnings", []), "metrics": {}})
    return {
        "status": "blocked",
        "model_hash": ir.get("model_hash"),
        "mesh_stats": {
            "nodes": 0,
            "s4r_elements": 0,
            "door_boundary_nodes": 0,
            "door_patch_elements": 0,
            "door_neighbor_elements": 0,
            "windward_spos_elements": 0,
            "windward_sneg_elements": 0,
            "global_size_mm": ir.get("mesh_and_set_ir", {}).get("global_size_mm"),
            "door_size_mm": ir.get("mesh_and_set_ir", {}).get("door_size_mm"),
            "flange_size_mm": ir.get("mesh_and_set_ir", {}).get("flange_size_mm"),
            "element_family": "S4R shell",
        },
        "set_stats": [],
        "quality_report": {
            "status": "blocked",
            "blocked": True,
            "errors": ir.get("validation_report", {}).get("errors", []),
            "warnings": ir.get("validation_report", {}).get("warnings", []),
            "metrics": {},
        },
        "mesh_automation_gate": ir.get("mesh_automation_gate"),
        "mesh_handoff_contract": ir.get("mesh_handoff_contract"),
        "warnings": [item.get("message", str(item)) for item in ir.get("validation_report", {}).get("warnings", [])],
    }


def build_l0_sets(nodes: dict[int, tuple[float, float, float]], elements: dict[int, list[int]], door_nodes: set[int], flange_ranges: list[Any], ir: dict[str, Any]) -> dict[str, Any]:
    min_z = min(xyz[2] for xyz in nodes.values())
    max_z = max(xyz[2] for xyz in nodes.values())
    base_nodes = [nid for nid, xyz in nodes.items() if abs(xyz[2] - min_z) < 1.0e-6]
    top_nodes = [nid for nid, xyz in nodes.items() if abs(xyz[2] - max_z) < 1.0e-6]
    door = ir["semantic_ir"].get("door_openings", [{}])[0]
    if door.get("enabled"):
        z0 = float(door.get("bottom_z_mm") or 0.0) - float(ir["mesh_and_set_ir"].get("patch_margin_z_mm", CANONICAL_DEFAULTS["mesh.patch_margin_z_mm"]))
        z1 = float(door.get("bottom_z_mm") or 0.0) + float(door.get("height_mm") or 0.0) + float(ir["mesh_and_set_ir"].get("patch_margin_z_mm", CANONICAL_DEFAULTS["mesh.patch_margin_z_mm"]))
        door_patch = [eid for eid, conn in elements.items() if z0 <= element_center(nodes, conn)[2] <= z1]
    else:
        door_patch = []
    node_to_eids: dict[int, set[int]] = {}
    for eid, conn in elements.items():
        for nid in conn:
            node_to_eids.setdefault(nid, set()).add(eid)
    door_neighbor = set(door_patch)
    for nid in door_nodes:
        door_neighbor.update(node_to_eids.get(nid, set()))
    flange_window_elements = {}
    if not _uses_tower_shell_scope(ir):
        for window in ir.get("derived_geometry", {}).get("flange_elevation_stress_windows", []):
            name = f"FLG_WIN_{str(window['flange_id']).split('_')[-1]}"
            flange_window_elements[name] = [
                eid
                for eid, conn in elements.items()
                if float(window["z_min"]) <= element_center(nodes, conn)[2] <= float(window["z_max"])
            ]
    door_input = normalize_door(door)
    reinforcement_elements = door_reinforcement_elements(nodes, elements, door_input)
    reinforcement_nodes = {
        nid for eid in reinforcement_elements for nid in elements.get(eid, [])
    }
    reinforcement_set = set(reinforcement_elements)
    reinforcement_adjacent = {
        eid
        for nid in reinforcement_nodes
        for eid in node_to_eids.get(nid, set())
        if eid not in reinforcement_set
    }
    wind_vector = normalize_wind_direction(
        ir.get("analysis_cases", {}).get("static_strength", {}).get("wind_direction_vector"),
        ir.get("semantic_ir", {}).get("loads_inventory", {}).get("wind_direction_deg", 0.0),
    )
    wind_spos, wind_sneg = windward_surface_groups(nodes, elements, wind_vector)
    wind_bands = _wind_band_surface_groups(nodes, elements, ir)
    return {
        "base_nodes": base_nodes,
        "top_nodes": top_nodes,
        "door_nodes": sorted(door_nodes),
        "door_patch_elements": sorted(set(door_patch)),
        "door_neighbor_elements": sorted(door_neighbor),
        "door_reinforcement_elements": sorted(reinforcement_elements),
        "door_reinforcement_adjacent_elements": sorted(reinforcement_adjacent),
        "door_reinforcement_check_elements": sorted(reinforcement_set | reinforcement_adjacent),
        "flange_window_elements": flange_window_elements,
        "wind_spos": wind_spos,
        "wind_sneg": wind_sneg,
        "wind_bands": wind_bands,
    }


def _wind_band_surface_groups(nodes: dict[int, tuple[float, float, float]], elements: dict[int, list[int]], ir: dict[str, Any]) -> list[dict[str, Any]]:
    profile = ir.get("analysis_cases", {}).get("static_strength", {}).get("wind_pressure_profile", [])
    if not isinstance(profile, list) or not profile:
        return []
    wind_vector = normalize_wind_direction(
        ir.get("analysis_cases", {}).get("static_strength", {}).get("wind_direction_vector"),
        ir.get("semantic_ir", {}).get("loads_inventory", {}).get("wind_direction_deg", 0.0),
    )
    wind_spos, wind_sneg = windward_surface_groups(nodes, elements, wind_vector)
    spos_set = set(wind_spos)
    sneg_set = set(wind_sneg)
    bands: list[dict[str, Any]] = []
    for index, item in enumerate(profile, start=1):
        try:
            z0 = float(item.get("z_min_mm"))
            z1 = float(item.get("z_max_mm"))
            pressure = float(item.get("pressure_mpa"))
        except Exception:
            continue
        name = abaqus_name(item.get("band_id") or f"WIND_BAND_{index:02d}", f"WIND_BAND_{index:02d}")
        eids = [eid for eid, conn in elements.items() if z0 <= element_center(nodes, conn)[2] <= z1]
        spos = sorted(eid for eid in eids if eid in spos_set)
        sneg = sorted(eid for eid in eids if eid in sneg_set)
        if not spos and not sneg:
            continue
        bands.append({"name": name, "z_min_mm": z0, "z_max_mm": z1, "pressure_mpa": pressure, "spos": spos, "sneg": sneg})
    return bands


def compute_mesh_quality(nodes: dict[int, tuple[float, float, float]], elements: dict[int, list[int]], sets: dict[str, Any], ir: dict[str, Any]) -> dict[str, Any]:
    limits = ir["mesh_and_set_ir"].get("mesh_quality", {})
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    aspect_ratios = []
    min_area = math.inf
    for conn in elements.values():
        lengths = [_distance(nodes[a], nodes[b]) for a, b in zip(conn, conn[1:] + conn[:1])]
        if min(lengths) <= 1.0e-9:
            min_area = 0.0
            continue
        aspect_ratios.append(max(lengths) / min(lengths))
        min_area = min(min_area, _quad_area(nodes, conn))
    max_aspect = max(aspect_ratios) if aspect_ratios else 0.0
    aspect_threshold = float(limits.get("max_aspect_ratio", 8.0))
    high_aspect_count = sum(1 for value in aspect_ratios if value > aspect_threshold)
    aspect_sorted = sorted(aspect_ratios)
    aspect_p95 = aspect_sorted[min(len(aspect_sorted) - 1, int(round((len(aspect_sorted) - 1) * 0.95)))] if aspect_sorted else 0.0
    door_enabled = bool(ir["semantic_ir"].get("door_openings", [{}])[0].get("enabled"))
    if door_enabled and sets["door_nodes"] and len(sets["door_nodes"]) < 8:
        errors.append({"field": "DOOR_OPENING_NODES", "code": "DOOR_BOUNDARY_NOT_CLOSED", "message": "Door boundary has too few nodes to form a stable closed curve."})
    if door_enabled and not sets["door_neighbor_elements"]:
        errors.append({"field": "DOOR_NEIGHBOR_ELEMENTS", "code": "MESH_SET_GENERATION_FAILED", "message": "Door neighbor element set is empty."})
    if not sets.get("base_nodes"):
        errors.append({"field": "BASE_FIXED", "code": "MESH_SET_GENERATION_FAILED", "message": "Base node set is empty."})
    if not sets.get("top_nodes"):
        errors.append({"field": "TOP_LOAD_NODES", "code": "MESH_SET_GENERATION_FAILED", "message": "Top load node set is empty."})
    if not (sets.get("wind_spos") or sets.get("wind_sneg")):
        errors.append({"field": "WINDWARD_SURFACE", "code": "MESH_SET_GENERATION_FAILED", "message": "Windward surface element set is empty."})
    if min_area <= 1.0e-8:
        errors.append({"field": "mesh.elements", "code": "MESH_QUALITY_BELOW_THRESHOLD", "message": "Degenerated shell elements were detected."})
    if max_aspect > aspect_threshold:
        warnings.append({
            "field": "mesh_quality.max_aspect_ratio",
            "message": f"Maximum aspect ratio {max_aspect:.2f} exceeds threshold {aspect_threshold:.2f}; {high_aspect_count} elements are highlighted in the PyVista preview.",
        })
    door = ir["semantic_ir"].get("door_openings", [{}])[0]
    if door.get("enabled"):
        center_z = float(door.get("center_z_mm") or 0.0)
        radius = _radius_at_z(ir["semantic_ir"].get("tower_sections", []), center_z)
        if float(door.get("width_mm") or 0.0) > 2.0 * radius:
            errors.append({"field": "door.width_mm", "code": "DOOR_POSITION_INVALID", "message": "Door width is larger than local tower diameter."})
    door_input = normalize_door(
        {
            "width_mm": door.get("width_mm"),
            "height_mm": door.get("height_mm"),
            "bottom_z_mm": door.get("bottom_z_mm"),
            "shape": door.get("shape", "ellipse"),
            "theta_center_deg": door.get("theta_center_deg", 0.0),
        }
    ) if door.get("enabled") else None
    upper_sets = {
        "DOOR_OPENING_NODES": sets.get("door_nodes", []),
        "DOOR_PATCH_ELEMENTS": sets.get("door_patch_elements", []),
        "DOOR_NEIGHBOR_ELEMENTS": sets.get("door_neighbor_elements", []),
    }
    door_check = validate_door_mesh_topology(nodes, elements, set(sets.get("door_nodes", [])), door_input, normalize_controls(ir.get("mesh_and_set_ir", {})), upper_sets)
    errors.extend(door_check["errors"])
    warnings.extend(door_check["warnings"])
    geometric = _quad_mesh_geometric_quality(nodes, elements, sets)
    skew_limit = float(limits.get("max_skew_angle", 70.0))
    warpage_limit = float(limits.get("max_warpage_angle", 30.0))
    jacobian_limit = float(limits.get("min_jacobian", 0.05))
    if geometric["normal_inconsistency_count"]:
        warnings.append({"field": "mesh.normals", "code": "SHELL_NORMAL_INCONSISTENT", "message": f"{geometric['normal_inconsistency_count']} shared edges have inconsistent shell orientation and require review before a no-warning claim."})
    if geometric["non_manifold_edge_count"]:
        errors.append({"field": "mesh.topology", "code": "NON_MANIFOLD_EDGE", "message": f"{geometric['non_manifold_edge_count']} non-manifold edges were detected."})
    if geometric["unexpected_free_edge_count"]:
        errors.append({"field": "mesh.topology", "code": "UNEXPECTED_FREE_EDGE", "message": f"{geometric['unexpected_free_edge_count']} free edges are outside the base, top and door boundaries."})
    if geometric["max_skew_angle_deg"] > skew_limit:
        warnings.append({"field": "mesh_quality.max_skew_angle", "message": f"Maximum angle skew {geometric['max_skew_angle_deg']:.2f}° exceeds {skew_limit:.2f}°."})
    if geometric["max_warpage_deg"] > warpage_limit:
        warnings.append({"field": "mesh_quality.max_warpage_angle", "message": f"Maximum warpage {geometric['max_warpage_deg']:.2f}° exceeds {warpage_limit:.2f}°."})
    if geometric["min_scaled_jacobian"] < jacobian_limit:
        errors.append({"field": "mesh_quality.min_scaled_jacobian", "code": "MESH_QUALITY_BELOW_THRESHOLD", "message": f"Minimum scaled Jacobian {geometric['min_scaled_jacobian']:.4f} is below {jacobian_limit:.4f}."})
    metrics = {
        "all_quadrilateral": all(len(conn) == 4 for conn in elements.values()),
        "aspect_ratio_threshold": aspect_threshold,
        "max_aspect_ratio": max_aspect,
        "aspect_ratio_p95": aspect_p95,
        "aspect_ratio_max": max_aspect,
        "high_aspect_ratio_count": high_aspect_count,
        "min_projected_area": min_area,
        "degenerate_element_count": sum(1 for conn in elements.values() if len(conn) == 4 and _quad_area(nodes, conn) <= 1.0e-8),
        "inverted_element_count": geometric["inverted_element_count"],
        "door_boundary_closed": bool(sets["door_nodes"]),
        "required_sets_present": {
            "BASE_FIXED": bool(sets.get("base_nodes")),
            "TOP_LOAD_NODES": bool(sets.get("top_nodes")),
            "DOOR_OPENING_NODES": bool(sets.get("door_nodes")) if door.get("enabled") else True,
            "DOOR_PATCH_ELEMENTS": bool(sets.get("door_patch_elements")) if door.get("enabled") else True,
            "DOOR_NEIGHBOR_ELEMENTS": bool(sets.get("door_neighbor_elements")) if door.get("enabled") else True,
        },
        "required_surfaces_present": {
            "WINDWARD_SURFACE": bool(sets.get("wind_spos") or sets.get("wind_sneg")),
        },
        "shell_normal_consistency": geometric["normal_inconsistency_count"] == 0,
        "normal_inconsistency_count": geometric["normal_inconsistency_count"],
        "free_edge_count": geometric["free_edge_count"],
        "unexpected_free_edge_count": geometric["unexpected_free_edge_count"],
        "non_manifold_edge_count": geometric["non_manifold_edge_count"],
        "duplicate_node_count": len(nodes) - len(set(nodes.values())),
        "max_transition_ratio": geometric["max_transition_ratio"],
        "max_skew_angle": geometric["max_skew_angle_deg"],
        "max_skew_angle_deg": geometric["max_skew_angle_deg"],
        "max_warpage_angle": geometric["max_warpage_deg"],
        "min_jacobian": geometric["min_scaled_jacobian"],
        "min_scaled_jacobian": geometric["min_scaled_jacobian"],
        "scaled_jacobian_p05": geometric["scaled_jacobian_p05"],
        "max_skewness": geometric["max_skewness"],
        "skewness_p95": geometric["skewness_p95"],
        "max_warpage_deg": geometric["max_warpage_deg"],
    }
    metrics.update(door_check["metrics"])
    diagnostic_hints = []
    if high_aspect_count:
        diagnostic_hints.append({
            "target": "mesh_and_set_ir",
            "reason": "High aspect ratio elements are usually concentrated around door transitions or coarse circumferential bands.",
            "suggested_actions": [
                "reduce door_size_mm for one trial",
                "increase hole_layers around the door boundary",
                "increase patch_margin_z_mm or patch_margin_u_mm",
                "inspect high_aspect_ratio scalar in PyVista preview before changing global mesh size",
            ],
        })
    return {
        "status": "blocked" if errors else ("warning" if warnings else "pass"),
        "blocked": bool(errors),
        "errors": errors,
        "warnings": warnings,
        "metrics": metrics,
        "diagnostic_hints": diagnostic_hints,
    }


def _quad_mesh_geometric_quality(
    nodes: dict[int, tuple[float, float, float]],
    elements: dict[int, list[int]],
    sets: dict[str, Any],
) -> dict[str, Any]:
    """Compute solver-independent shell topology and shape metrics."""

    def sub(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
        return a[0] - b[0], a[1] - b[1], a[2] - b[2]

    def dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
        return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]

    def cross(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
        return a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]

    def norm(a: tuple[float, float, float]) -> float:
        return math.sqrt(max(0.0, dot(a, a)))

    def angle(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
        denominator = norm(a) * norm(b)
        if denominator <= 1.0e-15:
            return 180.0
        cosine = max(-1.0, min(1.0, dot(a, b) / denominator))
        return math.degrees(math.acos(cosine))

    edge_owners: dict[tuple[int, int], list[tuple[int, tuple[int, int]]]] = {}
    areas: dict[int, float] = {}
    skewness: list[float] = []
    scaled_jacobians: list[float] = []
    warpage: list[float] = []
    radial_signs: list[float] = []
    for element_id, conn in elements.items():
        if len(conn) != 4:
            continue
        points = [nodes[node_id] for node_id in conn]
        areas[element_id] = _quad_area(nodes, conn)
        for first, second in zip(conn, conn[1:] + conn[:1]):
            edge_owners.setdefault(tuple(sorted((first, second))), []).append((element_id, (first, second)))
        corner_angles: list[float] = []
        corner_jacobians: list[float] = []
        for index in range(4):
            previous = sub(points[(index - 1) % 4], points[index])
            following = sub(points[(index + 1) % 4], points[index])
            corner_angles.append(angle(previous, following))
            denominator = norm(previous) * norm(following)
            corner_jacobians.append(norm(cross(previous, following)) / denominator if denominator > 1.0e-15 else 0.0)
        skewness.append(max(abs(value - 90.0) for value in corner_angles) / 90.0)
        scaled_jacobians.append(min(corner_jacobians))
        normal_a = cross(sub(points[1], points[0]), sub(points[2], points[0]))
        normal_b = cross(sub(points[2], points[0]), sub(points[3], points[0]))
        warpage.append(angle(normal_a, normal_b))
        center = tuple(sum(point[axis] for point in points) / 4.0 for axis in range(3))
        radial_signs.append(dot(normal_a, (center[0], center[1], 0.0)))

    free_edges = [edge for edge, owners in edge_owners.items() if len(owners) == 1]
    expected_nodes = set(sets.get("base_nodes") or []) | set(sets.get("top_nodes") or []) | set(sets.get("door_nodes") or [])
    unexpected_free = [edge for edge in free_edges if not set(edge) <= expected_nodes]
    non_manifold = sum(len(owners) > 2 for owners in edge_owners.values())
    inconsistent = sum(
        len(owners) == 2 and owners[0][1] == owners[1][1]
        for owners in edge_owners.values()
    )
    transition_ratios = []
    for owners in edge_owners.values():
        if len(owners) == 2:
            left = areas.get(owners[0][0], 0.0)
            right = areas.get(owners[1][0], 0.0)
            if min(left, right) > 1.0e-15:
                transition_ratios.append(max(left, right) / min(left, right))
    nonzero_signs = [value for value in radial_signs if abs(value) > 1.0e-12]
    majority_positive = sum(value > 0 for value in nonzero_signs) >= sum(value < 0 for value in nonzero_signs)
    inverted = sum((value < 0) if majority_positive else (value > 0) for value in nonzero_signs)

    def percentile(values: list[float], quantile: float, default: float) -> float:
        if not values:
            return default
        ordered = sorted(values)
        index = min(len(ordered) - 1, int(round((len(ordered) - 1) * quantile)))
        return ordered[index]

    max_skewness = max(skewness, default=0.0)
    return {
        "normal_inconsistency_count": inconsistent,
        "inverted_element_count": inverted,
        "free_edge_count": len(free_edges),
        "unexpected_free_edge_count": len(unexpected_free),
        "non_manifold_edge_count": non_manifold,
        "max_transition_ratio": max(transition_ratios, default=1.0),
        "max_skew_angle_deg": 90.0 * max_skewness,
        "max_warpage_deg": max(warpage, default=0.0),
        "min_scaled_jacobian": min(scaled_jacobians, default=0.0),
        "scaled_jacobian_p05": percentile(scaled_jacobians, 0.05, 0.0),
        "max_skewness": max_skewness,
        "skewness_p95": percentile(skewness, 0.95, 0.0),
    }


def write_l0_inp(path: Path, job_type: str, ir: dict[str, Any], nodes: dict[int, tuple[float, float, float]], elements: dict[int, list[int]], segments: dict[int, int], tower: list[dict[str, Any]], sets: dict[str, Any]) -> None:
    part_name = abaqus_name(ir["mesh_and_set_ir"].get("naming_rules", {}).get("part") or "WINDTOWER_SHELL_L0", "WINDTOWER_SHELL_L0")
    instance_name = abaqus_name(ir["mesh_and_set_ir"].get("naming_rules", {}).get("instance") or f"{part_name}_1", f"{part_name}_1")
    material = ir["semantic_ir"]["materials"][0]
    material_name = abaqus_name(material.get("name") or "STEEL", "STEEL")
    door = (ir.get("semantic_ir", {}).get("door_openings") or [{}])[0]
    reinforcement_ids = set(sets.get("door_reinforcement_elements") or [])
    reinforcement_material = abaqus_name(
        door.get("reinforcement_material") or "Q355ND", "Q355ND"
    )
    reinforcement_thickness = float(door.get("frame_thickness_mm") or 0.0)
    by_segment: dict[int, list[int]] = {}
    for eid, seg_id in segments.items():
        if eid in reinforcement_ids:
            continue
        by_segment.setdefault(seg_id, []).append(eid)
    with path.open("w", encoding="utf-8") as f:
        f.write("*Heading\n")
        f.write(f"** L0 wind tower {job_type} generated by {COMPILER_VERSION}\n")
        f.write("** Units: mm, N, MPa, tonne, s\n")
        _write_load_contract_comments(f, ir)
        f.write(f"*Part, name={part_name}\n")
        f.write("*Node\n")
        for nid in sorted(nodes):
            x, y, z = nodes[nid]
            f.write(f"{nid}, {x:.6f}, {y:.6f}, {z:.6f}\n")
        f.write("*Element, type=S4R\n")
        for eid in sorted(elements):
            f.write(f"{eid}, " + ", ".join(str(nid) for nid in elements[eid]) + "\n")
        _write_elset(f, "ALL_ELEMENTS", sorted(elements))
        for seg_id in sorted(by_segment):
            _write_elset(f, f"SEG_{seg_id:03d}", by_segment[seg_id])
        _write_elset(f, "DOOR_PATCH_ELEMENTS", sets["door_patch_elements"])
        _write_elset(f, "DOOR_NEIGHBOR_ELEMENTS", sets["door_neighbor_elements"])
        if reinforcement_ids:
            _write_elset(f, "DOOR_REINFORCEMENT_ELEMENTS", sorted(reinforcement_ids))
            _write_elset(f, "DOOR_REINFORCEMENT_ADJACENT_ELEMENTS", sets.get("door_reinforcement_adjacent_elements", []))
            _write_elset(f, "DOOR_REINFORCEMENT_CHECK_ELEMENTS", sets.get("door_reinforcement_check_elements", []))
        for name, values in sets["flange_window_elements"].items():
            _write_elset(f, name, values)
        for seg_id in sorted(by_segment):
            thickness = tower[seg_id - 1]["thickness_mm"]
            f.write(f"*Shell Section, elset=SEG_{seg_id:03d}, material={material_name}\n")
            f.write(f"{thickness:.6f}, 5\n")
        if reinforcement_ids:
            f.write(f"*Shell Section, elset=DOOR_REINFORCEMENT_ELEMENTS, material={reinforcement_material}\n")
            f.write(f"{reinforcement_thickness:.6f}, 5\n")
        f.write("*End Part\n")
        f.write("*Assembly, name=ASSEMBLY\n")
        f.write(f"*Instance, name={instance_name}, part={part_name}\n")
        f.write("*End Instance\n")
        _write_nset(f, "BASE_FIXED", sets["base_nodes"], instance_name)
        _write_nset(f, "TOP_LOAD_NODES", sets["top_nodes"], instance_name)
        _write_nset(f, "DOOR_OPENING_NODES", sets["door_nodes"], instance_name)
        if sets["wind_spos"]:
            _write_elset(f, "WINDWARD_SPOS_ELEMENTS", sets["wind_spos"], instance_name)
        if sets["wind_sneg"]:
            _write_elset(f, "WINDWARD_SNEG_ELEMENTS", sets["wind_sneg"], instance_name)
        f.write("*Surface, type=ELEMENT, name=WINDWARD_SURFACE\n")
        if sets["wind_spos"]:
            f.write("WINDWARD_SPOS_ELEMENTS, SPOS\n")
        if sets["wind_sneg"]:
            f.write("WINDWARD_SNEG_ELEMENTS, SNEG\n")
        for band in sets.get("wind_bands", []):
            if band.get("spos"):
                _write_elset(f, f"{band['name']}_SPOS_ELEMENTS", band["spos"], instance_name)
            if band.get("sneg"):
                _write_elset(f, f"{band['name']}_SNEG_ELEMENTS", band["sneg"], instance_name)
            f.write(f"*Surface, type=ELEMENT, name={band['name']}\n")
            if band.get("spos"):
                f.write(f"{band['name']}_SPOS_ELEMENTS, SPOS\n")
            if band.get("sneg"):
                f.write(f"{band['name']}_SNEG_ELEMENTS, SNEG\n")
        rotary = ir["top_equivalent_system"].get("rotary_inertia", {})
        rotary_enabled = bool(rotary.get("enabled") and all(rotary.get(key) is not None for key in ("Ixx", "Iyy", "Izz")))
        _write_top_rp_block(f, max(xyz[2] for xyz in nodes.values()), rotary_enabled=rotary_enabled, include_mass=float(ir["top_equivalent_system"].get("mass_tonne") or 0.0) > 0.0)
        f.write("*End Assembly\n")
        f.write(f"*Material, name={material_name}\n")
        f.write("*Density\n")
        f.write(f"{float(material.get('density') or STEEL_DENSITY_TONNE_PER_MM3):.8e}\n")
        f.write("*Elastic\n")
        f.write(f"{float(material.get('elastic_modulus_mpa') or 206000.0):.6g}, {float(material.get('poisson_ratio') if material.get('poisson_ratio') is not None else 0.3):.6g}\n")
        if reinforcement_ids and reinforcement_material != material_name:
            f.write(f"*Material, name={reinforcement_material}\n")
            f.write("*Density\n")
            f.write(f"{float(material.get('density') or STEEL_DENSITY_TONNE_PER_MM3):.8e}\n")
            f.write("*Elastic\n")
            f.write(f"{float(material.get('elastic_modulus_mpa') or 206000.0):.6g}, {float(material.get('poisson_ratio') if material.get('poisson_ratio') is not None else 0.3):.6g}\n")
        if float(ir["top_equivalent_system"].get("mass_tonne") or 0.0) > 0.0:
            f.write("*Mass, elset=TOP_NACELLE_MASS\n")
            f.write(f"{float(ir['top_equivalent_system']['mass_tonne']):.6f}\n")
        if rotary_enabled:
            f.write("*Rotary Inertia, elset=TOP_NACELLE_ROTARY_INERTIA\n")
            f.write(f"{float(rotary['Ixx']):.6f}, {float(rotary['Iyy']):.6f}, {float(rotary['Izz']):.6f}\n")
        if job_type == "static_strength":
            _write_static_steps(f, ir, instance_name)
        elif job_type == "modal":
            _write_modal_steps(f, ir, instance_name)
        elif job_type == "buckling":
            _write_buckling_steps(f, ir, instance_name)
        else:
            raise ValueError(f"Unsupported L0 job type: {job_type}")


def abaqus_name(value: Any, fallback: str) -> str:
    """Return a conservative Abaqus keyword name.

    Excel sometimes stores engineering identifiers in cells that later become
    floats, e.g. Q345 -> 345.0 or WINDTOWER_SHELL_V3 -> 3.0 in old artifacts.
    Names used in Abaqus keywords must start with a letter, so the compiler
    sanitizes at the last deterministic boundary.
    """
    text = str(value or "").strip()
    if not text:
        text = fallback
    if re.fullmatch(r"\d+(?:\.0+)?", text):
        text = fallback
    text = ABAQUS_NAME_RE.sub("_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text or not text[0].isalpha():
        text = f"{fallback}_{text}" if text else fallback
    return text[:36].rstrip("_") or fallback[:36]


def _write_load_contract_comments(f: Any, ir: dict[str, Any]) -> None:
    contract = ir.get("load_contract", {})
    components = contract.get("components", {})
    f.write(f"** Load contract status: {ir.get('load_readiness', {}).get('status', 'unknown')} policy={contract.get('policy', 'unknown')}\n")
    for key in ("tower_self_weight", "top_equivalent_mass", "top_vertical_weight", "side_wind_pressure", "top_horizontal_thrust", "top_eccentric_moment"):
        item = components.get(key)
        if not item:
            continue
        value = item.get("value")
        if isinstance(value, dict):
            value_text = ", ".join(f"{k}={v}" for k, v in value.items())
        elif isinstance(value, list):
            value_text = f"{len(value)} profile rows"
        else:
            value_text = str(value)
        f.write(f"** LOAD {key}: {value_text} {item.get('unit', '')} status={item.get('status', '')} source={item.get('source', '')}\n")


def _write_static_steps(f: Any, ir: dict[str, Any], instance_name: str) -> None:
    static = ir["analysis_cases"]["static_strength"]
    nlgeom = "YES" if bool(static.get("nlgeom")) else "NO"
    top = ir["top_equivalent_system"]
    vertical = float(static.get("top_force_fz_n") or (-float(top["mass_tonne"]) * GRAVITY_MM_PER_S2))
    top_fx = float(static.get("top_force_fx_n", static.get("top_force_x_n", 0.0)) or 0.0)
    top_fy = float(static.get("top_force_fy_n", 0.0) or 0.0)
    moment_mx = float(static.get("top_moment_mx_nmm", 0.0) or 0.0)
    moment_my = float(static.get("top_moment_my_nmm", abs(vertical) * float(top["center_of_mass_offset_mm"])) or 0.0)
    moment_mz = float(static.get("top_moment_mz_nmm", 0.0) or 0.0)
    f.write(f"*Step, name=STEP-1-GRAVITY-TOPMASS, nlgeom={nlgeom}\n")
    f.write("*Static\n0.1, 1., 1e-05, 0.1\n")
    f.write("*Boundary\nBASE_FIXED, 1, 6\n")
    f.write("*Dload\n")
    f.write(f"{instance_name}.ALL_ELEMENTS, GRAV, {GRAVITY_MM_PER_S2:.6f}, 0., 0., -1.\n")
    f.write("*Cload\n")
    f.write(f"TOP_RP_NSET, 3, {vertical:.6f}\n")
    _write_step_outputs(f, ir, instance_name, "static_general")
    f.write("*End Step\n")
    f.write(f"*Step, name=STEP-2-STATIC-WIND-THRUST-MOMENT, nlgeom={nlgeom}\n")
    f.write("*Static\n0.1, 1., 1e-05, 0.1\n")
    f.write("*Cload\n")
    if top_fx:
        f.write(f"TOP_RP_NSET, 1, {top_fx:.6f}\n")
    if top_fy:
        f.write(f"TOP_RP_NSET, 2, {top_fy:.6f}\n")
    if moment_mx:
        f.write(f"TOP_RP_NSET, 4, {moment_mx:.6f}\n")
    if moment_my:
        f.write(f"TOP_RP_NSET, 5, {moment_my:.6f}\n")
    if moment_mz:
        f.write(f"TOP_RP_NSET, 6, {moment_mz:.6f}\n")
    f.write("*Dsload\n")
    _write_wind_dsloads(f, static)
    _write_step_outputs(f, ir, instance_name, "static_general")
    f.write("*End Step\n")


def _write_modal_steps(f: Any, ir: dict[str, Any], instance_name: str) -> None:
    modes = int(ir["analysis_cases"]["modal"].get("modes", 20))
    if ir["analysis_cases"]["modal"].get("prestress", True):
        static = ir["analysis_cases"]["static_strength"]
        top = ir["top_equivalent_system"]
        vertical = float(static.get("top_force_fz_n") or (-float(top["mass_tonne"]) * GRAVITY_MM_PER_S2))
        top_fx = float(static.get("top_force_fx_n", static.get("top_force_x_n", 0.0)) or 0.0)
        top_fy = float(static.get("top_force_fy_n", 0.0) or 0.0)
        moment_mx = float(static.get("top_moment_mx_nmm", 0.0) or 0.0)
        moment_my = float(static.get("top_moment_my_nmm", abs(vertical) * float(top["center_of_mass_offset_mm"])) or 0.0)
        moment_mz = float(static.get("top_moment_mz_nmm", 0.0) or 0.0)
        f.write("*Step, name=STEP-1-MODAL-PRESTRESS, nlgeom=YES\n")
        f.write("*Static\n0.1, 1., 1e-05, 0.1\n")
        f.write("*Boundary\nBASE_FIXED, 1, 6\n")
        f.write("*Dload\n")
        f.write(f"{instance_name}.ALL_ELEMENTS, GRAV, {GRAVITY_MM_PER_S2:.6f}, 0., 0., -1.\n")
        f.write("*Cload\n")
        f.write(f"TOP_RP_NSET, 3, {vertical:.6f}\n")
        if top_fx:
            f.write(f"TOP_RP_NSET, 1, {top_fx:.6f}\n")
        if top_fy:
            f.write(f"TOP_RP_NSET, 2, {top_fy:.6f}\n")
        if moment_mx:
            f.write(f"TOP_RP_NSET, 4, {moment_mx:.6f}\n")
        if moment_my:
            f.write(f"TOP_RP_NSET, 5, {moment_my:.6f}\n")
        if moment_mz:
            f.write(f"TOP_RP_NSET, 6, {moment_mz:.6f}\n")
        f.write("*Dsload\n")
        _write_wind_dsloads(f, static)
        _write_step_outputs(f, ir, instance_name, "static_general")
        f.write("*End Step\n")
        f.write("*Step, name=STEP-2-FREQUENCY-PRESTRESSED\n")
    else:
        f.write("*Step, name=STEP-1-FREQUENCY\n")
    f.write("*Frequency, eigensolver=Lanczos\n")
    f.write(f"{modes},\n")
    f.write("*Boundary\nBASE_FIXED, 1, 6\n")
    _write_step_outputs(f, ir, instance_name, "frequency")
    f.write("*End Step\n")


def _write_buckling_steps(f: Any, ir: dict[str, Any], instance_name: str) -> None:
    modes = int((ir["analysis_cases"].get("buckling_screening") or ir["analysis_cases"].get("buckling") or {}).get("modes", 10))
    static = ir["analysis_cases"]["static_strength"]
    top = ir["top_equivalent_system"]
    vertical = float(static.get("top_force_fz_n") or (-float(top["mass_tonne"]) * GRAVITY_MM_PER_S2))
    top_fx = float(static.get("top_force_fx_n", static.get("top_force_x_n", 0.0)) or 0.0)
    top_fy = float(static.get("top_force_fy_n", 0.0) or 0.0)
    moment_mx = float(static.get("top_moment_mx_nmm", 0.0) or 0.0)
    moment_my = float(static.get("top_moment_my_nmm", abs(vertical) * float(top["center_of_mass_offset_mm"])) or 0.0)
    moment_mz = float(static.get("top_moment_mz_nmm", 0.0) or 0.0)
    f.write("*Step, name=STEP-1-LINEAR-BUCKLING, nlgeom=NO\n")
    f.write("*Buckle\n")
    f.write(f"{modes},\n")
    f.write("*Boundary\nBASE_FIXED, 1, 6\n")
    f.write("*Dload\n")
    f.write(f"{instance_name}.ALL_ELEMENTS, GRAV, {GRAVITY_MM_PER_S2:.6f}, 0., 0., -1.\n")
    f.write("*Cload\n")
    f.write(f"TOP_RP_NSET, 3, {vertical:.6f}\n")
    if top_fx:
        f.write(f"TOP_RP_NSET, 1, {top_fx:.6f}\n")
    if top_fy:
        f.write(f"TOP_RP_NSET, 2, {top_fy:.6f}\n")
    if moment_mx:
        f.write(f"TOP_RP_NSET, 4, {moment_mx:.6f}\n")
    if moment_my:
        f.write(f"TOP_RP_NSET, 5, {moment_my:.6f}\n")
    if moment_mz:
        f.write(f"TOP_RP_NSET, 6, {moment_mz:.6f}\n")
    f.write("*Dsload\n")
    _write_wind_dsloads(f, static)
    _write_step_outputs(f, ir, instance_name, "buckling")
    f.write("*End Step\n")


def _write_wind_dsloads(f: Any, static: dict[str, Any]) -> None:
    profile = static.get("wind_pressure_profile")
    if isinstance(profile, list) and profile:
        wrote = False
        for index, item in enumerate(profile, start=1):
            try:
                pressure = float(item.get("pressure_mpa"))
            except Exception:
                continue
            name = abaqus_name(item.get("band_id") or f"WIND_BAND_{index:02d}", f"WIND_BAND_{index:02d}")
            f.write(f"{name}, P, {pressure:.8f}\n")
            wrote = True
        if wrote:
            return
    wind = float(static.get("wind_pressure_mpa") or DEFAULT_WIND_PRESSURE_MPA)
    f.write(f"WINDWARD_SURFACE, P, {wind:.8f}\n")


def _write_step_outputs(f: Any, ir: dict[str, Any], instance_name: str, procedure: str) -> None:
    """Compile the typed output contract for one Abaqus procedure.

    Frequency and buckling history values are produced automatically by the
    eigensolver, so this function deliberately emits no *Modal Output block.
    """
    static_nlgeom = bool(
        procedure == "static_general"
        and (ir.get("analysis_cases", {}).get("static_strength", {}).get("nlgeom"))
    )
    matrix = resolved_output_matrix(
        ir["model_spec_ir"]["outputs"], static_nlgeom=static_nlgeom
    )
    request = matrix[procedure]
    field = request["field"]
    if procedure == "static_general":
        f.write(f"*Output, field, frequency={int(field['compiled_frequency'])}\n")
    else:
        f.write("*Output, field\n")
    for node_request in field.get("node", []):
        region = str(node_request.get("region") or "ALL_NODES")
        suffix = "" if region == "ALL_NODES" else f", nset={region}"
        f.write(f"*Node Output{suffix}\n")
        f.write(", ".join(node_request["variables"]) + "\n")
    for element_request in field.get("element", []):
        region = str(element_request.get("region") or "ALL_ELEMENTS")
        qualified_region = region if "." in region else f"{instance_name}.{region}"
        directions = ", directions=YES" if element_request.get("directions") else ""
        f.write(f"*Element Output, elset={qualified_region}{directions}\n")
        f.write(", ".join(element_request["variables"]) + "\n")

    history = request.get("history", {})
    if procedure != "static_general" or not history.get("node"):
        return
    f.write(f"*Output, history, frequency={int(history['frequency'])}\n")
    for node_request in history["node"]:
        f.write(f"*Node Output, nset={node_request['region']}\n")
        f.write(", ".join(node_request["variables"]) + "\n")
    for energy_request in history.get("energy", []):
        f.write("*Energy Output\n")
        f.write(", ".join(energy_request["variables"]) + "\n")


def _write_top_rp_block(f: Any, zmax: float, rotary_enabled: bool = False, include_mass: bool = True) -> None:
    f.write("*Node\n")
    f.write(f"{TOP_RP_NODE}, 0., 0., {zmax:.6f}\n")
    _write_nset(f, "TOP_RP_NSET", [TOP_RP_NODE])
    if include_mass:
        f.write("*Element, type=MASS, elset=TOP_NACELLE_MASS\n")
        f.write(f"{TOP_MASS_ELEMENT}, {TOP_RP_NODE}\n")
    if rotary_enabled:
        f.write("*Element, type=ROTARYI, elset=TOP_NACELLE_ROTARY_INERTIA\n")
        f.write(f"{TOP_ROTARY_INERTIA_ELEMENT}, {TOP_RP_NODE}\n")
    f.write("*Surface, type=NODE, name=TOP_LOAD_SURFACE\n")
    f.write("TOP_LOAD_NODES, 1.\n")
    f.write("*Coupling, constraint name=TOP_RP_DISTRIBUTING_COUPLING, ref node=TOP_RP_NSET, surface=TOP_LOAD_SURFACE\n")
    f.write("*Distributing\n1, 6\n")


def _preview_mesh_stats(
    nodes: dict[int, tuple[float, float, float]],
    elements: dict[int, list[int]],
    sets: dict[str, Any],
    mesh_cfg: dict[str, Any],
) -> dict[str, Any]:
    return {
        "nodes": len(nodes),
        "s4r_elements": len(elements),
        "door_boundary_nodes": len(sets["door_nodes"]),
        "door_patch_elements": len(sets["door_patch_elements"]),
        "door_neighbor_elements": len(sets["door_neighbor_elements"]),
        "windward_spos_elements": len(sets["wind_spos"]),
        "windward_sneg_elements": len(sets["wind_sneg"]),
        "global_size_mm": mesh_cfg["global_size_mm"],
        "door_size_mm": mesh_cfg["door_size_mm"],
        "flange_size_mm": mesh_cfg["flange_size_mm"],
        "element_family": "S4R shell",
    }


def _write_pyvista_mesh_data(
    path: Path,
    nodes: dict[int, tuple[float, float, float]],
    elements: dict[int, list[int]],
    sets: dict[str, Any],
    quality: dict[str, Any],
) -> dict[str, Any]:
    ordered_nodes = sorted(nodes)
    ordered_elements = sorted(elements)
    node_index = {nid: index for index, nid in enumerate(ordered_nodes)}
    door_patch = set(sets.get("door_patch_elements", []))
    windward = set(sets.get("wind_spos", [])) | set(sets.get("wind_sneg", []))
    aspect_limit = float(quality.get("metrics", {}).get("aspect_ratio_threshold", 10.0) or 10.0)
    points = [[float(nodes[nid][0]), float(nodes[nid][1]), float(nodes[nid][2])] for nid in ordered_nodes]
    quads: list[list[int]] = []
    feature_codes: list[int] = []
    aspect_ratios: list[float] = []
    high_aspect = 0
    for eid in ordered_elements:
        conn = elements[eid][:4]
        quads.append([node_index[nid] for nid in conn])
        lengths = [_distance(nodes[a], nodes[b]) for a, b in zip(conn, conn[1:] + conn[:1])]
        aspect = max(lengths) / min(lengths) if lengths and min(lengths) > 1.0e-9 else 0.0
        aspect_ratios.append(float(aspect))
        if aspect > aspect_limit:
            high_aspect += 1
        code = 0
        if eid in door_patch:
            code += 1
        if eid in windward:
            code += 2
        if aspect > aspect_limit:
            code += 4
        feature_codes.append(code)
    bounds = {
        "min": [min((point[index] for point in points), default=0.0) for index in range(3)],
        "max": [max((point[index] for point in points), default=0.0) for index in range(3)],
    }
    payload = {
        "schema_version": "pyvista_preview_mesh_v1",
        "source": "direct_nodes_elements",
        "units": "mm",
        "points": points,
        "quads": quads,
        "cell_data": {
            "element_id": ordered_elements,
            "feature_code": feature_codes,
            "aspect_ratio": aspect_ratios,
        },
        "feature_code_legend": {"1": "door_patch", "2": "windward", "4": "high_aspect_ratio"},
        "bounds": bounds,
        "stats": {
            "nodes": len(points),
            "quads": len(quads),
            "door_patch_elements": len(door_patch),
            "windward_elements": len(windward),
            "high_aspect_ratio_elements": high_aspect,
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return {"artifact_size_bytes": path.stat().st_size, "schema_version": payload["schema_version"], **payload["stats"]}


def _write_assembly_overlay(
    path: Path,
    nodes: dict[int, tuple[float, float, float]],
    elements: dict[int, list[int]],
    sets: dict[str, Any],
    ir: dict[str, Any],
) -> dict[str, Any]:
    z_values = [xyz[2] for xyz in nodes.values()]
    z_min = min(z_values, default=0.0)
    z_max = max(z_values, default=0.0)
    loads = ir.get("analysis_cases", {}).get("static_strength", {})
    top_system = ir.get("top_equivalent_system", {})
    payload = {
        "schema_version": "assembly_overlay_v1",
        "units": "mm-N-MPa",
        "status": "available",
        "sets": {
            "boundary": [
                {"name": "BASE_FIXED", "type": "NSET", "count": len(sets.get("base_nodes", [])), "dofs": [1, 2, 3, 4, 5, 6]},
            ],
            "load_targets": [
                {"name": "TOP_LOAD_NODES", "type": "NSET", "count": len(sets.get("top_nodes", []))},
                {"name": "WINDWARD_SURFACE", "type": "SURFACE", "count": len(set(sets.get("wind_spos", [])) | set(sets.get("wind_sneg", []))), "display_enabled": False},
            ],
            "local_features": [
                {"name": "DOOR_PATCH_ELEMENTS", "type": "ELSET", "count": len(sets.get("door_patch_elements", []))},
                {"name": "DOOR_NEIGHBOR_ELEMENTS", "type": "ELSET", "count": len(sets.get("door_neighbor_elements", []))},
            ],
        },
        "reference_points": [
            {
                "name": "TOP_RP_NSET",
                "node_id": TOP_RP_NODE,
                "coordinates_mm": [0.0, 0.0, float(z_max)],
                "purpose": "top_equivalent_mass_and_load_reference",
            }
        ],
        "couplings": [
            {
                "name": "TOP_RP_DISTRIBUTING_COUPLING",
                "type": top_system.get("coupling_type", "distributing"),
                "reference_point": "TOP_RP_NSET",
                "surface": "TOP_LOAD_SURFACE",
                "target_set": "TOP_LOAD_NODES",
                "dofs": [1, 2, 3, 4, 5, 6],
            }
        ],
        "boundary_conditions": [
            {"name": "BASE_FIXED_ENCASTRE", "target": "BASE_FIXED", "dofs": [1, 2, 3, 4, 5, 6]},
        ],
        "loads": [
            {
                "name": "TOP_EQUIVALENT_LOADS",
                "target": "TOP_RP_NSET",
                "components": {
                    "Fx": float(loads.get("top_force_fx_n", 0.0) or 0.0),
                    "Fy": float(loads.get("top_force_fy_n", 0.0) or 0.0),
                    "Fz": float(loads.get("top_force_fz_n", 0.0) or 0.0),
                    "Mx": float(loads.get("top_moment_mx_nmm", 0.0) or 0.0),
                    "My": float(loads.get("top_moment_my_nmm", 0.0) or 0.0),
                    "Mz": float(loads.get("top_moment_mz_nmm", 0.0) or 0.0),
                },
                "display_enabled": False,
            },
            {
                "name": "WIND_PRESSURE",
                "target": "WINDWARD_SURFACE",
                "pressure_mpa": float(loads.get("wind_pressure_mpa", 0.0) or 0.0),
                "display_enabled": False,
            },
        ],
        "interactions": [
            {"name": "L0_NO_CONTACT_INTERACTIONS", "status": "not_required", "reason": "L0 shell tower model has no contact pairs."},
        ],
        "model_bounds": {"z_min_mm": float(z_min), "z_max_mm": float(z_max)},
        "mesh_counts": {"nodes": len(nodes), "elements": len(elements)},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**payload, "artifact_size_bytes": path.stat().st_size}


def _desktop_python_executable() -> str:
    if os.name == "nt":
        executable = Path(sys.executable)
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.exists():
            return str(pythonw)
    return sys.executable


def _pyvista_desktop_launcher_script() -> str:
    return r'''from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pyvista as pv
import vtkmodules.all as vtk
from matplotlib.colors import LinearSegmentedColormap


def main() -> None:
    mesh_path = Path(sys.argv[1]).resolve()
    payload = json.loads(mesh_path.read_text(encoding="utf-8"))
    points = np.asarray(payload["points"], dtype=float)
    quads = np.asarray(payload["quads"], dtype=np.int64)
    feature_code = np.asarray(payload.get("cell_data", {}).get("feature_code", [0] * len(quads)), dtype=np.int8)
    if quads.size == 0:
        raise RuntimeError("No quadrilateral elements are available for PyVista desktop viewer.")
    cells = np.hstack((np.ones((quads.shape[0], 1), dtype=np.int64) * 4, quads))
    celltypes = np.empty(quads.shape[0], dtype=np.uint8)
    celltypes[:] = vtk.VTK_QUAD
    mesh = pv.UnstructuredGrid(cells, celltypes, points)
    mesh.cell_data["feature_code"] = feature_code
    if payload.get("cell_data", {}).get("aspect_ratio") is not None:
        mesh.cell_data["aspect_ratio"] = np.asarray(payload["cell_data"]["aspect_ratio"], dtype=float)
    cmap = LinearSegmentedColormap.from_list(
        "autofem_abaqus",
        ["#0b3c8c", "#2563eb", "#14b8a6", "#84cc16", "#facc15", "#f97316", "#dc2626"],
        N=7,
    )
    stats = payload.get("stats", {})
    plotter = pv.Plotter(window_size=(1500, 950), title="AutoFEM Windtower PyVista Desktop Preview")
    plotter.set_background("white")
    plotter.add_mesh(
        mesh,
        scalars="feature_code",
        cmap=cmap,
        show_edges=True,
        edge_color="#263241",
        line_width=0.35,
        clim=[0, 4],
    )
    plotter.add_text(
        "AutoFEM Windtower PyVista Desktop Preview\n"
        f"Nodes: {stats.get('nodes', points.shape[0])} | S4R shell elements: {stats.get('quads', quads.shape[0])}\n"
        f"Door patch: {stats.get('door_patch_elements', 0)} | Windward: {stats.get('windward_elements', 0)} | High aspect: {stats.get('high_aspect_ratio_elements', 0)}",
        color="black",
        font_size=10,
        position="upper_left",
    )
    try:
        plotter.add_camera_orientation_widget()
    except Exception:
        pass
    plotter.view_isometric()
    plotter.reset_camera()
    plotter.show()


if __name__ == "__main__":
    main()
'''


def _write_pyvista_preview(
    html_path: Path,
    nodes: dict[int, tuple[float, float, float]],
    elements: dict[int, list[int]],
    sets: dict[str, Any],
    quality: dict[str, Any],
) -> dict[str, Any]:
    try:
        import numpy as np
        import pyvista as pv
        import vtkmodules.all as vtk
        from matplotlib.colors import LinearSegmentedColormap
    except Exception as exc:
        _write_pyvista_error_html(html_path, f"PyVista/trame dependencies are unavailable: {exc}")
        return {
            "viewer": "pyvista_html",
            "source": "direct_pyvista_unstructured_grid",
            "fallback_required": False,
            "warnings": [f"PyVista HTML export is unavailable: {exc}"],
            "high_aspect_ratio_elements": 0,
        }

    ordered_nodes = sorted(nodes)
    ordered_elements = sorted(elements)
    node_index = {nid: index for index, nid in enumerate(ordered_nodes)}
    points = np.array([nodes[nid] for nid in ordered_nodes], dtype=float)
    if not ordered_elements:
        _write_pyvista_error_html(html_path, "No shell elements are available for PyVista preview.")
        return {
            "viewer": "pyvista_html",
            "source": "direct_pyvista_unstructured_grid",
            "fallback_required": False,
            "warnings": ["PyVista preview skipped because the mesh has no elements."],
            "high_aspect_ratio_elements": 0,
        }

    elem_new = np.array([[node_index[nid] for nid in elements[eid][:4]] for eid in ordered_elements], dtype=np.int64)
    head = np.ones((elem_new.shape[0], 1), dtype=np.int64) * 4
    cells = np.hstack((head, elem_new))
    celltypes = np.empty(len(ordered_elements), dtype=np.uint8)
    celltypes[:] = vtk.VTK_QUAD
    door_patch = set(sets.get("door_patch_elements", []))
    windward = set(sets.get("wind_spos", [])) | set(sets.get("wind_sneg", []))
    aspect_limit = float(quality.get("metrics", {}).get("aspect_ratio_threshold", 8.0) or 8.0)
    aspect_by_eid: dict[int, float] = {}
    feature_codes: list[int] = []
    high_aspect = 0
    for eid in ordered_elements:
        conn = elements[eid]
        lengths = [_distance(nodes[a], nodes[b]) for a, b in zip(conn, conn[1:] + conn[:1])]
        aspect = max(lengths) / min(lengths) if lengths and min(lengths) > 1.0e-9 else 0.0
        aspect_by_eid[eid] = aspect
        if aspect > aspect_limit:
            high_aspect += 1
        code = 0
        if eid in door_patch:
            code += 1
        if eid in windward:
            code += 2
        if aspect > aspect_limit:
            code += 4
        feature_codes.append(code)

    plotter = None
    try:
        grid = pv.UnstructuredGrid(cells, celltypes, points)
        grid.cell_data["element_id"] = np.array(ordered_elements, dtype=np.int64)
        grid.cell_data["aspect_ratio"] = np.array([aspect_by_eid[eid] for eid in ordered_elements], dtype=float)
        grid.cell_data["door_patch"] = np.array([1 if eid in door_patch else 0 for eid in ordered_elements], dtype=np.int8)
        grid.cell_data["windward"] = np.array([1 if eid in windward else 0 for eid in ordered_elements], dtype=np.int8)
        grid.cell_data["high_aspect_ratio"] = np.array(
            [1 if aspect_by_eid[eid] > aspect_limit else 0 for eid in ordered_elements],
            dtype=np.int8,
        )
        grid.cell_data["feature_code"] = np.array(feature_codes, dtype=np.int8)
        plotter = pv.Plotter(off_screen=True, window_size=(1400, 900))
        plotter.set_background("white")
        abaqus_cmap = LinearSegmentedColormap.from_list(
            "autofem_abaqus",
            ["#0b3c8c", "#2563eb", "#14b8a6", "#84cc16", "#facc15", "#f97316", "#dc2626"],
            N=7,
        )
        plotter.add_mesh(
            grid,
            scalars="feature_code",
            show_edges=True,
            cmap=abaqus_cmap,
            line_width=0.35,
            edge_color="#263241",
            clim=[0, 4],
        )
        try:
            plotter.add_camera_orientation_widget()
        except Exception:
            pass
        metrics = quality.get("metrics", {})
        max_aspect = float(metrics.get("max_aspect_ratio", metrics.get("aspect_ratio_max", 0.0)) or 0.0)
        info_text = (
            "AutoFEM Windtower PyVista Preview\n"
            f"Nodes: {len(ordered_nodes)} | S4R shell elements: {len(ordered_elements)}\n"
            f"Door patch: {len(door_patch)} | Windward: {len(windward)} | High aspect: {high_aspect}\n"
            f"Max aspect ratio: {max_aspect:.2f}"
        )
        plotter.add_text(info_text, font_size=10, color="black", position="upper_left")
        plotter.view_isometric()
        plotter.reset_camera()
        _export_pyvista_html(plotter, html_path)
        return {
            "viewer": "pyvista_html",
            "source": "direct_pyvista_unstructured_grid",
            "fallback_required": False,
            "warnings": [],
            "high_aspect_ratio_elements": high_aspect,
        }
    except Exception as exc:
        _write_pyvista_error_html(html_path, f"PyVista HTML export failed: {exc}")
        return {
            "viewer": "pyvista_html",
            "source": "direct_pyvista_unstructured_grid",
            "fallback_required": False,
            "warnings": [f"PyVista HTML export failed. Detail: {exc}"],
            "high_aspect_ratio_elements": high_aspect,
        }
    finally:
        try:
            if plotter is not None:
                plotter.close()
        except Exception:
            pass


def _export_pyvista_html(plotter: Any, html_path: Path) -> None:
    created_loop = None
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        created_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(created_loop)
    try:
        plotter.export_html(str(html_path))
    finally:
        if created_loop is not None:
            try:
                pending = [task for task in asyncio.all_tasks(created_loop) if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    created_loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                created_loop.run_until_complete(created_loop.shutdown_asyncgens())
            finally:
                asyncio.set_event_loop(None)
                created_loop.close()


def _write_pyvista_error_html(html_path: Path, reason: str) -> None:
    html_path.parent.mkdir(parents=True, exist_ok=True)
    safe_reason = html_lib.escape(reason)
    html_path.write_text(
        "\n".join(
            [
                "<!doctype html>",
                "<html><head><meta charset=\"utf-8\"><title>PyVista Preview Error</title>",
                "<style>",
                "body{font-family:Arial,sans-serif;margin:0;padding:32px;background:#f7f8fb;color:#1f2937}",
                ".box{max-width:860px;margin:auto;background:white;border:1px solid #d8dee9;border-radius:8px;padding:24px}",
                "h1{font-size:20px;margin:0 0 12px;color:#b42318}.muted{color:#667085;line-height:1.5}",
                "code{background:#f2f4f7;padding:2px 5px;border-radius:4px}",
                "</style></head>",
                "<body><div class=\"box\"><h1>PyVista preview could not be exported</h1>",
                f"<p class=\"muted\">{safe_reason}</p>",
                "<p class=\"muted\">Check backend dependencies: "
                "<code>pyvista</code>, <code>vtk</code>, <code>trame</code>, "
                "<code>trame-vtk</code>, <code>trame-vuetify</code>, and <code>trame-server==3.2.3</code>.</p>",
                "</div></body></html>",
            ]
        ),
        encoding="utf-8",
    )

def _write_l0_vtk_preview(
    path: Path,
    nodes: dict[int, tuple[float, float, float]],
    elements: dict[int, list[int]],
    sets: dict[str, Any],
    quality: dict[str, Any],
) -> dict[str, Any]:
    ordered_nodes = sorted(nodes)
    node_index = {nid: index for index, nid in enumerate(ordered_nodes)}
    ordered_elements = sorted(elements)
    door_patch = set(sets.get("door_patch_elements", []))
    windward = set(sets.get("wind_spos", [])) | set(sets.get("wind_sneg", []))
    aspect_limit = float(quality.get("metrics", {}).get("aspect_ratio_threshold", 8.0) or 8.0)
    if "aspect_ratio_threshold" not in quality.get("metrics", {}):
        aspect_limit = 8.0
    aspect_by_eid: dict[int, float] = {}
    high_aspect: list[int] = []
    for eid in ordered_elements:
        conn = elements[eid]
        lengths = [_distance(nodes[a], nodes[b]) for a, b in zip(conn, conn[1:] + conn[:1])]
        aspect = max(lengths) / min(lengths) if lengths and min(lengths) > 1.0e-9 else 0.0
        aspect_by_eid[eid] = aspect
        if aspect > aspect_limit:
            high_aspect.append(eid)
    high_aspect_set = set(high_aspect)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write("# vtk DataFile Version 3.0\n")
        f.write("Wind tower L0 true S4R mesh preview\n")
        f.write("ASCII\n")
        f.write("DATASET POLYDATA\n")
        f.write(f"POINTS {len(ordered_nodes)} float\n")
        for nid in ordered_nodes:
            x, y, z = nodes[nid]
            f.write(f"{x:.6f} {y:.6f} {z:.6f}\n")
        f.write(f"POLYGONS {len(ordered_elements)} {len(ordered_elements) * 5}\n")
        for eid in ordered_elements:
            conn = elements[eid]
            f.write("4 " + " ".join(str(node_index[nid]) for nid in conn[:4]) + "\n")
        f.write(f"POINT_DATA {len(ordered_nodes)}\n")
        f.write("SCALARS node_role int 1\nLOOKUP_TABLE default\n")
        door_nodes = set(sets.get("door_nodes", []))
        top_nodes = set(sets.get("top_nodes", []))
        base_nodes = set(sets.get("base_nodes", []))
        for nid in ordered_nodes:
            value = 3 if nid in door_nodes else (2 if nid in top_nodes else (1 if nid in base_nodes else 0))
            f.write(f"{value}\n")
        f.write(f"CELL_DATA {len(ordered_elements)}\n")
        _write_vtk_scalar(f, "element_id", [float(eid) for eid in ordered_elements])
        _write_vtk_scalar(f, "aspect_ratio", [aspect_by_eid[eid] for eid in ordered_elements])
        _write_vtk_scalar(f, "door_patch", [1.0 if eid in door_patch else 0.0 for eid in ordered_elements])
        _write_vtk_scalar(f, "windward", [1.0 if eid in windward else 0.0 for eid in ordered_elements])
        _write_vtk_scalar(f, "high_aspect_ratio", [1.0 if eid in high_aspect_set else 0.0 for eid in ordered_elements])
        _write_vtk_scalar(
            f,
            "feature_code",
            [
                4.0 if eid in high_aspect_set else (2.0 if eid in windward else (1.0 if eid in door_patch else 0.0))
                for eid in ordered_elements
            ],
        )
    return {
        "point_data_arrays": ["node_role"],
        "cell_data_arrays": ["element_id", "aspect_ratio", "door_patch", "windward", "high_aspect_ratio", "feature_code"],
        "high_aspect_ratio_elements": len(high_aspect),
    }


def _uses_tower_shell_scope(ir: dict[str, Any]) -> bool:
    return ir.get("modeling_decision_ir", {}).get("model_scope", "tower_shell_with_door") == "tower_shell_with_door"


def _active_flange_ranges(tower: list[dict[str, Any]], flanges: list[dict[str, Any]]) -> list[Any]:
    # The paper-scope L0 compiler excludes explicit flange geometry. Avoid
    # loading the optional legacy ABAQUSwork adapter when no flanges are active.
    return find_flange_ranges(tower, flanges) if flanges else []


def _segments_from_element_centers(
    nodes: dict[int, tuple[float, float, float]],
    elements: dict[int, list[int]],
    tower: list[dict[str, Any]],
) -> dict[int, int]:
    def segment_for_z(z_mm: float) -> int:
        for index, (bottom, top) in enumerate(zip(tower[:-1], tower[1:]), start=1):
            if float(bottom["z_mm"]) - 1.0e-6 <= z_mm <= float(top["z_mm"]) + 1.0e-6:
                return index
        return max(len(tower) - 1, 1)

    return {eid: segment_for_z(element_center(nodes, conn)[2]) for eid, conn in elements.items()}


def _door_focus_target(
    nodes: dict[int, tuple[float, float, float]],
    elements: dict[int, list[int]],
    sets: dict[str, Any],
    door: dict[str, Any],
    mesh_cfg: dict[str, Any],
) -> dict[str, Any] | None:
    element_ids = sets.get("door_neighbor_elements") or sets.get("door_patch_elements") or []
    if not element_ids:
        return None
    points = [nodes[nid] for eid in element_ids for nid in elements.get(eid, []) if nid in nodes]
    if not points:
        return None
    x_values, y_values, z_values = zip(*points)
    center = [sum(x_values) / len(x_values), sum(y_values) / len(y_values), sum(z_values) / len(z_values)]
    theta = math.radians(float(door.get("theta_center_deg") or 0.0))
    normal = [math.cos(theta), math.sin(theta), 0.0]
    span = max(max(x_values) - min(x_values), max(y_values) - min(y_values), max(z_values) - min(z_values), float(mesh_cfg.get("door_size_mm") or 1.0))
    return {
        "center_mm": center,
        "normal": normal,
        "bounds_mm": [min(x_values), max(x_values), min(y_values), max(y_values), min(z_values), max(z_values)],
        "recommended_distance_mm": span * 2.4,
        "local_element_size_mm": float(mesh_cfg.get("door_size_mm") or 0.0),
        "neighbor_element_count": len(element_ids),
    }


def _write_vtk_scalar(f: Any, name: str, values: list[float]) -> None:
    f.write(f"SCALARS {name} float 1\n")
    f.write("LOOKUP_TABLE default\n")
    for value in values:
        f.write(f"{float(value):.8g}\n")


def static_check_l0_inp(
    path: Path,
    job_type: str,
    output_contract: dict[str, Any] | None = None,
) -> dict[str, bool]:
    text = path.read_text(encoding="utf-8")
    contract = normalize_output_contract(output_contract)
    steps = _inp_step_blocks(text)
    static_steps = [block for name, block in steps if "*STATIC" in block.upper()]
    frequency_steps = [block for name, block in steps if "*FREQUENCY" in block.upper()]
    buckling_steps = [block for name, block in steps if "*BUCKLE" in block.upper()]
    checks = {
        "has_s4r": "*Element, type=S4R" in text,
        "has_shell_section": "*Shell Section" in text,
        "has_top_rp": "TOP_RP_NSET" in text and "TOP_RP_DISTRIBUTING_COUPLING" in text,
        "has_base_fixed": "BASE_FIXED" in text,
        "has_windward_surface": "WINDWARD_SURFACE" in text,
        "has_door_neighbor": "DOOR_NEIGHBOR_ELEMENTS" in text,
    }
    if job_type == "static_strength":
        checks.update({
            "has_static": bool(static_steps),
            "has_dload": "*Dload" in text,
            "has_cload": "*Cload" in text,
            "has_dsload": "*Dsload" in text,
            "all_static_steps_have_field_and_history": bool(static_steps) and all(_static_output_block_complete(block) for block in static_steps),
        })
        if contract["profile"] == "global_to_local":
            checks["global_to_local_has_static_nforc"] = all(_keyword_variables(block, "Element Output").__contains__("NFORC") for block in static_steps)
    elif job_type == "modal":
        checks.update({
            "has_frequency": bool(frequency_steps),
            "no_buckle": "*Buckle" not in text,
            "prestress_static_has_field_and_history": all(_static_output_block_complete(block) for block in static_steps),
            "frequency_has_modal_fields": bool(frequency_steps) and all(
                {"U"}.issubset(_keyword_variables(block, "Node Output"))
                and {"S", "E"}.issubset(_keyword_variables(block, "Element Output"))
                for block in frequency_steps
            ),
            "frequency_excludes_nforc": all("NFORC" not in _keyword_variables(block, "Element Output") for block in frequency_steps),
            "no_modal_output_keyword": "*MODAL OUTPUT" not in text.upper(),
        })
        if contract["profile"] == "global_to_local" and static_steps:
            checks["prestress_static_has_nforc"] = all("NFORC" in _keyword_variables(block, "Element Output") for block in static_steps)
    elif job_type == "buckling":
        checks.update({
            "has_buckle": bool(buckling_steps),
            "has_reference_loads": "*Dload" in text and "*Cload" in text and "*Dsload" in text,
            "buckling_has_modal_fields": bool(buckling_steps) and all(
                {"U"}.issubset(_keyword_variables(block, "Node Output"))
                and {"S", "E"}.issubset(_keyword_variables(block, "Element Output"))
                for block in buckling_steps
            ),
            "buckling_excludes_nforc": all("NFORC" not in _keyword_variables(block, "Element Output") for block in buckling_steps),
        })
    return checks


def _inp_step_blocks(text: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    for match in re.finditer(r"(?ims)^\*Step\b(?P<header>[^\r\n]*)\r?\n(?P<body>.*?)^\*End Step\s*$", text):
        header = match.group("header")
        name_match = re.search(r"(?i)\bname\s*=\s*([^,\s]+)", header)
        blocks.append((name_match.group(1) if name_match else "UNNAMED", match.group(0)))
    return blocks


def _keyword_variables(step_block: str, keyword: str) -> set[str]:
    variables: set[str] = set()
    pattern = rf"(?im)^\*{re.escape(keyword)}\b[^\r\n]*\r?\n(?P<values>(?:^(?!\*)[^\r\n]*\r?\n?)*)"
    for match in re.finditer(pattern, step_block):
        for token in re.split(r"[,\s]+", match.group("values").strip()):
            if token:
                variables.add(token.upper())
    return variables


def _static_output_block_complete(step_block: str) -> bool:
    upper = step_block.upper()
    node_variables = _keyword_variables(step_block, "Node Output")
    element_variables = _keyword_variables(step_block, "Element Output")
    energy_variables = _keyword_variables(step_block, "Energy Output")
    expected_strain = "LE" if "NLGEOM=YES" in upper else "E"
    forbidden_strain = "E" if expected_strain == "LE" else "LE"
    return (
        "*OUTPUT, FIELD" in upper
        and "*OUTPUT, HISTORY" in upper
        and "NSET=TOP_RP_NSET" in upper
        and "NSET=BASE_FIXED" in upper
        and set(matrix_value for matrix_value in ["U", "RF", "RM"]).issubset(node_variables)
        and {"S", expected_strain, "SF"}.issubset(element_variables)
        and forbidden_strain not in element_variables
        and {"ALLIE", "ALLSE", "ALLWK", "ALLAE"}.issubset(energy_variables)
    )


def run_l0_job(
    state: ProjectState,
    settings: Settings,
    job_type: str,
    mode: str,
    ir: dict[str, Any] | None = None,
    build_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if job_type in {"buckling", "buckling_screening"} and not _experimental_buckling_enabled(ir or state.ir):
        return {
            "status": "disabled",
            "jobs": {},
            "categories": [
                {
                    "category": "scope_deferred",
                    "severity": "info",
                    "message": "Linear buckling is an experimental screening interface and is disabled by default for this paper scope.",
                }
            ],
        }
    build = build_report or build_l0(state, ir)
    if build["status"] == "blocked":
        return {"status": "blocked", "jobs": {}, "categories": [{"category": "build_blocked", "severity": "fatal", "message": "L0 build is blocked."}], "build": build}
    abaqus_available = Path(settings.abaqus_cmd).exists() or shutil.which(settings.abaqus_cmd) is not None
    if not abaqus_available:
        return {"status": "blocked", "jobs": {}, "categories": [{"category": "environment_blocked", "severity": "fatal", "message": f"Abaqus command not found: {settings.abaqus_cmd}"}]}
    requested = _requested_l0_job_types(state.ir) if job_type == "all" else (_normalize_job_type(job_type),)
    jobs = {}
    categories = []
    for current in requested:
        if current not in build["jobs"]:
            categories.append({"category": "job_type_invalid", "severity": "fatal", "message": current})
            continue
        jobs[current] = _run_single_abaqus_job(state, settings, build["jobs"][current]["path"], current, mode)
    fatal = any(item.get("status") == "blocked" for item in jobs.values()) or any(c["severity"] == "fatal" for c in categories)
    warning = any(item.get("status") == "warning" for item in jobs.values())
    return {"status": "blocked" if fatal else ("warning" if warning else "pass"), "jobs": jobs, "categories": categories}


def _run_single_abaqus_job(state: ProjectState, settings: Settings, inp_path: str, job_type: str, mode: str) -> dict[str, Any]:
    job_dir = state.root / "jobs" / job_type
    job_dir.mkdir(parents=True, exist_ok=True)
    source_inp = Path(inp_path)
    local_inp = job_dir / source_inp.name
    if source_inp.resolve() != local_inp.resolve():
        shutil.copy2(source_inp, local_inp)
    job_id = f"{state.project_id}_{state.ir.get('model_hash', 'nohash')}_{job_type}_{int(time.time())}"
    command = [
        settings.abaqus_cmd,
        f"job={job_id}",
        f"input={local_inp.name}",
        f"cpus={settings.abaqus_job_cpus}",
    ]
    # Abaqus 2020's default preprocessor memory cap can be below physical RAM.
    # The explicit value is opt-in, recorded in the command/status artifact, and
    # deliberately supplied by the campaign environment rather than inferred.
    requested_memory = os.getenv("WINDTOWER_ABAQUS_MEMORY", "").strip()
    if requested_memory:
        command.append(f"memory={requested_memory}")
    if mode == "datacheck":
        command.append("datacheck")
    command.append("interactive")
    status = {
        "job_id": job_id,
        "job_type": job_type,
        "mode": mode,
        "status": "running",
        "command": " ".join(command),
        "categories": [],
        "artifacts": {},
    }
    (job_dir / "job_status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    try:
        result = subprocess.run(
            command,
            cwd=job_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=settings.abaqus_job_timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        status["status"] = "blocked"
        status["categories"].append({"category": "timeout", "severity": "fatal", "message": "Abaqus job timed out."})
        (job_dir / "job_status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
        return status
    status["returncode"] = result.returncode
    log_text = (result.stdout or "") + "\n" + (result.stderr or "") + "\n" + _read_job_logs(job_dir, job_id)
    (job_dir / "run.log").write_text(log_text, encoding="utf-8", errors="ignore")
    state.artifacts[f"{job_type}_run.log"] = job_dir / "run.log"
    parsed = parse_abaqus_log(log_text, result.returncode)
    diagnostic_report = build_abaqus_diagnostic_report(log_text, result.returncode, job_type)
    status["categories"] = parsed
    status["diagnostic_report"] = diagnostic_report
    status["log_excerpt"] = _important_log_lines(log_text)
    status["status"] = "blocked" if any(item["severity"] == "fatal" for item in parsed) else ("warning" if any(item["severity"] == "warning" for item in parsed) else "pass")
    status["artifacts"][f"{job_type}_run.log"] = "run.log"
    for path in job_dir.glob(f"{job_id}.*"):
        key = f"{job_type}_{path.name}"
        state.artifacts[key] = path
        status["artifacts"][key] = path.name
    if mode == "run" and not _abaqus_run_completed(log_text, job_dir, job_id):
        status["status"] = "blocked"
        status["categories"].append(
            {
                "category": "analysis_incomplete",
                "severity": "fatal",
                "message": "Abaqus did not report successful analysis completion.",
            }
        )
    if mode == "run" and not any(path.suffix.lower() == ".odb" for path in job_dir.glob(f"{job_id}.*")):
        status["status"] = "blocked"
        status["categories"].append({"category": "odb_missing", "severity": "fatal", "message": "Abaqus run finished but ODB was not generated."})
    (job_dir / "job_status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    return status


def _abaqus_run_completed(log_text: str, job_dir: Path, job_id: str) -> bool:
    completion_pattern = re.compile(
        r"(THE ANALYSIS HAS COMPLETED SUCCESSFULLY|COMPLETED SUCCESSFULLY)",
        re.IGNORECASE,
    )
    if completion_pattern.search(log_text):
        return True
    for suffix in (".sta", ".msg", ".dat"):
        path = job_dir / f"{job_id}{suffix}"
        if path.is_file() and completion_pattern.search(
            path.read_text(encoding="utf-8", errors="ignore")
        ):
            return True
    return False


def _requested_l0_job_types(ir: dict[str, Any]) -> tuple[str, ...]:
    raw = ir.get("analysis_request", {}).get("analysis_types")
    if isinstance(raw, str):
        requested = [raw]
    elif isinstance(raw, (list, tuple)):
        requested = [str(item) for item in raw]
    else:
        requested = list(L0_JOB_TYPES)
    normalized = []
    for item in requested:
        current = _normalize_job_type(item)
        if current in L0_JOB_TYPES and current not in normalized:
            normalized.append(current)
        elif current == "buckling_screening" and _experimental_buckling_enabled(ir) and current not in normalized:
            normalized.append(current)
    return tuple(normalized or L0_JOB_TYPES)


def _normalize_job_type(job_type: str | None) -> str:
    if job_type in {"buckling", "linear_buckling"}:
        return "buckling_screening"
    if job_type in {"static", "static_strength"}:
        return "static_strength"
    if job_type == "frequency":
        return "modal"
    return str(job_type or "static_strength")


def _compiler_job_type(job_type: str) -> str:
    return "buckling" if job_type == "buckling_screening" else job_type


def _experimental_buckling_enabled(ir: dict[str, Any] | None) -> bool:
    if not isinstance(ir, dict):
        return False
    experimental = ir.get("experimental_analysis", {})
    return bool(experimental.get("enable_buckling_screening"))


def _inp_path(inp_dir: Path, project_id: str, ir: dict[str, Any], job_type: str) -> Path:
    model_hash = ir["model_hash"]
    if job_type == "static_strength":
        load_case = abaqus_name(ir.get("analysis_cases", {}).get("active_load_case") or "LC00_reference_excel", "LC00_reference_excel")
        return inp_dir / "static" / f"{project_id}_{load_case}_static.inp"
    if job_type == "modal":
        return inp_dir / "modal" / f"{project_id}_modal.inp"
    return inp_dir / "experimental_buckling" / f"{project_id}_{model_hash}_buckling_screening.inp"


def parse_abaqus_log(text: str, returncode: int) -> list[dict[str, str]]:
    categories = []
    has_license = bool(re.search(r"(license.*(error|denied|failed|not available|cannot|unable))|(licensed number.*exceeded)", text, re.IGNORECASE))
    if has_license:
        categories.append({"category": "license_blocked", "severity": "fatal", "message": "Abaqus license issue detected."})
    if _has_abaqus_fatal_error(text, returncode):
        categories.append({"category": "datacheck_failed", "severity": "fatal", "message": "Abaqus reported errors."})
    elif returncode != 0 and not has_license:
        categories.append({"category": "job_failed", "severity": "fatal", "message": f"Abaqus exited with return code {returncode}."})
    if re.search(r"\bWARNING\b", text, re.IGNORECASE):
        categories.append({"category": "abaqus_warning", "severity": "warning", "message": "Abaqus warnings were found in logs."})
    if not categories:
        categories.append({"category": "abaqus_completed", "severity": "info", "message": "Abaqus command completed without detected errors."})
    return categories


def _has_abaqus_fatal_error(text: str, returncode: int) -> bool:
    if _benign_post_input_processor_error(text, returncode):
        return False
    return bool(re.search(r"^\s*\*\*\*ERROR|Abaqus Error", text, re.IGNORECASE | re.MULTILINE)) or _has_nonzero_abaqus_error_summary(text)


def _benign_post_input_processor_error(text: str, returncode: int) -> bool:
    if returncode != 0:
        return False
    lower = text.lower()
    if "end analysis input file processor" not in lower and "end of user input processing" not in lower:
        return False
    error_lines = [raw.strip() for raw in text.splitlines() if re.search(r"^\s*\*\*\*error", raw, re.IGNORECASE)]
    if error_lines and any("sim database file cannot be opened" not in line.lower() for line in error_lines):
        return False
    if _has_nonzero_abaqus_error_summary(text):
        return False
    return "sim database file cannot be opened" in lower or "abaqus/analysis exited with errors" in lower


def _has_nonzero_abaqus_error_summary(text: str) -> bool:
    for raw in text.splitlines():
        match = re.search(r"\b(\d+)\s+ERROR MESSAGES?\b", raw, re.IGNORECASE)
        if match and int(match.group(1)) > 0:
            return True
    return False


def _important_log_lines(text: str, limit: int = 8) -> list[str]:
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if re.search(r"license|error|warning|fail|abort|odb|analysis input file processor", line, re.IGNORECASE):
            lines.append(line[:260])
        if len(lines) >= limit:
            break
    return lines


def _read_job_logs(job_dir: Path, job_id: str) -> str:
    pieces = []
    for suffix in (".dat", ".msg", ".sta"):
        path = job_dir / f"{job_id}{suffix}"
        if path.exists():
            pieces.append(path.read_text(errors="ignore"))
    return "\n".join(pieces)


def _find_job_artifact(state: ProjectState, job_type: str, suffix: str) -> Path | None:
    job_dir = state.root / "jobs" / job_type
    if not job_dir.exists():
        return None
    candidates = sorted(job_dir.glob(f"*{suffix}"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _write_l0_artifact(state: ProjectState, folder: str, name: str, data: dict[str, Any]) -> None:
    path = state.root / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, data)


def _write_parameter_consumption(state: ProjectState, ir: dict[str, Any]) -> None:
    report = ir.get("parameter_consumption") or legacy_consumption_report()
    _write_l0_artifact(state, "reports", "parameter_consumption.json", report)
    path = state.root / "reports" / "parameter_consumption.json"
    state.artifacts["reports/parameter_consumption.json"] = path
    state.artifacts["parameter_consumption.json"] = path


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _placeholder_stress_rows(windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "flange_id": window.get("flange_id"),
            "z_flange": window.get("z_flange"),
            "window_height": window.get("window_height"),
            "z_min": window.get("z_min"),
            "z_max": window.get("z_max"),
            "element_count": "pending_odb_extraction",
            "max_mises": "pending_odb_extraction",
            "mean_mises": "pending_odb_extraction",
            "p95_mises": "pending_odb_extraction",
            "p99_mises": "pending_odb_extraction",
            "max_element_label": "pending_odb_extraction",
            "max_element_centroid": "pending_odb_extraction",
        }
        for window in windows
    ] or [{"flange_id": "none", "status": "no_flange_windows_defined"}]


def _placeholder_door_rows(ir: dict[str, Any]) -> list[dict[str, Any]]:
    door = ir["semantic_ir"].get("door_openings", [{}])[0]
    return [{"door_enabled": door.get("enabled"), "max_mises": "pending_odb_extraction", "mean_mises": "pending_odb_extraction", "p95_mises": "pending_odb_extraction", "p99_mises": "pending_odb_extraction"}]


def _placeholder_modal_rows(odb: Path | None) -> list[dict[str, Any]]:
    return [{"mode": "", "frequency_hz": "pending_odb_extraction", "odb": str(odb) if odb else ""}]


def _placeholder_buckling_rows(odb: Path | None) -> list[dict[str, Any]]:
    return [{"mode": "", "buckling_factor": "pending_odb_extraction", "odb": str(odb) if odb else ""}]


def _placeholder_displacement_rows(odb: Path | None) -> list[dict[str, Any]]:
    return [{"metric": "top_displacement_mm", "max": "pending_odb_extraction", "odb": str(odb) if odb else ""}]


def _write_nset(f: Any, name: str, values: list[int], instance_name: str | None = None) -> None:
    suffix = f", instance={instance_name}" if instance_name else ""
    f.write(f"*Nset, nset={name}{suffix}\n")
    _write_wrapped(f, values)


def _write_elset(f: Any, name: str, values: list[int], instance_name: str | None = None) -> None:
    suffix = f", instance={instance_name}" if instance_name else ""
    f.write(f"*Elset, elset={name}{suffix}\n")
    _write_wrapped(f, values)


def _write_wrapped(f: Any, values: list[int]) -> None:
    values = sorted(set(values))
    if not values:
        f.write("\n")
        return
    for i in range(0, len(values), 16):
        f.write(", ".join(str(v) for v in values[i : i + 16]) + "\n")


def _distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def _quad_area(nodes: dict[int, tuple[float, float, float]], conn: list[int]) -> float:
    p = [nodes[nid] for nid in conn]
    return 0.5 * (_tri_cross_area(p[0], p[1], p[2]) + _tri_cross_area(p[0], p[2], p[3]))


def _tri_cross_area(a: tuple[float, float, float], b: tuple[float, float, float], c: tuple[float, float, float]) -> float:
    ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    ac = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    cross = (ab[1] * ac[2] - ab[2] * ac[1], ab[2] * ac[0] - ab[0] * ac[2], ab[0] * ac[1] - ab[1] * ac[0])
    return math.sqrt(sum(v * v for v in cross))


def _radius_at_z(sections: list[dict[str, Any]], z: float) -> float:
    for section in sections:
        if float(section["z_bottom_mm"]) <= z <= float(section["z_top_mm"]):
            span = float(section["z_top_mm"]) - float(section["z_bottom_mm"])
            ratio = (z - float(section["z_bottom_mm"])) / span if span else 0.0
            diameter = float(section["diameter_bottom_mm"]) + ratio * (float(section["diameter_top_mm"]) - float(section["diameter_bottom_mm"]))
            return 0.5 * diameter
    return 0.0
