from __future__ import annotations

import json
from pathlib import Path

import pytest

from windtower_backend.benchmark.e1_real_engineering import (
    _upgrade_resumed_row,
    DATASET_SCHEMA_VERSION,
    RedactingProvider,
    _numeric_match_rate,
    _fem_response_payload,
    discover_real_case_pairs,
    file_sha256,
    generate_e1_dataset,
    summarize_e1_results,
)
from windtower_backend.input_batches.providers import EngineeringLlmProvider


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "core" / "example"


class RecordingProvider(EngineeringLlmProvider):
    def __init__(self) -> None:
        self.received = []

    def capabilities(self):
        return {"provider": "fake", "configured": True}

    def map_parameters(self, evidence, modeling_goal, mapping_prompt=None, target_keys=None):
        self.received.append(evidence)
        return {"status": "called", "provider": "fake", "model": "fake", "fields": [], "usage": {}}

    def extract_image_text(self, content, media_type, prompt):
        return {"status": "not_called", "text": ""}

    def usage(self):
        return {}

    def connection_test(self):
        return {"status": "ready"}


def test_real_cases_are_paired_without_modifying_sources(tmp_path: Path):
    pairs = discover_real_case_pairs(SOURCE_ROOT)
    before = {str(item["raw"]): file_sha256(item["raw"]) for item in pairs}
    before.update({str(item["fem"]): file_sha256(item["fem"]) for item in pairs})
    manifest = generate_e1_dataset(SOURCE_ROOT, tmp_path / "e1")
    assert manifest["schema_version"] == DATASET_SCHEMA_VERSION
    assert manifest["condition_count"] == 16
    assert len(manifest["cases"]) == 4
    assert all(len(item["scenarios"]) == 4 for item in manifest["cases"])
    after = {path: file_sha256(path) for path in before}
    assert after == before


def test_truth_separates_observed_and_supplemental_values(tmp_path: Path):
    root = tmp_path / "e1"
    manifest = generate_e1_dataset(SOURCE_ROOT, root)
    for case in manifest["cases"]:
        truth = json.loads((root / case["ground_truth"]["truth"]).read_text(encoding="utf-8"))
        assert truth["status"] == "provisional_pending_engineer_review"
        assert truth["observed_engineering_truth"]["tower_segments"]
        assert "door_opening" not in truth["observed_engineering_truth"]
        assert truth["supplemental_advisory_truth"]["door_opening"]
    t04_truth = json.loads((root / manifest["cases"][3]["ground_truth"]["truth"]).read_text(encoding="utf-8"))
    labels = {item["label"] for item in t04_truth["raw_auxiliary"]["embedded_subtables"]}
    assert labels == {"法兰参数", "极限载荷"}


def test_redacting_provider_removes_identifiers_and_blocks_truth_sentinel():
    delegate = RecordingProvider()
    provider = RedactingProvider(delegate, "engineering")
    provider.configure(sensitive_terms=["某风电项目", "H171-4.0MW"], leakage_sentinel="TRUTH_ONLY")
    result = provider.map_parameters(
        [{"evidence_id": "ev-1", "filename": "某风电项目.xls", "location": {"sheet": "塔节参数"}, "text": "项目 某风电项目，机型 H171-4.0MW"}],
        "建立风塔模型",
    )
    assert result["status"] == "called"
    outbound = json.dumps(delegate.received, ensure_ascii=False)
    assert "某风电项目" not in outbound
    assert "H171-4.0MW" not in outbound
    assert "REDACTED_IDENTIFIER" in outbound
    assert provider.audit[-1]["truth_sentinel_absent"] is True

    with pytest.raises(RuntimeError, match="leakage sentinel"):
        provider.map_parameters(
            [{"evidence_id": "ev-2", "filename": "x.xls", "location": {}, "text": "TRUTH_ONLY"}],
            "建立风塔模型",
        )


def test_zero_fem_candidate_is_scored_as_a_miss_on_resume(tmp_path: Path):
    root = tmp_path / "e1"
    manifest = generate_e1_dataset(SOURCE_ROOT, root)
    row = {
        "fem_candidate_accuracy": None,
        "fem_usage": {"calls": 1},
        "details": {"fem_comparison": None},
    }
    upgraded, changed = _upgrade_resumed_row(root, manifest["cases"][0], row)
    assert changed is True
    assert upgraded["fem_candidate_accuracy"] == 0.0
    assert upgraded["details"]["fem_response_status"] == "called_no_scored_fields"
    assert upgraded["details"]["fem_comparison"]["missing_paths"]


def test_fem_response_accepts_dict_fields_and_ignores_malformed_items():
    assert _fem_response_payload({
        "fields": {
            "mesh.global_size_mm": {"value": 500.0, "normalized_value": None},
            "unsupported.key": {"value": 1},
        },
    }) == {"mesh.global_size_mm": 500.0}
    assert _fem_response_payload({"fields": ["bad", None]}) == {}


def test_numeric_tolerance_uses_only_numeric_truth_fields():
    truth = {"schema_version": "x", "tower_segments": [{"z_bottom_mm": 0.0, "z_top_mm": 1000.0}]}
    predicted = {"schema_version": "x", "tower_segments": [{"z_bottom_mm": 0.0, "z_top_mm": 1000.0005}]}
    assert _numeric_match_rate(predicted, truth) == 1.0
    predicted["tower_segments"][0]["z_top_mm"] = 1001.0
    assert _numeric_match_rate(predicted, truth) == 0.5


def test_summary_does_not_hide_failed_runs():
    manifest = {"truth_status": "provisional_pending_engineer_review"}
    rows = []
    scenarios = ("raw", "blank_note_variant", "duplicate_appendix_variant", "numeric_display_variant")
    for condition in ("R0", "R1"):
        for case in range(4):
            for scenario in scenarios:
                repetitions = 1 if condition == "R0" else (5 if scenario == "raw" else 3)
                for repetition in range(repetitions):
                    failed = condition == "R0" and case == 3 and scenario == "raw" and repetition == 0
                    rows.append({
                        "condition": condition,
                        "case_id": f"T{case + 1:02d}",
                        "scenario_family": scenario,
                        "status": "completed",
                        "case_exact": not failed,
                        "failure_class": "unsupported_or_noise_promoted" if failed else "none",
                        "error_promotion_count": 0,
                        "parameter_hash": f"{condition}-{case}-{scenario}",
                        "field_accuracy": 0.5 if failed else 1.0,
                        "field_precision": 1.0,
                        "field_recall": 1.0,
                        "numeric_tolerance_match_rate": 1.0,
                            "unit_normalization_accuracy": None,
                        "source_location_rate": 1.0,
                        "evidence_coverage": 1.0,
                        "error_promotion_rate": 0.0,
                        "unsupported_extraction_rate": 0.1 if failed else 0.0,
                        "fem_candidate_accuracy": None,
                        "elapsed_seconds": 0.1,
                        "total_tokens": 0,
                    })
    summary = summarize_e1_results(rows, manifest)
    assert summary["status"] == "pre_experiment_complete"
    assert summary["run_count"] == 72
    assert summary["conditions"]["R0"]["failure_classes"]["unsupported_or_noise_promoted"] == 1
    assert summary["conditions"]["R0"]["case_exact"]["rate"] < 1.0
