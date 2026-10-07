from __future__ import annotations

from typing import Any, TypedDict


class DoorDerivationResult(TypedDict):
    input_status: str
    candidates: list[dict[str, Any]]
    selected_shape: str | None
    requires_confirmation: bool
    standard_references: list[str]


def derive_door_candidates(tower: list[dict[str, Any]], door: dict[str, Any]) -> DoorDerivationResult:
    if _complete_door(door):
        width = float(door["X2"])
        height = float(door["Y2"])
        bottom = float(door["H"])
        return {
            "input_status": "complete_excel",
            "candidates": [
                _candidate("ellipse", width, height, bottom, "excel:door", ["Door dimensions were read from Excel and selected by priority."], False)
            ],
            "selected_shape": "ellipse",
            "requires_confirmation": False,
            "standard_references": ["manual_rule:door_excel_priority"],
        }

    base_diameter = _base_diameter(tower)
    width = min(900.0, max(700.0, 0.22 * base_diameter)) if base_diameter else 900.0
    height = max(2200.0, 2.4 * width)
    bottom = 1200.0
    return {
        "input_status": "door_derivation_required",
        "candidates": [
            _candidate("ellipse", width, height, bottom, "derived:framework_default", ["No complete Excel door dimensions were available.", "Ellipse candidate is generated for comparison, not asserted as more stable."], True),
            _candidate("rounded_rectangle", width, height, bottom, "derived:framework_default", ["No complete Excel door dimensions were available.", "Rounded rectangle means upper/lower semicircles plus a middle rectangle.", "Stability should be compared through static, buckling and local stress results."], True),
        ],
        "selected_shape": None,
        "requires_confirmation": True,
        "standard_references": ["manual_rule:door_candidate_comparison", "manual_rule:no_shape_stability_claim_without_fea"],
    }


def _candidate(shape: str, width: float, height: float, bottom: float, source: str, assumptions: list[str], requires_confirmation: bool) -> dict[str, Any]:
    item = {
        "shape": shape,
        "width_mm": round(width, 3),
        "height_mm": round(height, 3),
        "bottom_z_mm": round(bottom, 3),
        "center_z_mm": round(bottom + 0.5 * height, 3),
        "source": source,
        "assumptions": assumptions,
        "requires_confirmation": requires_confirmation,
    }
    if shape == "rounded_rectangle":
        item["corner_radius_mm"] = round(0.5 * width, 3)
        item["middle_rect_height_mm"] = round(max(height - width, 0.0), 3)
    return item


def _complete_door(door: dict[str, Any]) -> bool:
    return all(_number(door.get(key)) is not None for key in ("X2", "Y2", "H"))


def _base_diameter(tower: list[dict[str, Any]]) -> float:
    if not tower:
        return 0.0
    return float(tower[0].get("diameter_mm") or 0.0)


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
