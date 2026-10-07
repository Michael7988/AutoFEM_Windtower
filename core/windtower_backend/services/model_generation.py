from __future__ import annotations

import json
import mimetypes
from pathlib import Path
import shutil
from typing import Any

from ..capability_profile import L2_CAPABILITY_ID
from ..config import Settings
from ..fe_semantic import build_fe_semantic_ir, dependency_report
from ..l0 import build_l0, mesh_l0, mesh_pyvista_preview_l0, validate_l0
from ..l2 import build_l2, mesh_l2, mesh_preview_l2
from ..modeling import get_state
from ..modeling.repository import ArtifactRepository, artifact_category
from ..parameter_ir import ParameterSet, apply_parameter_set_to_ir, validate_parameter_set
from .parameters import ParameterService
from .semantic_commands import SemanticCommandRequest, repair_command_v1, semantic_command_v1


class ModelGenerationService:
    def __init__(self, settings: Settings, parameters: ParameterService) -> None:
        self.settings = settings
        self.parameters = parameters
        self.artifact_repository = ArtifactRepository()

    def validate(self, project_id: str) -> dict[str, Any]:
        state, parameter_set = self._prepared_state(project_id)
        parameter_validation = validate_parameter_set(parameter_set)
        if parameter_validation["status"] != "pass":
            return parameter_validation
        if parameter_set.capability_id == L2_CAPABILITY_ID:
            semantic_ir = build_fe_semantic_ir(parameter_set)
            semantic_gate = dependency_report(semantic_ir)
            return {
                "schema_version": "three_layer_validation_v1",
                "parameter": parameter_validation,
                "model": semantic_gate,
                "numerical": {"status": "not_run", "issues": []},
            }
        return {
            "schema_version": "three_layer_validation_v1",
            "parameter": parameter_validation,
            "model": validate_l0(state),
            "numerical": {"status": "not_run", "issues": []},
        }

    def validation_summary(self, project_id: str) -> dict[str, Any]:
        """Return UI-facing three-layer states without treating placeholders as passes."""
        state = get_state(project_id)
        parameter_payload = self.parameters.get(project_id)
        parameter_set = ParameterSet.model_validate(parameter_payload["parameter_set"])
        parameter_validation = parameter_payload["validation"]

        unconfirmed = [
            key
            for key, record in parameter_set.parameters.items()
            if record.confirmation_status == "pending"
            or (record.requires_confirmation and record.confirmation_status != "confirmed")
        ]
        parameter_checks = [
            _summary_check(
                "schema",
                "Schema and parameter legality",
                "pass" if parameter_validation.get("status") == "pass" else "fail",
                len(parameter_validation.get("issues") or []),
            ),
            _summary_check(
                "confirmation",
                "Active parameters confirmed",
                "pass" if not unconfirmed else "fail",
                len(unconfirmed),
            ),
        ]

        mesh_report = _read_json(state.root / "mesh" / "mesh_quality_report.json")
        quality = mesh_report.get("quality_report") or {}
        metrics = quality.get("metrics") or {}
        model_checks = [
            _metric_zero_check("duplicate-nodes", "No duplicate nodes", metrics, "duplicate_node_count"),
            _metric_zero_check("degenerate-elements", "No degenerate elements", metrics, "degenerate_element_count"),
            _bounded_metric_check(
                "aspect-ratio",
                "Maximum aspect ratio",
                metrics,
                ("max_aspect_ratio", "aspect_ratio_max"),
                "aspect_ratio_threshold",
            ),
        ]
        for check_id, label, keys in (
            ("skew", "Element skew", ("max_skew_angle_deg", "skew_angle_max_deg")),
            ("warpage", "Element warpage", ("max_warpage_deg", "warpage_max_deg")),
            ("scaled-jacobian", "Minimum scaled Jacobian", ("min_scaled_jacobian",)),
            ("normal-consistency", "Shell normal consistency", ("normal_inconsistency_count",)),
            ("non-manifold", "No non-manifold edges", ("non_manifold_edge_count",)),
            ("unexpected-free-edge", "No unexpected free edges", ("unexpected_free_edge_count",)),
        ):
            value = _first_numeric(metrics, keys)
            model_checks.append(
                _summary_check(check_id, label, "not_run" if value is None else "pass", value)
            )

        build_report = _read_json(state.root / "inp" / "l0_build_report.json")
        solver_jobs = []
        for job_type in ("static_strength", "modal"):
            for mode in ("datacheck", "solve"):
                payload = _read_json(state.root / "jobs" / job_type / "job_status.json")
                if payload and (payload.get("mode") == mode or mode == "solve"):
                    solver_jobs.append(payload)
                    break
        result_report = _read_json(state.root / "results" / "output_availability_report.json")
        numerical_checks = [
            _summary_check(
                "inp-build",
                "Deterministic INP generated",
                "pass" if build_report.get("status") == "ready" else ("fail" if build_report else "not_run"),
                build_report.get("build_id"),
            ),
            _solver_check(solver_jobs),
            _summary_check(
                "odb-output",
                "ODB and required result fields available",
                _result_status(result_report),
                result_report.get("status"),
            ),
        ]

        groups = [
            _summary_group("parameter", "Parameter and evidence", parameter_checks),
            _summary_group("model", "Model and mesh", model_checks),
            _summary_group("numerical", "Solver and results", numerical_checks),
        ]
        totals = {name: 0 for name in ("passed", "warnings", "failed", "not_run")}
        bucket = {"pass": "passed", "warning": "warnings", "fail": "failed", "not_run": "not_run"}
        for group in groups:
            for check in group["checks"]:
                totals[bucket[check["status"]]] += 1
        status = "fail" if totals["failed"] else ("warning" if totals["warnings"] else ("not_run" if totals["not_run"] else "pass"))
        return {
            "schema_version": "validation_summary_v1",
            "project_id": project_id,
            "parameter_revision": parameter_set.revision,
            "status": status,
            "groups": groups,
            "totals": totals,
        }

    def mesh(self, project_id: str) -> dict[str, Any]:
        state, parameter_set = self._prepared_state(project_id)
        validation = validate_parameter_set(parameter_set)
        if validation["status"] != "pass":
            return validation
        report = (
            mesh_l2(state, parameter_set)
            if parameter_set.capability_id == L2_CAPABILITY_ID
            else mesh_l0(state)
        )
        self._persist_artifacts(state)
        return report

    def mesh_preview(self, project_id: str) -> dict[str, Any]:
        state, parameter_set = self._prepared_state(project_id)
        validation = validate_parameter_set(parameter_set)
        if validation["status"] != "pass":
            return validation
        report = (
            mesh_preview_l2(state, parameter_set)
            if parameter_set.capability_id == L2_CAPABILITY_ID
            else mesh_pyvista_preview_l0(state)
        )
        self._persist_artifacts(state)
        return report

    def build(self, project_id: str) -> dict[str, Any]:
        state, parameter_set = self._prepared_state(project_id)
        validation = validate_parameter_set(parameter_set)
        if validation["status"] != "pass":
            return validation
        mesh_report = _read_json(state.root / "mesh" / "mesh_quality_report.json")
        mesh_stats = mesh_report.get("mesh_stats") or {}
        mesh_status = (mesh_report.get("quality_report") or {}).get("status")
        l2_bolts_missing = (
            parameter_set.capability_id == L2_CAPABILITY_ID
            and int(mesh_stats.get("b31_elements") or 0) <= 0
        )
        if mesh_status not in {"pass", "warning"} or int(mesh_stats.get("nodes") or 0) <= 0 or int(mesh_stats.get("s4r_elements") or 0) <= 0 or l2_bolts_missing:
            return {
                "schema_version": "model_gate_v1",
                "layer": "model",
                "status": "blocked",
                "issues": [{
                    "layer": "model",
                    "code": "MESH_NOT_READY",
                    "parameter_key": None,
                    "message": "网格尚未通过门禁，请先生成并检查 S4R 网格。",
                    "resolution_action": "generate_mesh",
                }],
            }
        report = (
            build_l2(state, parameter_set)
            if parameter_set.capability_id == L2_CAPABILITY_ID
            else build_l0(state)
        )
        self._persist_artifacts(state)
        return report

    def workflow_state(self, project_id: str) -> dict[str, Any]:
        state = get_state(project_id)
        parameter_payload = self.parameters.get(project_id)
        parameter_set = ParameterSet.model_validate(parameter_payload["parameter_set"])
        parameter_validation = parameter_payload["validation"]
        parameter_ok = parameter_validation.get("status") == "pass"
        semantic_gate = (
            dependency_report(build_fe_semantic_ir(parameter_set))
            if parameter_set.capability_id == L2_CAPABILITY_ID
            else {"status": "pass", "issues": []}
        )
        semantic_ok = semantic_gate.get("status") == "pass"

        mesh_report = _read_json(state.root / "mesh" / "mesh_quality_report.json")
        mesh_stats = mesh_report.get("mesh_stats") or {}
        mesh_ok = (
            parameter_ok and semantic_ok
            and (mesh_report.get("quality_report") or {}).get("status") in {"pass", "warning"}
            and int(mesh_stats.get("nodes") or 0) > 0
            and int(mesh_stats.get("s4r_elements") or 0) > 0
        )
        build_report = _read_json(state.root / "inp" / "l0_build_report.json")
        build_ok = mesh_ok and build_report.get("status") == "ready"

        task_names = [
            name for name, enabled in (
                ("static_strength", "static" in parameter_set.analysis_tasks),
                ("modal", "modal" in parameter_set.analysis_tasks),
            ) if enabled
        ]
        job_states = [
            _read_json(state.root / "jobs" / name / "job_status.json")
            for name in task_names
        ]
        datacheck_ok = bool(job_states) and all(
            item.get("mode") == "datacheck" and item.get("status") in {"pass", "warning"}
            for item in job_states
        )
        advisories = [
            {
                "layer": "model",
                "code": str(finding.get("category") or "ABAQUS_WARNING").upper(),
                "parameter_key": None,
                "message": str(finding.get("message") or "Abaqus 检查产生警告。"),
                "resolution_action": "review_mesh_quality",
            }
            for item in job_states
            for finding in (item.get("diagnostic_report") or {}).get("findings") or []
            if finding.get("severity") == "warning"
        ]
        result_report = _read_json(state.root / "results" / "output_availability_report.json")
        results_ok = result_report.get("status") in {"pass", "warning"}

        abaqus_command = str(self.settings.abaqus_cmd or "").strip().strip('"')
        abaqus_available = bool(
            abaqus_command
            and (Path(abaqus_command).is_file() or shutil.which(abaqus_command))
        )
        environment = {
            "native_l0_generator": {"status": "ready", "required": True},
            "native_l2_generator": {"status": "ready", "required": True},
            "abaqus": {
                "status": "ready" if abaqus_available else "blocked",
                "required_for": ["datacheck", "solve"],
                "command": self.settings.abaqus_cmd,
                "message": None if abaqus_available else "未找到 Abaqus 命令；模型门禁不受影响，但 Datacheck 与求解无法执行。",
            },
            "legacy_external_helpers": {"status": "optional", "required": False},
        }

        blockers: list[dict[str, Any]] = []
        if not parameter_ok:
            blockers.extend(parameter_validation.get("issues") or [])
        elif not semantic_ok:
            blockers.extend(semantic_gate.get("issues") or [])
        elif not mesh_ok and mesh_report.get("status") == "blocked":
            blockers.extend(_structured_issues(
                (mesh_report.get("quality_report") or {}).get("errors") or [],
                layer="model",
                default_action="generate_mesh",
            ))
        elif build_ok and not datacheck_ok and not abaqus_available:
            blockers.append({
                "layer": "environment",
                "code": "ABAQUS_UNAVAILABLE",
                "parameter_key": None,
                "message": environment["abaqus"]["message"],
                "resolution_action": "configure_abaqus",
            })

        steps = [
            {"id": "input", "label": "输入资料", "status": "completed"},
            {"id": "parameters", "label": "确认参数", "status": "completed" if parameter_ok else "blocked"},
            {"id": "mesh_model", "label": "网格与模型", "status": "completed" if build_ok else ("ready" if parameter_ok else "not_started")},
            {"id": "model_check", "label": "模型检查", "status": "completed" if datacheck_ok else ("blocked" if build_ok and not abaqus_available else ("ready" if build_ok else "not_started"))},
            {"id": "solve_results", "label": "求解结果", "status": "completed" if results_ok else ("ready" if datacheck_ok else "not_started")},
        ]
        current = next((item for item in steps if item["status"] != "completed"), steps[-1])
        actions = {
            "parameters": {"id": "confirm_parameters", "label": "去确认参数"},
            "mesh_model": {"id": "generate_mesh", "label": "生成并检查网格"} if not mesh_ok else {"id": "build_model", "label": "生成模型文件"},
            "model_check": {"id": "configure_abaqus", "label": "配置 Abaqus 环境"} if not abaqus_available else {"id": "run_datacheck", "label": "执行 Datacheck"},
            "solve_results": {"id": "submit_solver", "label": "提交求解"},
        }
        return {
            "schema_version": "project_workflow_state_v1",
            "project_id": project_id,
            "parameter_revision": parameter_set.revision,
            "current_step": current["id"],
            "steps": steps,
            "blockers": blockers,
            "advisories": advisories,
            "next_action": actions.get(current["id"]),
            "environment": environment,
            "capability_id": parameter_set.capability_id,
            "gates": {"parameter": parameter_ok, "fe_dependencies": semantic_ok, "mesh": mesh_ok, "model_file": build_ok, "datacheck": datacheck_ok, "results": results_ok},
        }

    def fe_semantic_ir(self, project_id: str) -> dict[str, Any]:
        _, parameter_set = self._prepared_state(project_id)
        return build_fe_semantic_ir(parameter_set)

    def dependency_report(self, project_id: str) -> dict[str, Any]:
        return dependency_report(self.fe_semantic_ir(project_id))

    def semantic_preview(self, project_id: str, request: SemanticCommandRequest) -> dict[str, Any]:
        payload = self.parameters.get(project_id)
        parameter_set = ParameterSet.model_validate(payload["parameter_set"])
        return semantic_command_v1(request, parameter_set)

    def repair_preview(self, project_id: str) -> dict[str, Any]:
        workflow = self.workflow_state(project_id)
        parameter_payload = self.parameters.get(project_id)
        parameter_set = ParameterSet.model_validate(parameter_payload["parameter_set"])
        dependency_issues = self.dependency_report(project_id).get("issues") or []
        return repair_command_v1(
            [*dependency_issues, *workflow["blockers"], *workflow.get("advisories", [])],
            parameter_set,
        )

    def result_mesh(self, project_id: str) -> dict[str, Any]:
        state = get_state(project_id)
        path = state.root / "results" / "odb_result_mesh.json"
        if not path.is_file():
            raise FileNotFoundError("ODB result mesh has not been generated.")
        return json.loads(path.read_text(encoding="utf-8"))

    def artifacts(self, project_id: str) -> dict[str, Any]:
        state = get_state(project_id)
        rows: list[dict[str, Any]] = []
        observed: set[str] = set()
        for name, path in sorted(state.artifacts.items()):
            if not path.is_file():
                continue
            resolved = path.resolve()
            try:
                relative = resolved.relative_to(state.root.resolve()).as_posix()
            except ValueError:
                continue
            if not _is_core_artifact(relative):
                continue
            if relative in observed:
                continue
            observed.add(relative)
            content_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
            rows.append(
                {
                    "name": name.replace("\\", "/"),
                    "filename": resolved.name,
                    "relative_path": relative,
                    "category": _ui_artifact_category(relative),
                    "repository_category": artifact_category(relative),
                    "size_bytes": resolved.stat().st_size,
                    "content_type": content_type,
                    "previewable": (
                        resolved.suffix.lower()
                        in {".json", ".txt", ".csv", ".log", ".sta", ".msg", ".dat", ".inp"}
                        and relative != "input/reference_model.inp"
                    ),
                }
            )
        return {
            "schema_version": "project_artifact_list_v1",
            "project_id": project_id,
            "items": rows,
        }

    def artifact_path(self, project_id: str, name: str) -> Path:
        state = get_state(project_id)
        normalized = name.replace("\\", "/").lstrip("/")
        candidates = [
            path
            for key, path in state.artifacts.items()
            if key.replace("\\", "/") == normalized
        ]
        if not candidates:
            candidates = []
            for path in state.artifacts.values():
                relative = _relative_artifact(path, state.root)
                if relative == normalized:
                    candidates.append(path)
        if not candidates:
            raise FileNotFoundError(f"Artifact not found: {name}")
        resolved = candidates[0].resolve()
        try:
            resolved.relative_to(state.root.resolve())
        except ValueError as exc:
            raise FileNotFoundError(f"Artifact is outside the project: {name}") from exc
        if not resolved.is_file():
            raise FileNotFoundError(f"Artifact not found: {name}")
        return resolved

    def _prepared_state(self, project_id: str) -> tuple[Any, ParameterSet]:
        state = get_state(project_id)
        payload = self.parameters.get(project_id)
        parameter_set = ParameterSet.model_validate(payload["parameter_set"])
        state.ir = apply_parameter_set_to_ir(parameter_set, state.ir)
        return state, parameter_set

    def _persist_artifacts(self, state: Any) -> None:
        self.artifact_repository.replace(
            state.project_id,
            state.root,
            state.artifacts,
        )


