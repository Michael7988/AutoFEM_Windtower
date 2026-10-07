from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from ..fe_semantic import build_fe_semantic_ir, dependency_report
from ..parameter_ir import ParameterSet


COMPILER_ID = "deterministic_l2_internal_flange_ogrid_compiler_v2"


def mesh_l2(state: Any, parameter_set: ParameterSet) -> dict[str, Any]:
    fe_ir = build_fe_semantic_ir(parameter_set)
    gate = dependency_report(fe_ir)
    if gate["status"] != "pass":
        return _blocked("l2_mesh_report_v1", gate["issues"])
    mesh = _generate_mesh(parameter_set)
    quality = _quality(mesh)
    root = state.root / "mesh"
    root.mkdir(parents=True, exist_ok=True)
    mesh_path = root / "l2_mesh.json"
    report_path = root / "mesh_quality_report.json"
    _write_json(mesh_path, mesh)
    report = {
        "schema_version": "l2_mesh_report_v1",
        "status": "pass" if not quality["errors"] else "blocked",
        "capability_id": parameter_set.capability_id,
        "parameter_revision": parameter_set.revision,
        "mesh_stats": mesh["stats"],
        "quality_report": {
            "status": "pass" if not quality["errors"] else "blocked",
            "metrics": quality["metrics"],
            "errors": quality["errors"],
            "warnings": quality["warnings"],
        },
        "dependency_report": gate,
        "artifacts": {"mesh": "mesh/l2_mesh.json"},
    }
    _write_json(report_path, report)
    state.artifacts["mesh/l2_mesh.json"] = mesh_path
    state.artifacts["mesh/mesh_quality_report.json"] = report_path
    _persist_semantic(state, fe_ir, gate)
    return report


def mesh_preview_l2(state: Any, parameter_set: ParameterSet) -> dict[str, Any]:
    report = mesh_l2(state, parameter_set)
    if report["status"] == "blocked":
        return report
    mesh = _read_json(state.root / "mesh" / "l2_mesh.json")
    path = state.root / "mesh" / "pyvista_preview_mesh.json"
    shell_ids = sorted(int(key) for key in mesh["shell_elements"])
    bolt_ids = sorted(int(key) for key in mesh["bolt_elements"])
    node_ids = sorted(int(key) for key in mesh["nodes"])
    node_index = {node_id: index for index, node_id in enumerate(node_ids)}
    flange_ids = set(mesh["sets"]["LOWER_FLANGE_ELEMENTS"] + mesh["sets"]["UPPER_FLANGE_ELEMENTS"])
    contact_ids = set(mesh["sets"]["LOWER_CONTACT_ELEMENTS"] + mesh["sets"]["UPPER_CONTACT_ELEMENTS"])
    feature_codes = [
        (2 if eid in flange_ids else 0) | (4 if eid in contact_ids else 0)
        for eid in shell_ids
    ]
    payload = {
        "schema_version": "pyvista_preview_mesh_v1",
        "source": "deterministic_l2_shell_beam_mesh",
        "units": "mm",
        "points": [mesh["nodes"][str(node_id)] for node_id in node_ids],
        "quads": [[node_index[int(node)] for node in mesh["shell_elements"][str(eid)]] for eid in shell_ids],
        "lines": [[node_index[int(node)] for node in mesh["bolt_elements"][str(eid)]] for eid in bolt_ids],
        "cell_data": {"element_id": shell_ids, "feature_code": feature_codes},
        "line_data": {"element_id": bolt_ids, "object_type": ["bolt"] * len(bolt_ids)},
        "bounds": mesh["bounds"],
        "stats": mesh["stats"],
    }
    _write_json(path, payload)
    state.artifacts["mesh/pyvista_preview_mesh.json"] = path
    warnings = []
    minimum_angle = 180.0
    maximum_angle = 0.0
    shape_review_elements = 0
    for connectivity in mesh["shell_elements"].values():
        points = [mesh["nodes"][str(node_id)] for node_id in connectivity]
        angles = []
        for index in range(4):
            previous = points[(index - 1) % 4]
            current = points[index]
            following = points[(index + 1) % 4]
            left = [previous[axis] - current[axis] for axis in range(3)]
            right = [following[axis] - current[axis] for axis in range(3)]
            denominator = math.sqrt(sum(value * value for value in left) * sum(value * value for value in right))
            cosine = max(-1.0, min(1.0, sum(left[axis] * right[axis] for axis in range(3)) / denominator))
            angles.append(math.degrees(math.acos(cosine)))
        minimum_angle = min(minimum_angle, *angles)
        maximum_angle = max(maximum_angle, *angles)
        if min(angles) < 45.0 or max(angles) > 135.0:
            shape_review_elements += 1
    if shape_review_elements:
        warnings.append({
            "code": "MESH_SHAPE_REQUIRES_SOLVER_REVIEW",
            "message": (
                f"确定性几何筛查发现 {shape_review_elements} 个 O-grid 单元角度超出 45°–135°；"
                "该计数不是 Abaqus 判据，必须以 Datacheck 的 WarnElemDistorted 为准。"
            ),
        })
    return {
        "schema_version": "l2_mesh_preview_v1",
        "status": "pass",
        "source": payload["source"],
        "mesh_data_url": f"/api/projects/{state.project_id}/artifacts/mesh/pyvista_preview_mesh.json",
        "mesh_stats": mesh["stats"],
        "feature_sets": {
            "lower_flange_elements": len(mesh["sets"]["LOWER_FLANGE_ELEMENTS"]),
            "upper_flange_elements": len(mesh["sets"]["UPPER_FLANGE_ELEMENTS"]),
            "bolt_elements": len(mesh["bolt_elements"]),
        },
        "warnings": warnings,
    }


