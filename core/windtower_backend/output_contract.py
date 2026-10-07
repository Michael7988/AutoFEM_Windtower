from __future__ import annotations

from copy import deepcopy
from typing import Any


OUTPUT_CONTRACT_SCHEMA_VERSION = "abaqus_output_contract_v1"
OUTPUT_PROFILES = {"standard", "global_to_local"}
DEFAULT_OUTPUT_PROFILE = "global_to_local"
LAST_INCREMENT_FREQUENCY = 999999

STATIC_NODE_FIELD_VARIABLES = ["U", "UR", "RF", "RM"]
STATIC_ELEMENT_BASE_FIELD_VARIABLES = ["S", "SF"]
STATIC_TOP_HISTORY_VARIABLES = [
    "U1", "U2", "U3", "UR1", "UR2", "UR3",
    "RF1", "RF2", "RF3", "RM1", "RM2", "RM3",
]
STATIC_BASE_HISTORY_VARIABLES = ["RF1", "RF2", "RF3", "RM1", "RM2", "RM3"]
STATIC_ENERGY_HISTORY_VARIABLES = ["ALLIE", "ALLSE", "ALLWK", "ALLAE"]
FREQUENCY_NODE_FIELD_VARIABLES = ["U", "UR"]
FREQUENCY_ELEMENT_FIELD_VARIABLES = ["S", "E"]
AUTOMATIC_FREQUENCY_HISTORY_VARIABLES = [
    "EIGVAL", "EIGFREQ", "GM",
    "PF1", "PF2", "PF3", "PF4", "PF5", "PF6",
    "EM1", "EM2", "EM3", "EM4", "EM5", "EM6",
]


def normalize_output_contract(raw: Any = None, *, legacy_field: Any = None) -> dict[str, Any]:
    """Promote legacy output declarations to the typed Abaqus output contract."""
    explicit_contract = isinstance(raw, dict) and (
        raw.get("schema_version") == OUTPUT_CONTRACT_SCHEMA_VERSION or "profile" in raw
    )
    legacy_variables = _legacy_variables(raw)
    legacy_variables.extend(_as_variable_list(legacy_field))
    source = "input" if explicit_contract else ("legacy_promoted" if raw or legacy_field else "framework_default")

    contract = {
        "schema_version": OUTPUT_CONTRACT_SCHEMA_VERSION,
        "profile": DEFAULT_OUTPUT_PROFILE,
        "source": source,
        "field_frequency": "LAST_INCREMENT",
        "history_frequency": 1,
        "nforc_region": "ALL_ELEMENTS",
        "legacy_requested_variables": _unique(legacy_variables),
    }
    if explicit_contract:
        for key in ("profile", "source", "field_frequency", "history_frequency", "nforc_region"):
            if raw.get(key) is not None:
                contract[key] = deepcopy(raw[key])
    return contract


