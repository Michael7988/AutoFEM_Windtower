from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
from typing import Any, Protocol

from pydantic import BaseModel, Field

from .modeling.capabilities import CAPABILITY_ID, current_capability, default_fem_parameters, get_capability

class FemAdviceProvider(Protocol):
    def map_parameters(
        self,
        evidence: list[dict[str, Any]],
        modeling_goal: str | None,
        mapping_prompt: str,
        target_keys: list[str],
    ) -> dict[str, Any]: ...


REFERENCE_MODEL_SCHEMA_VERSION = "reference_model_summary_v2"
FEM_SPECIFICATION_SCHEMA_VERSION = "fem_specification_candidate_v2"


class TransferDecision(BaseModel):
    module: str
    decision: str
    label: str
    reason: str
    source_lines: list[int] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class ReusableFemSpecification(BaseModel):
    items: list[TransferDecision] = Field(default_factory=list)


class ConditionalTransferItem(BaseModel):
    module: str
    decision: str = "conditional"
    label: str
    reason: str
    source_lines: list[int] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    requires_engineer_confirmation: bool = True


class DeferredInpFeature(BaseModel):
    module: str
    decision: str = "deferred"
    label: str
    reason: str
    source_lines: list[int] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    status: str = "observed_not_processed"
    future_capability_id: str | None = None

_INCOMPATIBLE_KEYWORDS = {
    "CONTACT",
    "CONTACT PAIR",
    "SURFACE INTERACTION",
    "PRE-TENSION SECTION",
    "USER MATERIAL",
    "SUBSTRUCTURE GENERATE",
}
_PARTIAL_KEYWORDS = {
    "INCLUDE",
    "TIE",
    "BEAM SECTION",
    "SOLID SECTION",
}
_DEFERRED_KEYWORD_MODULES = {
    "CONTACT": "interaction",
    "CONTACT PAIR": "interaction",
    "SURFACE INTERACTION": "interaction",
    "TIE": "connection",
    "PRE-TENSION SECTION": "pretension",
    "CONNECTOR SECTION": "connection",
    "BEAM SECTION": "unsupported_primary_section",
    "SOLID SECTION": "unsupported_primary_section",
    "USER MATERIAL": "nonlinear_material",
    "PLASTIC": "nonlinear_material",
    "DAMAGE INITIATION": "nonlinear_material",
    "DAMAGE EVOLUTION": "nonlinear_material",
    "BUCKLE": "extended_analysis",
    "DYNAMIC": "extended_analysis",
    "HEAT TRANSFER": "extended_analysis",
    "RESTART": "solver_extension",
    "ADAPTIVE MESH": "solver_extension",
    "INCLUDE": "external_dependency",
}
_SUPPORTED_AUXILIARY_ELEMENTS = {"MASS", "ROTARYI"}
_SUPPORTED_CONSTRAINT_KEYWORDS = {"COUPLING", "KINEMATIC"}


class ReferenceModelService:
    def __init__(self, provider: FemAdviceProvider):
        self.provider = provider

    def analyze(
        self,
        filename: str,
        content: bytes,
        *,
        modeling_goal: str | None,
        run_llm: bool,
    ) -> dict[str, Any]:
        if Path(filename).suffix.lower() != ".inp":
            raise ValueError("参考模型仅支持 Abaqus INP 文件。")
        if not content:
            raise ValueError("参考 INP 不能为空。")
        text, encoding = _decode_inp(content)
        summary = parse_reference_inp(filename, text)
        summary["encoding"] = encoding
        summary["sha256"] = hashlib.sha256(content).hexdigest()
        summary["size_bytes"] = len(content)
        summary["reference_model_id"] = f"ref-{summary['sha256'][:12]}"
        specification = _specification_from_summary(summary)
        llm_advice = {
            "status": "not_called",
            "provider": "none",
            "model": "none",
            "fields": [],
            "usage": {},
        }
        if run_llm and summary["status"] == "parsed":
            evidence = [{
                "evidence_id": "reference-inp-summary",
                "filename": Path(filename).name,
                "location": {"line": 1},
                "text": json.dumps(_compact_summary(summary), ensure_ascii=False),
            }]
            llm_advice = self.provider.map_parameters(
                evidence,
                modeling_goal,
                mapping_prompt=_fem_mapping_prompt(),
                target_keys=current_capability()["editable_fem_parameters"],
            )
            _merge_llm_advice(specification, llm_advice)
        return {
            "summary": summary,
            "fem_specification": specification,
            "reference_engineering_candidates": reference_engineering_candidates(summary),
            "llm_advice": llm_advice,
        }


