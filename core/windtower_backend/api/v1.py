from __future__ import annotations

from typing import Any, Literal
from pathlib import Path
import shutil

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..abaqus_adapter import adapter_status
from ..capability_profile import list_capabilities
from ..config import Settings
from ..config import STORAGE_ROOT
from ..modeling import get_state, project_response
from ..services import ModelGenerationService, ParameterService, ValidationService
from ..services.parameters import ConfirmPreviewRequest, PreviewRequest
from ..services.semantic_commands import SemanticCommandRequest


class DatacheckRequest(BaseModel):
    job_type: Literal["all", "static_strength", "modal"] = "all"


class SubmitRequest(BaseModel):
    job_types: list[Literal["static_strength", "modal"]] = Field(
        default_factory=lambda: ["static_strength", "modal"]
    )
    run_postprocess: bool = True
    idempotency_key: str | None = None


def create_v1_router(
    settings: Settings,
    parameters: ParameterService,
    models: ModelGenerationService,
    validation: ValidationService,
) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/capabilities")
    def capabilities() -> dict[str, Any]:
        return {
            "schema_version": "autofem_capability_registry_v1",
            "items": list_capabilities(),
        }

    @router.get("/health")
    def health() -> dict[str, Any]:
        legacy = adapter_status()
        abaqus_command = str(settings.abaqus_cmd or "").strip().strip('"')
        abaqus_available = bool(
            abaqus_command and (Path(abaqus_command).is_file() or shutil.which(abaqus_command))
        )
        return {
            "ok": True,
            "api_version": "v1",
            "method": [
                "heterogeneous_input_and_llm_mapping",
                "structured_parameter_layer",
                "deterministic_fem_generation",
                "abaqus_validation_and_parameter_iteration",
            ],
            "structure_families": [
                "wind_tower_shell_with_door",
                "wind_tower_local_bolted_flange",
            ],
            "supported_analysis_tasks": ["static", "modal", "nonlinear_static_preload"],
            "llm_policy": "candidate_mapping_only",
            "generators": {
                "native_l0": {"status": "ready", "required": True},
                "native_l2": {"status": "ready", "required": True},
                "legacy_external_helpers": {
                    "status": "available" if not legacy.get("errors") else "optional_unavailable",
                    "required": False,
                    "details": legacy,
                },
            },
            "abaqus": {
                "status": "ready" if abaqus_available else "environment_blocked",
                "command": settings.abaqus_cmd,
                "required_for": ["datacheck", "solve"],
            },
        }

    @router.get("/projects/latest")
    def latest_project() -> dict[str, Any]:
        candidates = [
            path for path in STORAGE_ROOT.iterdir()
            if path.is_dir() and (path / "ir.json").is_file() and (path / "parameter_set.json").is_file()
        ]
        if not candidates:
            raise HTTPException(status_code=404, detail="尚无可恢复的项目")
        latest = max(
            candidates,
            key=lambda path: max((path / "parameter_set.json").stat().st_mtime, (path / "ir.json").stat().st_mtime),
        )
        return {"project_id": latest.name}

    @router.get("/projects/{project_id}")
    def project(project_id: str) -> dict[str, Any]:
        try:
            return project_response(get_state(project_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/projects/{project_id}/parameters")
    def project_parameters(project_id: str) -> dict[str, Any]:
        try:
            return parameters.get(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/projects/{project_id}/parameters/{key:path}/trace")
    def parameter_trace(project_id: str, key: str) -> dict[str, Any]:
        try:
            return parameters.trace(project_id, key)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/projects/{project_id}/revisions")
    def project_revisions(project_id: str) -> dict[str, Any]:
        try:
            return parameters.revisions(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/projects/{project_id}/revisions/diff")
    def project_revision_diff(
        project_id: str,
        from_revision: int = Query(..., ge=1),
        to_revision: int = Query(..., ge=1),
    ) -> dict[str, Any]:
        try:
            return parameters.revision_diff(project_id, from_revision, to_revision)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/projects/{project_id}/parameter-changes/preview")
    def preview_parameter_changes(project_id: str, request: PreviewRequest) -> dict[str, Any]:
        try:
            return parameters.preview(project_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/projects/{project_id}/parameter-changes/confirm")
    def confirm_parameter_changes(project_id: str, request: ConfirmPreviewRequest) -> dict[str, Any]:
        try:
            return parameters.confirm(project_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/projects/{project_id}/validate")
    def validate_project(project_id: str) -> dict[str, Any]:
        try:
            return models.validate(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/projects/{project_id}/mesh")
    def mesh_project(project_id: str) -> dict[str, Any]:
        try:
            return models.mesh(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/projects/{project_id}/mesh-preview")
    def mesh_preview(project_id: str) -> dict[str, Any]:
        try:
            return models.mesh_preview(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/projects/{project_id}/build")
    def build_project(project_id: str) -> dict[str, Any]:
        try:
            return models.build(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/projects/{project_id}/validation-summary")
    def validation_summary(project_id: str) -> dict[str, Any]:
        try:
            return models.validation_summary(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/projects/{project_id}/workflow-state")
    def project_workflow_state(project_id: str) -> dict[str, Any]:
        try:
            return models.workflow_state(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/projects/{project_id}/fe-semantic-ir")
    def project_fe_semantic_ir(project_id: str) -> dict[str, Any]:
        try:
            return models.fe_semantic_ir(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/projects/{project_id}/dependency-report")
    def project_dependency_report(project_id: str) -> dict[str, Any]:
        try:
            return models.dependency_report(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/projects/{project_id}/semantic/preview")
    def semantic_preview(project_id: str, request: SemanticCommandRequest) -> dict[str, Any]:
        try:
            return models.semantic_preview(project_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/projects/{project_id}/repairs/preview")
    def repair_preview(project_id: str) -> dict[str, Any]:
        try:
            return models.repair_preview(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/projects/{project_id}/datacheck")
    def datacheck_project(project_id: str, request: DatacheckRequest) -> dict[str, Any]:
        try:
            return validation.datacheck(project_id, request.job_type)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/projects/{project_id}/submissions", status_code=202)
    def submit_project(project_id: str, request: SubmitRequest) -> dict[str, Any]:
        try:
            return validation.submit(
                project_id,
                job_types=list(request.job_types),
                run_postprocess=request.run_postprocess,
                idempotency_key=request.idempotency_key,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/projects/{project_id}/submissions/{submission_id}")
    def submission(project_id: str, submission_id: str) -> dict[str, Any]:
        try:
            return validation.submission(project_id, submission_id)
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/projects/{project_id}/postprocess")
    def postprocess_project(project_id: str) -> dict[str, Any]:
        try:
            return validation.postprocess(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/projects/{project_id}/results")
    def project_results(project_id: str) -> dict[str, Any]:
        try:
            return validation.results(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/projects/{project_id}/result-mesh")
    def project_result_mesh(project_id: str) -> dict[str, Any]:
        try:
            return models.result_mesh(project_id)
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/projects/{project_id}/artifacts")
    def project_artifacts(project_id: str) -> dict[str, Any]:
        try:
            return models.artifacts(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/projects/{project_id}/artifacts/{name:path}")
    def project_artifact(
        project_id: str,
        name: str,
        download: bool = Query(default=False),
    ) -> FileResponse:
        try:
            path = models.artifact_path(project_id, name)
        except (KeyError, FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(
            path,
            filename=path.name if download else None,
        )

    return router
