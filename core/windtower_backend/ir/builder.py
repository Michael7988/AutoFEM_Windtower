from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..facts.models import EngineeringFact, EngineeringFactPackage
from ..modeling import build_ir
from .models import IrBuildResult, IrBundle


def build_ir_from_facts(package: EngineeringFactPackage, project_id: str | None = None) -> IrBuildResult:
    project_id = project_id or _stable_project_id(package)
    facts = {fact.fact_id: fact for fact in package.facts}
    tower = _tower_from_facts(facts)
    flanges = _flanges_from_facts(facts)
    door = _door_from_facts(facts)
    fem_params = _fem_params_from_facts(facts)
    unresolved = [
        {
            "fact_id": fact.fact_id,
            "category": fact.category,
            "message": fact.issues[0].message if fact.issues else "Unresolved engineering fact.",
            "source": fact.source.model_dump(),
        }
        for fact in package.facts
        if fact.status == "unresolved_fact"
    ]
    import_meta = _import_meta_from_package(package)
    legacy_ir = build_ir(project_id, package.source_file, tower, flanges, door, fem_params, import_meta=import_meta)
    fact_ir = _fact_ir(package)
    model_ir = _model_ir(legacy_ir, package)
    abaqus_ir = _abaqus_ir(legacy_ir, package)
    status = "blocked" if unresolved else ("warning" if package.status == "warning" else "pass")
    bundle = IrBundle(
        bundle_id=f"ir-{_hash_payload({'package': package.package_id, 'facts': package.summary})}",
        project_id=project_id,
        source_file=package.source_file,
        status=status,
        fact_ir=fact_ir,
        model_ir=model_ir,
        abaqus_ir=abaqus_ir,
        legacy_ir=legacy_ir,
        assumptions=_assumptions(legacy_ir, package),
        unresolved=unresolved,
        summary={
            "tower_section_count": len(legacy_ir.get("semantic_ir", {}).get("tower_sections", [])),
            "material_count": len(legacy_ir.get("semantic_ir", {}).get("materials", [])),
            "door_count": len(legacy_ir.get("semantic_ir", {}).get("door_openings", [])),
            "fact_count": len(package.facts),
            "unresolved_count": len(unresolved),
        },
    )
    return IrBuildResult(status=status, ir_bundle=bundle, issues=unresolved)


