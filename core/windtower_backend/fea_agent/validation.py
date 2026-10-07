from __future__ import annotations

from typing import Any


def build_model_validation_report(ir: dict[str, Any], mesh_report: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a paper-oriented, typed validation report for the current FEA IR."""
    semantic = ir.get("semantic_ir", {})
    assembly = ir.get("abaqus_assembly_ir", {})
    mesh = ir.get("mesh_and_set_ir", {})
    raw = ir.get("raw_excel_ir", {})
    source_trace = ir.get("source_trace", {})
    checks = [
        _check_geometry(semantic),
        _check_material(semantic),
        _check_section_and_element(assembly),
        _check_boundary_load_steps(semantic, assembly, ir),
        _check_mesh(mesh, mesh_report),
        _check_provenance(raw, source_trace),
        _check_knowledge_refs(raw),
    ]
    errors = [item for group in checks for item in group["issues"] if item["severity"] == "fatal"]
    warnings = [item for group in checks for item in group["issues"] if item["severity"] == "warning"]
    coverage = {
        group["name"]: {
            "status": group["status"],
            "passed": group["passed"],
            "total": group["total"],
            "confidence": group["confidence"],
        }
        for group in checks
    }
    return {
        "report_type": "ModelValidationReport",
        "status": "blocked" if errors else ("warning" if warnings else "pass"),
        "blocked": bool(errors),
        "coverage": coverage,
        "issues": [item for group in checks for item in group["issues"]],
        "uncertainty_report": _uncertainty_report(ir),
        "traceability": {
            "source_trace_available": bool(source_trace),
            "field_trace_count": _count_trace_items(source_trace),
            "standard_reference_count": len(raw.get("standard_references", [])),
            "llm_authority": raw.get("llm_suggestions", {}).get("authority", "advisory_only"),
        },
        "llm_policy": {
            "allowed_outputs": ["missing item explanation", "field mapping", "candidate JSON patch", "engineering rationale"],
            "forbidden_outputs": ["direct Abaqus INP mutation", "uncited standard conclusion", "unvalidated final IR overwrite"],
        },
    }


def _group(name: str, items: list[tuple[bool, str, str, str]], confidence: float) -> dict[str, Any]:
    issues = [
        {"category": name, "field": field, "severity": severity, "message": message}
        for ok, field, severity, message in items
        if not ok
    ]
    passed = sum(1 for ok, *_ in items if ok)
    status = "blocked" if any(issue["severity"] == "fatal" for issue in issues) else ("warning" if issues else "pass")
    return {"name": name, "status": status, "passed": passed, "total": len(items), "confidence": confidence, "issues": issues}


def _check_geometry(semantic: dict[str, Any]) -> dict[str, Any]:
    sections = semantic.get("tower_sections", [])
    door = semantic.get("door_openings", [{}])[0] if semantic.get("door_openings") else {}
    increasing = all(float(a.get("z_top_mm", 0)) <= float(b.get("z_top_mm", 0)) for a, b in zip(sections, sections[1:]))
    return _group(
        "geometry",
        [
            (bool(sections), "semantic_ir.tower_sections", "fatal", "Tower sections are missing."),
            (len(sections) >= 2, "semantic_ir.tower_sections", "fatal", "At least two shell segments are required."),
            (increasing, "semantic_ir.tower_sections", "fatal", "Tower section elevations are not monotonic."),
            (bool(door.get("enabled")), "semantic_ir.door_openings", "warning", "Door opening is not enabled; door-local stress checks will be pending."),
        ],
        0.92 if sections else 0.35,
    )


def _check_material(semantic: dict[str, Any]) -> dict[str, Any]:
    material = semantic.get("materials", [{}])[0] if semantic.get("materials") else {}
    return _group(
        "material",
        [
            (bool(material.get("name")), "semantic_ir.materials.0.name", "fatal", "Material name is missing."),
            (material.get("elastic_modulus_mpa") is not None, "semantic_ir.materials.0.elastic_modulus_mpa", "fatal", "Elastic modulus is missing."),
            (material.get("poisson_ratio") is not None, "semantic_ir.materials.0.poisson_ratio", "fatal", "Poisson ratio is missing."),
            (material.get("yield_strength_mpa") is not None, "semantic_ir.materials.0.yield_strength_mpa", "warning", "Yield strength is missing; strength utilization cannot be checked."),
        ],
        0.9 if material.get("source") != "missing" else 0.45,
    )


def _check_section_and_element(assembly: dict[str, Any]) -> dict[str, Any]:
    elements = assembly.get("elements", [])
    sections = assembly.get("sections", [])
    element_type = elements[0].get("type") if elements else None
    section_type = sections[0].get("type") if sections else None
    return _group(
        "section_element",
        [
            (element_type == "S4R", "abaqus_assembly_ir.elements", "fatal", "Wind-tower L0 shell model expects S4R elements."),
            (section_type == "Shell Section", "abaqus_assembly_ir.sections", "fatal", "Shell Section definition is required."),
            (bool(sections and sections[0].get("thickness")), "abaqus_assembly_ir.sections.0.thickness", "warning", "Section thickness source is not explicit."),
        ],
        0.88,
    )


def _check_boundary_load_steps(semantic: dict[str, Any], assembly: dict[str, Any], ir: dict[str, Any]) -> dict[str, Any]:
    loads = semantic.get("loads_inventory", {})
    return _group(
        "boundary_load_step",
        [
            (bool(assembly.get("boundary_conditions")), "abaqus_assembly_ir.boundary_conditions", "fatal", "Base boundary condition is missing."),
            (bool(assembly.get("steps") or ir.get("analysis_cases")), "abaqus_assembly_ir.steps", "fatal", "Analysis step definitions are missing."),
            (loads.get("top_total_force_n") is not None or loads.get("top_horizontal_force_n") is not None, "semantic_ir.loads_inventory.top_total_force_n", "warning", "Top load is missing; default may be used."),
            (loads.get("wind_pressure_mpa") is not None, "semantic_ir.loads_inventory.wind_pressure_mpa", "warning", "Wind pressure is missing; default may be used."),
        ],
        0.82,
    )


def _check_mesh(mesh: dict[str, Any], mesh_report: dict[str, Any] | None) -> dict[str, Any]:
    required_sets = set(mesh.get("required_sets", []))
    set_stats = {item.get("name"): item for item in (mesh_report or {}).get("set_stats", [])}
    nonempty_required = all(set_stats.get(name, {}).get("count", 0) for name in required_sets) if set_stats else True
    quality = (mesh_report or {}).get("quality_report", {})
    return _group(
        "mesh",
        [
            (mesh.get("global_size_mm") is not None, "mesh_and_set_ir.global_size_mm", "fatal", "Global mesh size is missing."),
            (mesh.get("door_size_mm") is not None, "mesh_and_set_ir.door_size_mm", "warning", "Door local mesh size is missing."),
            (not quality.get("blocked", False), "mesh.quality_report", "fatal", "Mesh quality report is blocked."),
            (nonempty_required, "mesh.required_sets", "fatal", "One or more required NSET/ELSET collections are empty."),
        ],
        0.86 if mesh_report else 0.68,
    )


def _check_provenance(raw: dict[str, Any], source_trace: dict[str, Any]) -> dict[str, Any]:
    confidence = raw.get("confidence_report", {})
    return _group(
        "provenance",
        [
            (bool(raw.get("source_file") or raw.get("source_files")), "raw_excel_ir.source_file", "warning", "Source workbook is not recorded."),
            (bool(confidence), "raw_excel_ir.confidence_report", "warning", "Excel parse confidence report is missing."),
            (not confidence.get("llm_assist_required", False), "raw_excel_ir.confidence_report.llm_assist_required", "warning", "Input parser requested LLM assistance."),
            (bool(source_trace or raw.get("fem_param_records")), "source_trace", "warning", "Field-level provenance is limited."),
        ],
        0.8,
    )


def _check_knowledge_refs(raw: dict[str, Any]) -> dict[str, Any]:
    references = raw.get("standard_references", [])
    return _group(
        "knowledge_reference",
        [
            (bool(references), "raw_excel_ir.standard_references", "warning", "No standard/manual references are attached to current IR."),
            (raw.get("llm_suggestions", {}).get("authority", "advisory_only") == "advisory_only", "raw_excel_ir.llm_suggestions.authority", "fatal", "LLM authority must remain advisory-only."),
        ],
        0.72 if references else 0.55,
    )


def _uncertainty_report(ir: dict[str, Any]) -> dict[str, Any]:
    raw = ir.get("raw_excel_ir", {})
    tasks = ir.get("modeling_tasks", [])
    missing_tasks = [task for task in tasks if task.get("status") == "missing"]
    return {
        "missing_import_items": raw.get("missing_items", []),
        "missing_modeling_tasks": missing_tasks,
        "parse_warnings": raw.get("warnings", []),
        "derived_facts": sorted(raw.get("derived_facts", {}).keys()),
        "requires_engineer_confirmation": bool(missing_tasks or raw.get("confidence_report", {}).get("llm_assist_required")),
    }


def _count_trace_items(value: Any) -> int:
    if isinstance(value, dict):
        return sum(_count_trace_items(item) for item in value.values()) + len(value)
    if isinstance(value, list):
        return sum(_count_trace_items(item) for item in value)
    return 1 if value is not None else 0