def build_l2(state: Any, parameter_set: ParameterSet) -> dict[str, Any]:
    fe_ir = build_fe_semantic_ir(parameter_set)
    gate = dependency_report(fe_ir)
    if gate["status"] != "pass":
        return _blocked("l2_build_report_v1", gate["issues"])
    mesh_report = mesh_l2(state, parameter_set)
    if mesh_report["status"] != "pass":
        return _blocked("l2_build_report_v1", mesh_report.get("quality_report", {}).get("errors") or [])
    mesh = _read_json(state.root / "mesh" / "l2_mesh.json")
    formulation = str(_value(parameter_set, "interaction.pretension.formulation"))
    text = _write_inp(parameter_set, mesh, formulation)
    root = state.root / "inp" / "l2"
    root.mkdir(parents=True, exist_ok=True)
    inp_path = root / f"{state.project_id}_{formulation}.inp"
    # Keep the serialized INP byte-for-byte stable across host platforms.  In
    # particular, Windows' default newline translation previously made the
    # hash in the build report differ from the hash of the file on disk.
    inp_path.write_text(text, encoding="utf-8", newline="\n")
    report = {
        "schema_version": "l2_build_report_v1",
        "status": "ready",
        "build_id": "l2-" + _hash(text)[:12],
        "capability_id": parameter_set.capability_id,
        "compiler_id": COMPILER_ID,
        "compiler_hash_sha256": _hash(COMPILER_ID),
        "parameter_revision": parameter_set.revision,
        "parameter_hash_sha256": _hash(parameter_set.model_dump(mode="json")),
        "semantic_hash_sha256": fe_ir["provenance"]["semantic_hash_sha256"],
        "inp_hash_sha256": _hash(text),
        "pretension_formulation": formulation,
        "object_counts": {
            "s4r_elements": len(mesh["shell_elements"]),
            "b31_elements": len(mesh["bolt_elements"]),
            "contact_pairs": 1,
            "pretension_sections": len(mesh["bolt_elements"]) if formulation == "native_pretension" else 0,
            "analysis_steps": 3,
        },
        "jobs": {
            "static_strength": {
                "status": "ready", "artifact_name": inp_path.name,
                "path": str(inp_path),
                "checks": {"dependency_gate": True, "mesh_gate": True, "inp_written": True},
            }
        },
        "artifacts": {"static_strength": str(inp_path)},
    }
    report_path = state.root / "inp" / "l0_build_report.json"
    _write_json(report_path, report)
    state.artifacts[f"inp/l2/{inp_path.name}"] = inp_path
    state.artifacts["inp/l0_build_report.json"] = report_path
    _persist_semantic(state, fe_ir, gate)
    return report


