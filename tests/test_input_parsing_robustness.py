from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from windtower_backend.config import EXAMPLE_ROOT
from windtower_backend.importers.standard_input import normalize_import_to_standard_input
from windtower_backend.importers.windtower_adapter import parse_windtower_workbook
from windtower_backend.l0 import mesh_l0, validate_l0
from windtower_backend.modeling import ProjectState, build_ir


def _robustness_workbooks() -> list[Path]:
    paths = [
        EXAMPLE_ROOT / "synthetic_mechanical_interface_FEM.xls",
        EXAMPLE_ROOT / "synthetic_standard_tower.xlsx",
    ]
    assert all(path.is_file() for path in paths)
    return paths


def test_synthetic_reference_workbooks_parse_build_and_mesh(tmp_path: Path) -> None:
    for workbook in _robustness_workbooks():
        normalized = normalize_import_to_standard_input(workbook)
        parsed = parse_windtower_workbook(workbook)
        ir = build_ir(
            project_id=f"probe-{workbook.stem[-8:]}",
            source_name=workbook.name,
            tower=parsed["tower"],
            flanges=parsed["flanges"],
            door=parsed["door"],
            fem_params=parsed["fem_params"],
            import_meta=parsed,
        )
        state = ProjectState(project_id=f"probe-{workbook.stem[-8:]}", root=tmp_path / workbook.stem, excel_path=workbook, ir=ir, artifacts={})
        validation = validate_l0(state)
        mesh = mesh_l0(state)

        assert normalized["validation_report"]["blocked"] is False
        assert len(parsed["tower"]) >= 2
        assert parsed["door"]
        assert parsed["fem_params"]
        assert validation["status"] in {"pass", "warning"}
        assert mesh["status"] in {"pass", "warning"}

        if workbook.suffix.lower() == ".xls":
            assert parsed["flanges"]
            assert all(flange.get("total_thickness_mm") is not None for flange in parsed["flanges"])


def test_missing_flange_total_thickness_degrades_to_warning(tmp_path: Path) -> None:
    workbook = EXAMPLE_ROOT / "synthetic_mechanical_interface_FEM.xls"
    parsed = parse_windtower_workbook(workbook)
    degraded = deepcopy(parsed)
    degraded["flanges"] = [dict(flange, total_thickness_mm=None) for flange in parsed["flanges"]]
    degraded["warnings"] = list(parsed.get("warnings", []))

    ir = build_ir(
        project_id="probe-degraded-flange",
        source_name=workbook.name,
        tower=degraded["tower"],
        flanges=degraded["flanges"],
        door=degraded["door"],
        fem_params=degraded["fem_params"],
        import_meta=degraded,
    )
    state = ProjectState(project_id="probe-degraded-flange", root=tmp_path / "degraded", excel_path=workbook, ir=ir, artifacts={})
    validation = validate_l0(state)

    assert ir["semantic_ir"]["flanges"] == []
    assert any("skipped for flange-zone matching" in warning for warning in ir["raw_excel_ir"]["parse_warnings"])
    assert validation["status"] in {"pass", "warning"}
