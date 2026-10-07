from __future__ import annotations

from collections import Counter
from typing import Any

from .models import EngineeringFact, EngineeringFactPackage, FactSource, ParseIssue


def validate_fact_package(package: EngineeringFactPackage) -> EngineeringFactPackage:
    issues = list(package.issues)
    issues.extend(_required_fact_issues(package.facts, package.source_file))
    issues.extend(_geometry_issues(package.facts))
    issues.extend(_source_trace_issues(package.facts))

    severities = Counter(issue.severity for issue in issues)
    package.issues = issues
    package.status = "blocked" if severities["error"] else ("warning" if severities["warning"] else "pass")
    package.summary.update(
        {
            "status": package.status,
            "fact_count": len(package.facts),
            "confirmed_count": sum(1 for fact in package.facts if fact.status == "confirmed_fact"),
            "derived_count": sum(1 for fact in package.facts if fact.status == "derived_fact"),
            "assumed_count": sum(1 for fact in package.facts if fact.status == "assumed_fact"),
            "unresolved_count": sum(1 for fact in package.facts if fact.status == "unresolved_fact"),
            "issue_count": len(issues),
            "warning_count": severities["warning"],
            "error_count": severities["error"],
            "llm_assist_required": bool(package.raw_parse_summary.get("llm_assist_required")) or any(fact.status == "unresolved_fact" for fact in package.facts),
        }
    )
    return package


def _required_fact_issues(facts: list[EngineeringFact], workbook: str) -> list[ParseIssue]:
    ids = {fact.fact_id for fact in facts}
    required = {
        "material.name": "Material name is required before FEM IR generation.",
        "element.element_type": "Abaqus element type is required before INP compilation.",
        "mesh.strategy": "Mesh strategy is required before mesh generation.",
        "loads.top_total_force_n": "Top resultant force is required for static load setup.",
        "loads.wind_pressure_mpa": "Wind pressure is required for wind load setup.",
        "boundary.name": "Boundary condition name is required before model validation.",
    }
    source = FactSource(workbook=workbook, parser="engineering_fact_validator")
    return [
        ParseIssue(issue_id=f"missing:{fact_id}", severity="error", category="missing_required_fact", message=message, fact_id=fact_id, source=source)
        for fact_id, message in required.items()
        if fact_id not in ids
    ]


def _geometry_issues(facts: list[EngineeringFact]) -> list[ParseIssue]:
    issues: list[ParseIssue] = []
    z_facts = sorted((fact for fact in facts if fact.fact_id.startswith("tower_profile.") and fact.fact_id.endswith(".z_mm")), key=lambda item: item.fact_id)
    previous: float | None = None
    for fact in z_facts:
        value = _number(fact.value)
        if value is None:
            issues.append(ParseIssue(issue_id=f"geometry:nonnumeric:{fact.fact_id}", severity="error", category="geometry", message="Tower elevation must be numeric.", fact_id=fact.fact_id, source=fact.source))
            continue
        if previous is not None and value <= previous:
            issues.append(ParseIssue(issue_id=f"geometry:z_order:{fact.fact_id}", severity="warning", category="geometry", message="Tower elevations should be strictly increasing.", fact_id=fact.fact_id, source=fact.source))
        previous = value
    for fact in facts:
        if fact.fact_id.endswith((".diameter_mm", ".thickness_mm", ".width_mm", ".height_mm")):
            value = _number(fact.value)
            if value is not None and value <= 0:
                issues.append(ParseIssue(issue_id=f"geometry:positive:{fact.fact_id}", severity="warning", category="geometry", message="Geometry dimensions should be positive.", fact_id=fact.fact_id, source=fact.source))
    return issues


def _source_trace_issues(facts: list[EngineeringFact]) -> list[ParseIssue]:
    issues = []
    for fact in facts:
        if fact.status in {"confirmed_fact", "derived_fact"} and not fact.source.workbook:
            issues.append(ParseIssue(issue_id=f"trace:missing:{fact.fact_id}", severity="warning", category="traceability", message="Fact is missing workbook source trace.", fact_id=fact.fact_id, source=fact.source))
    return issues


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
