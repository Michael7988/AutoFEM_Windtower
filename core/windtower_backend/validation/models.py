from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


ValidationStatus = Literal["pass", "warning", "blocked"]


class ValidationIssue(BaseModel):
    field: str
    severity: Literal["warning", "error"] = "warning"
    message: str
    category: str
    source: dict[str, Any] | None = None
    fact_ref: str | None = None


class ValidationReport(BaseModel):
    report_type: str
    status: ValidationStatus
    checks: dict[str, Any] = Field(default_factory=dict)
    issues: list[ValidationIssue] = Field(default_factory=list)


class ValidationBundle(BaseModel):
    status: ValidationStatus
    schema_validation_report: ValidationReport
    geometry_validation_report: ValidationReport
    physics_validation_report: ValidationReport
    abaqus_semantic_validation_report: ValidationReport
    summary: dict[str, Any] = Field(default_factory=dict)