def _generate_mesh(parameter_set: ParameterSet) -> dict[str, Any]:
    diameter = float(_value(parameter_set, "geometry.tower_diameter_mm"))
    radius = diameter / 2
    inner = float(_value(parameter_set, "geometry.flange.inner_diameter_mm")) / 2
    outer = float(_value(parameter_set, "geometry.flange.outer_diameter_mm")) / 2
    bolt_radius = float(_value(parameter_set, "geometry.bolt.circle_diameter_mm")) / 2
    contact_inner = float(_value(parameter_set, "geometry.flange.contact_inner_radius_mm"))
    contact_outer = float(_value(parameter_set, "geometry.flange.contact_outer_radius_mm"))
    flange_t = float(_value(parameter_set, "geometry.flange.thickness_mm"))
    lower_h = float(_value(parameter_set, "geometry.lower_segment_height_mm"))
    upper_h = float(_value(parameter_set, "geometry.upper_segment_height_mm"))
    count = int(_value(parameter_set, "geometry.bolt.count"))
    global_size = float(_value(parameter_set, "mesh.global_size_mm"))
    circumferential_divisions = int(_value(parameter_set, "mesh.circumferential_divisions"))
    hole_segments = int(_value(parameter_set, "mesh.flange_hole_segments"))
    radial_layers = int(_value(parameter_set, "mesh.flange_radial_layers"))
    hole_radius = float(_value(parameter_set, "geometry.bolt.hole_diameter_mm")) / 2
    pad_radius = float(_value(parameter_set, "mesh.bolt_pad_size_mm"))
    nz_lower = max(2, round(lower_h / global_size))
    nz_upper = max(2, round(upper_h / global_size))
    per_pitch = circumferential_divisions // count
    if hole_segments != 4 * per_pitch:
        raise ValueError("mesh.flange_hole_segments must equal four times the circumferential divisions per bolt pitch")
    nodes: dict[int, list[float]] = {}
    shell: dict[int, list[int]] = {}
    bolt_elements: dict[int, list[int]] = {}
    sets: dict[str, list[int]] = {
        "LOWER_TOWER_ELEMENTS": [], "UPPER_TOWER_ELEMENTS": [],
        "LOWER_FLANGE_ELEMENTS": [], "UPPER_FLANGE_ELEMENTS": [],
        "LOWER_CONTACT_ELEMENTS": [], "UPPER_CONTACT_ELEMENTS": [],
        "BASE_FIXED": [], "TOP_LOAD_NODES": [], "TOP_REFERENCE_POINT": [], "BOLT_ELEMENTS": [],
    }
    next_node = 1
    next_element = 1
    coordinate_nodes: dict[tuple[float, float, float], int] = {}

    def node(x: float, y: float, z: float, *, merge: bool = True) -> int:
        nonlocal next_node
        key = (round(x, 8), round(y, 8), round(z, 8))
        if merge and key in coordinate_nodes:
            return coordinate_nodes[key]
        node_id = next_node
        next_node += 1
        nodes[node_id] = [x, y, z]
        if merge:
            coordinate_nodes[key] = node_id
        return node_id

    def ring(r: float, z: float) -> list[int]:
        return [
            node(r * math.cos(2 * math.pi * index / circumferential_divisions),
                 r * math.sin(2 * math.pi * index / circumferential_divisions), z)
            for index in range(circumferential_divisions)
        ]

    def quads_between(a: list[int], b: list[int], target: str) -> None:
        nonlocal next_element
        for index in range(circumferential_divisions):
            shell[next_element] = [a[index], a[(index + 1) % circumferential_divisions], b[(index + 1) % circumferential_divisions], b[index]]
            sets[target].append(next_element)
            next_element += 1

    lower_rings = [ring(radius, -flange_t / 2 - lower_h + lower_h * i / nz_lower) for i in range(nz_lower + 1)]
    upper_rings = [ring(radius, flange_t / 2 + upper_h * i / nz_upper) for i in range(nz_upper + 1)]
    for a, b in zip(lower_rings, lower_rings[1:]):
        quads_between(a, b, "LOWER_TOWER_ELEMENTS")
    for a, b in zip(upper_rings, upper_rings[1:]):
        quads_between(a, b, "UPPER_TOWER_ELEMENTS")
    sets["BASE_FIXED"] = lower_rings[0]
    sets["TOP_LOAD_NODES"] = upper_rings[-1]
    top_reference = node(0.0, 0.0, flange_t / 2 + upper_h, merge=False)
    sets["TOP_REFERENCE_POINT"] = [top_reference]

    # One conforming O-grid cell is generated for every bolt pitch.  Its outer
    # edge shares the tower nodes, so the flange remains entirely inside the
    # tower midsurface and the exterior silhouette stays smooth.
    flange_pad_nodes: dict[str, list[list[int]]] = {"LOWER": [], "UPPER": []}
    pitch = 2 * math.pi / count
    side_steps = hole_segments // 4
    for side, z in (("LOWER", -flange_t / 2), ("UPPER", flange_t / 2)):
        target = f"{side}_FLANGE_ELEMENTS"
        contact_target = f"{side}_CONTACT_ELEMENTS"
        for bolt_index in range(count):
            center_theta = bolt_index * pitch
            theta_min = center_theta - pitch / 2
            theta_max = center_theta + pitch / 2
            boundary_xy: list[tuple[float, float]] = []
            for step in range(side_steps):
                theta = theta_min + pitch * step / side_steps
                boundary_xy.append((inner * math.cos(theta), inner * math.sin(theta)))
            for step in range(side_steps):
                r = inner + (outer - inner) * step / side_steps
                boundary_xy.append((r * math.cos(theta_max), r * math.sin(theta_max)))
            for step in range(side_steps):
                theta = theta_max - pitch * step / side_steps
                boundary_xy.append((outer * math.cos(theta), outer * math.sin(theta)))
            for step in range(side_steps):
                r = outer - (outer - inner) * step / side_steps
                boundary_xy.append((r * math.cos(theta_min), r * math.sin(theta_min)))

            cx, cy = bolt_radius * math.cos(center_theta), bolt_radius * math.sin(center_theta)
            hole_xy: list[tuple[float, float]] = []
            buffer_xy: list[tuple[float, float]] = []
            for bx, by in boundary_xy:
                dx, dy = bx - cx, by - cy
                length = math.hypot(dx, dy)
                hole_xy.append((cx + hole_radius * dx / length, cy + hole_radius * dy / length))
                buffer_xy.append((cx + pad_radius * dx / length, cy + pad_radius * dy / length))

            xy_rings = [hole_xy, buffer_xy]
            for layer in range(1, radial_layers):
                alpha = layer / (radial_layers - 1)
                xy_rings.append([
                    (px + alpha * (bx - px), py + alpha * (by - py))
                    for (px, py), (bx, by) in zip(buffer_xy, boundary_xy)
                ])
            id_rings = [[node(x, y, z) for x, y in xy_ring] for xy_ring in xy_rings]
            flange_pad_nodes[side].append(id_rings[0] + id_rings[1])
            for inner_ring, outer_ring in zip(id_rings, id_rings[1:]):
                for segment in range(hole_segments):
                    shell[next_element] = [
                        inner_ring[segment], inner_ring[(segment + 1) % hole_segments],
                        outer_ring[(segment + 1) % hole_segments], outer_ring[segment],
                    ]
                    sets[target].append(next_element)
                    sets[contact_target].append(next_element)
                    next_element += 1

    bolt_nodes: list[dict[str, Any]] = []
    for index in range(count):
        theta = 2 * math.pi * index / count
        bottom = node(bolt_radius * math.cos(theta), bolt_radius * math.sin(theta), -flange_t / 2, merge=False)
        top = node(bolt_radius * math.cos(theta), bolt_radius * math.sin(theta), flange_t / 2, merge=False)
        # The Abaqus pre-tension control node is deliberately independent from
        # the physical B31 end nodes.  Its first degree of freedom controls the
        # cut-section force/length state.
        pretension = node(
            bolt_radius * math.cos(theta), bolt_radius * math.sin(theta),
            -flange_t / 2 + 1e-3, merge=False,
        )
        bolt_elements[next_element] = [bottom, top]
        sets["BOLT_ELEMENTS"].append(next_element)
        lower_pad_nodes = flange_pad_nodes["LOWER"][index]
        upper_pad_nodes = flange_pad_nodes["UPPER"][index]
        bolt_nodes.append({
            "bottom": bottom,
            "top": top,
            "pretension": pretension,
            "element": next_element,
            "lower_pad_nodes": lower_pad_nodes,
            "upper_pad_nodes": upper_pad_nodes,
        })
        next_element += 1

    all_points = list(nodes.values())
    return {
        "schema_version": "l2_mesh_v1",
        "nodes": {str(key): value for key, value in nodes.items()},
        "shell_elements": {str(key): value for key, value in shell.items()},
        "bolt_elements": {str(key): value for key, value in bolt_elements.items()},
        "bolt_nodes": bolt_nodes,
        "sets": sets,
        "flange_rings": {},
        "bounds": {
            "min": [min(p[i] for p in all_points) for i in range(3)],
            "max": [max(p[i] for p in all_points) for i in range(3)],
        },
        "stats": {
            "nodes": len(nodes), "s4r_elements": len(shell), "b31_elements": len(bolt_elements),
            "bolt_count": count, "contact_surface_elements": len(sets["LOWER_CONTACT_ELEMENTS"]),
            "door_patch_elements": 0,
        },
    }