def write_ir_artifacts(bundle: IrBundle, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "fact_ir.json": out_dir / "fact_ir.json",
        "model_ir.json": out_dir / "model_ir.json",
        "abaqus_ir.json": out_dir / "abaqus_ir.json",
        "ir_bundle.json": out_dir / "ir_bundle.json",
    }
    artifacts["fact_ir.json"].write_text(json.dumps(bundle.fact_ir, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts["model_ir.json"].write_text(json.dumps(bundle.model_ir, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts["abaqus_ir.json"].write_text(json.dumps(bundle.abaqus_ir, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts["ir_bundle.json"].write_text(json.dumps(bundle.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    return artifacts


def _fact_ir(package: EngineeringFactPackage) -> dict[str, Any]:
    return {
        "ir_type": "fact_ir",
        "source_file": package.source_file,
        "unit_system": package.unit_system,
        "facts": [fact.model_dump() for fact in package.facts],
        "issues": [issue.model_dump() for issue in package.issues],
        "summary": package.summary,
        "llm_policy": package.llm_policy,
    }


def _model_ir(legacy_ir: dict[str, Any], package: EngineeringFactPackage) -> dict[str, Any]:
    semantic = legacy_ir.get("semantic_ir", {})
    return {
        "ir_type": "model_ir",
        "project": legacy_ir.get("project_meta_ir", {}),
        "unit_system": semantic.get("unit_system"),
        "geometry": {
            "tower_sections": semantic.get("tower_sections", []),
            "door_openings": semantic.get("door_openings", []),
            "flanges": semantic.get("flanges", []),
            "fact_refs": _fact_refs(package, ("tower_profile.", "tower_section.", "door.", "flange.")),
        },
        "materials": semantic.get("materials", []),
        "sections": legacy_ir.get("abaqus_assembly_ir", {}).get("sections", []),
        "mesh": legacy_ir.get("mesh_and_set_ir", {}),
        "boundary_conditions": legacy_ir.get("abaqus_assembly_ir", {}).get("boundary_conditions", []),
        "loads": semantic.get("loads_inventory", {}),
        "analysis_steps": legacy_ir.get("abaqus_assembly_ir", {}).get("steps", []),
        "outputs": legacy_ir.get("model_spec_ir", {}).get("outputs") or {"field": ["U", "RF", "S", "E"]},
        "source_trace": legacy_ir.get("source_trace") or legacy_ir.get("traceability_summary", {}),
        "assumptions": _assumptions(legacy_ir, package),
    }


def _abaqus_ir(legacy_ir: dict[str, Any], package: EngineeringFactPackage) -> dict[str, Any]:
    assembly = legacy_ir.get("abaqus_assembly_ir", {})
    mesh = legacy_ir.get("mesh_and_set_ir", {})
    steps = _planned_abaqus_steps(assembly.get("steps", []))
    return {
        "ir_type": "abaqus_ir",
        "part": assembly.get("part", {}),
        "instance": assembly.get("instance", {}),
        "elements": assembly.get("elements", []),
        "sections": assembly.get("sections", []),
        "sets": {
            "required_nsets": ["BASE_FIXED", "TOP_LOAD_NODES", "DOOR_OPENING_NODES"],
            "required_elsets": ["DOOR_PATCH_ELEMENTS", "DOOR_NEIGHBOR_ELEMENTS"],
            "fact_refs": _fact_refs(package, ("sets_surfaces.", "boundary.")),
        },
        "surfaces": mesh.get("required_surfaces", ["WINDWARD_SURFACE"]),
        "couplings": assembly.get("couplings", []),
        "boundary_conditions": assembly.get("boundary_conditions", []),
        "loads": assembly.get("loads", []),
        "steps": steps,
        "outputs": assembly.get("outputs", {}),
        "compiler": {"target": "Abaqus INP", "element_type": "S4R", "section_type": "Shell Section"},
        "source_trace": legacy_ir.get("source_trace") or legacy_ir.get("traceability_summary", {}),
    }


def _planned_abaqus_steps(existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    steps = list(existing)
    names = {str(step.get("name", "")).upper() for step in steps}
    if not any("MODAL" in name or "FREQUENCY" in name for name in names):
        steps.append({"name": "STEP-MODAL-FREQUENCY", "procedure": "frequency", "modes": 20, "source": "compiler_default", "status": "planned"})
    return steps


def _tower_from_facts(facts: dict[str, EngineeringFact]) -> list[dict[str, Any]]:
    indices = sorted({fact_id.split(".")[1] for fact_id in facts if fact_id.startswith("tower_profile.")})
    rows = []
    for index in indices:
        z = _value(facts, f"tower_profile.{index}.z_mm")
        diameter = _value(facts, f"tower_profile.{index}.diameter_mm")
        thickness = _value(facts, f"tower_profile.{index}.thickness_mm")
        if z is None or diameter is None or thickness is None:
            continue
        rows.append({"index": int(index), "z_mm": z, "diameter_mm": diameter, "thickness_mm": thickness, "source": _source(facts[f"tower_profile.{index}.z_mm"])})
    return rows


def _flanges_from_facts(facts: dict[str, EngineeringFact]) -> list[dict[str, Any]]:
    indices = sorted({fact_id.split(".")[1] for fact_id in facts if fact_id.startswith("flange.")})
    rows = []
    for index in indices:
        row = {
            "index": int(index),
            "diameter_mm": _value(facts, f"flange.{index}.diameter_mm"),
            "bolt_circle_mm": _value(facts, f"flange.{index}.bolt_circle_mm"),
            "inner_diameter_mm": _value(facts, f"flange.{index}.inner_diameter_mm"),
            "total_thickness_mm": _value(facts, f"flange.{index}.total_thickness_mm"),
            "bolt_count": int(_value(facts, f"flange.{index}.bolt_count") or 0),
            "bolt_diameter_mm": _value(facts, f"flange.{index}.bolt_diameter_mm"),
            "source": _source(next(fact for fact_id, fact in facts.items() if fact_id.startswith(f"flange.{index}."))),
        }
        if row["diameter_mm"] is not None:
            rows.append(row)
    return rows


def _door_from_facts(facts: dict[str, EngineeringFact]) -> dict[str, Any]:
    door = {}
    for key in ("D", "H", "P", "W", "X1", "X2", "Y1", "Y2"):
        value = _value(facts, f"door.{key}")
        if value is not None:
            door[key] = value
    return door


def _fem_params_from_facts(facts: dict[str, EngineeringFact]) -> dict[str, Any]:
    ignored_prefixes = ("tower_profile.", "tower_section.", "flange.", "door.", "door_candidate.", "metadata.", "load.")
    params = {}
    for fact_id, fact in facts.items():
        if fact.status == "unresolved_fact" or fact_id.startswith(ignored_prefixes):
            continue
        if "." in fact_id:
            params[fact_id] = fact.value
    return params


def _import_meta_from_package(package: EngineeringFactPackage) -> dict[str, Any]:
    raw = package.raw_parse_summary
    fem_records = []
    for fact in package.facts:
        if fact.source.parser == "typed_fem_param_parser":
            group, key = fact.fact_id.split(".", 1)
            fem_records.append(
                {
                    "path": fact.fact_id,
                    "group": group,
                    "key": key,
                    "value": fact.value,
                    "raw_value": fact.source.raw_value,
                    "value_type": "",
                    "unit": fact.unit,
                    "source": fact.source.cell_range or "engineering_facts",
                    "description": fact.description,
                    "confidence": fact.confidence,
                }
            )
    return {
        "source_file": package.source_file,
        "source_files": package.source_files,
        "detected_format": package.detected_format,
        "sheet_map": raw.get("sheet_map", {}),
        "parsed_facts": raw.get("parsed_facts", {}),
        "confidence_report": raw.get("confidence_report", {}),
        "missing_items": raw.get("missing_items", []),
        "warnings": raw.get("warnings", []),
        "fem_param_records": fem_records,
        "standard_references": [],
        "engineering_fact_package": package.model_dump(),
    }


def _fact_refs(package: EngineeringFactPackage, prefixes: tuple[str, ...]) -> list[dict[str, Any]]:
    return [
        {"fact_id": fact.fact_id, "status": fact.status, "source": fact.source.model_dump()}
        for fact in package.facts
        if fact.fact_id.startswith(prefixes)
    ]


def _assumptions(legacy_ir: dict[str, Any], package: EngineeringFactPackage) -> list[str]:
    assumptions = [
        "S4R shell element dry-run compiler target.",
        "Shell Section thickness follows tower segment thickness facts.",
        "No Abaqus execution or ODB result extraction is performed in this workflow.",
    ]
    if package.summary.get("llm_assist_required"):
        assumptions.append("Input parser requested advisory LLM assistance; confirmed facts remain deterministic.")
    for item in legacy_ir.get("modeling_decision_ir", {}).get("assumptions", []) or []:
        if isinstance(item, str):
            assumptions.append(item)
    return sorted(set(assumptions))


def _value(facts: dict[str, EngineeringFact], fact_id: str) -> Any:
    fact = facts.get(fact_id)
    return fact.value if fact else None


def _source(fact: EngineeringFact) -> str:
    return f"{fact.source.sheet}!{fact.source.cell_range}" if fact.source.sheet or fact.source.cell_range else fact.source.workbook


def _stable_project_id(package: EngineeringFactPackage) -> str:
    return f"wt-ir-{_hash_payload({'source': package.source_file, 'summary': package.summary})[:10]}"


def _hash_payload(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
