from __future__ import annotations

from typing import Any


def validate_import_facts(
    tower: list[dict[str, Any]],
    flanges: list[dict[str, Any]],
    door: dict[str, Any],
    fem_params: dict[str, Any],
    loads_inventory: dict[str, Any],
) -> tuple[list[dict[str, str]], list[str]]:
    missing: list[dict[str, str]] = []
    warnings: list[str] = []

    if len(tower) < 2:
        missing.append({"item": "tower_profile", "reason": "At least two tower profile points are required."})
    else:
        previous_z = None
        for index, point in enumerate(tower):
            z = _number(point.get("z_mm"))
            diameter = _number(point.get("diameter_mm"))
            thickness = _number(point.get("thickness_mm"))
            if z is None or diameter is None or thickness is None:
                missing.append({"item": f"tower_profile[{index}]", "reason": "z, diameter and thickness must be numeric."})
                continue
            if previous_z is not None and z <= previous_z:
                warnings.append(f"Tower profile z is not strictly increasing near row {index + 1}.")
            if diameter <= 0 or thickness <= 0:
                warnings.append(f"Tower profile has non-positive diameter/thickness near row {index + 1}.")
            previous_z = z

    if not flanges:
        warnings.append("No flange rows were recognized.")
    for index, flange in enumerate(flanges):
        if _number(flange.get("total_thickness_mm")) is None:
            warnings.append(f"Flange row {index + 1} has no total height/thickness; flange zone matching will skip this row unless it can be recovered.")
        if _number(flange.get("bolt_count")) is None or _number(flange.get("bolt_diameter_mm")) is None:
            warnings.append(f"Flange row {index + 1} has incomplete bolt information.")

    required_door = ["X2", "Y2", "H"]
    missing_door = [key for key in required_door if _number(door.get(key)) is None]
    if missing_door:
        missing.append({"item": "door_geometry", "reason": f"Missing door values: {', '.join(missing_door)}."})
        warnings.append("Door geometry is incomplete; preview/build will use a no-door draft model until confirmed.")

    required_fem = [
        "material.name",
        "material.elastic_modulus_mpa",
        "material.poisson_ratio",
        "section.type",
        "element.element_type",
        "mesh.strategy",
        "loads.top_total_force_n",
        "loads.wind_pressure_mpa",
        "boundary.name",
    ]
    missing_fem = [key for key in required_fem if fem_params.get(key) in {None, ""}]
    if missing_fem:
        missing.append({"item": "fem_model_parameters", "reason": f"Missing FEM parameters: {', '.join(missing_fem)}."})

    if not loads_inventory.get("ultimate_cases") and not loads_inventory.get("fatigue_equivalent"):
        warnings.append("No load workbook summary was attached to this import.")

    return missing, warnings


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
