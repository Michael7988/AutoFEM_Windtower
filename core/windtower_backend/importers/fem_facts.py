from __future__ import annotations

import re
from typing import Any, Literal, TypedDict

from .workbook_reader import SheetData, WorkbookData


ValueType = Literal["string", "number", "integer", "number_list", "boolean"]


class FemParamRecord(TypedDict):
    path: str
    group: str
    key: str
    value: Any
    raw_value: Any
    value_type: str
    unit: str
    source: str
    description: str
    confidence: float


TEXT_KEYS = {
    "meta.model_level",
    "meta.unit_system",
    "meta.target_template",
    "material.name",
    "section.type",
    "section.thickness_source",
    "section.material",
    "element.part_name",
    "element.instance_name",
    "element.element_type",
    "mesh.strategy",
    "sets_surfaces.base_set",
    "sets_surfaces.top_set",
    "sets_surfaces.wind_surface",
    "sets_surfaces.door_set",
    "boundary.name",
    "boundary.target",
    "loads.top_force_direction",
    "loads.wind_surface",
    "steps.STEP-1-TOP-REACTION",
    "steps.STEP-2-WIND-PRESSURE",
    "steps.nlgeom",
    "interaction.level",
    "interaction.flange_strategy",
    "interaction.bolt_strategy",
    "interaction.contact_strategy",
    "interaction.pretension_workflow",
    "door.reinforcement_material",
}

INTEGER_KEYS = {"mesh.hole_layers", "analysis.modal_modes"}

NUMBER_LIST_KEYS = {"boundary.dofs", "loads.wind_direction_vector"}

BOOLEAN_KEYS = {
    "loads.gravity_enabled",
    "loads.top_rotary_inertia_enabled",
    "coupling.top_ring_enabled",
    "door.reinforcement_enabled",
    "analysis.static_enabled",
    "analysis.modal_enabled",
    "analysis.static_nlgeom",
    "analysis.modal_prestress",
}

NUMBER_KEYS = {
    "material.elastic_modulus_mpa",
    "material.poisson_ratio",
    "material.density",
    "material.density_tonne_mm3",
    "material.yield_strength_mpa",
    "mesh.global_size_mm",
    "mesh.door_size_mm",
    "mesh.flange_size_mm",
    "mesh.patch_margin_u_mm",
    "mesh.patch_margin_z_mm",
    "loads.top_total_force_n",
    "loads.top_mass_tonne",
    "loads.center_of_mass_offset_mm",
    "loads.gravity_acceleration_mm_s2",
    "loads.wind_pressure_mpa",
    "loads.wind_direction_deg",
    "loads.top_rotary_inertia_Ixx_tonne_mm2",
    "loads.top_rotary_inertia_Iyy_tonne_mm2",
    "loads.top_rotary_inertia_Izz_tonne_mm2",
    "loads.top_force_parallel_n",
    "loads.top_force_transverse_n",
    "loads.top_force_vertical_n",
    "loads.top_moment_roll_nmm",
    "loads.top_moment_pitch_nmm",
    "loads.top_moment_yaw_nmm",
    "loads.hub_vertical_offset_mm",
    "door.outer_width_mm",
    "door.outer_height_mm",
    "door.frame_thickness_mm",
}

REQUIRED_FEM_KEYS = (
    "material.name",
    "material.elastic_modulus_mpa",
    "material.poisson_ratio",
    "section.type",
    "element.element_type",
    "mesh.strategy",
    "loads.top_total_force_n",
    "loads.wind_pressure_mpa",
    "boundary.name",
)


def parse_fem_params(workbook: WorkbookData) -> tuple[dict[str, Any], list[FemParamRecord], dict[str, Any]]:
    sheet = find_fem_sheet(workbook)
    if not sheet:
        return {}, [], build_fem_model_facts({}, [])
    records: list[FemParamRecord] = []
    params: dict[str, Any] = {}
    for row in range(1, sheet.nrows):
        group = str(sheet.cell(row, 0)).strip()
        key = str(sheet.cell(row, 1)).strip()
        if not group or not key:
            continue
        path = f"{group}.{key}"
        raw_value = sheet.cell(row, 2)
        value_type = value_type_for_key(path)
        value = (
            parse_wind_pressure_profile(raw_value)
            if path == "loads.wind_pressure_profile"
            else parse_value_by_type(raw_value, value_type)
        )
        record: FemParamRecord = {
            "path": path,
            "group": group,
            "key": key,
            "value": value,
            "raw_value": raw_value,
            "value_type": value_type,
            "unit": str(sheet.cell(row, 3)).strip(),
            "source": str(sheet.cell(row, 4)).strip() or "excel",
            "description": str(sheet.cell(row, 5)).strip(),
            "confidence": 1.0 if value is not None else 0.0,
        }
        records.append(record)
        params[path] = value
    return params, records, build_fem_model_facts(params, records)


def find_fem_sheet(workbook: WorkbookData) -> SheetData | None:
    for name, sheet in workbook.sheets.items():
        normalized = name.lower()
        if "fem" in normalized or ("模型" in name and "参数" in name):
            return sheet
    return None


def value_type_for_key(path: str) -> ValueType:
    if path in TEXT_KEYS:
        return "string"
    if path in INTEGER_KEYS:
        return "integer"
    if path in NUMBER_LIST_KEYS:
        return "number_list"
    if path in BOOLEAN_KEYS:
        return "boolean"
    if path in NUMBER_KEYS:
        return "number"
    return "string"


