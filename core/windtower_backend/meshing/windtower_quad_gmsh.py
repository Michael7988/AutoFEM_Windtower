from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from .models import STRATEGY, DoorMeshInput, MeshResult, TowerMeshInput, normalize_controls, normalize_door, normalize_tower_points
from .quality import compute_quality
from .sets import build_mesh_sets, segments_from_centers


class ParamQuadMesh:
    def __init__(self, width: float, tower: list[TowerMeshInput]) -> None:
        self.width = width
        self.tower = tower
        self.param_nodes: dict[int, tuple[float, float]] = {}
        self.node_key: dict[tuple[Any, ...], int] = {}
        self.elements: dict[int, list[int]] = {}
        self.next_node = 1
        self.next_element = 1

    def add_node(self, u: float, z: float) -> int:
        if abs(abs(u) - 0.5 * self.width) < 1.0e-6:
            key: tuple[Any, ...] = ("seam", round(z, 6))
            u = -0.5 * self.width
        else:
            key = (round(u, 6), round(z, 6))
        if key in self.node_key:
            return self.node_key[key]
        nid = self.next_node
        self.next_node += 1
        self.node_key[key] = nid
        self.param_nodes[nid] = (u, z)
        return nid

    def add_quad(self, n1: int, n2: int, n3: int, n4: int) -> int | None:
        if len({n1, n2, n3, n4}) < 4:
            return None
        eid = self.next_element
        self.next_element += 1
        self.elements[eid] = [n1, n2, n3, n4]
        return eid

    def to_xyz_nodes(self, r_ref: float) -> dict[int, tuple[float, float, float]]:
        nodes = {}
        for nid, (u, z) in self.param_nodes.items():
            radius = 0.5 * interp_diameter(self.tower, z)
            theta = u / r_ref
            nodes[nid] = (radius * math.cos(theta), radius * math.sin(theta), z)
        return nodes


def build_windtower_quad_mesh(
    tower_sections: list[dict[str, Any]],
    door: dict[str, Any] | DoorMeshInput | None,
    flanges: list[dict[str, Any]] | None = None,
    controls: dict[str, Any] | None = None,
    msh_path: str | Path | None = None,
) -> MeshResult:
    tower = normalize_tower_points(tower_sections)
    if len(tower) < 2:
        raise ValueError("At least two tower profile points are required for wind-tower meshing.")
    mesh_controls = normalize_controls(controls)
    door_input = door if isinstance(door, DoorMeshInput) else normalize_door(door or {})
    if door_input is None or door_input.shape not in {"ellipse", "rounded_rectangle"}:
        nodes, elements, door_nodes = build_no_door_mesh(tower, mesh_controls.global_size_mm)
        r_ref = 0.5 * max(item.diameter_mm for item in tower)
    else:
        r_ref = 0.5 * interp_diameter(tower, door_input.center_z_mm)
        nodes, elements, door_nodes = build_local_refined_ellipse_mesh(tower, door_input, mesh_controls, r_ref)
    element_segments = segments_from_centers(nodes, elements, tower)
    sets, surfaces = build_mesh_sets(nodes, elements, door_nodes, door_input, mesh_controls)
    quality = compute_quality(nodes, elements, sets, door_input, mesh_controls)
    artifacts: dict[str, str] = {}
    warnings = list(quality.warnings)
    if msh_path:
        try:
            write_gmsh_msh(Path(msh_path), nodes, elements)
            artifacts["msh"] = str(msh_path)
        except Exception as exc:
            warnings.append({"field": "gmsh", "message": f"Gmsh .msh export failed: {exc}"})
            quality.warnings = warnings
            quality.status = "warning" if not quality.blocked else quality.status
    return MeshResult(
        strategy=STRATEGY,
        nodes=nodes,
        elements=elements,
        element_segments=element_segments,
        sets=sets,
        surfaces=surfaces,
        quality_report=quality,
        artifacts=artifacts,
        metadata={
            "r_ref_mm": r_ref,
            "global_size_mm": mesh_controls.global_size_mm,
            "door_size_mm": mesh_controls.door_size_mm,
            "hole_layers": mesh_controls.hole_layers,
            "flanges_count": len(flanges or []),
        },
    )


