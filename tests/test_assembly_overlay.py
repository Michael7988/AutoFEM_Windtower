from __future__ import annotations

import json
from pathlib import Path

from windtower_backend.config import EXAMPLE_ROOT
from windtower_backend.l0 import mesh_pyvista_preview_l0
from windtower_backend.modeling import create_project, get_state
from windtower_backend.services.parameters import ConfirmPreviewRequest, ParameterService, PreviewRequest


ROOT = Path(__file__).resolve().parents[1]
TEN_MW = EXAMPLE_ROOT / "synthetic_standard_tower.xlsx"


def test_pyvista_preview_writes_assembly_overlay_artifact() -> None:
    project = create_project(
        TEN_MW.name,
        TEN_MW.read_bytes(),
        "assembly overlay regression",
    )

    assert project.get("stage") != "input_blocked"
    parameters = ParameterService()
    current = parameters.get(project["project_id"])["parameter_set"]
    pending = [
        key for key, record in current["parameters"].items()
        if record["value"] is not None and (record["confirmation_status"] == "pending" or record["requires_confirmation"])
    ]
    preview_change = parameters.preview(project["project_id"], PreviewRequest(
        base_revision=current["revision"],
        confirm_keys=pending,
    ))
    parameters.confirm(project["project_id"], ConfirmPreviewRequest(
        preview_id=preview_change["preview_id"],
        base_revision=preview_change["base_revision"],
        operator="test_engineer",
    ))
    state = get_state(project["project_id"])
    preview = mesh_pyvista_preview_l0(state)

    assert preview["assembly_overlay_name"] == "mesh/assembly_overlay.json"
    overlay = preview["assembly_overlay"]
    assert overlay["schema_version"] == "assembly_overlay_v1"
    assert overlay["sets"]["boundary"][0]["name"] == "BASE_FIXED"
    assert overlay["reference_points"][0]["name"] == "TOP_RP_NSET"
    assert overlay["couplings"][0]["name"] == "TOP_RP_DISTRIBUTING_COUPLING"
    assert any(item["name"] == "TOP_LOAD_NODES" for item in overlay["sets"]["load_targets"])

    overlay_path = state.root / preview["assembly_overlay_name"]
    stored = json.loads(overlay_path.read_text(encoding="utf-8"))
    assert stored["mesh_counts"]["nodes"] > 0
    assert stored["mesh_counts"]["elements"] > 0
