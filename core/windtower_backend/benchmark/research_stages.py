from __future__ import annotations

import json
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..config import EXAMPLE_ROOT, ROOT, Settings
from ..importers.standard_input import normalize_import_to_standard_input
from ..l0 import build_l0, datacheck_l0, mesh_l0, postprocess_l0, run_l0, validate_l0
from ..input_batches.providers import create_engineering_provider
from ..parameter_ir import (
    ParameterChange,
    ParameterSet,
    apply_changes,
    apply_parameter_set_to_ir,
    instruction_changes,
    parameter_set_from_ir,
)
from ..modeling import ProjectState, build_ir
from ..reproducibility import stable_hash
from .metrics import aggregate_binary_metric, stability_metrics


def run_ir_stage(
    reference_names: list[str],
    *,
    repetitions: int,
    campaign_root: Path,
) -> dict[str, Any]:
    cases = []
    for reference_name in reference_names:
        source = _reference_source(reference_name)
        runs = []
        for repetition in range(1, repetitions + 1):
            started = time.perf_counter()
            try:
                ir = _build_reference_ir(
                    source,
                    project_id=f"ir-{stable_hash(reference_name, length=8)}-{repetition:03d}",
                    modeling_goal="Static and modal S4R shell IR trust experiment.",
                )
                parameter_set = parameter_set_from_ir(
                    f"ir-{stable_hash(reference_name, length=8)}",
                    ir,
                )
                parameter_hash = stable_hash(parameter_set.model_dump(mode="json"))
                runs.append(
                    {
                        "repetition": repetition,
                        "status": "completed",
                        "parameter_hash": parameter_hash,
                        "parameter_revision": parameter_set.revision,
                        "elapsed_seconds": round(time.perf_counter() - started, 6),
                    }
                )
            except Exception as exc:
                runs.append(
                    {
                        "repetition": repetition,
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "elapsed_seconds": round(time.perf_counter() - started, 6),
                    }
                )
        stability = stability_metrics(
            runs,
            hash_fields=("parameter_hash",),
        )
        completed = sum(item["status"] == "completed" for item in runs)
        case_result = {
            "case_id": reference_name,
            "status": (
                "pass"
                if completed == len(runs) and stability["all_hashes_consistent"]
                else "failed"
            ),
            "completion": aggregate_binary_metric(completed, len(runs)),
            "stability": stability,
            "runs": runs,
        }
        cases.append(case_result)
        _write_json(campaign_root / "ir" / f"{stable_hash(reference_name, length=12)}.json", case_result)
    passing = sum(item["status"] == "pass" for item in cases)
    return {
        "schema_version": "parameter_ir_experiment_stage_v1",
        "status": "pass" if passing == len(cases) else "failed",
        "case_count": len(cases),
        "passing_cases": passing,
        "cases": cases,
        "interpretation": (
            "This local stage checks repeated construction of the versioned parameter set."
        ),
    }