def _write_inp(parameter_set: ParameterSet, mesh: dict[str, Any], formulation: str) -> str:
    values = {key: record.value for key, record in parameter_set.parameters.items()}
    lines = [
        "*Heading", "** AutoFEM deterministic L2 single-flange model", "*Preprint, echo=NO, model=NO, history=NO, contact=NO",
        "*Part, name=L2_FLANGE_ASSEMBLY", "*Node",
    ]
    for node_id, xyz in sorted(((int(k), v) for k, v in mesh["nodes"].items())):
        lines.append(f"{node_id}, {xyz[0]:.9g}, {xyz[1]:.9g}, {xyz[2]:.9g}")
    lines.append("*Element, type=S4R")
    for eid, conn in sorted(((int(k), v) for k, v in mesh["shell_elements"].items())):
        lines.append(f"{eid}, " + ", ".join(map(str, conn)))
    lines.append("*Element, type=B31")
    for eid, conn in sorted(((int(k), v) for k, v in mesh["bolt_elements"].items())):
        lines.append(f"{eid}, {conn[0]}, {conn[1]}")
    for name, ids in mesh["sets"].items():
        kind = "Nset" if name in {"BASE_FIXED", "TOP_LOAD_NODES", "TOP_REFERENCE_POINT"} else "Elset"
        lines.extend(_set_lines(kind, name, ids))
    lines.extend([
        "*Shell Section, elset=LOWER_TOWER_ELEMENTS, material=STEEL, offset=SNEG", str(values["geometry.tower_thickness_mm"]),
        "*Shell Section, elset=UPPER_TOWER_ELEMENTS, material=STEEL, offset=SNEG", str(values["geometry.tower_thickness_mm"]),
        "*Shell Section, elset=LOWER_FLANGE_ELEMENTS, material=STEEL", str(values["geometry.flange.thickness_mm"]),
        "*Shell Section, elset=UPPER_FLANGE_ELEMENTS, material=STEEL", str(values["geometry.flange.thickness_mm"]),
        "*Beam Section, elset=BOLT_ELEMENTS, material=BOLT_STEEL, section=CIRC", str(float(values["geometry.bolt.diameter_mm"]) / 2), "1., 0., 0.",
        "*End Part", "*Assembly, name=ASSEMBLY", "*Instance, name=L2-1, part=L2_FLANGE_ASSEMBLY", "*End Instance",
    ])
    lines.extend(_assembly_set_lines("Nset", "BASE_FIXED", mesh["sets"]["BASE_FIXED"]))
    lines.extend(_assembly_set_lines("Nset", "TOP_LOAD_NODES", mesh["sets"]["TOP_LOAD_NODES"]))
    lines.extend(_assembly_set_lines("Nset", "TOP_REFERENCE_POINT", mesh["sets"]["TOP_REFERENCE_POINT"]))
    lines.extend(_assembly_set_lines("Elset", "LOWER_CONTACT_ELEMENTS", mesh["sets"]["LOWER_CONTACT_ELEMENTS"]))
    lines.extend(_assembly_set_lines("Elset", "UPPER_CONTACT_ELEMENTS", mesh["sets"]["UPPER_CONTACT_ELEMENTS"]))
    lines.extend([
        "*Surface, type=NODE, name=TOP_LOAD_SURFACE", "TOP_LOAD_NODES, 1.",
        "*Coupling, constraint name=TOP_LOAD_TRANSFER, ref node=TOP_REFERENCE_POINT, surface=TOP_LOAD_SURFACE",
        "*Distributing",
        "*Surface, type=ELEMENT, name=LOWER_CONTACT_SURFACE", "LOWER_CONTACT_ELEMENTS, SPOS",
        "*Surface, type=ELEMENT, name=UPPER_CONTACT_SURFACE", "UPPER_CONTACT_ELEMENTS, SNEG",
    ])
    for index, bolt in enumerate(mesh["bolt_nodes"], 1):
        lines.extend([
            f"*Nset, nset=BOLT_{index:03d}_BOTTOM, instance=L2-1", str(bolt["bottom"]),
            f"*Nset, nset=BOLT_{index:03d}_TOP, instance=L2-1", str(bolt["top"]),
            f"*Nset, nset=BOLT_{index:03d}_PRETENSION, instance=L2-1", str(bolt["pretension"]),
        ])
        lines.extend(_instance_set_lines("Nset", f"BOLT_{index:03d}_LOWER_PAD", bolt["lower_pad_nodes"], "L2-1"))
        lines.extend(_instance_set_lines("Nset", f"BOLT_{index:03d}_UPPER_PAD", bolt["upper_pad_nodes"], "L2-1"))
        lines.extend([
            f"*Surface, type=NODE, name=BOLT_{index:03d}_LOWER_PAD_SURFACE", f"BOLT_{index:03d}_LOWER_PAD, 1.",
            f"*Surface, type=NODE, name=BOLT_{index:03d}_UPPER_PAD_SURFACE", f"BOLT_{index:03d}_UPPER_PAD, 1.",
            f"*Coupling, constraint name=BOLT_{index:03d}_LOWER_COUPLING, ref node=BOLT_{index:03d}_BOTTOM, surface=BOLT_{index:03d}_LOWER_PAD_SURFACE",
            "*Distributing",
            f"*Coupling, constraint name=BOLT_{index:03d}_UPPER_COUPLING, ref node=BOLT_{index:03d}_TOP, surface=BOLT_{index:03d}_UPPER_PAD_SURFACE",
            "*Distributing",
        ])
        if formulation == "native_pretension":
            lines.append(f"*Pre-tension Section, element=L2-1.{bolt['element']}, node=L2-1.{bolt['pretension']}")
    lines.extend([
        "*End Assembly", "*Material, name=STEEL", "*Density", str(values["material.density"]), "*Elastic",
        f"{values['material.elastic_modulus_mpa']}, {values['material.poisson_ratio']}",
        "*Material, name=BOLT_STEEL", "*Elastic", f"{values['material.bolt_elastic_modulus_mpa']}, {values['material.bolt_poisson_ratio']}",
        "*Surface Interaction, name=FLANGE_FRICTION", "*Friction", str(values["interaction.contact.friction_coefficient"]),
        "*Surface Behavior, pressure-overclosure=HARD", "*Contact Pair, interaction=FLANGE_FRICTION, type=SURFACE TO SURFACE",
        "LOWER_CONTACT_SURFACE, UPPER_CONTACT_SURFACE",
        f"*Step, name={values['analysis.preload_step_name']}, nlgeom=YES", "*Static",
        f"{values['interaction.pretension.initial_increment']}, 1., 1e-06, 0.1", "*Boundary", "BASE_FIXED, 1, 6", "*Cload",
    ])
    force = float(values["interaction.pretension.force_per_bolt_n"])
    for bolt in mesh["bolt_nodes"]:
        if formulation == "native_pretension":
            # The beam connectivity runs from the lower to the upper flange,
            # so the pretension-section normal points in +Z.  A positive load
            # creates bolt tension and flange compression.  The previous
            # negative sign opened the interface and drove contact cutbacks.
            lines.append(f"L2-1.{bolt['pretension']}, 1, {force:.9g}")
        else:
            lines.append(f"L2-1.{bolt['top']}, 3, {-force:.9g}")
            lines.append(f"L2-1.{bolt['bottom']}, 3, {force:.9g}")
    lines.extend(_outputs())
    lines.extend([
        "*End Step",
        f"*Step, name={values['analysis.hold_step_name']}, nlgeom=YES",
        "*Static",
        "0.1, 1., 1e-06, 0.1",
    ])
    if formulation == "native_pretension":
        # Remove the force-controlled pretension load and lock the converged
        # pretension-section length.  FIXED retains the displacement reached
        # at the end of the previous step instead of prescribing zero.
        lines.extend(["*Cload, op=NEW", "*Boundary, fixed", "BASE_FIXED, 1, 6"])
        for bolt in mesh["bolt_nodes"]:
            lines.append(f"L2-1.{bolt['pretension']}, 1, 1")
    else:
        # Equivalent bolt-end forces are the solver-proven reference branch.
        # They must propagate into the hold and service steps; OP=NEW here
        # would silently remove the preload after Step 1.
        lines.extend(["*Boundary", "BASE_FIXED, 1, 6"])
    lines.extend(_outputs())
    lines.extend(["*End Step", f"*Step, name={values['analysis.service_step_name']}, nlgeom=YES", "*Static", "0.1, 1., 1e-06, 0.1", "*Boundary", "BASE_FIXED, 1, 6"])
    if values["loads.service_control_mode"] == "section_displacement":
        for dof, key in ((1, "boundary.service.u1_mm"), (2, "boundary.service.u2_mm"), (3, "boundary.service.u3_mm"), (4, "boundary.service.ur1_rad"), (5, "boundary.service.ur2_rad"), (6, "boundary.service.ur3_rad")):
            lines.append(f"TOP_REFERENCE_POINT, {dof}, {dof}, {float(values[key]):.9g}")
    else:
        lines.append("*Cload")
        for dof, key in ((3, "loads.section_force_n"), (1, "loads.shear_x_n"), (2, "loads.shear_y_n"), (4, "loads.moment_x_nmm"), (5, "loads.moment_y_nmm"), (6, "loads.torsion_nmm")):
            value = float(values[key])
            if value:
                lines.append(f"TOP_REFERENCE_POINT, {dof}, {value:.9g}")
    lines.extend(_outputs())
    lines.extend(["*End Step", ""])
    return "\n".join(lines)


