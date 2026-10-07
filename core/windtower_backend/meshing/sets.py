from __future__ import annotations

import math
from typing import Any

from .models import DoorMeshInput, TowerMeshInput

LOAD_DIRECTION = (1.0, 0.0, 0.0)


def element_center(nodes: dict[int, tuple[float, float, float]], conn: list[int]) -> tuple[float, float, float]:
    n = len(conn)
    return tuple(sum(nodes[nid][i] for nid in conn) / n for i in range(3))


def segment_for_z(tower: list[TowerMeshInput], z_mm: float) -> int:
    for i, (a, b) in enumerate(zip(tower[:-1], tower[1:]), start=1):
        if a.z_mm - 1.0e-6 <= z_mm <= b.z_mm + 1.0e-6:
            return i
    return max(len(tower) - 1, 1)


def segments_from_centers(
    nodes: dict[int, tuple[float, float, float]], elements: dict[int, list[int]], tower: list[TowerMeshInput]
) -> dict[int, int]:
    return {eid: segment_for_z(tower, element_center(nodes, conn)[2]) for eid, conn in elements.items()}


def build_mesh_sets(
    nodes: dict[int, tuple[float, float, float]],
    elements: dict[int, list[int]],
    door_nodes: set[int],
    door: DoorMeshInput | None,
    controls: Any,
) -> tuple[dict[str, Any], dict[str, dict[str, list[int]]]]:
    min_z = min(xyz[2] for xyz in nodes.values())
    max_z = max(xyz[2] for xyz in nodes.values())
    base_nodes = [nid for nid, xyz in nodes.items() if abs(xyz[2] - min_z) < 1.0e-6]
    top_nodes = [nid for nid, xyz in nodes.items() if abs(xyz[2] - max_z) < 1.0e-6]
    wind_spos, wind_sneg = windward_surface_groups(nodes, elements)
    door_patch = _door_patch_elements(nodes, elements, door, controls)
    reinforcement = door_reinforcement_elements(nodes, elements, door)
    node_to_eids: dict[int, set[int]] = {}
    for eid, conn in elements.items():
        for nid in conn:
            node_to_eids.setdefault(nid, set()).add(eid)
    door_neighbor = set(door_patch)
    for nid in door_nodes:
        door_neighbor.update(node_to_eids.get(nid, set()))
    reinforcement_nodes = {nid for eid in reinforcement for nid in elements.get(eid, [])}
    reinforcement_set = set(reinforcement)
    reinforcement_adjacent = {
        eid
        for nid in reinforcement_nodes
        for eid in node_to_eids.get(nid, set())
        if eid not in reinforcement_set
    }
    sets = {
        "BASE_FIXED": sorted(base_nodes),
        "TOP_LOAD_NODES": sorted(top_nodes),
        "DOOR_OPENING_NODES": sorted(door_nodes),
        "DOOR_PATCH_ELEMENTS": sorted(set(door_patch)),
        "DOOR_NEIGHBOR_ELEMENTS": sorted(door_neighbor),
        "DOOR_REINFORCEMENT_ELEMENTS": sorted(reinforcement),
        "DOOR_REINFORCEMENT_ADJACENT_ELEMENTS": sorted(reinforcement_adjacent),
        "DOOR_REINFORCEMENT_CHECK_ELEMENTS": sorted(reinforcement_set | reinforcement_adjacent),
        "WINDWARD_SPOS_ELEMENTS": sorted(wind_spos),
        "WINDWARD_SNEG_ELEMENTS": sorted(wind_sneg),
    }
    surfaces = {"WINDWARD_SURFACE": {"SPOS": sorted(wind_spos), "SNEG": sorted(wind_sneg)}}
    return sets, surfaces


def windward_surface_groups(
    nodes: dict[int, tuple[float, float, float]],
    elements: dict[int, list[int]],
    load_direction: tuple[float, float, float] = LOAD_DIRECTION,
) -> tuple[list[int], list[int]]:
    load_direction = normalize(load_direction)
    if abs(load_direction[2]) > 1.0e-9 or math.hypot(load_direction[0], load_direction[1]) < 1.0e-12:
        raise ValueError("Wind direction must be a non-zero horizontal vector.")
    spos = []
    sneg = []
    for eid, conn in sorted(elements.items()):
        normal = orient_shell_normal_outward(nodes, conn)
        if dot(normal, load_direction) <= 0.0:
            continue
        raw_normal = element_normal(nodes, conn)
        if dot(raw_normal, load_direction) >= 0.0:
            sneg.append(eid)
        else:
            spos.append(eid)
    return spos, sneg


def door_reinforcement_elements(
    nodes: dict[int, tuple[float, float, float]],
    elements: dict[int, list[int]],
    door: DoorMeshInput | None,
) -> list[int]:
    if not door or not door.reinforcement_enabled:
        return []
    rx_inner = 0.5 * door.width_mm
    ry_inner = 0.5 * door.height_mm
    rx_outer = 0.5 * door.outer_width_mm
    ry_outer = 0.5 * door.outer_height_mm
    if min(rx_inner, ry_inner) <= 0.0 or rx_outer <= rx_inner or ry_outer <= ry_inner:
        return []
    center_z = door.center_z_mm
    theta = math.radians(door.theta_center_deg)
    radial = (math.cos(theta), math.sin(theta), 0.0)
    tangent = (-math.sin(theta), math.cos(theta), 0.0)
    result = []
    for eid, conn in elements.items():
        center = element_center(nodes, conn)
        radius = math.hypot(center[0], center[1])
        surface_point = (center[0], center[1], 0.0)
        signed_u = radius * math.atan2(dot(surface_point, tangent), dot(surface_point, radial))
        dz = center[2] - center_z
        inside_outer = (signed_u / rx_outer) ** 2 + (dz / ry_outer) ** 2 < 1.0 - 1.0e-8
        outside_inner = (signed_u / rx_inner) ** 2 + (dz / ry_inner) ** 2 > 1.0 + 1.0e-8
        if inside_outer and outside_inner:
            result.append(eid)
    return sorted(result)


def _door_patch_elements(
    nodes: dict[int, tuple[float, float, float]], elements: dict[int, list[int]], door: DoorMeshInput | None, controls: Any
) -> list[int]:
    if door is None:
        return []
    z0 = door.bottom_z_mm - float(controls.patch_margin_z_mm)
    z1 = door.bottom_z_mm + door.height_mm + float(controls.patch_margin_z_mm)
    return [eid for eid, conn in elements.items() if z0 <= element_center(nodes, conn)[2] <= z1]


def element_normal(nodes: dict[int, tuple[float, float, float]], conn: list[int]) -> tuple[float, float, float]:
    p1 = nodes[conn[0]]
    p2 = nodes[conn[1]]
    p4 = nodes[conn[-1]]
    return normalize(cross(sub(p2, p1), sub(p4, p1)))


def orient_shell_normal_outward(nodes: dict[int, tuple[float, float, float]], conn: list[int]) -> tuple[float, float, float]:
    normal = element_normal(nodes, conn)
    center = element_center(nodes, conn)
    radial = normalize((center[0], center[1], 0.0))
    if dot(normal, radial) < 0.0:
        return (-normal[0], -normal[1], -normal[2])
    return normal


def dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def sub(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def normalize(v: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(dot(v, v))
    if length < 1.0e-12:
        return (0.0, 0.0, 0.0)
    return (v[0] / length, v[1] / length, v[2] / length)
