from __future__ import annotations

import json
from pathlib import Path

import pytest

from windtower_backend.config import load_settings
from windtower_backend.modeling import ProjectState
from windtower_backend.parameter_ir import parameter_set_from_ir
from windtower_backend.services.model_generation import ModelGenerationService
from windtower_backend.services.parameters import _without_generated_artifacts


class _Parameters:
    def get(self, _project_id: str) -> dict:
        raise AssertionError("not used by artifact tests")


def _service() -> ModelGenerationService:
    return ModelGenerationService(load_settings(), _Parameters())  # type: ignore[arg-type]


def _state(tmp_path: Path) -> ProjectState:
    ir = json.loads(
        Path("tests/fixtures/synthetic_l0_ir.json").read_text(encoding="utf-8")
    )
    ir.setdefault("semantic_ir", {}).setdefault("loads_inventory", {})[
        "center_of_mass_offset_mm"
    ] = 3000.0
    return ProjectState(
        project_id="wt-ui",
        root=tmp_path,
        excel_path=tmp_path / "source.xlsx",
        ir=ir,
        artifacts={},
    )


def test_mesh_preview_uses_prepared_confirmed_parameter_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service()
    state = _state(tmp_path)
    parameter_set = parameter_set_from_ir("wt-ui", state.ir)
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        service,
        "_prepared_state",
        lambda _project_id: (state, parameter_set),
    )

    def preview(prepared: ProjectState) -> dict:
        observed["state"] = prepared
        return {"status": "pass", "mesh_data_url": "/mesh.json"}

    monkeypatch.setattr(
        "windtower_backend.services.model_generation.mesh_pyvista_preview_l0",
        preview,
    )

    result = service.mesh_preview("wt-ui")

    assert result["status"] == "pass"
    assert observed["state"] is state


def test_artifact_access_is_registered_and_project_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service()
    state = _state(tmp_path)
    mesh = tmp_path / "mesh" / "pyvista_preview_mesh.json"
    mesh.parent.mkdir()
    mesh.write_text("{}", encoding="utf-8")
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    state.artifacts = {
        "mesh/pyvista_preview_mesh.json": mesh,
        "outside-secret.txt": outside,
    }
    monkeypatch.setattr(
        "windtower_backend.services.model_generation.get_state",
        lambda _project_id: state,
    )

    listing = service.artifacts("wt-ui")
    assert listing["items"] == []
    assert service.artifact_path(
        "wt-ui",
        "mesh/pyvista_preview_mesh.json",
    ) == mesh.resolve()
    with pytest.raises(FileNotFoundError):
        service.artifact_path("wt-ui", "../outside-secret.txt")
    with pytest.raises(FileNotFoundError):
        service.artifact_path("wt-ui", "outside-secret.txt")


def test_artifact_listing_only_exposes_engineering_deliverables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service()
    state = _state(tmp_path)
    paths = {
        "parameter_set.json": tmp_path / "parameter_set.json",
        "facts/traceability_matrix.csv": tmp_path / "facts" / "traceability_matrix.csv",
        "mesh/mesh_quality_report.json": tmp_path / "mesh" / "mesh_quality_report.json",
        "mesh/pyvista_preview_mesh.json": tmp_path / "mesh" / "pyvista_preview_mesh.json",
        "ir.json": tmp_path / "ir.json",
        "inp/static/model.inp": tmp_path / "inp" / "static" / "model.inp",
        "results/results_summary.json": tmp_path / "results" / "results_summary.json",
        "results/odb_result_mesh.json": tmp_path / "results" / "odb_result_mesh.json",
    }
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    state.artifacts = paths
    monkeypatch.setattr(
        "windtower_backend.services.model_generation.get_state",
        lambda _project_id: state,
    )

    listing = service.artifacts("wt-ui")

    assert [item["relative_path"] for item in listing["items"]] == [
        "facts/traceability_matrix.csv",
        "inp/static/model.inp",
        "mesh/mesh_quality_report.json",
        "parameter_set.json",
        "results/results_summary.json",
    ]
    assert service.artifact_path(
        "wt-ui",
        "mesh/pyvista_preview_mesh.json",
    ) == paths["mesh/pyvista_preview_mesh.json"].resolve()


def test_result_mesh_reports_missing_until_postprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service()
    state = _state(tmp_path)
    monkeypatch.setattr(
        "windtower_backend.services.model_generation.get_state",
        lambda _project_id: state,
    )

    with pytest.raises(FileNotFoundError):
        service.result_mesh("wt-ui")


def test_parameter_revision_invalidates_generated_artifacts(tmp_path: Path) -> None:
    parameter_set = tmp_path / "parameter_set.json"
    mesh = tmp_path / "mesh" / "pyvista_preview_mesh.json"
    result = tmp_path / "results" / "odb_result_mesh.json"
    parameter_set.write_text("{}", encoding="utf-8")
    mesh.parent.mkdir()
    mesh.write_text("{}", encoding="utf-8")
    result.parent.mkdir()
    result.write_text("{}", encoding="utf-8")

    retained = _without_generated_artifacts(
        {
            "parameter_set.json": parameter_set,
            "mesh/pyvista_preview_mesh.json": mesh,
            "results/odb_result_mesh.json": result,
        },
        tmp_path,
    )

    assert retained == {"parameter_set.json": parameter_set}