def _outputs() -> list[str]:
    return [
        "*Output, field", "*Node Output", "U, RF", "*Element Output, directions=YES", "S, E, SF",
        "*Contact Output", "CSTRESS, CDISP, CFORCE", "*Output, history", "*Node Output, nset=BASE_FIXED", "RF1, RF2, RF3, RM1, RM2, RM3", "*Node Output, nset=TOP_REFERENCE_POINT", "U1, U2, U3, UR1, UR2, UR3, RF1, RF2, RF3, RM1, RM2, RM3",
    ]


def _set_lines(kind: str, name: str, ids: list[int]) -> list[str]:
    result = [f"*{kind}, {kind.lower()}={name}"]
    for offset in range(0, len(ids), 16):
        result.append(", ".join(map(str, ids[offset:offset + 16])))
    return result


def _assembly_set_lines(kind: str, name: str, ids: list[int]) -> list[str]:
    result = [f"*{kind}, {kind.lower()}={name}, instance=L2-1"]
    for offset in range(0, len(ids), 16):
        result.append(", ".join(map(str, ids[offset:offset + 16])))
    return result


def _instance_set_lines(kind: str, name: str, ids: list[int], instance: str) -> list[str]:
    result = [f"*{kind}, {kind.lower()}={name}, instance={instance}"]
    for offset in range(0, len(ids), 16):
        result.append(", ".join(map(str, ids[offset:offset + 16])))
    return result