def run_modification_stage(
    modification_group: dict[str, Any],
    *,
    campaign_root: Path,
    settings: Settings,
    use_llm: bool,
    repetitions: int,
    compile_models: bool,
) -> dict[str, Any]:
    source = _reference_source(str(modification_group["base_case"]))
    base_ir = _build_reference_ir(
        source,
        project_id="modification-base",
        modeling_goal="Static and modal S4R shell model modification experiment.",
    )
    base_parameters = parameter_set_from_ir("modification-base", base_ir)
    base_snapshot = base_parameters.model_dump(mode="json")
    case_results = []
    for case in modification_group.get("cases") or []:
        expected_changes = [
            ParameterChange.model_validate(item) for item in case.get("changes") or []
        ]
        direct_parameters, direct_diff, direct_validation = apply_changes(
            base_parameters,
            expected_changes,
            increment_revision=False,
        )
        direct_ir = apply_parameter_set_to_ir(direct_parameters, base_ir)
        expected_semantics = _change_semantics(direct_diff)
        direct_compile = (
            _compile_candidate(
                direct_ir,
                source=source,
                root=campaign_root / "modification" / str(case["case_id"]) / "direct",
            )
            if compile_models and direct_validation["status"] == "pass"
            else None
        )
        variant_runs = []
        for variant_name, instruction in (case.get("natural_language") or {}).items():
            variant_repetitions = repetitions if use_llm else 1
            for repetition in range(1, variant_repetitions + 1):
                started = time.perf_counter()
                try:
                    inferred, source_kind, llm_audit = _instruction_changes(
                        base_parameters,
                        str(instruction),
                        settings=settings,
                        use_llm=use_llm,
                    )
                    candidate_parameters, normalized, validation = apply_changes(
                        base_parameters,
                        inferred,
                        increment_revision=False,
                    )
                    candidate_ir = apply_parameter_set_to_ir(
                        candidate_parameters,
                        base_ir,
                    )
                    actual_semantics = _change_semantics(normalized)
                    changes_match = actual_semantics == expected_semantics
                    candidate_compile = (
                        _compile_candidate(
                            candidate_ir,
                            source=source,
                            root=(
                                campaign_root
                                / "modification"
                                / str(case["case_id"])
                                / variant_name
                                / f"run_{repetition:03d}"
                            ),
                        )
                        if (
                            compile_models
                            and changes_match
                            and validation["status"] == "pass"
                        )
                        else None
                    )
                    variant_runs.append(
                        {
                            "variant": variant_name,
                            "repetition": repetition,
                            "status": (
                                "completed"
                                if validation["status"] == "pass"
                                else "blocked"
                            ),
                            "mapping_source": source_kind,
                            "expected_changes": expected_semantics,
                            "actual_changes": actual_semantics,
                            "changes_match": changes_match,
                            "parameter_key_precision": _set_precision(
                                {item["key"] for item in actual_semantics},
                                {item["key"] for item in expected_semantics},
                            ),
                            "parameter_key_recall": _set_recall(
                                {item["key"] for item in actual_semantics},
                                {item["key"] for item in expected_semantics},
                            ),
                            "parameter_equivalent": (
                                candidate_parameters.parameters
                                == direct_parameters.parameters
                            ),
                            "inp_equivalent": (
                                _inp_hash(candidate_compile) == _inp_hash(direct_compile)
                                if candidate_compile and direct_compile
                                else None
                            ),
                            "validation_errors": validation["issues"],
                            "parent_immutable": (
                                base_parameters.model_dump(mode="json")
                                == base_snapshot
                            ),
                            "llm_audit": llm_audit,
                            "elapsed_seconds": round(time.perf_counter() - started, 6),
                        }
                    )
                except Exception as exc:
                    variant_runs.append(
                        {
                            "variant": variant_name,
                            "repetition": repetition,
                            "status": "failed",
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "elapsed_seconds": round(time.perf_counter() - started, 6),
                        }
                    )
        matches = sum(bool(item.get("changes_match")) for item in variant_runs)
        result = {
            "case_id": case["case_id"],
            "group": case["group"],
            "direct_status": direct_validation["status"],
            "direct_errors": direct_validation["issues"],
            "direct_changes": expected_semantics,
            "runs": variant_runs,
            "mapping_completion": aggregate_binary_metric(matches, len(variant_runs)),
        }
        case_results.append(result)
        _write_json(
            campaign_root / "modification" / f"{case['case_id']}.json",
            result,
        )
    all_runs = [run for case in case_results for run in case["runs"]]
    completed = [run for run in all_runs if run.get("status") in {"completed", "blocked"}]
    matches = sum(bool(run.get("changes_match")) for run in completed)
    parent_safe = sum(bool(run.get("parent_immutable")) for run in completed)
    status = (
        "pass"
        if use_llm and completed and matches == len(completed) and parent_safe == len(completed)
        else ("warning" if completed else "failed")
    )
    return {
        "schema_version": "g5_parameter_modification_stage_v1",
        "status": status,
        "mode": "qwen" if use_llm else "rules_only_precheck",
        "compile_models": compile_models,
        "case_count": len(case_results),
        "run_count": len(all_runs),
        "exact_change_match": aggregate_binary_metric(matches, len(completed)),
        "parent_immutability": aggregate_binary_metric(parent_safe, len(completed)),
        "cases": case_results,
        "interpretation": (
            "Rules-only mode is a local precheck and does not substitute for the preregistered "
            "Qwen repetitions. Formal E4 conclusions require use_llm=true."
        ),
    }


