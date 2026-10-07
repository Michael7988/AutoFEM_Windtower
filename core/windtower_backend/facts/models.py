from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


FactStatus = Literal["confirmed_fact", "derived_fact", "assumed_fact", "unresolved_fact"]
FactCategory = Literal["geometry", "material", "section", "mesh", "load", "boundary", "analysis", "metadata"]
PackageStatus = Literal["pass", "warning", "blocked"]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class UnitValue(BaseModel):
    value: Any = None
    unit: str = ""
    normalized_value: Any = None
    normalized_unit: str = ""


class FactSource(BaseModel):
    workbook: str
    sheet: str = ""
    cell_range: str = ""
    field_name: str = ""
    raw_value: Any = None
    parser: str = "deterministic_excel_parser"


class ParseIssue(BaseModel):
    issue_id: str
    severity: Literal["info", "warning", "error"] = "warning"
    category: str
    message: str
    fact_id: str | None = None
    source: FactSource | None = None


class EngineeringFact(BaseModel):
    fact_id: str
    category: FactCategory
    status: FactStatus
    value: Any = None
    unit: str = ""
    source: FactSource
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    issues: list[ParseIssue] = Field(default_factory=list)
    derivation: dict[str, Any] | None = None
    description: str = ""


class EngineeringFactPackage(BaseModel):
    package_id: str
    source_file: str
    source_files: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now)
    parser_version: str = "engineering_facts_v1"
    status: PackageStatus = "pass"
    detected_format: str = ""
    unit_system: str = "mm-N-MPa"
    facts: list[EngineeringFact] = Field(default_factory=list)
    issues: list[ParseIssue] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    raw_parse_summary: dict[str, Any] = Field(default_factory=dict)
    llm_policy: str = "LLM suggestions are advisory only and must not overwrite confirmed deterministic facts."
