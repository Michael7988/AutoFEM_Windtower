from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from windtower_backend.config import EXAMPLE_ROOT
from windtower_backend.l0 import build_l0, enrich_l0_ir, postprocess_l0
from windtower_backend.modeling import ProjectState
from windtower_backend.output_contract import (
    OUTPUT_CONTRACT_SCHEMA_VERSION,
    normalize_output_contract,
    resolved_output_matrix,
    validate_output_contract,
)


def _sample_state(root: Path, project_id: str = "wt-output-contract") -> ProjectState:
    ir = json.loads((EXAMPLE_ROOT / "synthetic_l0_ir.json").read_text(encoding="utf-8"))
    ir["project_meta_ir"]["project_id"] = project_id
    workbook = EXAMPLE_ROOT / "synthetic_mechanical_interface_FEM.xls"
    return ProjectState(project_id=project_id, root=root, excel_path=workbook, ir=ir, artifacts={})


def test_legacy_field_list_is_promoted_deterministically() -> None:
    first = normalize_output_contract({"field": ["U", "RF", "S", "E"]})
    second = normalize_output_contract({"field": ["U", "RF", "S", "E"]})

    assert first == second
    assert first["schema_version"] == OUTPUT_CONTRACT_SCHEMA_VERSION
    assert first["profile"] == "global_to_local"
    assert first["source"] == "legacy_promoted"
    assert first["legacy_requested_variables"] == ["U", "RF", "S", "E"]


def test_profiles_resolve_nforc_only_for_static_general() -> None:
    standard = resolved_output_matrix(normalize_output_contract({"profile": "standard"}))
    global_to_local = resolved_output_matrix(normalize_output_contract({"profile": "global_to_local"}))

    assert "NFORC" not in standard["static_general"]["field"]["element"][0]["variables"]
    assert "NFORC" in global_to_local["static_general"]["field"]["element"][0]["variables"]
    assert "NFORC" not in global_to_local["frequency"]["field"]["element"][0]["variables"]
    assert "NFORC" not in global_to_local["buckling"]["field"]["element"][0]["variables"]
    assert global_to_local["static_general"]["field"]["node"][0]["variables"][:2] == ["U", "UR"]
    assert global_to_local["frequency"]["field"]["node"][0]["variables"] == ["U", "UR"]


def test_invalid_contract_is_blocked_instead_of_silently_downgraded() -> None:
    contract = normalize_output_contract({"profile": "unsupported", "history_frequency": 0})
    errors = validate_output_contract(contract)

    assert {item["field"] for item in errors} == {
        "model_spec_ir.outputs.profile",
        "model_spec_ir.outputs.history_frequency",
    }


def test_output_contract_changes_model_hash(tmp_path: Path) -> None:
    state = _sample_state(tmp_path / "hash")
    standard_ir = deepcopy(state.ir)
    standard_ir["model_spec_ir"]["outputs"] = normalize_output_contract({"profile": "standard"})
    global_ir = deepcopy(state.ir)
    global_ir["model_spec_ir"]["outputs"] = normalize_output_contract({"profile": "global_to_local"})

    assert enrich_l0_ir(state, standard_ir)["model_hash"] != enrich_l0_ir(state, global_ir)["model_hash"]


def test_unknown_nforc_region_blocks_ir_validation(tmp_path: Path) -> None:
    state = _sample_state(tmp_path / "bad-region")
    state.ir["model_spec_ir"]["outputs"] = normalize_output_contract(
        {"profile": "global_to_local", "nforc_region": "NOT_A_REAL_ELSET"}
    )

    enriched = enrich_l0_ir(state)
    assert enriched["validation_report"]["blocked"] is True
    assert any(item.get("code") == "OUTPUT_REGION_NOT_FOUND" for item in enriched["validation_report"]["errors"])


def test_compiler_writes_step_specific_output_requests_and_audit(tmp_path: Path) -> None:
    state = _sample_state(tmp_path / "build")
    report = build_l0(state)

    assert report["status"] == "ready"
    assert report["output_contract"]["profile"] == "global_to_local"
    assert report["output_request_audit"]["validation_errors"] == []
    assert all(all(job["checks"].values()) for job in report["jobs"].values())

    static_text = Path(report["jobs"]["static_strength"]["path"]).read_text(encoding="utf-8")
    modal_text = Path(report["jobs"]["modal"]["path"]).read_text(encoding="utf-8")
    prestress, frequency = modal_text.split("*End Step", 1)

    assert static_text.count("*Output, history, frequency=1") == 2
    assert "U, UR, RF, RM" in static_text
    assert "CF, CM" not in static_text
    assert static_text.count("NFORC") == 2
    assert "*Energy Output" in static_text
    assert "*Output, history, frequency=1" in prestress
    assert "NFORC" in prestress
    assert "NFORC" not in frequency
    assert "S, E" in frequency
    assert "U, UR" in frequency
    assert "*Output, field, frequency=" not in frequency
    assert "*Modal Output" not in modal_text


def test_same_ir_compiles_to_byte_identical_inp(tmp_path: Path) -> None:
    first_state = _sample_state(tmp_path / "run-00", "wt-repeatable")
    reports = [build_l0(first_state)]
    for index in range(1, 10):
        state = ProjectState(
            project_id=first_state.project_id,
            root=tmp_path / f"run-{index:02d}",
            excel_path=first_state.excel_path,
            ir=deepcopy(first_state.ir),
            artifacts={},
        )
        reports.append(build_l0(state))
    for job_type in ("static_strength", "modal"):
        generated = {
            Path(report["jobs"][job_type]["path"]).read_bytes()
            for report in reports
        }
        assert len(generated) == 1


def test_postprocess_exposes_pending_output_availability_report(tmp_path: Path) -> None:
    state = _sample_state(tmp_path / "postprocess")
    summary = postprocess_l0(state)
    report_path = state.root / "results" / "output_availability_report.json"

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert summary["artifacts"]["output_availability_report"] == "output_availability_report.json"
    assert report["schema_version"] == "abaqus_output_availability_v1"
    assert report["status"] == "pending_odb_extraction"
    assert report["requested_output_matrix"]["static_general"]["field"]["element"][0]["variables"][-1] == "NFORC"
