from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..capability_profile import L2_CAPABILITY_ID
from ..parameter_ir import ParameterSet, l2_parameter_set, validate_parameter_set


DATABASE_SCHEMA = "autofem_l2_parameter_database_v1"


def load_l2_parameter_database(
    path: str | Path,
    *,
    model_id: str,
    project_id: str | None = None,
) -> tuple[ParameterSet, dict[str, Any]]:
    """Load one frozen L2 record without allowing solver syntax in the database."""
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("schema_version") != DATABASE_SCHEMA:
        raise ValueError(f"Unsupported L2 parameter database schema: {payload.get('schema_version')}")
    models = {item.get("model_id"): item for item in payload.get("models", [])}
    if model_id not in models:
        raise KeyError(f"L2 model is not present in the database: {model_id}")
    model = models[model_id]
    if model.get("capability_id") != L2_CAPABILITY_ID:
        raise ValueError("The database record is not bound to the supported L2 capability.")
    records = model.get("parameters") or []
    forbidden = {"nodes", "elements", "abaqus_keywords", "inp", "solver_commands"}
    if forbidden.intersection(model):
        raise ValueError("The parameter database must not contain generated mesh or solver syntax.")
    values = {str(item["key"]): item.get("value") for item in records}
    confirmed = bool(records) and all(item.get("confirmation_status") == "confirmed" for item in records)
    parameter_set = l2_parameter_set(project_id or model_id, values, confirmed=confirmed)
    validation = validate_parameter_set(parameter_set)
    if validation["status"] != "pass":
        raise ValueError(f"L2 database record failed canonical validation: {validation['issues']}")
    provenance = {
        "database_path": str(source.resolve()),
        "database_schema": DATABASE_SCHEMA,
        "model_id": model_id,
        "baseline_role": model.get("baseline_role"),
        "source_artifacts": model.get("source_artifacts") or [],
        "assumptions": model.get("assumptions") or [],
    }
    return parameter_set, provenance