def fem_specification_from_goal(
    provider: FemAdviceProvider,
    modeling_goal: str,
    *,
    run_llm: bool,
) -> dict[str, Any]:
    specification = capability_fem_specification()
    advice = {
        "status": "not_called",
        "provider": "none",
        "model": "none",
        "fields": [],
        "usage": {},
    }
    if run_llm:
        advice = provider.map_parameters(
            [],
            modeling_goal,
            mapping_prompt=_fem_mapping_prompt(),
            target_keys=current_capability()["editable_fem_parameters"],
        )
        _merge_llm_advice(specification, advice)
    specification["source_mode"] = "modeling_goal_and_capability"
    return {"fem_specification": specification, "llm_advice": advice}


def parse_reference_inp(filename: str, text: str) -> dict[str, Any]:
    lines = text.splitlines()
    keyword_inventory: Counter[str] = Counter()
    keyword_lines: dict[str, list[int]] = defaultdict(list)
    element_types: Counter[str] = Counter()
    sets: dict[str, set[str]] = {"nset": set(), "elset": set(), "surface": set()}
    materials: list[dict[str, Any]] = []
    shell_sections: list[dict[str, Any]] = []
    boundaries: list[dict[str, Any]] = []
    loads: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    output_requests: set[str] = set()
    constraints: list[dict[str, Any]] = []
    mass_properties: dict[str, Any] = {}
    node_coordinates: dict[int, tuple[float, float, float]] = {}
    shell_connectivity: dict[int, tuple[int, int, int, int]] = {}
    elset_members: dict[str, set[int]] = defaultdict(set)
    nset_members: dict[str, set[int]] = defaultdict(set)
    nset_lines: dict[str, list[int]] = defaultdict(list)
    current_keyword = ""
    current_options: dict[str, str] = {}
    current_material: dict[str, Any] | None = None
    current_section: dict[str, Any] | None = None
    current_step: dict[str, Any] | None = None
    node_count = 0
    element_count = 0
    malformed = 0

    for line_number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("**"):
            continue
        if line.startswith("*"):
            current_keyword, current_options = _parse_keyword(line)
            keyword_inventory[current_keyword] += 1
            keyword_lines[current_keyword].append(line_number)
            if current_keyword == "MATERIAL":
                current_material = {
                    "name": current_options.get("NAME", "UNNAMED"),
                    "line": line_number,
                }
                materials.append(current_material)
            elif current_keyword == "SHELL SECTION":
                current_section = {
                    "elset": current_options.get("ELSET"),
                    "material": current_options.get("MATERIAL"),
                    "line": line_number,
                }
                shell_sections.append(current_section)
            elif current_keyword == "STEP":
                current_step = {
                    "name": current_options.get("NAME", f"STEP-{len(steps) + 1}"),
                    "line": line_number,
                    "procedures": [],
                    "nlgeom": current_options.get("NLGEOM", "NO").upper(),
                }
                steps.append(current_step)
            elif current_keyword in {"STATIC", "FREQUENCY", "BUCKLE", "DYNAMIC", "VISCO"}:
                if current_step is not None:
                    current_step["procedures"].append(current_keyword.lower())
            elif current_keyword in {"NSET", "ELSET", "SURFACE"}:
                option_name = current_options.get(current_keyword)
                if option_name:
                    sets[current_keyword.lower()].add(option_name)
                    if current_keyword == "NSET":
                        nset_lines[option_name].append(line_number)
            elif current_keyword == "ELEMENT":
                element_type = current_options.get("TYPE", "UNKNOWN").upper()
                element_types.setdefault(element_type, 0)
                if current_options.get("ELSET"):
                    sets["elset"].add(current_options["ELSET"])
            elif current_keyword in _SUPPORTED_CONSTRAINT_KEYWORDS:
                constraints.append({
                    "keyword": current_keyword,
                    "line": line_number,
                    "options": dict(current_options),
                })
            elif current_keyword == "NODE" and current_options.get("NSET"):
                sets["nset"].add(current_options["NSET"])
                nset_lines[current_options["NSET"]].append(line_number)
            continue

        values = [item.strip() for item in line.split(",")]
        try:
            if current_keyword == "NODE":
                if len(values) >= 4:
                    node_id = int(values[0])
                    node_count += 1
                    node_coordinates[node_id] = (
                        float(values[1]), float(values[2]), float(values[3])
                    )
                    node_set = current_options.get("NSET")
                    if node_set:
                        nset_members[node_set].add(node_id)
                else:
                    malformed += 1
            elif current_keyword == "ELEMENT":
                element_id = int(values[0])
                element_count += 1
                element_type = current_options.get("TYPE", "UNKNOWN").upper()
                element_types[element_type] += 1
                element_set = current_options.get("ELSET")
                if element_set:
                    elset_members[element_set].add(element_id)
                if element_type == "S4R" and len(values) >= 5:
                    shell_connectivity[element_id] = (
                        int(values[1]),
                        int(values[2]),
                        int(values[3]),
                        int(values[4]),
                    )
            elif current_keyword == "ELSET":
                name = current_options.get("ELSET")
                if name:
                    if "GENERATE" in current_options and len(values) >= 2:
                        start, stop = int(values[0]), int(values[1])
                        step = int(values[2]) if len(values) > 2 and values[2] else 1
                        elset_members[name].update(range(start, stop + 1, step))
                    else:
                        elset_members[name].update(int(item) for item in values if item)
            elif current_keyword == "NSET":
                name = current_options.get("NSET")
                if name:
                    nset_lines[name].append(line_number)
                    if "GENERATE" in current_options and len(values) >= 2:
                        start, stop = int(values[0]), int(values[1])
                        step = int(values[2]) if len(values) > 2 and values[2] else 1
                        nset_members[name].update(range(start, stop + 1, step))
                    else:
                        nset_members[name].update(int(item) for item in values if item)
            elif current_keyword == "ELASTIC" and current_material is not None:
                current_material["elastic"] = [float(values[0]), float(values[1])]
            elif current_keyword == "DENSITY" and current_material is not None:
                current_material["density"] = float(values[0])
            elif current_keyword == "SHELL SECTION" and current_section is not None:
                current_section.setdefault("thickness", float(values[0]))
            elif current_keyword == "BOUNDARY":
                dofs: list[int] = []
                if len(values) > 1 and values[1].upper() == "ENCASTRE":
                    dofs = list(range(1, 7))
                elif len(values) > 1 and values[1]:
                    first = int(float(values[1]))
                    last = int(float(values[2])) if len(values) > 2 and values[2] else first
                    dofs = list(range(first, last + 1))
                boundaries.append({"target": values[0], "dofs": dofs, "line": line_number})
            elif current_keyword == "MASS":
                mass_properties["mass"] = {
                    "value": float(values[0]),
                    "elset": current_options.get("ELSET"),
                    "line": line_number,
                }
            elif current_keyword == "ROTARY INERTIA":
                mass_properties["rotary_inertia"] = {
                    "values": [float(item) for item in values[:6]],
                    "elset": current_options.get("ELSET"),
                    "line": line_number,
                }
            elif current_keyword in {"CLOAD", "DLOAD", "DSLOAD", "GRAVITY"}:
                load: dict[str, Any] = {
                    "keyword": current_keyword,
                    "target": values[0] if values else None,
                    "line": line_number,
                    "step": current_step.get("name") if current_step else None,
                }
                if current_keyword == "CLOAD" and len(values) >= 3:
                    load["component"] = int(float(values[1]))
                    load["value"] = float(values[2])
                loads.append(load)
            elif current_keyword == "FREQUENCY" and current_step is not None and values[0]:
                current_step["modes"] = int(float(values[0]))
            elif current_keyword in {"NODE OUTPUT", "ELEMENT OUTPUT"}:
                for value in values:
                    output_requests.update(
                        token.upper() for token in re.findall(r"[A-Za-z][A-Za-z0-9_]*", value)
                    )
        except (TypeError, ValueError, IndexError):
            malformed += 1

    incompatible = sorted(_INCOMPATIBLE_KEYWORDS.intersection(keyword_inventory))
    partial = sorted(_PARTIAL_KEYWORDS.intersection(keyword_inventory))
    non_s4r = sorted(
        item
        for item in element_types
        if item not in {"S4R", *_SUPPORTED_AUXILIARY_ELEMENTS}
    )
    auxiliary_elements = {
        item: count
        for item, count in element_types.items()
        if item in _SUPPORTED_AUXILIARY_ELEMENTS
    }
    has_s4r = element_types.get("S4R", 0) > 0
    reasons: list[str] = []
    warnings: list[str] = []
    if incompatible:
        reasons.append(f"包含当前能力不支持的关键词：{', '.join(incompatible)}")
    if non_s4r:
        reasons.append(f"包含非 S4R 单元：{', '.join(non_s4r)}")
    if not has_s4r:
        reasons.append("未检测到 S4R 单元。")
    if partial:
        warnings.append(f"以下特征仅记录、不迁移：{', '.join(partial)}")
    if malformed:
        warnings.append(f"有 {malformed} 行未能完整解释。")
    if incompatible or non_s4r or not has_s4r:
        compatibility = "incompatible"
    elif partial or malformed:
        compatibility = "partial"
    else:
        compatibility = "compatible"
    status = "parsed" if keyword_inventory else "failed"
    if status == "failed":
        compatibility = "incompatible"
        reasons.append("文件中未检测到有效 Abaqus 关键词。")

    mesh_statistics = _mesh_statistics(
        node_coordinates,
        shell_connectivity,
        elset_members,
    )
    derived_geometry = _derive_door_geometry(
        node_coordinates,
        nset_members,
        nset_lines,
    )
    reference_transfer = _classify_reference_transfer(
        keyword_inventory=dict(keyword_inventory),
        keyword_lines=keyword_lines,
        element_types=dict(element_types),
        materials=materials,
        shell_sections=shell_sections,
        sets=sets,
        boundaries=boundaries,
        loads=loads,
        constraints=constraints,
        mass_properties=mass_properties,
        mesh_statistics=mesh_statistics,
        steps=steps,
        output_requests=output_requests,
    )
    has_nontransferable_control = any(
        item.get("module") == "solver_controls"
        for item in reference_transfer["conditional"]
    )
    if has_nontransferable_control or reference_transfer["deferred_features"]:
        if compatibility == "compatible":
            compatibility = "partial"
    warnings.extend(
        item["reason"]
        for item in reference_transfer["conditional"]
        if item["reason"] not in warnings
    )
    return {
        "schema_version": REFERENCE_MODEL_SCHEMA_VERSION,
        "filename": Path(filename).name,
        "status": status,
        "compatibility": compatibility,
        "compatibility_reasons": reasons,
        "warnings": warnings,
        "keyword_inventory": dict(sorted(keyword_inventory.items())),
        "keyword_lines": {key: value for key, value in sorted(keyword_lines.items())},
        "node_count": node_count,
        "element_count": element_count,
        "element_types": dict(sorted(element_types.items())),
        "primary_element_types": {"S4R": element_types.get("S4R", 0)} if has_s4r else {},
        "auxiliary_element_types": dict(sorted(auxiliary_elements.items())),
        "supported_constraints": sorted(
            _SUPPORTED_CONSTRAINT_KEYWORDS.intersection(keyword_inventory)
        ),
        "sets": {key: sorted(value) for key, value in sets.items()},
        "materials": materials,
        "shell_sections": shell_sections,
        "boundaries": boundaries,
        "loads": loads,
        "constraints": constraints,
        "mass_properties": mass_properties,
        "mesh_statistics": mesh_statistics,
        "derived_geometry": derived_geometry,
        "steps": steps,
        "output_requests": sorted(output_requests),
        "symbol_table": _supported_symbol_table(
            materials, shell_sections, sets, boundaries, loads
        ),
        "reference_graph": _supported_reference_graph(
            shell_sections, boundaries, loads, constraints
        ),
        "reference_transfer": reference_transfer,
        "deferred_features": reference_transfer["deferred_features"],
    }