def run_abaqus_stage(
    solver_group: dict[str, Any],
    *,
    campaign_root: Path,
    settings: Settings,
) -> dict[str, Any]:
    records = []
    solve_names = set((solver_group.get("solve") or {}).get("cases") or [])
    datacheck = solver_group.get("datacheck") or {}
    repetitions = int(datacheck.get("repetitions") or 1)
    for reference_name in datacheck.get("cases") or []:
        source = _reference_source(str(reference_name))
        for analysis in datacheck.get("analyses") or []:
            for repetition in range(1, repetitions + 1):
                run_root = (
                    campaign_root
                    / "abaqus"
                    / stable_hash(reference_name, length=10)
                    / analysis
                    / f"run_{repetition:03d}"
                )
                started = time.perf_counter()
                try:
                    ir = _build_reference_ir(
                        source,
                        project_id=f"abaqus-{stable_hash(reference_name, length=8)}-{analysis}-{repetition}",
                        modeling_goal=f"{analysis} S4R shell solver reliability experiment.",
                    )
                    state = ProjectState(
                        project_id=f"abaqus-{stable_hash(reference_name, length=8)}-{analysis}-{repetition}",
                        root=run_root,
                        excel_path=source,
                        ir=ir,
                        artifacts={},
                    )
                    validation = validate_l0(state)
                    mesh = mesh_l0(state)
                    build = build_l0(state)
                    check = datacheck_l0(state, settings, job_type=analysis)
                    solve = None
                    postprocess = None
                    if (
                        reference_name in solve_names
                        and check.get("status") in {"pass", "warning"}
                    ):
                        solve = run_l0(state, settings, job_type=analysis)
                        if solve.get("status") in {"pass", "warning"}:
                            postprocess = postprocess_l0(
                                state,
                                settings,
                                expected_job_types={analysis},
                            )
                    records.append(
                        {
                            "case_id": reference_name,
                            "analysis": analysis,
                            "repetition": repetition,
                            "status": (
                                "completed"
                                if check.get("status") in {"pass", "warning"}
                                else "failed"
                            ),
                            "validation_status": validation.get("status"),
                            "mesh_status": mesh.get("status"),
                            "build_status": build.get("status"),
                            "datacheck_status": check.get("status"),
                            "solve_status": (solve or {}).get("status"),
                            "postprocess_status": (postprocess or {}).get("status"),
                            "failure_classification": _failure_categories(check, solve),
                            "elapsed_seconds": round(time.perf_counter() - started, 6),
                        }
                    )
                except Exception as exc:
                    records.append(
                        {
                            "case_id": reference_name,
                            "analysis": analysis,
                            "repetition": repetition,
                            "status": "failed",
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "elapsed_seconds": round(time.perf_counter() - started, 6),
                        }
                    )
    checks = [item for item in records if item.get("datacheck_status")]
    solved = [item for item in records if item.get("solve_status")]
    return {
        "schema_version": "abaqus_reliability_stage_v1",
        "status": (
            "pass"
            if checks and all(item["datacheck_status"] == "pass" for item in checks)
            else "warning"
        ),
        "records": records,
        "datacheck_pass_rate": aggregate_binary_metric(
            sum(item.get("datacheck_status") == "pass" for item in checks),
            len(checks),
        ),
        "solve_completion_rate": aggregate_binary_metric(
            sum(
                item.get("solve_status") in {"pass", "warning"}
                for item in solved
            ),
            len(solved),
        ),
        "acceptance": deepcopy(solver_group.get("acceptance") or {}),
        "engineering_disclaimer": (
            "Solver completion and numerical sanity do not constitute structural design approval."
        ),
    }


