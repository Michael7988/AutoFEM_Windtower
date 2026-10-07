from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

from windtower_backend.config import EXAMPLE_ROOT, load_settings
from windtower_backend.importers.windtower_adapter import parse_windtower_workbook
from windtower_backend.l0 import get_l0_submission_status, postprocess_l0, submit_l0
from windtower_backend.modeling import ProjectState, build_ir


def _sample_state(tmp_path: Path, project_id: str = "wt-l0-test") -> ProjectState:
    workbook = sorted(EXAMPLE_ROOT.glob("*FEM*.xls"))[0]
    parsed = parse_windtower_workbook(workbook)
    ir = build_ir(
        project_id,
        workbook.name,
        parsed["tower"],
        parsed["flanges"],
        parsed["door"],
        parsed["fem_params"],
        import_meta=parsed,
    )
    return ProjectState(project_id=project_id, root=tmp_path, excel_path=workbook, ir=ir, artifacts={})


def test_submit_l0_writes_pollable_environment_blocked_status(tmp_path: Path) -> None:
    state = _sample_state(tmp_path, "wt-submit-test")
    settings = replace(load_settings(), abaqus_cmd="__missing_abaqus_for_test__")

    response = submit_l0(state, settings, job_type="all", run_postprocess=False)
    status_path = tmp_path / "jobs" / "submissions" / f"{response['submission_id']}.json"

    # Model generation happens inside the worker before the deliberately missing
    # solver command is classified. Coverage instrumentation can make that work
    # exceed five seconds on slower Windows hosts, so wait for the durable terminal
    # state instead of coupling the assertion to normal interpreter speed.
    deadline = time.monotonic() + 30.0
    status = get_l0_submission_status(state, response["submission_id"])
    while status.get("status") in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.1)
        status = get_l0_submission_status(state, response["submission_id"])

    assert status_path.exists()
    assert status["status"] == "blocked"
    assert any(item.get("category") == "environment_blocked" for item in status.get("categories", []))
    assert "run" in status


def test_postprocess_merges_static_field_data_into_odb_result_mesh(tmp_path: Path) -> None:
    state = _sample_state(tmp_path, "wt-odb-mesh-test")
    results_dir = tmp_path / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "static_field_data.json").write_text(
        json.dumps(
            {
                "status": "pass",
                "source": "unit_test",
                "scalars": {
                    "S_Mises_by_element": {"1": 42.0},
                    "U_mag_by_node": {"1": 3.0},
                },
                "vectors": {"U_by_node": {"1": [1.0, 2.0, 2.0]}},
                "warnings": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    summary = postprocess_l0(state)
    mesh = json.loads((results_dir / "odb_result_mesh.json").read_text(encoding="utf-8"))

    assert summary["status"] == "warning"
    assert summary["artifacts"]["odb_result_mesh"] == "odb_result_mesh.json"
    assert "S_Mises" in mesh["scalar_arrays"]
    assert "U_mag" in mesh["scalar_arrays"]
    assert mesh["summary"]["element_count"] > 0
    assert mesh["summary"]["node_count"] > 0
    assert mesh["summary"]["max_mises"] == 42.0
    assert mesh["summary"]["max_u_mag"] == 3.0
    assert mesh["deformed_points"][0] != mesh["points"][0]