def _specification_from_summary(summary: dict[str, Any]) -> dict[str, Any]:
    defaults = default_fem_parameters()
    values: dict[str, dict[str, Any]] = {}
    if summary["element_types"].get("S4R"):
        values["mesh.element_type"] = _candidate("S4R", "reference_model", True)
    procedures = {
        procedure
        for step in summary["steps"]
        for procedure in step.get("procedures", [])
    }
    if "static" in procedures:
        values["analysis.static_enabled"] = _candidate(True, "reference_model", True)
    if "frequency" in procedures:
        values["analysis.modal_enabled"] = _candidate(True, "reference_model", True)
        modes = next(
            (step.get("modes") for step in summary["steps"] if step.get("modes")),
            None,
        )
        if modes:
            values["analysis.modal_modes"] = _candidate(modes, "reference_model", True)
    if summary["boundaries"]:
        values["boundary.name"] = _candidate("BASE_FIXED", "reference_model", True)
    mesh_statistics = summary.get("mesh_statistics") or {}
    for key, value in defaults.items():
        values.setdefault(key, _candidate(value, "capability_default", True))
    return {
        "schema_version": FEM_SPECIFICATION_SCHEMA_VERSION,
        "capability_id": current_capability()["capability_id"],
        "editable_parameter_keys": current_capability()["editable_fem_parameters"],
        "reference_model_id": summary.get("reference_model_id"),
        "compatibility": summary["compatibility"],
        "status": "candidate",
        "parameters": values,
        "output_requests": summary["output_requests"] or current_capability()["output_requests"],
        "modeling_semantics": {
            "constraints": summary.get("constraints") or [],
            "mass_properties": summary.get("mass_properties") or {},
            "mesh_statistics": mesh_statistics,
            "loads": summary.get("loads") or [],
            "reference_transfer": summary.get("reference_transfer") or {},
        },
        "reference_transfer": summary.get("reference_transfer") or {},
        "deferred_features": summary.get("deferred_features") or [],
        "requires_engineer_confirmation": True,
    }


