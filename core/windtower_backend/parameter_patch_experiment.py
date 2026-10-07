"""Local gates and audit records for the LLM-controlled Parameter IR patch study."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_patch(patch: Mapping[str, Any], allowed_paths: Sequence[str], ranges: Mapping[str, Mapping[str, float]]) -> Sequence[str]:
    """Return deterministic failure codes; no repair or model judging is performed."""
    if set(patch) != {"operations"} or not isinstance(patch["operations"], list) or not patch["operations"]:
        return ["PATCH_SCHEMA_INVALID"]
    errors = []
    for operation in patch["operations"]:
        if set(operation) != {"op", "path", "value", "unit"} or operation.get("op") != "replace":
            errors.append("PATCH_OPERATION_INVALID")
            continue
        path = operation["path"]
        if path not in allowed_paths:
            errors.append("PATCH_PATH_FORBIDDEN")
            continue
        if not isinstance(operation["value"], (int, float)):
            errors.append("PATCH_VALUE_NOT_NUMERIC")
            continue
        limit = ranges.get(path, {})
        if operation["value"] < limit.get("min", float("-inf")) or operation["value"] > limit.get("max", float("inf")):
            errors.append("PATCH_VALUE_OUT_OF_RANGE")
    return sorted(set(errors))


def apply_patch_to_parameter_set(parameter_set: Mapping[str, Any], patch: Mapping[str, Any]) -> Mapping[str, Any]:
    """Apply a validated patch to the existing ParameterSet key/value registry.

    The function cannot create paths or alter provenance, confirmation, or
    schema fields.  That restriction prevents an LLM response from becoming an
    alternate compiler input format.
    """
    result = deepcopy(parameter_set)
    parameters = result.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError("PARAMETER_SET_SCHEMA_INVALID")
    for operation in patch["operations"]:
        path = operation["path"]
        item = parameters.get(path)
        if not isinstance(item, dict) or item.get("key") != path:
            raise ValueError("PATCH_PATH_MISSING_FROM_PARAMETER_SET")
        if item.get("unit") != operation["unit"]:
            raise ValueError("PATCH_UNIT_MISMATCH")
        item["value"] = operation["value"]
    return result


def audit_record(*, task_id: str, repeat: int, request: Mapping[str, Any], response_text: str,
                 patch: Mapping[str, Any] | None, structural_errors: Sequence[str], contract_errors: Sequence[str],
                 compile_status: str = "NOT_RUN", semantic_result: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    return {"schema_version": "llm_parameter_patch_audit_v1", "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "task_id": task_id, "repeat": repeat, "request_hash": _hash(request), "response_hash": _hash(response_text),
            "patch_hash": _hash(patch) if patch is not None else None, "structural_errors": list(structural_errors),
            "contract_errors": list(contract_errors), "compile_status": compile_status, "semantic_result": semantic_result,
            "structural_pass": not structural_errors, "contract_pass": not contract_errors,
            "semantic_pass": bool(semantic_result and semantic_result.get("pass"))}


def summarize(records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    total = len(records)
    def count(field: str) -> int: return sum(bool(row.get(field)) for row in records)
    return {"total_attempts": total, "structural_patch_pass": count("structural_pass"),
            "contract_pass": count("contract_pass"), "compile_datacheck_pass": sum(row.get("compile_status") == "PASS" for row in records),
            "semantic_pass": count("semantic_pass"), "failure_codes": sorted({code for row in records for code in row.get("structural_errors", []) + row.get("contract_errors", [])})}
