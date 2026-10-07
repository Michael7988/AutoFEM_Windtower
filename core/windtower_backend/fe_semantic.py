from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, Field

from .capability_profile import CAPABILITY_HASH_SHA256, L2_CAPABILITY_ID
from .parameter_ir import ParameterSet


class FeObject(BaseModel):
    object_id: str
    object_type: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class DependencyEdge(BaseModel):
    source_id: str
    relation: Literal["requires", "active_in", "created_in", "locked_in", "requests"]
    target_id: str


class FeSemanticIr(BaseModel):
    schema_version: Literal["fe_semantic_ir_v1"] = "fe_semantic_ir_v1"
    project_id: str
    capability_id: str
    parameter_revision: int
    engineering_domain: dict[str, Any]
    fem_domain: dict[str, Any]
    objects: list[FeObject]
    dependencies: list[DependencyEdge]
    solver_mapping: dict[str, Any]
    provenance: dict[str, Any]


def build_fe_semantic_ir(parameter_set: ParameterSet) -> dict[str, Any]:
    if parameter_set.capability_id == L2_CAPABILITY_ID:
        model = _build_l2(parameter_set)
    else:
        model = _build_l1(parameter_set)
    payload = model.model_dump(mode="json")
    payload["provenance"]["semantic_hash_sha256"] = _hash(payload)
    return payload


def dependency_report(fe_ir: dict[str, Any]) -> dict[str, Any]:
    objects = {str(item.get("object_id")): item for item in fe_ir.get("objects") or []}
    dependencies = list(fe_ir.get("dependencies") or [])
    issues: list[dict[str, Any]] = []

    duplicate_ids = _duplicates(str(item.get("object_id")) for item in fe_ir.get("objects") or [])
    for object_id in duplicate_ids:
        issues.append(_issue("DUPLICATE_OBJECT_NAME", object_id, "FE 对象 ID 重复，依赖目标不唯一。"))

    for edge in dependencies:
        source = str(edge.get("source_id") or "")
        target = str(edge.get("target_id") or "")
        if source not in objects or target not in objects:
            issues.append(_issue(
                "MISSING_DEPENDENCY", source or target,
                f"依赖边 {source} -> {target} 引用了不存在的 FE 对象。",
            ))

    for object_id, item in objects.items():
        object_type = str(item.get("object_type") or "")
        attrs = item.get("attributes") or {}
        if object_type == "surface" and int(attrs.get("estimated_member_count") or 0) <= 0:
            issues.append(_issue("EMPTY_SURFACE", object_id, "接触面为空或未关联任何单元面。"))
        if object_type == "contact_pair" and attrs.get("orientation") not in {"lower_to_upper", "primary_secondary_valid"}:
            issues.append(_issue("INVALID_CONTACT_ORIENTATION", object_id, "接触主从面方向无效。"))
        if object_type == "bolt_group":
            if int(attrs.get("declared_count") or 0) != int(attrs.get("generated_count") or 0):
                issues.append(_issue("BOLT_COUNT_MISMATCH", object_id, "声明螺栓数量与生成数量不一致。"))
        if object_type == "pretension" and attrs.get("formulation") == "native_pretension":
            if int(attrs.get("section_count") or 0) != int(attrs.get("bolt_count") or 0):
                issues.append(_issue("PRETENSION_SECTION_MISSING", object_id, "原生预紧截面数量必须等于螺栓数量。"))
        if object_type == "load_activation" and not bool(attrs.get("valid", True)):
            issues.append(_issue("LOAD_ACTIVATION_INVALID", object_id, "载荷创建、保持或激活步骤不合法。"))
        if object_type == "output_request" and not bool(attrs.get("dependencies_available", True)):
            issues.append(_issue("OUTPUT_DEPENDENCY_MISSING", object_id, "输出请求依赖的对象或分析步不存在。"))
        if object_type == "boundary_conflict":
            issues.append(_issue("BOUNDARY_CONFLICT", object_id, "同一自由度存在相互冲突的边界或载荷约束。"))
        if object_type == "unsupported_topology" or attrs.get("supported") is False:
            issues.append(_issue("UNSUPPORTED_TOPOLOGY", object_id, "该拓扑超出当前局部单法兰能力边界。"))

    steps = [
        item for item in objects.values()
        if str(item.get("object_type")) == "analysis_step"
    ]
    ordered = sorted(steps, key=lambda item: int((item.get("attributes") or {}).get("order") or 0))
    roles = [str((item.get("attributes") or {}).get("role") or "") for item in ordered]
    if fe_ir.get("capability_id") == L2_CAPABILITY_ID and roles != ["preload", "hold", "service"]:
        issues.append(_issue("PRELOAD_STEP_ORDER_INVALID", "analysis_sequence", "分析步必须依次为 preload、hold、service。"))

    topology, cycle = _topological_order(objects, dependencies)
    if cycle:
        issues.append(_issue("MISSING_DEPENDENCY", "dependency_graph", "FE 依赖图包含循环依赖。"))
    return {
        "schema_version": "fe_dependency_report_v1",
        "project_id": fe_ir.get("project_id"),
        "capability_id": fe_ir.get("capability_id"),
        "status": "blocked" if issues else "pass",
        "object_count": len(objects),
        "dependency_count": len(dependencies),
        "topological_order": topology,
        "issues": issues,
        "gate_sequence": [
            "parameters", "fe_dependencies", "geometry_mesh", "sets_surfaces_couplings",
            "contact_pretension_steps", "inp", "datacheck", "solve", "numerical_validation",
        ],
    }


