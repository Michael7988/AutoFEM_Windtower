from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings, load_settings
from ..l0 import (
    build_l0,
    datacheck_l0,
    mesh_l0,
    postprocess_l0,
    run_l0,
    validate_l0,
)
from ..modeling import ProjectState, create_project, get_state


@dataclass(frozen=True)
class SingleL0WorkflowOptions:
    excel_path: Path
    job_type: str = "all"
    run_abaqus: bool = False
    extract_odb: bool = True
    project_id: str | None = None


def run_single_l0_workflow(options: SingleL0WorkflowOptions, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or load_settings()
    excel_path = Path(options.excel_path)
    project = create_project(excel_path.name, excel_path.read_bytes())
    state = get_state(str(project["project_id"]))
    return run_project_l0_workflow(state, options, settings)


def run_project_l0_workflow(state: ProjectState, options: SingleL0WorkflowOptions, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or load_settings()
    steps = {
        "parameter_gate": validate_l0(state),
        "mesh_gate": mesh_l0(state),
    }
    steps["build"] = build_l0(state)
    if steps["build"]["status"] == "blocked":
        return _workflow_result(state, steps, "blocked")
    if options.run_abaqus:
        steps["datacheck"] = datacheck_l0(
            state,
            settings,
            job_type=options.job_type,
        )
        steps["solve"] = run_l0(state, settings, job_type=options.job_type)
        if options.extract_odb:
            steps["postprocess"] = postprocess_l0(state, settings)
    else:
        steps["datacheck"] = {"status": "skipped"}
        steps["solve"] = {"status": "skipped"}
        steps["postprocess"] = {"status": "skipped"}
    blocked = any(step.get("status") == "blocked" for step in steps.values())
    return _workflow_result(state, steps, "blocked" if blocked else "pass")


def _workflow_result(
    state: ProjectState,
    steps: dict[str, dict[str, Any]],
    status: str,
) -> dict[str, Any]:
    return {
        "schema_version": "single_l0_run_v1",
        "project_id": state.project_id,
        "status": status,
        "stages": steps,
        "artifacts": {
            name: str(path)
            for name, path in sorted(state.artifacts.items())
        },
    }


def copy_workflow_package(project_root: Path, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    target = out / project_root.name
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(project_root, target)
    return target
