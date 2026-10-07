from __future__ import annotations

import re
from typing import Any


PATTERNS: list[tuple[str, str, str, str]] = [
    ("license_blocked", "fatal", r"(license.*(error|denied|failed|not available|cannot|unable))|(licensed number.*exceeded)", "Abaqus license checkout failed or is unavailable."),
    ("syntax_error", "fatal", r"\*\*\*ERROR:.*(keyword|syntax|unknown option|input file processor)|unknown option|invalid keyword", "Abaqus input keyword or syntax problem detected."),
    ("empty_set", "fatal", r"has not been defined|set .* is empty|unknown assembly set|unknown part set", "A referenced NSET/ELSET/SURFACE is missing or empty."),
    ("element_degenerate", "fatal", r"zero area|excessive distortion|jacobian.*error", "Element quality or geometric degeneracy problem detected."),
    ("section_missing", "fatal", r"elements? (?:have|has) missing property definitions|section property.*not defined|no section propert", "One or more elements do not have a valid section/property assignment."),
    ("element_quality_warning", "warning", r"distorted isoparametric elements|\*\*\*WARNING: .*elements are distorted", "Element distortion warning detected."),
    ("negative_eigen_warning", "warning", r"negative eigenvalue", "Abaqus reported negative eigenvalue warnings."),
    ("boundary_missing", "warning", r"rigid body motion|zero pivot|singular|unconstrained", "Boundary conditions may be insufficient."),
    ("load_invalid", "fatal", r"\*\*\*ERROR:.*(cload|dload|dsload|surface)|surface.*not found|load.*error", "Load target or load definition appears invalid."),
    ("contact_conflict", "fatal", r"\*\*\*ERROR:.*(contact|tie constraint|constraint.*conflict|overconstraint)", "Interaction or constraint conflict detected."),
    ("contact_increment_too_large", "warning", r"displacement increment for contact is too big", "Contact displacement increment was too large."),
    ("too_many_attempts", "fatal", r"too many attempts made for this increment", "Abaqus exhausted increment attempts."),
    ("odb_missing", "fatal", r"odb.*not.*generated|output database.*not", "Abaqus did not generate an ODB file."),
    ("abaqus_warning", "warning", r"\bwarning\b", "Abaqus warnings were found."),
    ("abaqus_error", "fatal", r"^\s*\*\*\*ERROR|Abaqus Error", "Abaqus reported one or more errors."),
]


ROUTES: dict[str, dict[str, str]] = {
    "license_blocked": {"layer": "environment", "owner": "engineer", "action": "check_license_and_solver_environment"},
    "syntax_error": {"layer": "compiler_syntax", "owner": "developer", "action": "inspect_keyword_and_compiler"},
    "empty_set": {"layer": "boundary_or_set", "owner": "developer", "action": "inspect_set_generation_and_references"},
    "element_degenerate": {"layer": "geometry_or_mesh", "owner": "engineer", "action": "inspect_geometry_and_mesh_quality"},
    "element_quality_warning": {"layer": "geometry_or_mesh", "owner": "engineer", "action": "review_mesh_quality_before_acceptance"},
    "section_missing": {"layer": "material_or_section", "owner": "developer", "action": "inspect_segment_section_assignment"},
    "boundary_missing": {"layer": "boundary_or_set", "owner": "engineer", "action": "review_constraint_completeness"},
    "load_invalid": {"layer": "load_or_surface", "owner": "engineer", "action": "inspect_load_target_and_surface"},
    "contact_conflict": {"layer": "interaction", "owner": "engineer", "action": "review_interaction_constraints"},
    "contact_increment_too_large": {"layer": "interaction", "owner": "engineer", "action": "review_contact_controls"},
    "too_many_attempts": {"layer": "solver_convergence", "owner": "engineer", "action": "review_step_controls_and_model_nonlinearity"},
    "negative_eigen_warning": {"layer": "model_stability", "owner": "engineer", "action": "review_constraints_and_preload_state"},
    "odb_missing": {"layer": "solver_or_output", "owner": "engineer", "action": "inspect_solver_completion_and_output_database"},
    "job_failed": {"layer": "solver", "owner": "engineer", "action": "inspect_structured_diagnostic_evidence"},
    "abaqus_error": {"layer": "unresolved", "owner": "engineer", "action": "manual_diagnostic_review"},
    "abaqus_warning": {"layer": "unresolved", "owner": "engineer", "action": "manual_warning_review"},
}


