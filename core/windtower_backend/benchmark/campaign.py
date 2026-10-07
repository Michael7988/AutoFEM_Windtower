from __future__ import annotations

import csv
import json
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..config import EXAMPLE_ROOT, Settings, load_settings
from ..importers.standard_input import normalize_import_to_standard_input
from ..l0 import mesh_l0, validate_l0
from ..modeling import ProjectState, build_ir, get_state
from ..parameter_ir import parameter_set_from_ir
from ..reproducibility import (
    append_paper_traceability,
    build_environment_manifest,
    file_sha256,
    stable_hash,
)
from .dataset import DEFAULT_BENCHMARK_ROOT, DISCLAIMER
from .experiment_dataset import generate_experiment_datasets
from .fault_evaluator import evaluate_fault_manifest
from .metrics import (
    aggregate_binary_metric,
    compare_parameter_payloads,
    stability_metrics,
)
from .protocols import experiment_protocols
from .research_stages import run_abaqus_stage, run_modification_stage
from .runner import run_llm_benchmark


class ExperimentCampaignConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["wind_tower_experiment_campaign_config_v1"] = (
        "wind_tower_experiment_campaign_config_v1"
    )
    campaign_name: str = "wind_tower_llm_assisted_fem_v1"
    case_ids: list[str] = Field(default_factory=lambda: [f"S{index:02d}" for index in range(1, 11)])
    stages: list[
        Literal["G1", "G2", "G3", "G4", "G5", "G6"]
    ] = Field(default_factory=lambda: ["G1", "G2", "G3", "G4", "G5"])
    deterministic_repetitions: int = Field(default=10, ge=1, le=100)
    ir_repetitions: int = Field(default=10, ge=1, le=100)
    compiler_repetitions: int = Field(default=3, ge=1, le=20)
    global_mesh_factors: list[float] = Field(
        default_factory=lambda: [1.5, 1.0, 0.75]
    )
    door_mesh_factors: list[float] = Field(
        default_factory=lambda: [1.5, 1.0, 0.75]
    )
    llm_repetitions: int = Field(default=5, ge=1, le=20)
    modification_repetitions: int = Field(default=3, ge=1, le=20)
    run_llm: bool = False
    run_abaqus: bool = False
    compile_modifications: bool = False
    allow_direct_inp_ablation: bool = False
    include_internal_engineering: bool = False
    relative_tolerance: float = Field(default=1e-3, gt=0)
    engineering_disclaimer: str = (
        "The campaign validates automation and numerical workflow behavior; "
        "it is not structural design approval."
    )