def _ui_artifact_category(relative_path: str) -> str:
    normalized = relative_path.lower()
    if normalized in {"parameter_set.json", "ir.json"} or normalized.startswith(
        ("parameters/", "facts/", "input/")
    ):
        return "parameters"
    if normalized.startswith("mesh/"):
        return "mesh"
    if normalized.startswith("inp/static/"):
        return "static_inp"
    if normalized.startswith("inp/modal/"):
        return "modal_inp"
    if normalized.startswith(("jobs/", "datacheck/")) or normalized.endswith(
        (".log", ".sta", ".msg", ".dat")
    ):
        return "solver"
    if normalized.startswith("results/"):
        return "results"
    return "other"


def _is_core_artifact(relative_path: str) -> bool:
    """Limit the workspace file list to engineer-facing deliverables.

    Internal IR, preview payloads, parser working files and audit records remain
    registered because the application may still need them, but they are not
    presented as downloadable project deliverables.
    """
    normalized = relative_path.replace("\\", "/").lower().lstrip("/")
    filename = normalized.rsplit("/", 1)[-1]

    exact_paths = {
        "parameter_set.json",
        "facts/parse_validation_report.json",
        "facts/traceability_matrix.csv",
        "mesh/mesh_quality_report.json",
        "inp/l0_build_report.json",
        "results/results_summary.json",
        "results/equilibrium_check.json",
        "results/output_availability_report.json",
        "results/overall_static_summary.csv",
        "results/door_stress_summary.csv",
        "results/modal_frequencies.csv",
        "results/top_displacement_summary.csv",
        "results/buckling_factors.csv",
    }
    if normalized in exact_paths:
        return True

    if normalized.startswith("inp/") and filename.endswith(".inp"):
        return True
    if normalized.startswith(("jobs/", "datacheck/")) and filename.endswith(
        (".dat", ".msg", ".sta", ".odb")
    ):
        return True
    if normalized.startswith("results/") and filename.endswith(".odb"):
        return True
    return False


