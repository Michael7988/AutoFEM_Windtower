from __future__ import annotations

from copy import deepcopy

import pytest

from windtower_backend.modeling import ProjectState
from windtower_backend.modeling.implementation import geometry_from_ir
from windtower_backend.parameter_ir import (
    ParameterChange,
    apply_changes,
    instruction_changes,
    parameter_set_from_ir,
    validate_parameter_set,
)
from windtower_backend.services.parameters import (
    ConfirmPreviewRequest,
    ParameterService,
    PreviewRequest,
)


def _ir() -> dict:
    return {
        "project_meta_ir": {"project_id": "wt-test", "source_file": "synthetic.xlsx"},
        "semantic_ir": {
            "tower_sections": [
                {
                    "z_bottom_mm": 0.0,
                    "z_top_mm": 50_000.0,
                    "diameter_bottom_mm": 8_000.0,
                    "diameter_top_mm": 6_000.0,
                    "thickness_mm": 40.0,
                },
                {
                    "z_bottom_mm": 50_000.0,
                    "z_top_mm": 100_000.0,
                    "diameter_bottom_mm": 6_000.0,
                    "diameter_top_mm": 4_000.0,
                    "thickness_mm": 25.0,
                },
            ],
            "door_openings": [
                {
                    "enabled": True,
                    "width_mm": 1_500.0,
                    "height_mm": 3_000.0,
                    "bottom_z_mm": 500.0,
                    "theta_center_deg": 0.0,
                }
            ],
            "materials": [
                {
                    "name": "STEEL",
                    "elastic_modulus_mpa": 206_000.0,
                    "poisson_ratio": 0.3,
                    "density": 7.85e-9,
                    "yield_strength_mpa": 355.0,
                }
            ],
            "loads_inventory": {
                "top_mass_tonne": 600.0,
                "center_of_mass_offset_mm": 0.0,
                "top_horizontal_force_n": 800_000.0,
                "top_force_direction": "+X",
                "wind_pressure_mpa": 0.0012,
            },
        },
        "mesh_and_set_ir": {
            "global_size_mm": 650.0,
            "door_size_mm": 120.0,
            "patch_margin_u_mm": 1_450.0,
            "patch_margin_z_mm": 900.0,
            "hole_layers": 32,
        },
        "abaqus_assembly_ir": {
            "elements": [{"type": "S4R"}],
            "boundary_conditions": [{"name": "BASE_FIXED"}],
        },
        "analysis_cases": {
            "static_strength": {"enabled": True},
            "modal": {"enabled": True, "modes": 10},
        },
    }


def test_parameter_set_is_single_formal_interface() -> None:
    parameter_set = parameter_set_from_ir("wt-test", _ir())
    assert parameter_set.structure_family == "wind_tower_shell_with_door"
    assert parameter_set.revision == 1
    assert validate_parameter_set(parameter_set)["status"] == "pass"


def test_missing_active_center_of_mass_offset_is_pending_zero_candidate() -> None:
    ir = _ir()
    del ir["semantic_ir"]["loads_inventory"]["center_of_mass_offset_mm"]
    parameter_set = parameter_set_from_ir("wt-test", ir)
    record = parameter_set.parameters["loads.center_of_mass_offset_mm"]
    assert record.value == 0.0
    assert record.confirmation_status == "pending"
    assert record.requires_confirmation is True
    issues = validate_parameter_set(parameter_set)["issues"]
    issue = next(item for item in issues if item["parameter_key"] == record.key)
    assert issue["resolution_action"] == "confirm_parameter"


def test_center_of_mass_offset_is_inactive_without_top_mass() -> None:
    ir = _ir()
    ir["semantic_ir"]["loads_inventory"]["top_mass_tonne"] = 0.0
    del ir["semantic_ir"]["loads_inventory"]["center_of_mass_offset_mm"]
    parameter_set = parameter_set_from_ir("wt-test", ir)
    record = parameter_set.parameters["loads.center_of_mass_offset_mm"]
    assert record.confirmation_status == "not_required"
    assert validate_parameter_set(parameter_set)["status"] == "pass"


def test_confirm_keys_can_confirm_unchanged_pending_candidate(tmp_path, monkeypatch) -> None:
    ir = _ir()
    del ir["semantic_ir"]["loads_inventory"]["center_of_mass_offset_mm"]
    state = ProjectState(
        project_id="wt-test",
        root=tmp_path,
        excel_path=tmp_path / "中文参数.xlsx",
        ir=ir,
        artifacts={},
    )
    monkeypatch.setattr("windtower_backend.services.parameters.get_state", lambda _project_id: state)
    service = ParameterService()
    current = service.get("wt-test")["parameter_set"]
    preview = service.preview("wt-test", PreviewRequest(
        base_revision=current["revision"],
        confirm_keys=["loads.center_of_mass_offset_mm"],
    ))
    assert preview["validation"]["status"] == "pass"
    confirmed = service.confirm("wt-test", ConfirmPreviewRequest(
        preview_id=preview["preview_id"],
        base_revision=preview["base_revision"],
        operator="测试工程师",
    ))
    record = confirmed["parameter_set"]["parameters"]["loads.center_of_mass_offset_mm"]
    assert record["value"] == 0.0
    assert record["confirmation_status"] == "confirmed"


