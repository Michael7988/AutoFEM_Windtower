"""Explicit public interface for the deterministic S4R adapter."""
from .compiler import abaqus_name, build_l0, write_l0_inp
from .contracts import (
    build_derived_geometry,
    build_load_contract,
    build_model_hash,
    build_source_trace,
    enrich_l0_ir,
    summarize_load_readiness,
)
from .execution import (
    datacheck_l0,
    get_l0_submission_status,
    job_status_l0,
    parse_abaqus_log,
    run_l0,
    run_l0_job,
    submit_l0,
)
from .mesh import (
    build_l0_mesh_report,
    build_l0_sets,
    compute_mesh_quality,
    mesh_l0,
    mesh_pyvista_preview_l0,
    mesh_vtk_preview_l0,
    open_pyvista_desktop_l0,
)
from .postprocess import postprocess_l0, results_l0
from .validation import static_check_l0_inp, validate_l0, validate_l0_ir

__all__ = [
    "abaqus_name",
    "build_derived_geometry",
    "build_l0",
    "build_l0_mesh_report",
    "build_l0_sets",
    "build_load_contract",
    "build_model_hash",
    "build_source_trace",
    "compute_mesh_quality",
    "datacheck_l0",
    "enrich_l0_ir",
    "get_l0_submission_status",
    "job_status_l0",
    "mesh_l0",
    "mesh_pyvista_preview_l0",
    "mesh_vtk_preview_l0",
    "open_pyvista_desktop_l0",
    "parse_abaqus_log",
    "postprocess_l0",
    "results_l0",
    "run_l0",
    "run_l0_job",
    "static_check_l0_inp",
    "submit_l0",
    "summarize_load_readiness",
    "validate_l0",
    "validate_l0_ir",
    "write_l0_inp",
]
