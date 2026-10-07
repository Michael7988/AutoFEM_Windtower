from __future__ import annotations

import math
import random
from collections import Counter
from statistics import mean, median, pstdev
from typing import Any, Iterable, Sequence


def descriptive_statistics(values: Iterable[float]) -> dict[str, Any]:
    numbers = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not numbers:
        return {
            "count": 0,
            "mean": None,
            "standard_deviation": None,
            "coefficient_of_variation": None,
            "median": None,
            "q1": None,
            "q3": None,
            "minimum": None,
            "maximum": None,
        }
    average = mean(numbers)
    deviation = pstdev(numbers) if len(numbers) > 1 else 0.0
    return {
        "count": len(numbers),
        "mean": average,
        "standard_deviation": deviation,
        "coefficient_of_variation": deviation / abs(average) if average else 0.0,
        "median": median(numbers),
        "q1": _percentile(numbers, 0.25),
        "q3": _percentile(numbers, 0.75),
        "minimum": numbers[0],
        "maximum": numbers[-1],
    }


def paired_bootstrap_difference(
    left: Sequence[float],
    right: Sequence[float],
    *,
    repetitions: int = 5000,
    seed: int = 20260729,
) -> dict[str, Any]:
    if len(left) != len(right):
        raise ValueError("Paired samples must have the same length.")
    if not left:
        return {"count": 0, "mean_difference": None, "ci95": None}
    differences = [float(a) - float(b) for a, b in zip(left, right)]
    rng = random.Random(seed)
    bootstrap = []
    for _ in range(repetitions):
        sample = [differences[rng.randrange(len(differences))] for _ in differences]
        bootstrap.append(mean(sample))
    bootstrap.sort()
    return {
        "count": len(differences),
        "mean_difference": mean(differences),
        "ci95": [
            _percentile(bootstrap, 0.025),
            _percentile(bootstrap, 0.975),
        ],
        "bootstrap_repetitions": repetitions,
        "seed": seed,
    }


def holm_adjust(p_values: Sequence[float]) -> list[float]:
    indexed = sorted(enumerate(float(value) for value in p_values), key=lambda item: item[1])
    adjusted = [0.0] * len(indexed)
    running = 0.0
    total = len(indexed)
    for rank, (original_index, value) in enumerate(indexed):
        running = max(running, min(1.0, (total - rank) * value))
        adjusted[original_index] = running
    return adjusted


def cohen_kappa(left: Sequence[Any], right: Sequence[Any]) -> dict[str, Any]:
    if len(left) != len(right):
        raise ValueError("Rater labels must have the same length.")
    if not left:
        return {"count": 0, "observed_agreement": None, "expected_agreement": None, "kappa": None}
    observed = sum(a == b for a, b in zip(left, right)) / len(left)
    left_counts = Counter(left)
    right_counts = Counter(right)
    labels = set(left_counts) | set(right_counts)
    expected = sum(
        (left_counts[label] / len(left)) * (right_counts[label] / len(right))
        for label in labels
    )
    kappa = (observed - expected) / (1 - expected) if expected < 1 else 1.0
    return {
        "count": len(left),
        "observed_agreement": observed,
        "expected_agreement": expected,
        "kappa": kappa,
    }


def icc_two_way_absolute(left: Sequence[float], right: Sequence[float]) -> dict[str, Any]:
    """ICC(A,1) for two raters using a two-way random absolute-agreement model."""

    if len(left) != len(right):
        raise ValueError("Rater values must have the same length.")
    if len(left) < 2:
        return {"count": len(left), "icc_a1": None}
    rows = [[float(a), float(b)] for a, b in zip(left, right)]
    n = len(rows)
    k = 2
    grand = mean(value for row in rows for value in row)
    row_means = [mean(row) for row in rows]
    column_means = [mean(row[index] for row in rows) for index in range(k)]
    ss_rows = k * sum((value - grand) ** 2 for value in row_means)
    ss_columns = n * sum((value - grand) ** 2 for value in column_means)
    ss_total = sum((value - grand) ** 2 for row in rows for value in row)
    ss_error = max(0.0, ss_total - ss_rows - ss_columns)
    ms_rows = ss_rows / (n - 1)
    ms_columns = ss_columns / (k - 1)
    ms_error = ss_error / ((n - 1) * (k - 1))
    denominator = ms_rows + (k - 1) * ms_error + k * (ms_columns - ms_error) / n
    return {
        "count": n,
        "icc_a1": (ms_rows - ms_error) / denominator if denominator else None,
        "mean_square_rows": ms_rows,
        "mean_square_columns": ms_columns,
        "mean_square_error": ms_error,
    }


def _percentile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("Percentile requires at least one value.")
    position = (len(values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(values[lower])
    weight = position - lower
    return float(values[lower] * (1 - weight) + values[upper] * weight)