def parse_value_by_type(value: Any, value_type: ValueType) -> Any:
    if value is None or value == "":
        return None
    if value_type == "string":
        return normalize_string(value)
    if value_type == "integer":
        number = parse_number(value)
        return int(number) if number is not None else None
    if value_type == "number":
        return parse_number(value)
    if value_type == "boolean":
        text = str(value).strip().lower()
        return text in {"1", "1.0", "true", "yes", "y", "on", "是"}
    if value_type == "number_list":
        if isinstance(value, (int, float)):
            return [int(value)]
        numbers = [parse_number(item) for item in re.split(r"[,;，、\s]+", str(value).strip()) if item]
        return [int(item) if float(item).is_integer() else item for item in numbers if item is not None]
    if value_type == "boolean":
        text = str(value).strip().lower()
        return text in {"1", "true", "yes", "y", "是"}
    return value


def normalize_string(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def parse_number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    match = re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_wind_pressure_profile(value: Any) -> list[dict[str, Any]]:
    """Parse ``0-20m:0.0008`` style Excel pressure bands.

    Heights are normalized to millimetres and pressures to MPa. Pressure is
    signed because Abaqus ``*DSLOAD, P`` uses the sign together with the
    selected SPOS/SNEG face to define the physical direction. Invalid,
    overlapping, reversed, or discontinuous profiles are rejected rather than
    silently falling back to a uniform pressure.
    """
    if isinstance(value, list):
        return value
    text = str(value or "").strip()
    if not text:
        return []
    bands: list[dict[str, Any]] = []
    pattern = re.compile(
        r"^\s*([-+]?\d+(?:\.\d+)?)\s*(mm|m)?\s*[-~～至]\s*"
        r"([-+]?\d+(?:\.\d+)?)\s*(mm|m)?\s*[:：]\s*"
        r"([-+]?\d+(?:\.\d+)?)\s*(pa|kpa|mpa)?\s*$",
        re.IGNORECASE,
    )
    for index, token in enumerate(re.split(r"[;；\n]+", text), start=1):
        if not token.strip():
            continue
        match = pattern.match(token)
        if not match:
            return []
        z0, z0_unit, z1, z1_unit, pressure, pressure_unit = match.groups()
        height_unit = (z1_unit or z0_unit or "m").lower()
        z_scale = 1.0 if height_unit == "mm" else 1000.0
        p_scale = {"pa": 1.0e-6, "kpa": 1.0e-3, "mpa": 1.0}.get(
            (pressure_unit or "mpa").lower(), 1.0
        )
        band = {
            "band_id": f"WIND_BAND_{index:02d}",
            "z_min_mm": float(z0) * z_scale,
            "z_max_mm": float(z1) * z_scale,
            "pressure_mpa": float(pressure) * p_scale,
            "source_text": token.strip(),
        }
        if band["z_max_mm"] <= band["z_min_mm"]:
            return []
        if bands and abs(band["z_min_mm"] - bands[-1]["z_max_mm"]) > 1.0e-6:
            return []
        bands.append(band)
    return bands


def build_fem_model_facts(params: dict[str, Any], records: list[FemParamRecord]) -> dict[str, Any]:
    missing = [key for key in REQUIRED_FEM_KEYS if params.get(key) in {None, ""}]
    return {
        "material": _group(params, "material", ["name", "elastic_modulus_mpa", "poisson_ratio", "density", "density_tonne_mm3", "yield_strength_mpa"]),
        "section": _group(params, "section", ["type", "thickness_source", "material"]),
        "element": _group(params, "element", ["part_name", "instance_name", "element_type"]),
        "mesh": _group(params, "mesh", ["strategy", "global_size_mm", "door_size_mm", "flange_size_mm", "hole_layers", "patch_margin_u_mm", "patch_margin_z_mm"]),
        "boundary_conditions": [_group(params, "boundary", ["name", "target", "dofs"])],
        "loads": _group(params, "loads", [
            "top_total_force_n",
            "top_force_direction",
            "top_mass_tonne",
            "center_of_mass_offset_mm",
            "gravity_enabled",
            "gravity_acceleration_mm_s2",
            "wind_pressure_mpa",
            "wind_pressure_profile",
            "wind_surface",
            "wind_direction_deg",
            "wind_direction_vector",
            "top_force_parallel_n",
            "top_force_transverse_n",
            "top_force_vertical_n",
            "top_moment_roll_nmm",
            "top_moment_pitch_nmm",
            "top_moment_yaw_nmm",
            "hub_vertical_offset_mm",
        ]),
        "door_reinforcement": _group(params, "door", [
            "reinforcement_enabled", "outer_width_mm", "outer_height_mm",
            "frame_thickness_mm", "reinforcement_material",
        ]),
        "steps": _group(params, "steps", ["STEP-1-TOP-REACTION", "STEP-2-WIND-PRESSURE", "nlgeom"]),
        "interactions": _group(params, "interaction", ["level", "flange_strategy", "bolt_strategy", "contact_strategy", "pretension_workflow"]),
        "outputs": {"field": ["U", "RF", "S", "E"], "source": "framework_default"},
        "records": records,
        "missing_items": [{"item": key, "reason": "Required FEM model parameter is missing."} for key in missing],
        "confidence_report": {
            "required_count": len(REQUIRED_FEM_KEYS),
            "missing_count": len(missing),
            "status": "pass" if not missing else "missing_required_fields",
        },
    }


def _group(params: dict[str, Any], prefix: str, keys: list[str]) -> dict[str, Any]:
    return {key: params.get(f"{prefix}.{key}") for key in keys if f"{prefix}.{key}" in params}
