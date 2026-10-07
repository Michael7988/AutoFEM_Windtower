from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from ..automation import SingleL0WorkflowOptions, run_project_l0_workflow
from ..config import Settings, load_settings
from ..importers.candidate_mapping import candidate_mapping_to_standard_input, compare_standard_input_to_ground_truth
from ..importers.standard_input import normalize_import_to_standard_input
from ..modeling import create_project, get_state
from ..qwen_client import qwen_normalize_standard_input
from .dataset import DEFAULT_BENCHMARK_ROOT, DISCLAIMER, generate_llm_benchmark


SUPPORTED_LLM_MODES = {"off", "adaptive", "forced_parse", "forced_review"}


def run_llm_benchmark(
    case_id: str = "S01",
    llm_mode: str = "forced_parse",
    analyses: tuple[str, ...] = ("static_strength", "modal"),
    run_abaqus: bool = False,
    root: Path | None = None,
    settings: Settings | None = None,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    """Run one evidence-gated heterogeneous-input benchmark case.

    Hidden truth is loaded only after the model response.  ``attempt_id`` gives
    repeated research runs an isolated output directory while preserving the
    historical S01 path when it is omitted.
    """
    case_id = case_id.upper()
    if llm_mode not in SUPPORTED_LLM_MODES:
        raise ValueError(f"Unsupported llm_mode={llm_mode!r}.")
    if llm_mode != "forced_parse":
        raise ValueError("The hidden-truth acceptance experiment requires llm_mode=forced_parse.")
    invalid_analyses = set(analyses) - {"static_strength", "modal"}
    if invalid_analyses or set(analyses) != {"static_strength", "modal"}:
        raise ValueError("Phase one requires both static_strength and modal analyses.")

    settings = settings or load_settings()
    root = Path(root or DEFAULT_BENCHMARK_ROOT)
    manifest_path = root / "benchmark_manifest.json"
    if not manifest_path.exists():
        generate_llm_benchmark(root)
    manifest = _read_json(manifest_path)
    cases = {str(item["case_id"]).upper(): item for item in manifest["cases"]}
    if case_id not in cases:
        raise ValueError(
            f"Unknown benchmark case {case_id!r}; expected one of {sorted(cases)}."
        )
    case = cases[case_id]
    input_path = Path(case["input"])
    truth_path = Path(case["ground_truth"])
    if not input_path.is_absolute():
        input_path = root / input_path
    if not truth_path.is_absolute():
        truth_path = root / truth_path
    run_dir = root / "runs" / case_id
    if attempt_id:
        safe_attempt_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", attempt_id).strip("._")
        if not safe_attempt_id:
            raise ValueError("attempt_id must contain at least one safe character.")
        run_dir = run_dir / safe_attempt_id
    run_dir.mkdir(parents=True, exist_ok=True)
    _archive_previous_attempt(run_dir)

    audit: dict[str, Any] = {
        "schema_version": "llm_cae_benchmark_run_v1",
        "case_id": case_id,
        "declaration": DISCLAIMER,
        "llm_mode": llm_mode,
        "analyses": list(analyses),
        "run_abaqus": run_abaqus,
        "attempt_id": attempt_id,
        "input": str(input_path),
        "ground_truth_file": str(truth_path),
        "stages": {},
        "promotion_status": "not_started",
        "failure_classification": None,
        "engineering_disclaimer": "可计算仅表示完成指定数值求解，不代表结构满足设计规范或安全验收。",
    }

    baseline = normalize_import_to_standard_input(input_path, enable_llm=False, force_llm=False)
    baseline_pass = bool(
        baseline.get("input_format") == "unknown_llm_assisted"
        and (baseline.get("validation_report") or {}).get("blocked")
    )
    audit["stages"]["rule_only_control"] = {
        "status": "pass" if baseline_pass else "failed",
        "input_format": baseline.get("input_format"),
        "parse_strategy": baseline.get("parse_strategy"),
        "validation_status": (baseline.get("validation_report") or {}).get("status"),
        "blocked": (baseline.get("validation_report") or {}).get("blocked"),
    }
    _write_json(run_dir / "rule_only_control.json", baseline)
    if not baseline_pass:
        return _stop(audit, run_dir, "llm_mapping", "S01 rule-only control unexpectedly recognized or passed.")

    llm_result = qwen_normalize_standard_input(
        settings,
        baseline.get("workbook_profile") or {},
        baseline.get("validation_report") or {},
    )
    candidate = llm_result.pop("candidate", None)
    audit["stages"]["llm_call"] = {
        **llm_result,
        "candidate_available": bool(candidate),
        "api_key_persisted": False,
    }
    _write_json(run_dir / "llm_call_audit.json", audit["stages"]["llm_call"])
    if candidate is not None:
        _write_json(run_dir / "candidate_mapping_ir.json", candidate)
    if (
        llm_result.get("provider") != "qwen"
        or llm_result.get("status") != "called"
        or int(llm_result.get("total_tokens") or 0) <= 0
        or not candidate
    ):
        return _stop(audit, run_dir, "llm_mapping", "Qwen was not successfully called or returned an empty candidate.")

    converted = candidate_mapping_to_standard_input(candidate, input_path, require_benchmark_fields=True)
    candidate_validation = converted.get("candidate_validation") or converted.get("validation_report") or {}
    audit["stages"]["candidate_validation"] = candidate_validation
    _write_json(run_dir / "candidate_validation.json", candidate_validation)
    if converted.get("promotion_status") == "blocked" or candidate_validation.get("blocked"):
        return _stop(audit, run_dir, "llm_mapping", "CandidateMappingIR schema, evidence, units, or critical-field gate failed.")

    # Deliberately load truth only after the external model response has been received.
    truth = _read_json(truth_path)
    truth_comparison = compare_standard_input_to_ground_truth(converted, truth, relative_tolerance=0.001)
    audit["stages"]["hidden_truth_gate"] = truth_comparison
    audit["field_accuracy"] = truth_comparison.get("accuracy")
    audit["critical_field_gate"] = truth_comparison.get("status")
    _write_json(run_dir / "hidden_truth_comparison.json", truth_comparison)
    if truth_comparison.get("blocked"):
        return _stop(audit, run_dir, "llm_mapping", "LLM candidate disagreed with hidden truth for one or more critical fields.")

    audit["promotion_status"] = "promoted_by_hidden_truth_gate"
    promoted = converted
    promoted["promotion_status"] = audit["promotion_status"]
    promoted["requires_user_confirmation"] = False
    promoted["llm_parse_assist"] = audit["stages"]["llm_call"]
    _write_json(run_dir / "standard_input_package.json", promoted.get("standard_input_package"))

    try:
        project = create_project(
            input_path.name,
            input_path.read_bytes(),
            modeling_goal="Synthetic benchmark: deterministic S4R shell static strength and 20-mode modal analyses.",
            imported=promoted,
        )
        state = get_state(str(project["project_id"]))
        audit["project_id"] = state.project_id
        audit["project_root"] = str(state.root)
        audit["stages"]["deterministic_ir"] = {
            "status": "created",
            "producer": "windtower_backend.modeling.create_project",
            "llm_wrote_formal_ir": False,
            "ir_path": str(state.root / "ir.json"),
        }
        workflow = run_project_l0_workflow(
            state,
            SingleL0WorkflowOptions(
                excel_path=input_path,
                job_type="all",
                run_abaqus=run_abaqus,
                extract_odb=run_abaqus,
                project_id=state.project_id,
            ),
            settings,
        )
    except Exception as exc:
        audit["stages"]["deterministic_compiler"] = {"status": "failed", "error": str(exc)}
        return _stop(audit, run_dir, "deterministic_compiler", str(exc))

    audit["stages"]["deterministic_workflow"] = workflow
    audit["artifacts"] = _public_artifacts(workflow)
    audit["execution_summary"] = _execution_summary(workflow)
    _write_json(run_dir / "deterministic_workflow.json", workflow)
    workflow_status = _workflow_acceptance(workflow, run_abaqus)
    audit["stages"]["acceptance"] = workflow_status
    if workflow_status["status"] != "pass":
        failure_class = workflow_status.get("failure_classification") or "solver_model"
        return _stop(audit, run_dir, failure_class, workflow_status.get("reason") or "Workflow acceptance failed.")

    audit["status"] = "pass"
    audit["failure_classification"] = None
    _write_reports(run_dir, audit)
    return audit


def _workflow_acceptance(workflow: dict[str, Any], run_abaqus: bool) -> dict[str, Any]:
    steps = workflow.get("steps") or {}
    required_compile = ("validate_l0", "mesh_l0", "build_l0")
    failed_compile = [name for name in required_compile if (steps.get(name) or {}).get("status") in {None, "blocked", "failed"}]
    if failed_compile:
        return {"status": "failed", "failure_classification": "deterministic_compiler", "reason": f"Compiler stages failed: {failed_compile}"}
    if not run_abaqus:
        return {"status": "pass", "scope": "compile_only", "reason": "Abaqus execution was not requested."}
    for name in ("datacheck_l0", "run_l0", "postprocess_l0"):
        status = (steps.get(name) or {}).get("status")
        if status in {None, "blocked", "failed", "skipped"}:
            diagnostics = ((steps.get(name) or {}).get("payload") or {}).get("diagnostics") or []
            text = json.dumps(diagnostics, ensure_ascii=False).lower()
            infra_tokens = ("not found", "license", "command", "executable", "timeout", "permission")
            failure_class = "abaqus_infrastructure" if any(token in text for token in infra_tokens) else "solver_model"
            return {"status": "failed", "failure_classification": failure_class, "reason": f"{name} status={status}", "diagnostics": diagnostics}
    artifacts = workflow.get("artifacts") or {}
    odbs = [path for name, path in artifacts.items() if str(name).lower().endswith(".odb") and Path(path).exists() and Path(path).stat().st_size > 0]
    if len(odbs) < 2:
        return {"status": "failed", "failure_classification": "solver_model", "reason": f"Expected two non-empty ODB files, found {len(odbs)}.", "odb_paths": odbs}
    return {"status": "pass", "scope": "compile_datacheck_solve_odb", "odb_paths": odbs}


def _public_artifacts(workflow: dict[str, Any]) -> dict[str, Any]:
    artifacts = workflow.get("artifacts") or {}
    return {
        "inp_paths": [path for name, path in artifacts.items() if str(name).lower().endswith(".inp")],
        "odb_paths": [path for name, path in artifacts.items() if str(name).lower().endswith(".odb")],
        "workflow_report": workflow.get("analysis_report"),
    }


def _execution_summary(workflow: dict[str, Any]) -> dict[str, Any]:
    steps = workflow.get("steps") or {}
    return {
        "ir_validation": (steps.get("validate_l0") or {}).get("status"),
        "mesh_and_sets": (steps.get("mesh_l0") or {}).get("status"),
        "inp_compilation": (steps.get("build_l0") or {}).get("status"),
        "datacheck": (steps.get("datacheck_l0") or {}).get("status"),
        "solve": (steps.get("run_l0") or {}).get("status"),
        "odb_postprocess": (steps.get("postprocess_l0") or {}).get("status"),
        "result_sanity": (steps.get("result_sanity") or {}).get("status"),
    }


def _stop(audit: dict[str, Any], run_dir: Path, failure_class: str, reason: str) -> dict[str, Any]:
    audit["status"] = "blocked"
    audit["failure_classification"] = failure_class
    audit["failure_reason"] = reason
    if audit.get("promotion_status") == "not_started":
        audit["promotion_status"] = "blocked"
    _write_reports(run_dir, audit)
    return audit


def _write_reports(run_dir: Path, audit: dict[str, Any]) -> None:
    _write_json(run_dir / "audit_report.json", audit)
    acceptance = (audit.get("stages") or {}).get("acceptance") or {}
    llm = (audit.get("stages") or {}).get("llm_call") or {}
    artifacts = audit.get("artifacts") or {}
    lines = [
        f"# {audit.get('case_id')} LLM-CAE benchmark audit",
        "",
        f"> {audit.get('declaration')}",
        "",
        f"- Overall status: `{audit.get('status')}`",
        f"- Promotion: `{audit.get('promotion_status')}`",
        f"- Failure classification: `{audit.get('failure_classification') or 'none'}`",
        f"- Qwen status/model/tokens: `{llm.get('status')}` / `{llm.get('model')}` / `{llm.get('total_tokens', 0)}`",
        f"- Critical-field accuracy: `{audit.get('field_accuracy', 'not evaluated')}`",
        f"- Deterministic/Abaqus acceptance: `{acceptance.get('status', 'not reached')}`",
        f"- INP paths: `{artifacts.get('inp_paths', [])}`",
        f"- ODB paths: `{artifacts.get('odb_paths', [])}`",
        "",
        f"Failure reason: {audit.get('failure_reason', 'none')}",
        "",
        f"Engineering limitation: {audit.get('engineering_disclaimer')}",
    ]
    (run_dir / "audit_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _archive_previous_attempt(run_dir: Path) -> None:
    report = run_dir / "audit_report.json"
    if not report.exists():
        return
    attempts = run_dir / "attempts"
    attempts.mkdir(parents=True, exist_ok=True)
    index = 1
    while (attempts / f"attempt_{index:03d}").exists():
        index += 1
    target = attempts / f"attempt_{index:03d}"
    target.mkdir()
    for path in run_dir.iterdir():
        if path.is_file():
            shutil.copy2(path, target / path.name)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)
