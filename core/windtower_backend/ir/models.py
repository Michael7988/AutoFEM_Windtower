from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


IrBuildStatus = Literal["pass", "warning", "blocked"]


class IrBundle(BaseModel):
    bundle_id: str
    project_id: str
    source_file: str
    status: IrBuildStatus = "pass"
    fact_ir: dict[str, Any] = Field(default_factory=dict)
    model_ir: dict[str, Any] = Field(default_factory=dict)
    abaqus_ir: dict[str, Any] = Field(default_factory=dict)
    legacy_ir: dict[str, Any] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    unresolved: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class IrBuildResult(BaseModel):
    status: IrBuildStatus
    ir_bundle: IrBundle
    issues: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
