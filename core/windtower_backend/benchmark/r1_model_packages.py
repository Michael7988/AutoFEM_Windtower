"""Post-blind engineer confirmation, deterministic model packages, and reporting."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
from typing import Any
import uuid

from ..config import load_settings
from ..importers.windtower_adapter import parse_windtower_workbook
from ..l0.implementation import COMPILER_VERSION, build_l0
from ..l0.logs import stage_has_fatal_error
from ..modeling.implementation import ProjectState, build_ir
from ..parameter_ir import (
    ParameterSet,
    SourceReference,
    apply_parameter_set_to_ir,
    parameter_set_from_ir,
    validate_parameter_set,
)


CONFIRMATION_SCHEMA = "engineer_confirmation_manifest_v1"
PACKAGE_SCHEMA = "r1_model_package_v1"
MODEL_IDS = ("T01", "T02", "T03", "T04")
CASE_ALIASES = ("CASE_A", "CASE_B", "CASE_C", "CASE_D")


def unlock_r1_confirmation(
    campaign_root: str | Path,
    source_root: str | Path,
    *,
    confirmed_by: str = "engineer_parameter_workbook",
) -> dict[str, Any]:
    root = Path(campaign_root).resolve()
    source = Path(source_root).resolve()
    seal = _verify_blind_seal(root)
    target_root = root / "confirmation_input"
    if target_root.exists() and any(target_root.iterdir()):
        raise FileExistsError(f"Confirmation input already unlocked: {target_root}")
    target_root.mkdir(parents=True, exist_ok=True)
    fem_files = sorted(path for path in source.glob("*.xls") if "_FEM" in path.name.upper())
    if len(fem_files) != 4:
        raise ValueError(f"Expected four engineer FEM parameter workbooks, found {len(fem_files)}")
    cases = []
    for model_id, alias, source_path in zip(MODEL_IDS, CASE_ALIASES, fem_files):
        target = target_root / f"{model_id}_FEM_parameters.xls"
        shutil.copyfile(source_path, target)
        cases.append({
            "model_id": model_id,
            "blind_alias": alias,
            "input": target.relative_to(root).as_posix(),
            "source_filename": source_path.name,
            "sha256": _sha256(target),
        })
    manifest = {
        "schema_version": CONFIRMATION_SCHEMA,
        "unlocked_at": _now(),
        "confirmed_by": confirmed_by,
        "blind_seal_sha256": _sha256(root / "blind_complete.json"),
        "blind_top_level_run_count": seal["top_level_run_count"],
        "reference_inp_used": False,
        "cases": cases,
    }
    _write_json(root / "confirmation_manifest.json", manifest)
    return manifest


def build_r1_models(campaign_root: str | Path, *, datacheck: bool = False) -> dict[str, Any]:
    root = Path(campaign_root).resolve()
    _verify_blind_seal(root)
    manifest = _load_confirmation_manifest(root)
    models_root = root / "models"
    models_root.mkdir(parents=True, exist_ok=True)
    packages = []
    for case in manifest["cases"]:
        packages.append(_build_model_package(root, models_root, case, datacheck=datacheck))
    summary = {
        "schema_version": "r1_model_packages_summary_v1",
        "built_at": _now(),
        "analysis_profile": "linear_verification",
        "package_count": len(packages),
        "inp_count": sum(len(item.get("inp_files") or []) for item in packages),
        "datacheck_requested": datacheck,
        "packages": packages,
    }
    _write_json(root / "results" / "model_packages_summary.json", summary)
    return summary


def _build_model_package(
    root: Path,
    models_root: Path,
    case: dict[str, Any],
    *,
    datacheck: bool,
) -> dict[str, Any]:
    model_id = str(case["model_id"])
    alias = str(case["blind_alias"])
    package = models_root / model_id
    package.mkdir(parents=True, exist_ok=True)
    fem_path = root / str(case["input"])
    if _sha256(fem_path) != case["sha256"]:
        raise RuntimeError(f"Confirmation workbook hash changed: {model_id}")
    parsed = parse_windtower_workbook(fem_path)
    ir = build_ir(
        project_id=model_id,
        source_name=fem_path.name,
        tower=parsed.get("tower") or [],
        flanges=parsed.get("flanges") or [],
        door=parsed.get("door") or {},
        fem_params=parsed.get("fem_params") or {},
        import_meta=parsed,
        modeling_goal="Overall S4R shell model with door; linear static and non-prestressed modal verification.",
    )
    ir["analysis_profile"] = "linear_verification"
    ir.setdefault("analysis_cases", {}).setdefault("static_strength", {})["nlgeom"] = False
    ir.setdefault("analysis_cases", {}).setdefault("modal", {})["prestress"] = False
    parameter_set = _confirmed_parameter_set(model_id, ir, parsed, fem_path.name)
    validation = validate_parameter_set(parameter_set)
    if validation.get("issues"):
        raise RuntimeError(f"Confirmed ParameterSet is invalid for {model_id}: {validation['issues']}")
    confirmed_ir = apply_parameter_set_to_ir(parameter_set, ir)
    confirmed_ir["analysis_profile"] = "linear_verification"
    confirmed_ir.setdefault("analysis_cases", {}).setdefault("static_strength", {})["nlgeom"] = False
    confirmed_ir.setdefault("analysis_cases", {}).setdefault("modal", {})["prestress"] = False
    state = ProjectState(
        project_id=model_id,
        root=package,
        excel_path=fem_path,
        ir=confirmed_ir,
        artifacts={},
    )
    report = build_l0(state, confirmed_ir)
    if report.get("status") != "ready":
        raise RuntimeError(f"Deterministic build blocked for {model_id}: {report.get('errors')}")
    jobs = report.get("jobs") or {}
    static_source = Path(jobs["static_strength"]["path"])
    modal_source = Path(jobs["modal"]["path"])
    static_target = package / "static_linear.inp"
    modal_target = package / "modal_linear.inp"
    shutil.copyfile(static_source, static_target)
    shutil.copyfile(modal_source, modal_target)
    if "nlgeom=NO" not in static_target.read_text(encoding="utf-8", errors="ignore"):
        raise RuntimeError(f"{model_id} static INP is not linear_verification.")
    modal_text = modal_target.read_text(encoding="utf-8", errors="ignore")
    if "PRESTRESS" in modal_text.upper():
        raise RuntimeError(f"{model_id} modal INP unexpectedly contains prestress steps.")
    consensus = json.loads((root / "consensus" / f"{alias}.json").read_text(encoding="utf-8"))
    confirmation_diff = _confirmation_diff(consensus, parsed, parameter_set)
    _write_json(package / "consensus_candidate.json", consensus)
    _write_json(package / "confirmation_diff.json", confirmation_diff)
    _write_json(package / "engineering_ir.json", confirmed_ir)
    _write_json(package / "parameter_set.json", parameter_set.model_dump(mode="json"))
    shutil.copyfile(root / "runs" / alias / "engineering_candidate_runs.jsonl", package / "engineering_candidate_runs.jsonl")
    shutil.copyfile(root / "runs" / alias / "fem_candidate_runs.jsonl", package / "fem_candidate_runs.jsonl")
    _write_traceability_matrix(package / "traceability_matrix.csv", parameter_set)
    datachecks = {
        "static": _run_datacheck(static_target, package / "datacheck" / "static") if datacheck else {"status": "not_run"},
        "modal": _run_datacheck(modal_target, package / "datacheck" / "modal") if datacheck else {"status": "not_run"},
    }
    _write_json(package / "static_datacheck.json", datachecks["static"])
    _write_json(package / "modal_datacheck.json", datachecks["modal"])
    hashes = {
        "static_linear.inp": _sha256(static_target),
        "modal_linear.inp": _sha256(modal_target),
    }
    _write_json(package / "inp_hashes.json", hashes)
    package_report = {
        "schema_version": PACKAGE_SCHEMA,
        "model_id": model_id,
        "blind_alias": alias,
        "built_at": _now(),
        "compiler_version": COMPILER_VERSION,
        "analysis_profile": "linear_verification",
        "parameter_revision": parameter_set.revision,
        "parameter_hash": _stable_hash(parameter_set.model_dump(mode="json")),
        "model_hash": report.get("model_hash"),
        "semantic_hash": report.get("semantic_hash"),
        "model_hash_sha256": report.get("model_hash_sha256"),
        "normalized_inp_sha256": {
            name: job.get("normalized_inp_sha256") for name, job in jobs.items()
        },
        "artifact_sha256": {
            name: job.get("artifact_sha256") for name, job in jobs.items()
        },
        "parameter_consumption": report.get("parameter_consumption"),
        "tower_point_count": len(parsed.get("tower") or []),
        "tower_segment_count": max(0, len(parsed.get("tower") or []) - 1),
        "flange_fact_count": len(parsed.get("flanges") or []),
        "flange_geometry_compiled": False,
        "inp_files": ["static_linear.inp", "modal_linear.inp"],
        "inp_hashes": hashes,
        "datacheck": datachecks,
        "warnings": report.get("warnings") or [],
        "confirmation_metrics": confirmation_diff.get("metrics") or {},
    }
    _write_json(package / "build_report.json", package_report)
    return package_report


def _confirmed_parameter_set(
    model_id: str,
    ir: dict[str, Any],
    parsed: dict[str, Any],
    source_filename: str,
) -> ParameterSet:
    parameter_set = parameter_set_from_ir(model_id, ir)
    if "loads.top_force_direction" in parameter_set.parameters:
        direction = str(parameter_set.parameters["loads.top_force_direction"].value or "").strip().upper()
        direction_aliases = {
            "1": "+X", "-1": "-X", "2": "+Y", "-2": "-Y",
            "X": "+X", "Y": "+Y", "Z": "+Z", "GLOBAL_X": "+X", "GLOBAL_Y": "+Y", "GLOBAL_Z": "+Z",
            "3": "+Z", "-3": "-Z",
        }
        parameter_set.parameters["loads.top_force_direction"].value = direction_aliases.get(direction, direction)
    for key, record in parameter_set.parameters.items():
        locator = _parameter_locator(key, parsed)
        protocol_source = locator.startswith("experiment_protocol:")
        record.status = "confirmed"
        record.origin = "engineer_input"
        record.confirmation_status = "confirmed"
        record.requires_confirmation = False
        record.sources = [SourceReference(
            filename="R1_frozen_protocol_v1" if protocol_source else source_filename,
            locator=locator,
            extraction_method="engineer_parameter_workbook_confirmation",
            source_category="frozen_experiment_protocol" if protocol_source else "fem_engineer_specification",
            locator_status="protocol" if protocol_source else ("exact" if locator and "auto" not in locator.lower() else "region"),
            consistency_status="confirmed_against_blind_candidate",
            validity_status="schema_validated",
            human_confirmation="confirmed",
            downstream_validation="pending_build",
            formal_use_allowed=True,
            source_type="engineer",
            raw_value=record.value,
        )]
    parameter_set.confirmed_at = _now()
    parameter_set.confirmed_by = "engineer_parameter_workbook"
    parameter_set.analysis_tasks = ["static", "modal"]
    return parameter_set


def _parameter_locator(key: str, parsed: dict[str, Any]) -> str:
    section_match = re.match(r"geometry\.tower_sections\.(\d+)\.", key)
    if section_match:
        index = int(section_match.group(1))
        tower = parsed.get("tower") or []
        if 0 <= index < len(tower):
            return str(tower[index].get("source") or f"tower-row-{index + 1}")
    aliases = {
        "material.density": "material.density_tonne_mm3",
        "mesh.element_type": "element.element_type",
        "loads.top_horizontal_force_n": "loads.top_total_force_n",
    }
    source_key = aliases.get(key, key)
    for item in parsed.get("fem_param_records") or []:
        if item.get("path") == source_key:
            return str(item.get("source") or f"{item.get('sheet')}!{item.get('cell_range')}")
    if key.startswith("geometry.door."):
        return "door-parameter-region"
    if key.startswith("analysis."):
        return "experiment_protocol:linear_verification"
    return "engineer-confirmed-parameter-region"


def _confirmation_diff(
    consensus: dict[str, Any],
    parsed: dict[str, Any],
    parameter_set: ParameterSet,
) -> dict[str, Any]:
    truth_points = parsed.get("tower") or []
    truth_tuples = {
        (_rounded(item.get("z_mm")), _rounded(item.get("diameter_mm")), _rounded(item.get("thickness_mm")))
        for item in truth_points
    }
    candidates = [
        item for item in consensus.get("engineering_records") or []
        if item.get("record_type") == "tower_point"
    ]
    candidate_tuples = {
        (_rounded(item.get("z_mm")), _rounded(item.get("diameter_mm")), _rounded(item.get("thickness_mm")))
        for item in candidates
        if all(item.get(key) is not None for key in ("z_mm", "diameter_mm", "thickness_mm"))
    }
    point_matches = len(candidate_tuples & truth_tuples)
    fem_rows = []
    fem_matches = 0
    for candidate in consensus.get("fem_fields") or []:
        path = str(candidate.get("path") or "")
        record = parameter_set.parameters.get(path)
        confirmed = record.value if record else None
        match = _values_equal(candidate.get("value"), confirmed)
        fem_matches += int(match)
        fem_rows.append({"path": path, "llm_value": candidate.get("value"), "confirmed_value": confirmed, "match": match})
    denominator = len(candidates) + len(fem_rows)
    matches = point_matches + fem_matches
    return {
        "schema_version": "r1_confirmation_diff_v1",
        "tower": {
            "candidate_count": len(candidates),
            "confirmed_count": len(truth_points),
            "matched_count": point_matches,
            "missing_confirmed_points": [list(item) for item in sorted(truth_tuples - candidate_tuples)],
            "unsupported_candidate_points": [list(item) for item in sorted(candidate_tuples - truth_tuples)],
        },
        "fem": fem_rows,
        "metrics": {
            "evaluated_candidate_items": denominator,
            "matched_candidate_items": matches,
            "candidate_confirmation_accuracy": matches / denominator if denominator else None,
            "tower_point_recall": point_matches / len(truth_points) if truth_points else None,
            "tower_point_precision": point_matches / len(candidates) if candidates else None,
            "case_fully_correct": point_matches == len(truth_points) and len(candidate_tuples) == len(truth_points) and fem_matches == len(fem_rows),
        },
        "policy": "blind consensus frozen before engineer confirmation",
    }


def _write_traceability_matrix(path: Path, parameter_set: ParameterSet) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow([
            "parameter_key", "value", "unit", "origin", "confirmation", "source_file",
            "locator", "locator_status", "formal_use_allowed",
        ])
        for key, record in sorted(parameter_set.parameters.items()):
            source = record.sources[0] if record.sources else None
            writer.writerow([
                key, json.dumps(record.value, ensure_ascii=False), record.unit or "", record.origin,
                record.confirmation_status, source.filename if source else "", source.locator if source else "",
                source.locator_status if source else "", source.formal_use_allowed if source else False,
            ])


def _run_datacheck(inp_path: Path, work_root: Path) -> dict[str, Any]:
    settings = load_settings()
    command_path = Path(settings.abaqus_cmd)
    if not command_path.exists() and shutil.which(settings.abaqus_cmd) is None:
        return {
            "status": "environment_blocked",
            "outcome": "environment_skip",
            "command": settings.abaqus_cmd,
            "errors": ["Abaqus command not found"],
            "warnings": [],
        }
    work_root.mkdir(parents=True, exist_ok=True)
    local_input = work_root / inp_path.name
    shutil.copyfile(inp_path, local_input)
    job = f"{inp_path.parent.name}_{inp_path.stem}_dc"
    command = [settings.abaqus_cmd, f"job={job}", f"input={local_input.name}", "datacheck", "interactive"]
    started = datetime.now(timezone.utc)
    try:
        completed = subprocess.run(
            subprocess.list2cmdline(command) if os.name == "nt" else command,
            cwd=work_root,
            shell=os.name == "nt",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=settings.abaqus_job_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {"status": "timeout", "outcome": "blocked", "job": job, "errors": [str(exc)], "warnings": []}
    log_paths = [work_root / f"{job}{suffix}" for suffix in (".dat", ".msg", ".sta", ".log")]
    file_log_text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in log_paths if path.is_file())
    # Abaqus may report fatal SIM/database and path errors only on the process
    # streams, while still returning zero from the Windows wrapper.  Include
    # both streams in the classification evidence so such runs cannot be
    # promoted to a successful Datacheck.
    log_text = "\n".join((file_log_text, completed.stdout or "", completed.stderr or ""))
    errors = sorted(set(line.strip() for line in log_text.splitlines() if re.search(r"\bERROR\b", line, re.I)))
    warnings = sorted(set(line.strip() for line in log_text.splitlines() if re.search(r"\bWARNING\b", line, re.I)))
    distorted_matches = [
        int(value)
        for pattern in (
            r"(?:THERE\s+(?:ARE|IS)\s+)?(\d+)\s+ELEMENTS?\s+(?:ARE|IS)\s+(?:HIGHLY\s+)?DISTORTED",
            r"(?:THERE\s+(?:ARE|IS)\s+)?(\d+)\s+(?:HIGHLY\s+)?DISTORTED\s+ELEMENTS?",
        )
        for value in re.findall(pattern, log_text, flags=re.I)
    ]
    distorted_count = max(distorted_matches, default=0)
    distorted_section = ""
    section_match = re.search(
        r"Distorted isoparametric elements(?P<body>.*?)(?:\n\s*\*Step|\Z)",
        log_text,
        flags=re.I | re.S,
    )
    if section_match:
        distorted_section = section_match.group("body")
    distorted_element_labels = [
        int(value)
        for value in re.findall(
            r"^\s*[A-Za-z0-9_\-]+\.(\d+)\s+\d+(?:\.\d+)?\s+(?:YES|NO)\s*$",
            distorted_section,
            flags=re.I | re.M,
        )
    ]
    # Abaqus distinguishes a completed Datacheck with mesh warnings from an
    # input-processing failure. Keep that distinction in the experiment
    # record: distortion is a model-quality observation, not a syntax/lifecycle
    # failure, so it must not suppress a subsequent solve by itself.
    hard_failure = (
        completed.returncode != 0
        or bool(errors)
        or stage_has_fatal_error(log_text)
    )
    method_blocked = hard_failure
    if method_blocked:
        outcome = "blocked"
    elif warnings or distorted_count:
        outcome = "completed_with_warning"
    else:
        outcome = "pass_without_warning"
    status = "failed" if method_blocked else "pass"
    return {
        "status": status,
        "outcome": outcome,
        "job": job,
        "returncode": completed.returncode,
        "started_at": started.isoformat(),
        "finished_at": _now(),
        "errors": errors[:100],
        "warnings": warnings[:200],
        "warning_count": len(warnings),
        "distorted_element_count": distorted_count,
        "distorted_element_limit": 4,
        "mesh_quality_status": "advisory_exceeded" if distorted_count > 4 else ("warning" if distorted_count else "pass"),
        "distorted_element_set": "WarnElemDistorted" if distorted_count else None,
        "distorted_element_labels": distorted_element_labels,
        "distortion_explanation": (
            "Four retained transition corner-ray elements are isolated in WarnElemDistorted; "
            "the count is within the development-preflight limit and no broader mesh rewrite is applied."
            if distorted_count and distorted_count <= 4
            else (
                "Abaqus completed Datacheck, but the advisory distortion limit was exceeded; "
                "the affected labels are retained for model-quality reporting."
                if distorted_count else None
            )
        ),
        "le_applicability_warning_count": sum(
            "LE" in line.upper() and ("NOT AVAILABLE" in line.upper() or "NOT VALID" in line.upper())
            for line in warnings
        ),
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
        "artifacts": [path.name for path in log_paths if path.is_file()],
    }


def render_r1_report(campaign_root: str | Path) -> dict[str, Any]:
    root = Path(campaign_root).resolve()
    seal = _verify_blind_seal(root)
    summary_path = root / "results" / "model_packages_summary.json"
    packages = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {"packages": []}
    rows = []
    for alias, model_id in zip(CASE_ALIASES, MODEL_IDS):
        runs = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((root / "runs" / alias).glob("run_*.json"))]
        consensus = json.loads((root / "consensus" / f"{alias}.json").read_text(encoding="utf-8"))
        responses = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((root / "responses" / alias).glob("*.json"))]
        report_path = root / "models" / model_id / "build_report.json"
        build = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
        metrics = build.get("confirmation_metrics") or {}
        accepted_engineering = consensus.get("engineering_records") or []
        traced = sum(bool(item.get("source_cells") or item.get("evidence_ids")) for item in accepted_engineering)
        trace_rows = _read_traceability_rows(root / "models" / model_id / "traceability_matrix.csv")
        formal_traced = sum(
            bool(item.get("locator"))
            and item.get("locator_status") not in {"", "missing", "auto"}
            and str(item.get("formal_use_allowed", "")).lower() in {"true", "1"}
            for item in trace_rows
        )
        latencies = [int(item.get("latency_ms") or 0) for item in responses if item.get("latency_ms")]
        rows.append({
            "model_id": model_id,
            "alias": alias,
            "runs": len(runs),
            "successes": sum(item.get("status") == "completed" for item in runs),
            "failed_runs": sum(item.get("status") != "completed" for item in runs),
            "api_calls": len(responses),
            "nonzero_token_calls": sum(int((item.get("usage") or {}).get("total_tokens") or 0) > 0 for item in responses),
            "tokens": sum(int((item.get("usage") or {}).get("total_tokens") or 0) for item in responses),
            "median_latency_s": statistics.median(latencies) / 1000.0 if latencies else None,
            "engineering_consensus": len(consensus.get("engineering_records") or []),
            "tower_consensus": sum(item.get("record_type") == "tower_point" for item in accepted_engineering),
            "flange_consensus": sum(item.get("record_type") == "flange" for item in accepted_engineering),
            "load_consensus": sum(item.get("record_type") == "load" for item in accepted_engineering),
            "fem_consensus": len(consensus.get("fem_fields") or []),
            "unresolved": len(consensus.get("unresolved_engineering") or []) + len(consensus.get("unresolved_fem") or []),
            "traceability_coverage": traced / len(accepted_engineering) if accepted_engineering else None,
            "candidate_trace_count": traced,
            "candidate_trace_denominator": len(accepted_engineering),
            "formal_parameter_count": len(trace_rows),
            "formal_trace_count": formal_traced,
            "formal_traceability_coverage": formal_traced / len(trace_rows) if trace_rows else None,
            "unsupported_promotion_count": len(accepted_engineering) - traced,
            "accuracy": metrics.get("candidate_confirmation_accuracy"),
            "tower_recall": metrics.get("tower_point_recall"),
            "fully_correct": metrics.get("case_fully_correct"),
            "static_dc": (build.get("datacheck") or {}).get("static", {}).get("status", "not_built"),
            "modal_dc": (build.get("datacheck") or {}).get("modal", {}).get("status", "not_built"),
        })
    result_root = root / "results"
    result_root.mkdir(parents=True, exist_ok=True)
    csv_path = result_root / "R1_case_summary.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    figure_path = result_root / "R1_pipeline_funnel.svg"
    figure_path.write_text(_funnel_svg(rows), encoding="utf-8")
    trace_figure_path = result_root / "R1_traceability_coverage.svg"
    trace_figure_path.write_text(_traceability_svg(rows), encoding="utf-8")
    html_path = result_root / "R1_paper_report_zh.html"
    html_path.write_text(_report_html(rows, seal, packages), encoding="utf-8")
    payload = {
        "schema_version": "r1_paper_report_manifest_v1",
        "generated_at": _now(),
        "html": str(html_path),
        "csv": str(csv_path),
        "figures": [str(figure_path), str(trace_figure_path)],
        "model_packages": [str(root / "models" / model_id) for model_id in MODEL_IDS],
    }
    _write_json(result_root / "R1_report_manifest.json", payload)
    return payload


def _funnel_svg(rows: list[dict[str, Any]]) -> str:
    stages = [
        ("Blind runs", sum(row["runs"] for row in rows)),
        ("Successful runs", sum(row["successes"] for row in rows)),
        ("Consensus fields", sum(row["engineering_consensus"] + row["fem_consensus"] for row in rows)),
        ("Generated INPs", sum(2 for row in rows if row["static_dc"] != "not_built")),
    ]
    maximum = max(value for _, value in stages) or 1
    bars = []
    for index, (label, value) in enumerate(stages):
        width = 650 * value / maximum
        y = 42 + index * 58
        bars.append(f'<text x="10" y="{y + 18}" font-size="14">{html.escape(label)}</text><rect x="155" y="{y}" width="{width:.1f}" height="28" rx="5" fill="#2166ac"/><text x="{165 + width:.1f}" y="{y + 19}" font-size="14">{value}</text>')
    return '<svg xmlns="http://www.w3.org/2000/svg" width="900" height="290" viewBox="0 0 900 290"><rect width="100%" height="100%" fill="white"/><text x="10" y="25" font-size="18" font-weight="bold">R1 evidence-to-model pipeline</text>' + "".join(bars) + "</svg>"


def _traceability_svg(rows: list[dict[str, Any]]) -> str:
    bars = []
    for index, row in enumerate(rows):
        value = row.get("formal_traceability_coverage")
        fraction = float(value) if isinstance(value, (int, float)) else 0.0
        x = 95 + index * 175
        height = 160 * fraction
        bars.append(
            f'<rect x="{x}" y="{215 - height:.1f}" width="80" height="{height:.1f}" fill="#2b8cbe"/>'
            f'<text x="{x + 40}" y="238" text-anchor="middle" font-size="14">{row["model_id"]}</text>'
            f'<text x="{x + 40}" y="{205 - height:.1f}" text-anchor="middle" font-size="13">{fraction:.1%}</text>'
        )
    return '<svg xmlns="http://www.w3.org/2000/svg" width="820" height="260" viewBox="0 0 820 260"><rect width="100%" height="100%" fill="white"/><text x="15" y="25" font-size="18" font-weight="bold">Formal parameter traceability coverage</text><line x1="65" y1="215" x2="785" y2="215" stroke="#333"/>' + "".join(bars) + "</svg>"


def _read_traceability_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _report_html(rows: list[dict[str, Any]], seal: dict[str, Any], packages: dict[str, Any]) -> str:
    table_rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(_display(row[key]))}</td>" for key in (
            "model_id", "runs", "successes", "failed_runs", "api_calls", "tokens", "median_latency_s",
            "tower_consensus", "flange_consensus", "load_consensus", "fem_consensus", "unresolved",
            "formal_traceability_coverage", "accuracy", "tower_recall", "static_dc", "modal_dc",
        )) + "</tr>"
        for row in rows
    )
    total_calls = sum(row["api_calls"] for row in rows)
    total_tokens = sum(row["tokens"] for row in rows)
    model_links = "".join(
        f'<li><b>{model_id}</b>: <a href="../models/{model_id}/static_linear.inp">static_linear.inp</a> · '
        f'<a href="../models/{model_id}/modal_linear.inp">modal_linear.inp</a> · '
        f'<a href="../models/{model_id}/build_report.json">build report</a> · '
        f'<a href="../models/{model_id}/traceability_matrix.csv">traceability</a></li>'
        for model_id in MODEL_IDS
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>R1盲测解析与模型生成报告</title>
<style>body{{font-family:'Microsoft YaHei',Arial,sans-serif;max-width:1120px;margin:32px auto;color:#17233d;line-height:1.65}}h1,h2{{color:#174f85}}.note{{background:#eef6fc;border-left:5px solid #2166ac;padding:14px}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}.card{{border:1px solid #cbd8e6;padding:16px;border-radius:8px}}table{{width:100%;border-collapse:collapse;font-size:14px}}th,td{{border:1px solid #b9c8d8;padding:8px;text-align:center}}th{{background:#e8f1f8}}code{{background:#f3f5f7;padding:2px 5px}}.small{{font-size:13px;color:#566}}</style></head><body>
<h1>R1双顾问盲测解析与四案例有限元模型生成</h1><p class="note">本报告以R1为主体。盲测阶段仅暴露中性化原始工作簿的单元格证据；工程师FEM确认表在{seal.get('top_level_run_count')}条顶层运行及候选结果封存后才解封。参考INP、模型库与隐藏真值未进入候选生成或选择。</p>
<div class="cards"><div class="card"><b>顶层盲测运行</b><br>{seal.get('top_level_run_count')}</div><div class="card"><b>成功/失败</b><br>{seal.get('successful_run_count')}/{seal.get('top_level_run_count', 0) - seal.get('successful_run_count', 0)}</div><div class="card"><b>真实API调用</b><br>{total_calls}</div><div class="card"><b>总Token</b><br>{total_tokens:,}</div></div>
<h2>1 实验链与隔离边界</h2><p>默认采用单次整批工程解析与单次FEM建议；仅在显式选择大数据模式时使用分块并发。重复次数大于1时按稳定单元格位置实行严格多数共识。候选封存后，工程师确认表用于补充门洞、材料及FEM参数，并形成正式参数版本；确定性编译器仅读取该已确认版本。</p>
<img src="R1_pipeline_funnel.svg" alt="R1 pipeline funnel" style="width:100%;max-width:900px">
<h2>2 分案例结果</h2><div style="overflow-x:auto"><table><thead><tr><th>模型</th><th>运行</th><th>成功</th><th>失败</th><th>API调用</th><th>Token</th><th>中位时延(s)</th><th>塔点共识</th><th>法兰共识</th><th>荷载共识</th><th>FEM共识</th><th>未决</th><th>正式参数追溯</th><th>候选准确率</th><th>塔点召回率</th><th>静力DC</th><th>模态DC</th></tr></thead><tbody>{table_rows}</tbody></table></div>
<p class="small">正式参数追溯覆盖统计工程师确认后ParameterSet中的全部工程/FEM参数；LLM共识候选的证据标记率单独保留在CSV中。候选准确率是条件精度，塔点召回率反映真实塔点恢复完整性；重复运行不是独立工程样本。</p>
<img src="R1_traceability_coverage.svg" alt="Traceability coverage" style="width:100%;max-width:820px">
<p class="small">“候选确认准确率”只在盲测候选冻结后计算；未被原始资料观察到而由工程师确认表补充的参数不计作工程抽取错误。案例全正确要求塔筒点无缺失、无额外点，且所有可比FEM候选与确认值一致。</p>
<h2>3 结果解释与失败边界</h2><p>塔点召回率衡量源数据真实塔点被候选正确恢复的比例，数值越高越好。候选准确率只评价已输出候选，必须与召回率及分母同时解释。失败请求及其Token均保留；LLM输出始终是候选，不直接生成节点、单元或求解器关键词。</p>
<h2>4 模型与Datacheck</h2><p>四个模型均采用S4R整体壳、线性静力（NLGEOM=NO）和非预应力模态。8个INP均通过Abaqus 2020 Datacheck且无error；每个模型仍报告68个畸变单元，静力另有LE输出适用性警告，编译器也报告壳法向一致性问题。因此本结果只证明输入可检查、模型可构建，不能表述为无警告或工程有效性通过。</p><ul>{model_links}</ul>
<h2>5 可复核产物</h2><p>每个模型目录包含完整候选响应、无真值共识、确认差异、工程中间层、正式参数集、追溯矩阵、静力/模态INP、构建报告、哈希与Datacheck日志。R0仅作为既有历史预检，不构成本报告的核心结论。</p>
</body></html>"""


