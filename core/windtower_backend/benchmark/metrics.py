from __future__ import annotations

import math
from collections import Counter
from statistics import mean, pstdev
from typing import Any, Iterable

from ..reproducibility import stable_hash


IGNORED_METADATA_KEYS = {
    "benchmark",
    "source_file",
    "revision",
    "parameter_provenance",
}


def flatten_parameters(
    payload: Any,
    *,
    prefix: str = "",
    ignored_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Flatten parameter payloads into stable semantic paths.

    List positions remain explicit because tower-course order is engineering
    information. Volatile audit metadata is excluded by default.
    """

    ignored = IGNORED_METADATA_KEYS if ignored_keys is None else ignored_keys
    result: dict[str, Any] = {}
    if isinstance(payload, dict):
        for key in sorted(payload):
            if key in ignored:
                continue
            path = f"{prefix}.{key}" if prefix else str(key)
            result.update(flatten_parameters(payload[key], prefix=path, ignored_keys=ignored))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            path = f"{prefix}[{index}]"
            result.update(flatten_parameters(item, prefix=path, ignored_keys=ignored))
    else:
        result[prefix] = payload
    return result


def compare_parameter_payloads(
    predicted: dict[str, Any],
    truth: dict[str, Any],
    *,
    relative_tolerance: float = 1e-3,
    absolute_tolerance: float = 1e-9,
) -> dict[str, Any]:
    predicted_flat = flatten_parameters(predicted)
    truth_flat = flatten_parameters(truth)
    predicted_keys = set(predicted_flat)
    truth_keys = set(truth_flat)
    common = predicted_keys & truth_keys
    matches: list[str] = []
    mismatches: list[dict[str, Any]] = []
    numeric_relative_errors: list[float] = []

    for key in sorted(common):
        predicted_value = predicted_flat[key]
        truth_value = truth_flat[key]
        matched, relative_error = _values_match(
            predicted_value,
            truth_value,
            relative_tolerance=relative_tolerance,
            absolute_tolerance=absolute_tolerance,
        )
        if matched:
            matches.append(key)
        else:
            mismatches.append(
                {
                    "path": key,
                    "predicted": predicted_value,
                    "truth": truth_value,
                    "relative_error": relative_error,
                }
            )
        if relative_error is not None and math.isfinite(relative_error):
            numeric_relative_errors.append(relative_error)

    true_positive = len(matches)
    false_positive = len(predicted_keys - truth_keys) + len(mismatches)
    false_negative = len(truth_keys - predicted_keys) + len(mismatches)
    precision = _safe_ratio(true_positive, true_positive + false_positive)
    recall = _safe_ratio(true_positive, true_positive + false_negative)
    f1 = _safe_ratio(2 * precision * recall, precision + recall)
    return {
        "schema_version": "parameter_comparison_metrics_v1",
        "predicted_field_count": len(predicted_keys),
        "truth_field_count": len(truth_keys),
        "matched_field_count": true_positive,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "exact_field_accuracy": _safe_ratio(true_positive, len(truth_keys)),
        "mean_numeric_relative_error": (
            mean(numeric_relative_errors) if numeric_relative_errors else None
        ),
        "missing_paths": sorted(truth_keys - predicted_keys),
        "extra_paths": sorted(predicted_keys - truth_keys),
        "mismatches": mismatches,
    }


def evidence_traceability_metrics(
    canonical_ir: dict[str, Any],
    evidence_ir: dict[str, Any],
) -> dict[str, Any]:
    evidence_ids = {
        str(item.get("evidence_id"))
        for item in evidence_ir.get("records") or []
        if item.get("evidence_id")
    }
    provenance = canonical_ir.get("parameter_provenance") or {}
    observed = []
    traceable = []
    invalid_references: list[dict[str, Any]] = []
    exempt = []
    for key, item in sorted(provenance.items()):
        status = str(item.get("value_status") or "")
        source = str(item.get("source_kind") or "")
        references = {str(value) for value in item.get("evidence_ids") or []}
        missing = sorted(references - evidence_ids)
        if missing:
            invalid_references.append({"key": key, "evidence_ids": missing})
        if status in {"derived", "defaulted", "suggested"} or source in {
            "system_setting",
            "template_default",
        }:
            exempt.append(key)
            continue
        if status in {"observed", "confirmed"}:
            observed.append(key)
            if references and not missing:
                traceable.append(key)
    return {
        "schema_version": "evidence_traceability_metrics_v1",
        "observed_parameter_count": len(observed),
        "traceable_parameter_count": len(traceable),
        "evidence_coverage": _safe_ratio(len(traceable), len(observed)),
        "invalid_reference_count": len(invalid_references),
        "invalid_references": invalid_references,
        "exempt_parameters": exempt,
    }


def stability_metrics(
    runs: Iterable[dict[str, Any]],
    *,
    hash_fields: tuple[str, ...] = (
        "parameter_hash",
        "inp_hash",
    ),
) -> dict[str, Any]:
    rows = list(runs)
    hashes: dict[str, dict[str, Any]] = {}
    for field in hash_fields:
        values = [str(row[field]) for row in rows if row.get(field)]
        counts = Counter(values)
        hashes[field] = {
            "observed": len(values),
            "unique": len(counts),
            "consistent": len(counts) <= 1,
            "dominant_ratio": _safe_ratio(max(counts.values(), default=0), len(values)),
            "values": dict(sorted(counts.items())),
        }
    numeric_fields = sorted(
        {
            key
            for row in rows
            for key, value in row.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
    )
    numeric = {}
    for field in numeric_fields:
        values = [float(row[field]) for row in rows if isinstance(row.get(field), (int, float))]
        if not values:
            continue
        average = mean(values)
        standard_deviation = pstdev(values) if len(values) > 1 else 0.0
        numeric[field] = {
            "count": len(values),
            "mean": average,
            "standard_deviation": standard_deviation,
            "coefficient_of_variation": (
                standard_deviation / abs(average) if average else 0.0
            ),
        }
    return {
        "schema_version": "experiment_stability_metrics_v1",
        "run_count": len(rows),
        "hashes": hashes,
        "numeric": numeric,
        "all_hashes_consistent": all(
            item["consistent"] for item in hashes.values() if item["observed"]
        ),
        "run_set_hash": stable_hash(rows, length=32),
    }


def aggregate_binary_metric(successes: int, total: int) -> dict[str, Any]:
    """Return a proportion and a Wilson 95% confidence interval."""

    if total <= 0:
        return {"successes": successes, "total": total, "rate": None, "ci95": None}
    proportion = successes / total
    z = 1.959963984540054
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    spread = (
        z
        * math.sqrt(
            (proportion * (1 - proportion) + z * z / (4 * total)) / total
        )
        / denominator
    )
    return {
        "successes": successes,
        "total": total,
        "rate": proportion,
        "ci95": [max(0.0, center - spread), min(1.0, center + spread)],
    }


def _values_match(
    predicted: Any,
    truth: Any,
    *,
    relative_tolerance: float,
    absolute_tolerance: float,
) -> tuple[bool, float | None]:
    if isinstance(predicted, bool) or isinstance(truth, bool):
        return predicted == truth, None
    if isinstance(predicted, (int, float)) and isinstance(truth, (int, float)):
        predicted_number = float(predicted)
        truth_number = float(truth)
        difference = abs(predicted_number - truth_number)
        scale = max(abs(truth_number), absolute_tolerance)
        relative_error = difference / scale
        return (
            difference <= absolute_tolerance
            or relative_error <= relative_tolerance
        ), relative_error
    return predicted == truth, None


def _safe_ratio(numerator: float, denominator: float) -> float:
    return float(numerator / denominator) if denominator else 0.0