def _build_l2(parameter_set: ParameterSet) -> FeSemanticIr:
    values = {key: record.value for key, record in parameter_set.parameters.items()}
    bolt_count = int(values["geometry.bolt.count"])
    formulation = str(values["interaction.pretension.formulation"])
    engineering_keys = (
        "geometry.", "material.", "loads.",
    )
    engineering = {
        "structure_family": "wind_tower_local_bolted_flange",
        "scope": "local_two_segment_single_flange",
        "parameters": {key: value for key, value in values.items() if key.startswith(engineering_keys)},
    }
    fem = {
        "parameters": {key: value for key, value in values.items() if not key.startswith(engineering_keys)},
        "idealization": {"tower_and_flange": "S4R_shell", "bolts": "B31_beam"},
        "step_sequence": ["preload_step", "hold_step", "service_step"],
    }
    objects = [
        FeObject(object_id="lower_segment", object_type="part", attributes={"element_type": "S4R"}),
        FeObject(object_id="upper_segment", object_type="part", attributes={"element_type": "S4R"}),
        FeObject(object_id="bolt_group", object_type="bolt_group", attributes={
            "element_type": "B31", "declared_count": bolt_count, "generated_count": bolt_count,
        }),
        FeObject(object_id="lower_contact_surface", object_type="surface", attributes={"estimated_member_count": bolt_count}),
        FeObject(object_id="upper_contact_surface", object_type="surface", attributes={"estimated_member_count": bolt_count}),
        FeObject(object_id="flange_contact", object_type="contact_pair", attributes={
            "orientation": "lower_to_upper", "normal": "hard",
            "friction_coefficient": values["interaction.contact.friction_coefficient"],
        }),
        FeObject(object_id="bolt_pad_couplings", object_type="coupling_group", attributes={"count": bolt_count * 2}),
        FeObject(object_id="bolt_preload", object_type="pretension", attributes={
            "formulation": formulation, "bolt_count": bolt_count,
            "section_count": bolt_count if formulation == "native_pretension" else 0,
            "force_per_bolt_n": values["interaction.pretension.force_per_bolt_n"],
        }),
        FeObject(object_id="preload_step", object_type="analysis_step", attributes={"role": "preload", "order": 1, "nlgeom": True}),
        FeObject(object_id="hold_step", object_type="analysis_step", attributes={"role": "hold", "order": 2, "nlgeom": True}),
        FeObject(object_id="service_step", object_type="analysis_step", attributes={"role": "service", "order": 3, "nlgeom": True}),
        FeObject(object_id="preload_activation", object_type="load_activation", attributes={"valid": True}),
        FeObject(object_id="l2_outputs", object_type="output_request", attributes={
            "variables": parameter_set.output_requests, "dependencies_available": True,
        }),
    ]
    dependencies = [
        DependencyEdge(source_id="flange_contact", relation="requires", target_id="lower_contact_surface"),
        DependencyEdge(source_id="flange_contact", relation="requires", target_id="upper_contact_surface"),
        DependencyEdge(source_id="flange_contact", relation="active_in", target_id="preload_step"),
        DependencyEdge(source_id="flange_contact", relation="active_in", target_id="hold_step"),
        DependencyEdge(source_id="flange_contact", relation="active_in", target_id="service_step"),
        DependencyEdge(source_id="bolt_pad_couplings", relation="requires", target_id="bolt_group"),
        DependencyEdge(source_id="bolt_preload", relation="requires", target_id="bolt_group"),
        DependencyEdge(source_id="bolt_preload", relation="created_in", target_id="preload_step"),
        DependencyEdge(source_id="bolt_preload", relation="locked_in", target_id="hold_step"),
        DependencyEdge(source_id="preload_activation", relation="requires", target_id="bolt_preload"),
        DependencyEdge(source_id="preload_activation", relation="created_in", target_id="preload_step"),
        DependencyEdge(source_id="preload_activation", relation="locked_in", target_id="hold_step"),
        DependencyEdge(source_id="l2_outputs", relation="requests", target_id="flange_contact"),
        DependencyEdge(source_id="l2_outputs", relation="requests", target_id="bolt_group"),
        DependencyEdge(source_id="l2_outputs", relation="active_in", target_id="service_step"),
    ]
    return FeSemanticIr(
        project_id=parameter_set.project_id,
        capability_id=parameter_set.capability_id,
        parameter_revision=parameter_set.revision,
        engineering_domain=engineering,
        fem_domain=fem,
        objects=objects,
        dependencies=dependencies,
        solver_mapping={
            "target": "Abaqus INP",
            "compiler_id": "deterministic_l2_flange_compiler_v1",
            "mapping_only": True,
            "llm_solver_syntax_allowed": False,
            "object_map": {item.object_id: f"ABAQUS::{item.object_type}" for item in objects},
        },
        provenance={
            "parameter_set_hash_sha256": _hash(parameter_set.model_dump(mode="json")),
            "registry_hash_sha256": parameter_set.capability_hash_sha256,
            "compiler_contract_hash_sha256": _hash("deterministic_l2_flange_compiler_v1"),
        },
    )


