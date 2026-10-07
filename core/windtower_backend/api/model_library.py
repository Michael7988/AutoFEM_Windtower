from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..model_library import ModelLibraryService


class PublishModelRequest(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    operator: str = Field(default="engineer", min_length=1, max_length=80)


def create_model_library_router(service: ModelLibraryService) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/model-library")
    def model_library() -> dict[str, Any]:
        return service.list_entries()

    @router.post("/projects/{project_id}/model-library/publish", status_code=201)
    def publish_model(project_id: str, request: PublishModelRequest) -> dict[str, Any]:
        try:
            return service.publish_project(
                project_id,
                label=request.label,
                operator=request.operator,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到项目") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return router
