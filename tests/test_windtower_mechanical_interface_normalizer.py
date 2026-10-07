from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone

import pytest

from windtower_backend.config import EXAMPLE_ROOT, load_settings
from windtower_backend.core.pipeline import WindTowerPipeline
from windtower_backend.input_batches.models import EvidenceFragment, InputBatch, ParameterCandidate
from windtower_backend.input_batches.service import EngineeringInputBatchService
from windtower_backend.importers.standard_input import normalize_import_to_standard_input
from windtower_backend.importers.standard_input.implementation import _strict_cell_number


ROOT = Path(__file__).resolve().parents[1]
TEN_MW = EXAMPLE_ROOT / "synthetic_standard_tower.xlsx"


def _mechanical_interface_workbook() -> Path:
    return EXAMPLE_ROOT / "synthetic_mechanical_interface_FEM.xls"


def _value(field: object) -> object:
    if isinstance(field, dict) and "value" in field:
        return field["value"]
    return field


def test_door_axis_labels_are_not_misread_as_two_millimetres() -> None:
    assert _strict_cell_number("内椭圆长轴Y2") is None
    assert _strict_cell_number("内椭圆短轴X2") is None
    assert _strict_cell_number("2300") == 2300.0


def test_legacy_two_millimetre_door_candidates_are_removed(tmp_path: Path) -> None:
    service = EngineeringInputBatchService(WindTowerPipeline(load_settings()))
    service.root = tmp_path / "batches"
    evidence = EvidenceFragment(
        evidence_id="ev-door-axis",
        file_id="file-old",
        filename="mechanical.xls",
        kind="structured",
        text="2.0",
        location={"sheet": "门洞参数", "cell_or_range": "R5C1"},
        extraction_method="deterministic_excel",
        content_hash="old",
    )
    candidate = ParameterCandidate(
        candidate_id="candidate-old",
        key="geometry.door.width_mm",
        label="门洞宽度",
        raw_value=2.0,
        normalized_value=2.0,
        source_file_id="file-old",
        evidence_ids=[evidence.evidence_id],
        extraction_method="deterministic_excel",
    )
    now = datetime.now(timezone.utc).isoformat()
    batch = InputBatch(
        batch_id="batch-old-door-axis",
        status="ready",
        files=[],
        evidence=[evidence],
        candidates=[candidate],
        resolved_parameters={"geometry.door.width_mm": 2.0},
        created_at=now,
        updated_at=now,
    )

    service._upgrade_legacy_door_axis_candidates(batch)

    assert "geometry.door.width_mm" not in batch.resolved_parameters
    assert batch.candidates == []
    assert any("X2/Y2" in warning for warning in batch.warnings)


def test_ten_mw_standard_workbook_upload_contract_remains_parseable() -> None:
    normalized = normalize_import_to_standard_input(TEN_MW)
    package = normalized["standard_input_package"]
    door = package["door_opening"]

    assert normalized["validation_report"]["status"] in {"pass", "warning"}
    assert normalized["validation_report"]["blocked"] is False
    assert len(package["tower_segments"]) == 10
    assert _value(door["width_mm"]) == 1100.0
    assert _value(door["height_mm"]) == 2200.0
    assert _value(door["z_bottom_mm"]) == 500.0
    assert normalized["fem_params"]["material.elastic_modulus_mpa"] == 205000.0
    assert normalized["fem_params"]["material.poisson_ratio"] == 0.3


def test_synthetic_mechanical_interface_normalizes_to_standard_input() -> None:
    normalized = normalize_import_to_standard_input(_mechanical_interface_workbook())
    package = normalized["standard_input_package"]
    door = package["door_opening"]
    segments = package["tower_segments"]

    assert normalized["input_format"] == "windtower_mechanical_interface"
    assert normalized["validation_report"]["status"] in {"pass", "warning"}
    assert normalized["validation_report"]["blocked"] is False
    assert normalized["validation_report"]["parser_name"] == "windtower_mechanical_interface_v2"
    assert len(segments) > 40
    assert _value(segments[0]["z_bottom_mm"]) == 0.0
    assert _value(segments[-1]["z_top_mm"]) == pytest.approx(98400.0)
    assert _value(door["width_mm"]) == 900.0
    assert _value(door["height_mm"]) == 2300.0
    assert _value(door["z_bottom_mm"]) == 1200.0
    assert normalized["fem_params"]["material.name"] == "SYNTHETIC_STEEL"
    assert normalized["fem_params"]["material.elastic_modulus_mpa"] == 205000.0
    assert normalized["fem_params"]["material.poisson_ratio"] == 0.3
    assert normalized["fem_params"]["element.element_type"] == "S4R"


def test_create_project_analysis_upload_path_does_not_block_latest_excels() -> None:
    pipeline = WindTowerPipeline(load_settings())
    for workbook in (TEN_MW, _mechanical_interface_workbook()):
        response = pipeline.create_project_from_upload(
            workbook.name,
            workbook.read_bytes(),
            "parse robustness regression",
        )

        assert response.get("stage") != "input_blocked"
        assert response["validation_report"]["blocked"] is False
        assert response["summary"]["section_count"] > 0