def build_local_refined_ellipse_mesh(
    tower: list[TowerMeshInput], door: DoorMeshInput, controls: Any, r_ref: float
) -> tuple[dict[int, tuple[float, float, float]], dict[int, list[int]], set[int]]:
    width = 2.0 * math.pi * r_ref
    mesh = ParamQuadMesh(width, tower)
    (u0, u1, z0, z1), side = build_patch(mesh, door, controls, width)
    w0 = -0.5 * width
    w1 = 0.5 * width
    height = tower[-1].z_mm

    bottom_us = [w0] + graded_values(w0, u0, controls.global_size_mm, include_a=False, include_b=False)
    bottom_us += [mesh.param_nodes[n][0] for n in side["bottom"]]
    bottom_us += graded_values(u1, w1, controls.global_size_mm, include_a=False, include_b=False) + [w1]
    bottom_us = sorted(set(round(u, 6) for u in bottom_us))
    bottom_zs = graded_values(0.0, z0, controls.global_size_mm)
    add_structured_block(mesh, [bottom_us for _ in bottom_zs], bottom_zs)

    top_us = [w0] + graded_values(w0, u0, controls.global_size_mm, include_a=False, include_b=False)
    top_us += sorted(mesh.param_nodes[n][0] for n in side["top"])
    top_us += graded_values(u1, w1, controls.global_size_mm, include_a=False, include_b=False) + [w1]
    top_us = sorted(set(round(u, 6) for u in top_us))
    top_zs = graded_values(z1, height, controls.global_size_mm)
    add_structured_block(mesh, [top_us for _ in top_zs], top_zs)

    left_zs = sorted(set(round(mesh.param_nodes[n][1], 6) for n in side["left"]))
    right_zs = sorted(set(round(mesh.param_nodes[n][1], 6) for n in side["right"]))
    add_structured_block(mesh, [graded_values(w0, u0, controls.global_size_mm) for _ in left_zs], left_zs)
    add_structured_block(mesh, [graded_values(u1, w1, controls.global_size_mm) for _ in right_zs], right_zs)

    return mesh.to_xyz_nodes(r_ref), mesh.elements, set(side["door"])


def build_no_door_mesh(
    tower: list[TowerMeshInput], target_size_mm: float
) -> tuple[dict[int, tuple[float, float, float]], dict[int, list[int]], set[int]]:
    max_diameter = max(item.diameter_mm for item in tower)
    ntheta = max(16, int(math.ceil(math.pi * max_diameter / target_size_mm / 8.0) * 8))
    z_values = make_z_grid(tower, target_size_mm)
    nodes: dict[int, tuple[float, float, float]] = {}
    node_key: dict[tuple[int, int], int] = {}
    nid = 1
    for iz, z in enumerate(z_values):
        radius = 0.5 * interp_diameter(tower, z)
        for it in range(ntheta):
            theta = -math.pi + 2.0 * math.pi * it / ntheta
            node_key[(iz, it)] = nid
            nodes[nid] = (radius * math.cos(theta), radius * math.sin(theta), z)
            nid += 1
    elements: dict[int, list[int]] = {}
    eid = 1
    for iz in range(len(z_values) - 1):
        for it in range(ntheta):
            it2 = (it + 1) % ntheta
            elements[eid] = [node_key[(iz, it)], node_key[(iz, it2)], node_key[(iz + 1, it2)], node_key[(iz + 1, it)]]
            eid += 1
    return nodes, elements, set()


