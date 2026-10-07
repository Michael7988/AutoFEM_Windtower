from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..capability_profile import CAPABILITY_HASH_SHA256
from ..parameter_ir import ParameterSet, apply_changes, instruction_changes


SemanticStage = Literal["pre_model", "parameter_change", "model_repair"]


class SemanticCommandRequest(BaseModel):
    stage: SemanticStage
    instruction: str = ""
    issues: list[dict[str, Any]] = Field(default_factory=list)


def semantic_command_v1(
    request: SemanticCommandRequest,
    parameter_set: ParameterSet,
) -> dict[str, Any]:
    """Create an auditable proposal. This function never changes a project."""
    text = request.instruction.strip()
    lower = text.lower()
    negated_change = bool(re.search(r"不要(?:修改|调整|设置)|不允许(?:修改|调整)|do\s+not\s+(?:change|set)", text, re.IGNORECASE))
    forbidden: list[str] = []
    mutation_intent = bool(re.search(r"修改|改为|调整|设置|change|replace|set\s+", text, re.IGNORECASE))
    if mutation_intent and ("mesh.strategy" in lower or "网格策略" in text):
        forbidden.append("mesh.strategy")
    if mutation_intent and ("mesh.element_type" in lower or "单元类型" in text or "s4r" in lower):
        forbidden.append("mesh.element_type")

    changes = [] if request.stage == "model_repair" or negated_change else instruction_changes(text, parameter_set)
    patches = []
    for change in changes:
        _candidate, diff, _validation = apply_changes(
            parameter_set, [change], increment_revision=False
        )
        normalized = diff[0]
        patches.append({
            "op": "replace",
            "parameter_key": change.key,
            "value": normalized["new_value"],
            "unit": normalized.get("unit"),
            "requires_engineer_confirmation": True,
        })
    if forbidden:
        status = "blocked"
        explanation = "指令涉及固定 FEM 规则，已阻止生成补丁。"
    elif request.stage == "model_repair":
        status = "manual_required"
        explanation = "模型修复只提供诊断和参数补丁预览，不自动修改模型或求解。"
    elif negated_change:
        status = "clarification_required"
        explanation = "检测到否定表达，未生成任何参数补丁；请明确需要保持不变的对象。"
    elif not patches:
        status = "clarification_required"
        explanation = "未识别到明确且允许修改的 Canonical 参数，请补充参数名、数值和单位。"
    else:
        status = "proposed"
        explanation = "确定性规则已生成参数候选，必须进入 preview → confirm 工程师审查。"

    prompt_hash = _hash({"stage": request.stage, "instruction": text})
    payload: dict[str, Any] = {
        "schema_version": "semantic_command_v1",
        "stage": request.stage,
        "status": status,
        "canonical_parameter_patch": patches,
        "forbidden_parameters": forbidden,
        "evidence": [],
        "knowledge_references": [],
        "knowledge_status": "not_indexed",
        "confidence": 1.0 if patches and not forbidden else 0.0,
        "explanation": explanation,
        "affected_artifacts": sorted({
            artifact
            for patch in patches
            for artifact in _affected_artifacts(str(patch["parameter_key"]))
        }),
        "provenance": {
            "parser": "deterministic_semantic_rules_v1",
            "model": "none",
            "prompt_hash_sha256": prompt_hash,
            "capability_id": parameter_set.capability_id,
            "registry_hash_sha256": parameter_set.capability_hash_sha256 or CAPABILITY_HASH_SHA256,
            "parameter_schema_version": parameter_set.schema_version,
        },
        "execution_policy": {
            "may_generate_nodes_or_elements": False,
            "may_generate_abaqus_keywords_or_inp": False,
            "may_execute_solver": False,
        },
    }
    payload["provenance"]["response_hash_sha256"] = _hash(payload)
    return payload


def repair_command_v1(
    issues: list[dict[str, Any]],
    parameter_set: ParameterSet,
) -> dict[str, Any]:
    patches = []
    manual = []
    for issue in issues:
        key = issue.get("parameter_key")
        record = parameter_set.parameters.get(str(key)) if key else None
        if issue.get("code") == "unconfirmed" and record is not None:
            patches.append({
                "op": "confirm",
                "parameter_key": key,
                "value": record.value,
                "unit": record.unit,
                "requires_engineer_confirmation": True,
            })
        else:
            manual.append(issue)
    payload: dict[str, Any] = {
        "schema_version": "semantic_command_v1",
        "stage": "model_repair",
        "status": "proposed" if patches else "manual_required",
        "canonical_parameter_patch": patches,
        "manual_diagnostics": manual,
        "forbidden_parameters": [],
        "explanation": "仅生成诊断与补丁预览；不会自动修改、重建或求解。",
        "provenance": {
            "parser": "deterministic_repair_rules_v1",
            "model": "none",
            "capability_id": parameter_set.capability_id,
            "registry_hash_sha256": parameter_set.capability_hash_sha256 or CAPABILITY_HASH_SHA256,
            "issues_hash_sha256": _hash(issues),
        },
        "execution_policy": {
            "automatic_apply": False,
            "may_generate_nodes_or_elements": False,
            "may_generate_inp": False,
            "may_execute_solver": False,
        },
    }
    payload["provenance"]["response_hash_sha256"] = _hash(payload)
    return payload


def _affected_artifacts(key: str) -> list[str]:
    if key.startswith(("geometry.", "mesh.")):
        return ["mesh", "inp", "datacheck", "solver_jobs", "results"]
    return ["inp", "datacheck", "solver_jobs", "results"]


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
