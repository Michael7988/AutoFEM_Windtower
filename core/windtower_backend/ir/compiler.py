from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from ..l0 import build_l0
from ..modeling import ProjectState
from ..validation import validate_ir_layers
from .models import IrBundle


def compile_ir_to_s4r_inp(ir_bundle: IrBundle, out_dir: Path, job_type: str = "all") -> dict[str, Any]:
    normalized_job = _normalize_job_type(job_type)
    if normalized_job == "metadata_only":
        return {
            "status": "disabled",
            "reason": "Batch audit requested facts/IR/validation artifacts only; calculation INP is written by the L0 compiler stage.",
            "jobs": {},
            "requested_job_type": job_type,
        }
    if normalized_job == "buckling_screening":
        return {
            "status": "disabled",
            "reason": "Linear buckling is an experimental screening interface and is disabled by default for this paper scope.",
            "jobs": {},
            "requested_job_type": job_type,
            "experimental_buckling_status": "disabled_scope_deferred",
        }
    validation = validate_ir_layers(ir_bundle)
    if validation.status == "blocked":
        return {
            "status": "blocked",
            "reason": "IR validation is blocked; final INP files were not generated.",
            "validation": validation.model_dump(),
            "jobs": {},
        }
    root = out_dir.parent if out_dir.name == "inp" else out_dir
    root.mkdir(parents=True, exist_ok=True)
    legacy_ir = deepcopy(ir_bundle.legacy_ir)
    legacy_ir["analysis_request"] = {"analysis_types": list(_requested_analysis_types(normalized_job))}
    legacy_ir["experimental_analysis"] = {"enable_buckling_screening": False}
    state = ProjectState(project_id=ir_bundle.project_id, root=root, excel_path=root / ir_bundle.source_file, ir=legacy_ir, artifacts={})
    report = build_l0(state)
    if normalized_job != "all":
        jobs = report.get("jobs", {})
        report["jobs"] = {normalized_job: jobs.get(normalized_job)} if normalized_job in jobs else {}
        report["requested_job_type"] = normalized_job
    report["validation"] = validation.model_dump()
    report["compiler_boundary"] = "IR -> deterministic L0 S4R shell INP; Abaqus execution is not invoked."
    return report


def _normalize_job_type(job_type: str | None) -> str:
    if not job_type or job_type == "all":
        return "all"
    if job_type in {"static", "static_strength"}:
        return "static_strength"
    if job_type in {"frequency", "modal"}:
        return "modal"
    if job_type in {"buckling", "linear_buckling", "buckling_screening"}:
        return "buckling_screening"
    if job_type in {"metadata_only", "none", "audit_only"}:
        return "metadata_only"
    return "all"


def _requested_analysis_types(job_type: str) -> tuple[str, ...]:
    if job_type == "static_strength":
        return ("static_strength",)
    if job_type == "modal":
        return ("modal",)
    return ("static_strength", "modal")
