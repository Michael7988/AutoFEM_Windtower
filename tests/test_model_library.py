from __future__ import annotations

from pathlib import Path

from windtower_backend.model_library import ModelLibraryService
from windtower_backend.modeling import ProjectState
from windtower_backend.parameter_ir import parameter_set_from_ir


def test_project_model_is_explicitly_published_as_immutable_bundle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "project"
    inp = project_root / "inp" / "static" / "tower.inp"
    inp.parent.mkdir(parents=True)
    inp.write_text("*Heading\n*Element, type=S4R\n1,1,2,3,4\n", encoding="utf-8")
    parameter_set = parameter_set_from_ir("wt-library", _ir())
    (project_root / "parameter_set.json").write_text(
        parameter_set.model_dump_json(indent=2),
        encoding="utf-8",
    )
    state = ProjectState(
        project_id="wt-library",
        root=project_root,
        excel_path=project_root / "source.xlsx",
        ir=_ir(),
        artifacts={inp.name: inp},
    )
    monkeypatch.setattr("windtower_backend.model_library.get_state", lambda _project_id: state)
    library = ModelLibraryService(tmp_path / "library")

    first = library.publish_project("wt-library", label="审查通过模型", operator="engineer")
    second = library.publish_project("wt-library", label="不同显示名", operator="engineer")
    entry = library.get_entry(first["model_id"])

    assert first["model_id"] == second["model_id"]
    assert first["validation_level"] == "engineer_reviewed"
    assert entry["parameter_set"].revision == 1
    assert entry["reference_path"].name == "reference.inp"
    assert library.list_entries()["items"] == [first]


def test_historical_bundle_requires_parameter_gate_and_explicit_validation_level(
    tmp_path: Path,
) -> None:
    parameter_set = parameter_set_from_ir("wt-import", _ir())
    parameter_path = tmp_path / "parameter_set.json"
    parameter_path.write_text(parameter_set.model_dump_json(indent=2), encoding="utf-8")
    inp_path = tmp_path / "reference.inp"
    inp_path.write_text("*Heading\n*Element, type=S4R\n1,1,2,3,4\n", encoding="utf-8")
    library = ModelLibraryService(tmp_path / "library")

    imported = library.import_bundle(
        parameter_path,
        inp_path,
        label="历史复核模型",
        operator="engineer",
        validation_level="datacheck_passed",
    )

    assert imported["validation_level"] == "datacheck_passed"
    assert library.get_entry(imported["model_id"])["parameter_set"].project_id == "wt-import"
    recommendations = library.recommend({
        "height_mm": 30_000.0,
        "base_diameter_mm": 4_500.0,
        "top_diameter_mm": 3_200.0,
        "door_enabled": True,
    }, capability_id=parameter_set.capability_id)
    assert recommendations[0]["model_id"] == imported["model_id"]
    assert recommendations[0]["similarity_score"] == 1.0


def _ir() -> dict:
    return {
        "project_meta_ir": {"project_id": "wt-library", "source_file": "fixture.json"},
        "semantic_ir": {
            "tower_sections": [{
                "z_bottom_mm": 0.0,
                "z_top_mm": 30_000.0,
                "diameter_bottom_mm": 4_500.0,
                "diameter_top_mm": 3_200.0,
                "thickness_mm": 28.0,
            }],
            "door_openings": [{
                "enabled": True,
                "width_mm": 1_200.0,
                "height_mm": 2_300.0,
                "bottom_z_mm": 300.0,
                "theta_center_deg": 0.0,
            }],
            "materials": [{
                "name": "STEEL",
                "elastic_modulus_mpa": 206_000.0,
                "poisson_ratio": 0.3,
                "density": 7.85e-9,
                "yield_strength_mpa": 355.0,
            }],
            "loads_inventory": {
                "top_mass_tonne": 300.0,
                "center_of_mass_offset_mm": 0.0,
                "top_horizontal_force_n": 500_000.0,
                "top_force_direction": "+X",
                "wind_pressure_mpa": 0.0012,
            },
        },
        "mesh_and_set_ir": {
            "global_size_mm": 650.0,
            "door_size_mm": 120.0,
            "patch_margin_u_mm": 900.0,
            "patch_margin_z_mm": 1450.0,
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