def run_experiment_campaign(
    config: ExperimentCampaignConfig | dict[str, Any] | None = None,
    *,
    root: Path | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Execute an auditable campaign.

    The default campaign is local-only. Real model and Abaqus calls require the
    explicit ``llm`` or ``abaqus`` stages, which prevents accidental cost or
    solver execution in normal regression tests.
    """

    config = (
        config
        if isinstance(config, ExperimentCampaignConfig)
        else ExperimentCampaignConfig.model_validate(config or {})
    )
    settings = settings or load_settings()
    root = Path(root or DEFAULT_BENCHMARK_ROOT)
    campaign_id = f"exp-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    campaign_root = root / "campaigns" / campaign_id
    campaign_root.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    dataset_manifest = _load_or_generate_dataset_manifest(root, config)
    benchmark_manifest = _read_json(root / "benchmark_manifest.json")
    cases = {
        str(item["case_id"]).upper(): _resolve_benchmark_case(root, item)
        for item in benchmark_manifest.get("cases") or []
    }
    report: dict[str, Any] = {
        "schema_version": "wind_tower_experiment_campaign_report_v1",
        "campaign_id": campaign_id,
        "campaign_name": config.campaign_name,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "declaration": DISCLAIMER,
        "engineering_disclaimer": config.engineering_disclaimer,
        "config": config.model_dump(),
        "environment": _environment(settings),
        "experiment_protocols": experiment_protocols(),
        "dataset_manifest": dataset_manifest["manifest"],
        "dataset_manifest_sha256": dataset_manifest["manifest_sha256"],
        "stage_results": {},
        "summary": {},
    }
    _write_json(campaign_root / "campaign_config.json", config.model_dump())
    _write_json(campaign_root / "campaign_report.json", report)

    groups = dataset_manifest.get("groups") or {}
    if "G1" in config.stages:
        report["stage_results"]["G1"] = {
            "schema_version": "g1_reference_cases_v1",
            "status": "pass",
            "case_count": len((groups.get("G1") or {}).get("cases") or []),
            "cases": (groups.get("G1") or {}).get("cases") or [],
            "manifest": dataset_manifest["manifest"],
            "manifest_sha256": dataset_manifest["manifest_sha256"],
        }

    if "G2" in config.stages:
        report["stage_results"]["G2"] = (
            _run_llm_stage(cases, config, root, settings, campaign_id)
            if config.run_llm
            else _run_deterministic_stage(cases, config, campaign_root)
        )

    if "G3" in config.stages:
        g3 = groups.get("G3") or {}
        artifacts = [
            artifact
            for case in g3.get("cases") or []
            for artifact in case.get("artifacts") or []
        ]
        report["stage_results"]["G3"] = {
            "schema_version": "g3_cross_format_v1",
            "status": "pass" if artifacts and all(Path(item["path"]).is_file() for item in artifacts) else "failed",
            "case_count": len(g3.get("cases") or []),
            "artifact_count": len(artifacts),
            "formats": g3.get("formats") or [],
        }

    if "G4" in config.stages:
        report["stage_results"]["G4"] = evaluate_fault_manifest(
            (groups.get("G4") or {}).get("cases") or []
        )

    if "G5" in config.stages:
        report["stage_results"]["G5"] = run_modification_stage(
            groups.get("G5") or {},
            campaign_root=campaign_root,
            settings=settings,
            use_llm=config.run_llm,
            repetitions=config.modification_repetitions,
            compile_models=config.compile_modifications,
        )

    if "G6" in config.stages:
        mesh_result = _run_mesh_stage(config, campaign_root)
        abaqus_result = None
        if config.run_abaqus:
            abaqus_result = run_abaqus_stage(
                groups.get("G6") or {},
                campaign_root=campaign_root,
                settings=settings,
            )
        report["stage_results"]["G6"] = {
            "schema_version": "g6_solver_validation_v1",
            "status": (
                abaqus_result.get("status")
                if abaqus_result is not None
                else mesh_result.get("status")
            ),
            "mesh_sensitivity": mesh_result,
            "abaqus": abaqus_result or {"status": "skipped", "explicit_enable_required": True},
        }

    report["summary"] = _campaign_summary(report["stage_results"])
    report["status"] = (
        "pass"
        if all(
            item.get("status") in {"pass", "ready"}
            for item in report["stage_results"].values()
        )
        else "warning"
    )
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    report["elapsed_seconds"] = round(time.perf_counter() - started, 6)
    _write_json(campaign_root / "campaign_report.json", report)
    _write_tabular_results(campaign_root, report)
    _write_markdown_report(campaign_root, report)
    traceability_path = append_paper_traceability(
        root,
        campaign_id=campaign_id,
        environment=report["environment"],
        dataset_manifest_sha256=report["dataset_manifest_sha256"],
        result_root=campaign_root,
    )
    report["artifacts"] = {
        "root": str(campaign_root),
        "json": str(campaign_root / "campaign_report.json"),
        "jsonl": str(campaign_root / "run_results.jsonl"),
        "csv": str(campaign_root / "run_results.csv"),
        "markdown": str(campaign_root / "campaign_report.md"),
        "traceability": str(traceability_path),
    }
    _write_json(campaign_root / "campaign_report.json", report)
    return report


def _run_deterministic_stage(
    cases: dict[str, dict[str, Any]],
    config: ExperimentCampaignConfig,
    campaign_root: Path,
) -> dict[str, Any]:
    case_results = []
    for case_id in config.case_ids:
        case = cases.get(case_id.upper())
        if not case:
            case_results.append(
                {"case_id": case_id, "status": "failed", "error": "case_not_found"}
            )
            continue
        truth = _read_json(Path(case["ground_truth"]))
        runs = []
        for repetition in range(1, config.deterministic_repetitions + 1):
            started = time.perf_counter()
            try:
                normalized = normalize_import_to_standard_input(
                    Path(case["input"]),
                    enable_llm=False,
                    force_llm=False,
                )
                package = (
                    normalized.get("standard_input_package")
                    or normalized.get("standard_input_candidate")
                    or {}
                )
                comparison = (
                    compare_parameter_payloads(
                        package,
                        truth,
                        relative_tolerance=config.relative_tolerance,
                    )
                    if package
                    else None
                )
                runs.append(
                    {
                        "case_id": case_id,
                        "repetition": repetition,
                        "status": "completed",
                        "blocked": bool(
                            (normalized.get("validation_report") or {}).get("blocked")
                        ),
                        "input_format": normalized.get("input_format"),
                        "parameter_hash": stable_hash(package, length=32) if package else None,
                        "validation_hash": stable_hash(
                            normalized.get("validation_report") or {},
                            length=32,
                        ),
                        "field_accuracy": (
                            comparison.get("exact_field_accuracy")
                            if comparison
                            else None
                        ),
                        "elapsed_seconds": round(time.perf_counter() - started, 6),
                    }
                )
            except Exception as exc:
                runs.append(
                    {
                        "case_id": case_id,
                        "repetition": repetition,
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "elapsed_seconds": round(time.perf_counter() - started, 6),
                    }
                )
        stability = stability_metrics(
            runs,
            hash_fields=("parameter_hash", "validation_hash"),
        )
        successful = sum(item["status"] == "completed" for item in runs)
        result = {
            "case_id": case_id,
            "status": (
                "pass"
                if successful == len(runs) and stability["all_hashes_consistent"]
                else "failed"
            ),
            "runs": runs,
            "stability": stability,
            "completion": aggregate_binary_metric(successful, len(runs)),
        }
        case_results.append(result)
        _write_json(
            campaign_root / "deterministic" / f"{case_id}.json",
            result,
        )
    passing = sum(item.get("status") == "pass" for item in case_results)
    return {
        "schema_version": "deterministic_experiment_stage_v1",
        "status": "pass" if passing == len(case_results) else "failed",
        "case_count": len(case_results),
        "passing_cases": passing,
        "cases": case_results,
    }


def _run_llm_stage(
    cases: dict[str, dict[str, Any]],
    config: ExperimentCampaignConfig,
    root: Path,
    settings: Settings,
    campaign_id: str,
) -> dict[str, Any]:
    results = []
    for case_id in config.case_ids:
        if case_id.upper() not in cases:
            results.append({"case_id": case_id, "status": "failed", "error": "case_not_found"})
            continue
        for repetition in range(1, config.llm_repetitions + 1):
            attempt_id = f"{campaign_id}-{repetition:03d}"
            result = run_llm_benchmark(
                case_id=case_id,
                llm_mode="forced_parse",
                analyses=("static_strength", "modal"),
                run_abaqus=config.run_abaqus,
                root=root,
                settings=settings,
                attempt_id=attempt_id,
            )
            parameter_hash = None
            parameter_revision = None
            if result.get("project_id"):
                try:
                    state = get_state(str(result["project_id"]))
                    parameter_set = parameter_set_from_ir(
                        state.project_id,
                        state.ir,
                    )
                    parameter_hash = stable_hash(
                        parameter_set.model_dump(mode="json"),
                        length=32,
                    )
                    parameter_revision = parameter_set.revision
                except Exception:
                    # The benchmark result still retains its explicit failure
                    # classification if project state cannot be inspected.
                    pass
            results.append(
                {
                    "case_id": case_id,
                    "repetition": repetition,
                    "status": result.get("status"),
                    "failure_classification": result.get("failure_classification"),
                    "field_accuracy": result.get("field_accuracy"),
                    "project_id": result.get("project_id"),
                    "total_tokens": (
                        (result.get("stages") or {}).get("llm_call") or {}
                    ).get("total_tokens", 0),
                    "parameter_hash": parameter_hash,
                    "parameter_revision": parameter_revision,
                }
            )
    successful = sum(item.get("status") == "pass" for item in results)
    accuracies = [
        float(item["field_accuracy"])
        for item in results
        if isinstance(item.get("field_accuracy"), (int, float))
    ]
    case_summaries = []
    for case_id in config.case_ids:
        case_runs = [item for item in results if item.get("case_id") == case_id]
        case_summaries.append(
            {
                "case_id": case_id,
                "run_count": len(case_runs),
                "stability": stability_metrics(case_runs),
                "completion": aggregate_binary_metric(
                    sum(item.get("status") == "pass" for item in case_runs),
                    len(case_runs),
                ),
            }
        )
    return {
        "schema_version": "llm_experiment_stage_v1",
        "status": "pass" if successful == len(results) else "warning",
        "run_abaqus": config.run_abaqus,
        "run_count": len(results),
        "completion": aggregate_binary_metric(successful, len(results)),
        "mean_field_accuracy": mean(accuracies) if accuracies else None,
        "cases": case_summaries,
        "runs": results,
    }


def _run_mesh_stage(
    config: ExperimentCampaignConfig,
    campaign_root: Path,
) -> dict[str, Any]:
    source = EXAMPLE_ROOT / "synthetic_standard_tower.xlsx"
    imported = normalize_import_to_standard_input(source)
    base_global = float(imported["fem_params"]["mesh.global_size_mm"])
    base_door = float(imported["fem_params"]["mesh.door_size_mm"])
    runs = []
    for global_factor in config.global_mesh_factors:
        for door_factor in config.door_mesh_factors:
            case_key = f"mesh-g{global_factor:g}-d{door_factor:g}".replace(".", "p")
            run_root = campaign_root / "mesh" / case_key
            run_root.mkdir(parents=True, exist_ok=False)
            started = time.perf_counter()
            candidate = deepcopy(imported)
            candidate["fem_params"] = deepcopy(imported["fem_params"])
            candidate["fem_params"]["mesh.global_size_mm"] = (
                base_global * float(global_factor)
            )
            candidate["fem_params"]["mesh.door_size_mm"] = (
                base_door * float(door_factor)
            )
            try:
                ir = build_ir(
                    project_id=case_key,
                    source_name=source.name,
                    tower=candidate["tower"],
                    flanges=candidate["flanges"],
                    door=candidate["door"],
                    fem_params=candidate["fem_params"],
                    import_meta=candidate,
                    modeling_goal="S4R shell mesh sensitivity experiment.",
                )
                state = ProjectState(
                    project_id=case_key,
                    root=run_root,
                    excel_path=source,
                    ir=ir,
                    artifacts={},
                )
                validation = validate_l0(state)
                mesh = mesh_l0(state)
                quality = mesh.get("quality_report") or {}
                stats = mesh.get("mesh_stats") or {}
                runs.append(
                    {
                        "case_id": case_key,
                        "status": (
                            "completed"
                            if validation.get("status") != "blocked"
                            and mesh.get("status") != "blocked"
                            else "failed"
                        ),
                        "global_factor": float(global_factor),
                        "door_factor": float(door_factor),
                        "global_size_mm": candidate["fem_params"]["mesh.global_size_mm"],
                        "door_size_mm": candidate["fem_params"]["mesh.door_size_mm"],
                        "node_count": stats.get("nodes"),
                        "element_count": stats.get("s4r_elements"),
                        "door_neighbor_elements": stats.get("door_neighbor_elements"),
                        "quality_blocked": bool(quality.get("blocked")),
                        "quality_error_count": len(quality.get("errors") or []),
                        "quality_warning_count": len(quality.get("warnings") or []),
                        "quality_errors": deepcopy(quality.get("errors") or []),
                        "quality_warnings": deepcopy(quality.get("warnings") or []),
                        "quality_metrics": deepcopy(quality.get("metrics") or {}),
                        "elapsed_seconds": round(time.perf_counter() - started, 6),
                    }
                )
            except Exception as exc:
                runs.append(
                    {
                        "case_id": case_key,
                        "status": "failed",
                        "global_factor": float(global_factor),
                        "door_factor": float(door_factor),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "elapsed_seconds": round(time.perf_counter() - started, 6),
                    }
                )
    successful = sum(item.get("status") == "completed" for item in runs)
    return {
        "schema_version": "mesh_sensitivity_experiment_stage_v1",
        "status": (
            "pass"
            if successful == len(runs)
            else ("warning" if successful else "failed")
        ),
        "run_count": len(runs),
        "completion": aggregate_binary_metric(successful, len(runs)),
        "runs": runs,
        "interpretation": (
            "This stage evaluates mesh generation and quality trends only. "
            "Displacement, stress and modal convergence require the Abaqus stage."
        ),
    }


def _campaign_summary(stages: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage_count": len(stages),
        "passing_stages": sum(
            item.get("status") == "pass" for item in stages.values()
        ),
        "warning_or_failed_stages": [
            name for name, item in stages.items() if item.get("status") != "pass"
        ],
        "claims": {
            "deterministic_stability_evaluated": "G2" in stages,
            "real_llm_evaluated": bool(
                (stages.get("G2") or {}).get("real_llm_call_count")
            ),
            "abaqus_evaluated": bool(
                ((stages.get("G6") or {}).get("abaqus") or {}).get("status")
                not in {None, "skipped"}
            ),
        },
    }


def _environment(settings: Settings) -> dict[str, Any]:
    return build_environment_manifest(settings)


def _write_tabular_results(root: Path, report: dict[str, Any]) -> None:
    rows = []
    for case in (report.get("stage_results", {}).get("G2") or {}).get("cases") or []:
        rows.extend({"stage": "G2", **run} for run in case.get("runs") or [])
    rows.extend(
        {"stage": "G2", **run}
        for run in (report.get("stage_results", {}).get("G2") or {}).get("runs") or []
    )
    rows.extend(
        {
            "stage": "G4",
            "case_id": item.get("fault_id"),
            "status": item.get("status"),
            "observed_behavior": item.get("observed_behavior"),
            "expected_behavior": item.get("expected_behavior"),
        }
        for item in (report.get("stage_results", {}).get("G4") or {}).get("cases") or []
    )
    for case in (report.get("stage_results", {}).get("G5") or {}).get("cases") or []:
        rows.extend(
            {
                "stage": "G5",
                "case_id": case.get("case_id"),
                "group": case.get("group"),
                **run,
            }
            for run in case.get("runs") or []
        )
    rows.extend(
        {"stage": "G6", **item}
        for item in (
            ((report.get("stage_results", {}).get("G6") or {}).get("abaqus") or {}).get("records")
            or []
        )
    )
    with (root / "run_results.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
                + "\n"
            )
    fields = sorted({key for row in rows for key in row})
    with (root / "run_results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _write_markdown_report(root: Path, report: dict[str, Any]) -> None:
    lines = [
        f"# Wind-tower experiment campaign {report['campaign_id']}",
        "",
        f"> {report['engineering_disclaimer']}",
        "",
        f"- Status: `{report['status']}`",
        f"- Dataset manifest: `{report['dataset_manifest']}`",
        f"- Elapsed: `{report.get('elapsed_seconds', 0)} s`",
        "",
        "## Stage summary",
        "",
        "| Stage | Status | Runs/cases |",
        "|---|---:|---:|",
    ]
    for name, stage in report.get("stage_results", {}).items():
        count = stage.get("run_count", stage.get("case_count", 0))
        lines.append(f"| {name} | {stage.get('status')} | {count} |")
    lines.extend(
        [
            "",
            "## Interpretation boundary",
            "",
            "A passing campaign demonstrates the configured automation checks only. "
            "It does not certify structural safety or compliance with a design code.",
        ]
    )
    (root / "campaign_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_benchmark_case(root: Path, item: dict[str, Any]) -> dict[str, Any]:
    resolved = dict(item)
    for key in ("input", "ground_truth"):
        path = Path(str(item[key]))
        if not path.is_absolute():
            path = root / path
        resolved[key] = str(path.resolve())
    return resolved


def _load_or_generate_dataset_manifest(
    root: Path,
    config: ExperimentCampaignConfig,
) -> dict[str, Any]:
    manifest_path = (
        root / "experiment_datasets" / "experiment_dataset_manifest.json"
    )
    if not manifest_path.is_file():
        return generate_experiment_datasets(
            root,
            case_ids=tuple(case_id.upper() for case_id in config.case_ids),
            include_internal_engineering=config.include_internal_engineering,
        )
    payload = _read_json(manifest_path)
    available = {
        str(item.get("case_id") or "").upper()
        for item in ((payload.get("groups") or {}).get("G2") or {}).get("cases") or []
    }
    missing = sorted(set(config.case_ids) - available)
    if missing:
        raise ValueError(
            f"Requested cases are absent from the frozen dataset manifest: {missing}"
        )
    payload["manifest"] = str(manifest_path)
    payload["manifest_sha256"] = file_sha256(manifest_path)
    return payload


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)
