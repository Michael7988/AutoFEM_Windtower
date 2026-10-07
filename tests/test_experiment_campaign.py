from __future__ import annotations

import json
import hashlib
from pathlib import Path

from windtower_backend.benchmark.campaign import (
    ExperimentCampaignConfig,
    run_experiment_campaign,
)
from windtower_backend.benchmark.experiment_dataset import (
    CROSS_FORMATS,
    generate_experiment_datasets,
)
from windtower_backend.benchmark.fault_evaluator import evaluate_fault_manifest
from windtower_backend.benchmark.metrics import (
    compare_parameter_payloads,
    evidence_traceability_metrics,
    stability_metrics,
)
from windtower_backend.benchmark.experiment_statistics import (
    descriptive_statistics,
    holm_adjust,
    paired_bootstrap_difference,
)
from windtower_backend.benchmark.protocols import experiment_protocols


def test_experiment_dataset_contains_versioned_g1_to_g6_contracts(tmp_path: Path) -> None:
    payload = generate_experiment_datasets(tmp_path / "benchmark", case_ids=("S01",))

    manifest = json.loads(Path(payload["manifest"]).read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "wind_tower_experiment_dataset_manifest_v2"
    assert set(manifest["groups"]) == {"G1", "G2", "G3", "G4", "G5", "G6"}
    assert set(manifest["groups"]["G3"]["formats"]) == set(CROSS_FORMATS)
    assert len(manifest["groups"]["G3"]["cases"]) == 3
    assert sum(
        len(case["artifacts"]) for case in manifest["groups"]["G3"]["cases"]
    ) == 3 * len(CROSS_FORMATS)
    artifacts = manifest["groups"]["G3"]["cases"][0]["artifacts"]
    assert all(Path(item["path"]).is_file() and item["size_bytes"] > 0 for item in artifacts)
    assert next(item for item in artifacts if item["content_kind"] == "text_pdf")["requires_ocr"] is False
    assert next(item for item in artifacts if item["content_kind"] == "scanned_pdf")["requires_ocr"] is True
    csv_artifact = next(item for item in artifacts if item["format"] == "csv")
    csv_text = Path(csv_artifact["path"]).read_text(encoding="utf-8-sig")
    assert "tower_segments[0].thickness_mm" in csv_text
    assert "fem_settings.loads.top_force_direction" in csv_text
    fault_ids = {item["fault_id"] for item in manifest["groups"]["G4"]["cases"]}
    assert len(manifest["groups"]["G4"]["cases"]) == 24
    assert manifest["groups"]["G5"]["case_count"] == 18
    assert manifest["groups"]["G5"]["single_parameter_cases"] == 12
    assert manifest["groups"]["G5"]["compound_cases"] == 6
    assert manifest["groups"]["G6"]["datacheck"]["repetitions"] == 3
    assert {
        "missing_required_thickness",
        "invalid_negative_thickness",
        "invalid_load_direction",
        "unknown_reinforcement_feature",
        "corrupt_document",
    } <= fault_ids


def test_parameter_traceability_and_statistics_are_machine_readable() -> None:
    summary = descriptive_statistics([1, 2, 3, 4])
    assert summary["median"] == 2.5
    first = paired_bootstrap_difference(
        [1, 2, 3],
        [0, 2, 4],
        repetitions=200,
    )
    second = paired_bootstrap_difference(
        [1, 2, 3],
        [0, 2, 4],
        repetitions=200,
    )
    assert first == second
    assert holm_adjust([0.01, 0.03, 0.04]) == [0.03, 0.06, 0.06]
    comparison = compare_parameter_payloads(
        {"height": 100.0, "direction": "+X", "extra": 1},
        {"height": 100.05, "direction": "+X", "required": 2},
        relative_tolerance=1e-3,
    )
    assert comparison["matched_field_count"] == 2
    assert comparison["extra_paths"] == ["extra"]
    assert comparison["missing_paths"] == ["required"]
    assert 0 < comparison["f1"] < 1

    traceability = evidence_traceability_metrics(
        {
            "parameter_provenance": {
                "geometry.height": {
                    "value_status": "observed",
                    "source_kind": "file",
                    "evidence_ids": ["ev-1"],
                },
                "mesh.element_type": {
                    "value_status": "defaulted",
                    "source_kind": "template_default",
                    "evidence_ids": [],
                },
            }
        },
        {"records": [{"evidence_id": "ev-1"}]},
    )
    assert traceability["evidence_coverage"] == 1.0
    assert traceability["invalid_reference_count"] == 0


def test_stability_metrics_detect_hash_drift() -> None:
    stable = stability_metrics(
        [{"inp_hash": "same"}, {"inp_hash": "same"}],
        hash_fields=("inp_hash",),
    )
    drift = stability_metrics(
        [{"inp_hash": "a"}, {"inp_hash": "b"}],
        hash_fields=("inp_hash",),
    )
    assert stable["all_hashes_consistent"] is True
    assert drift["all_hashes_consistent"] is False


def test_experiment_protocol_registry_covers_g1_to_g6() -> None:
    protocols = experiment_protocols()
    assert [item["experiment_id"] for item in protocols] == [
        f"G{index}" for index in range(1, 7)
    ]
    assert all(item["metrics"] and item["acceptance"] for item in protocols)


def test_local_campaign_is_repeatable_and_never_calls_external_services(tmp_path: Path) -> None:
    report = run_experiment_campaign(
        ExperimentCampaignConfig(
            case_ids=["S01"],
            stages=["G2"],
            deterministic_repetitions=2,
            llm_repetitions=1,
        ),
        root=tmp_path / "benchmark",
    )

    assert report["status"] == "pass"
    deterministic = report["stage_results"]["G2"]
    assert deterministic["case_count"] == 1
    assert deterministic["cases"][0]["stability"]["all_hashes_consistent"] is True
    assert report["summary"]["claims"]["real_llm_evaluated"] is False
    assert report["summary"]["claims"]["abaqus_evaluated"] is False
    assert Path(report["artifacts"]["json"]).is_file()
    assert Path(report["artifacts"]["jsonl"]).is_file()
    assert Path(report["artifacts"]["csv"]).is_file()
    assert Path(report["artifacts"]["markdown"]).is_file()
    manifest = (
        tmp_path
        / "benchmark"
        / "experiment_datasets"
        / "experiment_dataset_manifest.json"
    )
    before = hashlib.sha256(manifest.read_bytes()).hexdigest()
    run_experiment_campaign(
        ExperimentCampaignConfig(case_ids=["S01"], stages=["G3"]),
        root=tmp_path / "benchmark",
    )
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == before


def test_fault_injection_stage_blocks_or_isolates_expected_cases(tmp_path: Path) -> None:
    datasets = generate_experiment_datasets(
        tmp_path / "benchmark",
        case_ids=("S01",),
    )
    result = evaluate_fault_manifest(datasets["groups"]["G4"]["cases"])

    assert result["status"] == "pass"
    observed = {
        item["fault_id"]: item["observed_behavior"]
        for item in result["cases"]
    }
    assert observed["invalid_load_direction"] == "invalid_registered_enum"
    assert observed["unknown_reinforcement_feature"] == "isolate_as_unsupported"
    assert observed["missing_defaultable_output"] == "defaulted_output"
    assert result["duplicate_groups"]


def test_mesh_stage_records_quality_metrics_without_solver(tmp_path: Path) -> None:
    report = run_experiment_campaign(
        ExperimentCampaignConfig(
            case_ids=["S01"],
            stages=["G6"],
            global_mesh_factors=[1.0],
            door_mesh_factors=[1.0],
        ),
        root=tmp_path / "benchmark",
    )

    mesh = report["stage_results"]["G6"]["mesh_sensitivity"]
    assert mesh["status"] == "pass"
    assert mesh["run_count"] == 1
    assert mesh["runs"][0]["node_count"] > 0
    assert mesh["runs"][0]["element_count"] > 0
    assert mesh["runs"][0]["quality_error_count"] == 0