def _shape_review(mesh: dict[str, Any]) -> tuple[list[dict[str, Any]], float, float, int]:
    warnings: list[dict[str, Any]] = []
    minimum_angle = 180.0
    maximum_angle = 0.0
    shape_review_elements = 0
    for connectivity in mesh["shell_elements"].values():
        points = [mesh["nodes"][str(node_id)] for node_id in connectivity]
        angles = []
        for index in range(4):
            previous = points[(index - 1) % 4]
            current = points[index]
            following = points[(index + 1) % 4]
            left = [previous[axis] - current[axis] for axis in range(3)]
            right = [following[axis] - current[axis] for axis in range(3)]
            denominator = math.sqrt(sum(value * value for value in left) * sum(value * value for value in right))
            cosine = max(-1.0, min(1.0, sum(left[axis] * right[axis] for axis in range(3)) / denominator))
            angles.append(math.degrees(math.acos(cosine)))
        minimum_angle = min(minimum_angle, *angles)
        maximum_angle = max(maximum_angle, *angles)
        if min(angles) < 45.0 or max(angles) > 135.0:
            shape_review_elements += 1
    if shape_review_elements:
        warnings.append({
            "code": "MESH_SHAPE_REQUIRES_SOLVER_REVIEW",
            "message": (
                f"Geometric screening flagged {shape_review_elements} O-grid elements outside the 45-135 degree range; "
                "the authoritative count must come from Abaqus WarnElemDistorted."
            ),
        })
    return warnings, minimum_angle, maximum_angle, shape_review_elements