def build_patch(mesh: ParamQuadMesh, door: DoorMeshInput, controls: Any, width: float) -> tuple[tuple[float, float, float, float], dict[str, list[int]]]:
    cx = 0.0
    cy = door.center_z_mm
    rx = 0.5 * door.width_mm
    ry = 0.5 * door.height_mm
    rx_frame = max(rx, 0.5 * door.outer_width_mm) if door.reinforcement_enabled else rx
    ry_frame = max(ry, 0.5 * door.outer_height_mm) if door.reinforcement_enabled else ry
    u0 = cx - rx - controls.patch_margin_u_mm
    u1 = cx + rx + controls.patch_margin_u_mm
    z0 = max(0.0, cy - ry - controls.patch_margin_z_mm)
    z1 = min(mesh.tower[-1].z_mm, cy + ry + controls.patch_margin_z_mm)

    n_bottom = max(16, int(math.ceil((u1 - u0) / controls.door_size_mm)))
    n_right = max(20, int(math.ceil((z1 - z0) / controls.door_size_mm)))
    n_top = n_bottom
    n_left = n_right
    outer_points = []
    for i in range(n_bottom):
        t = i / n_bottom
        outer_points.append((u0 + (u1 - u0) * t, z0))
    for i in range(n_right):
        t = i / n_right
        outer_points.append((u1, z0 + (z1 - z0) * t))
    for i in range(n_top):
        t = i / n_top
        outer_points.append((u1 - (u1 - u0) * t, z1))
    for i in range(n_left):
        t = i / n_left
        outer_points.append((u0, z1 - (z1 - z0) * t))

    ring_points: list[list[tuple[float, float]]] = []
    radial_layers = max(int(controls.hole_layers), 1)
    frame_layers = 0
    if door.reinforcement_enabled and (rx_frame > rx or ry_frame > ry):
        frame_layers = max(
            1,
            int(math.ceil(max(rx_frame - rx, ry_frame - ry) / controls.door_size_mm)),
        )
        radial_layers = max(radial_layers, frame_layers + 1)
    for k in range(radial_layers + 1):
        row: list[tuple[float, float]] = []
        for u_outer, z_outer in outer_points:
            angle = math.atan2(z_outer - cy, u_outer - cx)
            u_inner, z_inner = door_boundary_point(cx, cy, rx, ry, angle, door.shape)
            if frame_layers and k <= frame_layers:
                u_frame, z_frame = door_boundary_point(cx, cy, rx_frame, ry_frame, angle, door.shape)
                t = k / frame_layers
                row.append((u_inner + (u_frame - u_inner) * t, z_inner + (z_frame - z_inner) * t))
            else:
                u_start, z_start = (
                    door_boundary_point(cx, cy, rx_frame, ry_frame, angle, door.shape)
                    if frame_layers
                    else (u_inner, z_inner)
                )
                denominator = max(radial_layers - frame_layers, 1)
                t = ((k - frame_layers) / denominator) ** 0.85
                row.append((u_start + (u_outer - u_start) * t, z_start + (z_outer - z_start) * t))
        ring_points.append(row)
    n = len(outer_points)

    # One deterministic Jacobi pass.  Both geometric boundaries remain fixed;
    # only interior transition-ring nodes receive alpha=0.5 relaxation.
    if radial_layers > 1:
        original = ring_points
        smoothed = [list(row) for row in original]
        alpha = 0.5
        fixed_layers = {frame_layers} if frame_layers else set()
        for k in range(1, radial_layers):
            if k in fixed_layers:
                continue
            for j in range(n):
                neighbors = (
                    original[k - 1][j], original[k + 1][j],
                    original[k][(j - 1) % n], original[k][(j + 1) % n],
                )
                target_u = sum(point[0] for point in neighbors) / 4.0
                target_z = sum(point[1] for point in neighbors) / 4.0
                u, z = original[k][j]
                smoothed[k][j] = (u + alpha * (target_u - u), z + alpha * (target_z - z))
        ring_points = smoothed

    ring_nodes = [
        [mesh.add_node(u, z) for u, z in row]
        for row in ring_points
    ]
    for k in range(radial_layers):
        for j in range(n):
            mesh.add_quad(
                ring_nodes[k][(j + 1) % n], ring_nodes[k][j],
                ring_nodes[k + 1][j], ring_nodes[k + 1][(j + 1) % n],
            )
    side = {
        "bottom": ring_nodes[-1][0 : n_bottom + 1],
        "right": ring_nodes[-1][n_bottom : n_bottom + n_right + 1],
        "top": ring_nodes[-1][n_bottom + n_right : n_bottom + n_right + n_top + 1],
        "left": ring_nodes[-1][n_bottom + n_right + n_top :] + [ring_nodes[-1][0]],
        "door": ring_nodes[0],
        "reinforcement_outer": ring_nodes[frame_layers] if frame_layers else [],
    }
    return (u0, u1, z0, z1), side