def reference_engineering_candidates(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Return scalar engineering candidates without exposing reference topology."""
    candidates: list[dict[str, Any]] = []
    geometry = summary.get("derived_geometry") or {}
    geometry_map = {
        "width_mm": ("geometry.door.width_mm", "mm"),
        "height_mm": ("geometry.door.height_mm", "mm"),
        "bottom_z_mm": ("geometry.door.bottom_z_mm", "mm"),
        "theta_center_deg": ("geometry.door.theta_center_deg", "deg"),
    }
    for source_key, (parameter_key, unit) in geometry_map.items():
        value = geometry.get(source_key)
        if value is not None:
            candidates.append({
                "key": parameter_key,
                "value": value,
                "unit": unit,
                "source_locator": geometry.get("source_set") or "door semantic node set",
                "source_lines": list(geometry.get("source_lines") or []),
                "confidence": 1.0,
            })

    material = _primary_shell_material(summary)
    if material:
        elastic = material.get("elastic") or []
        material_values: tuple[tuple[str, Any, str | None], ...] = (
            ("material.elastic_modulus_mpa", elastic[0] if len(elastic) > 0 else None, "MPa"),
            ("material.poisson_ratio", elastic[1] if len(elastic) > 1 else None, None),
            ("material.density", material.get("density"), "tonne/mm³"),
        )
        for key, value, material_unit in material_values:
            if value is not None:
                candidates.append({
                    "key": key,
                    "value": value,
                    "unit": material_unit,
                    "source_locator": f"MATERIAL {material.get('name') or 'UNNAMED'}",
                    "source_lines": [int(material["line"])] if material.get("line") else [],
                    "confidence": 1.0,
                })

    direction = _single_horizontal_load_direction(summary.get("loads") or [])
    if direction:
        candidates.append({
            "key": "loads.top_force_direction",
            "value": direction["value"],
            "unit": None,
            "source_locator": direction["target"],
            "source_lines": direction["source_lines"],
            "confidence": 1.0,
        })
    return candidates


def capability_fem_specification(capability_id: str = CAPABILITY_ID) -> dict[str, Any]:
    capability = get_capability(capability_id)
    return {
        "schema_version": FEM_SPECIFICATION_SCHEMA_VERSION,
        "capability_id": capability["capability_id"],
        "editable_parameter_keys": capability["editable_fem_parameters"],
        "reference_model_id": None,
        "compatibility": "not_provided",
        "status": "candidate",
        "parameters": {
            key: _candidate(value, "capability_default", True)
            for key, value in capability["default_fem_parameters"].items()
        },
        "output_requests": capability["output_requests"],
        "requires_engineer_confirmation": True,
    }


def _candidate(value: Any, origin: str, requires_confirmation: bool) -> dict[str, Any]:
    return {
        "value": value,
        "origin": origin,
        "confirmation_status": "pending" if requires_confirmation else "not_required",
        "requires_confirmation": requires_confirmation,
        "alternatives": [],
        "sources": [{
            "branch": _fem_source_branch(origin),
            "origin": origin,
            "locator": "current capability profile" if origin == "capability_default" else None,
        }],
    }


def _fem_source_branch(origin: str) -> str:
    if origin in {"reference_model", "model_library"}:
        return "reference_model"
    if origin == "llm_suggestion":
        return "llm_recommendation"
    if origin == "engineer_input":
        return "engineer_input"
    if origin == "capability_default":
        return "capability_profile"
    return "source_data"


def _merge_llm_advice(specification: dict[str, Any], advice: dict[str, Any]) -> None:
    capability_id = str(specification.get("capability_id") or CAPABILITY_ID)
    allowed = set(get_capability(capability_id)["editable_fem_parameters"])
    fields = advice.get("fields") or []
    if isinstance(fields, dict):
        normalized_fields = []
        for key, value in fields.items():
            if not isinstance(value, dict):
                continue
            normalized_fields.append({"path": key, **value})
        fields = normalized_fields
    if not isinstance(fields, list):
        return
    for field in fields:
        if not isinstance(field, dict):
            continue
        key = str(field.get("path") or field.get("key") or "")
        if key not in allowed or field.get("value") is None:
            continue
        suggestion = _candidate(field["value"], "llm_suggestion", True)
        suggestion["confidence"] = float(field.get("confidence") or 0.5)
        suggestion["evidence_ids"] = list(field.get("evidence_ids") or [])
        suggestion["sources"] = [{
            "branch": "llm_recommendation",
            "origin": "llm_suggestion",
            "evidence_ids": list(field.get("evidence_ids") or []),
            "model": advice.get("model"),
        }]
        current = specification["parameters"].get(key)
        if current:
            current.setdefault("alternatives", []).append(suggestion)
        else:
            specification["parameters"][key] = suggestion


def _compact_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "compatibility": summary["compatibility"],
        "element_types": summary["element_types"],
        "materials": summary["materials"],
        "shell_sections": summary["shell_sections"],
        "set_names": summary["sets"],
        "boundary_targets": summary["boundaries"],
        "load_keywords": summary["loads"],
        "steps": summary["steps"],
        "output_requests": summary["output_requests"],
        "warnings": summary["warnings"],
        "constraints": summary.get("constraints") or [],
        "mass_properties": summary.get("mass_properties") or {},
        "mesh_statistics": summary.get("mesh_statistics") or {},
        "reference_transfer": summary.get("reference_transfer") or {},
    }


def _classify_reference_transfer(
    *,
    keyword_inventory: dict[str, int],
    keyword_lines: dict[str, list[int]],
    element_types: dict[str, int],
    materials: list[dict[str, Any]],
    shell_sections: list[dict[str, Any]],
    sets: dict[str, set[str]],
    boundaries: list[dict[str, Any]],
    loads: list[dict[str, Any]],
    constraints: list[dict[str, Any]],
    mass_properties: dict[str, Any],
    mesh_statistics: dict[str, Any],
    steps: list[dict[str, Any]],
    output_requests: set[str],
) -> dict[str, Any]:
    reusable: list[TransferDecision] = []
    conditional: list[ConditionalTransferItem] = []
    deferred: list[DeferredInpFeature] = []

    def lines(*keywords: str) -> list[int]:
        return sorted({line for keyword in keywords for line in keyword_lines.get(keyword, [])})

    if element_types.get("S4R"):
        reusable.append(TransferDecision(
            module="element_and_section",
            decision="reusable",
            label="S4R 壳单元与截面规范",
            reason="仅迁移单元与截面语义，新模型网格由结构族适配器重新生成。",
            source_lines=lines("ELEMENT", "SHELL SECTION"),
            keywords=["ELEMENT", "SHELL SECTION"],
        ))
    if materials:
        reusable.append(TransferDecision(
            module="material",
            decision="reusable",
            label="线弹性材料规范",
            reason="材料属性作为候选值；工程资料中的观测值保持更高优先级。",
            source_lines=lines("MATERIAL", "ELASTIC", "DENSITY"),
            keywords=["MATERIAL", "ELASTIC", "DENSITY"],
        ))
    if mesh_statistics.get("global_edge_median_mm"):
        reusable.append(TransferDecision(
            module="mesh_size",
            decision="reusable",
            label="网格尺寸统计",
            reason="迁移网格尺寸统计，不迁移节点、单元连接或原始网格拓扑。",
            source_lines=lines("NODE", "ELEMENT"),
            keywords=["NODE", "ELEMENT"],
        ))
    if boundaries:
        reusable.append(TransferDecision(
            module="boundary",
            decision="reusable",
            label="边界约束规范",
            reason="仅在可映射为当前 BASE_FIXED 语义时进入确认流程。",
            source_lines=lines("BOUNDARY"),
            keywords=["BOUNDARY"],
        ))
    if constraints or mass_properties:
        reusable.append(TransferDecision(
            module="load_transfer",
            decision="reusable",
            label="参考点与载荷传递规范",
            reason="支持分布耦合、KINEMATIC 以及 MASS/ROTARYI 辅助特征。",
            source_lines=lines("COUPLING", "KINEMATIC", "MASS", "ROTARY INERTIA"),
            keywords=[item for item in ("COUPLING", "KINEMATIC", "MASS", "ROTARY INERTIA") if item in keyword_inventory],
        ))
    procedures = {procedure for step in steps for procedure in step.get("procedures", [])}
    supported_procedures = sorted(procedures.intersection({"static", "frequency"}))
    if supported_procedures or output_requests:
        reusable.append(TransferDecision(
            module="analysis_and_output",
            decision="reusable",
            label="静力、模态与输出规范",
            reason="仅迁移当前能力支持的分析开关、模态阶数和输出变量。",
            source_lines=lines("STEP", "STATIC", "FREQUENCY", "NODE OUTPUT", "ELEMENT OUTPUT"),
            keywords=[item.upper() for item in supported_procedures] + sorted(output_requests),
        ))

    semantic_names = sorted({name for values in sets.values() for name in values})
    semantic_roles = sorted({role for role in ("BASE", "TOP", "DOOR", "WIND") if any(role in name.upper() for name in semantic_names)})
    if semantic_roles:
        conditional.append(ConditionalTransferItem(
            module="semantic_regions",
            label="集合与表面语义",
            reason="仅复用 BASE/TOP/DOOR/WINDWARD 等语义角色和重建意图，不复用编号成员。",
            source_lines=lines("NSET", "ELSET", "SURFACE"),
            keywords=semantic_roles,
        ))
    if loads:
        conditional.append(ConditionalTransferItem(
            module="loads",
            label="荷载实现策略",
            reason="荷载类型、方向和作用区域可参考，项目荷载数值不得默认迁移。",
            source_lines=lines("CLOAD", "DLOAD", "DSLOAD", "GRAVITY"),
            keywords=sorted({str(item.get("keyword")) for item in loads}),
        ))
    if any(str(step.get("nlgeom", "NO")).upper() == "YES" for step in steps):
        conditional.append(ConditionalTransferItem(
            module="solver_controls",
            label="非线性求解控制",
            reason="检测到 NLGEOM=YES；当前能力限定线弹性分析，因此记录但不迁移。",
            source_lines=[int(step["line"]) for step in steps if str(step.get("nlgeom", "NO")).upper() == "YES"],
            keywords=["NLGEOM"],
        ))

    for keyword, module in _DEFERRED_KEYWORD_MODULES.items():
        if keyword not in keyword_inventory:
            continue
        deferred.append(DeferredInpFeature(
            module=module,
            label=keyword,
            reason=f"{keyword} 超出当前风塔 S4R 线弹性静力/模态能力，仅保留来源记录。",
            source_lines=lines(keyword),
            keywords=[keyword],
            future_capability_id=f"future:{module}",
        ))
    unsupported_elements = sorted(
        element_type
        for element_type in element_types
        if element_type not in {"S4R", *_SUPPORTED_AUXILIARY_ELEMENTS}
    )
    if unsupported_elements:
        deferred.append(DeferredInpFeature(
            module="unsupported_primary_elements",
            label="非 S4R 主体单元",
            reason="当前结构族适配器不会迁移实体、梁或其他主体单元拓扑。",
            source_lines=lines("ELEMENT"),
            keywords=unsupported_elements,
            future_capability_id="future:additional_element_families",
        ))
    return {
        "schema_version": "reference_transfer_v1",
        "reusable": [item.model_dump() for item in reusable],
        "conditional": [item.model_dump() for item in conditional],
        "deferred_features": [item.model_dump() for item in deferred],
        "blocked": [],
    }


def _supported_symbol_table(
    materials: list[dict[str, Any]],
    shell_sections: list[dict[str, Any]],
    sets: dict[str, set[str]],
    boundaries: list[dict[str, Any]],
    loads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    symbols = [
        {"name": item.get("name"), "object_type": "material", "line": item.get("line")}
        for item in materials
    ]
    symbols.extend(
        {"name": item.get("elset"), "object_type": "shell_section", "line": item.get("line")}
        for item in shell_sections
    )
    for kind, names in sets.items():
        symbols.extend({"name": name, "object_type": kind} for name in sorted(names))
    symbols.extend(
        {"name": item.get("target"), "object_type": "boundary_target", "line": item.get("line")}
        for item in boundaries
    )
    symbols.extend(
        {"name": item.get("target"), "object_type": "load_target", "line": item.get("line")}
        for item in loads
    )
    return [item for item in symbols if item.get("name")]


def _supported_reference_graph(
    shell_sections: list[dict[str, Any]],
    boundaries: list[dict[str, Any]],
    loads: list[dict[str, Any]],
    constraints: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for item in shell_sections:
        if item.get("material") and item.get("elset"):
            edges.append({"source": item["material"], "target": item["elset"], "relation": "assigned_to"})
    edges.extend(
        {"source": "boundary", "target": item.get("target"), "relation": "applies_to"}
        for item in boundaries if item.get("target")
    )
    edges.extend(
        {"source": item.get("keyword"), "target": item.get("target"), "relation": "applies_to"}
        for item in loads if item.get("target")
    )
    for item in constraints:
        options = item.get("options") or {}
        reference = options.get("REF NODE")
        surface = options.get("SURFACE")
        if reference and surface:
            edges.append({"source": reference, "target": surface, "relation": "coupled_to"})
    return edges


def _mesh_statistics(
    nodes: dict[int, tuple[float, float, float]],
    elements: dict[int, tuple[int, int, int, int]],
    elsets: dict[str, set[int]],
) -> dict[str, Any]:
    if not nodes or not elements:
        return {}
    edge_lengths: dict[int, list[float]] = {}
    all_lengths: list[float] = []
    for element_id, connectivity in elements.items():
        coordinates = [nodes.get(node_id) for node_id in connectivity]
        if any(point is None for point in coordinates):
            continue
        resolved_coordinates = [point for point in coordinates if point is not None]
        lengths = [
            math.dist(
                resolved_coordinates[index],
                resolved_coordinates[(index + 1) % 4],
            )
            for index in range(4)
        ]
        edge_lengths[element_id] = lengths
        all_lengths.extend(lengths)
    door_ids = set().union(*(
        members for name, members in elsets.items() if "DOOR" in name.upper()
    )) if any("DOOR" in name.upper() for name in elsets) else set()
    door_lengths = [
        length
        for element_id in door_ids
        for length in edge_lengths.get(element_id, [])
    ]
    xs = [point[0] for point in nodes.values()]
    ys = [point[1] for point in nodes.values()]
    zs = [point[2] for point in nodes.values()]
    return {
        "global_edge_median_mm": statistics.median(all_lengths) if all_lengths else None,
        "door_edge_median_mm": statistics.median(door_lengths) if door_lengths else None,
        "door_element_count": len(door_ids.intersection(elements)),
        "bounding_box_mm": {
            "x": [min(xs), max(xs)],
            "y": [min(ys), max(ys)],
            "z": [min(zs), max(zs)],
        },
    }


def _derive_door_geometry(
    nodes: dict[int, tuple[float, float, float]],
    nsets: dict[str, set[int]],
    source_lines: dict[str, list[int]],
) -> dict[str, Any]:
    semantic_names = [
        name
        for name in nsets
        if "DOOR" in name.upper()
        and any(token in name.upper() for token in ("OPENING", "EDGE", "BOUNDARY"))
    ]
    if not semantic_names:
        return {}
    node_ids = set().union(*(nsets[name] for name in semantic_names))
    points = [nodes[node_id] for node_id in node_ids if node_id in nodes]
    if len(points) < 4:
        return {}
    radii = [math.hypot(x, y) for x, y, _z in points]
    if not radii or statistics.median(radii) <= 0:
        return {}
    angles = [math.atan2(y, x) for x, y, _z in points]
    circular_center = math.atan2(
        sum(math.sin(angle) for angle in angles),
        sum(math.cos(angle) for angle in angles),
    )
    offsets = [_wrap_angle(angle - circular_center) for angle in angles]
    angle_min = min(offsets)
    angle_max = max(offsets)
    angle_span = angle_max - angle_min
    zs = [point[2] for point in points]
    height = max(zs) - min(zs)
    width = statistics.median(radii) * angle_span
    if width <= 0 or height <= 0 or angle_span >= math.pi:
        return {}
    center = _wrap_angle(circular_center + (angle_min + angle_max) / 2.0)
    return {
        "width_mm": round(width, 6),
        "height_mm": round(height, 6),
        "bottom_z_mm": round(min(zs), 6),
        "theta_center_deg": round(math.degrees(center), 6),
        "source_set": ", ".join(sorted(semantic_names)),
        "source_lines": sorted({
            line
            for name in semantic_names
            for line in source_lines.get(name, [])
        }),
        "node_count": len(points),
    }


def _wrap_angle(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


def _primary_shell_material(summary: dict[str, Any]) -> dict[str, Any] | None:
    material_names = {
        str(section.get("material") or "").upper()
        for section in summary.get("shell_sections") or []
        if section.get("material")
    }
    linked = [
        material
        for material in summary.get("materials") or []
        if str(material.get("name") or "").upper() in material_names
    ]
    return linked[0] if len(linked) == 1 else None


def _single_horizontal_load_direction(loads: list[dict[str, Any]]) -> dict[str, Any] | None:
    horizontal = [
        load
        for load in loads
        if load.get("keyword") == "CLOAD"
        and load.get("component") in {1, 2}
        and float(load.get("value") or 0.0) != 0.0
    ]
    if not horizontal:
        return None
    labels = {
        ("+" if float(load["value"]) > 0 else "-")
        + ("X" if int(load["component"]) == 1 else "Y")
        for load in horizontal
    }
    if len(labels) != 1:
        return None
    return {
        "value": labels.pop(),
        "target": ", ".join(sorted({str(load.get("target") or "CLOAD") for load in horizontal})),
        "source_lines": sorted({int(load["line"]) for load in horizontal if load.get("line")}),
    }


def _fem_mapping_prompt() -> str:
    return """You are a restricted FEM specification advisor. Interpret only the supplied
Abaqus INP summary and return JSON with schema_version and fields. Each field must use
one allowed target key and include path, value, status, confidence, mapping_reason and
evidence_ids. Never produce nodes, elements, Abaqus keywords, executable code or INP.
Never infer engineering geometry, material values or loads. Omit uncertain values.
All returned values are suggestions requiring engineer confirmation."""


def _parse_keyword(line: str) -> tuple[str, dict[str, str]]:
    parts = [item.strip() for item in line[1:].split(",")]
    keyword = parts[0].upper()
    options: dict[str, str] = {}
    for part in parts[1:]:
        if "=" in part:
            key, value = part.split("=", 1)
            options[key.strip().upper()] = value.strip()
        elif part:
            options[part.upper()] = "YES"
    return keyword, options


def _decode_inp(content: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "gb18030", "cp1252"):
        try:
            return content.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1", errors="replace"), "latin-1"