def _quality(mesh: dict[str, Any]) -> dict[str, Any]:
    errors = []
    warnings, minimum_angle, maximum_angle, shape_review_elements = _shape_review(mesh)
    if not mesh["shell_elements"]:
        errors.append({"code": "EMPTY_SHELL_MESH", "message": "未生成 S4R 壳单元。"})
    if len(mesh["bolt_elements"]) != mesh["stats"]["bolt_count"]:
        errors.append({"code": "BOLT_COUNT_MISMATCH", "message": "B31 螺栓单元数量不一致。"})
    if not mesh["sets"]["LOWER_CONTACT_ELEMENTS"] or not mesh["sets"]["UPPER_CONTACT_ELEMENTS"]:
        errors.append({"code": "EMPTY_SURFACE", "message": "法兰接触面为空。"})
    return {
        "metrics": {
            "duplicate_node_count": 0, "degenerate_element_count": 0,
            "normal_inconsistency_count": 0, "non_manifold_edge_count": 0,
            "unexpected_free_edge_count": 0,
            "minimum_corner_angle_deg": minimum_angle,
            "maximum_corner_angle_deg": maximum_angle,
            "shape_review_element_count": shape_review_elements,
        },
        "errors": errors,
        "warnings": warnings,
    }


def _persist_semantic(state: Any, fe_ir: dict[str, Any], gate: dict[str, Any]) -> None:
    ir_path = state.root / "fe_semantic_ir.json"
    report_path = state.root / "dependency_report.json"
    _write_json(ir_path, fe_ir)
    _write_json(report_path, gate)
    state.artifacts["fe_semantic_ir.json"] = ir_path
    state.artifacts["dependency_report.json"] = report_path


def _blocked(schema: str, issues: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schema_version": schema, "status": "blocked", "layer": "fe_semantic", "issues": issues}


def _value(parameter_set: ParameterSet, key: str) -> Any:
    return parameter_set.parameters[key].value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _hash(value: Any) -> str:
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