def _build_l1(parameter_set: ParameterSet) -> FeSemanticIr:
    values = {key: record.value for key, record in parameter_set.parameters.items()}
    objects = [
        FeObject(object_id="tower_shell", object_type="part", attributes={"element_type": "S4R"}),
        FeObject(object_id="base_fixed", object_type="boundary_condition"),
        FeObject(object_id="static_step", object_type="analysis_step", attributes={"role": "static", "order": 1}),
        FeObject(object_id="modal_step", object_type="analysis_step", attributes={"role": "modal", "order": 2}),
    ]
    return FeSemanticIr(
        project_id=parameter_set.project_id,
        capability_id=parameter_set.capability_id,
        parameter_revision=parameter_set.revision,
        engineering_domain={"parameters": {key: value for key, value in values.items() if parameter_set.parameters[key].domain == "engineering"}},
        fem_domain={"parameters": {key: value for key, value in values.items() if parameter_set.parameters[key].domain == "fem"}},
        objects=objects,
        dependencies=[DependencyEdge(source_id="base_fixed", relation="requires", target_id="tower_shell")],
        solver_mapping={
            "target": "Abaqus INP", "compiler_id": "deterministic_windtower_adapter_v1",
            "mapping_only": True, "llm_solver_syntax_allowed": False,
        },
        provenance={
            "parameter_set_hash_sha256": _hash(parameter_set.model_dump(mode="json")),
            "registry_hash_sha256": parameter_set.capability_hash_sha256 or CAPABILITY_HASH_SHA256,
        },
    )


