from __future__ import annotations

from typing import Any


def experiment_protocols() -> list[dict[str, Any]]:
    """Protocols aligned directly with the paper's G1-G6 groups."""

    return [
        {
            "experiment_id": "G1",
            "name": "reference_engineering_cases",
            "metrics": ["parameter_gate_rate", "inp_repeatability", "model_gate_rate"],
            "acceptance": {"repeat_build_count": 10, "byte_identical_inp_rate": 1.0},
            "external_service": False,
        },
        {
            "experiment_id": "G2",
            "name": "s01_to_s10_heterogeneous_excel",
            "metrics": ["parameter_precision", "parameter_recall", "conflict_detection_rate", "source_coverage"],
            "acceptance": {"case_count": 10, "truth_leakage_count": 0},
            "external_service": "explicit_llm_only",
        },
        {
            "experiment_id": "G3",
            "name": "three_cases_nine_formats",
            "metrics": ["parse_completion_rate", "parameter_equivalence", "ocr_requirement_accuracy"],
            "acceptance": {"case_count": 3, "format_count": 9, "artifact_count": 27},
            "external_service": "explicit_llm_or_ocr_only",
        },
        {
            "experiment_id": "G4",
            "name": "abnormal_input_robustness",
            "metrics": ["expected_block_rate", "safe_degradation_rate", "unhandled_exception_rate"],
            "acceptance": {"case_count": 24, "unhandled_exception_rate": 0.0},
            "external_service": False,
        },
        {
            "experiment_id": "G5",
            "name": "parameter_change_tasks",
            "metrics": ["change_key_precision", "change_key_recall", "revision_guard_rate", "parent_immutability_rate"],
            "acceptance": {"case_count": 18, "parent_immutability_rate": 1.0, "stale_revision_rejection_rate": 1.0},
            "external_service": "explicit_llm_only",
        },
        {
            "experiment_id": "G6",
            "name": "abaqus_and_numerical_validation",
            "metrics": ["datacheck_pass_rate", "solve_completion_rate", "odb_valid_rate", "reaction_balance", "frequency_repeatability", "mesh_sensitivity"],
            "acceptance": {"datacheck_repetitions": 3, "odb_valid_rate": 1.0},
            "external_service": "explicit_local_abaqus_only",
        },
    ]
