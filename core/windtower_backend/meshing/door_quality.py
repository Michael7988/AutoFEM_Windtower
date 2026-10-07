from __future__ import annotations

import hashlib
import json
import math
from statistics import median
from typing import Any

from .models import DoorMeshInput

DOOR_DIMENSION_INVALID = "DOOR_DIMENSION_INVALID"
DOOR_POSITION_INVALID = "DOOR_POSITION_INVALID"
PATCH_MARGIN_INSUFFICIENT = "PATCH_MARGIN_INSUFFICIENT"
DOOR_BOUNDARY_NOT_CLOSED = "DOOR_BOUNDARY_NOT_CLOSED"
MESH_TOPOLOGY_INVALID = "MESH_TOPOLOGY_INVALID"
MESH_QUALITY_BELOW_THRESHOLD = "MESH_QUALITY_BELOW_THRESHOLD"
MESH_SET_GENERATION_FAILED = "MESH_SET_GENERATION_FAILED"


def validate_door_input(door: Any, controls: Any, tower_height_mm: float, circumference_mm: float | None = None) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    if door is None:
        return errors
    width = _field(door, "width_mm", "X2")
    height = _field(door, "height_mm", "Y2")
    bottom = _field(door, "bottom_z_mm", "H")
    for field, value in (("door.width_mm", width), ("door.height_mm", height), ("door.bottom_z_mm", bottom)):
        if value is None or not math.isfinite(float(value)):
            errors.append(_error(field, DOOR_DIMENSION_INVALID, f"{field} must be numeric."))
    if errors:
        return errors
    width = float(width)
    height = float(height)
    bottom = float(bottom)
    if width <= 0.0 or height <= 0.0:
        errors.append(_error("semantic_ir.door_openings", DOOR_DIMENSION_INVALID, "Door width and height must be positive."))
    if bottom < 0.0 or bottom + height > tower_height_mm:
        errors.append(_error("semantic_ir.door_openings", DOOR_POSITION_INVALID, "Door opening must stay within the tower height."))
    if circumference_mm and width >= 0.45 * circumference_mm:
        errors.append(_error("semantic_ir.door_openings.width_mm", DOOR_POSITION_INVALID, "Door width is too large for the local tower circumference."))
    door_size = float(getattr(controls, "door_size_mm", _dict_get(controls, "door_size_mm", 0.0)) or 0.0)
    global_size = float(getattr(controls, "global_size_mm", _dict_get(controls, "global_size_mm", 0.0)) or 0.0)
    layers = int(getattr(controls, "hole_layers", _dict_get(controls, "hole_layers", 0)) or 0)
    margin_u = float(getattr(controls, "patch_margin_u_mm", _dict_get(controls, "patch_margin_u_mm", 0.0)) or 0.0)
    margin_z = float(getattr(controls, "patch_margin_z_mm", _dict_get(controls, "patch_margin_z_mm", 0.0)) or 0.0)
    if door_size <= 0.0 or global_size <= 0.0 or layers < 1:
        errors.append(_error("mesh_and_set_ir", DOOR_DIMENSION_INVALID, "Mesh sizes must be positive and hole_layers must be at least 1."))
    if margin_u <= 0.0 or margin_z <= 0.0:
        errors.append(_error("mesh_and_set_ir", PATCH_MARGIN_INSUFFICIENT, "Door patch margins must be positive."))
    if margin_u < max(door_size, 0.25 * width) or margin_z < max(door_size, 0.15 * height):
        errors.append(_error("mesh_and_set_ir", PATCH_MARGIN_INSUFFICIENT, "Door patch margins are too small for a stable transition region."))
    return errors