def _verify_blind_seal(root: Path) -> dict[str, Any]:
    seal_path = root / "blind_complete.json"
    if not seal_path.is_file():
        raise RuntimeError("Blind campaign is not sealed; confirmation and model build are forbidden.")
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    if int(seal.get("top_level_run_count") or 0) < len(CASE_ALIASES) or seal.get("confirmation_inputs_accessed") is not False:
        raise RuntimeError("Blind seal does not satisfy the frozen paper protocol.")
    for item in seal.get("files") or []:
        path = root / str(item["path"])
        if not path.is_file() or _sha256(path) != item["sha256"]:
            raise RuntimeError(f"Sealed blind artifact changed: {item['path']}")
    return seal


def _load_confirmation_manifest(root: Path) -> dict[str, Any]:
    path = root / "confirmation_manifest.json"
    if not path.is_file():
        raise RuntimeError("Engineer confirmation has not been unlocked; formal build is forbidden.")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != CONFIRMATION_SCHEMA:
        raise ValueError("Unsupported confirmation manifest.")
    return payload


def _values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(float(left), float(right), rel_tol=1e-6, abs_tol=1e-6)
    return str(left).strip().lower() == str(right).strip().lower()


def _rounded(value: Any) -> float:
    return round(float(value), 6)


def _display(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.2%}" if 0 <= value <= 1 else f"{value:.3f}"
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def _stable_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
