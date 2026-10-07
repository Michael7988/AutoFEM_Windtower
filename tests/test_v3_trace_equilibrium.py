from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path


from windtower_backend.capability_profile import (
    CAPABILITY_HASH_SHA256,
    CANONICAL_DEFAULTS,
    current_capability,
)
from windtower_backend.config import EXAMPLE_ROOT
from windtower_backend.fea_agent.equilibrium import evaluate_equilibrium
from windtower_backend.l0 import build_l0, enrich_l0_ir
from windtower_backend.modeling import ProjectState
from windtower_backend.parameter_ir import (
    ParameterRecord,
    apply_parameter_set_to_ir,
    parameter_set_from_ir,
    validate_parameter_set,
)


def _state(root: Path, project_id: str = "v3-trace") -> ProjectState:
    ir = json.loads((EXAMPLE_ROOT / "synthetic_l0_ir.json").read_text(encoding="utf-8"))
    ir["project_meta_ir"]["project_id"] = project_id
    return ProjectState(
        project_id=project_id,
        root=root,
        excel_path=EXAMPLE_ROOT / "synthetic_mechanical_interface_FEM.xls",
        ir=ir,
        artifacts={},
    )


def test_capability_defaults_have_one_stable_source() -> None:
    capability = current_capability()
    assert capability["capability_hash_sha256"] == CAPABILITY_HASH_SHA256
    assert len(CAPABILITY_HASH_SHA256) == 64
    assert capability["default_fem_parameters"] == CANONICAL_DEFAULTS
    assert CANONICAL_DEFAULTS["mesh.patch_margin_u_mm"] == 1450.0
    assert CANONICAL_DEFAULTS["mesh.patch_margin_z_mm"] == 900.0
    assert CANONICAL_DEFAULTS["analysis.modal_modes"] == 20


def test_unknown_parameter_is_blocked() -> None:
    state = _state(Path("unused"))
    parameter_set = parameter_set_from_ir("v3", state.ir)
    parameter_set.parameters["unknown.experimental"] = ParameterRecord(
        key="unknown.experimental", label="unknown", group="analysis", value=1
    )
    result = validate_parameter_set(parameter_set)
    assert result["status"] == "blocked"
    assert any(item["code"] == "unknown_parameter" for item in result["issues"])


def test_door_disabled_consumption_is_not_applicable(tmp_path: Path) -> None:
    state = _state(tmp_path / "disabled")
    parameter_set = parameter_set_from_ir(state.project_id, state.ir)
    parameter_set.parameters["geometry.door.enabled"].value = False
    state.ir = apply_parameter_set_to_ir(parameter_set, state.ir)
    enriched = enrich_l0_ir(state)
    entries = enriched["parameter_consumption"]["parameters"]
    assert entries["geometry.door.width_mm"]["activity"] == "not_applicable"
    assert entries["mesh.door_size_mm"]["consumption_status"] == "not_applicable"


def test_build_hashes_and_shell_orientation_are_closed(tmp_path: Path) -> None:
    state = _state(tmp_path / "build")
    first = build_l0(state)
    second_state = _state(tmp_path / "repeat", state.project_id)
    second = build_l0(second_state)
    assert first["semantic_hash"] == second["semantic_hash"]
    assert first["model_hash_sha256"] == second["model_hash_sha256"]
    assert all(
        first["jobs"][name]["normalized_inp_sha256"]
        == second["jobs"][name]["normalized_inp_sha256"]
        for name in first["jobs"]
    )
    metrics = first["mesh_report"]["quality_report"]["metrics"]
    assert metrics["normal_inconsistency_count"] == 0
    assert metrics["inverted_element_count"] == 0


def test_linear_and_nonlinear_strain_output_are_mutually_exclusive(tmp_path: Path) -> None:
    linear_state = _state(tmp_path / "linear", "linear")
    linear_state.ir.setdefault("analysis_cases", {}).setdefault("static_strength", {})["nlgeom"] = False
    linear = build_l0(linear_state)
    linear_text = Path(linear["jobs"]["static_strength"]["path"]).read_text(encoding="utf-8")
    assert "S, E, SF, NFORC" in linear_text
    assert "S, LE, SF, NFORC" not in linear_text

    nonlinear_state = _state(tmp_path / "nonlinear", "nonlinear")
    nonlinear_state.ir.setdefault("analysis_cases", {}).setdefault("static_strength", {})["nlgeom"] = True
    nonlinear = build_l0(nonlinear_state)
    nonlinear_text = Path(nonlinear["jobs"]["static_strength"]["path"]).read_text(encoding="utf-8")
    assert "S, LE, SF, NFORC" in nonlinear_text
    assert "S, E, SF, NFORC" not in nonlinear_text


def test_equilibrium_fixture_passes_and_missing_rm_blocks() -> None:
    frozen = {"STEP-1": {"force_n": [10.0, 0.0, -20.0], "moment_nmm": [0.0, 50.0, 0.0]}}
    reactions = {"steps": {"STEP-1": {
        "force_n": [-10.0, 0.0, 20.0],
        "moment_nmm": [0.0, -50.0, 0.0],
        "rf_available": True,
        "rm_available": True,
    }}}
    assert evaluate_equilibrium(frozen, reactions)["status"] == "pass"
    missing = deepcopy(reactions)
    missing["steps"]["STEP-1"]["rm_available"] = False
    assert evaluate_equilibrium(frozen, missing)["status"] == "blocked"