def door_boundary_point(cx: float, cy: float, rx: float, ry: float, angle: float, shape: str) -> tuple[float, float]:
    ca = math.cos(angle)
    sa = math.sin(angle)
    if shape == "rounded_rectangle":
        radius = max(rx, 1.0)
        straight = max(0.0, 2.0 * ry - 2.0 * radius)
        y_rect = 0.5 * straight
        if abs(sa) < 1.0e-10:
            return (cx + math.copysign(rx, ca), cy)
        if sa > 0.0:
            # Intersection of ray and top half-circle.
            b = -2.0 * y_rect * sa
            c = y_rect * y_rect - radius * radius
            distance = 0.5 * (-b + math.sqrt(max(0.0, b * b - 4.0 * c)))
            return (cx + distance * ca, cy + distance * sa)
        b = 2.0 * y_rect * sa
        c = y_rect * y_rect - radius * radius
        distance = 0.5 * (-b + math.sqrt(max(0.0, b * b - 4.0 * c)))
        return (cx + distance * ca, cy + distance * sa)
    scale = 1.0 / math.sqrt((ca / rx) ** 2 + (sa / ry) ** 2)
    return (cx + scale * ca, cy + scale * sa)


def add_structured_block(mesh: ParamQuadMesh, u_lines_by_row: list[list[float]], z_lines: list[float]) -> list[list[int]]:
    rows = []
    for iz, z in enumerate(z_lines):
        row = [mesh.add_node(u, z) for u in u_lines_by_row[iz]]
        rows.append(row)
    for iz in range(len(rows) - 1):
        for iu in range(len(rows[iz]) - 1):
            mesh.add_quad(rows[iz][iu], rows[iz][iu + 1], rows[iz + 1][iu + 1], rows[iz + 1][iu])
    return rows


def graded_values(a: float, b: float, target: float, include_a: bool = True, include_b: bool = True) -> list[float]:
    n = max(1, int(math.ceil(abs(b - a) / target)))
    vals = [a + (b - a) * i / n for i in range(n + 1)]
    if not include_a:
        vals = vals[1:]
    if not include_b:
        vals = vals[:-1]
    return vals


def make_z_grid(tower: list[TowerMeshInput], target_z_mm: float) -> list[float]:
    z_values = []
    for a, b in zip(tower[:-1], tower[1:]):
        length = b.z_mm - a.z_mm
        n = max(1, int(math.ceil(length / target_z_mm)))
        if not z_values:
            z_values.append(a.z_mm)
        for k in range(1, n + 1):
            z_values.append(a.z_mm + length * k / n)
    return z_values


def interp_diameter(tower: list[TowerMeshInput], z_mm: float) -> float:
    if z_mm <= tower[0].z_mm:
        return tower[0].diameter_mm
    for a, b in zip(tower[:-1], tower[1:]):
        if a.z_mm <= z_mm <= b.z_mm:
            dz = b.z_mm - a.z_mm
            if abs(dz) < 1.0e-9:
                return a.diameter_mm
            t = (z_mm - a.z_mm) / dz
            return a.diameter_mm + t * (b.diameter_mm - a.diameter_mm)
    return tower[-1].diameter_mm


def write_gmsh_msh(path: Path, nodes: dict[int, tuple[float, float, float]], elements: dict[int, list[int]]) -> None:
    import gmsh

    gmsh.initialize()
    try:
        gmsh.model.add("windtower_quad_gmsh_v1")
        gmsh.model.addDiscreteEntity(2, 1)
        node_tags = sorted(nodes)
        coords = []
        for nid in node_tags:
            coords.extend(nodes[nid])
        gmsh.model.mesh.addNodes(2, 1, node_tags, coords)
        elem_tags = sorted(elements)
        conn = []
        for eid in elem_tags:
            conn.extend(elements[eid])
        gmsh.model.mesh.addElements(2, 1, [3], [elem_tags], [conn])
        path.parent.mkdir(parents=True, exist_ok=True)
        gmsh.write(str(path))
    finally:
        gmsh.finalize()