def _topological_order(objects: dict[str, dict[str, Any]], dependencies: list[dict[str, Any]]) -> tuple[list[str], bool]:
    indegree = {key: 0 for key in objects}
    graph: dict[str, list[str]] = {key: [] for key in objects}
    for edge in dependencies:
        source, target = str(edge.get("source_id") or ""), str(edge.get("target_id") or "")
        if source not in objects or target not in objects or edge.get("relation") == "active_in":
            continue
        graph[target].append(source)
        indegree[source] += 1
    queue = sorted(key for key, count in indegree.items() if count == 0)
    ordered: list[str] = []
    while queue:
        item = queue.pop(0)
        ordered.append(item)
        for dependent in sorted(graph[item]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                queue.append(dependent)
                queue.sort()
    return ordered, len(ordered) != len(objects)


def _duplicates(values: Any) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)


def _issue(code: str, object_id: str, message: str) -> dict[str, Any]:
    return {
        "layer": "fe_semantic",
        "code": code,
        "object_id": object_id,
        "parameter_key": None,
        "message": message,
        "resolution_action": "review_semantic_object",
    }


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def inject_semantic_fault(fe_ir: dict[str, Any], fault_id: str) -> dict[str, Any]:
    """Create a deterministic fault fixture. Never mutates the source IR."""
    result = deepcopy(fe_ir)
    objects = result.setdefault("objects", [])
    dependencies = result.setdefault("dependencies", [])
    by_id = {item.get("object_id"): item for item in objects}
    if fault_id == "empty_surface":
        by_id["lower_contact_surface"]["attributes"]["estimated_member_count"] = 0
    elif fault_id == "invalid_contact_orientation":
        by_id["flange_contact"]["attributes"]["orientation"] = "reversed"
    elif fault_id == "missing_coupling":
        objects[:] = [item for item in objects if item.get("object_id") != "bolt_pad_couplings"]
        dependencies.append({"source_id": "bolt_pad_couplings", "relation": "requires", "target_id": "bolt_group"})
    elif fault_id == "bolt_count_mismatch":
        by_id["bolt_group"]["attributes"]["generated_count"] -= 1
    elif fault_id == "pretension_section_missing":
        by_id["bolt_preload"]["attributes"]["section_count"] = 0
    elif fault_id == "preload_step_order_invalid":
        by_id["preload_step"]["attributes"]["order"] = 3
        by_id["service_step"]["attributes"]["order"] = 1
    elif fault_id == "hold_step_missing":
        objects[:] = [item for item in objects if item.get("object_id") != "hold_step"]
    elif fault_id == "load_activation_invalid":
        by_id["preload_activation"]["attributes"]["valid"] = False
    elif fault_id == "output_dependency_missing":
        by_id["l2_outputs"]["attributes"]["dependencies_available"] = False
    elif fault_id == "duplicate_object_name":
        objects.append(deepcopy(by_id["bolt_group"]))
    elif fault_id == "boundary_conflict":
        objects.append(FeObject(object_id="boundary_conflict", object_type="boundary_conflict", attributes={"valid": False}).model_dump())
    elif fault_id == "unsupported_topology":
        objects.append(FeObject(object_id="solid_bolt_threads", object_type="unsupported_topology", attributes={"supported": False}).model_dump())
        dependencies.append({"source_id": "solid_bolt_threads", "relation": "requires", "target_id": "missing_solid_compiler"})
    else:
        raise ValueError(f"未知故障注入：{fault_id}")
    return result