def test_failed_change_does_not_mutate_parent() -> None:
    parameter_set = parameter_set_from_ir("wt-test", _ir())
    before = deepcopy(parameter_set.model_dump())
    candidate, _diff, validation = apply_changes(
        parameter_set,
        [ParameterChange(key="mesh.door_size_mm", value=900)],
        increment_revision=False,
    )
    assert validation["status"] == "blocked"
    assert candidate.parameters["mesh.door_size_mm"].value == 900
    assert parameter_set.model_dump() == before


def test_confirmed_change_increments_revision_once() -> None:
    parameter_set = parameter_set_from_ir("wt-test", _ir())
    candidate, diff, validation = apply_changes(
        parameter_set,
        [ParameterChange(key="geometry.door.width_mm", value=1600)],
        increment_revision=True,
    )
    assert validation["status"] == "pass"
    assert candidate.revision == 2
    assert diff[0]["old_value"] == 1500
    assert parameter_set.revision == 1


def test_change_unit_is_normalized_to_formal_unit() -> None:
    parameter_set = parameter_set_from_ir("wt-test", _ir())
    candidate, diff, validation = apply_changes(
        parameter_set,
        [ParameterChange(key="geometry.door.width_mm", value=1.6, unit="m")],
        increment_revision=False,
    )
    assert validation["status"] == "pass"
    assert candidate.parameters["geometry.door.width_mm"].value == 1600
    assert diff[0]["unit"] == "mm"


def test_incompatible_change_unit_is_rejected() -> None:
    parameter_set = parameter_set_from_ir("wt-test", _ir())
    with pytest.raises(ValueError, match="不兼容"):
        apply_changes(
            parameter_set,
            [ParameterChange(key="geometry.door.width_mm", value=1, unit="MPa")],
            increment_revision=False,
        )


def test_instruction_supports_relative_parameter_change() -> None:
    parameter_set = parameter_set_from_ir("wt-test", _ir())
    changes = instruction_changes("门洞宽度增加5%", parameter_set)
    assert changes == [ParameterChange(key="geometry.door.width_mm", value=1575.0)]


def test_unknown_parameter_is_rejected() -> None:
    parameter_set = parameter_set_from_ir("wt-test", _ir())
    with pytest.raises(ValueError, match="不允许修改"):
        apply_changes(
            parameter_set,
            [ParameterChange(key="abaqus.keyword", value="*NODE")],
            increment_revision=False,
        )


def test_stale_preview_cannot_overwrite_new_revision(
    tmp_path,
    monkeypatch,
) -> None:
    state = ProjectState(
        project_id="wt-test",
        root=tmp_path,
        excel_path=tmp_path / "source.xlsx",
        ir=_ir(),
        artifacts={},
    )
    monkeypatch.setattr(
        "windtower_backend.services.parameters.get_state",
        lambda _project_id: state,
    )
    service = ParameterService()
    preview = service.preview(
        "wt-test",
        PreviewRequest(
            base_revision=1,
            changes=[
                ParameterChange(key="geometry.door.width_mm", value=1600),
            ],
        ),
    )
    current = parameter_set_from_ir("wt-test", state.ir, revision=2)
    service._save(tmp_path, current)

    with pytest.raises(ValueError, match="版本"):
        service.confirm(
            "wt-test",
            ConfirmPreviewRequest(
                preview_id=preview["preview_id"],
                base_revision=1,
            ),
        )
    assert service.get("wt-test")["parameter_set"]["revision"] == 2


def test_confirmed_semantic_sections_are_the_geometry_authority() -> None:
    ir = _ir()
    ir["excel_facts_ir"] = {
        "tower_profile_points": [
            {"z_mm": 0, "diameter_mm": 9999, "thickness_mm": 99},
            {"z_mm": 100000, "diameter_mm": 9999, "thickness_mm": 99},
        ]
    }
    tower, _flanges, _door = geometry_from_ir(ir)
    assert len(tower) == 3
    assert tower[0]["diameter_mm"] == 8000
    assert tower[-1]["diameter_mm"] == 4000
    assert all(item["diameter_mm"] != 9999 for item in tower)