def validate_output_contract(contract: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    if contract.get("schema_version") != OUTPUT_CONTRACT_SCHEMA_VERSION:
        errors.append(_error("schema_version", "Unsupported Abaqus output contract schema."))
    if contract.get("profile") not in OUTPUT_PROFILES:
        errors.append(_error("profile", "Output profile must be 'standard' or 'global_to_local'."))
    if contract.get("field_frequency") != "LAST_INCREMENT":
        errors.append(_error("field_frequency", "L0 currently supports field_frequency='LAST_INCREMENT' only."))
    try:
        history_frequency = int(contract.get("history_frequency", 0))
    except (TypeError, ValueError):
        history_frequency = 0
    if history_frequency < 1:
        errors.append(_error("history_frequency", "History output frequency must be a positive integer."))
    region = str(contract.get("nforc_region") or "").strip()
    if contract.get("profile") == "global_to_local" and not region:
        errors.append(_error("nforc_region", "global_to_local output requires a non-empty NFORC ELSET."))
    return errors


def resolved_output_matrix(
    contract: dict[str, Any], *, static_nlgeom: bool = False
) -> dict[str, Any]:
    """Return compiler-ready, procedure-specific output requests."""
    profile = contract.get("profile") if contract.get("profile") in OUTPUT_PROFILES else "standard"
    static_element = ["S", "LE" if static_nlgeom else "E", "SF"]
    if profile == "global_to_local":
        static_element.append("NFORC")
    return {
        "static_general": {
            "field": {
                "frequency": contract.get("field_frequency", "LAST_INCREMENT"),
                "compiled_frequency": LAST_INCREMENT_FREQUENCY,
                "node": [{"region": "ALL_NODES", "variables": list(STATIC_NODE_FIELD_VARIABLES)}],
                "element": [{
                    "region": str(contract.get("nforc_region") or "ALL_ELEMENTS"),
                    "variables": static_element,
                    "directions": True,
                }],
            },
            "history": {
                "frequency": int(contract.get("history_frequency") or 1),
                "node": [
                    {"region": "TOP_RP_NSET", "variables": list(STATIC_TOP_HISTORY_VARIABLES)},
                    {"region": "BASE_FIXED", "variables": list(STATIC_BASE_HISTORY_VARIABLES)},
                ],
                "energy": [{"region": "WHOLE_MODEL", "variables": list(STATIC_ENERGY_HISTORY_VARIABLES)}],
            },
        },
        "frequency": {
            "field": {
                "frequency": 1,
                "compiled_frequency": "EACH_MODE",
                "node": [{"region": "ALL_NODES", "variables": list(FREQUENCY_NODE_FIELD_VARIABLES)}],
                "element": [{"region": "ALL_ELEMENTS", "variables": list(FREQUENCY_ELEMENT_FIELD_VARIABLES), "directions": True}],
            },
            "history": {
                "source": "abaqus_automatic_frequency_extraction",
                "variables": list(AUTOMATIC_FREQUENCY_HISTORY_VARIABLES),
            },
        },
        "buckling": {
            "field": {
                "frequency": 1,
                "compiled_frequency": "EACH_MODE",
                "node": [{"region": "ALL_NODES", "variables": ["U", "UR"]}],
                "element": [{"region": "ALL_ELEMENTS", "variables": ["S", "E"], "directions": True}],
            },
            "history": {"source": "abaqus_automatic_eigenvalue_extraction", "variables": ["EIGVAL"]},
        },
    }


def output_contract_audit(contract: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract": deepcopy(contract),
        "validation_errors": validate_output_contract(contract),
        "resolved_requests": resolved_output_matrix(contract),
        "notes": [
            "LAST_INCREMENT is compiled to FREQUENCY=999999; Abaqus/Standard always writes the final increment.",
            "Frequency-step eigenvalues, frequencies, generalized masses, participation factors and effective masses are automatic history output.",
            "Abaqus 2023 switches LE to E in linear perturbation eigensteps; the compiler requests E explicitly to avoid an inapplicable-output warning.",
            "Eigensteps omit the ignored FREQUENCY parameter on *OUTPUT; Abaqus writes one field frame per requested mode.",
            "NFORC is field output for static-general steps only.",
        ],
    }


def _legacy_variables(raw: Any) -> list[str]:
    if isinstance(raw, list):
        variables: list[str] = []
        for item in raw:
            if isinstance(item, dict):
                variables.extend(_as_variable_list(item.get("variables")))
            else:
                variables.extend(_as_variable_list(item))
        return variables
    if isinstance(raw, dict):
        variables = _as_variable_list(raw.get("field"))
        variables.extend(_as_variable_list(raw.get("variables")))
        return variables
    return _as_variable_list(raw)


def _as_variable_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip().upper() for item in value.replace(";", ",").split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip().upper() for item in value if str(item).strip()]
    return []


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _error(field: str, message: str) -> dict[str, Any]:
    return {"field": f"model_spec_ir.outputs.{field}", "code": "OUTPUT_CONTRACT_INVALID", "message": message}
