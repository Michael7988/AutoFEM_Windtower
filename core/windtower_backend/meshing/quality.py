from __future__ import annotations

import math
from typing import Any

from .door_quality import validate_door_mesh_topology
from .models import MeshQualityReport


def compute_quality(
    nodes: dict[int, tuple[float, float, float]],
    elements: dict[int, list[int]],
    sets: dict[str, Any],
    door: Any | None = None,
    controls: Any | None = None,
    max_aspect_ratio: float = 8.0,
) -> MeshQualityReport:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    aspect_ratios = []
    min_area = math.inf
    non_quad = 0
    for eid, conn in elements.items():
        if len(conn) != 4:
            non_quad += 1
            continue
        lengths = [_distance(nodes[a], nodes[b]) for a, b in zip(conn, conn[1:] + conn[:1])]
        if min(lengths) <= 1.0e-9:
            min_area = 0.0
            errors.append({"field": "mesh.elements", "message": f"Degenerated edge in element {eid}."})
            continue
        aspect_ratios.append(max(lengths) / min(lengths))
        min_area = min(min_area, _quad_area(nodes, conn))
    max_aspect = max(aspect_ratios) if aspect_ratios else 0.0
    aspect_sorted = sorted(aspect_ratios)
    aspect_p95 = aspect_sorted[min(len(aspect_sorted) - 1, int(round((len(aspect_sorted) - 1) * 0.95)))] if aspect_sorted else 0.0
    if non_quad:
        errors.append({"field": "mesh.elements", "message": f"{non_quad} non-quadrilateral elements were detected."})
    if min_area <= 1.0e-8:
        errors.append({"field": "mesh.elements", "message": "Degenerated shell elements were detected."})
    if max_aspect > max_aspect_ratio:
        warnings.append({"field": "mesh.max_aspect_ratio", "message": f"Maximum aspect ratio {max_aspect:.2f} exceeds threshold {max_aspect_ratio:.2f}."})
    for name in ("BASE_FIXED", "TOP_LOAD_NODES"):
        if not sets.get(name):
            errors.append({"field": name, "message": f"{name} is empty."})
    if not (sets.get("WINDWARD_SPOS_ELEMENTS") or sets.get("WINDWARD_SNEG_ELEMENTS")):
        errors.append({"field": "WINDWARD_SURFACE", "message": "WINDWARD_SURFACE is empty."})
    if "DOOR_OPENING_NODES" in sets and len(sets["DOOR_OPENING_NODES"]) < 8:
        errors.append({"field": "DOOR_OPENING_NODES", "message": "Door boundary has too few nodes."})
    if not sets.get("DOOR_NEIGHBOR_ELEMENTS"):
        warnings.append({"field": "DOOR_NEIGHBOR_ELEMENTS", "message": "Door neighbor element set is empty."})
    door_check = validate_door_mesh_topology(
        nodes,
        elements,
        set(sets.get("DOOR_OPENING_NODES", [])),
        door,
        controls or {},
        sets,
    )
    errors.extend(door_check["errors"])
    warnings.extend(door_check["warnings"])
    metrics = {
        "element_count": len(elements),
        "node_count": len(nodes),
        "all_quadrilateral": non_quad == 0,
        "max_aspect_ratio": max_aspect,
        "aspect_ratio_p95": aspect_p95,
        "aspect_ratio_max": max_aspect,
        "min_projected_area": min_area,
        "degenerate_element_count": sum(1 for conn in elements.values() if len(conn) == 4 and _quad_area(nodes, conn) <= 1.0e-8),
        "inverted_element_count": "not_evaluated_shell_orientation_v1",
        "min_scaled_jacobian": "not_evaluated_v1",
        "scaled_jacobian_p05": "not_evaluated_v1",
        "max_skewness": "not_evaluated_v1",
        "skewness_p95": "not_evaluated_v1",
        "max_warpage_deg": "not_evaluated_v1",
        "door_boundary_closed": bool(sets.get("DOOR_OPENING_NODES")),
        "door_boundary_nodes": len(sets.get("DOOR_OPENING_NODES", [])),
    }
    metrics.update(door_check["metrics"])
    return MeshQualityReport(
        status="blocked" if errors else ("warning" if warnings else "pass"),
        blocked=bool(errors),
        errors=errors,
        warnings=warnings,
        metrics=metrics,
    )


def _distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def _quad_area(nodes: dict[int, tuple[float, float, float]], conn: list[int]) -> float:
    p = [nodes[nid] for nid in conn]
    return 0.5 * (_tri_area(p[0], p[1], p[2]) + _tri_area(p[0], p[2], p[3]))


def _tri_area(a: tuple[float, float, float], b: tuple[float, float, float], c: tuple[float, float, float]) -> float:
    ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    ac = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    cross = (ab[1] * ac[2] - ab[2] * ac[1], ab[2] * ac[0] - ab[0] * ac[2], ab[0] * ac[1] - ab[1] * ac[0])
    return math.sqrt(sum(v * v for v in cross))
