from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def postprocess_l2(
    state: Any,
    *,
    settings: Any | None = None,
    odb_paths: dict[str, Path] | None = None,
    results_dir_override: Path | None = None,
) -> dict[str, Any]:
    """Aggregate L2 convergence diagnostics and optional deterministic ODB extraction."""
    root = results_dir_override or state.root / "results"
    root.mkdir(parents=True, exist_ok=True)
    sources = [Path(path) for path in (odb_paths or {}).values()]
    diagnostic_files: list[Path] = []
    if sources:
        for odb_path in sources:
            diagnostic_files.extend(
                candidate for suffix in (".sta", ".msg", ".dat")
                if (candidate := odb_path.with_suffix(suffix)).is_file()
            )
    else:
        candidates = list((state.root / "jobs").rglob("*.msg"))
        if candidates:
            latest_msg = max(candidates, key=lambda path: path.stat().st_mtime)
            diagnostic_files.extend(
                candidate for suffix in (".sta", ".msg", ".dat")
                if (candidate := latest_msg.with_suffix(suffix)).is_file()
            )
    text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in diagnostic_files
    )
    negative_values = [
        int(value) for value in re.findall(
            r"(?:HAS|WITH)\s+(\d+)\s+NEGATIVE\s+EIGENVALUES", text, re.IGNORECASE
        )
    ]
    negative = max(negative_values, default=0)
    negative_occurrences = len(re.findall(r"negative\s+eigenvalues?", text, re.IGNORECASE))
    cutbacks = len(re.findall(r"cutback|time increment.*reduced", text, re.IGNORECASE))
    severe = len(re.findall(r"severe\s+discontinuity", text, re.IGNORECASE))
    completed = "THE ANALYSIS HAS COMPLETED SUCCESSFULLY" in text.upper()

    extracted_path = state.root / "results" / "l2_solver_extract.json"
    if settings is not None:
        from .extractor import extract_l2_odb, latest_l2_odb

        source_odb = sources[0] if sources else latest_l2_odb(state)
        if source_odb is not None and source_odb.is_file() and (
            not extracted_path.is_file()
            or extracted_path.stat().st_mtime < source_odb.stat().st_mtime
        ):
            extract_l2_odb(state, settings, source_odb)
    extracted = _read_json(extracted_path)
    eta_f = _number(extracted.get("eta_force"))
    eta_m = _number(extracted.get("eta_moment"))
    pretension_error = _number(extracted.get("max_pretension_error_ratio"))
    numerical_ready = eta_f is not None and eta_m is not None and pretension_error is not None
    issues: list[dict[str, Any]] = []
    if not completed:
        issues.append(_issue("SOLVE_INCOMPLETE", "模型缺陷", "未找到三步分析成功完成记录。"))
    if negative:
        issues.append(_issue("NEGATIVE_EIGENVALUES", "模型缺陷", f"检测到 {negative} 处负特征值警告，正式参考前必须解释或消除。"))
    if eta_f is not None and eta_m is not None and (eta_f >= 0.01 or eta_m >= 0.01):
        issues.append(_issue("EQUILIBRIUM_TOLERANCE_EXCEEDED", "模型缺陷", "截面力或力矩平衡误差不满足 1% 门槛。"))
    if pretension_error is not None and pretension_error > 0.02:
        issues.append(_issue("PRETENSION_TARGET_MISSED", "模型缺陷", "目标预紧力误差超过 2%。"))
    if not numerical_ready:
        issues.append(_issue("NUMERICAL_OUTPUT_NOT_EXTRACTED", "环境错误", "尚未生成 L2 ODB 数值提取文件。"))
    status = (
        "blocked"
        if any(item["code"] != "NUMERICAL_OUTPUT_NOT_EXTRACTED" for item in issues)
        else ("pass" if completed and numerical_ready else "not_run")
    )
    payload = {
        "schema_version": "l2_result_summary_v1",
        "status": status,
        "project_id": state.project_id,
        "solver_completion": completed,
        "bolt_summary": extracted.get("bolts") or [],
        "contact_summary": extracted.get("contact") or {},
        "local_stress_summary": extracted.get("local_stress") or {},
        "equilibrium": {"eta_force": eta_f, "eta_moment": eta_m, "threshold": 0.01},
        "pretension": {"max_error_ratio": pretension_error, "threshold": 0.02},
        "convergence": {
            "increments": extracted.get("increments"), "cutbacks": cutbacks,
            "severe_discontinuity_iterations": severe,
            "negative_eigenvalue_warning_count": negative,
            "negative_eigenvalue_warning_occurrences": negative_occurrences,
        },
        "warning_classification": issues,
        "formal_reference_eligible": status == "pass" and numerical_ready,
    }
    (root / "results_summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "output_availability_report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def results_l2(state: Any) -> dict[str, Any]:
    for name in ("results_summary.json", "output_availability_report.json"):
        payload = _read_json(state.root / "results" / name)
        if payload:
            return payload
    return {"schema_version": "l2_result_summary_v1", "status": "not_run", "project_id": state.project_id}


def _issue(code: str, category: str, message: str) -> dict[str, str]:
    return {"code": code, "category": category, "message": message}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