def _build_reference_ir(source: Path, *, project_id: str, modeling_goal: str) -> dict[str, Any]:
    imported = normalize_import_to_standard_input(source)
    return build_ir(
        project_id=project_id,
        source_name=source.name,
        tower=imported["tower"],
        flanges=imported["flanges"],
        door=imported["door"],
        fem_params=imported["fem_params"],
        import_meta=imported,
        modeling_goal=modeling_goal,
    )


def _reference_source(reference_name: str) -> Path:
    internal = ROOT / "core" / "example" / reference_name
    if internal.is_file():
        return internal
    return EXAMPLE_ROOT / reference_name


def _instruction_changes(
    parameter_set: ParameterSet,
    instruction: str,
    *,
    settings: Settings,
    use_llm: bool,
) -> tuple[list[ParameterChange], str, dict[str, Any] | None]:
    if not use_llm:
        return (
            instruction_changes(instruction, parameter_set),
            "deterministic_instruction_rules",
            None,
        )
    provider = create_engineering_provider(settings)
    response = provider.map_parameters(
        [{
            "evidence_id": "g5-instruction",
            "filename": "natural_language_instruction.txt",
            "location": {"paragraph": 1},
            "text": instruction,
        }],
        instruction,
        target_keys=list(parameter_set.parameters),
    )
    changes = [
        ParameterChange(
            key=str(item.get("path") or item.get("key")),
            value=item.get("normalized_value", item.get("value")),
            unit=item.get("normalized_unit", item.get("unit")),
        )
        for item in response.get("fields") or []
        if str(item.get("path") or item.get("key")) in parameter_set.parameters
    ]
    return changes, str(response.get("provider") or "llm"), {
        "provider": response.get("provider"),
        "model": response.get("model"),
        "status": response.get("status"),
        "candidate_only": True,
    }


def _change_semantics(normalized: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        [
            {
                "key": str(item["key"]),
                "value": item.get("new_value"),
                "unit": item.get("unit"),
            }
            for item in normalized
        ],
        key=lambda item: item["key"],
    )


def _compile_candidate(
    ir: dict[str, Any],
    *,
    source: Path,
    root: Path,
) -> dict[str, Any]:
    state = ProjectState(
        project_id=f"mod-{stable_hash(str(root), length=10)}",
        root=root,
        excel_path=source,
        ir=deepcopy(ir),
        artifacts={},
    )
    validation = validate_l0(state)
    if validation.get("status") == "blocked":
        return {"status": "blocked", "validation": validation}
    mesh = mesh_l0(state)
    build = build_l0(state)
    return {
        "status": build.get("status"),
        "mesh": mesh.get("status"),
        "build_manifest": deepcopy(state.ir.get("build_manifest") or {}),
    }


def _inp_hash(compilation: dict[str, Any] | None) -> str | None:
    if not compilation:
        return None
    artifacts = (compilation.get("build_manifest") or {}).get("inp_artifacts") or {}
    if not artifacts:
        return None
    return stable_hash(
        {key: value.get("sha256") for key, value in artifacts.items()},
        length=32,
    )


def _set_precision(actual: set[str], expected: set[str]) -> float:
    return len(actual & expected) / len(actual) if actual else 0.0


def _set_recall(actual: set[str], expected: set[str]) -> float:
    return len(actual & expected) / len(expected) if expected else 1.0


def _failure_categories(
    datacheck: dict[str, Any],
    solve: dict[str, Any] | None,
) -> list[str]:
    return sorted(
        {
            str(item.get("category"))
            for payload in (datacheck, solve or {})
            for item in payload.get("categories") or []
            if item.get("category")
        }
    )


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)