def validate_door_mesh_topology(
    nodes: dict[int, tuple[float, float, float]],
    elements: dict[int, list[int]],
    door_nodes: set[int] | list[int],
    door: DoorMeshInput | None,
    controls: Any,
    sets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metrics = compute_door_mesh_metrics(nodes, elements, sets or {}, door_nodes, door, controls)
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if door is None:
        return {"errors": errors, "warnings": warnings, "metrics": metrics}
    if metrics["door_boundary_loop_count"] != 1 or not metrics["door_boundary_is_closed"] or metrics["door_boundary_branch_nodes"] != 0:
        errors.append(_error("DOOR_OPENING_NODES", DOOR_BOUNDARY_NOT_CLOSED, "Door boundary nodes must form one closed unbranched loop."))
    if metrics["elements_inside_door_opening"] != 0:
        errors.append(_error("mesh.elements", MESH_TOPOLOGY_INVALID, "Elements remain inside the door opening."))
    if metrics["elements_crossing_door_boundary"] != 0:
        errors.append(_error("mesh.elements", MESH_TOPOLOGY_INVALID, "Elements cross the door boundary."))
    actual = metrics["door_boundary_nodes"]
    expected = metrics["door_expected_boundary_nodes"]
    if actual < 32 or actual < 0.70 * expected or actual > 1.80 * expected:
        errors.append(_error("DOOR_OPENING_NODES", MESH_SET_GENERATION_FAILED, f"Door boundary node count {actual} is inconsistent with expected {expected:.1f}."))
    global_size = float(getattr(controls, "global_size_mm", _dict_get(controls, "global_size_mm", 650.0)) or 650.0)
    door_size = float(getattr(controls, "door_size_mm", _dict_get(controls, "door_size_mm", 120.0)) or 120.0)
    if _finite(metrics["door_neighbor_edge_p50_mm"]) and metrics["door_neighbor_edge_p50_mm"] >= 0.35 * global_size:
        errors.append(_error("DOOR_NEIGHBOR_ELEMENTS", MESH_QUALITY_BELOW_THRESHOLD, "Door-neighbor p50 edge length is too coarse."))
    if _finite(metrics["door_neighbor_edge_p90_mm"]) and metrics["door_neighbor_edge_p90_mm"] >= 0.55 * global_size:
        errors.append(_error("DOOR_NEIGHBOR_ELEMENTS", MESH_QUALITY_BELOW_THRESHOLD, "Door-neighbor p90 edge length is too coarse."))
    if _finite(metrics["door_boundary_edge_p90_mm"]) and metrics["door_boundary_edge_p90_mm"] >= 1.50 * door_size:
        errors.append(_error("DOOR_OPENING_NODES", MESH_QUALITY_BELOW_THRESHOLD, "Door-boundary p90 edge length exceeds the local target."))
    if metrics["transition_band_count"] < 8:
        warnings.append(_error("mesh.transition", MESH_QUALITY_BELOW_THRESHOLD, "Too few transition bands were detected around the door."))
    elif _finite(metrics["transition_growth_p95"]) and metrics["transition_growth_p95"] > 1.50:
        warnings.append(_error("mesh.transition", MESH_QUALITY_BELOW_THRESHOLD, "Door transition growth p95 exceeds 1.50."))
    return {"errors": errors, "warnings": warnings, "metrics": metrics}


def compute_door_mesh_metrics(
    nodes: dict[int, tuple[float, float, float]],
    elements: dict[int, list[int]],
    sets: dict[str, Any],
    door_nodes: set[int] | list[int],
    door: DoorMeshInput | None,
    controls: Any,
) -> dict[str, Any]:
    door_node_set = set(door_nodes or [])
    base = {
        "door_boundary_nodes": len(door_node_set),
        "door_expected_boundary_nodes": 0.0,
        "door_boundary_loop_count": 0,
        "door_boundary_is_closed": False,
        "door_boundary_branch_nodes": 0,
        "elements_inside_door_opening": 0,
        "elements_crossing_door_boundary": 0,
        "door_boundary_edge_p50_mm": "not_evaluated_no_door",
        "door_boundary_edge_p90_mm": "not_evaluated_no_door",
        "door_neighbor_edge_p50_mm": "not_evaluated_no_door",
        "door_neighbor_edge_p90_mm": "not_evaluated_no_door",
        "door_neighbor_edge_max_mm": "not_evaluated_no_door",
        "global_region_edge_p50_mm": "not_evaluated_no_door",
        "transition_band_count": 0,
        "transition_monotonic_ratio": "not_evaluated_no_door",
        "transition_growth_p95": "not_evaluated_no_door",
        "geometry_hash": _hash_json([]),
        "connectivity_hash_normalized": _hash_json([]),
        "set_membership_hash_normalized": _hash_json([]),
    }
    if door is None or not door_node_set:
        return base
    r_ref = _radius_at_z(nodes, door.center_z_mm)
    door_size = float(getattr(controls, "door_size_mm", _dict_get(controls, "door_size_mm", 120.0)) or 120.0)
    margin_u = float(getattr(controls, "patch_margin_u_mm", _dict_get(controls, "patch_margin_u_mm", 1450.0)) or 1450.0)
    margin_z = float(getattr(controls, "patch_margin_z_mm", _dict_get(controls, "patch_margin_z_mm", 900.0)) or 900.0)
    perimeter = _door_perimeter(door)
    patch_perimeter = 2.0 * (door.width_mm + 2.0 * margin_u + door.height_mm + 2.0 * margin_z)
    base["door_expected_boundary_nodes"] = max(perimeter, patch_perimeter) / max(door_size, 1.0)
    boundary_edges = _boundary_edges(elements, door_node_set)
    degrees: dict[int, int] = {}
    for a, b in boundary_edges:
        degrees[a] = degrees.get(a, 0) + 1
        degrees[b] = degrees.get(b, 0) + 1
    base["door_boundary_branch_nodes"] = sum(1 for value in degrees.values() if value != 2)
    base["door_boundary_loop_count"] = _component_count(degrees, boundary_edges)
    base["door_boundary_is_closed"] = bool(degrees) and base["door_boundary_loop_count"] == 1 and base["door_boundary_branch_nodes"] == 0
    boundary_lengths = [_distance(nodes[a], nodes[b]) for a, b in boundary_edges if a in nodes and b in nodes]
    base["door_boundary_edge_p50_mm"] = _percentile(boundary_lengths, 0.50)
    base["door_boundary_edge_p90_mm"] = _percentile(boundary_lengths, 0.90)

    phi_by_node = {nid: _door_phi(nodes[nid], door, r_ref) for nid in nodes}
    inside = 0
    crossing = 0
    for conn in elements.values():
        phis = [phi_by_node[nid] for nid in conn]
        center_phi = _door_phi(_center(nodes, conn), door, r_ref)
        if center_phi < -1.0e-6:
            inside += 1
        if min(phis) < -1.0e-6 and max(phis) > 1.0e-6:
            crossing += 1
    base["elements_inside_door_opening"] = inside
    base["elements_crossing_door_boundary"] = crossing

    door_neighbor = set(sets.get("DOOR_NEIGHBOR_ELEMENTS") or sets.get("door_neighbor_elements") or [])
    door_patch = set(sets.get("DOOR_PATCH_ELEMENTS") or sets.get("door_patch_elements") or door_neighbor)
    neighbor_lengths = _edge_lengths_for_elements(nodes, elements, door_neighbor)
    base["door_neighbor_edge_p50_mm"] = _percentile(neighbor_lengths, 0.50)
    base["door_neighbor_edge_p90_mm"] = _percentile(neighbor_lengths, 0.90)
    base["door_neighbor_edge_max_mm"] = max(neighbor_lengths) if neighbor_lengths else "not_evaluated_empty_set"
    global_ids = set(elements) - door_patch
    base["global_region_edge_p50_mm"] = _percentile(_edge_lengths_for_elements(nodes, elements, global_ids), 0.50)

    bands = _transition_bands(nodes, elements, door_patch, door, r_ref, max(margin_u, margin_z))
    medians = [_percentile(values, 0.50) for values in bands if values]
    medians = [value for value in medians if _finite(value)]
    base["transition_band_count"] = len(medians)
    if len(medians) > 1:
        growth = [medians[i + 1] / medians[i] for i in range(len(medians) - 1) if medians[i] > 1.0e-9]
        base["transition_monotonic_ratio"] = sum(1 for item in growth if item >= 0.95) / len(growth) if growth else "not_evaluated_no_growth"
        base["transition_growth_p95"] = _percentile(growth, 0.95)
    base["geometry_hash"] = _hash_json(sorted((round(x, 6), round(y, 6), round(z, 6)) for x, y, z in nodes.values()))
    base["connectivity_hash_normalized"] = _hash_json(_normalized_connectivity(nodes, elements))
    base["set_membership_hash_normalized"] = _hash_json({key: len(value) for key, value in sorted(sets.items()) if isinstance(value, list)})
    return base


def _boundary_edges(elements: dict[int, list[int]], door_nodes: set[int]) -> list[tuple[int, int]]:
    edges = []
    for conn in elements.values():
        for a, b in zip(conn, conn[1:] + conn[:1]):
            if a in door_nodes and b in door_nodes:
                edges.append(tuple(sorted((a, b))))
    return sorted(set(edges))


def _component_count(degrees: dict[int, int], edges: list[tuple[int, int]]) -> int:
    graph: dict[int, set[int]] = {node: set() for node in degrees}
    for a, b in edges:
        graph.setdefault(a, set()).add(b)
        graph.setdefault(b, set()).add(a)
    seen: set[int] = set()
    count = 0
    for start in graph:
        if start in seen:
            continue
        count += 1
        stack = [start]
        seen.add(start)
        while stack:
            cur = stack.pop()
            for nxt in graph[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
    return count


def _door_phi(point: tuple[float, float, float], door: DoorMeshInput, r_ref: float) -> float:
    theta = math.atan2(point[1], point[0])
    center_theta = math.radians(float(getattr(door, "theta_center_deg", 0.0) or 0.0))
    dtheta = math.atan2(math.sin(theta - center_theta), math.cos(theta - center_theta))
    u = dtheta * r_ref
    z = point[2]
    rx = max(0.5 * door.width_mm, 1.0e-9)
    ry = max(0.5 * door.height_mm, 1.0e-9)
    return math.sqrt((u / rx) ** 2 + ((z - door.center_z_mm) / ry) ** 2) - 1.0


def _transition_bands(
    nodes: dict[int, tuple[float, float, float]],
    elements: dict[int, list[int]],
    eids: set[int],
    door: DoorMeshInput,
    r_ref: float,
    max_margin: float,
    band_count: int = 12,
) -> list[list[float]]:
    bands = [[] for _ in range(band_count)]
    if not eids:
        return bands
    scale = max(max_margin / max(0.5 * min(door.width_mm, door.height_mm), 1.0), 1.0)
    for eid in eids:
        conn = elements.get(eid)
        if not conn:
            continue
        center_phi = _door_phi(_center(nodes, conn), door, r_ref)
        if center_phi < 0.0:
            continue
        index = min(band_count - 1, int(center_phi / scale * band_count))
        bands[index].extend(_edge_lengths(nodes, conn))
    return bands


def _edge_lengths_for_elements(nodes: dict[int, tuple[float, float, float]], elements: dict[int, list[int]], eids: set[int]) -> list[float]:
    values: list[float] = []
    for eid in eids:
        conn = elements.get(eid)
        if conn:
            values.extend(_edge_lengths(nodes, conn))
    return values


def _edge_lengths(nodes: dict[int, tuple[float, float, float]], conn: list[int]) -> list[float]:
    return [_distance(nodes[a], nodes[b]) for a, b in zip(conn, conn[1:] + conn[:1])]


def _normalized_connectivity(nodes: dict[int, tuple[float, float, float]], elements: dict[int, list[int]]) -> list[Any]:
    coord_key = {nid: tuple(round(v, 5) for v in xyz) for nid, xyz in nodes.items()}
    return sorted(sorted(coord_key[nid] for nid in conn) for conn in elements.values())


def _door_perimeter(door: DoorMeshInput) -> float:
    a = 0.5 * door.width_mm
    b = 0.5 * door.height_mm
    if str(door.shape) == "rounded_rectangle":
        radius = min(a, b)
        return 2.0 * max(door.height_mm - 2.0 * radius, 0.0) + 2.0 * math.pi * radius
    h = ((a - b) ** 2) / max((a + b) ** 2, 1.0e-9)
    return math.pi * (a + b) * (1.0 + 3.0 * h / (10.0 + math.sqrt(4.0 - 3.0 * h)))


def _center(nodes: dict[int, tuple[float, float, float]], conn: list[int]) -> tuple[float, float, float]:
    return tuple(sum(nodes[nid][i] for nid in conn) / len(conn) for i in range(3))


def _radius_at_z(nodes: dict[int, tuple[float, float, float]], z: float) -> float:
    candidates = sorted(nodes.values(), key=lambda xyz: abs(xyz[2] - z))[:64]
    radii = [math.hypot(x, y) for x, y, _z in candidates]
    return median(radii) if radii else 1.0


def _distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def _percentile(values: list[float], pct: float) -> float | str:
    if not values:
        return "not_evaluated_empty_set"
    data = sorted(values)
    idx = min(len(data) - 1, max(0, int(round((len(data) - 1) * pct))))
    return data[idx]


def _hash_json(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _field(obj: Any, attr: str, key: str) -> Any:
    if hasattr(obj, attr):
        return getattr(obj, attr)
    if isinstance(obj, dict):
        return obj.get(attr, obj.get(key))
    return None


def _dict_get(obj: Any, key: str, default: Any) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else default


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def _error(field: str, code: str, message: str) -> dict[str, Any]:
    return {"field": field, "code": code, "message": message}