def build_abaqus_diagnostic_report(log_text: str, returncode: int = 0, job_type: str | None = None) -> dict[str, Any]:
    findings = []
    benign_post_input_error = _benign_post_input_processor_error(log_text, returncode)
    for category, severity, pattern, message in PATTERNS:
        matches = _matching_lines(log_text, pattern)
        if matches:
            if benign_post_input_error and category == "abaqus_error":
                severity = "warning"
                category = "post_input_processor_warning"
                message = "Abaqus reported a post-input-processing environment error; no input-file error was detected."
            findings.append(
                {
                    "category": category,
                    "severity": severity,
                    "message": message,
                    "evidence": matches[:5],
                    "job_type": job_type,
                }
            )
    error_summary = _abaqus_error_summary(log_text)
    if error_summary:
        findings.append(
            {
                "category": "abaqus_error",
                "severity": "fatal",
                "message": "Abaqus reported one or more errors.",
                "evidence": error_summary[:5],
                "job_type": job_type,
            }
        )
    if returncode != 0 and not any(item["severity"] == "fatal" for item in findings):
        findings.append({"category": "job_failed", "severity": "fatal", "message": f"Abaqus exited with return code {returncode}.", "evidence": [], "job_type": job_type})
    if not findings:
        findings.append({"category": "abaqus_completed", "severity": "info", "message": "No Abaqus error or warning pattern was detected.", "evidence": [], "job_type": job_type})
    return {
        "report_type": "AbaqusDiagnosticReport",
        "status": "blocked" if any(item["severity"] == "fatal" for item in findings) else ("warning" if any(item["severity"] == "warning" for item in findings) else "pass"),
        "returncode": returncode,
        "job_type": job_type,
        "findings": findings,
        "routing": _diagnostic_routes(findings),
        "log_excerpt": _important_lines(log_text),
        "llm_input_policy": "LLM should consume this structured report, not raw unrestricted logs.",
        "repair_policy": "advisory_only; no automatic parameter mutation, rebuild or solve",
    }


def _diagnostic_routes(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    routes = []
    seen = set()
    for finding in findings:
        category = str(finding.get("category") or "")
        route = ROUTES.get(category)
        if not route:
            continue
        signature = (route["layer"], route["owner"], route["action"])
        if signature in seen:
            continue
        seen.add(signature)
        routes.append({"category": category, **route, "automatic_apply": False})
    return routes


def _benign_post_input_processor_error(text: str, returncode: int) -> bool:
    if returncode != 0:
        return False
    lower = text.lower()
    if "end analysis input file processor" not in lower and "end of user input processing" not in lower:
        return False
    error_lines = _matching_lines(text, r"^\s*\*\*\*error")
    if error_lines and any("sim database file cannot be opened" not in line.lower() for line in error_lines):
        return False
    if re.search(r"\b[1-9]\d*\s+error messages?\b", text, re.IGNORECASE):
        return False
    return "sim database file cannot be opened" in lower or "abaqus/analysis exited with errors" in lower


def _matching_lines(text: str, pattern: str) -> list[str]:
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if line and re.search(pattern, line, re.IGNORECASE):
            lines.append(line[:260])
    return lines


def _abaqus_error_summary(text: str) -> list[str]:
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        match = re.search(r"\b(\d+)\s+ERROR MESSAGES?\b", line, re.IGNORECASE)
        if match and int(match.group(1)) > 0:
            lines.append(line[:260])
    return lines


def _important_lines(text: str, limit: int = 12) -> list[str]:
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if re.search(r"error|warning|fail|abort|odb|license|singular|distort|zero|surface|set", line, re.IGNORECASE):
            lines.append(line[:260])
        if len(lines) >= limit:
            break
    return lines
