from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def build_result_sanity_report(project_root: Path, ir: dict[str, Any]) -> dict[str, Any]:
    results_dir = project_root / "results"
    buckling_enabled = bool((ir.get("experimental_analysis") or {}).get("enable_buckling_screening"))
    checks = [
        _check_file(results_dir / "results_summary.json", "results_summary"),
        _check_numeric_csv(results_dir / "door_stress_summary.csv", "door_stress", ["max_mises", "p95_mises"]),
        _check_numeric_csv(results_dir / "modal_frequencies.csv", "modal_frequency", ["frequency_hz"]),
        _check_equilibrium(results_dir / "equilibrium_check.json"),
        _check_strength(ir, results_dir / "door_stress_summary.csv"),
    ]
    if buckling_enabled:
        checks.append(_check_numeric_csv(results_dir / "buckling_factors.csv", "buckling_factor", ["buckling_factor"]))
    fatal = [item for item in checks if item["severity"] == "fatal"]
    warnings = [item for item in checks if item["severity"] == "warning"]
    physical_consistency = {
        "reaction_equilibrium": _read_status(results_dir / "equilibrium_check.json"),
        "stress_units": "MPa",
        "modal_frequency_units": "Hz",
    }
    if buckling_enabled:
        physical_consistency["buckling_factor_dimensionless"] = True
    else:
        physical_consistency["buckling_screening"] = "disabled_scope_deferred"
    return {
        "report_type": "ResultSanityReport",
        "status": "blocked" if fatal else ("warning" if warnings else "pass"),
        "checks": checks,
        "physical_consistency": physical_consistency,
        "llm_use": "LLM may explain failed checks and propose next experiments, but numerical acceptance is rule-based.",
    }


def _check_file(path: Path, name: str) -> dict[str, Any]:
    if not path.exists():
        return {"name": name, "severity": "warning", "status": "pending", "message": f"{path.name} is missing."}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"name": name, "severity": "fatal", "status": "blocked", "message": f"{path.name} is not valid JSON: {exc}"}
    status = data.get("status", "unknown")
    severity = "warning" if status in {"pending", "warning", "unknown"} else "info"
    return {"name": name, "severity": severity, "status": status, "message": f"{path.name} status is {status}."}


def _check_numeric_csv(path: Path, name: str, fields: list[str]) -> dict[str, Any]:
    if not path.exists():
        return {"name": name, "severity": "warning", "status": "pending", "message": f"{path.name} is missing."}
    rows = _read_csv(path)
    pending = any(str(row.get(field, "")).startswith("pending") for row in rows for field in fields)
    values = [_to_float(row.get(field)) for row in rows for field in fields]
    numeric = [value for value in values if value is not None]
    if pending or not numeric:
        return {"name": name, "severity": "warning", "status": "pending", "message": f"{path.name} still contains pending or nonnumeric result values."}
    if any(value < 0 for value in numeric):
        return {"name": name, "severity": "fatal", "status": "blocked", "message": f"{path.name} contains negative values in fields {fields}."}
    return {"name": name, "severity": "info", "status": "pass", "message": f"{path.name} contains numeric values.", "max_value": max(numeric)}


def _check_equilibrium(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"name": "equilibrium", "severity": "warning", "status": "pending", "message": "equilibrium_check.json is missing."}
    data = json.loads(path.read_text(encoding="utf-8"))
    status = data.get("status", "pending")
    return {"name": "equilibrium", "severity": "info" if status == "pass" else "warning", "status": status, "message": f"Equilibrium check status is {status}."}


def _check_strength(ir: dict[str, Any], path: Path) -> dict[str, Any]:
    material = (ir.get("semantic_ir", {}).get("materials") or [{}])[0]
    fy = _to_float(material.get("yield_strength_mpa"))
    if fy is None:
        return {"name": "strength_utilization", "severity": "warning", "status": "pending", "message": "Yield strength is missing."}
    if not path.exists():
        return {"name": "strength_utilization", "severity": "warning", "status": "pending", "message": "Door stress CSV is missing."}
    max_stress = max((_to_float(row.get("max_mises")) or 0.0 for row in _read_csv(path)), default=0.0)
    if max_stress <= 0:
        return {"name": "strength_utilization", "severity": "warning", "status": "pending", "message": "Maximum Mises stress is pending."}
    utilization = max_stress / fy
    return {
        "name": "strength_utilization",
        "severity": "warning" if utilization > 1.0 else "info",
        "status": "warning" if utilization > 1.0 else "pass",
        "message": f"Max Mises/yield utilization = {utilization:.3f}.",
        "utilization": utilization,
    }


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _to_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_status(path: Path) -> str:
    if not path.exists():
        return "pending"
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("status", "pending"))
    except Exception:
        return "blocked"
