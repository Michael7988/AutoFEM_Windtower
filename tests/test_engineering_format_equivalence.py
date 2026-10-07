from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

import fitz
import pytest

from core.windtower_backend.benchmark.engineering_format_equivalence import (
    DATASET_SCHEMA_VERSION,
    ENGINEERING_CARRIERS,
    carrier_artifacts,
    discover_engineering_case_pairs,
    generate_engineering_format_dataset,
    run_engineering_format_experiment,
)
from core.windtower_backend.input_batches.extractors import (
    extract_file,
    validate_upload,
)
from core.windtower_backend.parameter_ir import ParameterSet, validate_parameter_set


@pytest.fixture
def synthetic_engineering_dataset(tmp_path: Path) -> tuple[Path, dict]:
    source_root = tmp_path / "example"
    source_root.mkdir()
    fixture = Path("tests/fixtures/synthetic_mechanical_interface_FEM.xls")
    for index in range(1, 5):
        raw = source_root / f"synthetic-interface-{index}.xls"
        fem = source_root / f"synthetic-interface-{index}_FEM参数.xls"
        shutil.copy2(fixture, raw)
        shutil.copy2(fixture, fem)
    output_root = source_root / "private_llm_format_equivalence"
    manifest = generate_engineering_format_dataset(
        source_root,
        output_root,
        compile_reference_models=False,
    )
    return output_root, manifest


def test_discovers_four_cases_and_eight_source_workbooks(
    synthetic_engineering_dataset: tuple[Path, dict],
) -> None:
    output_root, manifest = synthetic_engineering_dataset
    pairs = discover_engineering_case_pairs(output_root.parent)
    assert len(pairs) == 4
    assert manifest["schema_version"] == DATASET_SCHEMA_VERSION
    assert manifest["case_count"] == 4
    assert len(list((output_root / "source").glob("CASE*/*.xls"))) == 8


def test_generates_exactly_ten_carriers_per_case(
    synthetic_engineering_dataset: tuple[Path, dict],
) -> None:
    _, manifest = synthetic_engineering_dataset
    assert manifest["condition_count"] == 40
    for case in manifest["cases"]:
        assert [item["carrier"] for item in case["carriers"]] == list(
            ENGINEERING_CARRIERS
        )
        assert len(case["carriers"]) == 10
        assert all(item["input_only"] for item in case["carriers"])
        assert {
            item["active_payload_sha256"] for item in case["carriers"]
        } == {case["ground_truth"]["active_payload_sha256"]}


def test_truth_is_valid_and_not_mixed_with_carrier_inputs(
    synthetic_engineering_dataset: tuple[Path, dict],
) -> None:
    output_root, manifest = synthetic_engineering_dataset
    for case in manifest["cases"]:
        truth_path = output_root / case["ground_truth"]["parameter_set"]
        parameter_set = ParameterSet.model_validate_json(
            truth_path.read_text(encoding="utf-8")
        )
        assert validate_parameter_set(parameter_set)["status"] == "pass"
        for carrier in case["carriers"]:
            for artifact in carrier_artifacts(output_root, carrier):
                assert "ground_truth" not in artifact.parts
                content = artifact.read_bytes()
                assert truth_path.name.encode() not in content


def test_pdf_text_layers_and_png_segment_coverage(
    synthetic_engineering_dataset: tuple[Path, dict],
) -> None:
    output_root, manifest = synthetic_engineering_dataset
    for case in manifest["cases"]:
        carriers = {item["carrier"]: item for item in case["carriers"]}
        text_pdf = carrier_artifacts(output_root, carriers["text_pdf"])[0]
        scan_pdf = carrier_artifacts(output_root, carriers["scan_pdf"])[0]
        with fitz.open(text_pdf) as document:
            assert sum(len(page.get_text().strip()) for page in document) > 100
        with fitz.open(scan_pdf) as document:
            assert all(not page.get_text().strip() for page in document)
        png_files = carrier_artifacts(output_root, carriers["png"])
        assert len(png_files) > 1
        assert carriers["png"]["tower_segment_coverage"] == list(
            range(1, case["tower_segment_count"] + 1)
        )


def test_every_generated_artifact_is_accepted_by_current_extractors(
    synthetic_engineering_dataset: tuple[Path, dict],
) -> None:
    output_root, manifest = synthetic_engineering_dataset
    for case in manifest["cases"]:
        for carrier in case["carriers"]:
            for artifact in carrier_artifacts(output_root, carrier):
                content = artifact.read_bytes()
                validate_upload(artifact.name, content)
                fragments, metadata = extract_file(
                    "synthetic-file",
                    artifact.name,
                    content,
                )
                assert metadata["ocr_pages"] >= 0
                if artifact.suffix.lower() not in {".xls", ".xlsx", ".xlsm"}:
                    assert fragments


def test_result_template_contains_required_measurement_fields(
    synthetic_engineering_dataset: tuple[Path, dict],
) -> None:
    output_root, _ = synthetic_engineering_dataset
    with (output_root / "results" / "run_results_template.csv").open(
        encoding="utf-8-sig", newline=""
    ) as stream:
        fields = next(csv.reader(stream))
    assert {
        "case_id",
        "carrier",
        "repeat",
        "model",
        "temperature",
        "prompt_version",
        "parameter_precision",
        "parameter_recall",
        "model_signature_equivalent",
        "ocr_pages",
        "total_tokens",
        "latency_ms",
        "failure_class",
    }.issubset(fields)
    summary = json.loads(
        (output_root / "results" / "summary_template.json").read_text(
            encoding="utf-8"
        )
    )
    assert summary["status"] == "not_run"


def test_default_runner_never_enables_llm(
    synthetic_engineering_dataset: tuple[Path, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root, _ = synthetic_engineering_dataset
    parse_modes: list[str] = []

    class FakeBatchService:
        def __init__(self, pipeline: object) -> None:
            self.pipeline = pipeline
            self.provider = self

        def usage(self) -> dict:
            return {}

        def capabilities(self) -> dict:
            return {
                "configured": True,
                "provider": "must-not-be-called",
                "main_model": "none",
            }

        def create_batch(
            self,
            uploads: list[tuple[str, str, bytes]],
            modeling_goal: str,
            parse_mode: str,
        ) -> dict:
            assert uploads
            assert modeling_goal
            parse_modes.append(parse_mode)
            return {
                "batch_id": f"fake-{len(parse_modes)}",
                "status": "blocked",
                "provider": "deterministic",
                "model": "none",
                "files": [],
                "resolved_parameters": {},
                "candidates": [],
                "conflicts": [],
                "usage": {},
            }

    monkeypatch.setattr(
        "core.windtower_backend.benchmark.engineering_format_equivalence.EngineeringInputBatchService",
        FakeBatchService,
    )
    result = run_engineering_format_experiment(output_root)
    assert result["mode"] == "deterministic"
    assert result["run_count"] == 40
    assert parse_modes == ["deterministic"] * 40