def _relative_artifact(path: Path, root: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def _structured_issues(
    issues: list[dict[str, Any]],
    *,
    layer: str,
    default_action: str,
) -> list[dict[str, Any]]:
    result = []
    for issue in issues:
        field = str(issue.get("parameter_key") or issue.get("field") or issue.get("path") or "")
        parameter_key = field if field.startswith(("geometry.", "material.", "mesh.", "loads.", "analysis.", "boundary.")) else None
        result.append({
            "layer": layer,
            "code": str(issue.get("code") or "GATE_BLOCKED"),
            "parameter_key": parameter_key,
            "message": str(issue.get("message") or "门禁未通过。"),
            "resolution_action": str(issue.get("resolution_action") or default_action),
        })
    return result


def _summary_check(check_id: str, label: str, status: str, value: Any = None) -> dict[str, Any]:
    return {"id": check_id, "label": label, "status": status, "value": value}


def _summary_group(group_id: str, label: str, checks: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {
        "passed": sum(item["status"] == "pass" for item in checks),
        "warnings": sum(item["status"] == "warning" for item in checks),
        "failed": sum(item["status"] == "fail" for item in checks),
        "not_run": sum(item["status"] == "not_run" for item in checks),
    }
    status = "fail" if counts["failed"] else ("warning" if counts["warnings"] else ("not_run" if counts["not_run"] else "pass"))
    return {"id": group_id, "label": label, "status": status, "counts": counts, "checks": checks}


def _first_numeric(metrics: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = metrics.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _metric_zero_check(check_id: str, label: str, metrics: dict[str, Any], key: str) -> dict[str, Any]:
    value = _first_numeric(metrics, (key,))
    return _summary_check(check_id, label, "not_run" if value is None else ("pass" if value == 0 else "fail"), value)


def _bounded_metric_check(
    check_id: str,
    label: str,
    metrics: dict[str, Any],
    keys: tuple[str, ...],
    limit_key: str,
) -> dict[str, Any]:
    value = _first_numeric(metrics, keys)
    limit = _first_numeric(metrics, (limit_key,))
    if value is None:
        status = "not_run"
    elif limit is not None and value > limit:
        status = "warning"
    else:
        status = "pass"
    return _summary_check(check_id, label, status, value)


def _solver_check(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    if not jobs:
        return _summary_check("solver", "Datacheck / solve status", "not_run")
    states = {str(job.get("status") or "not_run") for job in jobs}
    if states & {"blocked", "failed", "error"}:
        status = "fail"
    elif "warning" in states:
        status = "warning"
    elif states <= {"completed", "pass", "ready"}:
        status = "pass"
    else:
        status = "not_run"
    return _summary_check("solver", "Datacheck / solve status", status, sorted(states))


def _result_status(report: dict[str, Any]) -> str:
    if not report:
        return "not_run"
    value = str(report.get("status") or "").lower()
    if value in {"blocked", "failed", "error", "missing"}:
        return "fail"
    if value in {"warning", "partial"}:
        return "warning"
    if value in {"ready", "completed", "pass", "available"}:
        return "pass"
    return "not_run"
